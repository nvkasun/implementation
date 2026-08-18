#!/usr/bin/env python3
"""hack/orchestration/monitor_state.py: read-only shared GoldenGate monitor ownership-safety preflight classifier (Phase B3B) -- answers exactly one question, "is it safe for MAIN to reconcile the shared gg-monitor deployment?", as one of ABSENT/OWNED/BROKEN. This is NOT a HEALTHY-skip prerequisite classifier: like a GoldenGate runtime, the monitor's canonical registry input changes whenever the GLOBAL active runtime inventory changes, so a currently-healthy monitor may still need reconciliation -- OWNED (not HEALTHY) is the "safe to reconcile" state. Readiness/health is validated separately, post-reconciliation, by hack/orchestration/monitor_acceptance.py. Never mutates the cluster: every kubectl invocation here is a `get` (read-only); no apply/create/delete/patch/annotate/label/helm call exists in this module."""
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


STATE_ABSENT = "ABSENT"
STATE_OWNED = "OWNED"
STATE_BROKEN = "BROKEN"

# Current Helm/main-workflow naming contract (helm/goldengate-monitor/templates/, .github/workflows/50-sub-monitor.yaml) -- verified against the real vendored chart, never guessed.
HELM_REPO_PATH = "helm/goldengate-monitor"
RELEASE_NAME = "gg-monitor"
ARGOCD_APP_NAME = "goldengate-monitor"

DEPLOYMENT_NAME = "gg-monitor"
SERVICE_NAME = "gg-monitor"
SERVICE_ACCOUNT_NAME = "gg-monitor"
SECRETPROVIDERCLASS_NAME = "gg-monitor-secrets"
CONFIGMAP_NAME = "goldengate-monitor-canonical-config"
INGRESS_NAME = "gg-monitor"
NETWORKPOLICY_NAME = "gg-monitor"

# Resource kind, and whether it lives inside MONITOR_NAMESPACE (namespace itself is cluster-scoped).
_FOOTPRINT_RESOURCE_TYPES = {
    "namespace": "namespace",
    "deployment": "deployment",
    "service": "service",
    "serviceaccount": "serviceaccount",
    "secretproviderclass": "secretproviderclass",
    "configmap": "configmap",
    "ingress": "ingress",
    "networkpolicy": "networkpolicy",
}


def _footprint_names(monitor_namespace):
    return {
        "namespace": (monitor_namespace, None),
        "deployment": (DEPLOYMENT_NAME, monitor_namespace),
        "service": (SERVICE_NAME, monitor_namespace),
        "serviceaccount": (SERVICE_ACCOUNT_NAME, monitor_namespace),
        "secretproviderclass": (SECRETPROVIDERCLASS_NAME, monitor_namespace),
        "configmap": (CONFIGMAP_NAME, monitor_namespace),
        "ingress": (INGRESS_NAME, monitor_namespace),
        "networkpolicy": (NETWORKPOLICY_NAME, monitor_namespace),
    }


def _resource_ownership_reason(resource_label, obj, environment):
    """helm/goldengate-monitor/templates/_helpers.tpl's goldengate-monitor.labels helper (rendered on every chart resource except the Namespace, which uses its own literal label set checked separately) -- app.kubernetes.io/instance=gg-monitor (the Helm release name) + goldengate.adcb/environment=<environment> together identify this exact monitor release, distinguishing it from any foreign same-name resource."""
    labels = ((obj.get("metadata") or {}).get("labels")) or {}
    instance = labels.get("app.kubernetes.io/instance")
    env_label = labels.get("goldengate.adcb/environment")
    if instance != RELEASE_NAME or env_label != environment:
        return (
            f"{resource_label} has incompatible ownership labels (app.kubernetes.io/instance={instance!r}, "
            f"goldengate.adcb/environment={env_label!r}), expected app.kubernetes.io/instance={RELEASE_NAME!r} goldengate.adcb/environment={environment!r}"
        )
    return None


def classify(run, environment, argocd_namespace, monitor_namespace, ecr_registry):
    """Returns the stable {"state", "environment", "namespace", "reasons", "checks"} shape. Raises ClassifierInspectionError if Kubernetes access itself could not be trusted -- callers must let that propagate as a hard failure, never a downgrade to ABSENT."""
    reasons = []
    checks = {}

    expected_repo_url = f"oci://{ecr_registry}/{HELM_REPO_PATH}"

    app_found, app_obj = get_json(run, "application", ARGOCD_APP_NAME, argocd_namespace)
    checks["application_found"] = app_found

    footprint_names = _footprint_names(monitor_namespace)
    footprint = {}
    for label, (name, namespace) in footprint_names.items():
        found, obj = get_json(run, _FOOTPRINT_RESOURCE_TYPES[label], name, namespace)
        footprint[label] = (found, obj)
    checks["footprint_found"] = {label: found for label, (found, _obj) in footprint.items()}

    any_footprint_found = any(found for found, _obj in footprint.values())

    # ABSENT: no owning Application and no meaningful expected-name footprint at all -- safe to create from nothing. Any one of these existing without the Application means this classifier must not silently adopt orphaned/partial state.
    if not app_found and not any_footprint_found:
        return {"state": STATE_ABSENT, "environment": environment, "namespace": monitor_namespace, "reasons": [], "checks": checks}

    if not app_found:
        owned_names = [label for label, (found, _obj) in footprint.items() if found]
        reasons.append(f"Application {ARGOCD_APP_NAME} does not exist in {argocd_namespace} but expected-name monitor resource(s) already exist: {owned_names!r}")
    else:
        labels = ((app_obj.get("metadata") or {}).get("labels")) or {}
        if labels.get("app.kubernetes.io/name") != "gg-monitor":
            reasons.append(f"Application {ARGOCD_APP_NAME} label app.kubernetes.io/name={labels.get('app.kubernetes.io/name')!r}, expected 'gg-monitor'")
        if labels.get("app.kubernetes.io/managed-by") != "argocd":
            reasons.append(f"Application {ARGOCD_APP_NAME} label app.kubernetes.io/managed-by={labels.get('app.kubernetes.io/managed-by')!r}, expected 'argocd'")

        actual_app_namespace = (app_obj.get("metadata") or {}).get("namespace")
        if actual_app_namespace != argocd_namespace:
            reasons.append(f"Application {ARGOCD_APP_NAME} metadata.namespace={actual_app_namespace!r}, expected {argocd_namespace!r}")

        spec = app_obj.get("spec") or {}
        destination = spec.get("destination") or {}
        source = spec.get("source") or {}
        helm_source = source.get("helm") or {}

        actual_dest_ns = destination.get("namespace")
        if actual_dest_ns != monitor_namespace:
            reasons.append(f"Application {ARGOCD_APP_NAME} destination.namespace={actual_dest_ns!r}, expected {monitor_namespace!r}")

        actual_repo_url = source.get("repoURL")
        if actual_repo_url != expected_repo_url:
            reasons.append(f"Application {ARGOCD_APP_NAME} source.repoURL={actual_repo_url!r}, expected {expected_repo_url!r}")

        actual_release_name = helm_source.get("releaseName")
        if actual_release_name != RELEASE_NAME:
            reasons.append(f"Application {ARGOCD_APP_NAME} source.helm.releaseName={actual_release_name!r}, expected {RELEASE_NAME!r}")

        # Deliberately NOT checked here: status.sync.status / status.health.status / spec.source.targetRevision. This is a pre-reconciliation ownership-safety classifier, not a readiness classifier -- an OutOfSync/Progressing/Degraded Application (or one with a missing/unhealthy Deployment) that otherwise clearly belongs to this monitor is exactly what MAIN is about to reconcile, not an ownership conflict. Post-reconciliation health is monitor_acceptance.py's job.

    # Any expected-name resource that currently exists must carry compatible ownership, regardless of whether the Application itself was found.
    for label, (found, obj) in footprint.items():
        if not found or label == "namespace":
            continue
        reason = _resource_ownership_reason(label, obj, environment)
        if reason:
            reasons.append(reason)

    ns_found, ns_obj = footprint["namespace"]
    if ns_found:
        # helm/goldengate-monitor/templates/namespace.yaml renders its own literal label set (never via goldengate-monitor.labels): app.kubernetes.io/name=gg-monitor, app.kubernetes.io/managed-by=argocd, goldengate.adcb/environment.
        ns_labels = ((ns_obj.get("metadata") or {}).get("labels")) or {}
        if ns_labels.get("app.kubernetes.io/name") != "gg-monitor":
            reasons.append(f"namespace {monitor_namespace} label app.kubernetes.io/name={ns_labels.get('app.kubernetes.io/name')!r}, expected 'gg-monitor'")
        if ns_labels.get("goldengate.adcb/environment") != environment:
            reasons.append(f"namespace {monitor_namespace} label goldengate.adcb/environment={ns_labels.get('goldengate.adcb/environment')!r}, expected {environment!r}")

    state = STATE_BROKEN if reasons else STATE_OWNED
    return {"state": state, "environment": environment, "namespace": monitor_namespace, "reasons": reasons, "checks": checks}


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
            argocd_namespace=values["ARGOCD_NAMESPACE"],
            monitor_namespace=values["MONITOR_NAMESPACE"],
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
        print("GoldenGate monitor ownership-safety diagnostics:", file=sys.stderr)
        for reason in result["reasons"]:
            print(f"  - {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
