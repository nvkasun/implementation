#!/usr/bin/env python3
"""automation/phases/phase4/observability_state.py: read-only Observability (amazon-cloudwatch-observability) ownership-safety preflight classifier -- answers exactly one question, "is it safe for MAIN to reconcile the Observability installation?", as one of ABSENT/OWNED/BROKEN. This is NOT a HEALTHY-skip prerequisite classifier: the observability desired state (chart version, image digests, cloudwatch-agent ServiceAccount role-arn, any future values-driven resource) may legitimately change on every run, so OWNED (not HEALTHY) is the "safe to reconcile" state -- readiness/exact-desired-state acceptance (including workload readiness, image provenance, Agent CR shape, the metrics-only negative-safety contract, and the ServiceAccount IRSA role-arn) is validated separately, post-reconciliation, by automation/phases/phase4/observability_acceptance.py. Generic by design: this module checks OWNERSHIP IDENTITY on the Argo CD Application only (repoURL/destination/release name), never the correctness/readiness of any individual chart-rendered resource -- adding a new values-driven chart resource, or flipping an existing one false<->true, never requires touching this file. The upstream amazon-cloudwatch-observability chart itself is not vendored as source in this repo, so this module deliberately does not assert a per-resource ownership-label schema it cannot prove from source -- the Application identity is the authoritative ownership boundary here. Never mutates the cluster: every kubectl invocation here is a `get` (read-only); no apply/create/delete/patch/annotate/label/helm call exists in this module. Consumes environment identity through automation/goldengate-environment.py, never a second environment parser."""
from __future__ import annotations

import argparse
import importlib.util
import json
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
get_json = _k8s_common.get_json

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

# Current chart/workflow contract (.github/workflows/40-sub-observability.yaml) -- verified against the real workflow's own Application-manifest generation, never guessed. Only used here to name the Application this module inspects for OWNERSHIP; individual resource correctness/readiness (workloads, Agent CR shape, image provenance, forbidden components, IRSA role-arn, chart version) is deliberately out of scope -- that is observability_acceptance.py's job.
HELM_REPO_PATH = "helm/amazon-cloudwatch-observability"
RELEASE_NAME = "amazon-cloudwatch-observability"
ARGOCD_APP_NAME = "goldengate-observability"

# Footprint resources queried ONLY to distinguish "nothing at all" (ABSENT) from "something partial already exists" -- never individually inspected for ownership labels here (the upstream chart is not vendored as source, so this module does not assert a per-resource label schema it cannot prove).
CLOUDWATCH_AGENT_SA_NAME = "cloudwatch-agent"
FOOTPRINT_DEPLOYMENTS = (
    "amazon-cloudwatch-observability-controller-manager",
    "cloudwatch-agent-cluster-scraper",
    "kube-state-metrics",
)
FOOTPRINT_DAEMONSETS = ("cloudwatch-agent", "node-exporter")


def classify(run, environment, observability_namespace, argocd_namespace, ecr_registry):
    """Returns the stable {"state", "environment", "namespace", "reasons", "checks"} shape. Raises ClassifierInspectionError if Kubernetes access itself could not be trusted -- callers must let that propagate as a hard failure, never a downgrade to ABSENT."""
    reasons = []
    checks = {}

    expected_repo_url = f"oci://{ecr_registry}/{HELM_REPO_PATH}"

    app_found, app_obj = get_json(run, "application", ARGOCD_APP_NAME, argocd_namespace)
    checks["application_found"] = app_found

    ns_found, _ = get_json(run, "namespace", observability_namespace)
    checks["namespace_found"] = ns_found

    deploy_found = {name: get_json(run, "deployment", name, observability_namespace)[0] for name in FOOTPRINT_DEPLOYMENTS}
    checks["deployments_found"] = deploy_found
    ds_found = {name: get_json(run, "daemonset", name, observability_namespace)[0] for name in FOOTPRINT_DAEMONSETS}
    checks["daemonsets_found"] = ds_found
    sa_found, _ = get_json(run, "serviceaccount", CLOUDWATCH_AGENT_SA_NAME, observability_namespace)
    checks["cloudwatch_agent_serviceaccount_found"] = sa_found

    any_footprint_found = any(deploy_found.values()) or any(ds_found.values()) or sa_found

    # ABSENT only when the Application, the target namespace, AND every footprint resource are absent -- the Application + target namespace remain the primary ownership boundary, but a lingering footprint resource without the Application must not be silently treated as a clean slate.
    if not app_found and not ns_found and not any_footprint_found:
        return {"state": STATE_ABSENT, "environment": environment, "namespace": observability_namespace, "reasons": [], "checks": checks}

    if not app_found:
        if any_footprint_found:
            owned = [name for name, found in {**deploy_found, **ds_found}.items() if found]
            reasons.append(f"Application {ARGOCD_APP_NAME} does not exist in {argocd_namespace} but expected-name resource(s) already exist: {owned!r} -- possible foreign/ambiguous ownership")
        else:
            reasons.append(f"Application {ARGOCD_APP_NAME} does not exist in {argocd_namespace}")
    else:
        spec = app_obj.get("spec") or {}
        source = spec.get("source") or {}
        destination = spec.get("destination") or {}
        helm_source = source.get("helm") or {}

        actual_repo_url = source.get("repoURL")
        if actual_repo_url != expected_repo_url:
            reasons.append(f"Application {ARGOCD_APP_NAME} source.repoURL={actual_repo_url!r}, expected {expected_repo_url!r} -- possible foreign/ambiguous ownership")

        actual_dest_ns = destination.get("namespace")
        if actual_dest_ns != observability_namespace:
            reasons.append(f"Application {ARGOCD_APP_NAME} destination.namespace={actual_dest_ns!r}, expected {observability_namespace!r} -- possible foreign/ambiguous ownership")

        actual_release_name = helm_source.get("releaseName")
        if actual_release_name != RELEASE_NAME:
            reasons.append(f"Application {ARGOCD_APP_NAME} source.helm.releaseName={actual_release_name!r}, expected {RELEASE_NAME!r} -- possible foreign/ambiguous ownership")

        # Deliberately NOT checked here: status.sync.status / status.health.status / source.targetRevision (chart version) -- this is a pre-reconciliation ownership-safety classifier, not a readiness/version classifier. An OutOfSync/Progressing/Degraded Application, or one pinned to a stale chart version, that otherwise clearly belongs to this Observability installation is exactly what MAIN is about to reconcile, never an ownership conflict. Post-reconciliation health is observability_acceptance.py's job.

    if not ns_found and app_found:
        # The Application exists but the target namespace does not -- not itself unsafe (CreateNamespace=true will create it on reconcile), so this contributes no reason; only complete absence of everything (handled above) or a genuine identity mismatch is a safety concern here.
        pass

    state = STATE_BROKEN if reasons else STATE_OWNED
    return {"state": state, "environment": environment, "namespace": observability_namespace, "reasons": reasons, "checks": checks}


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
            observability_namespace=values["OBSERVABILITY_NAMESPACE"],
            argocd_namespace=values["ARGOCD_NAMESPACE"],
            ecr_registry=values["ECR_REGISTRY"],
        )
    except (ClassifierInspectionError, ValueError, OSError) as exc:
        print(f"INSPECTION ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result))
    if result["reasons"]:
        print("Observability ownership-safety diagnostics:", file=sys.stderr)
        for reason in result["reasons"]:
            print(f"  - {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
