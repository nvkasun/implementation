#!/usr/bin/env python3
"""hack/orchestration/argocd_state.py: read-only Argo CD prerequisite classifier -- answers exactly one question, "what is the current Argo CD prerequisite state?", as one of ABSENT/HEALTHY/RECONCILABLE/BROKEN. RECONCILABLE is a narrow, deliberately conservative carve-out: (1, Live Argo Recovery Fix) the generated repository Secrets are missing/drifted, and (2, Fresh-Cluster Platform + Argo Ingress Self-Recovery Fix) the desired, clearly Argo/Helm-owned argocd-server-ingress is missing or carries safe deterministic spec/annotation drift (including the AWS Load Balancer Controller not yet having published a status.loadBalancer.ingress address) -- both classes of drift the existing reusable Argo specialist workflow (its idempotent Helm chart reconciliation, own immediate bounded ECR token-sync validation step, and own bounded Ingress/ALB readiness wait) already safely repairs. A same-name Ingress with foreign/ambiguous ownership is never taken over -- that stays exclusively in `reasons`, forcing BROKEN. Any other drift at all still classifies BROKEN. Never mutates the cluster: every kubectl invocation here is a `get` (read-only); no apply/create/delete/patch/annotate/label/helm call exists in this module. Consumes environment identity through hack/goldengate-environment.py, never a second environment parser."""
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


def ingress_config_from_values(environment):
    """Reads envs/<environment>/argocd/values.yaml's whole argocdServerIngress block -- the actual deployed-chart application-constant contract (enabled, mode, ingressClassName, serviceName, servicePort, groupOrder, targetType, backendProtocol, listenPorts, healthcheck*, scheme), never hardcoded into this classifier. host/groupName/certificateArn are deliberately NOT read from here -- those are shared environment identity, injected at deploy time from envs/<environment>/environment.yaml via the canonical resolver, and are passed into classify() separately (argocd_host/alb_group_name/acm_certificate_arn)."""
    path = os.path.join(REPO_ROOT, "envs", environment, "argocd", "values.yaml")
    with open(path) as f:
        doc = yaml.safe_load(f) or {}
    return dict(doc.get("argocdServerIngress") or {})


STATE_ABSENT = "ABSENT"
STATE_HEALTHY = "HEALTHY"
STATE_RECONCILABLE = "RECONCILABLE"
STATE_BROKEN = "BROKEN"

# Stable compatibility marker, not an operational recovery mechanism: lets a caller (00-main-goldengate-orchestrator.yaml) fail closed if it is ever paired with a classifier source whose state semantics have drifted (e.g. an older copy of this file that predates RECONCILABLE), rather than silently trusting a state value the caller's own if: expressions were never written to understand. Bump only when the {"state", ...} contract itself changes meaning.
CLASSIFIER_CONTRACT = "argocd-recovery-v2"

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

# Labels rendered by helm/argocd/templates/argocd-server-ingress.yaml itself -- the ownership proof used to decide whether an existing same-name Ingress is safe to treat as Argo/Helm-owned drift (RECONCILABLE-eligible) versus foreign/ambiguous (always BROKEN, never automatically taken over).
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


def _ingress_drift_reasons(ingress_obj, argocd_host, alb_group_name, acm_certificate_arn, ingress_values):
    """Returns (reasons, reconcilable_reasons) for an EXISTING argocd-server-ingress object. Foreign/ambiguous ownership (labels do not match the expected Argo CD Helm release) is reasons-only, never reconcilable -- this module must never automatically take over an Ingress it cannot prove it owns. Every other field checked below (host, backend Service/port, ALB annotations, and AWS Load Balancer Controller readiness) is safe/deterministic Argo-owned drift, already reconcilable via 20-sub-argocd.yaml's own idempotent Helm reconciliation plus its own bounded Ingress/ALB readiness wait."""
    reasons = []
    reconcilable_reasons = []

    labels = (ingress_obj.get("metadata") or {}).get("labels") or {}
    owned = all(labels.get(key) == value for key, value in INGRESS_OWNERSHIP_LABELS.items())
    if not owned:
        reasons.append(
            f"ingress/{INGRESS_NAME} exists but its ownership labels {labels!r} do not match the expected Argo CD Helm "
            f"release {INGRESS_OWNERSHIP_LABELS!r} -- possible foreign/ambiguous ownership, never automatically taken over"
        )
        return reasons, reconcilable_reasons

    def add(reason):
        reasons.append(reason)
        reconcilable_reasons.append(reason)

    annotations = (ingress_obj.get("metadata") or {}).get("annotations") or {}
    spec = ingress_obj.get("spec") or {}
    rule = (spec.get("rules") or [{}])[0] or {}
    path = (((rule.get("http") or {}).get("paths")) or [{}])[0] or {}
    backend_service = ((path.get("backend") or {}).get("service")) or {}

    expected_ingress_class = ingress_values.get("ingressClassName") or "alb"
    expected_service_name = ingress_values.get("serviceName") or "argocd-server"
    expected_service_port = ingress_values.get("servicePort")
    if expected_service_port is None:
        expected_service_port = 443

    if spec.get("ingressClassName") != expected_ingress_class:
        add(f"ingress/{INGRESS_NAME} spec.ingressClassName={spec.get('ingressClassName')!r}, expected {expected_ingress_class!r}")
    if rule.get("host") != argocd_host:
        add(f"ingress/{INGRESS_NAME} spec.rules[0].host={rule.get('host')!r}, expected {argocd_host!r}")
    if backend_service.get("name") != expected_service_name:
        add(f"ingress/{INGRESS_NAME} backend service name={backend_service.get('name')!r}, expected {expected_service_name!r}")
    actual_backend_port = ((backend_service.get("port") or {}).get("number"))
    if actual_backend_port != expected_service_port:
        add(f"ingress/{INGRESS_NAME} backend service port={actual_backend_port!r}, expected {expected_service_port!r}")

    annotation_checks = (
        ("alb.ingress.kubernetes.io/group.name", alb_group_name),
        ("alb.ingress.kubernetes.io/group.order", ingress_values.get("groupOrder")),
        ("alb.ingress.kubernetes.io/certificate-arn", acm_certificate_arn),
        ("alb.ingress.kubernetes.io/listen-ports", ingress_values.get("listenPorts")),
        ("alb.ingress.kubernetes.io/target-type", ingress_values.get("targetType")),
        ("alb.ingress.kubernetes.io/backend-protocol", ingress_values.get("backendProtocol")),
        ("alb.ingress.kubernetes.io/healthcheck-protocol", ingress_values.get("healthcheckProtocol")),
        ("alb.ingress.kubernetes.io/healthcheck-path", ingress_values.get("healthcheckPath")),
        ("alb.ingress.kubernetes.io/healthcheck-port", ingress_values.get("healthcheckPort")),
    )
    for annotation_key, expected_value in annotation_checks:
        if expected_value is None or expected_value == "":
            continue
        actual_value = annotations.get(annotation_key)
        if actual_value != str(expected_value):
            add(f"ingress/{INGRESS_NAME} {annotation_key}={actual_value!r}, expected {str(expected_value)!r}")

    # Standalone-only: the resident/anchor Ingress must own the ALB scheme. Never checked in shared mode -- a shared-mode Ingress must NOT carry this annotation at all (repeating it alongside the resident anchor's own value causes an ALB Controller IngressGroup conflicting-attribute error), so absence there is correct, not drift.
    if ingress_values.get("mode") == "standalone":
        expected_scheme = ingress_values.get("scheme")
        if expected_scheme:
            actual_scheme = annotations.get("alb.ingress.kubernetes.io/scheme")
            if actual_scheme != expected_scheme:
                add(f"ingress/{INGRESS_NAME} alb.ingress.kubernetes.io/scheme={actual_scheme!r}, expected {expected_scheme!r} (standalone resident anchor)")

    # AWS Load Balancer Controller readiness: a spec/annotation-correct Ingress with no published address yet is exactly the transient condition 20-sub-argocd.yaml's own bounded post-deploy wait is designed to resolve -- safe/reconcilable here, never itself terminal BROKEN. If that bounded wait itself times out live, 20-sub-argocd.yaml fails closed on its own (reconcile_argocd never reports success), so this classifier is never the sole gate against a permanently-unprovisioned ALB.
    lb_ingress = ((ingress_obj.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
    if not lb_ingress:
        add(f"ingress/{INGRESS_NAME} status.loadBalancer.ingress is empty -- the AWS Load Balancer Controller has not yet published an address")

    return reasons, reconcilable_reasons


def classify(run, environment, namespace, ecr_registry, argocd_ecr_read_role_arn, argocd_host, alb_group_name, acm_certificate_arn, ingress_values):
    """Returns the stable {"contract", "state", "environment", "namespace", "reasons", "checks"} shape (contract == CLASSIFIER_CONTRACT). Raises ClassifierInspectionError if Kubernetes access itself could not be trusted -- callers must let that propagate as a hard failure, never a downgrade to ABSENT."""
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
        return {"contract": CLASSIFIER_CONTRACT, "state": STATE_ABSENT, "environment": environment, "namespace": namespace, "reasons": [], "checks": checks}

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

        ingress_enabled = bool(ingress_values.get("enabled"))
        ingress_found, ingress_obj = _get_json(run, "ingress", INGRESS_NAME, namespace)
        checks["ingress_found"] = ingress_found
        checks["ingress_enabled_in_values"] = ingress_enabled
        if ingress_enabled:
            if not ingress_found:
                # Missing-but-desired is exactly the deterministic, application-owned drift 20-sub-argocd.yaml's own idempotent Helm reconciliation already safely repairs -- reconcilable, mirroring the existing repository-Secret carve-out above.
                reason = f"ingress/{INGRESS_NAME} does not exist but argocdServerIngress.enabled=true in envs/{environment}/argocd/values.yaml"
                reasons.append(reason)
                reconcilable_reasons.append(reason)
            else:
                ingress_reasons, ingress_reconcilable_reasons = _ingress_drift_reasons(ingress_obj, argocd_host, alb_group_name, acm_certificate_arn, ingress_values)
                reasons.extend(ingress_reasons)
                reconcilable_reasons.extend(ingress_reconcilable_reasons)

    # RECONCILABLE only when EVERY collected reason is in the reconcilable subset (missing/mislabeled/wrong-URL repository Secrets and/or missing/drifted clearly-owned Ingress, with the core Argo footprint, ECR token-sync RBAC/identity, and everything else otherwise clean) -- any other reason at all (foreign/ambiguous Ingress ownership, a not-ready core component, a broken ECR token-sync identity) forces BROKEN, never a broader "partial install is fine" default.
    if not reasons:
        state = STATE_HEALTHY
    elif reconcilable_reasons and len(reconcilable_reasons) == len(reasons):
        state = STATE_RECONCILABLE
    else:
        state = STATE_BROKEN
    return {"contract": CLASSIFIER_CONTRACT, "state": state, "environment": environment, "namespace": namespace, "reasons": reasons, "checks": checks}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--kubectl-bin", default="kubectl")
    args = parser.parse_args(argv)

    try:
        values = environment_derived_values(args.environment)
        ingress_values = ingress_config_from_values(args.environment)
        run = KubectlRunner(args.kubectl_bin)
        result = classify(
            run,
            environment=args.environment,
            namespace=values["ARGOCD_NAMESPACE"],
            ecr_registry=values["ECR_REGISTRY"],
            argocd_ecr_read_role_arn=values["ARGOCD_ECR_READ_ROLE_ARN"],
            argocd_host=values["ARGOCD_HOST"],
            alb_group_name=values["ALB_GROUP_NAME"],
            acm_certificate_arn=values["ACM_CERTIFICATE_ARN"],
            ingress_values=ingress_values,
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
