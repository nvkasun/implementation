#!/usr/bin/env python3
"""hack/orchestration/runtime_state.py: read-only GoldenGate runtime ownership-safety preflight classifier (Phase B3A) -- answers exactly one question, "is it safe for MAIN to reconcile this GoldenGate runtime deployment?", as one of ABSENT/OWNED/BROKEN. This is NOT a HEALTHY-skip prerequisite classifier: a GoldenGate runtime is an actual desired deployment target whose descriptor/image/chart may intentionally change on every run, so OWNED (not HEALTHY) is the "safe to reconcile" state -- readiness/health is validated separately, post-reconciliation, by hack/orchestration/runtime_acceptance.py. Never mutates the cluster: every kubectl invocation here is a `get` (read-only); no apply/create/delete/patch/annotate/label/helm call exists in this module. Consumes deployment identity through hack/goldengate-deployment-model.py's `describe` output (the same canonical folder-driven descriptor resolver used everywhere else), never a second descriptor schema."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys


def _load_sibling_module(name, filename):
    """Lazy import of a same-directory hack/orchestration/ module by explicit file path -- the same importlib.util convention this repo already uses for hack/goldengate-environment.py, so this module never depends on sys.path/CWD."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_k8s_common = _load_sibling_module("k8s_common", "k8s_common.py")
ClassifierInspectionError = _k8s_common.ClassifierInspectionError
KubectlRunner = _k8s_common.KubectlRunner
get_json = _k8s_common.get_json

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

_ENVIRONMENT_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "goldengate-environment.py")
_DEPLOYMENT_MODEL_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "goldengate-deployment-model.py")
_environment_module = None
_deployment_model_module = None


def _load_environment_module():
    """Lazy import of hack/goldengate-environment.py -- the single canonical environment-config parser/deriver. Never a second independent schema implementation."""
    global _environment_module
    if _environment_module is None:
        spec = importlib.util.spec_from_file_location("goldengate_environment", _ENVIRONMENT_MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _environment_module = module
    return _environment_module


def _load_deployment_model_module():
    """Lazy import of hack/goldengate-deployment-model.py -- the single canonical folder-driven descriptor resolver. Never a second independent descriptor schema."""
    global _deployment_model_module
    if _deployment_model_module is None:
        spec = importlib.util.spec_from_file_location("goldengate_deployment_model", _DEPLOYMENT_MODEL_MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _deployment_model_module = module
    return _deployment_model_module


def environment_derived_values(environment):
    """Loads+validates+derives envs/<environment>/environment.yaml via the canonical resolver."""
    env_module = _load_environment_module()
    env_module.REPO_ROOT = REPO_ROOT
    doc = env_module.load_environment_config(environment)
    return env_module.derive_values(doc)


def describe_deployment(environment, deployment_id):
    """Returns the canonical descriptor dict for one deployment ID via hack/goldengate-deployment-model.py's own scan/validation -- exactly what `describe` prints, never re-parsed independently. Raises ValueError (a configuration error, never a cluster inspection error) if the folder-driven model itself has a problem or the deployment ID is unknown."""
    gdm = _load_deployment_model_module()
    gdm.REPO_ROOT = REPO_ROOT
    active, inactive, invalid, problems = gdm._run_full_validation(environment)
    if invalid or problems:
        raise ValueError(f"the folder-driven deployment model for {environment!r} has validation problems -- refusing to classify runtime ownership against an inconsistent model")
    by_id = {d["deploymentId"]: d for d in active + inactive}
    descriptor = by_id.get(deployment_id)
    if descriptor is None:
        raise ValueError(f"unknown deployment ID {deployment_id!r} in environment {environment!r} -- no envs/{environment}/{deployment_id}/values.yaml descriptor was found")
    return descriptor


STATE_ABSENT = "ABSENT"
STATE_OWNED = "OWNED"
STATE_BROKEN = "BROKEN"

# Current Helm/main-workflow naming contract (helm/goldengate/templates/_helpers.tpl, 00-main-goldengate-orchestrator.yaml) -- verified against the real vendored chart, never guessed.
HELM_REPO_PATH = "helm/goldengate"

# Resources whose ownership is verified via the shared goldengate.runtimeLabels helper (app.kubernetes.io/instance == deployment ID, goldengate.adcb/deployment-name == deployment ID, goldengate.adcb/environment == environment).
_RUNTIME_LABELS_OWNED_KINDS = (
    "statefulset", "service", "headless_service", "pvc",
    "admin_secretproviderclass", "certificate_secretproviderclass", "ingress",
)

# StorageClass is deliberately NOT rendered via goldengate.runtimeLabels (helm/goldengate/templates/efs-storageclass.yaml) -- it uses its own fixed label set with goldengate.adcb/deployment-id (not deployment-name).
_STORAGECLASS_KIND = "storageclass"

# The synced admin Secret is created out-of-band by the Secrets Store CSI driver (mirroring the SecretProviderClass), not directly rendered by this chart -- it carries no goldengate.adcb/* ownership labels to verify. Its exact expected name is itself the only ownership signal available; mere existence under that name is not a conflict.
_ADMIN_SECRET_KIND = "admin_secret"

# GoldenGate Runtime Presence Contract -- Final Safety Correction, Gap 5: the runtime u02 PVC is intentionally retained (helm/goldengate/templates/runtime-pvc.yaml carries argocd.argoproj.io/sync-options: Prune=false) across Application deletion, so it is durable STORAGE STATE, never runtime compute -- it needs special handling below, distinct from every other footprint kind, which all remain pure compute/workload objects.
_PVC_KIND = "pvc"


def _app_suffix(deployment_id):
    """APP_SUFFIX="${DEPLOYMENT_ID#gg-}" -- strips a leading "gg-" only if present, exactly like the real workflow's own bash parameter expansion."""
    if deployment_id.startswith("gg-"):
        return deployment_id[len("gg-"):]
    return deployment_id


def _expected_footprint_names(environment, deployment_id, runtime_namespace):
    return {
        "statefulset": (deployment_id, runtime_namespace),
        "service": (deployment_id, runtime_namespace),
        "headless_service": (f"{deployment_id}-headless", runtime_namespace),
        "pvc": (f"{deployment_id}-u02", runtime_namespace),
        _STORAGECLASS_KIND: (f"gg-efs-{environment}-{deployment_id}", None),
        "admin_secretproviderclass": (f"{deployment_id}-admin", runtime_namespace),
        "certificate_secretproviderclass": (f"{deployment_id}-certificate", runtime_namespace),
        "ingress": (f"{deployment_id}-ingress", runtime_namespace),
        _ADMIN_SECRET_KIND: (f"{deployment_id}-admin", runtime_namespace),
    }


_K8S_RESOURCE_TYPE = {
    "statefulset": "statefulset",
    "service": "service",
    "headless_service": "service",
    "pvc": "persistentvolumeclaim",
    _STORAGECLASS_KIND: "storageclass",
    "admin_secretproviderclass": "secretproviderclass",
    "certificate_secretproviderclass": "secretproviderclass",
    "ingress": "ingress",
    _ADMIN_SECRET_KIND: "secret",
}


def _ownership_reason(resource_label, obj, environment, deployment_id):
    """Returns a reason string if the given already-fetched resource's ownership labels do not clearly belong to this exact deployment, else None. The admin Secret is exempt (see _ADMIN_SECRET_KIND docstring above) -- its mere existence under the expected name is never itself a conflict."""
    labels = ((obj.get("metadata") or {}).get("labels")) or {}
    env_label = labels.get("goldengate.adcb/environment")
    if resource_label == _STORAGECLASS_KIND:
        id_label = labels.get("goldengate.adcb/deployment-id")
        id_key = "goldengate.adcb/deployment-id"
    else:
        id_label = labels.get("goldengate.adcb/deployment-name")
        id_key = "goldengate.adcb/deployment-name"

    if env_label != environment or id_label != deployment_id:
        return (
            f"{resource_label} has incompatible ownership labels (goldengate.adcb/environment={env_label!r}, "
            f"{id_key}={id_label!r}), expected environment={environment!r} {id_key}={deployment_id!r}"
        )
    return None


def classify(run, environment, deployment_id, argocd_namespace, runtime_namespace, ecr_registry):
    """Returns the stable {"state", "environment", "deployment_id", "namespace", "reasons", "checks"} shape. Raises ClassifierInspectionError if Kubernetes access itself could not be trusted -- callers must let that propagate as a hard failure, never a downgrade to ABSENT. Raises ValueError if the folder-driven model itself is inconsistent (invalid descriptors/cross-descriptor problems elsewhere) -- a configuration error, never ABSENT/OWNED/BROKEN cluster state."""
    # Confirms the folder-driven model is internally consistent before any cluster call -- fails closed if ANY descriptor in the environment is invalid, the same guard the reconcile path already relies on. Deliberately does NOT require THIS deployment_id's own descriptor to still be present: this classifier is also reused for a PHYSICALLY REMOVED descriptor's leftover live resources (GoldenGate Runtime Presence Contract Finalization -- ownership-safe delete, deletion_matrix reason=physical-removal), where by design no envs/<environment>/<deployment_id>/values.yaml exists any more; the caller (delete_removed_argocd_applications) already independently proved this ID was a genuine GoldenGate deployment before it ever reached this classifier.
    descriptor = None
    try:
        descriptor = describe_deployment(environment, deployment_id)
    except ValueError as exc:
        if "unknown deployment ID" not in str(exc):
            raise

    app_suffix = _app_suffix(deployment_id)
    app_name = f"goldengate-{environment}-{app_suffix}"
    expected_repo_url = f"oci://{ecr_registry}/{HELM_REPO_PATH}"

    reasons = []
    checks = {}

    app_found, app_obj = get_json(run, "application", app_name, argocd_namespace)
    checks["application_found"] = app_found

    footprint_names = _expected_footprint_names(environment, deployment_id, runtime_namespace)
    footprint = {}
    for label, (name, namespace) in footprint_names.items():
        found, obj = get_json(run, _K8S_RESOURCE_TYPE[label], name, namespace)
        footprint[label] = (found, obj)
    checks["footprint_found"] = {label: found for label, (found, _obj) in footprint.items()}

    any_footprint_found = any(found for found, _obj in footprint.values())

    # ABSENT: no owning Application and no meaningful expected-name footprint at all -- safe to create from nothing. Any one of these existing without the Application means this classifier must not silently adopt orphaned/partial state.
    if not app_found and not any_footprint_found:
        return {"state": STATE_ABSENT, "environment": environment, "deployment_id": deployment_id, "namespace": runtime_namespace, "reasons": [], "checks": checks}

    # GoldenGate Runtime Presence Contract -- Final Safety Correction, Gap 5: the u02 PVC is intentionally retained across Application deletion (Prune=false, see helm/goldengate/templates/runtime-pvc.yaml) -- "Application absent, ONLY the retained PVC exists" is the expected, SAFE shape of a disabled-then-re-enableable runtime, never an unexplained orphan on its own. Every OTHER compute/workload footprint kind (StatefulSet/Service/headless Service/StorageClass/SecretProviderClasses/admin Secret) is still pruned/cascade-deleted as normal and remains exactly as unsafe as before when found without an owning Application. "Chart-owned" persistence means the descriptor both declares EFS persistence (efsMode is not None) AND the chart actually creates its own PVC rather than referencing a pre-existing one via runtime.storage.u02.existingClaim (pvcClaimName empty) -- the SAME condition helm/goldengate/templates/runtime-pvc.yaml itself renders on.
    declares_chart_owned_persistence = bool(descriptor and descriptor.get("efsMode") and not descriptor.get("pvcClaimName"))
    pvc_found, _pvc_obj = footprint[_PVC_KIND]
    non_pvc_footprint_found = any(found for label, (found, _obj) in footprint.items() if label != _PVC_KIND)

    if not app_found:
        if non_pvc_footprint_found:
            owned_names = [label for label, (found, _obj) in footprint.items() if found and label != _PVC_KIND]
            reasons.append(f"Application {app_name} does not exist in {argocd_namespace} but expected-name runtime resource(s) already exist: {owned_names!r}")
        elif pvc_found and not declares_chart_owned_persistence:
            reasons.append(f"Application {app_name} does not exist in {argocd_namespace} but a retained persistence PVC exists although this deployment's descriptor does not declare chart-owned EFS persistence -- not the recognized retained-persistence footprint, treated as an unexplained orphan")
        # else: Application absent, ONLY the retained PVC exists, and this deployment's descriptor legitimately declares chart-owned EFS persistence -- the recognized "disabled runtime, durable /u02 data retained for a future re-enable" shape. Its own ownership labels are still verified unconditionally below, exactly like every other footprint kind -- a foreign/mislabeled PVC under the expected name is never silently adopted.
    else:
        labels = ((app_obj.get("metadata") or {}).get("labels")) or {}
        actual_env_label = labels.get("goldengate.adcb/environment")
        actual_id_label = labels.get("goldengate.adcb/deployment-id")
        if actual_env_label != environment:
            reasons.append(f"Application {app_name} label goldengate.adcb/environment={actual_env_label!r}, expected {environment!r}")
        if actual_id_label != deployment_id:
            reasons.append(f"Application {app_name} label goldengate.adcb/deployment-id={actual_id_label!r}, expected {deployment_id!r}")

        spec = app_obj.get("spec") or {}
        destination = spec.get("destination") or {}
        source = spec.get("source") or {}
        helm_source = source.get("helm") or {}

        actual_dest_ns = destination.get("namespace")
        if actual_dest_ns != runtime_namespace:
            reasons.append(f"Application {app_name} destination.namespace={actual_dest_ns!r}, expected {runtime_namespace!r}")

        actual_repo_url = source.get("repoURL")
        if actual_repo_url != expected_repo_url:
            reasons.append(f"Application {app_name} source.repoURL={actual_repo_url!r}, expected {expected_repo_url!r}")

        actual_release_name = helm_source.get("releaseName")
        if actual_release_name != deployment_id:
            reasons.append(f"Application {app_name} source.helm.releaseName={actual_release_name!r}, expected {deployment_id!r}")

        # Deliberately NOT checked here: status.sync.status / status.health.status / spec.source.targetRevision. This is a pre-reconciliation ownership-safety classifier, not a readiness classifier -- an OutOfSync/Progressing/Degraded Application that otherwise clearly belongs to this deployment is exactly what MAIN is about to reconcile, not an ownership conflict. Post-reconciliation health is runtime_acceptance.py's job.

    # Any expected-name resource that currently exists must carry compatible ownership, regardless of whether the Application itself was found -- this is what actually distinguishes a safe partial-OWNED footprint (this deployment's own prior partial rollout) from a foreign/orphaned collision.
    for label, (found, obj) in footprint.items():
        if not found or label == _ADMIN_SECRET_KIND:
            continue
        reason = _ownership_reason(label, obj, environment, deployment_id)
        if reason:
            reasons.append(reason)

    state = STATE_BROKEN if reasons else STATE_OWNED
    return {"state": state, "environment": environment, "deployment_id": deployment_id, "namespace": runtime_namespace, "reasons": reasons, "checks": checks}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--kubectl-bin", default="kubectl")
    args = parser.parse_args(argv)

    try:
        values = environment_derived_values(args.environment)
        run = KubectlRunner(args.kubectl_bin)
        result = classify(
            run,
            environment=args.environment,
            deployment_id=args.deployment_id,
            argocd_namespace=values["ARGOCD_NAMESPACE"],
            runtime_namespace=values["RUNTIME_NAMESPACE"],
            ecr_registry=values["ECR_REGISTRY"],
        )
    except ValueError as exc:
        print(f"CONFIGURATION ERROR: {exc}", file=sys.stderr)
        return 1
    except (ClassifierInspectionError, OSError) as exc:
        print(f"INSPECTION ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result))
    if result["reasons"]:
        print("GoldenGate runtime ownership-safety diagnostics:", file=sys.stderr)
        for reason in result["reasons"]:
            print(f"  - {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
