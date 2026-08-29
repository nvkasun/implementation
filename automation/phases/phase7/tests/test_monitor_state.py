"""Offline tests for automation/orchestration/monitor_state.py; run directly via `python3 automation/phases/phase7/tests/test_monitor_state.py`. No live Kubernetes -- every kubectl response is a fake, injected fixture. Exercises the classifier's actual logic (never merely greps its source)."""
from __future__ import annotations

import importlib.util
import json
import os
import unittest
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parents[4])
TOOL_PATH = os.path.join(REPO_ROOT, "automation", "orchestration", "monitor_state.py")


def _load_tool():
    spec = importlib.util.spec_from_file_location("monitor_state", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


monitor_state = _load_tool()

ENVIRONMENT = "dev"
ARGOCD_NAMESPACE = "argocd"
MONITOR_NAMESPACE = "goldengate-monitor-dev"
ECR_REGISTRY = "229410149234.dkr.ecr.eu-west-1.amazonaws.com"

APP_NAME = monitor_state.ARGOCD_APP_NAME


class FakeCluster:
    """Models exactly the subset of `kubectl get <resource> <name> [-n ns] -o json` behavior the classifier depends on -- never a real kubectl process. Every call here is a single-name get; unset keys default to NotFound, matching real kubectl semantics."""

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


def _resource_labels(environment=ENVIRONMENT):
    return {
        "app.kubernetes.io/name": "gg-monitor",
        "app.kubernetes.io/instance": monitor_state.RELEASE_NAME,
        "goldengate.adcb/environment": environment,
    }


def _namespace_labels(environment=ENVIRONMENT):
    return {
        "app.kubernetes.io/name": "gg-monitor",
        "app.kubernetes.io/managed-by": "argocd",
        "goldengate.adcb/environment": environment,
    }


def _app_obj(environment=ENVIRONMENT, dest_ns=MONITOR_NAMESPACE, repo_url=None, release_name=None, name_label="gg-monitor", managed_by_label="argocd", app_namespace=ARGOCD_NAMESPACE):
    return {
        "metadata": {
            "name": APP_NAME,
            "namespace": app_namespace,
            "labels": {
                "app.kubernetes.io/name": name_label,
                "app.kubernetes.io/managed-by": managed_by_label,
            },
        },
        "spec": {
            "source": {
                "repoURL": repo_url if repo_url is not None else f"oci://{ECR_REGISTRY}/{monitor_state.HELM_REPO_PATH}",
                "helm": {"releaseName": release_name if release_name is not None else monitor_state.RELEASE_NAME},
            },
            "destination": {"namespace": dest_ns},
        },
    }


def _named_obj(name, labels):
    return {"metadata": {"name": name, "labels": labels}}


def _populate_owned_footprint(cluster, environment=ENVIRONMENT):
    """Populates every expected-name resource with correctly-owned labels (footprint fully present, matching a prior successful reconciliation)."""
    cluster.put("namespace", MONITOR_NAMESPACE, None, _named_obj(MONITOR_NAMESPACE, _namespace_labels(environment)))
    cluster.put("deployment", monitor_state.DEPLOYMENT_NAME, MONITOR_NAMESPACE, _named_obj(monitor_state.DEPLOYMENT_NAME, _resource_labels(environment)))
    cluster.put("service", monitor_state.SERVICE_NAME, MONITOR_NAMESPACE, _named_obj(monitor_state.SERVICE_NAME, _resource_labels(environment)))
    cluster.put("serviceaccount", monitor_state.SERVICE_ACCOUNT_NAME, MONITOR_NAMESPACE, _named_obj(monitor_state.SERVICE_ACCOUNT_NAME, _resource_labels(environment)))
    cluster.put("secretproviderclass", monitor_state.SECRETPROVIDERCLASS_NAME, MONITOR_NAMESPACE, _named_obj(monitor_state.SECRETPROVIDERCLASS_NAME, _resource_labels(environment)))
    cluster.put("configmap", monitor_state.CONFIGMAP_NAME, MONITOR_NAMESPACE, _named_obj(monitor_state.CONFIGMAP_NAME, _resource_labels(environment)))
    return cluster


def _classify(cluster):
    return monitor_state.classify(
        cluster,
        environment=ENVIRONMENT,
        argocd_namespace=ARGOCD_NAMESPACE,
        monitor_namespace=MONITOR_NAMESPACE,
        ecr_registry=ECR_REGISTRY,
    )


class MonitorStateClassifierTests(unittest.TestCase):
    # 1. no App, no footprint at all -> ABSENT.
    def test_1_no_app_no_footprint_is_absent(self):
        cluster = FakeCluster()
        result = _classify(cluster)
        self.assertEqual(result["state"], monitor_state.STATE_ABSENT)
        self.assertEqual(result["reasons"], [])

    # 2. App absent, Deployment exists -> BROKEN.
    def test_2_app_absent_deployment_exists_is_broken(self):
        cluster = FakeCluster()
        cluster.put("deployment", monitor_state.DEPLOYMENT_NAME, MONITOR_NAMESPACE, _named_obj(monitor_state.DEPLOYMENT_NAME, _resource_labels()))
        result = _classify(cluster)
        self.assertEqual(result["state"], monitor_state.STATE_BROKEN)
        self.assertTrue(any("does not exist" in r and "Application" in r for r in result["reasons"]))

    # 3. App absent, namespace exists -> BROKEN.
    def test_3_app_absent_namespace_exists_is_broken(self):
        cluster = FakeCluster()
        cluster.put("namespace", MONITOR_NAMESPACE, None, _named_obj(MONITOR_NAMESPACE, _namespace_labels()))
        result = _classify(cluster)
        self.assertEqual(result["state"], monitor_state.STATE_BROKEN)
        self.assertTrue(any("does not exist" in r and "Application" in r for r in result["reasons"]))

    # 4. Correct App, no resources yet -> OWNED (safe to reconcile from nothing).
    def test_4_correct_app_no_resources_is_owned(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj())
        result = _classify(cluster)
        self.assertEqual(result["state"], monitor_state.STATE_OWNED)
        self.assertEqual(result["reasons"], [])

    # 5. Correct App, OutOfSync -> still OWNED (health/sync is never an ownership-safety concern).
    def test_5_correct_app_outofsync_is_still_owned(self):
        cluster = FakeCluster()
        app = _app_obj()
        app["status"] = {"sync": {"status": "OutOfSync"}, "health": {"status": "Healthy"}}
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, app)
        result = _classify(cluster)
        self.assertEqual(result["state"], monitor_state.STATE_OWNED)

    # 6. Correct App, Degraded -> still OWNED.
    def test_6_correct_app_degraded_is_still_owned(self):
        cluster = FakeCluster()
        app = _app_obj()
        app["status"] = {"sync": {"status": "Synced"}, "health": {"status": "Degraded"}}
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, app)
        result = _classify(cluster)
        self.assertEqual(result["state"], monitor_state.STATE_OWNED)

    # 7. Correct App, Deployment exists but is unhealthy/not-ready -> still OWNED (a struggling rollout is exactly what MAIN is about to reconcile, not an ownership conflict).
    def test_7_correct_app_unhealthy_deployment_is_still_owned(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj())
        not_ready = _named_obj(monitor_state.DEPLOYMENT_NAME, _resource_labels())
        not_ready["status"] = {"readyReplicas": 0}
        cluster.put("deployment", monitor_state.DEPLOYMENT_NAME, MONITOR_NAMESPACE, not_ready)
        result = _classify(cluster)
        self.assertEqual(result["state"], monitor_state.STATE_OWNED)

    # 8. Correct App, missing Deployment/Service entirely -> still OWNED.
    def test_8_correct_app_missing_deployment_and_service_is_still_owned(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj())
        result = _classify(cluster)
        self.assertEqual(result["state"], monitor_state.STATE_OWNED)

    # 9. Wrong destination.namespace -> BROKEN.
    def test_9_app_wrong_destination_namespace_is_broken(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(dest_ns="some-other-namespace"))
        result = _classify(cluster)
        self.assertEqual(result["state"], monitor_state.STATE_BROKEN)
        self.assertTrue(any("destination.namespace" in r for r in result["reasons"]))

    # 10. Wrong source.repoURL -> BROKEN.
    def test_10_app_wrong_repo_url_is_broken(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(repo_url="oci://wrong.example.com/helm/goldengate-monitor"))
        result = _classify(cluster)
        self.assertEqual(result["state"], monitor_state.STATE_BROKEN)
        self.assertTrue(any("source.repoURL" in r for r in result["reasons"]))

    # 11. Wrong source.helm.releaseName -> BROKEN.
    def test_11_app_wrong_release_name_is_broken(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(release_name="some-other-release"))
        result = _classify(cluster)
        self.assertEqual(result["state"], monitor_state.STATE_BROKEN)
        self.assertTrue(any("source.helm.releaseName" in r for r in result["reasons"]))

    # 12. Wrong Application ownership label (app.kubernetes.io/name / app.kubernetes.io/managed-by) -> BROKEN.
    def test_12_app_wrong_ownership_label_is_broken(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(name_label="something-else"))
        result = _classify(cluster)
        self.assertEqual(result["state"], monitor_state.STATE_BROKEN)
        self.assertTrue(any("app.kubernetes.io/name" in r for r in result["reasons"]))

        cluster2 = FakeCluster()
        cluster2.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(managed_by_label="helm"))
        result2 = _classify(cluster2)
        self.assertEqual(result2["state"], monitor_state.STATE_BROKEN)
        self.assertTrue(any("app.kubernetes.io/managed-by" in r for r in result2["reasons"]))

    # 12b. Wrong Application metadata.namespace -> BROKEN.
    def test_12b_app_wrong_metadata_namespace_is_broken(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(app_namespace="some-other-argocd-ns"))
        result = _classify(cluster)
        self.assertEqual(result["state"], monitor_state.STATE_BROKEN)
        self.assertTrue(any("metadata.namespace" in r for r in result["reasons"]))

    # 13. Foreign Deployment ownership -> BROKEN.
    def test_13_foreign_deployment_ownership_is_broken(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj())
        cluster.put("deployment", monitor_state.DEPLOYMENT_NAME, MONITOR_NAMESPACE, _named_obj(monitor_state.DEPLOYMENT_NAME, _resource_labels(environment="sit")))
        result = _classify(cluster)
        self.assertEqual(result["state"], monitor_state.STATE_BROKEN)
        self.assertTrue(any("deployment" in r and "incompatible ownership" in r for r in result["reasons"]))

    # 14. Foreign ServiceAccount ownership -> BROKEN.
    def test_14_foreign_serviceaccount_ownership_is_broken(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj())
        cluster.put("serviceaccount", monitor_state.SERVICE_ACCOUNT_NAME, MONITOR_NAMESPACE, _named_obj(monitor_state.SERVICE_ACCOUNT_NAME, {"app.kubernetes.io/instance": "some-other-release"}))
        result = _classify(cluster)
        self.assertEqual(result["state"], monitor_state.STATE_BROKEN)
        self.assertTrue(any("serviceaccount" in r and "incompatible ownership" in r for r in result["reasons"]))

    # 15. Foreign ConfigMap ownership -> BROKEN.
    def test_15_foreign_configmap_ownership_is_broken(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj())
        cluster.put("configmap", monitor_state.CONFIGMAP_NAME, MONITOR_NAMESPACE, _named_obj(monitor_state.CONFIGMAP_NAME, {}))
        result = _classify(cluster)
        self.assertEqual(result["state"], monitor_state.STATE_BROKEN)
        self.assertTrue(any("configmap" in r and "incompatible ownership" in r for r in result["reasons"]))

    # 16. Foreign SecretProviderClass ownership -> BROKEN.
    def test_16_foreign_secretproviderclass_ownership_is_broken(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj())
        cluster.put("secretproviderclass", monitor_state.SECRETPROVIDERCLASS_NAME, MONITOR_NAMESPACE, _named_obj(monitor_state.SECRETPROVIDERCLASS_NAME, _resource_labels(environment="sit")))
        result = _classify(cluster)
        self.assertEqual(result["state"], monitor_state.STATE_BROKEN)
        self.assertTrue(any("secretproviderclass" in r and "incompatible ownership" in r for r in result["reasons"]))

    # 17. Foreign namespace ownership (right App, wrong namespace labels) -> BROKEN.
    def test_17_foreign_namespace_ownership_is_broken(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj())
        cluster.put("namespace", MONITOR_NAMESPACE, None, _named_obj(MONITOR_NAMESPACE, _namespace_labels(environment="sit")))
        result = _classify(cluster)
        self.assertEqual(result["state"], monitor_state.STATE_BROKEN)
        self.assertTrue(any("namespace" in r and "goldengate.adcb/environment" in r for r in result["reasons"]))

    # 18. Forbidden/API failure -> ClassifierInspectionError, never silently downgraded to ABSENT.
    def test_18_forbidden_or_api_error_raises_inspection_error_not_absent(self):
        cluster = FakeCluster()
        cluster.fail("application", APP_NAME, ARGOCD_NAMESPACE, "Error from server (Forbidden): applications.argoproj.io is forbidden")
        with self.assertRaises(monitor_state.ClassifierInspectionError):
            _classify(cluster)

    def test_full_owned_footprint_with_correct_app_is_owned(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj())
        _populate_owned_footprint(cluster)
        result = _classify(cluster)
        self.assertEqual(result["state"], monitor_state.STATE_OWNED)
        self.assertEqual(result["reasons"], [])

    def test_malformed_json_raises_inspection_error(self):
        cluster = FakeCluster()

        def bad_run(args):
            if args[:2] == ["get", "application"]:
                return 0, "not valid json{{{", ""
            return cluster(args)

        with self.assertRaises(monitor_state.ClassifierInspectionError):
            monitor_state.classify(
                bad_run,
                environment=ENVIRONMENT,
                argocd_namespace=ARGOCD_NAMESPACE,
                monitor_namespace=MONITOR_NAMESPACE,
                ecr_registry=ECR_REGISTRY,
            )


class MonitorStateNoMutationSourceSweepTests(unittest.TestCase):
    """Static source-safety proof: the classifier module (and its shared k8s_common helper) must never construct a mutating kubectl/helm command."""

    FORBIDDEN_SUBSTRINGS = (
        "kubectl apply", "kubectl create", "kubectl delete", "kubectl patch",
        "kubectl annotate", "kubectl label",
        "helm install", "helm upgrade", "helm uninstall",
    )

    def test_source_contains_no_mutating_command(self):
        k8s_common_path = os.path.join(REPO_ROOT, "automation", "orchestration", "k8s_common.py")
        for path in (TOOL_PATH, k8s_common_path):
            with open(path) as f:
                source = f.read()
            hits = [s for s in self.FORBIDDEN_SUBSTRINGS if s in source]
            self.assertEqual(hits, [], f"{path} contains a mutating-looking construct: {hits}")

    def test_every_get_json_call_uses_get_verb_only(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj())
        _populate_owned_footprint(cluster)
        _classify(cluster)


if __name__ == "__main__":
    unittest.main()
