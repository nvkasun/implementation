"""Offline tests for hack/orchestration/observability_state.py; run directly via `python3 hack/test-goldengate-observability-state.py`. No live Kubernetes -- every kubectl response is a fake, injected fixture. Exercises the classifier's actual logic (never merely greps its source)."""
from __future__ import annotations

import importlib.util
import json
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL_PATH = os.path.join(REPO_ROOT, "hack", "orchestration", "observability_state.py")


def _load_tool():
    spec = importlib.util.spec_from_file_location("observability_state", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


observability_state = _load_tool()

ENVIRONMENT = "dev"
OBSERVABILITY_NAMESPACE = "amazon-cloudwatch"
ARGOCD_NAMESPACE = "argocd"
ECR_REGISTRY = "229410149234.dkr.ecr.eu-west-1.amazonaws.com"
CLOUDWATCH_METRICS_ROLE_ARN = "arn:aws:iam::668311715351:role/GoldenGateCloudWatchMetricsRole-dev"

APP_NAME = observability_state.ARGOCD_APP_NAME


class FakeCluster:
    """Models exactly the subset of `kubectl get <resource> [name] [-n ns] -o json` behavior the classifier depends on -- never a real kubectl process. A single-name get defaults to NotFound when unset; a list (no name) get defaults to an empty items array when unset, matching real kubectl semantics. A resource kind registered in `unknown_kinds` simulates a CRD that was never installed on the cluster ("doesn't have a resource type")."""

    def __init__(self):
        self.objects = {}
        self.lists = {}
        self.force_errors = {}
        self.unknown_kinds = set()

    def put(self, resource, name, namespace, obj):
        self.objects[(resource, name, namespace)] = obj

    def put_list(self, resource, namespace, items):
        self.lists[(resource, namespace)] = items

    def fail(self, resource, name, namespace, stderr):
        self.force_errors[(resource, name, namespace)] = stderr

    def mark_unknown_kind(self, resource, namespace):
        self.unknown_kinds.add((resource, namespace))

    def __call__(self, args):
        assert args[0] == "get", f"classifier issued a non-read-only kubectl verb: {args}"
        resource = args[1]
        idx = 2
        name = None
        namespace = None
        if idx < len(args) and not args[idx].startswith("-"):
            name = args[idx]
            idx += 1
        while idx < len(args):
            if args[idx] == "-n":
                namespace = args[idx + 1]
                idx += 2
            else:
                idx += 1

        key = (resource, name, namespace)
        if key in self.force_errors:
            return 1, "", self.force_errors[key]

        if name is not None:
            obj = self.objects.get(key)
            if obj is None:
                return 1, "", f'Error from server (NotFound): {resource} "{name}" not found'
            return 0, json.dumps(obj), ""

        if (resource, namespace) in self.unknown_kinds:
            return 1, "", f'error: the server doesn\'t have a resource type "{resource}"'
        items = self.lists.get((resource, namespace), [])
        return 0, json.dumps({"items": items}), ""


def _app_obj(name, healthy=True, repo_url=None, target_revision=None, dest_ns=OBSERVABILITY_NAMESPACE, release_name=observability_state.RELEASE_NAME):
    return {
        "metadata": {"name": name},
        "status": {
            "sync": {"status": "Synced" if healthy else "OutOfSync"},
            "health": {"status": "Healthy" if healthy else "Degraded"},
        },
        "spec": {
            "source": {
                "repoURL": repo_url if repo_url is not None else f"oci://{ECR_REGISTRY}/{observability_state.HELM_REPO_PATH}",
                "targetRevision": target_revision if target_revision is not None else observability_state.CHART_VERSION,
                "helm": {"releaseName": release_name},
            },
            "destination": {"namespace": dest_ns},
        },
    }


def _sa_obj(name, role_arn):
    return {"metadata": {"name": name, "annotations": {"eks.amazonaws.com/role-arn": role_arn}}}


def _image_ref(repo, digest="a" * 64):
    return f"{ECR_REGISTRY}/{repo}:latest@sha256:{digest}"


def _deployment_obj(name, generation=3, desired=1, image_repo="aws-cloud-factory-cloudwatch-agent-operator"):
    return {
        "metadata": {"name": name, "generation": generation},
        "spec": {
            "replicas": desired,
            "template": {"spec": {"containers": [{"name": name, "image": _image_ref(image_repo)}]}},
        },
        "status": {
            "observedGeneration": generation,
            "updatedReplicas": desired,
            "readyReplicas": desired,
            "availableReplicas": desired,
        },
    }


def _daemonset_obj(name, generation=3, desired=2, image_repo="aws-cloud-factory-cloudwatch-agent"):
    return {
        "metadata": {"name": name, "generation": generation},
        "spec": {
            "template": {"spec": {"containers": [{"name": name, "image": _image_ref(image_repo)}]}},
        },
        "status": {
            "observedGeneration": generation,
            "desiredNumberScheduled": desired,
            "currentNumberScheduled": desired,
            "updatedNumberScheduled": desired,
            "numberReady": desired,
            "numberAvailable": desired,
            "numberUnavailable": 0,
        },
    }


def _agent_cr_obj(name, mode, host_network):
    return {"metadata": {"name": name}, "spec": {"mode": mode, "hostNetwork": host_network}}


def _populate_healthy_cluster(cluster):
    cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(APP_NAME))
    cluster.put("namespace", OBSERVABILITY_NAMESPACE, None, {"metadata": {"name": OBSERVABILITY_NAMESPACE}})
    cluster.put("serviceaccount", observability_state.CLOUDWATCH_AGENT_SA_NAME, OBSERVABILITY_NAMESPACE, _sa_obj(observability_state.CLOUDWATCH_AGENT_SA_NAME, CLOUDWATCH_METRICS_ROLE_ARN))

    for name in observability_state.REQUIRED_DEPLOYMENTS:
        repo = "aws-cloud-factory-cloudwatch-agent-operator" if "controller-manager" in name else ("aws-cloud-factory-cloudwatch-agent" if "scraper" in name else "aws-cloud-factory-kube-state-metrics")
        cluster.put("deployment", name, OBSERVABILITY_NAMESPACE, _deployment_obj(name, image_repo=repo))
    for name in observability_state.REQUIRED_DAEMONSETS:
        repo = "aws-cloud-factory-cloudwatch-agent" if name == "cloudwatch-agent" else "aws-cloud-factory-node-exporter"
        cluster.put("daemonset", name, OBSERVABILITY_NAMESPACE, _daemonset_obj(name, image_repo=repo))
    for name, expected in observability_state.AGENT_CR_EXPECTED.items():
        cluster.put(observability_state.AGENT_CR_RESOURCE, name, OBSERVABILITY_NAMESPACE, _agent_cr_obj(name, expected["mode"], expected["hostNetwork"]))

    cluster.put_list("daemonset", OBSERVABILITY_NAMESPACE, [_daemonset_obj(n) for n in observability_state.REQUIRED_DAEMONSETS])
    cluster.put_list("pods", OBSERVABILITY_NAMESPACE, [])
    for resource in observability_state.FORBIDDEN_LIST_RESOURCES:
        cluster.put_list(resource, OBSERVABILITY_NAMESPACE, [])
    return cluster


def _classify(cluster):
    return observability_state.classify(
        cluster,
        environment=ENVIRONMENT,
        observability_namespace=OBSERVABILITY_NAMESPACE,
        argocd_namespace=ARGOCD_NAMESPACE,
        ecr_registry=ECR_REGISTRY,
        cloudwatch_metrics_role_arn=CLOUDWATCH_METRICS_ROLE_ARN,
    )


class ObservabilityStateClassifierTests(unittest.TestCase):
    def test_1_no_application_no_namespace_is_absent(self):
        cluster = FakeCluster()
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_ABSENT)
        self.assertEqual(result["reasons"], [])

    def test_2_namespace_exists_without_application_is_broken(self):
        cluster = FakeCluster()
        cluster.put("namespace", OBSERVABILITY_NAMESPACE, None, {"metadata": {"name": OBSERVABILITY_NAMESPACE}})
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any("Application" in r and "does not exist" in r for r in result["reasons"]))

    def test_3_application_exists_without_namespace_is_broken(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(APP_NAME))
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any("namespace" in r and "does not exist" in r for r in result["reasons"]))

    def test_4_application_not_synced_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(APP_NAME, healthy=False))
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any("sync status" in r for r in result["reasons"]))

    def test_5_application_not_healthy_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        app = _app_obj(APP_NAME)
        app["status"]["health"]["status"] = "Progressing"
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, app)
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any("health status" in r for r in result["reasons"]))

    def test_6_wrong_repo_url_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(APP_NAME, repo_url="oci://wrong.example.com/helm/amazon-cloudwatch-observability"))
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any("source.repoURL" in r for r in result["reasons"]))

    def test_7_wrong_destination_namespace_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(APP_NAME, dest_ns="some-other-namespace"))
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any("destination.namespace" in r for r in result["reasons"]))

    def test_8_cloudwatch_agent_sa_missing_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.objects.pop(("serviceaccount", observability_state.CLOUDWATCH_AGENT_SA_NAME, OBSERVABILITY_NAMESPACE))
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any(f"serviceaccount/{observability_state.CLOUDWATCH_AGENT_SA_NAME} does not exist" in r for r in result["reasons"]))

    def test_9_wrong_cloudwatch_metrics_role_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("serviceaccount", observability_state.CLOUDWATCH_AGENT_SA_NAME, OBSERVABILITY_NAMESPACE, _sa_obj(observability_state.CLOUDWATCH_AGENT_SA_NAME, "arn:aws:iam::668311715351:role/SomeOtherRole"))
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any("eks.amazonaws.com/role-arn" in r for r in result["reasons"]))

    def test_10_controller_deployment_not_ready_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        obj = _deployment_obj("amazon-cloudwatch-observability-controller-manager")
        obj["status"]["readyReplicas"] = 0
        cluster.put("deployment", "amazon-cloudwatch-observability-controller-manager", OBSERVABILITY_NAMESPACE, obj)
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any("amazon-cloudwatch-observability-controller-manager not ready" in r for r in result["reasons"]))

    def test_11_cloudwatch_agent_daemonset_not_ready_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        obj = _daemonset_obj("cloudwatch-agent")
        obj["status"]["numberReady"] = 0
        cluster.put("daemonset", "cloudwatch-agent", OBSERVABILITY_NAMESPACE, obj)
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any("daemonset/cloudwatch-agent not ready" in r for r in result["reasons"]))

    def test_12_cluster_scraper_not_ready_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        obj = _deployment_obj("cloudwatch-agent-cluster-scraper")
        obj["status"]["availableReplicas"] = 0
        cluster.put("deployment", "cloudwatch-agent-cluster-scraper", OBSERVABILITY_NAMESPACE, obj)
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any("cloudwatch-agent-cluster-scraper not ready" in r for r in result["reasons"]))

    def test_13_kube_state_metrics_not_ready_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        obj = _deployment_obj("kube-state-metrics")
        obj["status"]["updatedReplicas"] = 0
        cluster.put("deployment", "kube-state-metrics", OBSERVABILITY_NAMESPACE, obj)
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any("kube-state-metrics not ready" in r for r in result["reasons"]))

    def test_14_node_exporter_not_ready_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        obj = _daemonset_obj("node-exporter")
        obj["status"]["numberAvailable"] = 0
        cluster.put("daemonset", "node-exporter", OBSERVABILITY_NAMESPACE, obj)
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any("daemonset/node-exporter not ready" in r for r in result["reasons"]))

    def test_15_required_agent_cr_missing_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.objects.pop((observability_state.AGENT_CR_RESOURCE, "cloudwatch-agent", OBSERVABILITY_NAMESPACE))
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any("AmazonCloudWatchAgent/cloudwatch-agent does not exist" in r for r in result["reasons"]))

    def test_16_wrong_cr_mode_hostnetwork_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put(observability_state.AGENT_CR_RESOURCE, "cloudwatch-agent", OBSERVABILITY_NAMESPACE, _agent_cr_obj("cloudwatch-agent", "deployment", False))
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any("spec.mode" in r for r in result["reasons"]))
        self.assertTrue(any("spec.hostNetwork" in r for r in result["reasons"]))

    def test_17_public_image_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        obj = _deployment_obj("kube-state-metrics")
        obj["spec"]["template"]["spec"]["containers"][0]["image"] = "public.ecr.aws/some/image:latest@sha256:" + "a" * 64
        cluster.put("deployment", "kube-state-metrics", OBSERVABILITY_NAMESPACE, obj)
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any("not on the private registry" in r for r in result["reasons"]))

    def test_18_non_digest_pinned_image_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        obj = _deployment_obj("kube-state-metrics")
        obj["spec"]["template"]["spec"]["containers"][0]["image"] = f"{ECR_REGISTRY}/aws-cloud-factory-kube-state-metrics:v2.18.0"
        cluster.put("deployment", "kube-state-metrics", OBSERVABILITY_NAMESPACE, obj)
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any("not digest-pinned" in r for r in result["reasons"]))

    def test_18b_unapproved_repository_image_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        obj = _deployment_obj("kube-state-metrics", image_repo="some-unapproved-repo")
        cluster.put("deployment", "kube-state-metrics", OBSERVABILITY_NAMESPACE, obj)
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any("unapproved repository" in r for r in result["reasons"]))

    def test_19_forbidden_fluent_bit_daemonset_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put_list("daemonset", OBSERVABILITY_NAMESPACE, [_daemonset_obj(n) for n in observability_state.REQUIRED_DAEMONSETS] + [{"metadata": {"name": "fluent-bit"}}])
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any("forbidden Fluent Bit-like DaemonSet" in r for r in result["reasons"]))

    def test_19b_forbidden_target_allocator_pod_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put_list("pods", OBSERVABILITY_NAMESPACE, [{"metadata": {"name": "otel-target-allocator-abc123"}}])
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any("forbidden target-allocator pod" in r for r in result["reasons"]))

    def test_19c_forbidden_instrumentation_resource_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put_list("instrumentations.cloudwatch.aws.amazon.com", OBSERVABILITY_NAMESPACE, [{"metadata": {"name": "default"}}])
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any("instrumentations.cloudwatch.aws.amazon.com" in r for r in result["reasons"]))

    def test_19d_forbidden_dcgm_exporter_resource_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put_list("dcgmexporters.cloudwatch.aws.amazon.com", OBSERVABILITY_NAMESPACE, [{"metadata": {"name": "default"}}])
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any("dcgmexporters.cloudwatch.aws.amazon.com" in r for r in result["reasons"]))

    def test_19e_forbidden_neuron_monitor_resource_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put_list("neuronmonitors.cloudwatch.aws.amazon.com", OBSERVABILITY_NAMESPACE, [{"metadata": {"name": "default"}}])
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any("neuronmonitors.cloudwatch.aws.amazon.com" in r for r in result["reasons"]))

    def test_19f_forbidden_crd_kind_never_installed_does_not_break_healthy(self):
        # These optional CRD kinds may genuinely never be registered on the cluster at all (dcgmExporter.enabled=false / neuronMonitor.enabled=false permanently) -- that must be treated as zero results, not an inspection error, and must not itself cause BROKEN.
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.mark_unknown_kind("dcgmexporters.cloudwatch.aws.amazon.com", OBSERVABILITY_NAMESPACE)
        cluster.mark_unknown_kind("neuronmonitors.cloudwatch.aws.amazon.com", OBSERVABILITY_NAMESPACE)
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_HEALTHY)

    def test_20_complete_expected_state_is_healthy(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_HEALTHY)
        self.assertEqual(result["reasons"], [])

    def test_21a_api_forbidden_raises_inspection_error_not_absent(self):
        cluster = FakeCluster()
        cluster.fail("application", APP_NAME, ARGOCD_NAMESPACE, "Error from server (Forbidden): applications.argoproj.io is forbidden")
        with self.assertRaises(observability_state.ClassifierInspectionError):
            _classify(cluster)

    def test_21b_malformed_json_raises_inspection_error(self):
        cluster = FakeCluster()

        def bad_run(args):
            if args[:2] == ["get", "application"]:
                return 0, "not valid json{{{", ""
            return cluster(args)

        with self.assertRaises(observability_state.ClassifierInspectionError):
            observability_state.classify(
                bad_run,
                environment=ENVIRONMENT,
                observability_namespace=OBSERVABILITY_NAMESPACE,
                argocd_namespace=ARGOCD_NAMESPACE,
                ecr_registry=ECR_REGISTRY,
                cloudwatch_metrics_role_arn=CLOUDWATCH_METRICS_ROLE_ARN,
            )

    def test_wrong_release_name_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(APP_NAME, release_name="some-other-release"))
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any("source.helm.releaseName" in r for r in result["reasons"]))

    def test_wrong_target_revision_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(APP_NAME, target_revision="5.0.0"))
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any("source.targetRevision" in r for r in result["reasons"]))


class ObservabilityStateNoMutationSourceSweepTests(unittest.TestCase):
    """Static source-safety proof: the classifier module (and its shared k8s_common helper) must never construct a mutating kubectl/helm command."""

    FORBIDDEN_SUBSTRINGS = (
        "kubectl apply", "kubectl create", "kubectl delete", "kubectl patch",
        "kubectl annotate", "kubectl label",
        "helm install", "helm upgrade", "helm uninstall",
    )

    def test_source_contains_no_mutating_command(self):
        k8s_common_path = os.path.join(REPO_ROOT, "hack", "orchestration", "k8s_common.py")
        for path in (TOOL_PATH, k8s_common_path):
            with open(path) as f:
                source = f.read()
            hits = [s for s in self.FORBIDDEN_SUBSTRINGS if s in source]
            self.assertEqual(hits, [], f"{path} contains a mutating-looking construct: {hits}")

    def test_every_get_json_call_uses_get_verb_only(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        _classify(cluster)


if __name__ == "__main__":
    unittest.main()
