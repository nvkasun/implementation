#!/usr/bin/env python3
"""automation/phases/phase3/argocd_state.py: read-only Argo CD ownership-safety preflight classifier -- answers exactly one question, "is it safe for MAIN to reconcile this Argo CD installation?", as one of ABSENT/OWNED/BROKEN. This is NOT a HEALTHY-skip prerequisite classifier: Argo CD's own desired state (core replicas, ecrTokenSync repositories, argocdServerIngress.enabled, image/tag, any future wrapper-chart resource) may legitimately change on every run, so OWNED (not HEALTHY) is the "safe to reconcile" state -- readiness/exact-desired-state acceptance is validated separately, post-reconciliation, by automation/phases/phase3/argocd_acceptance.py. Generic by design: this module checks OWNERSHIP LABELS on whatever currently exists, never the correctness/readiness of any individual resource -- adding a new values-driven chart resource, or flipping an existing one false<->true, never requires touching this file. Never mutates the cluster: every kubectl invocation here is a `get` (read-only); no apply/create/delete/patch/annotate/label/helm call exists in this module. Consumes environment identity through automation/goldengate-environment.py, never a second environment parser."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

_ENVIRONMENT_MODULE_PATH = REPO_ROOT / "automation" / "goldengate-environment.py"
_environment_module = None


def _load_environment_module():
    """Lazy import of automation/goldengate-environment.py -- the single canonical environment-config parser/deriver. Never a second independent schema implementation."""
    global _environment_module
    if _environment_module is None:
        spec = importlib.util.spec_from_file_location("goldengate_environment", _ENVIRONMENT_MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _environment_module = module
    return _environment_module


def environment_derived_values(environment):
    """Loads+validates+derives envs/<environment>/environment.yaml via the canonical resolver."""
    env_module = _load_environment_module()
    env_module.REPO_ROOT = REPO_ROOT
    doc = env_module.load_environment_config(environment)
    return env_module.derive_values(doc)


STATE_ABSENT = "ABSENT"
STATE_OWNED = "OWNED"
STATE_BROKEN = "BROKEN"

# Current chart/naming contract (helm/argocd, its vendored argo-cd/ dependency) -- verified against the real vendored chart's rendered output, never guessed. Only used here to name the resources this module inspects for OWNERSHIP; individual field correctness (readiness, image, IRSA role-arn, Secret URL, Ingress contract) is deliberately out of scope -- that is argocd_acceptance.py's job.
ARGOCD_RELEASE_NAME = "argocd"

REQUIRED_CRDS = (
    "applications.argoproj.io",
    "appprojects.argoproj.io",
    "applicationsets.argoproj.io",
)

CORE_DEPLOYMENTS = (
    "argocd-server",
    "argocd-repo-server",
    "argocd-redis",
    "argocd-applicationset-controller",
    "argocd-notifications-controller",
)

CORE_STATEFULSETS = ("argocd-application-controller",)

CORE_SERVICES = ("argocd-server", "argocd-repo-server", "argocd-redis")

ECR_TOKEN_SYNC_NAME = "argocd-ecr-token-sync"

# Repository Secret names this CronJob may create -- checked for ownership only if/when they already exist; a missing one is never itself a reason (that is exactly the deterministic drift 20-sub-argocd.yaml's own bounded token-sync validation repairs on every reconcile).
REPOSITORY_SECRET_NAMES = (
    "argocd-ecr-goldengate-oci",
    "argocd-ecr-goldengate-monitor-oci",
    "argocd-ecr-goldengate-platform-oci",
    "argocd-ecr-amazon-cloudwatch-observability-oci",
)

INGRESS_NAME = "argocd-server-ingress"

# Labels rendered by helm/argocd/templates/argocd-server-ingress.yaml itself -- the ownership proof used to decide whether an existing same-name Ingress is safe to treat as Argo/Helm-owned (OWNED-eligible) versus foreign/ambiguous (always BROKEN, never automatically taken over). Exact field-level drift (host/group/certificate/scheme/etc.) is checked only in argocd_acceptance.py.
INGRESS_OWNERSHIP_LABELS = {
    "app.kubernetes.io/name": "argocd-server",
    "app.kubernetes.io/part-of": "argocd",
}


class ClassifierInspectionError(Exception):
    """Raised when the classifier could not determine truth -- API unreachable, permission denied, malformed JSON. Never conflated with ABSENT; callers must fail closed on this, not treat it as "not installed"."""


class KubectlRunner:
    """Read-only kubectl wrapper. Every call site in this module passes only a `get` subcommand -- never apply/create/delete/patch/annotate/label."""

    def __init__(self, kubectl_bin="kubectl"):
        self.kubectl_bin = kubectl_bin

    def __call__(self, args):
        proc = subprocess.run([self.kubectl_bin, *args], capture_output=True, text=True)
        return proc.returncode, proc.stdout, proc.stderr


def _get_json(run, resource, name=None, namespace=None):
    """Runs `kubectl get <resource> [name] [-n namespace] -o json` (read-only). Returns (True, obj) if found, (False, None) if the API server reported NotFound, and raises ClassifierInspectionError for any other failure (auth, connectivity, malformed JSON) -- "could not tell" is never silently treated as "absent"."""
    args = ["get", resource]
    if name:
        args.append(name)
    if namespace:
        args += ["-n", namespace]
    args += ["-o", "json"]
    rc, out, err = run(args)
    if rc == 0:
        try:
            return True, json.loads(out)
        except json.JSONDecodeError as exc:
            raise ClassifierInspectionError(f"kubectl get {resource} {name or ''} returned unparseable JSON: {exc}")
    if "(NotFound)" in err:
        return False, None
    raise ClassifierInspectionError(f"kubectl get {resource} {name or ''} failed: {err.strip() or out.strip() or 'unknown error'}")


def _labels_of(obj):
    return ((obj.get("metadata") or {}).get("labels")) or {}


def _instance_owned_reason(resource_label, obj):
    """Ownership proof for every resource the argo-cd upstream subchart itself renders (Deployments/StatefulSet/Services): app.kubernetes.io/instance == the Helm release name, exactly like runtime_state.py/monitor_state.py's own per-resource ownership-label pattern."""
    actual = _labels_of(obj).get("app.kubernetes.io/instance")
    if actual != ARGOCD_RELEASE_NAME:
        return f"{resource_label} has incompatible ownership label (app.kubernetes.io/instance={actual!r}), expected {ARGOCD_RELEASE_NAME!r} -- possible foreign/ambiguous ownership"
    return None


def _part_of_owned_reason(resource_label, obj):
    """Ownership proof for the ecr-token-sync footprint (helm/argocd/templates/ecr-token-sync-*.yaml): app.kubernetes.io/part-of == argocd."""
    actual = _labels_of(obj).get("app.kubernetes.io/part-of")
    if actual != "argocd":
        return f"{resource_label} has incompatible ownership label (app.kubernetes.io/part-of={actual!r}), expected 'argocd' -- possible foreign/ambiguous ownership"
    return None


def _ingress_owned_reason(obj):
    labels = _labels_of(obj)
    owned = all(labels.get(key) == value for key, value in INGRESS_OWNERSHIP_LABELS.items())
    if not owned:
        return (
            f"ingress/{INGRESS_NAME} has incompatible ownership labels {labels!r}, expected {INGRESS_OWNERSHIP_LABELS!r} "
            "-- possible foreign/ambiguous ownership, never automatically taken over"
        )
    return None


def classify(run, environment, namespace):
    """Returns the stable {"state", "environment", "namespace", "reasons", "checks"} shape. Raises ClassifierInspectionError if Kubernetes access itself could not be trusted -- callers must let that propagate as a hard failure, never a downgrade to ABSENT. Deliberately takes no ecr_registry/role-arn/host/group/certificate/ingress-values arguments -- ownership never depends on desired-state correctness, only on whether whatever currently exists is safely ours."""
    reasons = []
    checks = {}

    ns_found, _ = _get_json(run, "namespace", namespace)
    checks["namespace"] = ns_found

    crd_found = {crd: _get_json(run, "crd", crd)[0] for crd in REQUIRED_CRDS}
    checks["crds"] = crd_found
    any_crd_found = any(crd_found.values())

    deploy_footprint = {}
    for name in CORE_DEPLOYMENTS:
        deploy_footprint[name] = _get_json(run, "deployment", name, namespace)
    checks["deployments_found"] = {name: found for name, (found, _obj) in deploy_footprint.items()}

    sts_footprint = {}
    for name in CORE_STATEFULSETS:
        sts_footprint[name] = _get_json(run, "statefulset", name, namespace)
    checks["statefulsets_found"] = {name: found for name, (found, _obj) in sts_footprint.items()}

    svc_footprint = {}
    for name in CORE_SERVICES:
        svc_footprint[name] = _get_json(run, "service", name, namespace)
    checks["services_found"] = {name: found for name, (found, _obj) in svc_footprint.items()}

    ecr_sync_footprint = {
        "serviceaccount": _get_json(run, "serviceaccount", ECR_TOKEN_SYNC_NAME, namespace),
        "role": _get_json(run, "role", ECR_TOKEN_SYNC_NAME, namespace),
        "rolebinding": _get_json(run, "rolebinding", ECR_TOKEN_SYNC_NAME, namespace),
        "cronjob": _get_json(run, "cronjob", ECR_TOKEN_SYNC_NAME, namespace),
    }
    checks["ecr_token_sync_found"] = {kind: found for kind, (found, _obj) in ecr_sync_footprint.items()}

    secret_found = {name: _get_json(run, "secret", name, namespace)[0] for name in REPOSITORY_SECRET_NAMES}
    checks["repository_secrets_found"] = secret_found

    ingress_found, ingress_obj = _get_json(run, "ingress", INGRESS_NAME, namespace)
    checks["ingress_found"] = ingress_found

    any_footprint_found = (
        any_crd_found
        or any(found for found, _obj in deploy_footprint.values())
        or any(found for found, _obj in sts_footprint.values())
        or any(found for found, _obj in svc_footprint.values())
        or any(found for found, _obj in ecr_sync_footprint.values())
        or any(secret_found.values())
        or ingress_found
    )

    # ABSENT: no meaningful footprint at all -- no namespace and nothing else either. Any one of these existing without the rest means this classifier must not silently adopt orphaned/partial state; it falls through to the ownership-label checks below instead.
    if not ns_found and not any_footprint_found:
        return {"state": STATE_ABSENT, "environment": environment, "namespace": namespace, "reasons": [], "checks": checks}

    if not ns_found:
        reasons.append(f"namespace {namespace} does not exist but at least one Argo CD resource is already present -- ownership is ambiguous without the owning namespace")

    # Ownership-label proof on whatever currently exists -- deliberately NOT a completeness check: a resource that is simply missing (never rendered yet, or not yet caught up with a partial prior rollout) is exactly what MAIN is about to reconcile, never itself a reason. Only a WRONG ownership label on something that DOES exist is a genuine conflict signal.
    for name, (found, obj) in deploy_footprint.items():
        if not found:
            continue
        reason = _instance_owned_reason(f"deployment/{name}", obj)
        if reason:
            reasons.append(reason)

    for name, (found, obj) in sts_footprint.items():
        if not found:
            continue
        reason = _instance_owned_reason(f"statefulset/{name}", obj)
        if reason:
            reasons.append(reason)

    for name, (found, obj) in svc_footprint.items():
        if not found:
            continue
        reason = _instance_owned_reason(f"service/{name}", obj)
        if reason:
            reasons.append(reason)

    for kind, (found, obj) in ecr_sync_footprint.items():
        if not found:
            continue
        reason = _part_of_owned_reason(f"{kind}/{ECR_TOKEN_SYNC_NAME}", obj)
        if reason:
            reasons.append(reason)

    if ingress_found:
        reason = _ingress_owned_reason(ingress_obj)
        if reason:
            reasons.append(reason)

    state = STATE_BROKEN if reasons else STATE_OWNED
    return {"state": state, "environment": environment, "namespace": namespace, "reasons": reasons, "checks": checks}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--kubectl-bin", default="kubectl")
    args = parser.parse_args(argv)

    try:
        values = environment_derived_values(args.environment)
        run = KubectlRunner(args.kubectl_bin)
        result = classify(
            run,
            environment=args.environment,
            namespace=values["ARGOCD_NAMESPACE"],
        )
    except (ClassifierInspectionError, ValueError, OSError) as exc:
        print(f"INSPECTION ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result))
    if result["reasons"]:
        print("Argo CD ownership-safety diagnostics:", file=sys.stderr)
        for reason in result["reasons"]:
            print(f"  - {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
