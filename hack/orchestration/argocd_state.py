#!/usr/bin/env python3
"""hack/orchestration/argocd_state.py: read-only Argo CD prerequisite classifier -- answers exactly one question, "what is the current Argo CD prerequisite state?", as one of ABSENT/HEALTHY/RECONCILABLE/BROKEN. RECONCILABLE (Live Argo Recovery Fix) is a narrow, deliberately conservative carve-out: the core Argo footprint and ECR token-sync RBAC/identity are structurally healthy and only the generated repository Secrets are missing/drifted, a class of drift the existing reusable Argo specialist workflow (its idempotent Helm chart reconciliation plus its own immediate bounded ECR token-sync validation step) already safely repairs -- any other drift at all still classifies BROKEN. Never mutates the cluster: every kubectl invocation here is a `get` (read-only); no apply/create/delete/patch/annotate/label/helm call exists in this module. Consumes environment identity through hack/goldengate-environment.py, never a second environment parser."""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import subprocess
import sys

import yaml

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

_ENVIRONMENT_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "goldengate-environment.py")
_environment_module = None


def _load_environment_module():
    """Lazy import of hack/goldengate-environment.py -- the single canonical environment-config parser/deriver. Never a second independent schema implementation."""
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


def ingress_enabled_from_values(environment):
    """Reads envs/<environment>/argocd/values.yaml's argocdServerIngress.enabled -- the actual deployed-chart contract, never hardcoded into this classifier."""
    path = os.path.join(REPO_ROOT, "envs", environment, "argocd", "values.yaml")
    with open(path) as f:
        doc = yaml.safe_load(f) or {}
    return bool((doc.get("argocdServerIngress") or {}).get("enabled"))


STATE_ABSENT = "ABSENT"
STATE_HEALTHY = "HEALTHY"
STATE_RECONCILABLE = "RECONCILABLE"
STATE_BROKEN = "BROKEN"

# Current chart/values contract (helm/argocd, envs/<environment>/argocd/values.yaml) -- verified against the real vendored chart's rendered output, never guessed.
REQUIRED_CRDS = (
    "applications.argoproj.io",
    "appprojects.argoproj.io",
    "applicationsets.argoproj.io",
)

REQUIRED_DEPLOYMENTS = (
    "argocd-server",
    "argocd-repo-server",
    "argocd-redis",
    "argocd-applicationset-controller",
    "argocd-notifications-controller",
)

REQUIRED_STATEFULSETS = ("argocd-application-controller",)

REQUIRED_SERVICES = ("argocd-server", "argocd-repo-server", "argocd-redis")

ECR_TOKEN_SYNC_NAME = "argocd-ecr-token-sync"

# Repository Secret name -> Helm OCI repository path (relative to ECR_REGISTRY), matching envs/<environment>/argocd/values.yaml ecrTokenSync.repositories exactly.
REQUIRED_REPO_SECRETS = {
    "argocd-ecr-goldengate-oci": "helm/goldengate",
    "argocd-ecr-goldengate-monitor-oci": "helm/goldengate-monitor",
    "argocd-ecr-goldengate-platform-oci": "helm/goldengate-platform",
    "argocd-ecr-amazon-cloudwatch-observability-oci": "helm/amazon-cloudwatch-observability",
}

INGRESS_NAME = "argocd-server-ingress"


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


def _replicaset_like_ready(obj, ready_fields):
    """Shared Deployment/StatefulSet readiness check: observedGeneration caught up, and every field in ready_fields equals the desired replica count."""
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    metadata = obj.get("metadata") or {}
    desired = spec.get("replicas")
    if desired is None:
        desired = 1
    if desired <= 0:
        return False, "spec.replicas is not > 0"
    if metadata.get("generation") != status.get("observedGeneration"):
        return False, f"status.observedGeneration={status.get('observedGeneration')!r} does not match metadata.generation={metadata.get('generation')!r}"
    for field in ready_fields:
        if status.get(field) != desired:
            return False, f"status.{field}={status.get(field)!r}, expected desired replicas={desired}"
    return True, None


def _deployment_ready(obj):
    return _replicaset_like_ready(obj, ("updatedReplicas", "readyReplicas", "availableReplicas"))


def _statefulset_ready(obj):
    return _replicaset_like_ready(obj, ("updatedReplicas", "readyReplicas", "currentReplicas"))


def classify(run, environment, namespace, ecr_registry, argocd_ecr_read_role_arn, ingress_enabled):
    """Returns the stable {"state", "environment", "namespace", "reasons", "checks"} shape. Raises ClassifierInspectionError if Kubernetes access itself could not be trusted -- callers must let that propagate as a hard failure, never a downgrade to ABSENT."""
    reasons = []
    # Subset of `reasons` that is safe to auto-repair via the existing reusable Argo specialist workflow's idempotent Helm chart reconciliation plus its own immediate bounded ECR token-sync validation step (which triggers a one-off Job from the already-correct CronJob and waits for it to (re)create the repository Secrets) -- never a broader "any drift is safe" carve-out. Only a missing/mislabeled/wrong-URL required repository Secret is added here; every other reason (core Argo footprint, ECR token-sync RBAC/identity, ingress) stays exclusively in `reasons` and therefore forces BROKEN below.
    reconcilable_reasons = []
    checks = {}

    ns_found, _ = _get_json(run, "namespace", namespace)
    checks["namespace"] = ns_found

    crd_found = {crd: _get_json(run, "crd", crd)[0] for crd in REQUIRED_CRDS}
    checks["crds"] = crd_found
    any_crd_found = any(crd_found.values())
    all_crds_found = all(crd_found.values())

    # ABSENT: no meaningful footprint at all -- no namespace and no cluster-scoped Argo CRD.
    if not ns_found and not any_crd_found:
        return {"state": STATE_ABSENT, "environment": environment, "namespace": namespace, "reasons": [], "checks": checks}

    if not ns_found:
        reasons.append(f"namespace {namespace} does not exist but at least one Argo CRD is registered cluster-wide")
    if not all_crds_found:
        missing = sorted(c for c, found in crd_found.items() if not found)
        reasons.append(f"missing required CRD(s): {missing}")

    # The remaining checks only make sense once the namespace exists.
    if ns_found:
        deploy_status = {}
        for name in REQUIRED_DEPLOYMENTS:
            found, obj = _get_json(run, "deployment", name, namespace)
            if not found:
                reasons.append(f"deployment/{name} does not exist")
                deploy_status[name] = False
                continue
            ready, why = _deployment_ready(obj)
            deploy_status[name] = ready
            if not ready:
                reasons.append(f"deployment/{name} not ready: {why}")
        checks["deployments"] = deploy_status

        sts_status = {}
        for name in REQUIRED_STATEFULSETS:
            found, obj = _get_json(run, "statefulset", name, namespace)
            if not found:
                reasons.append(f"statefulset/{name} does not exist")
                sts_status[name] = False
                continue
            ready, why = _statefulset_ready(obj)
            sts_status[name] = ready
            if not ready:
                reasons.append(f"statefulset/{name} not ready: {why}")
        checks["statefulsets"] = sts_status

        svc_status = {}
        for name in REQUIRED_SERVICES:
            found, _ = _get_json(run, "service", name, namespace)
            svc_status[name] = found
            if not found:
                reasons.append(f"service/{name} does not exist")
        checks["services"] = svc_status

        sa_found, sa_obj = _get_json(run, "serviceaccount", ECR_TOKEN_SYNC_NAME, namespace)
        checks["ecr_token_sync_serviceaccount"] = sa_found
        if not sa_found:
            reasons.append(f"serviceaccount/{ECR_TOKEN_SYNC_NAME} does not exist")
        else:
            role_arn = ((sa_obj.get("metadata") or {}).get("annotations") or {}).get("eks.amazonaws.com/role-arn")
            if role_arn != argocd_ecr_read_role_arn:
                reasons.append(f"serviceaccount/{ECR_TOKEN_SYNC_NAME} eks.amazonaws.com/role-arn={role_arn!r}, expected {argocd_ecr_read_role_arn!r}")

        role_found, _ = _get_json(run, "role", ECR_TOKEN_SYNC_NAME, namespace)
        checks["ecr_token_sync_role"] = role_found
        if not role_found:
            reasons.append(f"role/{ECR_TOKEN_SYNC_NAME} does not exist")

        rb_found, _ = _get_json(run, "rolebinding", ECR_TOKEN_SYNC_NAME, namespace)
        checks["ecr_token_sync_rolebinding"] = rb_found
        if not rb_found:
            reasons.append(f"rolebinding/{ECR_TOKEN_SYNC_NAME} does not exist")

        cj_found, cj_obj = _get_json(run, "cronjob", ECR_TOKEN_SYNC_NAME, namespace)
        checks["ecr_token_sync_cronjob"] = cj_found
        if not cj_found:
            reasons.append(f"cronjob/{ECR_TOKEN_SYNC_NAME} does not exist")
        elif (cj_obj.get("spec") or {}).get("suspend"):
            reasons.append(f"cronjob/{ECR_TOKEN_SYNC_NAME} is suspended")

        secret_status = {}
        for secret_name, helm_repo in REQUIRED_REPO_SECRETS.items():
            found, obj = _get_json(run, "secret", secret_name, namespace)
            secret_status[secret_name] = found
            if not found:
                reason = f"Secret {secret_name} does not exist"
                reasons.append(reason)
                reconcilable_reasons.append(reason)
                continue
            labels = (obj.get("metadata") or {}).get("labels") or {}
            if labels.get("argocd.argoproj.io/secret-type") != "repository":
                reason = f"Secret {secret_name} is missing label argocd.argoproj.io/secret-type=repository"
                reasons.append(reason)
                reconcilable_reasons.append(reason)
            url_b64 = (obj.get("data") or {}).get("url")
            actual_url = base64.b64decode(url_b64).decode("utf-8") if url_b64 else None
            expected_url = f"oci://{ecr_registry}/{helm_repo}"
            if actual_url != expected_url:
                reason = f"Secret {secret_name} url={actual_url!r}, expected {expected_url!r}"
                reasons.append(reason)
                reconcilable_reasons.append(reason)
            # Password is intentionally never read or included in classifier output.
        checks["repository_secrets"] = secret_status

        ingress_found, _ = _get_json(run, "ingress", INGRESS_NAME, namespace)
        checks["ingress_found"] = ingress_found
        checks["ingress_enabled_in_values"] = ingress_enabled
        if ingress_enabled and not ingress_found:
            reasons.append(f"ingress/{INGRESS_NAME} does not exist but argocdServerIngress.enabled=true in envs/{environment}/argocd/values.yaml")

    # RECONCILABLE only when EVERY collected reason is in the reconcilable subset (missing/mislabeled/wrong-URL repository Secrets, with the core Argo footprint, ECR token-sync RBAC/identity, and ingress contract all otherwise clean) -- any other reason at all (foreign/ambiguous ownership, a not-ready core component, a broken ECR token-sync identity) forces BROKEN, never a broader "partial install is fine" default.
    if not reasons:
        state = STATE_HEALTHY
    elif len(reconcilable_reasons) == len(reasons):
        state = STATE_RECONCILABLE
    else:
        state = STATE_BROKEN
    return {"state": state, "environment": environment, "namespace": namespace, "reasons": reasons, "checks": checks}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--kubectl-bin", default="kubectl")
    args = parser.parse_args(argv)

    try:
        values = environment_derived_values(args.environment)
        ingress_enabled = ingress_enabled_from_values(args.environment)
        run = KubectlRunner(args.kubectl_bin)
        result = classify(
            run,
            environment=args.environment,
            namespace=values["ARGOCD_NAMESPACE"],
            ecr_registry=values["ECR_REGISTRY"],
            argocd_ecr_read_role_arn=values["ARGOCD_ECR_READ_ROLE_ARN"],
            ingress_enabled=ingress_enabled,
        )
    except (ClassifierInspectionError, ValueError, OSError) as exc:
        print(f"INSPECTION ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result))
    if result["reasons"]:
        print("Argo CD prerequisite diagnostics:", file=sys.stderr)
        for reason in result["reasons"]:
            print(f"  - {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
