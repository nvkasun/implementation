"""Offline tests for hack/orchestration/platform_state.py; run directly via `python3 hack/test-goldengate-platform-state.py`. No live Kubernetes -- every kubectl response is a fake, injected fixture. Exercises the classifier's actual logic (never merely greps its source)."""
from __future__ import annotations

import importlib.util
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL_PATH = os.path.join(REPO_ROOT, "hack", "orchestration", "platform_state.py")


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
RUNTIME_ROLE_ARN = "arn:aws:iam::668311715351:role/GoldenGateSecretsReadRole-dev"
PLATFORM_LOGGING_ROLE_ARN = "arn:aws:iam::668311715351:role/GoldenGatePlatformLoggingRole-dev"
FLUENT_BIT_IMAGE = f"{ECR_REGISTRY}/aws-cloud-factory-fluent-bit@sha256:{'a' * 64}"

RELEASE_NAME = f"goldengate-{ENVIRONMENT}-platform"
APP_NAME = RELEASE_NAME


class FakeCluster:
    """Models exactly the subset of `kubectl get <resource> [name] [-n ns] [-l selector] -o json` behavior the classifier depends on -- never a real kubectl process. A single-name get defaults to NotFound when unset; a list (no name) get defaults to an empty items array when unset, matching real kubectl semantics."""

    def __init__(self):
        self.objects = {}  # (resource, name, namespace) -> dict
        self.lists = {}  # (resource, namespace) -> [items...]
        self.force_errors = {}  # (resource, name, namespace) -> stderr text

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


def _app_obj(name, healthy=True, repo_url=None, dest_ns=RUNTIME_NAMESPACE, release_name=RELEASE_NAME):
    return {
        "metadata": {"name": name},
        "status": {
            "sync": {"status": "Synced" if healthy else "OutOfSync"},
            "health": {"status": "Healthy" if healthy else "Degraded"},
        },
        "spec": {
            "source": {
                "repoURL": repo_url if repo_url is not None else f"oci://{ECR_REGISTRY}/{platform_state.HELM_REPO_PATH}",
                "helm": {"releaseName": release_name},
            },
            "destination": {"namespace": dest_ns},
        },
    }


def _namespace_obj(name, labeled=True):
    labels = dict(platform_state.MANAGED_NAMESPACE_LABELS) if labeled else {}
    return {"metadata": {"name": name, "labels": labels}}


def _clusterrole_obj(name):
    return {"metadata": {"name": name}}


def _crb_obj(name, role_name=platform_state.FLUENT_BIT_CLUSTERROLE_NAME, sa_name=platform_state.FLUENT_BIT_SA_NAME, sa_namespace=RUNTIME_NAMESPACE):
    return {
        "metadata": {"name": name},
        "roleRef": {"kind": "ClusterRole", "name": role_name},
        "subjects": [{"kind": "ServiceAccount", "name": sa_name, "namespace": sa_namespace}],
    }


def _sa_obj(name, role_arn):
    return {"metadata": {"name": name, "annotations": {"eks.amazonaws.com/role-arn": role_arn}}}


def _configmap_obj(name):
    return {"metadata": {"name": name}}


def _daemonset_obj(name, generation=3, desired=2, service_account=platform_state.FLUENT_BIT_SA_NAME, image=FLUENT_BIT_IMAGE, container_name="fluent-bit", extra_containers=None, init_containers=None):
    containers = [{"name": container_name, "image": image}]
    if extra_containers:
        containers.extend(extra_containers)
    pod_spec = {
        "serviceAccountName": service_account,
        "containers": containers,
    }
    if init_containers:
        pod_spec["initContainers"] = init_containers
    return {
        "metadata": {"name": name, "generation": generation},
        "spec": {
            "template": {
                "spec": pod_spec,
            },
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


def _populate_healthy_cluster(cluster):
    cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(APP_NAME))
    cluster.put("namespace", RUNTIME_NAMESPACE, None, _namespace_obj(RUNTIME_NAMESPACE))
    cluster.put("clusterrole", platform_state.FLUENT_BIT_CLUSTERROLE_NAME, None, _clusterrole_obj(platform_state.FLUENT_BIT_CLUSTERROLE_NAME))
    cluster.put("clusterrolebinding", platform_state.FLUENT_BIT_CLUSTERROLEBINDING_NAME, None, _crb_obj(platform_state.FLUENT_BIT_CLUSTERROLEBINDING_NAME))
    cluster.put("serviceaccount", platform_state.RUNTIME_SA_NAME, RUNTIME_NAMESPACE, _sa_obj(platform_state.RUNTIME_SA_NAME, RUNTIME_ROLE_ARN))
    cluster.put("serviceaccount", platform_state.FLUENT_BIT_SA_NAME, RUNTIME_NAMESPACE, _sa_obj(platform_state.FLUENT_BIT_SA_NAME, PLATFORM_LOGGING_ROLE_ARN))
    cluster.put("configmap", platform_state.FLUENT_BIT_CONFIGMAP_NAME, RUNTIME_NAMESPACE, _configmap_obj(platform_state.FLUENT_BIT_CONFIGMAP_NAME))
    cluster.put("daemonset", platform_state.FLUENT_BIT_DAEMONSET_NAME, RUNTIME_NAMESPACE, _daemonset_obj(platform_state.FLUENT_BIT_DAEMONSET_NAME))
    cluster.put_list("statefulset", RUNTIME_NAMESPACE, [])
    cluster.put_list("deployment", RUNTIME_NAMESPACE, [])
    return cluster


def _classify(cluster, fluent_bit_image=FLUENT_BIT_IMAGE, ecr_registry=ECR_REGISTRY):
    return platform_state.classify(
        cluster,
        environment=ENVIRONMENT,
        runtime_namespace=RUNTIME_NAMESPACE,
        argocd_namespace=ARGOCD_NAMESPACE,
        ecr_registry=ecr_registry,
        runtime_role_arn=RUNTIME_ROLE_ARN,
        platform_logging_role_arn=PLATFORM_LOGGING_ROLE_ARN,
        fluent_bit_image=fluent_bit_image,
    )


class PlatformStateClassifierTests(unittest.TestCase):
    def test_1_no_footprint_is_absent(self):
        cluster = FakeCluster()
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_ABSENT)
        self.assertEqual(result["reasons"], [])

    def test_2_runtime_namespace_exists_application_absent_is_broken(self):
        cluster = FakeCluster()
        cluster.put("namespace", RUNTIME_NAMESPACE, None, _namespace_obj(RUNTIME_NAMESPACE))
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any("Application" in r and "does not exist" in r for r in result["reasons"]))

    def test_3_leftover_clusterrole_only_is_broken(self):
        cluster = FakeCluster()
        cluster.put("clusterrole", platform_state.FLUENT_BIT_CLUSTERROLE_NAME, None, _clusterrole_obj(platform_state.FLUENT_BIT_CLUSTERROLE_NAME))
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)

    def test_4_application_missing_while_resources_exist_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.objects.pop(("application", APP_NAME, ARGOCD_NAMESPACE))
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any("Application" in r and "does not exist" in r for r in result["reasons"]))

    def test_5_application_not_synced_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(APP_NAME, healthy=False))
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any("sync status" in r for r in result["reasons"]))

    def test_6_application_not_healthy_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        app = _app_obj(APP_NAME)
        app["status"]["health"]["status"] = "Progressing"
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, app)
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any("health status" in r for r in result["reasons"]))

    def test_7_wrong_application_repo_url_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(APP_NAME, repo_url="oci://wrong.example.com/helm/goldengate-platform"))
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any("source.repoURL" in r for r in result["reasons"]))

    def test_8_wrong_destination_namespace_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(APP_NAME, dest_ns="some-other-namespace"))
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any("destination.namespace" in r for r in result["reasons"]))

    def test_9_runtime_sa_missing_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.objects.pop(("serviceaccount", platform_state.RUNTIME_SA_NAME, RUNTIME_NAMESPACE))
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any(f"serviceaccount/{platform_state.RUNTIME_SA_NAME} does not exist" in r for r in result["reasons"]))

    def test_10_runtime_sa_wrong_irsa_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("serviceaccount", platform_state.RUNTIME_SA_NAME, RUNTIME_NAMESPACE, _sa_obj(platform_state.RUNTIME_SA_NAME, "arn:aws:iam::668311715351:role/SomeOtherRole"))
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any(f"serviceaccount/{platform_state.RUNTIME_SA_NAME} eks.amazonaws.com/role-arn" in r for r in result["reasons"]))

    def test_11_fluent_bit_sa_wrong_irsa_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("serviceaccount", platform_state.FLUENT_BIT_SA_NAME, RUNTIME_NAMESPACE, _sa_obj(platform_state.FLUENT_BIT_SA_NAME, "arn:aws:iam::668311715351:role/SomeOtherRole"))
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any(f"serviceaccount/{platform_state.FLUENT_BIT_SA_NAME} eks.amazonaws.com/role-arn" in r for r in result["reasons"]))

    def test_12_clusterrolebinding_wrong_subject_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("clusterrolebinding", platform_state.FLUENT_BIT_CLUSTERROLEBINDING_NAME, None, _crb_obj(platform_state.FLUENT_BIT_CLUSTERROLEBINDING_NAME, sa_name="some-other-sa"))
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any("subjects=" in r for r in result["reasons"]))

    def test_13_configmap_missing_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.objects.pop(("configmap", platform_state.FLUENT_BIT_CONFIGMAP_NAME, RUNTIME_NAMESPACE))
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any(f"configmap/{platform_state.FLUENT_BIT_CONFIGMAP_NAME} does not exist" in r for r in result["reasons"]))

    def test_14_daemonset_missing_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.objects.pop(("daemonset", platform_state.FLUENT_BIT_DAEMONSET_NAME, RUNTIME_NAMESPACE))
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any(f"daemonset/{platform_state.FLUENT_BIT_DAEMONSET_NAME} does not exist" in r for r in result["reasons"]))

    def test_15_daemonset_not_fully_ready_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        ds = _daemonset_obj(platform_state.FLUENT_BIT_DAEMONSET_NAME)
        ds["status"]["numberReady"] = 1
        cluster.put("daemonset", platform_state.FLUENT_BIT_DAEMONSET_NAME, RUNTIME_NAMESPACE, ds)
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any("not ready" in r for r in result["reasons"]))

    def test_16_wrong_fluent_bit_image_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        ds = _daemonset_obj(platform_state.FLUENT_BIT_DAEMONSET_NAME, image=f"{ECR_REGISTRY}/aws-cloud-factory-fluent-bit@sha256:{'b' * 64}")
        cluster.put("daemonset", platform_state.FLUENT_BIT_DAEMONSET_NAME, RUNTIME_NAMESPACE, ds)
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any("expected FLUENT_BIT_IMAGE" in r for r in result["reasons"]))

    def test_17_unexpected_platform_owned_deployment_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put_list("deployment", RUNTIME_NAMESPACE, [{"metadata": {"name": "gg-oracle-payments-01"}}])
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any("unexpectedly owns" in r for r in result["reasons"]))

    def test_17b_unexpected_platform_owned_statefulset_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put_list("statefulset", RUNTIME_NAMESPACE, [{"metadata": {"name": "gg-oracle-payments-01"}}])
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any("unexpectedly owns" in r for r in result["reasons"]))

    def test_18_complete_valid_platform_is_healthy(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_HEALTHY)
        self.assertEqual(result["reasons"], [])

    def test_19_forbidden_or_api_error_raises_inspection_error_not_absent(self):
        cluster = FakeCluster()
        cluster.fail("application", APP_NAME, ARGOCD_NAMESPACE, "Error from server (Forbidden): applications.argoproj.io is forbidden")
        with self.assertRaises(platform_state.ClassifierInspectionError):
            _classify(cluster)

    def test_managed_namespace_label_wrong_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("namespace", RUNTIME_NAMESPACE, None, _namespace_obj(RUNTIME_NAMESPACE, labeled=False))
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any("managedNamespaceMetadata" in r for r in result["reasons"]))

    def test_wrong_release_name_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(APP_NAME, release_name="some-other-release"))
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any("source.helm.releaseName" in r for r in result["reasons"]))

    def test_wrong_daemonset_service_account_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        ds = _daemonset_obj(platform_state.FLUENT_BIT_DAEMONSET_NAME, service_account="wrong-sa")
        cluster.put("daemonset", platform_state.FLUENT_BIT_DAEMONSET_NAME, RUNTIME_NAMESPACE, ds)
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any("serviceAccountName" in r for r in result["reasons"]))

    # --- Fluent Bit image contract: expected FLUENT_BIT_IMAGE operational configuration itself must be an exact, valid, immutable private-ECR digest reference (a caller configuration error, never ABSENT/HEALTHY/BROKEN cluster state) ---

    def test_20_expected_immutable_fluent_bit_image_passes_validation(self):
        # Validation alone (never touches the cluster); a still-clean cluster classifies ABSENT, proving no exception was raised for a well-formed FLUENT_BIT_IMAGE.
        result = _classify(FakeCluster(), fluent_bit_image=FLUENT_BIT_IMAGE)
        self.assertEqual(result["state"], platform_state.STATE_ABSENT)

    def test_21_expected_image_with_latest_tag_is_configuration_error(self):
        with self.assertRaises(ValueError):
            _classify(FakeCluster(), fluent_bit_image=f"{ECR_REGISTRY}/aws-cloud-factory-fluent-bit:latest")

    def test_22_expected_image_with_any_tag_is_configuration_error(self):
        with self.assertRaises(ValueError):
            _classify(FakeCluster(), fluent_bit_image=f"{ECR_REGISTRY}/aws-cloud-factory-fluent-bit:v1.2.3")

    def test_23_expected_image_wrong_ecr_registry_is_configuration_error(self):
        with self.assertRaises(ValueError):
            _classify(FakeCluster(), fluent_bit_image=f"public.ecr.aws/aws-cloud-factory-fluent-bit@sha256:{'a' * 64}")

    def test_23b_expected_image_wrong_private_registry_is_configuration_error(self):
        with self.assertRaises(ValueError):
            _classify(FakeCluster(), fluent_bit_image=f"999999999999.dkr.ecr.eu-west-1.amazonaws.com/aws-cloud-factory-fluent-bit@sha256:{'a' * 64}")

    def test_24_expected_image_wrong_repository_is_configuration_error(self):
        with self.assertRaises(ValueError):
            _classify(FakeCluster(), fluent_bit_image=f"{ECR_REGISTRY}/some-other-repository@sha256:{'a' * 64}")

    def test_25_expected_image_malformed_short_digest_is_configuration_error(self):
        with self.assertRaises(ValueError):
            _classify(FakeCluster(), fluent_bit_image=f"{ECR_REGISTRY}/aws-cloud-factory-fluent-bit@sha256:{'a' * 40}")

    def test_25b_expected_image_uppercase_digest_is_configuration_error(self):
        with self.assertRaises(ValueError):
            _classify(FakeCluster(), fluent_bit_image=f"{ECR_REGISTRY}/aws-cloud-factory-fluent-bit@sha256:{'A' * 64}")

    def test_25c_configuration_error_is_never_an_inspection_error(self):
        # The two error paths must stay distinct -- a bad FLUENT_BIT_IMAGE is a configuration error (ValueError), never a ClassifierInspectionError, and vice versa.
        with self.assertRaises(ValueError):
            _classify(FakeCluster(), fluent_bit_image="not-even-a-plausible-image-reference")
        try:
            _classify(FakeCluster(), fluent_bit_image="not-even-a-plausible-image-reference")
        except platform_state.ClassifierInspectionError:
            self.fail("an invalid FLUENT_BIT_IMAGE must raise ValueError, never ClassifierInspectionError")
        except ValueError:
            pass

    # --- Fluent Bit pod-template contract: exactly one container named fluent-bit using exactly FLUENT_BIT_IMAGE, no initContainers -- never merely "expected_image in images" ---

    def test_26_unexpected_normal_sidecar_alongside_correct_fluent_bit_container_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        ds = _daemonset_obj(platform_state.FLUENT_BIT_DAEMONSET_NAME, extra_containers=[{"name": "log-shipper-sidecar", "image": "some/other:image"}])
        cluster.put("daemonset", platform_state.FLUENT_BIT_DAEMONSET_NAME, RUNTIME_NAMESPACE, ds)
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any("expected exactly 1" in r for r in result["reasons"]))

    def test_27_unexpected_init_container_alongside_correct_fluent_bit_container_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        ds = _daemonset_obj(platform_state.FLUENT_BIT_DAEMONSET_NAME, init_containers=[{"name": "wait-for-something", "image": "busybox"}])
        cluster.put("daemonset", platform_state.FLUENT_BIT_DAEMONSET_NAME, RUNTIME_NAMESPACE, ds)
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any("initContainers" in r for r in result["reasons"]))

    def test_28_more_than_one_container_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        ds = _daemonset_obj(platform_state.FLUENT_BIT_DAEMONSET_NAME, extra_containers=[{"name": "another-container", "image": FLUENT_BIT_IMAGE}])
        cluster.put("daemonset", platform_state.FLUENT_BIT_DAEMONSET_NAME, RUNTIME_NAMESPACE, ds)
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any("expected exactly 1" in r for r in result["reasons"]))

    def test_29_sole_container_name_not_fluent_bit_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        ds = _daemonset_obj(platform_state.FLUENT_BIT_DAEMONSET_NAME, container_name="unexpected-container")
        cluster.put("daemonset", platform_state.FLUENT_BIT_DAEMONSET_NAME, RUNTIME_NAMESPACE, ds)
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any("sole container is named" in r for r in result["reasons"]))

    def test_30_exactly_one_fluent_bit_container_wrong_image_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        ds = _daemonset_obj(platform_state.FLUENT_BIT_DAEMONSET_NAME, image=f"{ECR_REGISTRY}/aws-cloud-factory-fluent-bit@sha256:{'b' * 64}")
        cluster.put("daemonset", platform_state.FLUENT_BIT_DAEMONSET_NAME, RUNTIME_NAMESPACE, ds)
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN)
        self.assertTrue(any("image=" in r and FLUENT_BIT_IMAGE in r for r in result["reasons"]))

    def test_31_exact_approved_fluent_bit_shape_is_healthy(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_HEALTHY)
        self.assertEqual(result["reasons"], [])

    def test_malformed_json_raises_inspection_error(self):
        cluster = FakeCluster()

        def bad_run(args):
            if args[:2] == ["get", "application"]:
                return 0, "not valid json{{{", ""
            return cluster(args)

        with self.assertRaises(platform_state.ClassifierInspectionError):
            platform_state.classify(
                bad_run,
                environment=ENVIRONMENT,
                runtime_namespace=RUNTIME_NAMESPACE,
                argocd_namespace=ARGOCD_NAMESPACE,
                ecr_registry=ECR_REGISTRY,
                runtime_role_arn=RUNTIME_ROLE_ARN,
                platform_logging_role_arn=PLATFORM_LOGGING_ROLE_ARN,
                fluent_bit_image=FLUENT_BIT_IMAGE,
            )


class PlatformStateFluentBitShapeArchitecturalRegressionTests(unittest.TestCase):
    """Behavioral proof that the Fluent Bit contract cannot regress back to the old, insufficient `expected_image in images` membership check -- both fixtures below contain FLUENT_BIT_IMAGE somewhere in the pod template (so a membership check would wrongly pass them), yet the real approved shape requires exactly one container named fluent-bit and no initContainers."""

    def test_membership_check_would_have_wrongly_passed_the_sidecar_fixture(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        ds = _daemonset_obj(platform_state.FLUENT_BIT_DAEMONSET_NAME, extra_containers=[{"name": "log-shipper-sidecar", "image": "some/other:image"}])
        cluster.put("daemonset", platform_state.FLUENT_BIT_DAEMONSET_NAME, RUNTIME_NAMESPACE, ds)

        pod_spec = ds["spec"]["template"]["spec"]
        naive_images = [c["image"] for c in pod_spec.get("containers", [])] + [c["image"] for c in pod_spec.get("initContainers", [])]
        self.assertIn(FLUENT_BIT_IMAGE, naive_images, "the old membership check's own precondition must hold for this fixture to be a meaningful regression proof")

        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN, "the classifier must reject the extra sidecar even though FLUENT_BIT_IMAGE is present somewhere in the pod template")

    def test_membership_check_would_have_wrongly_passed_the_init_container_fixture(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        ds = _daemonset_obj(platform_state.FLUENT_BIT_DAEMONSET_NAME, init_containers=[{"name": "wait-for-something", "image": "busybox"}])
        cluster.put("daemonset", platform_state.FLUENT_BIT_DAEMONSET_NAME, RUNTIME_NAMESPACE, ds)

        pod_spec = ds["spec"]["template"]["spec"]
        naive_images = [c["image"] for c in pod_spec.get("containers", [])] + [c["image"] for c in pod_spec.get("initContainers", [])]
        self.assertIn(FLUENT_BIT_IMAGE, naive_images, "the old membership check's own precondition must hold for this fixture to be a meaningful regression proof")

        result = _classify(cluster)
        self.assertEqual(result["state"], platform_state.STATE_BROKEN, "the classifier must reject the unexpected initContainer even though FLUENT_BIT_IMAGE is present somewhere in the pod template")

    def test_membership_check_would_have_wrongly_passed_a_mutable_tag_used_live(self):
        # A cluster whose live DaemonSet runs a mutable-tag image would satisfy an "in images" check only if the *expected* value were also mutable -- the real regression this closeout fixes is that the expected FLUENT_BIT_IMAGE itself is now validated up front, so a mutable expected value can never reach the cluster-state comparison at all.
        with self.assertRaises(ValueError):
            _classify(FakeCluster(), fluent_bit_image=f"{ECR_REGISTRY}/aws-cloud-factory-fluent-bit:latest")


class PlatformStateNoMutationSourceSweepTests(unittest.TestCase):
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
