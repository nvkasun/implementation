"""Offline tests for hack/orchestration/runtime_acceptance.py; run directly via `python3 hack/test-goldengate-runtime-acceptance.py`. No live Kubernetes/AWS -- every kubectl response is a fake, injected fixture, and the expected EFS filesystem ID is passed in exactly as the real workflow would after its own read-only AWS resolution. Exercises the classifier's actual logic (never merely greps its source). Fixtures are shaped after the real, currently-inactive envs/dev/gg-postgresql-repltest-01 descriptor (source role, managed EFS, ingress enabled) -- describe_deployment() reads the real repository, never a scratch root."""
from __future__ import annotations

import importlib.util
import json
import os
import unittest

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL_PATH = os.path.join(REPO_ROOT, "hack", "orchestration", "runtime_acceptance.py")


def _load_tool():
    spec = importlib.util.spec_from_file_location("runtime_acceptance", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime_acceptance = _load_tool()

ENVIRONMENT = "dev"
DEPLOYMENT_ID = "gg-postgresql-repltest-01"
ARGOCD_NAMESPACE = "argocd"
RUNTIME_NAMESPACE = "goldengate-dev"
ECR_REGISTRY = "229410149234.dkr.ecr.eu-west-1.amazonaws.com"
DNS_DOMAIN = "goldengate-dev.adcbmis.local"
ALB_GROUP_NAME = "gg-poc-dev-alb"
ACM_CERTIFICATE_ARN = "arn:aws:acm:eu-west-1:668311715351:certificate/9e53e28e-3243-47fc-85a1-50f9a94acde7"
AWS_REGION = "eu-west-1"
EXPECTED_FS_ID = "fs-0123456789abcdef0"

APP_NAME = f"goldengate-{ENVIRONMENT}-postgresql-repltest-01"
SC_NAME = f"gg-efs-{ENVIRONMENT}-{DEPLOYMENT_ID}"
CONTAINER_NAME = "ogg-postgresql"
SA_NAME = "gg-runtime-sa"
ADMIN_SECRET_OBJECT_NAME = "dev/goldengate/source/admin"
TLS_SECRET_OBJECT_NAME = "dev/goldengate/tls-certificate"
EXPECTED_IMAGE = f"{ECR_REGISTRY}/ogg-postgresql:23.26.2.0.1"


class FakeCluster:
    """Models exactly the subset of `kubectl get <resource> [name] [-n ns] [-l selector] -o json` behavior the classifier depends on -- never a real kubectl process. A single-name get defaults to NotFound when unset; a list (no name) get defaults to an empty items array when unset, matching real kubectl semantics."""

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
            return 0, json.dumps(obj), ""

        items = self.lists.get((resource, namespace), [])
        return 0, json.dumps({"items": items}), ""


def _app_obj(healthy=True, repo_url=None, dest_ns=RUNTIME_NAMESPACE, release_name=DEPLOYMENT_ID, env_label=ENVIRONMENT, id_label=DEPLOYMENT_ID):
    return {
        "metadata": {"labels": {"goldengate.adcb/environment": env_label, "goldengate.adcb/deployment-id": id_label}},
        "status": {
            "sync": {"status": "Synced" if healthy else "OutOfSync"},
            "health": {"status": "Healthy" if healthy else "Degraded"},
        },
        "spec": {
            "source": {
                "repoURL": repo_url if repo_url is not None else f"oci://{ECR_REGISTRY}/{runtime_acceptance.HELM_REPO_PATH}",
                "helm": {"releaseName": release_name},
            },
            "destination": {"namespace": dest_ns},
        },
    }


def _sts_obj(generation=3, replicas=1, containers=None, init_containers="default", service_account=SA_NAME, current_revision="rev-1", update_revision="rev-1"):
    if containers is None:
        containers = [{"name": CONTAINER_NAME, "image": EXPECTED_IMAGE}]
    if init_containers == "default":
        init_containers = [{"name": "prepare-u02-permissions", "image": EXPECTED_IMAGE}]
    pod_spec = {"serviceAccountName": service_account, "containers": containers}
    if init_containers:
        pod_spec["initContainers"] = init_containers
    return {
        "metadata": {"name": DEPLOYMENT_ID, "generation": generation},
        "spec": {"template": {"spec": pod_spec}},
        "status": {
            "observedGeneration": generation,
            "readyReplicas": replicas,
            "currentReplicas": replicas,
            "updatedReplicas": replicas,
            "currentRevision": current_revision,
            "updateRevision": update_revision,
        },
    }


def _service_obj(name, service_type="ClusterIP", cluster_ip=None, selector=None, ports=None):
    selector = selector if selector is not None else {"app.kubernetes.io/name": "goldengate", "app.kubernetes.io/instance": DEPLOYMENT_ID}
    ports = ports if ports is not None else [{"name": "https", "port": 8443}, {"name": "dist", "port": 9013}, {"name": "metrics", "port": 9015}]
    spec = {"type": service_type, "selector": selector, "ports": ports}
    if cluster_ip is not None:
        spec["clusterIP"] = cluster_ip
    return {"metadata": {"name": name}, "spec": spec}


def _storageclass_obj(provisioner="efs.csi.aws.com", provisioning_mode="efs-ap", file_system_id=EXPECTED_FS_ID, reclaim_policy="Retain"):
    return {"provisioner": provisioner, "parameters": {"provisioningMode": provisioning_mode, "fileSystemId": file_system_id}, "reclaimPolicy": reclaim_policy}


def _pvc_obj(phase="Bound", storage_class_name=SC_NAME, volume_name="pv-001"):
    return {"status": {"phase": phase}, "spec": {"storageClassName": storage_class_name, "volumeName": volume_name}}


def _pv_obj(driver="efs.csi.aws.com", volume_handle=None):
    volume_handle = volume_handle if volume_handle is not None else f"{EXPECTED_FS_ID}::fsap-0123456789abcdef0"
    return {"spec": {"csi": {"driver": driver, "volumeHandle": volume_handle}}}


def _spc_obj(object_name, region=AWS_REGION):
    objects_yaml = yaml.safe_dump([{"objectName": object_name, "objectType": "secretsmanager"}])
    return {"spec": {"provider": "aws", "parameters": {"region": region, "objects": objects_yaml}}}


def _secret_obj(keys=("OGG_ADMIN", "OGG_ADMIN_PWD")):
    return {"data": {k: "base64placeholder" for k in keys}}


def _endpointslice_obj(ready=True):
    return {"endpoints": [{"conditions": {"ready": ready}}]}


def _ingress_obj(host=None, group_name=ALB_GROUP_NAME, group_order="112", cert_arn=ACM_CERTIFICATE_ARN, target_type="ip", ingress_class="alb", backend_name=DEPLOYMENT_ID, backend_port="https"):
    host = host if host is not None else f"{DEPLOYMENT_ID}.{DNS_DOMAIN}"
    return {
        "metadata": {
            "namespace": RUNTIME_NAMESPACE,
            "annotations": {
                "alb.ingress.kubernetes.io/group.name": group_name,
                "alb.ingress.kubernetes.io/group.order": group_order,
                "alb.ingress.kubernetes.io/certificate-arn": cert_arn,
                "alb.ingress.kubernetes.io/target-type": target_type,
            },
        },
        "spec": {
            "ingressClassName": ingress_class,
            "rules": [{"host": host, "http": {"paths": [{"backend": {"service": {"name": backend_name, "port": {"name": backend_port}}}}]}}],
        },
    }


def _populate_healthy_cluster(cluster):
    cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj())
    cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj())
    cluster.put("storageclass", SC_NAME, None, _storageclass_obj())
    cluster.put("persistentvolumeclaim", f"{DEPLOYMENT_ID}-u02", RUNTIME_NAMESPACE, _pvc_obj())
    cluster.put("persistentvolume", "pv-001", None, _pv_obj())
    cluster.put("secretproviderclass", f"{DEPLOYMENT_ID}-admin", RUNTIME_NAMESPACE, _spc_obj(ADMIN_SECRET_OBJECT_NAME))
    cluster.put("secretproviderclass", f"{DEPLOYMENT_ID}-certificate", RUNTIME_NAMESPACE, _spc_obj(TLS_SECRET_OBJECT_NAME))
    cluster.put("secret", f"{DEPLOYMENT_ID}-admin", RUNTIME_NAMESPACE, _secret_obj())
    cluster.put("service", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _service_obj(DEPLOYMENT_ID))
    cluster.put("service", f"{DEPLOYMENT_ID}-headless", RUNTIME_NAMESPACE, _service_obj(f"{DEPLOYMENT_ID}-headless", cluster_ip="None"))
    cluster.put_list("endpointslices.discovery.k8s.io", RUNTIME_NAMESPACE, [_endpointslice_obj()])
    cluster.put("ingress", f"{DEPLOYMENT_ID}-ingress", RUNTIME_NAMESPACE, _ingress_obj())
    return cluster


def _classify(cluster, **overrides):
    kwargs = dict(
        environment=ENVIRONMENT,
        deployment_id=DEPLOYMENT_ID,
        argocd_namespace=ARGOCD_NAMESPACE,
        runtime_namespace=RUNTIME_NAMESPACE,
        ecr_registry=ECR_REGISTRY,
        dns_domain=DNS_DOMAIN,
        alb_group_name=ALB_GROUP_NAME,
        acm_certificate_arn=ACM_CERTIFICATE_ARN,
        aws_region=AWS_REGION,
        expected_efs_file_system_id=EXPECTED_FS_ID,
    )
    kwargs.update(overrides)
    return runtime_acceptance.classify(cluster, **kwargs)


class RuntimeAcceptanceClassifierTests(unittest.TestCase):
    def test_1_fully_expected_runtime_is_healthy(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_HEALTHY)
        self.assertEqual(result["reasons"], [])

    def test_2_application_missing_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.objects.pop(("application", APP_NAME, ARGOCD_NAMESPACE))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("does not exist" in r and "Application" in r for r in result["reasons"]))

    def test_3_application_not_synced_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(healthy=False))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("sync status" in r for r in result["reasons"]))

    def test_4_application_not_healthy_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        app = _app_obj()
        app["status"]["health"]["status"] = "Progressing"
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, app)
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("health status" in r for r in result["reasons"]))

    def test_5_wrong_application_identity_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(dest_ns="some-other-namespace"))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("destination.namespace" in r for r in result["reasons"]))

    def test_6_statefulset_missing_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.objects.pop(("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any(f"statefulset/{DEPLOYMENT_ID} does not exist" in r for r in result["reasons"]))

    def test_7_statefulset_not_fully_ready_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        sts = _sts_obj(replicas=1)
        sts["status"]["readyReplicas"] = 0
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, sts)
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("not ready" in r for r in result["reasons"]))

    def test_8_current_revision_not_equal_update_revision_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(current_revision="rev-1", update_revision="rev-2"))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("currentRevision" in r for r in result["reasons"]))

    def test_9_unexpected_regular_sidecar_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(containers=[{"name": CONTAINER_NAME, "image": EXPECTED_IMAGE}, {"name": "sidecar", "image": "some/other:image"}]))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("expected exactly 1" in r for r in result["reasons"]))

    def test_10_wrong_main_container_name_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(containers=[{"name": "wrong-name", "image": EXPECTED_IMAGE}]))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("sole container is named" in r for r in result["reasons"]))

    def test_11_wrong_main_image_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(containers=[{"name": CONTAINER_NAME, "image": f"{ECR_REGISTRY}/ogg-postgresql:wrong-tag"}]))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("image=" in r for r in result["reasons"]))

    def test_12_unexpected_init_container_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(init_containers=[{"name": "prepare-u02-permissions", "image": EXPECTED_IMAGE}, {"name": "extra-init", "image": "busybox"}]))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("initContainer" in r for r in result["reasons"]))

    def test_13_approved_prepare_u02_permissions_only_is_accepted(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_HEALTHY)

    def test_14_wrong_service_account_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(service_account="wrong-sa"))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("serviceAccountName" in r for r in result["reasons"]))

    def test_15_pvc_missing_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.objects.pop(("persistentvolumeclaim", f"{DEPLOYMENT_ID}-u02", RUNTIME_NAMESPACE))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("does not exist" in r and "persistentvolumeclaim" in r for r in result["reasons"]))

    def test_15b_pvc_not_bound_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("persistentvolumeclaim", f"{DEPLOYMENT_ID}-u02", RUNTIME_NAMESPACE, _pvc_obj(phase="Pending"))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("phase=" in r for r in result["reasons"]))

    def test_16_storageclass_wrong_efs_id_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("storageclass", SC_NAME, None, _storageclass_obj(file_system_id="fs-wrongwrongwrong"))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("fileSystemId" in r for r in result["reasons"]))

    def test_17_pv_wrong_csi_driver_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("persistentvolume", "pv-001", None, _pv_obj(driver="some-other-driver.csi.k8s.io"))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("spec.csi.driver" in r for r in result["reasons"]))

    def test_17b_pv_wrong_filesystem_volume_handle_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("persistentvolume", "pv-001", None, _pv_obj(volume_handle="fs-unrelatedfilesystem::fsap-0123456789abcdef0"))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("volumeHandle" in r for r in result["reasons"]))

    def test_18_admin_spc_wrong_object_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("secretproviderclass", f"{DEPLOYMENT_ID}-admin", RUNTIME_NAMESPACE, _spc_obj("dev/goldengate/wrong/admin"))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any(f"secretproviderclass/{DEPLOYMENT_ID}-admin" in r and "objectName" in r for r in result["reasons"]))

    def test_19_certificate_spc_wrong_object_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("secretproviderclass", f"{DEPLOYMENT_ID}-certificate", RUNTIME_NAMESPACE, _spc_obj("dev/goldengate/wrong/tls"))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any(f"secretproviderclass/{DEPLOYMENT_ID}-certificate" in r and "objectName" in r for r in result["reasons"]))

    def test_20_synced_admin_secret_missing_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.objects.pop(("secret", f"{DEPLOYMENT_ID}-admin", RUNTIME_NAMESPACE))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any(f"secret/{DEPLOYMENT_ID}-admin does not exist" in r for r in result["reasons"]))

    def test_21_synced_admin_secret_missing_one_required_key_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("secret", f"{DEPLOYMENT_ID}-admin", RUNTIME_NAMESPACE, _secret_obj(keys=("OGG_ADMIN",)))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("OGG_ADMIN_PWD" in r for r in result["reasons"]))
        # Never decodes/logs/compares the actual secret value.
        self.assertTrue(all("base64placeholder" not in r for r in result["reasons"]))

    def test_22_wrong_service_port_contract_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("service", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _service_obj(DEPLOYMENT_ID, ports=[{"name": "https", "port": 8443}]))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any(f"service/{DEPLOYMENT_ID} ports=" in r for r in result["reasons"]))

    def test_23_no_ready_service_endpoint_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put_list("endpointslices.discovery.k8s.io", RUNTIME_NAMESPACE, [_endpointslice_obj(ready=False)])
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("no Ready backing endpoint" in r for r in result["reasons"]))

    def test_24_wrong_ingress_host_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("ingress", f"{DEPLOYMENT_ID}-ingress", RUNTIME_NAMESPACE, _ingress_obj(host="wrong-host.example.com"))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("no rule with host" in r for r in result["reasons"]))

    def test_25_wrong_alb_group_certificate_order_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("ingress", f"{DEPLOYMENT_ID}-ingress", RUNTIME_NAMESPACE, _ingress_obj(group_name="wrong-alb-group"))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("group.name" in r for r in result["reasons"]))

        cluster2 = _populate_healthy_cluster(FakeCluster())
        cluster2.put("ingress", f"{DEPLOYMENT_ID}-ingress", RUNTIME_NAMESPACE, _ingress_obj(cert_arn="arn:aws:acm:eu-west-1:668311715351:certificate/wrong"))
        result2 = _classify(cluster2)
        self.assertEqual(result2["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("certificate-arn" in r for r in result2["reasons"]))

        cluster3 = _populate_healthy_cluster(FakeCluster())
        cluster3.put("ingress", f"{DEPLOYMENT_ID}-ingress", RUNTIME_NAMESPACE, _ingress_obj(group_order="999"))
        result3 = _classify(cluster3)
        self.assertEqual(result3["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("group.order" in r for r in result3["reasons"]))

    def test_26a_api_forbidden_raises_inspection_error(self):
        cluster = FakeCluster()
        cluster.fail("application", APP_NAME, ARGOCD_NAMESPACE, "Error from server (Forbidden): applications.argoproj.io is forbidden")
        with self.assertRaises(runtime_acceptance.ClassifierInspectionError):
            _classify(cluster)

    def test_26b_malformed_json_raises_inspection_error(self):
        cluster = FakeCluster()

        def bad_run(args):
            if args[:2] == ["get", "application"]:
                return 0, "not valid json{{{", ""
            return cluster(args)

        with self.assertRaises(runtime_acceptance.ClassifierInspectionError):
            runtime_acceptance.classify(
                bad_run,
                environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID,
                argocd_namespace=ARGOCD_NAMESPACE, runtime_namespace=RUNTIME_NAMESPACE,
                ecr_registry=ECR_REGISTRY, dns_domain=DNS_DOMAIN, alb_group_name=ALB_GROUP_NAME,
                acm_certificate_arn=ACM_CERTIFICATE_ARN, aws_region=AWS_REGION,
                expected_efs_file_system_id=EXPECTED_FS_ID,
            )

    def test_unknown_deployment_id_is_configuration_error(self):
        cluster = FakeCluster()
        with self.assertRaises(ValueError):
            _classify(cluster, deployment_id="gg-does-not-exist-anywhere")

    def test_managed_efs_without_expected_id_is_configuration_error(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        with self.assertRaises(ValueError):
            _classify(cluster, expected_efs_file_system_id=None)

    def test_target_type_wrong_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("ingress", f"{DEPLOYMENT_ID}-ingress", RUNTIME_NAMESPACE, _ingress_obj(target_type="instance"))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("target-type" in r for r in result["reasons"]))

    def test_wrong_ingress_backend_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("ingress", f"{DEPLOYMENT_ID}-ingress", RUNTIME_NAMESPACE, _ingress_obj(backend_port="http"))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("no path backend routing" in r for r in result["reasons"]))

    def test_headless_service_missing_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.objects.pop(("service", f"{DEPLOYMENT_ID}-headless", RUNTIME_NAMESPACE))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any(f"service/{DEPLOYMENT_ID}-headless does not exist" in r for r in result["reasons"]))

    def test_headless_service_wrong_cluster_ip_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("service", f"{DEPLOYMENT_ID}-headless", RUNTIME_NAMESPACE, _service_obj(f"{DEPLOYMENT_ID}-headless", cluster_ip="10.0.0.5"))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("clusterIP" in r for r in result["reasons"]))


class RuntimeAcceptanceNoMutationSourceSweepTests(unittest.TestCase):
    """Static source-safety proof: the classifier module (and its shared k8s_common helper) must never construct a mutating kubectl/helm command, and must never call AWS directly."""

    FORBIDDEN_SUBSTRINGS = (
        "kubectl apply", "kubectl create", "kubectl delete", "kubectl patch",
        "kubectl annotate", "kubectl label",
        "helm install", "helm upgrade", "helm uninstall",
        "boto3", "import subprocess",
    )

    def test_source_contains_no_mutating_or_aws_command(self):
        k8s_common_path = os.path.join(REPO_ROOT, "hack", "orchestration", "k8s_common.py")
        with open(TOOL_PATH) as f:
            source = f.read()
        # k8s_common.py legitimately imports subprocess (to shell out to kubectl); runtime_acceptance.py itself must not.
        hits = [s for s in self.FORBIDDEN_SUBSTRINGS if s in source]
        self.assertEqual(hits, [], f"{TOOL_PATH} contains a mutating-looking or AWS-SDK construct: {hits}")
        with open(k8s_common_path) as f:
            k8s_common_source = f.read()
        mutating_hits = [s for s in self.FORBIDDEN_SUBSTRINGS[:8] if s in k8s_common_source]
        self.assertEqual(mutating_hits, [], f"{k8s_common_path} contains a mutating-looking construct: {mutating_hits}")

    def test_every_get_json_call_uses_get_verb_only(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        _classify(cluster)


if __name__ == "__main__":
    unittest.main()
