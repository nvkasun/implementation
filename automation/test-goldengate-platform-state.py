"""Offline tests for automation/orchestration/platform_state.py (ownership-safety preflight: ABSENT/OWNED/BROKEN); run directly via `python3 automation/test-goldengate-platform-state.py`. No live Kubernetes -- every kubectl response is a fake, injected fixture. Exercises the classifier's actual logic (never merely greps its source). Post-reconciliation acceptance (HEALTHY/BROKEN) is a separate module -- see automation/test-goldengate-platform-acceptance.py."""
from __future__ import annotations

import importlib.util
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL_PATH = os.path.join(REPO_ROOT, "automation", "orchestration", "platform_state.py")


def _load_tool():
    spec = importlib.util.spec_from_file_location("platform_state", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


platform_state = _load_tool()

ENVIRONMENT = "dev"
RUNTIME_NAMESPACE = "goldengate-dev"
ARGOCD_NAMESPACE = "argocd"
ECR_REGISTRY = "229410149234.dkr.ecr.eu-west-1.amazonaws.com"

RELEASE_NAME = f"goldengate-{ENVIRONMENT}-platform"
APP_NAME = RELEASE_NAME


class FakeCluster:
    """Models exactly the subset of `kubectl get <resource> [name] [-n ns] [-l selector] -o json` behavior the classifier depends on -- never a real kubectl process."""

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


def _app_obj(repo_url=None, dest_ns=RUNTIME_NAMESPACE, release_name=RELEASE_NAME):
    return {
        "spec": {
            "source": {"repoURL": repo_url if repo_url is not None else f"oci://{ECR_REGISTRY}/{platform_state.HELM_REPO_PATH}", "helm": {"releaseName": release_name}},
            "destination": {"namespace": dest_ns},
        },
    }


def _namespace_obj(labeled=True, phase="Active"):
    labels = {"app.kubernetes.io/name": platform_state.NAMESPACE_OWNERSHIP_NAME_LABEL} if labeled else {}
    return {"metadata": {"labels": labels}, "status": {"phase": phase}}


def _owned_labels(extra=None):
    labels = {"app.kubernetes.io/instance": RELEASE_NAME, "goldengate.adcb/environment": ENVIRONMENT}
    if extra:
        labels.update(extra)
    return labels


def _populate_owned_cluster(cluster):
    cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj())
    cluster.put("namespace", RUNTIME_NAMESPACE, None, _namespace_obj())
    cluster.put("clusterrole", platform_state.FLUENT_BIT_CLUSTERROLE_NAME, None, {"metadata": {"labels": _owned_labels()}})
    cluster.put("clusterrolebinding", platform_state.FLUENT_BIT_CLUSTERROLEBINDING_NAME, None, {"metadata": {"labels": _owned_labels()}})
    cluster.put("serviceaccount", platform_state.RUNTIME_SA_NAME, RUNTIME_NAMESPACE, {"metadata": {"labels": _owned_labels()}})
    cluster.put("serviceaccount", platform_state.FLUENT_BIT_SA_NAME, RUNTIME_NAMESPACE, {"metadata": {"labels": _owned_labels()}})
    cluster.put("configmap", platform_state.FLUENT_BIT_CONFIGMAP_NAME, RUNTIME_NAMESPACE, {"metadata": {"labels": _owned_labels()}})
    cluster.put("daemonset", platform_state.FLUENT_BIT_DAEMONSET_NAME, RUNTIME_NAMESPACE, {"metadata": {"labels": _owned_labels()}})
    cluster.put_list("statefulset", RUNTIME_NAMESPACE, [])
    cluster.put_list("deployment", RUNTIME_NAMESPACE, [])
    return cluster


def _classify(cluster):
    return platform_state.classify(cluster, environment=ENVIRONMENT, runtime_namespace=RUNTIME_NAMESPACE, argocd_namespace=ARGOCD_NAMESPACE, ecr_registry=ECR_REGISTRY)


class PlatformOwnershipStateTests(unittest.TestCase):
    def test_no_footprint_is_absent(self):
        cluster = FakeCluster()
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_ABSENT)
        self.assertEqual(result["reasons"], [])

    def test_fully_owned_is_owned(self):
        cluster = _populate_owned_cluster(FakeCluster())
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_OWNED)
        self.assertEqual(result["reasons"], [])

    def test_P1_exact_live_incident_managed_by_helm_is_owned(self):
        # The exact live incident this architecture must resolve generically: app.kubernetes.io/managed-by=Helm on the namespace is now NEVER checked in ownership at all -- OWNED regardless, letting normal Argo CD reconciliation (managedNamespaceMetadata) converge it. See automation/test-goldengate-platform-acceptance.py for the strict post-reconcile managed-by=argocd proof.
        cluster = _populate_owned_cluster(FakeCluster())
        ns = _namespace_obj(labeled=True)
        ns["metadata"]["labels"]["app.kubernetes.io/managed-by"] = "Helm"
        cluster.put("namespace", RUNTIME_NAMESPACE, None, ns)
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_OWNED)

    def test_missing_fluent_bit_resources_never_forces_broken(self):
        # ConfigMap/DaemonSet/ServiceAccounts simply missing -- exactly what 30-sub-platform.yaml's own reconciliation creates, never itself a reason.
        cluster = _populate_owned_cluster(FakeCluster())
        cluster.objects.pop(("configmap", platform_state.FLUENT_BIT_CONFIGMAP_NAME, RUNTIME_NAMESPACE))
        cluster.objects.pop(("daemonset", platform_state.FLUENT_BIT_DAEMONSET_NAME, RUNTIME_NAMESPACE))
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_OWNED)

    def test_foreign_clusterrole_instance_label_is_broken(self):
        # Fix 1 (Generic MAIN Desired-State Convergence Safety Correction): a same-name foreign ClusterRole must never silently contribute to an OWNED footprint -- ownership labels are now actually validated, not merely existence.
        cluster = _populate_owned_cluster(FakeCluster())
        cluster.put("clusterrole", platform_state.FLUENT_BIT_CLUSTERROLE_NAME, None, {"metadata": {"labels": {"app.kubernetes.io/instance": "some-other-release", "goldengate.adcb/environment": ENVIRONMENT}}})
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any("clusterrole" in r and "foreign/ambiguous ownership" in r for r in result["reasons"]))

    def test_foreign_clusterrolebinding_instance_label_is_broken(self):
        cluster = _populate_owned_cluster(FakeCluster())
        cluster.put("clusterrolebinding", platform_state.FLUENT_BIT_CLUSTERROLEBINDING_NAME, None, {"metadata": {"labels": {"app.kubernetes.io/instance": "some-other-release", "goldengate.adcb/environment": ENVIRONMENT}}})
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any("clusterrolebinding" in r and "foreign/ambiguous ownership" in r for r in result["reasons"]))

    def test_owned_clusterrole_only_partial_footprint_is_owned(self):
        # A correctly-labeled ClusterRole existing alone (nothing else yet reconciled) is a safe partial footprint, not a completeness failure -- the architecture intentionally permits recovering/continuing a labeled partial Platform footprint.
        cluster = FakeCluster()
        cluster.put("clusterrole", platform_state.FLUENT_BIT_CLUSTERROLE_NAME, None, {"metadata": {"labels": _owned_labels()}})
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_OWNED)
        self.assertEqual(result["reasons"], [])

    def test_owned_clusterrole_and_clusterrolebinding_is_owned(self):
        cluster = FakeCluster()
        cluster.put("clusterrole", platform_state.FLUENT_BIT_CLUSTERROLE_NAME, None, {"metadata": {"labels": _owned_labels()}})
        cluster.put("clusterrolebinding", platform_state.FLUENT_BIT_CLUSTERROLEBINDING_NAME, None, {"metadata": {"labels": _owned_labels()}})
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_OWNED)
        self.assertEqual(result["reasons"], [])

    def test_missing_clusterrole_and_clusterrolebinding_with_owned_application_never_forces_broken(self):
        # Missing owned cluster-scoped RBAC is exactly what 30-sub-platform.yaml's own reconciliation (re)creates -- never itself a reason, mirroring the existing namespaced-resource-missing behavior.
        cluster = _populate_owned_cluster(FakeCluster())
        cluster.objects.pop(("clusterrole", platform_state.FLUENT_BIT_CLUSTERROLE_NAME, None))
        cluster.objects.pop(("clusterrolebinding", platform_state.FLUENT_BIT_CLUSTERROLEBINDING_NAME, None))
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_OWNED)
        self.assertEqual(result["reasons"], [])

    def test_namespace_foreign_name_label_is_broken(self):
        cluster = _populate_owned_cluster(FakeCluster())
        cluster.put("namespace", RUNTIME_NAMESPACE, None, _namespace_obj(labeled=False))
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any("foreign/ambiguous ownership" in r for r in result["reasons"]))

    def test_terminating_namespace_is_broken(self):
        cluster = _populate_owned_cluster(FakeCluster())
        cluster.put("namespace", RUNTIME_NAMESPACE, None, _namespace_obj(labeled=True, phase="Terminating"))
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any("Terminating" in r for r in result["reasons"]))

    def test_foreign_serviceaccount_instance_label_is_broken(self):
        cluster = _populate_owned_cluster(FakeCluster())
        cluster.put("serviceaccount", platform_state.RUNTIME_SA_NAME, RUNTIME_NAMESPACE, {"metadata": {"labels": {"app.kubernetes.io/instance": "some-other-release"}}})
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)

    def test_unexpected_owned_runtime_deployment_is_broken(self):
        cluster = _populate_owned_cluster(FakeCluster())
        cluster.put_list("deployment", RUNTIME_NAMESPACE, [{"metadata": {"name": "gg-oracle-payments-01"}}])
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any("unexpectedly owns" in r for r in result["reasons"]))

    def test_application_repo_url_mismatch_is_broken(self):
        cluster = _populate_owned_cluster(FakeCluster())
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(repo_url="oci://wrong.example.com/helm/goldengate-platform"))
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)

    def test_application_wrong_release_name_is_broken(self):
        cluster = _populate_owned_cluster(FakeCluster())
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(release_name="some-other-release"))
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)

    def test_application_out_of_sync_never_forces_broken(self):
        # Ownership never inspects sync/health status -- an OutOfSync/Degraded Application that otherwise clearly belongs to this platform is exactly what MAIN is about to reconcile.
        cluster = _populate_owned_cluster(FakeCluster())
        app = _app_obj()
        app["status"] = {"sync": {"status": "OutOfSync"}, "health": {"status": "Degraded"}}
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, app)
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_OWNED)

    def test_forbidden_or_api_error_raises_inspection_error(self):
        cluster = FakeCluster()
        cluster.fail("application", APP_NAME, ARGOCD_NAMESPACE, "Error from server (Forbidden): applications.argoproj.io is forbidden")
        with self.assertRaises(platform_state.ClassifierInspectionError):
            _classify(cluster)

    def test_malformed_json_raises_inspection_error(self):
        cluster = FakeCluster()

        def bad_run(args):
            if args[:2] == ["get", "application"]:
                return 0, "not valid json{{{", ""
            return cluster(args)

        with self.assertRaises(platform_state.ClassifierInspectionError):
            platform_state.classify(bad_run, environment=ENVIRONMENT, runtime_namespace=RUNTIME_NAMESPACE, argocd_namespace=ARGOCD_NAMESPACE, ecr_registry=ECR_REGISTRY)

    def test_classify_signature_takes_no_fluent_bit_image_argument(self):
        # Structural proof of the generic-architecture goal: ownership classify() no longer takes fluent_bit_image/runtime_role_arn/platform_logging_role_arn -- those are acceptance-only concerns.
        import inspect
        params = list(inspect.signature(platform_state.classify).parameters)
        self.assertEqual(params, ["run", "environment", "runtime_namespace", "argocd_namespace", "ecr_registry"])


class PlatformStateNoMutationSourceSweepTests(unittest.TestCase):
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
        cluster = _populate_owned_cluster(FakeCluster())
        _classify(cluster)


if __name__ == "__main__":
    unittest.main()
