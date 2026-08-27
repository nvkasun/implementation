"""Offline tests for automation/phases/phase4/observability_acceptance.py (post-reconciliation acceptance: HEALTHY/BROKEN); run directly via `python3 automation/phases/phase4/tests/test_observability_acceptance.py`. No live Kubernetes -- every kubectl response is a fake, injected fixture. Exercises the classifier's actual logic (never merely greps its source). Pre-reconciliation ownership safety (ABSENT/OWNED/BROKEN) is a separate module -- see automation/phases/phase4/tests/test_observability_state.py."""
from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
TOOL_PATH = REPO_ROOT / "automation" / "phases" / "phase4" / "observability_acceptance.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("observability_acceptance", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


observability_acceptance = _load_tool()

ENVIRONMENT = "dev"
OBSERVABILITY_NAMESPACE = "amazon-cloudwatch"
ARGOCD_NAMESPACE = "argocd"
ECR_REGISTRY = "229410149234.dkr.ecr.eu-west-1.amazonaws.com"
CLOUDWATCH_METRICS_ROLE_ARN = "arn:aws:iam::668311715351:role/GoldenGateCloudWatchMetricsRole-dev"

APP_NAME = observability_acceptance.ARGOCD_APP_NAME


class FakeCluster:
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


def _app_obj(healthy=True):
    return {
        "status": {"sync": {"status": "Synced" if healthy else "OutOfSync"}, "health": {"status": "Healthy" if healthy else "Degraded"}},
        "spec": {
            "source": {"repoURL": f"oci://{ECR_REGISTRY}/{observability_acceptance.HELM_REPO_PATH}", "targetRevision": observability_acceptance.CHART_VERSION, "helm": {"releaseName": observability_acceptance.RELEASE_NAME}},
            "destination": {"namespace": OBSERVABILITY_NAMESPACE},
        },
    }


def _sa_obj(role_arn):
    return {"metadata": {"annotations": {"eks.amazonaws.com/role-arn": role_arn}}}


def _image_ref(repo, digest="a" * 64):
    return f"{ECR_REGISTRY}/{repo}:latest@sha256:{digest}"


def _deployment_obj(name, generation=3, desired=1, image_repo="aws-cloud-factory-cloudwatch-agent-operator"):
    return {
        "metadata": {"generation": generation},
        "spec": {"replicas": desired, "template": {"spec": {"containers": [{"name": name, "image": _image_ref(image_repo)}]}}},
        "status": {"observedGeneration": generation, "updatedReplicas": desired, "readyReplicas": desired, "availableReplicas": desired},
    }


def _daemonset_obj(name, generation=3, desired=2, image_repo="aws-cloud-factory-cloudwatch-agent"):
    return {
        "metadata": {"generation": generation},
        "spec": {"template": {"spec": {"containers": [{"name": name, "image": _image_ref(image_repo)}]}}},
        "status": {"observedGeneration": generation, "desiredNumberScheduled": desired, "currentNumberScheduled": desired, "updatedNumberScheduled": desired, "numberReady": desired, "numberAvailable": desired, "numberUnavailable": 0},
    }


def _agent_cr_obj(mode, host_network):
    return {"spec": {"mode": mode, "hostNetwork": host_network}}


def _namespace_obj(labeled=True, phase="Active"):
    labels = dict(observability_acceptance.MANAGED_NAMESPACE_LABELS) if labeled else {}
    return {"metadata": {"labels": labels}, "status": {"phase": phase}}


def _populate_healthy_cluster(cluster):
    cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj())
    cluster.put("namespace", OBSERVABILITY_NAMESPACE, None, _namespace_obj())
    cluster.put("serviceaccount", observability_acceptance.CLOUDWATCH_AGENT_SA_NAME, OBSERVABILITY_NAMESPACE, _sa_obj(CLOUDWATCH_METRICS_ROLE_ARN))
    for name in observability_acceptance.REQUIRED_DEPLOYMENTS:
        repo = "aws-cloud-factory-cloudwatch-agent-operator" if "controller-manager" in name else ("aws-cloud-factory-cloudwatch-agent" if "scraper" in name else "aws-cloud-factory-kube-state-metrics")
        cluster.put("deployment", name, OBSERVABILITY_NAMESPACE, _deployment_obj(name, image_repo=repo))
    for name in observability_acceptance.REQUIRED_DAEMONSETS:
        repo = "aws-cloud-factory-cloudwatch-agent" if name == "cloudwatch-agent" else "aws-cloud-factory-node-exporter"
        cluster.put("daemonset", name, OBSERVABILITY_NAMESPACE, _daemonset_obj(name, image_repo=repo))
    for name, expected in observability_acceptance.AGENT_CR_EXPECTED.items():
        cluster.put(observability_acceptance.AGENT_CR_RESOURCE, name, OBSERVABILITY_NAMESPACE, _agent_cr_obj(expected["mode"], expected["hostNetwork"]))
    cluster.put_list("daemonset", OBSERVABILITY_NAMESPACE, [_daemonset_obj(n) for n in observability_acceptance.REQUIRED_DAEMONSETS])
    cluster.put_list("pods", OBSERVABILITY_NAMESPACE, [])
    for resource in observability_acceptance.FORBIDDEN_LIST_RESOURCES:
        cluster.put_list(resource, OBSERVABILITY_NAMESPACE, [])
    return cluster


def _classify(cluster):
    return observability_acceptance.classify(cluster, environment=ENVIRONMENT, observability_namespace=OBSERVABILITY_NAMESPACE, argocd_namespace=ARGOCD_NAMESPACE, ecr_registry=ECR_REGISTRY, cloudwatch_metrics_role_arn=CLOUDWATCH_METRICS_ROLE_ARN)


class ObservabilityAcceptanceTests(unittest.TestCase):
    def test_complete_expected_state_is_healthy(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_acceptance.STATE_HEALTHY)
        self.assertEqual(result["reasons"], [])

    def test_wrong_cloudwatch_metrics_role_is_broken(self):
        # Strict here (unlike ownership, which never checks it at all): post-reconciliation the SA role-arn must be exactly correct.
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("serviceaccount", observability_acceptance.CLOUDWATCH_AGENT_SA_NAME, OBSERVABILITY_NAMESPACE, _sa_obj("arn:aws:iam::668311715351:role/SomeOtherRole"))
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_acceptance.STATE_BROKEN)

    def test_stale_namespace_managed_by_is_broken(self):
        # Fix 2 (Generic MAIN Desired-State Convergence Safety Correction): strict here, unlike ownership -- post-reconciliation the namespace must exactly satisfy the Application's own managedNamespaceMetadata contract.
        cluster = _populate_healthy_cluster(FakeCluster())
        ns = _namespace_obj(labeled=True)
        ns["metadata"]["labels"]["app.kubernetes.io/managed-by"] = "Helm"
        cluster.put("namespace", OBSERVABILITY_NAMESPACE, None, ns)
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_acceptance.STATE_BROKEN)
        self.assertTrue(any("managed-by" in r and "managedNamespaceMetadata" in r for r in result["reasons"]))

    def test_wrong_namespace_name_label_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        ns = _namespace_obj(labeled=True)
        ns["metadata"]["labels"]["app.kubernetes.io/name"] = "some-other-app"
        cluster.put("namespace", OBSERVABILITY_NAMESPACE, None, ns)
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_acceptance.STATE_BROKEN)
        self.assertTrue(any("app.kubernetes.io/name" in r and "managedNamespaceMetadata" in r for r in result["reasons"]))

    def test_terminating_namespace_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("namespace", OBSERVABILITY_NAMESPACE, None, _namespace_obj(labeled=True, phase="Terminating"))
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_acceptance.STATE_BROKEN)
        self.assertTrue(any("Terminating" in r for r in result["reasons"]))

    def test_missing_serviceaccount_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.objects.pop(("serviceaccount", observability_acceptance.CLOUDWATCH_AGENT_SA_NAME, OBSERVABILITY_NAMESPACE))
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_acceptance.STATE_BROKEN)

    def test_deployment_not_ready_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        obj = _deployment_obj("amazon-cloudwatch-observability-controller-manager")
        obj["status"]["readyReplicas"] = 0
        cluster.put("deployment", "amazon-cloudwatch-observability-controller-manager", OBSERVABILITY_NAMESPACE, obj)
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_acceptance.STATE_BROKEN)

    def test_wrong_agent_cr_hostnetwork_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put(observability_acceptance.AGENT_CR_RESOURCE, "cloudwatch-agent", OBSERVABILITY_NAMESPACE, _agent_cr_obj("deployment", False))
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_acceptance.STATE_BROKEN)

    def test_public_image_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        obj = _deployment_obj("kube-state-metrics")
        obj["spec"]["template"]["spec"]["containers"][0]["image"] = "public.ecr.aws/some/image:latest@sha256:" + "a" * 64
        cluster.put("deployment", "kube-state-metrics", OBSERVABILITY_NAMESPACE, obj)
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_acceptance.STATE_BROKEN)

    def test_application_not_synced_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(healthy=False))
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_acceptance.STATE_BROKEN)

    def test_forbidden_fluent_bit_daemonset_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put_list("daemonset", OBSERVABILITY_NAMESPACE, [_daemonset_obj(n) for n in observability_acceptance.REQUIRED_DAEMONSETS] + [{"metadata": {"name": "fluent-bit"}}])
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_acceptance.STATE_BROKEN)
        self.assertTrue(any("forbidden Fluent Bit-like DaemonSet" in r for r in result["reasons"]))

    def test_forbidden_crd_never_installed_does_not_break_healthy(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.mark_unknown_kind("dcgmexporters.cloudwatch.aws.amazon.com", OBSERVABILITY_NAMESPACE)
        cluster.mark_unknown_kind("neuronmonitors.cloudwatch.aws.amazon.com", OBSERVABILITY_NAMESPACE)
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_acceptance.STATE_HEALTHY)

    def test_forbidden_raises_inspection_error(self):
        cluster = FakeCluster()
        cluster.fail("application", APP_NAME, ARGOCD_NAMESPACE, "Error from server (Forbidden): applications.argoproj.io is forbidden")
        with self.assertRaises(observability_acceptance.ClassifierInspectionError):
            _classify(cluster)


class ObservabilityAcceptanceNoMutationSourceSweepTests(unittest.TestCase):
    FORBIDDEN_SUBSTRINGS = (
        "kubectl apply", "kubectl create", "kubectl delete", "kubectl patch",
        "kubectl annotate", "kubectl label",
        "helm install", "helm upgrade", "helm uninstall",
    )

    def test_source_contains_no_mutating_command(self):
        with open(TOOL_PATH) as f:
            source = f.read()
        hits = [s for s in self.FORBIDDEN_SUBSTRINGS if s in source]
        self.assertEqual(hits, [], f"classifier source contains a mutating-looking construct: {hits}")


if __name__ == "__main__":
    unittest.main()
