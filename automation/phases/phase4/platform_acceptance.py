#!/usr/bin/env python3
"""automation/phases/phase4/platform_acceptance.py: read-only GoldenGate Platform post-reconciliation acceptance classifier -- answers exactly one question, "does the live GoldenGate Platform installation exactly match the current committed desired state right now?", as one of HEALTHY/BROKEN. Unlike automation/phases/phase4/platform_state.py (a pre-reconciliation ownership-safety preflight that only checks OWNERSHIP LABELS), this tool DOES require full readiness and exact desired-state correctness: a missing/unready Fluent Bit DaemonSet, a wrong IRSA role-arn, a stale namespace app.kubernetes.io/managed-by label, an incorrect Fluent Bit image/shape, or a non-Synced/non-Healthy Application are all BROKEN here. Never mutates the cluster: every kubectl invocation here is a `get` (read-only); no apply/create/delete/patch/annotate/label/helm call exists in this module. Consumes environment identity through automation/goldengate-environment.py, never a second environment parser."""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_module(name, path):
    """Lazy import of a repository module by explicit absolute file path -- the same importlib.util convention this repo already uses for automation/goldengate-environment.py, so this module never depends on sys.path/CWD."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# k8s_common.py is genuinely cross-phase (shared by platform/observability/runtime/monitor classifiers) and stays under automation/orchestration/ -- never copied into automation/phases/phase4/.
_k8s_common = _load_module("k8s_common", REPO_ROOT / "automation" / "orchestration" / "k8s_common.py")
ClassifierInspectionError = _k8s_common.ClassifierInspectionError
KubectlRunner = _k8s_common.KubectlRunner
daemonset_ready = _k8s_common.daemonset_ready
get_json = _k8s_common.get_json
list_json = _k8s_common.list_json

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


STATE_HEALTHY = "HEALTHY"
STATE_BROKEN = "BROKEN"

# Current chart/workflow contract (helm/goldengate-platform/, .github/workflows/30-sub-platform.yaml) -- verified against the real vendored chart/values.yaml and workflow, never guessed.
HELM_REPO_PATH = "helm/goldengate-platform"

RUNTIME_SA_NAME = "gg-runtime-sa"
FLUENT_BIT_SA_NAME = "gg-fluent-bit"
FLUENT_BIT_CLUSTERROLE_NAME = "gg-fluent-bit"
FLUENT_BIT_CLUSTERROLEBINDING_NAME = "gg-fluent-bit"
FLUENT_BIT_CONFIGMAP_NAME = "gg-fluent-bit-config"
FLUENT_BIT_DAEMONSET_NAME = "gg-fluent-bit"

# The Fluent Bit ECR repository name is an application constant, matching .github/workflows/30-sub-platform.yaml's own FLUENT_BIT_ECR_REPOSITORY_EXPECTED env literal. The registry is never hardcoded here -- it is always the canonical ECR_REGISTRY passed in by the caller.
FLUENT_BIT_ECR_REPOSITORY = "aws-cloud-factory-fluent-bit"

# helm/goldengate-platform/templates/fluent-bit-daemonset.yaml renders exactly one container (name: fluent-bit) and no initContainers -- never invent a second desired container shape.
FLUENT_BIT_CONTAINER_NAME = "fluent-bit"

# helm/goldengate-platform's syncPolicy.managedNamespaceMetadata.labels -- applied by Argo CD to RUNTIME_NAMESPACE itself, only once the platform Application has actually synced it.
MANAGED_NAMESPACE_LABELS = {
    "app.kubernetes.io/name": "goldengate-platform",
    "app.kubernetes.io/managed-by": "argocd",
}


def _release_and_app_name(environment):
    """RELEASE_NAME == ARGOCD_APP_NAME == goldengate-<environment>-platform (.github/workflows/30-sub-platform.yaml "Prepare platform deployment variables" step) -- both derived from the same canonical environment, never independently maintained literals."""
    name = f"goldengate-{environment}-platform"
    return name, name


def _validate_fluent_bit_image(fluent_bit_image, ecr_registry):
    """Validates the caller-supplied FLUENT_BIT_IMAGE operational configuration (vars.FLUENT_BIT_IMAGE) against the approved contract -- exactly <ECR_REGISTRY>/aws-cloud-factory-fluent-bit@sha256:<64 lowercase hex characters>, matching .github/workflows/30-sub-platform.yaml's own "Validate FLUENT_BIT_IMAGE format" step -- before it is ever trusted as expected cluster state. This is operational-configuration validation, not cluster inspection: a violation means the caller supplied a bad value, never that the cluster is HEALTHY/BROKEN. Raises ValueError (a configuration error), never ClassifierInspectionError."""
    expected_prefix = f"{ecr_registry}/{FLUENT_BIT_ECR_REPOSITORY}@sha256:"
    if not fluent_bit_image.startswith(expected_prefix):
        raise ValueError(
            f"FLUENT_BIT_IMAGE {fluent_bit_image!r} is not a valid private, immutable digest reference -- "
            f"expected exactly {expected_prefix!r} followed by a 64-character lowercase hex digest "
            "(no mutable tag, no public.ecr.aws, no other registry/repository)."
        )
    digest = fluent_bit_image[len(expected_prefix):]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError(
            f"FLUENT_BIT_IMAGE {fluent_bit_image!r} has an invalid digest {digest!r} -- "
            "expected exactly 64 lowercase hex characters after @sha256:."
        )


def _fluent_bit_container_shape_reasons(daemonset_name, ds_obj, expected_image):
    """Explicitly inspects spec.template.spec.containers/initContainers (never merely whether expected_image appears "in" some flattened image list) against the exact approved shape rendered by helm/goldengate-platform/templates/fluent-bit-daemonset.yaml: exactly one container, named fluent-bit, using exactly expected_image, and no initContainers at all."""
    pod_spec = (((ds_obj.get("spec") or {}).get("template") or {}).get("spec")) or {}
    containers = pod_spec.get("containers") or []
    init_containers = pod_spec.get("initContainers") or []

    reasons = []

    if init_containers:
        init_names = [c.get("name") for c in init_containers]
        reasons.append(f"daemonset/{daemonset_name} has unexpected initContainers {init_names!r}, expected none")

    if len(containers) != 1:
        container_names = [c.get("name") for c in containers]
        reasons.append(f"daemonset/{daemonset_name} has {len(containers)} container(s) {container_names!r}, expected exactly 1 (named {FLUENT_BIT_CONTAINER_NAME!r})")
        return reasons

    container = containers[0]
    actual_name = container.get("name")
    if actual_name != FLUENT_BIT_CONTAINER_NAME:
        reasons.append(f"daemonset/{daemonset_name}'s sole container is named {actual_name!r}, expected {FLUENT_BIT_CONTAINER_NAME!r}")

    actual_image = container.get("image")
    if actual_image != expected_image:
        reasons.append(f"daemonset/{daemonset_name} container {FLUENT_BIT_CONTAINER_NAME!r} image={actual_image!r}, expected FLUENT_BIT_IMAGE {expected_image!r}")

    return reasons


def classify(run, environment, runtime_namespace, argocd_namespace, ecr_registry, runtime_role_arn, platform_logging_role_arn, fluent_bit_image):
    """Returns the stable {"state", "environment", "namespace", "reasons", "checks"} shape. Raises ClassifierInspectionError if Kubernetes access itself could not be trusted -- callers must let that propagate as a hard failure, never a downgrade to HEALTHY/BROKEN. Raises ValueError if the caller-supplied FLUENT_BIT_IMAGE operational configuration itself is invalid -- a configuration error, never HEALTHY/BROKEN cluster state."""
    _validate_fluent_bit_image(fluent_bit_image, ecr_registry)

    reasons = []
    checks = {}

    release_name, app_name = _release_and_app_name(environment)
    expected_repo_url = f"oci://{ecr_registry}/{HELM_REPO_PATH}"

    app_found, app_obj = get_json(run, "application", app_name, argocd_namespace)
    checks["application_found"] = app_found
    if not app_found:
        reasons.append(f"Application {app_name} does not exist in {argocd_namespace}")
    else:
        status = app_obj.get("status") or {}
        sync_status = ((status.get("sync") or {}).get("status"))
        health_status = ((status.get("health") or {}).get("status"))
        if sync_status != "Synced":
            reasons.append(f"Application {app_name} sync status is {sync_status!r}, expected 'Synced'")
        if health_status != "Healthy":
            reasons.append(f"Application {app_name} health status is {health_status!r}, expected 'Healthy'")

        spec = app_obj.get("spec") or {}
        source = spec.get("source") or {}
        destination = spec.get("destination") or {}
        helm_source = source.get("helm") or {}

        actual_repo_url = source.get("repoURL")
        if actual_repo_url != expected_repo_url:
            reasons.append(f"Application {app_name} source.repoURL={actual_repo_url!r}, expected {expected_repo_url!r}")

        actual_dest_ns = destination.get("namespace")
        if actual_dest_ns != runtime_namespace:
            reasons.append(f"Application {app_name} destination.namespace={actual_dest_ns!r}, expected {runtime_namespace!r}")

        actual_release_name = helm_source.get("releaseName")
        if actual_release_name != release_name:
            reasons.append(f"Application {app_name} source.helm.releaseName={actual_release_name!r}, expected {release_name!r}")

    ns_found, ns_obj = get_json(run, "namespace", runtime_namespace)
    checks["namespace_found"] = ns_found
    if not ns_found:
        reasons.append(f"namespace {runtime_namespace} does not exist")
    else:
        ns_phase = ((ns_obj.get("status") or {}).get("phase"))
        if ns_phase == "Terminating":
            reasons.append(f"namespace {runtime_namespace} is Terminating")

        # Strict here (unlike platform_state.py's ownership check): the namespace metadata must exactly match Argo CD's own managedNamespaceMetadata contract post-reconciliation -- a stale app.kubernetes.io/managed-by=Helm left over from the disabled competing chart-rendered Namespace source is a genuine acceptance failure at this point, not tolerated as "safe drift" forever.
        ns_labels = ((ns_obj.get("metadata") or {}).get("labels")) or {}
        for label_key, expected_value in MANAGED_NAMESPACE_LABELS.items():
            actual_value = ns_labels.get(label_key)
            if actual_value != expected_value:
                reasons.append(f"namespace {runtime_namespace} label {label_key}={actual_value!r}, expected {expected_value!r} (managedNamespaceMetadata)")

    cr_found, _ = get_json(run, "clusterrole", FLUENT_BIT_CLUSTERROLE_NAME)
    checks["fluent_bit_clusterrole_found"] = cr_found
    if not cr_found:
        reasons.append(f"clusterrole/{FLUENT_BIT_CLUSTERROLE_NAME} does not exist")

    crb_found, crb_obj = get_json(run, "clusterrolebinding", FLUENT_BIT_CLUSTERROLEBINDING_NAME)
    checks["fluent_bit_clusterrolebinding_found"] = crb_found
    if not crb_found:
        reasons.append(f"clusterrolebinding/{FLUENT_BIT_CLUSTERROLEBINDING_NAME} does not exist")
    else:
        role_ref = crb_obj.get("roleRef") or {}
        if role_ref.get("kind") != "ClusterRole" or role_ref.get("name") != FLUENT_BIT_CLUSTERROLE_NAME:
            reasons.append(f"clusterrolebinding/{FLUENT_BIT_CLUSTERROLEBINDING_NAME} roleRef={role_ref!r}, expected kind=ClusterRole name={FLUENT_BIT_CLUSTERROLE_NAME!r}")
        subjects = crb_obj.get("subjects") or []
        expected_subject = {"kind": "ServiceAccount", "name": FLUENT_BIT_SA_NAME, "namespace": runtime_namespace}
        matching = [s for s in subjects if s.get("kind") == expected_subject["kind"] and s.get("name") == expected_subject["name"] and s.get("namespace") == expected_subject["namespace"]]
        if not matching:
            reasons.append(f"clusterrolebinding/{FLUENT_BIT_CLUSTERROLEBINDING_NAME} subjects={subjects!r}, expected to contain {expected_subject!r}")

    if ns_found:
        sa_found, sa_obj = get_json(run, "serviceaccount", RUNTIME_SA_NAME, runtime_namespace)
        checks["runtime_serviceaccount_found"] = sa_found
        if not sa_found:
            reasons.append(f"serviceaccount/{RUNTIME_SA_NAME} does not exist")
        else:
            role_arn = ((sa_obj.get("metadata") or {}).get("annotations") or {}).get("eks.amazonaws.com/role-arn")
            if role_arn != runtime_role_arn:
                reasons.append(f"serviceaccount/{RUNTIME_SA_NAME} eks.amazonaws.com/role-arn={role_arn!r}, expected {runtime_role_arn!r}")

        fb_sa_found, fb_sa_obj = get_json(run, "serviceaccount", FLUENT_BIT_SA_NAME, runtime_namespace)
        checks["fluent_bit_serviceaccount_found"] = fb_sa_found
        if not fb_sa_found:
            reasons.append(f"serviceaccount/{FLUENT_BIT_SA_NAME} does not exist")
        else:
            role_arn = ((fb_sa_obj.get("metadata") or {}).get("annotations") or {}).get("eks.amazonaws.com/role-arn")
            if role_arn != platform_logging_role_arn:
                reasons.append(f"serviceaccount/{FLUENT_BIT_SA_NAME} eks.amazonaws.com/role-arn={role_arn!r}, expected {platform_logging_role_arn!r}")

        cm_found, _ = get_json(run, "configmap", FLUENT_BIT_CONFIGMAP_NAME, runtime_namespace)
        checks["fluent_bit_configmap_found"] = cm_found
        if not cm_found:
            reasons.append(f"configmap/{FLUENT_BIT_CONFIGMAP_NAME} does not exist")

        ds_found, ds_obj = get_json(run, "daemonset", FLUENT_BIT_DAEMONSET_NAME, runtime_namespace)
        checks["fluent_bit_daemonset_found"] = ds_found
        if not ds_found:
            reasons.append(f"daemonset/{FLUENT_BIT_DAEMONSET_NAME} does not exist")
        else:
            ready, why = daemonset_ready(ds_obj)
            checks["fluent_bit_daemonset_ready"] = ready
            if not ready:
                reasons.append(f"daemonset/{FLUENT_BIT_DAEMONSET_NAME} not ready: {why}")

            pod_spec = (((ds_obj.get("spec") or {}).get("template") or {}).get("spec")) or {}
            actual_sa_name = pod_spec.get("serviceAccountName")
            if actual_sa_name != FLUENT_BIT_SA_NAME:
                reasons.append(f"daemonset/{FLUENT_BIT_DAEMONSET_NAME} pod template serviceAccountName={actual_sa_name!r}, expected {FLUENT_BIT_SA_NAME!r}")

            reasons.extend(_fluent_bit_container_shape_reasons(FLUENT_BIT_DAEMONSET_NAME, ds_obj, fluent_bit_image))

        # The platform release intentionally owns shared namespace/identity/logging resources only -- it must never own a GoldenGate runtime StatefulSet/Deployment.
        owned_statefulsets = list_json(run, "statefulset", namespace=runtime_namespace, label_selector=f"app.kubernetes.io/instance={release_name}")
        owned_deployments = list_json(run, "deployment", namespace=runtime_namespace, label_selector=f"app.kubernetes.io/instance={release_name}")
        checks["owned_runtime_workload_count"] = len(owned_statefulsets) + len(owned_deployments)
        if owned_statefulsets or owned_deployments:
            names = sorted([s.get("metadata", {}).get("name") for s in owned_statefulsets] + [d.get("metadata", {}).get("name") for d in owned_deployments])
            reasons.append(f"platform release {release_name} unexpectedly owns StatefulSet/Deployment resource(s): {names!r}")

    state = STATE_HEALTHY if not reasons else STATE_BROKEN
    return {"state": state, "environment": environment, "namespace": runtime_namespace, "reasons": reasons, "checks": checks}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--fluent-bit-image", required=True, help="Expected immutable private-ECR digest reference (vars.FLUENT_BIT_IMAGE), never hardcoded here.")
    parser.add_argument("--kubectl-bin", default="kubectl")
    args = parser.parse_args(argv)

    try:
        values = environment_derived_values(args.environment)
        run = KubectlRunner(args.kubectl_bin)
        result = classify(
            run,
            environment=args.environment,
            runtime_namespace=values["RUNTIME_NAMESPACE"],
            argocd_namespace=values["ARGOCD_NAMESPACE"],
            ecr_registry=values["ECR_REGISTRY"],
            runtime_role_arn=values["RUNTIME_ROLE_ARN"],
            platform_logging_role_arn=values["PLATFORM_LOGGING_ROLE_ARN"],
            fluent_bit_image=args.fluent_bit_image,
        )
    except ValueError as exc:
        # A bad --fluent-bit-image (or other caller-supplied) value -- a configuration error, distinct from a Kubernetes inspection failure. Never exposes secrets; the value itself is not secret (it is an image reference, already logged verbatim by 30-sub-platform.yaml).
        print(f"CONFIGURATION ERROR: {exc}", file=sys.stderr)
        return 1
    except (ClassifierInspectionError, OSError) as exc:
        print(f"INSPECTION ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result))
    if result["reasons"]:
        print("GoldenGate Platform acceptance diagnostics:", file=sys.stderr)
        for reason in result["reasons"]:
            print(f"  - {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
