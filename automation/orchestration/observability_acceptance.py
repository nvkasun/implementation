#!/usr/bin/env python3
"""automation/orchestration/observability_acceptance.py: read-only Observability (amazon-cloudwatch-observability) post-reconciliation acceptance classifier -- answers exactly one question, "does the live Observability installation exactly match the current committed desired state right now?", as one of HEALTHY/BROKEN. Unlike automation/orchestration/observability_state.py (a pre-reconciliation ownership-safety preflight that only checks Application identity), this tool DOES require full readiness and exact desired-state correctness: a missing/unready workload, an unapproved/non-digest-pinned image, a wrong Agent CR mode/hostNetwork, a wrong cloudwatch-agent ServiceAccount role-arn, a stale chart version, or any forbidden component (Fluent Bit-like DaemonSet, target-allocator pod, Application Signals/DCGM/Neuron CRD) are all BROKEN here. Never mutates the cluster: every kubectl invocation here is a `get` (read-only); no apply/create/delete/patch/annotate/label/helm call exists in this module. Consumes environment identity through automation/goldengate-environment.py, never a second environment parser. The desired contract (resource names, Application identity, Agent CR/workload shape, allowed image repositories, forbidden components) is read directly from .github/workflows/40-sub-observability.yaml's own live-validation section, never guessed -- the upstream amazon-cloudwatch-observability chart itself is not vendored as source in this repo."""
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
daemonset_ready = _k8s_common.daemonset_ready
deployment_ready = _k8s_common.deployment_ready
get_json = _k8s_common.get_json
list_json = _k8s_common.list_json
pod_template_images = _k8s_common.pod_template_images

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


STATE_HEALTHY = "HEALTHY"
STATE_BROKEN = "BROKEN"

# Current chart/workflow contract (.github/workflows/40-sub-observability.yaml) -- verified against the real workflow's own Application-manifest generation and live-validation section, never guessed.
HELM_REPO_PATH = "helm/amazon-cloudwatch-observability"
RELEASE_NAME = "amazon-cloudwatch-observability"
ARGOCD_APP_NAME = "goldengate-observability"

# Must stay equal to 40-sub-observability.yaml's own `CHART_VERSION: "6.2.0"` env literal -- protected by a dedicated cross-file regression in automation/test-goldengate-deployment-models.sh, never an unguarded duplicate constant.
CHART_VERSION = "6.2.0"

CLOUDWATCH_AGENT_SA_NAME = "cloudwatch-agent"

REQUIRED_DEPLOYMENTS = (
    "amazon-cloudwatch-observability-controller-manager",
    "cloudwatch-agent-cluster-scraper",
    "kube-state-metrics",
)
REQUIRED_DAEMONSETS = ("cloudwatch-agent", "node-exporter")

# Full CRD plural, matching the workflow's own `kubectl get amazoncloudwatchagents.cloudwatch.aws.amazon.com` -- never the short/ambiguous kind alias.
AGENT_CR_RESOURCE = "amazoncloudwatchagents.cloudwatch.aws.amazon.com"
AGENT_CR_EXPECTED = {
    "cloudwatch-agent": {"mode": "daemonset", "hostNetwork": True},
    "cloudwatch-agent-cluster-scraper": {"mode": "deployment", "hostNetwork": False},
}

ALLOWED_IMAGE_REPOS = (
    "aws-cloud-factory-cloudwatch-agent-operator",
    "aws-cloud-factory-cloudwatch-agent",
    "aws-cloud-factory-kube-state-metrics",
    "aws-cloud-factory-node-exporter",
)

# Namespace-scoped CR kinds this metrics-only deployment must never carry (40-sub-observability.yaml checks 10/11: Application Signals Instrumentation, DCGM/GPU, Neuron).
FORBIDDEN_LIST_RESOURCES = (
    "instrumentations.cloudwatch.aws.amazon.com",
    "dcgmexporters.cloudwatch.aws.amazon.com",
    "neuronmonitors.cloudwatch.aws.amazon.com",
)

# .github/workflows/40-sub-observability.yaml's own Application-manifest generation ("Create or update the Argo CD Application" step) -- syncPolicy.managedNamespaceMetadata.labels, verified against the real workflow source, never guessed. Mirrors platform_acceptance.py's MANAGED_NAMESPACE_LABELS pattern: strict here (unlike observability_state.py's ownership check, which never inspects namespace labels at all), since a stale app.kubernetes.io/name/managed-by value post-reconciliation is a genuine acceptance failure, not tolerated as "safe drift" forever.
MANAGED_NAMESPACE_LABELS = {
    "app.kubernetes.io/name": ARGOCD_APP_NAME,
    "app.kubernetes.io/managed-by": "argocd",
}


def _image_reasons(resource_label, images, ecr_registry):
    reasons = []
    for image in images:
        if not image.startswith(f"{ecr_registry}/"):
            reasons.append(f"{resource_label} image {image!r} is not on the private registry {ecr_registry!r}")
            continue
        if "@sha256:" not in image:
            reasons.append(f"{resource_label} image {image!r} is not digest-pinned (no @sha256:)")
            continue
        repo = image[len(ecr_registry) + 1:].split("@")[0].split(":")[0]
        if repo not in ALLOWED_IMAGE_REPOS:
            reasons.append(f"{resource_label} image {image!r} belongs to an unapproved repository {repo!r}")
    return reasons


def classify(run, environment, observability_namespace, argocd_namespace, ecr_registry, cloudwatch_metrics_role_arn):
    """Returns the stable {"state", "environment", "namespace", "reasons", "checks"} shape. Raises ClassifierInspectionError if Kubernetes access itself could not be trusted -- callers must let that propagate as a hard failure, never a downgrade to HEALTHY/BROKEN."""
    reasons = []
    checks = {}

    expected_repo_url = f"oci://{ecr_registry}/{HELM_REPO_PATH}"

    app_found, app_obj = get_json(run, "application", ARGOCD_APP_NAME, argocd_namespace)
    checks["application_found"] = app_found
    if not app_found:
        reasons.append(f"Application {ARGOCD_APP_NAME} does not exist in {argocd_namespace}")
    else:
        status = app_obj.get("status") or {}
        sync_status = ((status.get("sync") or {}).get("status"))
        health_status = ((status.get("health") or {}).get("status"))
        if sync_status != "Synced":
            reasons.append(f"Application {ARGOCD_APP_NAME} sync status is {sync_status!r}, expected 'Synced'")
        if health_status != "Healthy":
            reasons.append(f"Application {ARGOCD_APP_NAME} health status is {health_status!r}, expected 'Healthy'")

        spec = app_obj.get("spec") or {}
        source = spec.get("source") or {}
        destination = spec.get("destination") or {}
        helm_source = source.get("helm") or {}

        actual_repo_url = source.get("repoURL")
        if actual_repo_url != expected_repo_url:
            reasons.append(f"Application {ARGOCD_APP_NAME} source.repoURL={actual_repo_url!r}, expected {expected_repo_url!r}")

        actual_target_revision = source.get("targetRevision")
        if actual_target_revision != CHART_VERSION:
            reasons.append(f"Application {ARGOCD_APP_NAME} source.targetRevision={actual_target_revision!r}, expected {CHART_VERSION!r}")

        actual_dest_ns = destination.get("namespace")
        if actual_dest_ns != observability_namespace:
            reasons.append(f"Application {ARGOCD_APP_NAME} destination.namespace={actual_dest_ns!r}, expected {observability_namespace!r}")

        actual_release_name = helm_source.get("releaseName")
        if actual_release_name != RELEASE_NAME:
            reasons.append(f"Application {ARGOCD_APP_NAME} source.helm.releaseName={actual_release_name!r}, expected {RELEASE_NAME!r}")

    ns_found, ns_obj = get_json(run, "namespace", observability_namespace)
    checks["namespace_found"] = ns_found
    if not ns_found:
        reasons.append(f"namespace {observability_namespace} does not exist")
    else:
        ns_phase = ((ns_obj.get("status") or {}).get("phase"))
        if ns_phase == "Terminating":
            reasons.append(f"namespace {observability_namespace} is Terminating")

        # Strict here (unlike observability_state.py's ownership check, which deliberately never inspects namespace labels): the namespace metadata must exactly match Argo CD's own managedNamespaceMetadata contract post-reconciliation.
        ns_labels = ((ns_obj.get("metadata") or {}).get("labels")) or {}
        for label_key, expected_value in MANAGED_NAMESPACE_LABELS.items():
            actual_value = ns_labels.get(label_key)
            if actual_value != expected_value:
                reasons.append(f"namespace {observability_namespace} label {label_key}={actual_value!r}, expected {expected_value!r} (managedNamespaceMetadata)")

    # The remaining checks only make sense once the target namespace exists.
    if ns_found:
        sa_found, sa_obj = get_json(run, "serviceaccount", CLOUDWATCH_AGENT_SA_NAME, observability_namespace)
        checks["cloudwatch_agent_serviceaccount_found"] = sa_found
        if not sa_found:
            reasons.append(f"serviceaccount/{CLOUDWATCH_AGENT_SA_NAME} does not exist")
        else:
            role_arn = ((sa_obj.get("metadata") or {}).get("annotations") or {}).get("eks.amazonaws.com/role-arn")
            if role_arn != cloudwatch_metrics_role_arn:
                reasons.append(f"serviceaccount/{CLOUDWATCH_AGENT_SA_NAME} eks.amazonaws.com/role-arn={role_arn!r}, expected {cloudwatch_metrics_role_arn!r}")

        deploy_status = {}
        for name in REQUIRED_DEPLOYMENTS:
            found, obj = get_json(run, "deployment", name, observability_namespace)
            if not found:
                reasons.append(f"deployment/{name} does not exist")
                deploy_status[name] = False
                continue
            ready, why = deployment_ready(obj)
            deploy_status[name] = ready
            if not ready:
                reasons.append(f"deployment/{name} not ready: {why}")
            else:
                reasons.extend(_image_reasons(f"deployment/{name}", pod_template_images(obj), ecr_registry))
        checks["deployments"] = deploy_status

        ds_status = {}
        for name in REQUIRED_DAEMONSETS:
            found, obj = get_json(run, "daemonset", name, observability_namespace)
            if not found:
                reasons.append(f"daemonset/{name} does not exist")
                ds_status[name] = False
                continue
            ready, why = daemonset_ready(obj)
            ds_status[name] = ready
            if not ready:
                reasons.append(f"daemonset/{name} not ready: {why}")
            else:
                reasons.extend(_image_reasons(f"daemonset/{name}", pod_template_images(obj), ecr_registry))
        checks["daemonsets"] = ds_status

        cr_status = {}
        for name, expected in AGENT_CR_EXPECTED.items():
            found, obj = get_json(run, AGENT_CR_RESOURCE, name, observability_namespace)
            cr_status[name] = found
            if not found:
                reasons.append(f"AmazonCloudWatchAgent/{name} does not exist")
                continue
            cr_spec = obj.get("spec") or {}
            actual_mode = cr_spec.get("mode")
            actual_host_network = cr_spec.get("hostNetwork")
            if actual_mode != expected["mode"]:
                reasons.append(f"AmazonCloudWatchAgent/{name} spec.mode={actual_mode!r}, expected {expected['mode']!r}")
            if actual_host_network is not expected["hostNetwork"]:
                reasons.append(f"AmazonCloudWatchAgent/{name} spec.hostNetwork={actual_host_network!r}, expected literal {expected['hostNetwork']!r}")
        checks["agent_crs"] = cr_status

        # Negative safety contract: this deployment is metrics-only. GoldenGate/container logs remain owned by platform Fluent Bit; GoldenGate business metrics remain owned by shared gg-monitor.
        daemonsets_in_ns = list_json(run, "daemonset", namespace=observability_namespace)
        fluent_like = [d.get("metadata", {}).get("name") for d in daemonsets_in_ns if "fluent" in (d.get("metadata", {}).get("name") or "").lower()]
        checks["fluent_bit_like_daemonsets"] = fluent_like
        if fluent_like:
            reasons.append(f"forbidden Fluent Bit-like DaemonSet(s) found in {observability_namespace}: {fluent_like!r}")

        pods_in_ns = list_json(run, "pods", namespace=observability_namespace)
        target_allocator_pods = [p.get("metadata", {}).get("name") for p in pods_in_ns if "target-allocator" in (p.get("metadata", {}).get("name") or "").lower()]
        checks["target_allocator_pods"] = target_allocator_pods
        if target_allocator_pods:
            reasons.append(f"forbidden target-allocator pod(s) found in {observability_namespace}: {target_allocator_pods!r}")

        for resource in FORBIDDEN_LIST_RESOURCES:
            items = list_json(run, resource, namespace=observability_namespace)
            checks[f"forbidden_{resource}_count"] = len(items)
            if items:
                names = [i.get("metadata", {}).get("name") for i in items]
                reasons.append(f"forbidden {resource} resource(s) found in {observability_namespace}: {names!r}")

    state = STATE_HEALTHY if not reasons else STATE_BROKEN
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
            cloudwatch_metrics_role_arn=values["CLOUDWATCH_METRICS_ROLE_ARN"],
        )
    except (ClassifierInspectionError, ValueError, OSError) as exc:
        print(f"INSPECTION ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result))
    if result["reasons"]:
        print("Observability acceptance diagnostics:", file=sys.stderr)
        for reason in result["reasons"]:
            print(f"  - {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
