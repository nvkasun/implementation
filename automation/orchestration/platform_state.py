#!/usr/bin/env python3
"""automation/orchestration/platform_state.py: read-only GoldenGate Platform ownership-safety preflight classifier -- answers exactly one question, "is it safe for MAIN to reconcile the GoldenGate Platform installation?", as one of ABSENT/OWNED/BROKEN. This is NOT a HEALTHY-skip prerequisite classifier: the platform's desired state (Fluent Bit image/config, shared ServiceAccount role ARNs, any future values-driven resource) may legitimately change on every run, so OWNED (not HEALTHY) is the "safe to reconcile" state -- readiness/exact-desired-state acceptance is validated separately, post-reconciliation, by automation/orchestration/platform_acceptance.py. Generic by design: this module checks OWNERSHIP LABELS on whatever currently exists, never the correctness/readiness of any individual resource (including the runtime namespace's app.kubernetes.io/managed-by label, which is a desired-state acceptance concern, not an ownership one) -- adding a new values-driven chart resource, or flipping an existing one false<->true, never requires touching this file. Never mutates the cluster: every kubectl invocation here is a `get` (read-only); no apply/create/delete/patch/annotate/label/helm call exists in this module. Consumes environment identity through automation/goldengate-environment.py, never a second environment parser."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys


def _load_sibling_module(name, filename):
    """Lazy import of a same-directory automation/orchestration/ module by explicit file path -- the same importlib.util convention this repo already uses for automation/goldengate-environment.py, so this module never depends on sys.path/CWD."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_k8s_common = _load_sibling_module("k8s_common", "k8s_common.py")
ClassifierInspectionError = _k8s_common.ClassifierInspectionError
KubectlRunner = _k8s_common.KubectlRunner
get_json = _k8s_common.get_json
list_json = _k8s_common.list_json

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

_ENVIRONMENT_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "goldengate-environment.py")
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

# Current chart/workflow contract (helm/goldengate-platform/, .github/workflows/30-sub-platform.yaml) -- verified against the real vendored chart/values.yaml and workflow, never guessed. Only used here to name the resources this module inspects for OWNERSHIP; individual field correctness (Fluent Bit image/shape, IRSA role-arn, namespace managed-by label, readiness) is deliberately out of scope -- that is platform_acceptance.py's job.
HELM_REPO_PATH = "helm/goldengate-platform"

RUNTIME_SA_NAME = "gg-runtime-sa"
FLUENT_BIT_SA_NAME = "gg-fluent-bit"
FLUENT_BIT_CLUSTERROLE_NAME = "gg-fluent-bit"
FLUENT_BIT_CLUSTERROLEBINDING_NAME = "gg-fluent-bit"
FLUENT_BIT_CONFIGMAP_NAME = "gg-fluent-bit-config"
FLUENT_BIT_DAEMONSET_NAME = "gg-fluent-bit"

# helm/goldengate-platform/templates/_helpers.tpl's goldengate-platform.labels/goldengate-platform.fluentBit.labels helpers, rendered on every chart resource except the Namespace (which this chart no longer renders at all -- namespaces.runtime.create stays false) -- app.kubernetes.io/instance == the Helm release name identifies this exact platform release, distinguishing it from any foreign same-name resource.
NAMESPACE_OWNERSHIP_NAME_LABEL = "goldengate-platform"


def _release_and_app_name(environment):
    """RELEASE_NAME == ARGOCD_APP_NAME == goldengate-<environment>-platform (.github/workflows/30-sub-platform.yaml "Prepare platform deployment variables" step) -- both derived from the same canonical environment, never independently maintained literals."""
    name = f"goldengate-{environment}-platform"
    return name, name


def _labels_of(obj):
    return ((obj.get("metadata") or {}).get("labels")) or {}


def _instance_owned_reason(resource_label, obj, release_name, environment):
    labels = _labels_of(obj)
    instance = labels.get("app.kubernetes.io/instance")
    env_label = labels.get("goldengate.adcb/environment")
    if instance != release_name or env_label != environment:
        return (
            f"{resource_label} has incompatible ownership labels (app.kubernetes.io/instance={instance!r}, "
            f"goldengate.adcb/environment={env_label!r}), expected app.kubernetes.io/instance={release_name!r} "
            f"goldengate.adcb/environment={environment!r} -- possible foreign/ambiguous ownership"
        )
    return None


def classify(run, environment, runtime_namespace, argocd_namespace, ecr_registry):
    """Returns the stable {"state", "environment", "namespace", "reasons", "checks"} shape. Raises ClassifierInspectionError if Kubernetes access itself could not be trusted -- callers must let that propagate as a hard failure, never a downgrade to ABSENT."""
    reasons = []
    checks = {}

    release_name, app_name = _release_and_app_name(environment)
    expected_repo_url = f"oci://{ecr_registry}/{HELM_REPO_PATH}"

    app_found, app_obj = get_json(run, "application", app_name, argocd_namespace)
    checks["application_found"] = app_found

    ns_found, ns_obj = get_json(run, "namespace", runtime_namespace)
    checks["namespace_found"] = ns_found

    cr_found, cr_obj = get_json(run, "clusterrole", FLUENT_BIT_CLUSTERROLE_NAME)
    checks["fluent_bit_clusterrole_found"] = cr_found

    crb_found, crb_obj = get_json(run, "clusterrolebinding", FLUENT_BIT_CLUSTERROLEBINDING_NAME)
    checks["fluent_bit_clusterrolebinding_found"] = crb_found

    # ABSENT: no meaningful footprint at all -- the platform chart is the designated owner of the shared runtime namespace, so any one of these existing without the rest means this classifier does not get to silently take ownership of partial/pre-existing state; it falls through to the ownership-label checks below instead.
    if not app_found and not ns_found and not cr_found and not crb_found:
        return {"state": STATE_ABSENT, "environment": environment, "namespace": runtime_namespace, "reasons": [], "checks": checks}

    if app_found:
        spec = app_obj.get("spec") or {}
        source = spec.get("source") or {}
        destination = spec.get("destination") or {}
        helm_source = source.get("helm") or {}

        actual_repo_url = source.get("repoURL")
        if actual_repo_url != expected_repo_url:
            reasons.append(f"Application {app_name} source.repoURL={actual_repo_url!r}, expected {expected_repo_url!r} -- possible foreign/ambiguous ownership")

        actual_dest_ns = destination.get("namespace")
        if actual_dest_ns != runtime_namespace:
            reasons.append(f"Application {app_name} destination.namespace={actual_dest_ns!r}, expected {runtime_namespace!r} -- possible foreign/ambiguous ownership")

        actual_release_name = helm_source.get("releaseName")
        if actual_release_name != release_name:
            reasons.append(f"Application {app_name} source.helm.releaseName={actual_release_name!r}, expected {release_name!r} -- possible foreign/ambiguous ownership")

        # Deliberately NOT checked here: status.sync.status / status.health.status. This is a pre-reconciliation ownership-safety classifier, not a readiness classifier -- an OutOfSync/Progressing/Degraded Application that otherwise clearly belongs to this platform is exactly what MAIN is about to reconcile, never an ownership conflict. Post-reconciliation health is platform_acceptance.py's job.

    if ns_found:
        # A Terminating namespace is a genuine safety failure, never merely stale desired state -- applying resources into a namespace mid-deletion is unsafe, so this alone still forces BROKEN (unlike every other check in this module, which only fires on an actual ownership-label mismatch).
        ns_phase = ((ns_obj.get("status") or {}).get("phase"))
        if ns_phase == "Terminating":
            reasons.append(f"namespace {runtime_namespace} is Terminating -- unsafe to reconcile into")

        # app.kubernetes.io/name is the one ownership signal present regardless of which mechanism currently owns this namespace's metadata (this chart's own now-disabled Namespace template, or Argo CD's managedNamespaceMetadata) -- app.kubernetes.io/managed-by is deliberately NOT checked here: it is exactly the kind of desired-state drift (Helm vs argocd) normal reconciliation converges, never an ownership conflict. See platform_acceptance.py for the strict managed-by=argocd post-reconcile check.
        ns_labels = _labels_of(ns_obj)
        if ns_labels.get("app.kubernetes.io/name") != NAMESPACE_OWNERSHIP_NAME_LABEL:
            reasons.append(
                f"namespace {runtime_namespace} label app.kubernetes.io/name={ns_labels.get('app.kubernetes.io/name')!r}, "
                f"expected {NAMESPACE_OWNERSHIP_NAME_LABEL!r} -- possible foreign/ambiguous ownership"
            )

    # Cluster-scoped resources (never namespace-gated -- a ClusterRole/ClusterRoleBinding exists independently of the runtime namespace). helm/goldengate-platform/templates/fluent-bit-rbac.yaml renders both with the same goldengate-platform.fluentBit.labels helper as the namespaced Fluent Bit resources below, so the same _instance_owned_reason ownership check applies -- a foreign same-name ClusterRole/ClusterRoleBinding must never silently contribute to an OWNED footprint. Missing is never a reason here either: that is exactly what reconciliation is for.
    if cr_found:
        reason = _instance_owned_reason(f"clusterrole/{FLUENT_BIT_CLUSTERROLE_NAME}", cr_obj, release_name, environment)
        if reason:
            reasons.append(reason)

    if crb_found:
        reason = _instance_owned_reason(f"clusterrolebinding/{FLUENT_BIT_CLUSTERROLEBINDING_NAME}", crb_obj, release_name, environment)
        if reason:
            reasons.append(reason)

    # The remaining checks only make sense once the runtime namespace exists. Ownership-label proof on whatever currently exists -- deliberately NOT a completeness/readiness check: a resource that is simply missing is exactly what MAIN is about to reconcile, never itself a reason.
    if ns_found:
        sa_found, sa_obj = get_json(run, "serviceaccount", RUNTIME_SA_NAME, runtime_namespace)
        checks["runtime_serviceaccount_found"] = sa_found
        if sa_found:
            reason = _instance_owned_reason(f"serviceaccount/{RUNTIME_SA_NAME}", sa_obj, release_name, environment)
            if reason:
                reasons.append(reason)

        fb_sa_found, fb_sa_obj = get_json(run, "serviceaccount", FLUENT_BIT_SA_NAME, runtime_namespace)
        checks["fluent_bit_serviceaccount_found"] = fb_sa_found
        if fb_sa_found:
            reason = _instance_owned_reason(f"serviceaccount/{FLUENT_BIT_SA_NAME}", fb_sa_obj, release_name, environment)
            if reason:
                reasons.append(reason)

        cm_found, cm_obj = get_json(run, "configmap", FLUENT_BIT_CONFIGMAP_NAME, runtime_namespace)
        checks["fluent_bit_configmap_found"] = cm_found
        if cm_found:
            reason = _instance_owned_reason(f"configmap/{FLUENT_BIT_CONFIGMAP_NAME}", cm_obj, release_name, environment)
            if reason:
                reasons.append(reason)

        ds_found, ds_obj = get_json(run, "daemonset", FLUENT_BIT_DAEMONSET_NAME, runtime_namespace)
        checks["fluent_bit_daemonset_found"] = ds_found
        if ds_found:
            reason = _instance_owned_reason(f"daemonset/{FLUENT_BIT_DAEMONSET_NAME}", ds_obj, release_name, environment)
            if reason:
                reasons.append(reason)

        # The platform release intentionally owns shared namespace/identity/logging resources only -- it must never own a GoldenGate runtime StatefulSet/Deployment. This is a genuine ownership-collision check (not a readiness one), so it stays here.
        owned_statefulsets = list_json(run, "statefulset", namespace=runtime_namespace, label_selector=f"app.kubernetes.io/instance={release_name}")
        owned_deployments = list_json(run, "deployment", namespace=runtime_namespace, label_selector=f"app.kubernetes.io/instance={release_name}")
        checks["owned_runtime_workload_count"] = len(owned_statefulsets) + len(owned_deployments)
        if owned_statefulsets or owned_deployments:
            names = sorted([s.get("metadata", {}).get("name") for s in owned_statefulsets] + [d.get("metadata", {}).get("name") for d in owned_deployments])
            reasons.append(f"platform release {release_name} unexpectedly owns StatefulSet/Deployment resource(s): {names!r} -- foreign/ambiguous ownership collision")

    state = STATE_BROKEN if reasons else STATE_OWNED
    return {"state": state, "environment": environment, "namespace": runtime_namespace, "reasons": reasons, "checks": checks}


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
            runtime_namespace=values["RUNTIME_NAMESPACE"],
            argocd_namespace=values["ARGOCD_NAMESPACE"],
            ecr_registry=values["ECR_REGISTRY"],
        )
    except (ClassifierInspectionError, OSError) as exc:
        print(f"INSPECTION ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result))
    if result["reasons"]:
        print("GoldenGate Platform ownership-safety diagnostics:", file=sys.stderr)
        for reason in result["reasons"]:
            print(f"  - {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
