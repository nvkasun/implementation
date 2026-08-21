#!/usr/bin/env python3
"""hack/orchestration/argocd_acceptance.py: read-only Argo CD post-reconciliation acceptance classifier -- answers exactly one question, "does the live Argo CD installation exactly match the current committed desired state right now?", as one of HEALTHY/BROKEN. Unlike hack/orchestration/argocd_state.py (a pre-reconciliation ownership-safety preflight that only checks OWNERSHIP LABELS), this tool DOES require full readiness and exact desired-state correctness: a missing/unready core component, an incomplete CRD set, a misconfigured ecr-token-sync identity, an incorrect/missing repository Secret, or (when argocdServerIngress.enabled=true) a missing/incorrect/not-yet-provisioned Ingress are all BROKEN here. Symmetrically, when argocdServerIngress.enabled=false the Ingress must be ABSENT -- a still-present, no-longer-desired Ingress is exactly as BROKEN as a missing desired one, proving true->false pruning genuinely completed. Never mutates the cluster: every kubectl invocation here is a `get` (read-only); no apply/create/delete/patch/annotate/label/helm call exists in this module. Consumes environment identity through hack/goldengate-environment.py, never a second environment parser."""
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


STATE_HEALTHY = "HEALTHY"
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

INGRESS_OWNERSHIP_LABELS = {
    "app.kubernetes.io/name": "argocd-server",
    "app.kubernetes.io/part-of": "argocd",
}


class ClassifierInspectionError(Exception):
    """Raised when the classifier could not determine truth -- API unreachable, permission denied, malformed JSON. Never conflated with BROKEN-due-to-drift; callers must fail closed on this too, but it is a distinct failure mode worth its own message."""


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


def _ingress_contract_reasons(ingress_obj, argocd_host, alb_group_name, acm_certificate_arn, ingress_values):
    """Full exact desired-state contract for an EXISTING argocd-server-ingress object -- every field below must match, never merely "close enough". Ownership itself is re-verified here too (never assumed from a prior reconciliation pass)."""
    reasons = []

    labels = (ingress_obj.get("metadata") or {}).get("labels") or {}
    owned = all(labels.get(key) == value for key, value in INGRESS_OWNERSHIP_LABELS.items())
    if not owned:
        reasons.append(
            f"ingress/{INGRESS_NAME} exists but its ownership labels {labels!r} do not match the expected Argo CD Helm "
            f"release {INGRESS_OWNERSHIP_LABELS!r} -- possible foreign/ambiguous ownership"
        )
        return reasons

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
        reasons.append(f"ingress/{INGRESS_NAME} spec.ingressClassName={spec.get('ingressClassName')!r}, expected {expected_ingress_class!r}")
    if rule.get("host") != argocd_host:
        reasons.append(f"ingress/{INGRESS_NAME} spec.rules[0].host={rule.get('host')!r}, expected {argocd_host!r}")
    if backend_service.get("name") != expected_service_name:
        reasons.append(f"ingress/{INGRESS_NAME} backend service name={backend_service.get('name')!r}, expected {expected_service_name!r}")
    actual_backend_port = ((backend_service.get("port") or {}).get("number"))
    if actual_backend_port != expected_service_port:
        reasons.append(f"ingress/{INGRESS_NAME} backend service port={actual_backend_port!r}, expected {expected_service_port!r}")

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
            reasons.append(f"ingress/{INGRESS_NAME} {annotation_key}={actual_value!r}, expected {str(expected_value)!r}")

    # Standalone-only: the resident/anchor Ingress must own the ALB scheme. Never checked in shared mode -- a shared-mode Ingress must NOT carry this annotation at all.
    if ingress_values.get("mode") == "standalone":
        expected_scheme = ingress_values.get("scheme")
        if expected_scheme:
            actual_scheme = annotations.get("alb.ingress.kubernetes.io/scheme")
            if actual_scheme != expected_scheme:
                reasons.append(f"ingress/{INGRESS_NAME} alb.ingress.kubernetes.io/scheme={actual_scheme!r}, expected {expected_scheme!r} (standalone resident anchor)")

    # AWS Load Balancer Controller readiness: strict here -- 20-sub-argocd.yaml's own bounded post-deploy wait has already had its chance by the time acceptance runs, so a still-empty status.loadBalancer.ingress at THIS point is a genuine failure, never a transient condition to tolerate.
    lb_ingress = ((ingress_obj.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
    if not lb_ingress:
        reasons.append(f"ingress/{INGRESS_NAME} status.loadBalancer.ingress is empty -- the AWS Load Balancer Controller has not published an address")

    return reasons


def classify(run, environment, namespace, ecr_registry, argocd_ecr_read_role_arn, argocd_host, alb_group_name, acm_certificate_arn, ingress_values):
    """Returns the stable {"state", "environment", "namespace", "reasons", "checks"} shape. Raises ClassifierInspectionError if Kubernetes access itself could not be trusted -- callers must let that propagate as a hard failure, never a downgrade to HEALTHY/BROKEN."""
    reasons = []
    checks = {}

    ns_found, _ = _get_json(run, "namespace", namespace)
    checks["namespace"] = ns_found
    if not ns_found:
        reasons.append(f"namespace {namespace} does not exist")

    crd_found = {crd: _get_json(run, "crd", crd)[0] for crd in REQUIRED_CRDS}
    checks["crds"] = crd_found
    missing_crds = sorted(c for c, found in crd_found.items() if not found)
    if missing_crds:
        reasons.append(f"missing required CRD(s): {missing_crds}")

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
                reasons.append(f"Secret {secret_name} does not exist")
                continue
            labels = (obj.get("metadata") or {}).get("labels") or {}
            if labels.get("argocd.argoproj.io/secret-type") != "repository":
                reasons.append(f"Secret {secret_name} is missing label argocd.argoproj.io/secret-type=repository")
            url_b64 = (obj.get("data") or {}).get("url")
            actual_url = base64.b64decode(url_b64).decode("utf-8") if url_b64 else None
            expected_url = f"oci://{ecr_registry}/{helm_repo}"
            if actual_url != expected_url:
                reasons.append(f"Secret {secret_name} url={actual_url!r}, expected {expected_url!r}")
            # Password is intentionally never read or included in classifier output.
        checks["repository_secrets"] = secret_status

        ingress_enabled = bool(ingress_values.get("enabled"))
        ingress_found, ingress_obj = _get_json(run, "ingress", INGRESS_NAME, namespace)
        checks["ingress_found"] = ingress_found
        checks["ingress_enabled_in_values"] = ingress_enabled
        if ingress_enabled:
            if not ingress_found:
                reasons.append(f"ingress/{INGRESS_NAME} does not exist but argocdServerIngress.enabled=true in envs/{environment}/argocd/values.yaml")
            else:
                reasons.extend(_ingress_contract_reasons(ingress_obj, argocd_host, alb_group_name, acm_certificate_arn, ingress_values))
        elif ingress_found:
            # true->false pruning proof: once disabled, Helm/Argo CD's own selfHeal must have removed it -- a still-present Ingress means pruning has not actually completed, never silently treated as harmless leftover state.
            reasons.append(f"ingress/{INGRESS_NAME} still exists but argocdServerIngress.enabled=false in envs/{environment}/argocd/values.yaml -- expected pruned")

    state = STATE_HEALTHY if not reasons else STATE_BROKEN
    return {"state": state, "environment": environment, "namespace": namespace, "reasons": reasons, "checks": checks}


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
        print("Argo CD acceptance diagnostics:", file=sys.stderr)
        for reason in result["reasons"]:
            print(f"  - {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
