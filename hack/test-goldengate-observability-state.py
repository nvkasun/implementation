"""Offline tests for hack/orchestration/observability_state.py (ownership-safety preflight: ABSENT/OWNED/BROKEN); run directly via `python3 hack/test-goldengate-observability-state.py`. No live Kubernetes -- every kubectl response is a fake, injected fixture. Exercises the classifier's actual logic (never merely greps its source). Post-reconciliation acceptance (HEALTHY/BROKEN) is a separate module -- see hack/test-goldengate-observability-acceptance.py."""
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

APP_NAME = observability_state.ARGOCD_APP_NAME


class FakeCluster:
    def __init__(self):
        self.objects = {}
        self.force_errors = {}

    def put(self, resource, name, namespace, obj):
        self.objects[(resource, name, namespace)] = obj

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
        obj = self.objects.get(key)
        if obj is None:
            return 1, "", f'Error from server (NotFound): {resource} "{name}" not found'
        return 0, json.dumps(obj), ""


def _app_obj(repo_url=None, dest_ns=OBSERVABILITY_NAMESPACE, release_name=observability_state.RELEASE_NAME):
    return {
        "spec": {
            "source": {"repoURL": repo_url if repo_url is not None else f"oci://{ECR_REGISTRY}/{observability_state.HELM_REPO_PATH}", "helm": {"releaseName": release_name}},
            "destination": {"namespace": dest_ns},
        },
    }


def _populate_owned_cluster(cluster):
    cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj())
    cluster.put("namespace", OBSERVABILITY_NAMESPACE, None, {"metadata": {}})
    for name in observability_state.FOOTPRINT_DEPLOYMENTS:
        cluster.put("deployment", name, OBSERVABILITY_NAMESPACE, {"metadata": {}})
    for name in observability_state.FOOTPRINT_DAEMONSETS:
        cluster.put("daemonset", name, OBSERVABILITY_NAMESPACE, {"metadata": {}})
    cluster.put("serviceaccount", observability_state.CLOUDWATCH_AGENT_SA_NAME, OBSERVABILITY_NAMESPACE, {"metadata": {}})
    return cluster


def _classify(cluster):
    return observability_state.classify(cluster, environment=ENVIRONMENT, observability_namespace=OBSERVABILITY_NAMESPACE, argocd_namespace=ARGOCD_NAMESPACE, ecr_registry=ECR_REGISTRY)


class ObservabilityOwnershipStateTests(unittest.TestCase):
    def test_no_footprint_is_absent(self):
        cluster = FakeCluster()
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_ABSENT)
        self.assertEqual(result["reasons"], [])

    def test_fully_owned_is_owned(self):
        cluster = _populate_owned_cluster(FakeCluster())
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_OWNED)
        self.assertEqual(result["reasons"], [])

    def test_application_only_no_footprint_yet_is_owned(self):
        # Fresh reconciliation-in-progress shape: Application exists, nothing else rendered yet -- OWNED, not a completeness failure.
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj())
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_OWNED)

    def test_stale_namespace_managed_by_never_forces_broken(self):
        # Fix 2 (Generic MAIN Desired-State Convergence Safety Correction): the Application's own managedNamespaceMetadata contract (app.kubernetes.io/name/managed-by) is exact-desired-state, not ownership -- ordinary owned drift here must remain OWNED, converged by 40-sub-observability.yaml's own reconciliation. Strict post-reconcile verification lives in observability_acceptance.py.
        cluster = _populate_owned_cluster(FakeCluster())
        cluster.put("namespace", OBSERVABILITY_NAMESPACE, None, {"metadata": {"labels": {"app.kubernetes.io/name": "wrong-name", "app.kubernetes.io/managed-by": "Helm"}}})
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_OWNED)

    def test_missing_workloads_never_forces_broken(self):
        cluster = _populate_owned_cluster(FakeCluster())
        cluster.objects.pop(("deployment", observability_state.FOOTPRINT_DEPLOYMENTS[0], OBSERVABILITY_NAMESPACE))
        cluster.objects.pop(("daemonset", observability_state.FOOTPRINT_DAEMONSETS[0], OBSERVABILITY_NAMESPACE))
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_OWNED)

    def test_wrong_repo_url_is_broken(self):
        cluster = _populate_owned_cluster(FakeCluster())
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(repo_url="oci://wrong.example.com/helm/amazon-cloudwatch-observability"))
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any("foreign/ambiguous ownership" in r for r in result["reasons"]))

    def test_wrong_destination_namespace_is_broken(self):
        cluster = _populate_owned_cluster(FakeCluster())
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(dest_ns="some-other-namespace"))
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)

    def test_wrong_release_name_is_broken(self):
        cluster = _populate_owned_cluster(FakeCluster())
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(release_name="some-other-release"))
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)

    def test_application_absent_but_footprint_present_is_broken(self):
        cluster = _populate_owned_cluster(FakeCluster())
        cluster.objects.pop(("application", APP_NAME, ARGOCD_NAMESPACE))
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_BROKEN)
        self.assertTrue(any("foreign/ambiguous ownership" in r for r in result["reasons"]))

    def test_application_out_of_sync_never_forces_broken(self):
        # Ownership never inspects sync/health status or chart version -- an OutOfSync Application/stale chart version that otherwise clearly belongs here is exactly what MAIN is about to reconcile.
        cluster = _populate_owned_cluster(FakeCluster())
        app = _app_obj()
        app["status"] = {"sync": {"status": "OutOfSync"}, "health": {"status": "Progressing"}}
        app["spec"]["source"]["targetRevision"] = "1.0.0"
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, app)
        result = _classify(cluster)
        self.assertEqual(result["state"], observability_state.STATE_OWNED)

    def test_sa_role_arn_never_checked_in_ownership(self):
        # Structural/behavioral proof: the SA IRSA role-arn correctness is entirely an acceptance concern -- ownership does not even accept a role-arn argument.
        import inspect
        params = list(inspect.signature(observability_state.classify).parameters)
        self.assertEqual(params, ["run", "environment", "observability_namespace", "argocd_namespace", "ecr_registry"])

    def test_forbidden_raises_inspection_error(self):
        cluster = FakeCluster()
        cluster.fail("application", APP_NAME, ARGOCD_NAMESPACE, "Error from server (Forbidden): applications.argoproj.io is forbidden")
        with self.assertRaises(observability_state.ClassifierInspectionError):
            _classify(cluster)

    def test_malformed_json_raises_inspection_error(self):
        cluster = FakeCluster()

        def bad_run(args):
            if args[:2] == ["get", "application"]:
                return 0, "not valid json{{{", ""
            return cluster(args)

        with self.assertRaises(observability_state.ClassifierInspectionError):
            observability_state.classify(bad_run, environment=ENVIRONMENT, observability_namespace=OBSERVABILITY_NAMESPACE, argocd_namespace=ARGOCD_NAMESPACE, ecr_registry=ECR_REGISTRY)


class ObservabilityStateNoMutationSourceSweepTests(unittest.TestCase):
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
        cluster = _populate_owned_cluster(FakeCluster())
        _classify(cluster)


if __name__ == "__main__":
    unittest.main()
