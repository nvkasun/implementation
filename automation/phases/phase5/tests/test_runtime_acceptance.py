"""Offline tests for automation/phases/phase5/runtime_acceptance.py; run directly via `python3 automation/phases/phase5/tests/test_runtime_acceptance.py`. No live Kubernetes/AWS -- every kubectl response is a fake, injected fixture, and the expected EFS filesystem ID is passed in exactly as the real workflow would after its own read-only AWS resolution. Exercises the classifier's actual logic (never merely greps its source). Fixtures are shaped after the real, currently-inactive envs/dev/gg-postgresql-repltest-01 descriptor (source role, managed EFS, ingress enabled) -- describe_deployment() reads the real repository, never a scratch root, EXCEPT the dedicated RuntimeAcceptanceExternalClaimTests class below, which uses an isolated scratch environment to exercise the supported explicit-existingClaim shape those real descriptors do not use."""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = str(Path(__file__).resolve().parents[4])
TOOL_PATH = os.path.join(REPO_ROOT, "automation", "phases", "phase5", "runtime_acceptance.py")


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
EXPECTED_U02_CLAIM = f"{DEPLOYMENT_ID}-u02"
ADMIN_MOUNT_PATH = "/mnt/secrets-store/admin"
CERTIFICATE_MOUNT_PATH = "/etc/nginx/cert"
EXPECTED_SELECTOR = {"app.kubernetes.io/name": "goldengate", "app.kubernetes.io/instance": DEPLOYMENT_ID}

# Real gg-postgresql-repltest-01 descriptor: source role -> https/dist/metrics, no receiver.
DEFAULT_SERVICE_PORT_VALUES = {"https": 8443, "dist": 9013, "receiver": 9014, "metrics": 9015}
MAIN_CONTAINER_PORTS = [
    {"name": "https", "containerPort": 8443, "protocol": "TCP"},
    {"name": "dist", "containerPort": 9013, "protocol": "TCP"},
    {"name": "metrics", "containerPort": 9015, "protocol": "TCP"},
]


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


def _pod_volumes(u02_claim_name="default", u02_empty_dir=False, u03_empty_dir=True,
                  include_admin_csi=True, admin_driver="secrets-store.csi.k8s.io", admin_spc="default", admin_read_only=True,
                  include_certificate_csi=True, certificate_driver="secrets-store.csi.k8s.io", certificate_spc="default", certificate_read_only=True,
                  extra_volumes=None, omit_u02=False, omit_u03=False):
    """Exact expected spec.template.spec.volumes shape rendered by helm/goldengate/templates/runtime-statefulset.yaml for this deployment's real descriptor (u02Type=efs, both CSI secrets enabled)."""
    volumes = []
    if not omit_u02:
        if u02_empty_dir:
            volumes.append({"name": "u02", "emptyDir": {}})
        else:
            claim = EXPECTED_U02_CLAIM if u02_claim_name == "default" else u02_claim_name
            volumes.append({"name": "u02", "persistentVolumeClaim": {"claimName": claim}})
    if not omit_u03:
        if u03_empty_dir:
            volumes.append({"name": "u03", "emptyDir": {}})
        else:
            volumes.append({"name": "u03", "persistentVolumeClaim": {"claimName": "not-supposed-to-be-a-pvc"}})
    if include_admin_csi:
        spc = f"{DEPLOYMENT_ID}-admin" if admin_spc == "default" else admin_spc
        volumes.append({"name": "ogg-admin-csi", "csi": {"driver": admin_driver, "readOnly": admin_read_only, "volumeAttributes": {"secretProviderClass": spc}}})
    if include_certificate_csi:
        spc = f"{DEPLOYMENT_ID}-certificate" if certificate_spc == "default" else certificate_spc
        volumes.append({"name": "ogg-nginx-cert-csi", "csi": {"driver": certificate_driver, "readOnly": certificate_read_only, "volumeAttributes": {"secretProviderClass": spc}}})
    if extra_volumes:
        volumes.extend(extra_volumes)
    return volumes


def _main_container_mounts(include_u02=True, u02_path="/u02", include_u03=True, u03_path="/u03",
                            include_admin_csi=True, admin_path="default", admin_read_only=True,
                            include_certificate_csi=True, certificate_path="default", certificate_read_only=True,
                            extra_mounts=None):
    mounts = []
    if include_u02:
        mounts.append({"name": "u02", "mountPath": u02_path})
    if include_u03:
        mounts.append({"name": "u03", "mountPath": u03_path})
    if include_admin_csi:
        path = ADMIN_MOUNT_PATH if admin_path == "default" else admin_path
        mounts.append({"name": "ogg-admin-csi", "mountPath": path, "readOnly": admin_read_only})
    if include_certificate_csi:
        path = CERTIFICATE_MOUNT_PATH if certificate_path == "default" else certificate_path
        mounts.append({"name": "ogg-nginx-cert-csi", "mountPath": path, "readOnly": certificate_read_only})
    if extra_mounts:
        mounts.extend(extra_mounts)
    return mounts


def _init_container_mounts(include_u02=True, include_u03=True, include_admin_csi=True, admin_path="default"):
    mounts = []
    if include_u02:
        mounts.append({"name": "u02", "mountPath": "/u02"})
    if include_u03:
        mounts.append({"name": "u03", "mountPath": "/u03"})
    if include_admin_csi:
        path = ADMIN_MOUNT_PATH if admin_path == "default" else admin_path
        mounts.append({"name": "ogg-admin-csi", "mountPath": path, "readOnly": True})
    return mounts


def _main_container(name=CONTAINER_NAME, image=EXPECTED_IMAGE, mounts="default", ports="default"):
    return {
        "name": name,
        "image": image,
        "volumeMounts": _main_container_mounts() if mounts == "default" else mounts,
        "ports": list(MAIN_CONTAINER_PORTS) if ports == "default" else ports,
    }


def _init_container(name="prepare-u02-permissions", image=EXPECTED_IMAGE, mounts="default"):
    return {
        "name": name,
        "image": image,
        "volumeMounts": _init_container_mounts() if mounts == "default" else mounts,
    }


def _sts_obj(generation=3, replicas=1, containers="default", init_containers="default", service_account=SA_NAME,
             current_revision="rev-1", update_revision="rev-1", service_name=None, match_labels="default",
             pod_labels="default", volumes="default"):
    if containers == "default":
        containers = [_main_container()]
    if init_containers == "default":
        init_containers = [_init_container()]
    pod_spec = {
        "serviceAccountName": service_account,
        "containers": containers,
        "volumes": _pod_volumes() if volumes == "default" else volumes,
    }
    if init_containers:
        pod_spec["initContainers"] = init_containers
    return {
        "metadata": {"name": DEPLOYMENT_ID, "generation": generation},
        "spec": {
            "serviceName": service_name if service_name is not None else f"{DEPLOYMENT_ID}-headless",
            "selector": {"matchLabels": dict(EXPECTED_SELECTOR) if match_labels == "default" else match_labels},
            "template": {
                "metadata": {"labels": dict(EXPECTED_SELECTOR) if pod_labels == "default" else pod_labels},
                "spec": pod_spec,
            },
        },
        "status": {
            "observedGeneration": generation,
            "readyReplicas": replicas,
            "currentReplicas": replicas,
            "updatedReplicas": replicas,
            "currentRevision": current_revision,
            "updateRevision": update_revision,
        },
    }


def _service_ports(names=("https", "dist", "metrics"), port_values=None, override=None):
    port_values = port_values or DEFAULT_SERVICE_PORT_VALUES
    ports = [{"name": n, "port": port_values[n], "targetPort": n, "protocol": "TCP"} for n in names]
    if override:
        for i, p in enumerate(ports):
            if p["name"] in override:
                ports[i] = {**p, **override[p["name"]]}
    return ports


def _service_obj(name, service_type="ClusterIP", cluster_ip=None, selector="default", ports="default"):
    selector = dict(EXPECTED_SELECTOR) if selector == "default" else selector
    ports = _service_ports() if ports == "default" else ports
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
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(containers=[_main_container(), {"name": "sidecar", "image": "some/other:image"}]))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("expected exactly 1" in r for r in result["reasons"]))

    def test_10_wrong_main_container_name_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(containers=[_main_container(name="wrong-name")]))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("sole container is named" in r for r in result["reasons"]))

    def test_11_wrong_main_image_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(containers=[_main_container(image=f"{ECR_REGISTRY}/ogg-postgresql:wrong-tag")]))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("image=" in r for r in result["reasons"]))

    def test_12_unexpected_init_container_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(init_containers=[_init_container(), {"name": "extra-init", "image": "busybox"}]))
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
        cluster.put("service", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _service_obj(DEPLOYMENT_ID, ports=[{"name": "https", "port": 8443, "targetPort": "https", "protocol": "TCP"}]))
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


class RuntimeAcceptanceStorageWiringTests(unittest.TestCase):
    """Phase B3A closeout Bug 1: HEALTHY must prove the pod actually CONSUMES the correct storage/secret volumes, not merely that the EFS/CSI/SPC objects exist independently."""

    def test_u02_volume_missing_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(volumes=_pod_volumes(omit_u02=True)))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("missing expected volume 'u02'" in r for r in result["reasons"]))

    def test_u02_points_at_wrong_pvc_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(volumes=_pod_volumes(u02_claim_name="some-other-claim")))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("volume 'u02' claimName=" in r for r in result["reasons"]))

    def test_u03_missing_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(volumes=_pod_volumes(omit_u03=True)))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("missing expected volume 'u03'" in r for r in result["reasons"]))

    def test_u03_not_empty_dir_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(volumes=_pod_volumes(u03_empty_dir=False)))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("volume 'u03' is not emptyDir" in r for r in result["reasons"]))

    def test_admin_csi_volume_missing_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(volumes=_pod_volumes(include_admin_csi=False)))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("missing expected volume 'ogg-admin-csi'" in r for r in result["reasons"]))

    def test_admin_csi_wrong_driver_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(volumes=_pod_volumes(admin_driver="some-other-driver")))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("volume 'ogg-admin-csi' csi.driver=" in r for r in result["reasons"]))

    def test_admin_csi_wrong_secret_provider_class_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(volumes=_pod_volumes(admin_spc="some-other-spc")))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("volume 'ogg-admin-csi' csi.volumeAttributes.secretProviderClass=" in r for r in result["reasons"]))

    def test_certificate_csi_volume_missing_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(volumes=_pod_volumes(include_certificate_csi=False)))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("missing expected volume 'ogg-nginx-cert-csi'" in r for r in result["reasons"]))

    def test_certificate_csi_wrong_provider_class_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(volumes=_pod_volumes(certificate_spc="some-other-spc")))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("volume 'ogg-nginx-cert-csi' csi.volumeAttributes.secretProviderClass=" in r for r in result["reasons"]))

    def test_main_container_does_not_mount_u02_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(containers=[_main_container(mounts=_main_container_mounts(include_u02=False))]))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("does not mount volume 'u02'" in r for r in result["reasons"]))

    def test_main_container_mounts_u02_at_wrong_path_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(containers=[_main_container(mounts=_main_container_mounts(u02_path="/wrong-path"))]))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("mounts 'u02' at" in r for r in result["reasons"]))

    def test_main_container_does_not_mount_u03_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(containers=[_main_container(mounts=_main_container_mounts(include_u03=False))]))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("does not mount volume 'u03'" in r for r in result["reasons"]))

    def test_admin_csi_mount_missing_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(containers=[_main_container(mounts=_main_container_mounts(include_admin_csi=False))]))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("does not mount volume 'ogg-admin-csi'" in r for r in result["reasons"]))

    def test_admin_csi_mount_wrong_path_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(containers=[_main_container(mounts=_main_container_mounts(admin_path="/wrong"))]))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("mounts 'ogg-admin-csi' at" in r for r in result["reasons"]))

    def test_admin_csi_mount_not_read_only_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(containers=[_main_container(mounts=_main_container_mounts(admin_read_only=False))]))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("mount 'ogg-admin-csi' readOnly=" in r for r in result["reasons"]))

    def test_certificate_csi_mount_missing_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(containers=[_main_container(mounts=_main_container_mounts(include_certificate_csi=False))]))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("does not mount volume 'ogg-nginx-cert-csi'" in r for r in result["reasons"]))

    def test_certificate_csi_mount_wrong_path_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(containers=[_main_container(mounts=_main_container_mounts(certificate_path="/wrong"))]))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("mounts 'ogg-nginx-cert-csi' at" in r for r in result["reasons"]))

    def test_certificate_csi_mount_not_read_only_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(containers=[_main_container(mounts=_main_container_mounts(certificate_read_only=False))]))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("mount 'ogg-nginx-cert-csi' readOnly=" in r for r in result["reasons"]))

    def test_init_container_missing_u02_u03_mounts_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(init_containers=[_init_container(mounts=_init_container_mounts(include_u02=False, include_u03=False))]))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("initContainer 'prepare-u02-permissions' does not mount volume 'u02'" in r for r in result["reasons"]))
        self.assertTrue(any("initContainer 'prepare-u02-permissions' does not mount volume 'u03'" in r for r in result["reasons"]))

    def test_init_admin_mount_wrong_path_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(init_containers=[_init_container(mounts=_init_container_mounts(admin_path="/wrong"))]))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("initContainer 'prepare-u02-permissions' mounts 'ogg-admin-csi' at" in r for r in result["reasons"]))

    def test_init_container_never_requires_certificate_csi_mount(self):
        # The real chart never mounts the certificate secret on the init container -- proving the healthy fixture's init container (which omits it) is itself HEALTHY confirms this isn't accidentally required.
        cluster = _populate_healthy_cluster(FakeCluster())
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_HEALTHY)
        init_mounts = {m["name"] for m in _init_container_mounts()}
        self.assertNotIn("ogg-nginx-cert-csi", init_mounts)

    def test_unexpected_pod_volume_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        extra = [{"name": "mystery-hostpath", "hostPath": {"path": "/etc"}}]
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(volumes=_pod_volumes(extra_volumes=extra)))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("unexpected volume(s)" in r and "mystery-hostpath" in r for r in result["reasons"]))

    def test_statefulset_service_name_wrong_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(service_name="wrong-headless-name"))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("spec.serviceName=" in r for r in result["reasons"]))

    def test_statefulset_selector_wrong_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(match_labels={"app.kubernetes.io/name": "goldengate", "app.kubernetes.io/instance": "some-other-instance"}))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("spec.selector.matchLabels=" in r for r in result["reasons"]))

    def test_main_container_port_name_wrong_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        wrong_ports = [{"name": "wrong-name", "containerPort": 8443, "protocol": "TCP"}, {"name": "dist", "containerPort": 9013, "protocol": "TCP"}, {"name": "metrics", "containerPort": 9015, "protocol": "TCP"}]
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(containers=[_main_container(ports=wrong_ports)]))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("container ports=" in r for r in result["reasons"]))

    def test_container_port_numeric_value_wrong_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        wrong_ports = [{"name": "https", "containerPort": 9999, "protocol": "TCP"}, {"name": "dist", "containerPort": 9013, "protocol": "TCP"}, {"name": "metrics", "containerPort": 9015, "protocol": "TCP"}]
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(containers=[_main_container(ports=wrong_ports)]))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("container port 'https' containerPort=" in r for r in result["reasons"]))


class RuntimeAcceptanceServiceRoutingTests(unittest.TestCase):
    """Phase B3A closeout Bug 2: Service targetPort/protocol must exactly match the canonical named-port contract, not merely name/port."""

    def test_main_service_target_port_wrong_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("service", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _service_obj(DEPLOYMENT_ID, ports=_service_ports(override={"https": {"targetPort": "definitely-wrong"}})))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any(f"service/{DEPLOYMENT_ID} port 'https' targetPort=" in r for r in result["reasons"]))

    def test_main_service_protocol_udp_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("service", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _service_obj(DEPLOYMENT_ID, ports=_service_ports(override={"https": {"protocol": "UDP"}})))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any(f"service/{DEPLOYMENT_ID} port 'https' protocol=" in r for r in result["reasons"]))

    def test_headless_service_target_port_wrong_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("service", f"{DEPLOYMENT_ID}-headless", RUNTIME_NAMESPACE, _service_obj(f"{DEPLOYMENT_ID}-headless", cluster_ip="None", ports=_service_ports(override={"dist": {"targetPort": "definitely-wrong"}})))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any(f"service/{DEPLOYMENT_ID}-headless port 'dist' targetPort=" in r for r in result["reasons"]))

    def test_headless_service_protocol_wrong_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("service", f"{DEPLOYMENT_ID}-headless", RUNTIME_NAMESPACE, _service_obj(f"{DEPLOYMENT_ID}-headless", cluster_ip="None", ports=_service_ports(override={"metrics": {"protocol": "UDP"}})))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any(f"service/{DEPLOYMENT_ID}-headless port 'metrics' protocol=" in r for r in result["reasons"]))

    def test_source_service_missing_dist_when_canonical_dist_exists_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("service", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _service_obj(DEPLOYMENT_ID, ports=_service_ports(names=("https", "metrics"))))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any(f"service/{DEPLOYMENT_ID} ports=" in r and "'dist'" in r for r in result["reasons"]))

    def test_source_service_unexpected_receiver_port_is_broken(self):
        # gg-postgresql-repltest-01 is a SOURCE runtime -- its canonical servicePorts has receiver=None, so a Service that adds a receiver port anyway is a contract mismatch, never silently accepted.
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("service", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _service_obj(DEPLOYMENT_ID, ports=_service_ports(names=("https", "dist", "receiver", "metrics"))))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any(f"service/{DEPLOYMENT_ID} ports=" in r and "'receiver'" in r for r in result["reasons"]))


class RuntimeAcceptanceReproductionProofTests(unittest.TestCase):
    """Explicit reproduction proofs for the two Phase B3A closeout bugs -- both must now classify BROKEN, never HEALTHY."""

    def test_proof_a_no_volumes_or_mounts_is_broken_not_healthy(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _sts_obj(
            volumes=[],
            containers=[_main_container(mounts=[])],
            init_containers=[_init_container(mounts=[])],
        ))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertNotEqual(result["state"], runtime_acceptance.STATE_HEALTHY)
        self.assertTrue(any("missing expected volume 'u02'" in r for r in result["reasons"]))
        self.assertTrue(any("does not mount volume 'u02'" in r for r in result["reasons"]))

    def test_proof_b_wrong_target_port_and_udp_protocol_is_broken_not_healthy(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        broken_ports = [{"name": n, "port": DEFAULT_SERVICE_PORT_VALUES[n], "targetPort": "definitely-wrong", "protocol": "UDP"} for n in ("https", "dist", "metrics")]
        cluster.put("service", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _service_obj(DEPLOYMENT_ID, ports=broken_ports))
        cluster.put("service", f"{DEPLOYMENT_ID}-headless", RUNTIME_NAMESPACE, _service_obj(f"{DEPLOYMENT_ID}-headless", cluster_ip="None", ports=broken_ports))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertNotEqual(result["state"], runtime_acceptance.STATE_HEALTHY)
        self.assertTrue(any("targetPort=" in r for r in result["reasons"]))
        self.assertTrue(any("protocol=" in r for r in result["reasons"]))


_SYNTHETIC_ENVIRONMENT_YAML = """\
schemaVersion: 1
environment: dev
aws:
  region: eu-west-1
  workloadAccountId: "668311715351"
  buildAccountId: "229410149234"
eks:
  clusterName: gg-scratch-test
  oidcIssuer: "https://oidc.eks.eu-west-1.amazonaws.com/id/0123456789ABCDEF0123456789ABCDEF"
namespaces:
  runtime: goldengate-dev
  monitoring: goldengate-monitoring
  argocd: argocd
  observability: amazon-cloudwatch
network:
  dnsDomain: goldengate-dev.adcbmis.local
  albGroupName: gg-scratch-test-alb
  certificateArn: arn:aws:acm:eu-west-1:668311715351:certificate/00000000-0000-0000-0000-000000000000
iam:
  roles:
    eksDeploy: GoldenGateEKSDeployRole-dev
    runtime: GoldenGateSecretsReadRole-dev
    monitor: GoldenGateMonitorReadRole-dev
    argocdEcrRead: GoldenGateArgocdECRRead-dev
    platformLogging: GoldenGatePlatformLoggingRole-dev
    cloudwatchMetrics: GoldenGateCloudWatchMetricsRole-dev
  runnerRoleName: RunnerRole-goldengate-eks-app_dev
  ecrSyncRoleArn: arn:aws:iam::229410149234:role/scratch-test-ecr-sync-role
kms:
  monitorDynamoDbKeyArn: arn:aws:kms:eu-west-1:668311715351:key/00000000-0000-0000-0000-000000000000
efs:
  sharedSecurityGroupDescription: "Security group for EFS filesystem - scratch test"
tags:
  applicationName: CloudFactory
  businessCriticality: Low
  businessUnit: TechnologyPlatform
  businessUnitOwner: scratch-test-owner
  costCenter: "000"
  mapMigrated: scratch-test
  requestReference: SCRATCH-TEST
  dataClassification: General
"""

EXTERNAL_CLAIM_DEPLOYMENT_ID = "gg-existingclaim-01"
EXTERNAL_CLAIM_NAME = "external-claim-01"
EXTERNAL_CLAIM_APP_NAME = f"goldengate-dev-existingclaim-01"
EXTERNAL_CLAIM_IMAGE = f"{ECR_REGISTRY}/ogg-oracle:1.0.0"
EXTERNAL_CLAIM_CONTAINER_NAME = "ogg-oracle"
EXTERNAL_CLAIM_SELECTOR = {"app.kubernetes.io/name": "goldengate", "app.kubernetes.io/instance": EXTERNAL_CLAIM_DEPLOYMENT_ID}


class RuntimeAcceptanceExternalClaimTests(unittest.TestCase):
    """External/existing-claim regression: this repository intentionally preserves runtime.storage.u02.type=existingClaim as a supported shape distinct from the chart-owned managed-EFS PVC the current real DEV descriptors use. Uses an isolated scratch environment (never modifies the real envs/dev descriptors)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._original_repo_root = runtime_acceptance.REPO_ROOT
        runtime_acceptance.REPO_ROOT = self._tmp.name
        env_dir = os.path.join(self._tmp.name, "envs", "dev")
        os.makedirs(env_dir, exist_ok=True)
        with open(os.path.join(env_dir, "environment.yaml"), "w") as f:
            f.write(_SYNTHETIC_ENVIRONMENT_YAML)

        doc = {
            "deployment": {"enabled": True, "pipeline": "existing-claim-pipeline", "role": "target"},
            "deploymentModel": "singleRuntime",
            "runtime": {
                "deploymentType": "oracle",
                "containerName": EXTERNAL_CLAIM_CONTAINER_NAME,
                "image": {"repositoryName": "ogg-oracle", "tag": "1.0.0"},
                "csi": {"enabled": True, "admin": {"enabled": True, "mountPath": ADMIN_MOUNT_PATH}, "certificate": {"enabled": True, "mountPath": CERTIFICATE_MOUNT_PATH}},
                "storage": {"u02": {"type": "existingClaim", "existingClaim": EXTERNAL_CLAIM_NAME}},
                "initPermissions": {"enabled": True},
                "service": {"type": "ClusterIP", "ports": {"https": 8443, "dist": None, "receiver": 9014, "metrics": 9015}},
            },
            "ingress": {"enabled": False},
        }
        deployment_dir = os.path.join(env_dir, EXTERNAL_CLAIM_DEPLOYMENT_ID)
        os.makedirs(deployment_dir, exist_ok=True)
        with open(os.path.join(deployment_dir, "values.yaml"), "w") as f:
            yaml.safe_dump(doc, f)

    def tearDown(self):
        runtime_acceptance.REPO_ROOT = self._original_repo_root
        self._tmp.cleanup()

    def _classify_external(self, cluster, **overrides):
        kwargs = dict(
            environment=ENVIRONMENT,
            deployment_id=EXTERNAL_CLAIM_DEPLOYMENT_ID,
            argocd_namespace=ARGOCD_NAMESPACE,
            runtime_namespace=RUNTIME_NAMESPACE,
            ecr_registry=ECR_REGISTRY,
            dns_domain=DNS_DOMAIN,
            alb_group_name=ALB_GROUP_NAME,
            acm_certificate_arn=ACM_CERTIFICATE_ARN,
            aws_region=AWS_REGION,
        )
        kwargs.update(overrides)
        return runtime_acceptance.classify(cluster, **kwargs)

    def _populate(self, cluster, claim_name=EXTERNAL_CLAIM_NAME):
        app_name = EXTERNAL_CLAIM_APP_NAME
        cluster.put("application", app_name, ARGOCD_NAMESPACE, {
            "metadata": {"labels": {"goldengate.adcb/environment": ENVIRONMENT, "goldengate.adcb/deployment-id": EXTERNAL_CLAIM_DEPLOYMENT_ID}},
            "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}},
            "spec": {
                "source": {"repoURL": f"oci://{ECR_REGISTRY}/{runtime_acceptance.HELM_REPO_PATH}", "helm": {"releaseName": EXTERNAL_CLAIM_DEPLOYMENT_ID}},
                "destination": {"namespace": RUNTIME_NAMESPACE},
            },
        })
        sts = {
            "metadata": {"name": EXTERNAL_CLAIM_DEPLOYMENT_ID, "generation": 1},
            "spec": {
                "serviceName": f"{EXTERNAL_CLAIM_DEPLOYMENT_ID}-headless",
                "selector": {"matchLabels": EXTERNAL_CLAIM_SELECTOR},
                "template": {
                    "metadata": {"labels": EXTERNAL_CLAIM_SELECTOR},
                    "spec": {
                        "serviceAccountName": SA_NAME,
                        "containers": [{
                            "name": EXTERNAL_CLAIM_CONTAINER_NAME,
                            "image": EXTERNAL_CLAIM_IMAGE,
                            "volumeMounts": [
                                {"name": "u02", "mountPath": "/u02"},
                                {"name": "u03", "mountPath": "/u03"},
                                {"name": "ogg-admin-csi", "mountPath": ADMIN_MOUNT_PATH, "readOnly": True},
                                {"name": "ogg-nginx-cert-csi", "mountPath": CERTIFICATE_MOUNT_PATH, "readOnly": True},
                            ],
                            "ports": [{"name": "https", "containerPort": 8443, "protocol": "TCP"}, {"name": "receiver", "containerPort": 9014, "protocol": "TCP"}, {"name": "metrics", "containerPort": 9015, "protocol": "TCP"}],
                        }],
                        "initContainers": [{
                            "name": "prepare-u02-permissions",
                            "image": EXTERNAL_CLAIM_IMAGE,
                            "volumeMounts": [
                                {"name": "u02", "mountPath": "/u02"},
                                {"name": "u03", "mountPath": "/u03"},
                                {"name": "ogg-admin-csi", "mountPath": ADMIN_MOUNT_PATH, "readOnly": True},
                            ],
                        }],
                        "volumes": [
                            {"name": "u02", "persistentVolumeClaim": {"claimName": claim_name}},
                            {"name": "u03", "emptyDir": {}},
                            {"name": "ogg-admin-csi", "csi": {"driver": "secrets-store.csi.k8s.io", "readOnly": True, "volumeAttributes": {"secretProviderClass": f"{EXTERNAL_CLAIM_DEPLOYMENT_ID}-admin"}}},
                            {"name": "ogg-nginx-cert-csi", "csi": {"driver": "secrets-store.csi.k8s.io", "readOnly": True, "volumeAttributes": {"secretProviderClass": f"{EXTERNAL_CLAIM_DEPLOYMENT_ID}-certificate"}}},
                        ],
                    },
                },
            },
            "status": {"observedGeneration": 1, "readyReplicas": 1, "currentReplicas": 1, "updatedReplicas": 1, "currentRevision": "rev-1", "updateRevision": "rev-1"},
        }
        cluster.put("statefulset", EXTERNAL_CLAIM_DEPLOYMENT_ID, RUNTIME_NAMESPACE, sts)
        cluster.put("secretproviderclass", f"{EXTERNAL_CLAIM_DEPLOYMENT_ID}-admin", RUNTIME_NAMESPACE, _spc_obj("dev/goldengate/target/admin"))
        cluster.put("secretproviderclass", f"{EXTERNAL_CLAIM_DEPLOYMENT_ID}-certificate", RUNTIME_NAMESPACE, _spc_obj(TLS_SECRET_OBJECT_NAME))
        cluster.put("secret", f"{EXTERNAL_CLAIM_DEPLOYMENT_ID}-admin", RUNTIME_NAMESPACE, _secret_obj())
        main_ports = [{"name": "https", "port": 8443, "targetPort": "https", "protocol": "TCP"}, {"name": "receiver", "port": 9014, "targetPort": "receiver", "protocol": "TCP"}, {"name": "metrics", "port": 9015, "targetPort": "metrics", "protocol": "TCP"}]
        cluster.put("service", EXTERNAL_CLAIM_DEPLOYMENT_ID, RUNTIME_NAMESPACE, {"metadata": {"name": EXTERNAL_CLAIM_DEPLOYMENT_ID}, "spec": {"type": "ClusterIP", "selector": EXTERNAL_CLAIM_SELECTOR, "ports": main_ports}})
        cluster.put("service", f"{EXTERNAL_CLAIM_DEPLOYMENT_ID}-headless", RUNTIME_NAMESPACE, {"metadata": {"name": f"{EXTERNAL_CLAIM_DEPLOYMENT_ID}-headless"}, "spec": {"clusterIP": "None", "selector": EXTERNAL_CLAIM_SELECTOR, "ports": main_ports}})
        cluster.put_list("endpointslices.discovery.k8s.io", RUNTIME_NAMESPACE, [_endpointslice_obj()])
        return cluster

    def test_explicit_existing_claim_is_mounted_and_healthy(self):
        cluster = self._populate(FakeCluster())
        result = self._classify_external(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_HEALTHY, result["reasons"])
        self.assertEqual(result["reasons"], [])

    def test_acceptance_does_not_demand_a_generated_pvc_name(self):
        # persistence.efs is never configured for this synthetic descriptor (existingClaim needs no persistence.efs block at all) -- _check_storage must skip entirely (efsMode is None), never demanding a StorageClass/PV or a generated "<deployment-id>-u02" PVC.
        cluster = self._populate(FakeCluster())
        result = self._classify_external(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_HEALTHY)
        self.assertTrue(all(f"{EXTERNAL_CLAIM_DEPLOYMENT_ID}-u02" not in r for r in result["reasons"]))

    def test_wrong_claim_name_is_broken(self):
        cluster = self._populate(FakeCluster(), claim_name="some-unrelated-claim")
        result = self._classify_external(cluster)
        self.assertEqual(result["state"], runtime_acceptance.STATE_BROKEN)
        self.assertTrue(any("volume 'u02' claimName=" in r for r in result["reasons"]))


class RuntimeAcceptanceNoMutationSourceSweepTests(unittest.TestCase):
    """Static source-safety proof: the classifier module (and its shared k8s_common helper) must never construct a mutating kubectl/helm command, and must never call AWS directly."""

    FORBIDDEN_SUBSTRINGS = (
        "kubectl apply", "kubectl create", "kubectl delete", "kubectl patch",
        "kubectl annotate", "kubectl label",
        "helm install", "helm upgrade", "helm uninstall",
        "boto3", "import subprocess",
    )

    def test_source_contains_no_mutating_or_aws_command(self):
        k8s_common_path = os.path.join(REPO_ROOT, "automation", "orchestration", "k8s_common.py")
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
