"""Offline tests for automation/orchestration/platform_acceptance.py (post-reconciliation acceptance: HEALTHY/BROKEN); run directly via `python3 automation/test-goldengate-platform-acceptance.py`. No live Kubernetes -- every kubectl response is a fake, injected fixture. Exercises the classifier's actual logic (never merely greps its source). Pre-reconciliation ownership safety (ABSENT/OWNED/BROKEN) is a separate module -- see automation/test-goldengate-platform-state.py."""
from __future__ import annotations

import importlib.util
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL_PATH = os.path.join(REPO_ROOT, "automation", "orchestration", "platform_acceptance.py")


def _load_tool():
    spec = importlib.util.spec_from_file_location("platform_acceptance", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


platform_acceptance = _load_tool()

ENVIRONMENT = "dev"
RUNTIME_NAMESPACE = "goldengate-dev"
ARGOCD_NAMESPACE = "argocd"
ECR_REGISTRY = "229410149234.dkr.ecr.eu-west-1.amazonaws.com"
RUNTIME_ROLE_ARN = "arn:aws:iam::668311715351:role/GoldenGateSecretsReadRole-dev"
PLATFORM_LOGGING_ROLE_ARN = "arn:aws:iam::668311715351:role/GoldenGatePlatformLoggingRole-dev"
FLUENT_BIT_IMAGE = f"{ECR_REGISTRY}/aws-cloud-factory-fluent-bit@sha256:{'a' * 64}"

RELEASE_NAME = f"goldengate-{ENVIRONMENT}-platform"
APP_NAME = RELEASE_NAME


class FakeCluster:
    def __init__(self):
        self.objects = {}
        self.lists = {}
        self.force_errors = {}

    def put(self, resource, name, namespace, obj):
        self.objects[(resource, name, namespace)] = obj

    def put_list(self, resource, namespace, items):
        self.lists[(resource, namespace)] = items

    def fail(self, resource, name, namespace, stderr):
        self.force_errors[(resource, name, namespace)] = stderr

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
            return 0, __import__("json").dumps(obj), ""

        items = self.lists.get((resource, namespace), [])
        return 0, __import__("json").dumps({"items": items}), ""


def _app_obj(healthy=True, release_name=RELEASE_NAME):
    return {
        "status": {"sync": {"status": "Synced" if healthy else "OutOfSync"}, "health": {"status": "Healthy" if healthy else "Degraded"}},
        "spec": {"source": {"repoURL": f"oci://{ECR_REGISTRY}/{platform_acceptance.HELM_REPO_PATH}", "helm": {"releaseName": release_name}}, "destination": {"namespace": RUNTIME_NAMESPACE}},
    }


def _namespace_obj(labeled=True, phase="Active"):
    labels = dict(platform_acceptance.MANAGED_NAMESPACE_LABELS) if labeled else {}
    return {"metadata": {"labels": labels}, "status": {"phase": phase}}


def _crb_obj(role_name=platform_acceptance.FLUENT_BIT_CLUSTERROLE_NAME, sa_name=platform_acceptance.FLUENT_BIT_SA_NAME, sa_namespace=RUNTIME_NAMESPACE):
    return {"roleRef": {"kind": "ClusterRole", "name": role_name}, "subjects": [{"kind": "ServiceAccount", "name": sa_name, "namespace": sa_namespace}]}


def _sa_obj(role_arn):
    return {"metadata": {"annotations": {"eks.amazonaws.com/role-arn": role_arn}}}


def _daemonset_obj(generation=3, desired=2, service_account=platform_acceptance.FLUENT_BIT_SA_NAME, image=FLUENT_BIT_IMAGE):
    return {
        "metadata": {"generation": generation},
        "spec": {"template": {"spec": {"serviceAccountName": service_account, "containers": [{"name": "fluent-bit", "image": image}]}}},
        "status": {"observedGeneration": generation, "desiredNumberScheduled": desired, "currentNumberScheduled": desired, "updatedNumberScheduled": desired, "numberReady": desired, "numberAvailable": desired, "numberUnavailable": 0},
    }


def _populate_healthy_cluster(cluster):
    cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj())
    cluster.put("namespace", RUNTIME_NAMESPACE, None, _namespace_obj())
    cluster.put("clusterrole", platform_acceptance.FLUENT_BIT_CLUSTERROLE_NAME, None, {"metadata": {}})
    cluster.put("clusterrolebinding", platform_acceptance.FLUENT_BIT_CLUSTERROLEBINDING_NAME, None, _crb_obj())
    cluster.put("serviceaccount", platform_acceptance.RUNTIME_SA_NAME, RUNTIME_NAMESPACE, _sa_obj(RUNTIME_ROLE_ARN))
    cluster.put("serviceaccount", platform_acceptance.FLUENT_BIT_SA_NAME, RUNTIME_NAMESPACE, _sa_obj(PLATFORM_LOGGING_ROLE_ARN))
    cluster.put("configmap", platform_acceptance.FLUENT_BIT_CONFIGMAP_NAME, RUNTIME_NAMESPACE, {"metadata": {}})
    cluster.put("daemonset", platform_acceptance.FLUENT_BIT_DAEMONSET_NAME, RUNTIME_NAMESPACE, _daemonset_obj())
    cluster.put_list("statefulset", RUNTIME_NAMESPACE, [])
    cluster.put_list("deployment", RUNTIME_NAMESPACE, [])
    return cluster


def _classify(cluster, fluent_bit_image=FLUENT_BIT_IMAGE):
    return platform_acceptance.classify(
        cluster, environment=ENVIRONMENT, runtime_namespace=RUNTIME_NAMESPACE, argocd_namespace=ARGOCD_NAMESPACE,
        ecr_registry=ECR_REGISTRY, runtime_role_arn=RUNTIME_ROLE_ARN, platform_logging_role_arn=PLATFORM_LOGGING_ROLE_ARN, fluent_bit_image=fluent_bit_image,
    )


class PlatformAcceptanceTests(unittest.TestCase):
    def test_complete_valid_platform_is_healthy(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_acceptance.STATE_HEALTHY)
        self.assertEqual(result["reasons"], [])

    def test_managed_by_helm_namespace_label_is_broken(self):
        # Strict here (unlike ownership): the exact live incident's namespace-label drift must be fully converged by the time acceptance runs, or it is a real acceptance failure.
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("namespace", RUNTIME_NAMESPACE, None, _namespace_obj(labeled=False))
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_acceptance.STATE_BROKEN)
        self.assertTrue(any("managedNamespaceMetadata" in r for r in result["reasons"]))

    def test_wrong_runtime_sa_role_arn_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("serviceaccount", platform_acceptance.RUNTIME_SA_NAME, RUNTIME_NAMESPACE, _sa_obj("arn:aws:iam::668311715351:role/SomeOtherRole"))
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_acceptance.STATE_BROKEN)

    def test_daemonset_not_ready_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        ds = _daemonset_obj()
        ds["status"]["numberReady"] = 1
        cluster.put("daemonset", platform_acceptance.FLUENT_BIT_DAEMONSET_NAME, RUNTIME_NAMESPACE, ds)
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_acceptance.STATE_BROKEN)
        self.assertTrue(any("not ready" in r for r in result["reasons"]))

    def test_wrong_fluent_bit_image_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        ds = _daemonset_obj(image=f"{ECR_REGISTRY}/aws-cloud-factory-fluent-bit@sha256:{'b' * 64}")
        cluster.put("daemonset", platform_acceptance.FLUENT_BIT_DAEMONSET_NAME, RUNTIME_NAMESPACE, ds)
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_acceptance.STATE_BROKEN)

    def test_application_not_synced_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(healthy=False))
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_acceptance.STATE_BROKEN)

    def test_missing_configmap_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.objects.pop(("configmap", platform_acceptance.FLUENT_BIT_CONFIGMAP_NAME, RUNTIME_NAMESPACE))
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_acceptance.STATE_BROKEN)

    def test_unexpected_owned_runtime_workload_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put_list("statefulset", RUNTIME_NAMESPACE, [{"metadata": {"name": "gg-oracle-payments-01"}}])
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_acceptance.STATE_BROKEN)

    def test_invalid_fluent_bit_image_is_configuration_error(self):
        with self.assertRaises(ValueError):
            _classify(FakeCluster(), fluent_bit_image="not-a-valid-image-reference")

    def test_forbidden_raises_inspection_error(self):
        cluster = FakeCluster()
        cluster.fail("application", APP_NAME, ARGOCD_NAMESPACE, "Error from server (Forbidden): applications.argoproj.io is forbidden")
        with self.assertRaises(platform_acceptance.ClassifierInspectionError):
            _classify(cluster)


class PlatformAcceptanceNoMutationSourceSweepTests(unittest.TestCase):
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
