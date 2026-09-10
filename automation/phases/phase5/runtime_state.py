#!/usr/bin/env python3
"""automation/phases/phase5/runtime_state.py: read-only GoldenGate runtime ownership-safety preflight classifier (Phase 5A) -- answers exactly one question, "is it safe for MAIN to reconcile this GoldenGate runtime deployment?", as one of ABSENT/OWNED/BROKEN. This is NOT a HEALTHY-skip prerequisite classifier: a GoldenGate runtime is an actual desired deployment target whose descriptor/image/chart may intentionally change on every run, so OWNED (not HEALTHY) is the "safe to reconcile" state -- readiness/health is validated separately, post-reconciliation, by automation/phases/phase5/runtime_acceptance.py. Never mutates the cluster: every kubectl invocation here is a `get` (read-only); no apply/create/delete/patch/annotate/label/helm call exists in this module. Consumes deployment identity through automation/goldengate-deployment-model.py's `describe` output (the same canonical folder-driven descriptor resolver used everywhere else), never a second descriptor schema."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_module(name, path):
    """Lazy import of a repo module by explicit path (pathlib-based, never fragile ".." string arithmetic) -- the same importlib.util convention this repo already uses for automation/goldengate-environment.py, so this module never depends on sys.path/CWD."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# k8s_common.py is genuinely cross-phase (shared by the Phase 4 Platform/Observability classifiers too) and stays under automation/orchestration/ -- never moved, never duplicated here.
_k8s_common = _load_module("k8s_common", REPO_ROOT / "automation" / "orchestration" / "k8s_common.py")
ClassifierInspectionError = _k8s_common.ClassifierInspectionError
KubectlRunner = _k8s_common.KubectlRunner
get_json = _k8s_common.get_json

_ENVIRONMENT_MODULE_PATH = REPO_ROOT / "automation" / "goldengate-environment.py"
_DEPLOYMENT_MODEL_MODULE_PATH = REPO_ROOT / "automation" / "goldengate-deployment-model.py"
_environment_module = None
_deployment_model_module = None


_environment_module = None
_deployment_model_module = None


def _load_environment_module():
    """Lazy import of automation/goldengate-environment.py -- the single canonical environment-config parser/deriver. Never a second independent schema implementation."""
    global _environment_module
    if _environment_module is None:
        _environment_module = _load_module("goldengate_environment", _ENVIRONMENT_MODULE_PATH)
    return _environment_module


def _load_deployment_model_module():
    """Lazy import of automation/goldengate-deployment-model.py -- the single canonical folder-driven descriptor resolver. Never a second independent descriptor schema."""
    global _deployment_model_module
    if _deployment_model_module is None:
        _deployment_model_module = _load_module("goldengate_deployment_model", _DEPLOYMENT_MODEL_MODULE_PATH)
    return _deployment_model_module


def environment_derived_values(environment):
    """Loads+validates+derives envs/<environment>/environment.yaml via the canonical resolver."""
    env_module = _load_environment_module()
    env_module.REPO_ROOT = REPO_ROOT
    doc = env_module.load_environment_config(environment)
    return env_module.derive_values(doc)


def describe_deployment(environment, deployment_id):
    """Returns the canonical descriptor dict for one deployment ID via automation/goldengate-deployment-model.py's own scan/validation -- exactly what `describe` prints, never re-parsed independently. Raises ValueError (a configuration error, never a cluster inspection error) if the folder-driven model itself has a problem or the deployment ID is unknown."""
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
# Phase 5 Runtime Application Self-Healing: an existing, correctly-owned STANDALONE Application (no runtime ApplicationSet yet) is never silently folded into OWNED -- OWNED means "the ApplicationSet-owned self-healing shape is already in place", which a pre-feature standalone Application is not. MIGRATION_CANDIDATE is a distinct, explicit fourth state so callers must handle the one-time migration deliberately (create the runtime's ApplicationSet with a generated child spec proven byte-for-byte equal to the existing standalone Application) rather than proceeding as if nothing needs to change.
STATE_MIGRATION_CANDIDATE = "MIGRATION_CANDIDATE"

# Current Helm/main-workflow naming contract (helm/goldengate/templates/_helpers.tpl, 00-main-goldengate-orchestrator.yaml) -- verified against the real vendored chart, never guessed.
HELM_REPO_PATH = "helm/goldengate"

# Resources whose ownership is verified via the shared goldengate.runtimeLabels helper (app.kubernetes.io/instance == deployment ID, goldengate.adcb/deployment-name == deployment ID, goldengate.adcb/environment == environment).

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


def _appset_name(app_name):
    """ONE canonical ApplicationSet name derivation -- always <application-name>-appset, mirrored (never imported, matching this file's existing self-contained convention already used for _app_suffix/app_name) by automation/phases/phase5/phase5_runtime.py's _canonical_appset_name() and automation/phases/phase5/runtime_acceptance.py's own copy; a dedicated drift test in automation/phases/phase5/tests/test_phase5_runtime.py proves all three agree."""
    return f"{app_name}-appset"


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

# The single canonical footprint-key set this classifier ever reports in checks["footprint_found"] -- exposed here so callers (Phase 5C post-delete acceptance) can validate they received the complete expected schema without maintaining a second, independently-drifting key list. Always exactly the key set of _K8S_RESOURCE_TYPE (itself equal to _expected_footprint_names()'s own keys).
RUNTIME_FOOTPRINT_KEYS = frozenset(_K8S_RESOURCE_TYPE)


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


def _check_application_ownership(app_obj, app_name, environment, deployment_id, runtime_namespace, expected_repo_url):
    """Factored out of classify() so both the no-ApplicationSet path (a still-present standalone Application) and the ApplicationSet-owned path (verifying its generated child) run the exact same Application-identity checks -- never two independently-drifting copies. Returns a list of reason strings (empty means the Application's own identity is fully compatible with this deployment); deliberately never checks status.sync.status/status.health.status/spec.source.targetRevision here -- this remains a pre-reconciliation ownership-safety check, not a readiness classifier."""
    reasons = []
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

    return reasons


def _check_applicationset_ownership(appset_obj, appset_name, app_name, environment, deployment_id, runtime_namespace, expected_repo_url):
    """Verifies the runtime ApplicationSet itself is genuinely this deployment's own -- expected name (via the caller), namespace label match, environment/deployment-id labels, and (where visible) the generated-Application identity/destination/repo declared inside spec.template -- never merely "an ApplicationSet with this name exists". A foreign/mislabeled ApplicationSet, or one whose template would generate a DIFFERENT Application than this deployment's own canonical name/namespace/repo, is BROKEN, exactly like a foreign standalone Application always has been."""
    reasons = []
    labels = ((appset_obj.get("metadata") or {}).get("labels")) or {}
    actual_env_label = labels.get("goldengate.adcb/environment")
    actual_id_label = labels.get("goldengate.adcb/deployment-id")
    if actual_env_label != environment:
        reasons.append(f"ApplicationSet {appset_name} label goldengate.adcb/environment={actual_env_label!r}, expected {environment!r}")
    if actual_id_label != deployment_id:
        reasons.append(f"ApplicationSet {appset_name} label goldengate.adcb/deployment-id={actual_id_label!r}, expected {deployment_id!r}")

    template = ((appset_obj.get("spec") or {}).get("template")) or {}
    template_metadata = template.get("metadata") or {}
    template_spec = template.get("spec") or {}

    actual_template_name = template_metadata.get("name")
    if actual_template_name != app_name:
        reasons.append(f"ApplicationSet {appset_name} spec.template.metadata.name={actual_template_name!r}, expected {app_name!r}")

    actual_template_dest_ns = ((template_spec.get("destination") or {})).get("namespace")
    if actual_template_dest_ns != runtime_namespace:
        reasons.append(f"ApplicationSet {appset_name} spec.template.spec.destination.namespace={actual_template_dest_ns!r}, expected {runtime_namespace!r}")

    actual_template_repo_url = ((template_spec.get("source") or {})).get("repoURL")
    if actual_template_repo_url != expected_repo_url:
        reasons.append(f"ApplicationSet {appset_name} spec.template.spec.source.repoURL={actual_template_repo_url!r}, expected {expected_repo_url!r}")

    return reasons


def classify(run, environment, deployment_id, argocd_namespace, runtime_namespace, ecr_registry, retained_pvc_expected=False):
    """Returns the stable {"state", "environment", "deployment_id", "namespace", "reasons", "checks"} shape, state one of ABSENT/OWNED/BROKEN/MIGRATION_CANDIDATE. Raises ClassifierInspectionError if Kubernetes access itself could not be trusted -- callers must let that propagate as a hard failure, never a downgrade to ABSENT. Raises ValueError if the folder-driven model itself is inconsistent (invalid descriptors/cross-descriptor problems elsewhere) -- a configuration error, never a cluster state. retained_pvc_expected (default False, byte-for-byte unchanged default behavior) is an OPTIONAL, EXPLICIT deletion-context hint for Phase 5C removal callers only: when the deployment's own descriptor still exists, "Application absent + only the retained PVC" is already recognized as safe via declares_chart_owned_persistence below and this hint changes nothing; it matters only for a PHYSICALLY REMOVED descriptor (no envs/<environment>/<deployment_id>/values.yaml exists any more), where declares_chart_owned_persistence can never be computed -- when the caller has independently validated (from a Phase 1 deletion_matrix entry) that the prior valid descriptor declared EFS persistence, passing retained_pvc_expected=True lets this classifier recognize the same "Application absent, ONLY the retained PVC exists" shape as safe, while the PVC's own ownership labels are still verified unconditionally below regardless of this hint -- a foreign/mislabeled PVC under the expected name is never silently adopted. Phase 5 Runtime Application Self-Healing: the runtime ApplicationSet is now the PRIMARY ownership signal -- when it exists and is correctly owned (_check_applicationset_ownership passes), the generated child Application's own absence is a RECOVERABLE OWNED state (the ApplicationSet controller is expected to recreate it on its own, so this classifier must never treat "Application absent, ApplicationSet owns it" as an orphan-footprint BROKEN condition the way it always has for a bare standalone Application); when the ApplicationSet is absent but a standalone Application exists and passes the exact same ownership checks a real OWNED Application always has, that is MIGRATION_CANDIDATE, not OWNED -- a caller must explicitly perform the one-time migration (create the ApplicationSet with a generated child spec proven byte-for-byte equal to the existing Application) rather than treating "nothing to do" as the safe interpretation; a foreign/mislabeled ApplicationSet, or a foreign child Application existing under the expected name despite a correctly-owned ApplicationSet, both remain BROKEN, exactly as a foreign standalone Application always has been."""
    # Confirms the folder-driven model is internally consistent before any cluster call -- fails closed if ANY descriptor in the environment is invalid, the same guard the reconcile path already relies on. Deliberately does NOT require THIS deployment_id's own descriptor to still be present: this classifier is also reused for a PHYSICALLY REMOVED descriptor's leftover live resources (GoldenGate Runtime Presence Contract Finalization -- ownership-safe delete, deletion_matrix reason=physical-removal), where by design no envs/<environment>/<deployment_id>/values.yaml exists any more; the caller (delete_removed_argocd_applications) already independently proved this ID was a genuine GoldenGate deployment before it ever reached this classifier.
    descriptor = None
    try:
        descriptor = describe_deployment(environment, deployment_id)
    except ValueError as exc:
        if "unknown deployment ID" not in str(exc):
            raise

    app_suffix = _app_suffix(deployment_id)
    app_name = f"goldengate-{environment}-{app_suffix}"
    appset_name = _appset_name(app_name)
    expected_repo_url = f"oci://{ecr_registry}/{HELM_REPO_PATH}"

    reasons = []
    checks = {}

    appset_found, appset_obj = get_json(run, "applicationset", appset_name, argocd_namespace)
    checks["applicationset_found"] = appset_found

    app_found, app_obj = get_json(run, "application", app_name, argocd_namespace)
    checks["application_found"] = app_found

    footprint_names = _expected_footprint_names(environment, deployment_id, runtime_namespace)
    footprint = {}
    for label, (name, namespace) in footprint_names.items():
        found, obj = get_json(run, _K8S_RESOURCE_TYPE[label], name, namespace)
        footprint[label] = (found, obj)
    checks["footprint_found"] = {label: found for label, (found, _obj) in footprint.items()}

    any_footprint_found = any(found for found, _obj in footprint.values())

    # ABSENT: no ApplicationSet, no owning Application, and no meaningful expected-name footprint at all -- safe to create from nothing.
    if not appset_found and not app_found and not any_footprint_found:
        return {"state": STATE_ABSENT, "environment": environment, "deployment_id": deployment_id, "namespace": runtime_namespace, "reasons": [], "checks": checks}

    # GoldenGate Runtime Presence Contract -- Final Safety Correction, Gap 5: the u02 PVC is intentionally retained across Application deletion (Prune=false, see helm/goldengate/templates/runtime-pvc.yaml) -- "Application absent, ONLY the retained PVC exists" is the expected, SAFE shape of a disabled-then-re-enableable runtime, never an unexplained orphan on its own. Every OTHER compute/workload footprint kind (StatefulSet/Service/headless Service/StorageClass/SecretProviderClasses/admin Secret) is still pruned/cascade-deleted as normal and remains exactly as unsafe as before when found without an owning Application AND without an owning ApplicationSet. "Chart-owned" persistence means the descriptor both declares EFS persistence (efsMode is not None) AND the chart actually creates its own PVC rather than referencing a pre-existing one via runtime.storage.u02.existingClaim (pvcClaimName empty) -- the SAME condition helm/goldengate/templates/runtime-pvc.yaml itself renders on.
    declares_chart_owned_persistence = bool(descriptor and descriptor.get("efsMode") and not descriptor.get("pvcClaimName"))
    pvc_found, _pvc_obj = footprint[_PVC_KIND]
    non_pvc_footprint_found = any(found for label, (found, _obj) in footprint.items() if label != _PVC_KIND)

    appset_owned = False
    if appset_found:
        appset_reasons = _check_applicationset_ownership(appset_obj, appset_name, app_name, environment, deployment_id, runtime_namespace, expected_repo_url)
        if appset_reasons:
            # State #4: a foreign/mislabeled ApplicationSet exists under the expected name -- BROKEN regardless of anything else below; never silently ignored in favor of the (possibly absent) Application/footprint.
            reasons.extend(appset_reasons)
        else:
            appset_owned = True

    if appset_owned:
        # State #2/#3: the ApplicationSet itself is correctly owned. The generated child Application's presence/absence is verified, but its ABSENCE is a recoverable OWNED condition (the ApplicationSet controller is expected to recreate it), never an orphan-footprint BROKEN one -- this is the entire self-healing point of this feature. A child that DOES exist under the expected name must still be genuinely this deployment's own (State #7: a foreign/unrelated child under the expected name remains BROKEN).
        if app_found:
            reasons.extend(_check_application_ownership(app_obj, app_name, environment, deployment_id, runtime_namespace, expected_repo_url))
        # Footprint ownership-label checks still apply unconditionally below; the "orphan non-PVC footprint without an owning Application" rule from the no-ApplicationSet path is deliberately NOT applied here -- a correctly-owned ApplicationSet is itself the authoritative ownership signal for any leftover-from-last-sync compute footprint while the child Application is being recreated.
    elif not appset_found:
        # No ApplicationSet at all: preserve the exact pre-feature ownership-safety behavior for the Application/footprint, with ONE addition -- a standalone Application that passes every existing ownership check is MIGRATION_CANDIDATE, never silently treated as the fully self-healing OWNED shape (State #6).
        if not app_found:
            if non_pvc_footprint_found:
                owned_names = [label for label, (found, _obj) in footprint.items() if found and label != _PVC_KIND]
                reasons.append(f"Application {app_name} does not exist in {argocd_namespace} but expected-name runtime resource(s) already exist: {owned_names!r}")
            elif pvc_found and not declares_chart_owned_persistence and not retained_pvc_expected:
                reasons.append(f"Application {app_name} does not exist in {argocd_namespace} but a retained persistence PVC exists although this deployment's descriptor does not declare chart-owned EFS persistence -- not the recognized retained-persistence footprint, treated as an unexplained orphan")
            # else: Application and ApplicationSet both absent, ONLY the retained PVC exists, and either this deployment's descriptor legitimately declares chart-owned EFS persistence OR the caller passed the explicit retained_pvc_expected hint -- the recognized "disabled/removed runtime, durable /u02 data retained for a future re-enable" shape. Its own ownership labels are still verified unconditionally below, exactly like every other footprint kind -- a foreign/mislabeled PVC under the expected name is never silently adopted.
        else:
            reasons.extend(_check_application_ownership(app_obj, app_name, environment, deployment_id, runtime_namespace, expected_repo_url))
    # else: appset_found but NOT appset_owned -- appset_reasons above already guarantees BROKEN; the Application/footprint are not independently re-classified in this branch.

    # Any expected-name resource that currently exists must carry compatible ownership, regardless of whether the Application/ApplicationSet was found -- this is what actually distinguishes a safe partial-OWNED footprint (this deployment's own prior partial rollout) from a foreign/orphaned collision.
    for label, (found, obj) in footprint.items():
        if not found or label == _ADMIN_SECRET_KIND:
            continue
        reason = _ownership_reason(label, obj, environment, deployment_id)
        if reason:
            reasons.append(reason)

    # State resolution: any accumulated reason -> BROKEN, regardless of source (foreign ApplicationSet, foreign Application, foreign footprint). Otherwise: appset_owned -> OWNED (State #2/#3, the self-healing shape is already in place, including the recoverable "child temporarily missing" case); no ApplicationSet but a standalone Application was found clean -> MIGRATION_CANDIDATE (State #6); every other clean shape (no ApplicationSet, no Application, only a legitimately-retained PVC, or truly nothing) -> OWNED, matching this classifier's pre-feature behavior for those disabled/removed shapes.
    if reasons:
        state = STATE_BROKEN
    elif appset_owned:
        state = STATE_OWNED
    elif not appset_found and app_found:
        state = STATE_MIGRATION_CANDIDATE
    else:
        state = STATE_OWNED

    return {"state": state, "environment": environment, "deployment_id": deployment_id, "namespace": runtime_namespace, "reasons": reasons, "checks": checks}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--kubectl-bin", default="kubectl")
    parser.add_argument("--retained-pvc-expected", action="store_true", default=False, help="Explicit Phase 5C removal-only hint: recognize 'Application absent, only the expected-name retained PVC exists' as safe even when the descriptor itself was physically removed. Default behavior (omitted) is byte-for-byte unchanged.")
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
            retained_pvc_expected=args.retained_pvc_expected,
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
