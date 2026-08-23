"""Offline tests for hack/orchestration/monitor_acceptance.py; run directly via `python3 hack/test-goldengate-monitor-acceptance.py`. No live Kubernetes -- every kubectl response is a fake, injected fixture. The canonical monitor values (helm/goldengate-monitor/values.yaml merged with envs/dev/goldengate-monitor/values.yaml) are the real, currently-committed files -- this tool never re-implements that merge as a second schema. Current intentional architecture change: the real dev files now have ingress.enabled=true (networkPolicy.enabled remains false), so _populate_healthy_cluster()'s "fully healthy" baseline fixture includes a matching Ingress object by default. MonitorAcceptanceIngressAndNetworkPolicyEnabledTests mostly monkeypatches _load_monitor_values to an explicit, self-contained values shape instead of relying on today's real committed file, so those specific tests stay correct and meaningful regardless of future ingress.enabled/networkPolicy.enabled flips. Exercises the classifier's actual logic (never merely greps its source)."""
from __future__ import annotations

import importlib.util
import json
import os
import unittest
from unittest import mock

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL_PATH = os.path.join(REPO_ROOT, "hack", "orchestration", "monitor_acceptance.py")


def _load_tool():
    spec = importlib.util.spec_from_file_location("monitor_acceptance", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


monitor_acceptance = _load_tool()

ENVIRONMENT = "dev"
ARGOCD_NAMESPACE = "argocd"
MONITOR_NAMESPACE = "goldengate-monitoring"
ECR_REGISTRY = "229410149234.dkr.ecr.eu-west-1.amazonaws.com"
AWS_REGION = "eu-west-1"
DNS_DOMAIN = "goldengate-dev.adcbmis.local"
ALB_GROUP_NAME = "gg-poc-dev-alb"
ACM_CERTIFICATE_ARN = "arn:aws:acm:eu-west-1:668311715351:certificate/9e53e28e-3243-47fc-85a1-50f9a94acde7"
MONITOR_ROLE_ARN = "arn:aws:iam::668311715351:role/GoldenGateMonitorReadRole-dev"
MONITOR_HOST = f"monitor.{DNS_DOMAIN}"

EXPECTED_IMAGE_REPOSITORY = f"{ECR_REGISTRY}/gg-monitor"
EXPECTED_IMAGE_TAG = "1.2.3"
EXPECTED_CHART_VERSION = "0.42.1"
EXPECTED_CLOUDWATCH_PUBLISH_ENABLED = True

SERVICE_PORT = 8080
DYNAMODB_TABLE = "gg-eks-pipeline"
CANONICAL_CONFIG_ROOT = "/etc/gg-canonical"
STALE_AFTER_SECONDS = 120
REFRESH_SECONDS = 30
DEPLOY_UID = "deploy-uid-1"
RS_UID = "rs-uid-1"
RS_NAME = "gg-monitor-rs1"

REGISTRY = {
    "environment": ENVIRONMENT,
    "runtimeNamespace": "goldengate-dev",
    "monitoringNamespace": MONITOR_NAMESPACE,
    "dnsDomain": DNS_DOMAIN,
    "tlsSecret": "dev/goldengate/tls-certificate",
    "deployments": [
        {"name": "gg-postgresql-repltest-01", "type": "postgresql", "pipeline": "repltest-01", "role": "source", "enabled": True, "adminSecret": "dev/goldengate/source/admin"},
        {"name": "gg-mssql-repltest-01", "type": "mssql", "pipeline": "repltest-01", "role": "target", "enabled": True, "adminSecret": "dev/goldengate/target/admin"},
    ],
}


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


def _app_obj(healthy=True, dest_ns=MONITOR_NAMESPACE, repo_url=None, target_revision=EXPECTED_CHART_VERSION, release_name="gg-monitor", param_overrides=None):
    params = {
        "image.repository": EXPECTED_IMAGE_REPOSITORY,
        "image.tag": EXPECTED_IMAGE_TAG,
        "cloudwatch.publishEnabled": "true" if EXPECTED_CLOUDWATCH_PUBLISH_ENABLED else "false",
        "global.environment": ENVIRONMENT,
        "namespace.name": MONITOR_NAMESPACE,
        "aws.region": AWS_REGION,
        "serviceAccount.roleArn": MONITOR_ROLE_ARN,
        "ingress.host": MONITOR_HOST,
        "ingress.alb.groupName": ALB_GROUP_NAME,
        "ingress.alb.certificateArn": ACM_CERTIFICATE_ARN,
    }
    if param_overrides:
        params.update(param_overrides)
    return {
        "status": {"sync": {"status": "Synced" if healthy else "OutOfSync"}, "health": {"status": "Healthy" if healthy else "Degraded"}},
        "spec": {
            "source": {
                "repoURL": repo_url if repo_url is not None else f"oci://{ECR_REGISTRY}/{monitor_acceptance.HELM_REPO_PATH}",
                "targetRevision": target_revision,
                "helm": {"releaseName": release_name, "parameters": [{"name": k, "value": v} for k, v in params.items()]},
            },
            "destination": {"namespace": dest_ns},
        },
    }


def _namespace_obj(env=ENVIRONMENT, name_label="gg-monitor", managed_by_label="argocd"):
    return {"metadata": {"labels": {"app.kubernetes.io/name": name_label, "app.kubernetes.io/managed-by": managed_by_label, "goldengate.adcb/environment": env}}}


def _serviceaccount_obj(role_arn=MONITOR_ROLE_ARN):
    return {"metadata": {"annotations": {"eks.amazonaws.com/role-arn": role_arn}}}


def _container(image=None, port=SERVICE_PORT, env_overrides=None, mounts=None, name="gg-monitor",
                startup_path="/healthz", liveness_path="/healthz", readiness_path="/readyz"):
    env = {
        "AWS_REGION": AWS_REGION,
        "AWS_DEFAULT_REGION": AWS_REGION,
        "DYNAMODB_TABLE": DYNAMODB_TABLE,
        "REPO_CONFIG_ROOT": CANONICAL_CONFIG_ROOT,
        "PORT": str(SERVICE_PORT),
        "STALE_AFTER_SECONDS": str(STALE_AFTER_SECONDS),
        "REFRESH_SECONDS": str(REFRESH_SECONDS),
        "CLOUDWATCH_PUBLISH_ENABLED": "true" if EXPECTED_CLOUDWATCH_PUBLISH_ENABLED else "false",
    }
    if env_overrides:
        for k, v in env_overrides.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v

    default_mounts = [
        {"name": "tmp", "mountPath": "/tmp"},
        {"name": "canonical-config", "mountPath": CANONICAL_CONFIG_ROOT, "readOnly": True},
        {"name": "secrets-store", "mountPath": "/mnt/secrets-store", "readOnly": True},
    ]
    return {
        "name": name,
        "image": image if image is not None else f"{EXPECTED_IMAGE_REPOSITORY}:{EXPECTED_IMAGE_TAG}",
        "ports": [{"name": "http", "containerPort": port, "protocol": "TCP"}],
        "startupProbe": {"httpGet": {"path": startup_path, "port": "http"}},
        "livenessProbe": {"httpGet": {"path": liveness_path, "port": "http"}},
        "readinessProbe": {"httpGet": {"path": readiness_path, "port": "http"}},
        "env": [{"name": k, "value": v} for k, v in env.items()],
        "volumeMounts": mounts if mounts is not None else default_mounts,
    }


def _pod_volumes(extra=None, omit=None):
    volumes = [
        {"name": "tmp", "emptyDir": {}},
        {"name": "canonical-config", "configMap": {"name": "goldengate-monitor-canonical-config", "items": [{"key": "goldengate-deployments.yaml", "path": "goldengate-deployments.yaml"}]}},
        {"name": "secrets-store", "csi": {"driver": "secrets-store.csi.k8s.io", "readOnly": True, "volumeAttributes": {"secretProviderClass": "gg-monitor-secrets"}}},
    ]
    if omit:
        volumes = [v for v in volumes if v["name"] not in omit]
    if extra:
        volumes.extend(extra)
    return volumes


def _deployment_obj(ready=True, containers=None, init_containers=None, volumes=None, service_account_name="gg-monitor", host_network=False):
    status = {"observedGeneration": 1}
    if ready:
        status.update({"updatedReplicas": 1, "readyReplicas": 1, "availableReplicas": 1})
    else:
        status.update({"updatedReplicas": 0, "readyReplicas": 0, "availableReplicas": 0})
    return {
        "metadata": {"generation": 1, "uid": DEPLOY_UID},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": dict(monitor_acceptance._SELECTOR_LABELS)},
            "template": {
                "spec": {
                    "serviceAccountName": service_account_name,
                    "hostNetwork": host_network,
                    "hostPID": False,
                    "hostIPC": False,
                    "containers": containers if containers is not None else [_container()],
                    "initContainers": init_containers or [],
                    "volumes": volumes if volumes is not None else _pod_volumes(),
                },
            },
        },
        "status": status,
    }


def _configmap_obj(registry=None):
    return {"data": {"goldengate-deployments.yaml": yaml.safe_dump(registry if registry is not None else REGISTRY)}}


def _spc_objects(registry=None):
    doc = registry if registry is not None else REGISTRY
    groups = {}
    for d in doc["deployments"]:
        if d.get("enabled"):
            groups.setdefault(d["adminSecret"], []).append(d["name"])
    objects = []
    for admin_secret in sorted(groups):
        jmes = []
        for deployment_name in groups[admin_secret]:
            jmes.append({"path": "OGG_ADMIN", "objectAlias": f"{deployment_name}-admin-user"})
            jmes.append({"path": "OGG_ADMIN_PWD", "objectAlias": f"{deployment_name}-admin-password"})
        objects.append({"objectName": admin_secret, "objectType": "secretsmanager", "jmesPath": jmes})
    objects.append({"objectName": doc["tlsSecret"], "objectType": "secretsmanager", "jmesPath": [{"path": '"ca-chain.pem"', "objectAlias": "ca-chain-pem"}]})
    return objects


def _secretproviderclass_obj(region=AWS_REGION, registry=None, objects=None):
    return {"spec": {"provider": "aws", "parameters": {"region": region, "objects": yaml.safe_dump(objects if objects is not None else _spc_objects(registry))}}}


def _service_obj(selector=None, port=SERVICE_PORT, target_port="http", svc_type="ClusterIP"):
    return {
        "spec": {
            "type": svc_type,
            "selector": selector if selector is not None else dict(monitor_acceptance._SELECTOR_LABELS),
            "ports": [{"name": "http", "port": port, "targetPort": target_port, "protocol": "TCP"}],
        },
    }


def _ready_endpointslice():
    return [{"endpoints": [{"conditions": {"ready": True}}]}]


_UNSET = object()


def _replicaset_and_pod(rs_name=RS_NAME, rs_uid=RS_UID, deploy_owner_name="gg-monitor", deploy_owner_uid=DEPLOY_UID,
                         pod_rs_owner_name=_UNSET, pod_rs_owner_uid=_UNSET):
    """A correct, full Pod->ReplicaSet->Deployment UID/name ownership chain by default -- pass any of the pod_rs_owner_*/deploy_owner_* kwargs to deliberately corrupt one link for a specific test. Pass pod_rs_owner_name/pod_rs_owner_uid explicitly as None (distinct from the _UNSET default) to omit that key from the pod's ownerReference entirely."""
    rs_obj = {"metadata": {"name": rs_name, "uid": rs_uid, "ownerReferences": [{"controller": True, "kind": "Deployment", "name": deploy_owner_name, "uid": deploy_owner_uid}]}}
    pod_owner_ref = {"controller": True, "kind": "ReplicaSet",
                      "name": rs_name if pod_rs_owner_name is _UNSET else pod_rs_owner_name,
                      "uid": rs_uid if pod_rs_owner_uid is _UNSET else pod_rs_owner_uid}
    if pod_owner_ref["uid"] is None:
        pod_owner_ref.pop("uid")
    if pod_owner_ref["name"] is None:
        pod_owner_ref.pop("name")
    pod_obj = {
        "metadata": {"name": "gg-monitor-rs1-abcde", "ownerReferences": [pod_owner_ref]},
        "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
        "spec": {"serviceAccountName": "gg-monitor"},
    }
    return rs_obj, pod_obj


def _ingress_obj(host=MONITOR_HOST, class_name="alb", group_order="120"):
    return {
        "spec": {
            "ingressClassName": class_name,
            "rules": [{"host": host, "http": {"paths": [{"backend": {"service": {"name": "gg-monitor", "port": {"name": "http"}}}}]}}],
        },
        "metadata": {"annotations": {
            "alb.ingress.kubernetes.io/group.name": ALB_GROUP_NAME,
            "alb.ingress.kubernetes.io/certificate-arn": ACM_CERTIFICATE_ARN,
            "alb.ingress.kubernetes.io/target-type": "ip",
            "alb.ingress.kubernetes.io/backend-protocol": "HTTP",
            "alb.ingress.kubernetes.io/healthcheck-protocol": "HTTP",
            "alb.ingress.kubernetes.io/healthcheck-path": "/healthz",
            "alb.ingress.kubernetes.io/group.order": group_order,
        }},
    }


def _populate_healthy_cluster(cluster, registry=None, app_kwargs=None, deployment_kwargs=None):
    reg = registry if registry is not None else REGISTRY
    cluster.put("application", monitor_acceptance.ARGOCD_APP_NAME, ARGOCD_NAMESPACE, _app_obj(**(app_kwargs or {})))
    cluster.put("namespace", MONITOR_NAMESPACE, None, _namespace_obj())
    cluster.put("serviceaccount", "gg-monitor", MONITOR_NAMESPACE, _serviceaccount_obj())
    cluster.put("deployment", "gg-monitor", MONITOR_NAMESPACE, _deployment_obj(**(deployment_kwargs or {})))
    cluster.put("configmap", "goldengate-monitor-canonical-config", MONITOR_NAMESPACE, _configmap_obj(reg))
    cluster.put("secretproviderclass", "gg-monitor-secrets", MONITOR_NAMESPACE, _secretproviderclass_obj(registry=reg))
    cluster.put("service", "gg-monitor", MONITOR_NAMESPACE, _service_obj())
    cluster.put_list("endpointslices.discovery.k8s.io", MONITOR_NAMESPACE, _ready_endpointslice())
    rs_obj, pod_obj = _replicaset_and_pod()
    cluster.put("replicaset", "gg-monitor-rs1", MONITOR_NAMESPACE, rs_obj)
    cluster.put_list("pods", MONITOR_NAMESPACE, [pod_obj])
    # Current intentional architecture change: the REAL, unpatched envs/dev/goldengate-monitor/values.yaml now commits ingress.enabled=true -- every test in this module that uses the real (unpatched) _load_monitor_values must therefore also see a correctly-shaped Ingress object in an otherwise-"fully healthy" fixture, or the classifier correctly reports BROKEN (ingress/gg-monitor does not exist). networkPolicy.enabled remains false in the real committed values (unchanged), so no matching NetworkPolicy object is added here.
    cluster.put("ingress", monitor_acceptance.INGRESS_NAME, MONITOR_NAMESPACE, _ingress_obj())
    return cluster


def _classify(cluster, healthz_status=None, readyz_status=None, registry=None):
    return monitor_acceptance.classify(
        cluster,
        environment=ENVIRONMENT,
        argocd_namespace=ARGOCD_NAMESPACE,
        monitor_namespace=MONITOR_NAMESPACE,
        ecr_registry=ECR_REGISTRY,
        aws_region=AWS_REGION,
        dns_domain=DNS_DOMAIN,
        alb_group_name=ALB_GROUP_NAME,
        acm_certificate_arn=ACM_CERTIFICATE_ARN,
        monitor_role_arn=MONITOR_ROLE_ARN,
        expected_image_repository=EXPECTED_IMAGE_REPOSITORY,
        expected_image_tag=EXPECTED_IMAGE_TAG,
        expected_chart_version=EXPECTED_CHART_VERSION,
        expected_cloudwatch_publish_enabled=EXPECTED_CLOUDWATCH_PUBLISH_ENABLED,
        registry=registry if registry is not None else REGISTRY,
        healthz_status=healthz_status,
        readyz_status=readyz_status,
    )


def _assert_broken(test, result, substring):
    test.assertEqual(result["state"], monitor_acceptance.STATE_BROKEN)
    test.assertTrue(any(substring in r for r in result["reasons"]), f"expected a reason containing {substring!r}, got {result['reasons']!r}")


class MonitorAcceptanceClassifierTests(unittest.TestCase):
    # 1. Fully healthy footprint -> HEALTHY, no reasons, ready pod identified.
    def test_1_fully_healthy_is_healthy(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        result = _classify(cluster)
        self.assertEqual(result["state"], monitor_acceptance.STATE_HEALTHY)
        self.assertEqual(result["reasons"], [])
        self.assertEqual(result["checks"]["ready_pod_name"], "gg-monitor-rs1-abcde")

    # 2. Application missing -> BROKEN.
    def test_2_application_missing_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.objects.pop(("application", monitor_acceptance.ARGOCD_APP_NAME, ARGOCD_NAMESPACE))
        result = _classify(cluster)
        _assert_broken(self, result, "does not exist")

    # 3. Application not Synced/Healthy -> BROKEN.
    def test_3_application_not_synced_healthy_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster(), app_kwargs={"healthy": False})
        result = _classify(cluster)
        _assert_broken(self, result, "sync status")

    # 4. Application wrong destination.namespace -> BROKEN.
    def test_4_application_wrong_destination_namespace_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster(), app_kwargs={"dest_ns": "some-other-ns"})
        result = _classify(cluster)
        _assert_broken(self, result, "destination.namespace")

    # 5. Application wrong repoURL -> BROKEN.
    def test_5_application_wrong_repo_url_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster(), app_kwargs={"repo_url": "oci://wrong.example.com/helm/goldengate-monitor"})
        result = _classify(cluster)
        _assert_broken(self, result, "source.repoURL")

    # 6. Application wrong targetRevision (chart version) -> BROKEN.
    def test_6_application_wrong_target_revision_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster(), app_kwargs={"target_revision": "0.99.9"})
        result = _classify(cluster)
        _assert_broken(self, result, "source.targetRevision")

    # 7. Application wrong releaseName -> BROKEN.
    def test_7_application_wrong_release_name_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster(), app_kwargs={"release_name": "some-other-release"})
        result = _classify(cluster)
        _assert_broken(self, result, "source.helm.releaseName")

    # 8. Application wrong/stale image.repository parameter -> BROKEN (Argo health alone must never be trusted).
    def test_8_application_wrong_image_parameter_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster(), app_kwargs={"param_overrides": {"image.repository": "some/other/image"}})
        result = _classify(cluster)
        _assert_broken(self, result, "helm parameter image.repository")

    # 9. Application wrong cloudwatch.publishEnabled parameter -> BROKEN.
    def test_9_application_wrong_cloudwatch_parameter_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster(), app_kwargs={"param_overrides": {"cloudwatch.publishEnabled": "false"}})
        result = _classify(cluster)
        _assert_broken(self, result, "helm parameter cloudwatch.publishEnabled")

    # B3B closeout Issue 5: exact Argo Helm parameter SET (reject extra/duplicate, not merely check the 10 expected values).
    def test_application_unexpected_extra_helm_parameter_is_broken(self):
        # Reproduces the exact original defect: all 10 canonical parameters correct, PLUS one unexpected extra parameter -- must be rejected, never accepted merely because the expected 10 all matched.
        cluster = _populate_healthy_cluster(FakeCluster(), app_kwargs={"param_overrides": {"unexpected.override": "true"}})
        result = _classify(cluster)
        _assert_broken(self, result, "helm parameters contain unexpected name(s)")
        _assert_broken(self, result, "'unexpected.override'")

    def test_application_missing_helm_parameter_is_broken(self):
        app = _app_obj()
        app["spec"]["source"]["helm"]["parameters"] = [
            p for p in app["spec"]["source"]["helm"]["parameters"] if p["name"] != "ingress.alb.certificateArn"
        ]
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("application", monitor_acceptance.ARGOCD_APP_NAME, ARGOCD_NAMESPACE, app)
        result = _classify(cluster)
        _assert_broken(self, result, "helm parameters are missing expected name(s)")
        _assert_broken(self, result, "'ingress.alb.certificateArn'")

    def test_application_duplicate_helm_parameter_name_is_broken(self):
        # A dict-comprehension collapse ({p["name"]: p["value"] for p in parameters}) would silently keep only the LAST occurrence -- this proves the duplicate is detected before any such collapse.
        app = _app_obj()
        app["spec"]["source"]["helm"]["parameters"].append({"name": "image.repository", "value": "some/other/image"})
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("application", monitor_acceptance.ARGOCD_APP_NAME, ARGOCD_NAMESPACE, app)
        result = _classify(cluster)
        _assert_broken(self, result, "helm parameters contain duplicate name(s)")
        _assert_broken(self, result, "'image.repository'")

    def test_application_exact_canonical_parameter_set_is_healthy(self):
        # Positive control: proves the new exact-set validation does not itself introduce a false positive against the existing exact, correct 10-parameter Application.
        cluster = _populate_healthy_cluster(FakeCluster())
        result = _classify(cluster)
        self.assertEqual(result["state"], monitor_acceptance.STATE_HEALTHY, result["reasons"])

    # Final DEPLOY freeze closeout Issue 3: malformed helm parameter rows must never be silently discarded.
    def test_application_canonical_parameters_plus_non_dict_row_is_broken(self):
        app = _app_obj()
        app["spec"]["source"]["helm"]["parameters"].append("not-an-object")
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("application", monitor_acceptance.ARGOCD_APP_NAME, ARGOCD_NAMESPACE, app)
        result = _classify(cluster)
        _assert_broken(self, result, "source.helm.parameters row #10 is not an object")

    def test_application_canonical_parameters_plus_row_without_usable_name_is_broken(self):
        app = _app_obj()
        app["spec"]["source"]["helm"]["parameters"].append({"value": "orphan-value"})
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("application", monitor_acceptance.ARGOCD_APP_NAME, ARGOCD_NAMESPACE, app)
        result = _classify(cluster)
        _assert_broken(self, result, "has a missing/empty/non-string name")

    # 10. Namespace missing -> BROKEN.
    def test_10_namespace_missing_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.objects.pop(("namespace", MONITOR_NAMESPACE, None))
        result = _classify(cluster)
        _assert_broken(self, result, f"namespace/{MONITOR_NAMESPACE} does not exist")

    # 11. ServiceAccount wrong role ARN -> BROKEN.
    def test_11_serviceaccount_wrong_role_arn_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("serviceaccount", "gg-monitor", MONITOR_NAMESPACE, _serviceaccount_obj(role_arn="arn:aws:iam::999999999999:role/wrong"))
        result = _classify(cluster)
        _assert_broken(self, result, "eks.amazonaws.com/role-arn")

    # 12. Deployment missing -> BROKEN.
    def test_12_deployment_missing_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.objects.pop(("deployment", "gg-monitor", MONITOR_NAMESPACE))
        result = _classify(cluster)
        _assert_broken(self, result, "deployment/gg-monitor does not exist")

    # 13. Deployment not ready -> BROKEN.
    def test_13_deployment_not_ready_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster(), deployment_kwargs={"ready": False})
        result = _classify(cluster)
        _assert_broken(self, result, "not ready")

    # 14. Deployment unexpected initContainer -> BROKEN.
    def test_14_deployment_unexpected_init_container_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster(), deployment_kwargs={"init_containers": [{"name": "unexpected-init"}]})
        result = _classify(cluster)
        _assert_broken(self, result, "unexpected initContainer")

    # 15. Deployment wrong container count (sidecar) -> BROKEN.
    def test_15_deployment_extra_container_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster(), deployment_kwargs={"containers": [_container(), _container(name="sidecar")]})
        result = _classify(cluster)
        _assert_broken(self, result, "expected exactly 1 (named 'gg-monitor')")

    # 16. Deployment wrong image -> BROKEN.
    def test_16_deployment_wrong_image_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster(), deployment_kwargs={"containers": [_container(image="some/other/image:9.9.9")]})
        result = _classify(cluster)
        _assert_broken(self, result, "image=")

    # 17. Deployment wrong serviceAccountName / hostNetwork=true -> BROKEN.
    def test_17_deployment_wrong_sa_and_host_network_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster(), deployment_kwargs={"service_account_name": "default", "host_network": True})
        result = _classify(cluster)
        _assert_broken(self, result, "serviceAccountName")
        _assert_broken(self, result, "hostNetwork")

    # 18. Deployment missing/wrong container port -> BROKEN.
    def test_18_deployment_wrong_container_port_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster(), deployment_kwargs={"containers": [_container(port=9999)]})
        result = _classify(cluster)
        _assert_broken(self, result, "containerPort=9999")

    # 19. Deployment wrong probe path -> BROKEN.
    def test_19_deployment_wrong_probe_path_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster(), deployment_kwargs={"containers": [_container(readiness_path="/wrong")]})
        result = _classify(cluster)
        _assert_broken(self, result, "readinessProbe.httpGet.path")

    # 20. Deployment missing/wrong env var -> BROKEN.
    def test_20_deployment_missing_env_var_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster(), deployment_kwargs={"containers": [_container(env_overrides={"DYNAMODB_TABLE": None})]})
        result = _classify(cluster)
        _assert_broken(self, result, "missing expected env var 'DYNAMODB_TABLE'")

    def test_20b_deployment_wrong_env_var_value_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster(), deployment_kwargs={"containers": [_container(env_overrides={"AWS_REGION": "ap-southeast-2"})]})
        result = _classify(cluster)
        _assert_broken(self, result, "env AWS_REGION=")

    # 21. Deployment missing/wrong volume mount -> BROKEN.
    def test_21_deployment_missing_volume_mount_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster(), deployment_kwargs={"containers": [_container(mounts=[{"name": "tmp", "mountPath": "/tmp"}])]})
        result = _classify(cluster)
        _assert_broken(self, result, "does not mount volume 'secrets-store'")

    # 22. Deployment unexpected pod volume (sidecar) -> BROKEN.
    def test_22_deployment_unexpected_pod_volume_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster(), deployment_kwargs={"volumes": _pod_volumes(extra=[{"name": "unexpected-vol", "emptyDir": {}}])})
        result = _classify(cluster)
        _assert_broken(self, result, "unexpected volume(s)")

    # 23. Deployment missing pod volume -> BROKEN.
    def test_23_deployment_missing_pod_volume_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster(), deployment_kwargs={"volumes": _pod_volumes(omit={"secrets-store"})})
        result = _classify(cluster)
        _assert_broken(self, result, "missing expected volume 'secrets-store'")

    # 24. ConfigMap missing -> BROKEN.
    def test_24_configmap_missing_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.objects.pop(("configmap", "goldengate-monitor-canonical-config", MONITOR_NAMESPACE))
        result = _classify(cluster)
        _assert_broken(self, result, "configmap/goldengate-monitor-canonical-config does not exist")

    # 25. ConfigMap registry semantically stale (missing a deployment) -> BROKEN.
    def test_25_configmap_registry_stale_is_broken(self):
        stale_registry = {**REGISTRY, "deployments": REGISTRY["deployments"][:1]}
        cluster = _populate_healthy_cluster(FakeCluster())  # cluster still serves the FULL registry
        result = _classify(cluster, registry=stale_registry)  # but the caller now expects only 1 deployment
        _assert_broken(self, result, "does not semantically match")

    # 26. ConfigMap registry extra/stale deployment (cluster ahead of the expected registry) -> BROKEN.
    def test_26_configmap_registry_extra_deployment_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster(), registry={**REGISTRY, "deployments": REGISTRY["deployments"][:1]})
        result = _classify(cluster)  # caller expects the FULL registry
        _assert_broken(self, result, "does not semantically match")

    # 27. SecretProviderClass wrong region -> BROKEN.
    def test_27_secretproviderclass_wrong_region_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("secretproviderclass", "gg-monitor-secrets", MONITOR_NAMESPACE, _secretproviderclass_obj(region="ap-southeast-2"))
        result = _classify(cluster)
        _assert_broken(self, result, "parameters.region")

    # 28. SecretProviderClass missing a required admin alias -> BROKEN.
    def test_28_secretproviderclass_missing_alias_is_broken(self):
        objects = _spc_objects()
        objects[0]["jmesPath"] = objects[0]["jmesPath"][:1]  # drop the admin-password alias for the first group
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("secretproviderclass", "gg-monitor-secrets", MONITOR_NAMESPACE, _secretproviderclass_obj(objects=objects))
        result = _classify(cluster)
        _assert_broken(self, result, "jmesPath is missing expected (path, objectAlias) pair(s)")

    # 29. SecretProviderClass unexpected foreign objectName -> BROKEN.
    def test_29_secretproviderclass_unexpected_object_is_broken(self):
        objects = _spc_objects() + [{"objectName": "dev/goldengate/foreign/admin", "objectType": "secretsmanager", "jmesPath": []}]
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("secretproviderclass", "gg-monitor-secrets", MONITOR_NAMESPACE, _secretproviderclass_obj(objects=objects))
        result = _classify(cluster)
        _assert_broken(self, result, "unexpected/unknown objectName")

    # 30. SecretProviderClass missing the TLS alias -> BROKEN.
    def test_30_secretproviderclass_missing_tls_alias_is_broken(self):
        objects = _spc_objects()
        objects[-1]["jmesPath"] = []
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("secretproviderclass", "gg-monitor-secrets", MONITOR_NAMESPACE, _secretproviderclass_obj(objects=objects))
        result = _classify(cluster)
        _assert_broken(self, result, "ca-chain-pem")

    # 31. SecretProviderClass duplicate objectName -> BROKEN.
    def test_31_secretproviderclass_duplicate_object_is_broken(self):
        objects = _spc_objects()
        objects.append(dict(objects[0]))
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("secretproviderclass", "gg-monitor-secrets", MONITOR_NAMESPACE, _secretproviderclass_obj(objects=objects))
        result = _classify(cluster)
        _assert_broken(self, result, "duplicate objectName")

    # B3B closeout Issue 2: exact SecretProviderClass field mapping (objectType + (path, objectAlias) pairs, not merely alias presence).
    def test_secretproviderclass_admin_wrong_object_type_is_broken(self):
        objects = _spc_objects()
        objects[0]["objectType"] = "ssmparameter"
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("secretproviderclass", "gg-monitor-secrets", MONITOR_NAMESPACE, _secretproviderclass_obj(objects=objects))
        result = _classify(cluster)
        _assert_broken(self, result, "objectType='ssmparameter'")

    def test_secretproviderclass_admin_wrong_jmespath_source_field_is_broken(self):
        # Reproduces the exact original defect: correct objectName, correct alias NAMES, but the jmesPath source field is wrong -- alias-presence-only checking previously let this through as HEALTHY.
        objects = _spc_objects()
        objects[0]["jmesPath"][0]["path"] = "WRONG_FIELD"
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("secretproviderclass", "gg-monitor-secrets", MONITOR_NAMESPACE, _secretproviderclass_obj(objects=objects))
        result = _classify(cluster)
        _assert_broken(self, result, "jmesPath is missing expected (path, objectAlias) pair(s)")
        _assert_broken(self, result, "jmesPath has unexpected (path, objectAlias) pair(s)")

    def test_secretproviderclass_admin_swapped_username_password_paths_is_broken(self):
        # Both alias NAMES are still present, but the OGG_ADMIN/OGG_ADMIN_PWD source fields are swapped between them -- must be rejected as a pair-identity mismatch, never accepted merely because both aliases exist somewhere.
        objects = _spc_objects()
        objects[0]["jmesPath"][0]["path"], objects[0]["jmesPath"][1]["path"] = (
            objects[0]["jmesPath"][1]["path"], objects[0]["jmesPath"][0]["path"])
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("secretproviderclass", "gg-monitor-secrets", MONITOR_NAMESPACE, _secretproviderclass_obj(objects=objects))
        result = _classify(cluster)
        _assert_broken(self, result, "jmesPath is missing expected (path, objectAlias) pair(s)")

    def test_secretproviderclass_admin_duplicate_pair_is_broken(self):
        objects = _spc_objects()
        objects[0]["jmesPath"].append(dict(objects[0]["jmesPath"][0]))
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("secretproviderclass", "gg-monitor-secrets", MONITOR_NAMESPACE, _secretproviderclass_obj(objects=objects))
        result = _classify(cluster)
        _assert_broken(self, result, "jmesPath contains a duplicate (path, objectAlias) pair")

    def test_secretproviderclass_admin_extra_pair_is_broken(self):
        objects = _spc_objects()
        objects[0]["jmesPath"].append({"path": "OGG_ADMIN", "objectAlias": "unexpected-extra-alias"})
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("secretproviderclass", "gg-monitor-secrets", MONITOR_NAMESPACE, _secretproviderclass_obj(objects=objects))
        result = _classify(cluster)
        _assert_broken(self, result, "jmesPath has unexpected (path, objectAlias) pair(s)")

    def test_secretproviderclass_tls_wrong_object_type_is_broken(self):
        objects = _spc_objects()
        objects[-1]["objectType"] = "ssmparameter"
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("secretproviderclass", "gg-monitor-secrets", MONITOR_NAMESPACE, _secretproviderclass_obj(objects=objects))
        result = _classify(cluster)
        _assert_broken(self, result, "(TLS) objectType='ssmparameter'")

    def test_secretproviderclass_tls_wrong_jmespath_source_field_is_broken(self):
        objects = _spc_objects()
        objects[-1]["jmesPath"][0]["path"] = "ca-chain.pem"  # missing the required JMESPath-quoted-identifier form
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("secretproviderclass", "gg-monitor-secrets", MONITOR_NAMESPACE, _secretproviderclass_obj(objects=objects))
        result = _classify(cluster)
        _assert_broken(self, result, "(TLS) jmesPath is missing expected (path, objectAlias) pair(s)")

    def test_secretproviderclass_tls_wrong_alias_is_broken(self):
        objects = _spc_objects()
        objects[-1]["jmesPath"][0]["objectAlias"] = "wrong-alias"
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("secretproviderclass", "gg-monitor-secrets", MONITOR_NAMESPACE, _secretproviderclass_obj(objects=objects))
        result = _classify(cluster)
        _assert_broken(self, result, "(TLS) jmesPath is missing expected (path, objectAlias) pair(s)")

    def test_secretproviderclass_tls_extra_alias_is_broken(self):
        objects = _spc_objects()
        objects[-1]["jmesPath"].append({"path": '"ca-chain.pem"', "objectAlias": "extra-alias"})
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("secretproviderclass", "gg-monitor-secrets", MONITOR_NAMESPACE, _secretproviderclass_obj(objects=objects))
        result = _classify(cluster)
        _assert_broken(self, result, "(TLS) jmesPath has unexpected (path, objectAlias) pair(s)")

    # Final DEPLOY freeze closeout Issue 3: malformed SecretProviderClass object/jmesPath rows must never be silently discarded.
    def test_secretproviderclass_canonical_objects_plus_non_dict_object_row_is_broken(self):
        objects = _spc_objects()
        objects.append("not-an-object")
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("secretproviderclass", "gg-monitor-secrets", MONITOR_NAMESPACE, _secretproviderclass_obj(objects=objects))
        result = _classify(cluster)
        _assert_broken(self, result, f"parameters.objects row #{len(objects) - 1} is not an object")

    def test_secretproviderclass_canonical_admin_jmespath_plus_non_dict_row_is_broken(self):
        objects = _spc_objects()
        objects[0]["jmesPath"].append("not-an-object")
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("secretproviderclass", "gg-monitor-secrets", MONITOR_NAMESPACE, _secretproviderclass_obj(objects=objects))
        result = _classify(cluster)
        _assert_broken(self, result, "jmesPath row #2 is not an object")

    def test_secretproviderclass_canonical_tls_jmespath_plus_non_dict_row_is_broken(self):
        objects = _spc_objects()
        objects[-1]["jmesPath"].append("not-an-object")
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("secretproviderclass", "gg-monitor-secrets", MONITOR_NAMESPACE, _secretproviderclass_obj(objects=objects))
        result = _classify(cluster)
        _assert_broken(self, result, "(TLS) jmesPath row #1 is not an object")

    def test_secretproviderclass_exact_correct_mapping_is_healthy(self):
        # Reproduction proof (positive control): the untouched, exactly-correct SecretProviderClass must remain HEALTHY under the new exact-pair validation.
        cluster = _populate_healthy_cluster(FakeCluster())
        result = _classify(cluster)
        self.assertEqual(result["state"], monitor_acceptance.STATE_HEALTHY, result["reasons"])

    # 32. Service missing -> BROKEN.
    def test_32_service_missing_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.objects.pop(("service", "gg-monitor", MONITOR_NAMESPACE))
        result = _classify(cluster)
        _assert_broken(self, result, "service/gg-monitor does not exist")

    # 33. Service wrong selector -> BROKEN.
    def test_33_service_wrong_selector_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("service", "gg-monitor", MONITOR_NAMESPACE, _service_obj(selector={"app": "wrong"}))
        result = _classify(cluster)
        _assert_broken(self, result, "selector=")

    # 34. Service wrong targetPort -> BROKEN.
    def test_34_service_wrong_target_port_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("service", "gg-monitor", MONITOR_NAMESPACE, _service_obj(target_port=9999))
        result = _classify(cluster)
        _assert_broken(self, result, "targetPort=")

    # 35. Service no Ready backing endpoint -> BROKEN.
    def test_35_service_no_ready_endpoint_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put_list("endpointslices.discovery.k8s.io", MONITOR_NAMESPACE, [])
        result = _classify(cluster)
        _assert_broken(self, result, "no Ready backing endpoint")

    # 36. Ingress absent while enabled -> BROKEN (covered via monkeypatched values in the dedicated class below).

    # 37. NetworkPolicy absent while enabled -> BROKEN (covered via monkeypatched values in the dedicated class below).

    # 38. Ready pod selection fails (no Running/Ready pod) -> BROKEN.
    def test_38_no_ready_pod_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put_list("pods", MONITOR_NAMESPACE, [])
        result = _classify(cluster)
        _assert_broken(self, result, "no Ready pod found")
        self.assertIsNone(result["checks"]["ready_pod_name"])

    # 39. Ready pod owned by a foreign ReplicaSet/Deployment UID -> excluded, no Ready pod found -> BROKEN.
    def test_39_pod_owned_by_foreign_deployment_is_excluded(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        rs_obj, pod_obj = _replicaset_and_pod()
        rs_obj["metadata"]["ownerReferences"][0]["uid"] = "some-other-deployment-uid"
        cluster.put("replicaset", "gg-monitor-rs1", MONITOR_NAMESPACE, rs_obj)
        cluster.put_list("pods", MONITOR_NAMESPACE, [pod_obj])
        result = _classify(cluster)
        _assert_broken(self, result, "no Ready pod found")

    # B3B closeout Issue 3: full Pod -> ReplicaSet -> Deployment UID/name ownership chain (never a name-only match).
    def test_pod_ownership_correct_full_uid_chain_is_accepted(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        result = _classify(cluster)
        self.assertEqual(result["state"], monitor_acceptance.STATE_HEALTHY, result["reasons"])
        self.assertEqual(result["checks"]["ready_pod_name"], "gg-monitor-rs1-abcde")

    def test_pod_owner_replicaset_uid_mismatch_is_rejected(self):
        # The pod's ownerReference NAME matches the real ReplicaSet, but the pod's claimed uid does not match the fetched ReplicaSet's actual metadata.uid -- a name match alone must never be trusted.
        cluster = _populate_healthy_cluster(FakeCluster())
        rs_obj, pod_obj = _replicaset_and_pod(pod_rs_owner_uid="rs-uid-STALE")
        cluster.put("replicaset", RS_NAME, MONITOR_NAMESPACE, rs_obj)
        cluster.put_list("pods", MONITOR_NAMESPACE, [pod_obj])
        result = _classify(cluster)
        _assert_broken(self, result, "no Ready pod found")
        self.assertIsNone(result["checks"]["ready_pod_name"])

    def test_pod_owner_replicaset_uid_missing_is_rejected(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        rs_obj, pod_obj = _replicaset_and_pod(pod_rs_owner_uid=None)
        cluster.put("replicaset", RS_NAME, MONITOR_NAMESPACE, rs_obj)
        cluster.put_list("pods", MONITOR_NAMESPACE, [pod_obj])
        result = _classify(cluster)
        _assert_broken(self, result, "no Ready pod found")
        self.assertIsNone(result["checks"]["ready_pod_name"])

    def test_replicaset_deployment_owner_name_wrong_is_rejected(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        rs_obj, pod_obj = _replicaset_and_pod(deploy_owner_name="some-other-deployment")
        cluster.put("replicaset", RS_NAME, MONITOR_NAMESPACE, rs_obj)
        cluster.put_list("pods", MONITOR_NAMESPACE, [pod_obj])
        result = _classify(cluster)
        _assert_broken(self, result, "no Ready pod found")
        self.assertIsNone(result["checks"]["ready_pod_name"])

    def test_replicaset_deployment_owner_uid_wrong_is_rejected(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        rs_obj, pod_obj = _replicaset_and_pod(deploy_owner_uid="some-other-deployment-uid")
        cluster.put("replicaset", RS_NAME, MONITOR_NAMESPACE, rs_obj)
        cluster.put_list("pods", MONITOR_NAMESPACE, [pod_obj])
        result = _classify(cluster)
        _assert_broken(self, result, "no Ready pod found")
        self.assertIsNone(result["checks"]["ready_pod_name"])

    # 40. healthz/readyz both healthy (200/200) -> stays HEALTHY.
    def test_40_healthz_readyz_200_stays_healthy(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        result = _classify(cluster, healthz_status=200, readyz_status=200)
        self.assertEqual(result["state"], monitor_acceptance.STATE_HEALTHY)

    # 41. healthz failing (non-200) -> BROKEN, readyz alone never treated as end-to-end success.
    def test_41_healthz_non_200_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        result = _classify(cluster, healthz_status=500, readyz_status=200)
        _assert_broken(self, result, "/healthz returned HTTP 500")

    # 42. readyz failing (non-200) -> BROKEN even if healthz passed.
    def test_42_readyz_non_200_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        result = _classify(cluster, healthz_status=200, readyz_status=503)
        _assert_broken(self, result, "/readyz returned HTTP 503")

    # 43. Forbidden/API failure -> ClassifierInspectionError, never silently downgraded.
    def test_43_forbidden_or_api_error_raises_inspection_error(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.fail("application", monitor_acceptance.ARGOCD_APP_NAME, ARGOCD_NAMESPACE, "Error from server (Forbidden): applications.argoproj.io is forbidden")
        with self.assertRaises(monitor_acceptance.ClassifierInspectionError):
            _classify(cluster)


class MonitorAcceptanceIngressAndNetworkPolicyEnabledTests(unittest.TestCase):
    """Current intentional architecture change: the real envs/dev/goldengate-monitor/values.yaml now has ingress.enabled=true (networkPolicy.enabled remains false) -- most tests below still monkeypatch _load_monitor_values to an explicit, self-contained values shape so they stay correct and meaningful regardless of what today's real committed file happens to say (exactly the fragility that made this real-file-dependent test module drift out of sync with the live ingress.enabled flip in the first place). Reuses the SAME module-level _ingress_obj()/_populate_healthy_cluster() every other class in this file uses -- never a second, independent Ingress-object schema."""

    def _enabled_values(self):
        base = monitor_acceptance._load_monitor_values(ENVIRONMENT)
        base = json.loads(json.dumps(base))  # cheap deep copy
        base["ingress"]["enabled"] = True
        base["ingress"]["host"] = MONITOR_HOST
        base["ingress"]["alb"]["groupOrder"] = "120"
        base.setdefault("networkPolicy", {})["enabled"] = True
        return base

    def _networkpolicy_obj(self, port=SERVICE_PORT):
        return {
            "spec": {
                "podSelector": {"matchLabels": dict(monitor_acceptance._SELECTOR_LABELS)},
                "policyTypes": ["Ingress"],
                "ingress": [{"ports": [{"protocol": "TCP", "port": port}]}],
            },
        }

    def test_ingress_enabled_and_correctly_shaped_is_healthy(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("networkpolicy", "gg-monitor", MONITOR_NAMESPACE, self._networkpolicy_obj())
        with mock.patch.object(monitor_acceptance, "_load_monitor_values", return_value=self._enabled_values()):
            result = _classify(cluster)
        self.assertEqual(result["state"], monitor_acceptance.STATE_HEALTHY, result["reasons"])

    def test_ingress_enabled_but_absent_is_broken(self):
        # _populate_healthy_cluster() now puts a correctly-shaped ingress by default (the real committed values.yaml has ingress.enabled=true) -- removed here explicitly to exercise the genuinely-absent case.
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.objects.pop(("ingress", monitor_acceptance.INGRESS_NAME, MONITOR_NAMESPACE))
        cluster.put("networkpolicy", "gg-monitor", MONITOR_NAMESPACE, self._networkpolicy_obj())
        with mock.patch.object(monitor_acceptance, "_load_monitor_values", return_value=self._enabled_values()):
            result = _classify(cluster)
        _assert_broken(self, result, "ingress/gg-monitor does not exist")

    def test_ingress_enabled_wrong_host_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("ingress", monitor_acceptance.INGRESS_NAME, MONITOR_NAMESPACE, _ingress_obj(host="wrong.example.com"))
        cluster.put("networkpolicy", "gg-monitor", MONITOR_NAMESPACE, self._networkpolicy_obj())
        with mock.patch.object(monitor_acceptance, "_load_monitor_values", return_value=self._enabled_values()):
            result = _classify(cluster)
        _assert_broken(self, result, "has no rule with host")

    def test_networkpolicy_enabled_but_absent_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        with mock.patch.object(monitor_acceptance, "_load_monitor_values", return_value=self._enabled_values()):
            result = _classify(cluster)
        _assert_broken(self, result, "networkpolicy/gg-monitor does not exist")

    def test_networkpolicy_enabled_wrong_port_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("networkpolicy", "gg-monitor", MONITOR_NAMESPACE, self._networkpolicy_obj(port=9999))
        with mock.patch.object(monitor_acceptance, "_load_monitor_values", return_value=self._enabled_values()):
            result = _classify(cluster)
        _assert_broken(self, result, "does not allow ingress TCP")

    def test_ingress_disabled_but_present_is_broken(self):
        # Deliberately forces ingress.enabled=false via monkeypatch -- independent of whatever the REAL committed envs/dev/goldengate-monitor/values.yaml currently says -- an ingress object existing anyway must never silently pass, regardless of today's real committed ingress.enabled value.
        cluster = _populate_healthy_cluster(FakeCluster())
        disabled_values = json.loads(json.dumps(monitor_acceptance._load_monitor_values(ENVIRONMENT)))
        disabled_values.setdefault("ingress", {})["enabled"] = False
        with mock.patch.object(monitor_acceptance, "_load_monitor_values", return_value=disabled_values):
            result = _classify(cluster)
        _assert_broken(self, result, "must be absent")


class MonitorAcceptanceNoMutationSourceSweepTests(unittest.TestCase):
    """Static source-safety proof: the classifier module (and its shared k8s_common helper) must never construct a mutating kubectl/helm command, and never issue a network HTTP request itself."""

    FORBIDDEN_SUBSTRINGS = (
        "kubectl apply", "kubectl create", "kubectl delete", "kubectl patch",
        "kubectl annotate", "kubectl label",
        "helm install", "helm upgrade", "helm uninstall",
        "urllib.request", "requests.get", "http.client",
    )

    def test_source_contains_no_mutating_or_network_command(self):
        k8s_common_path = os.path.join(REPO_ROOT, "hack", "orchestration", "k8s_common.py")
        for path in (TOOL_PATH, k8s_common_path):
            with open(path) as f:
                source = f.read()
            hits = [s for s in self.FORBIDDEN_SUBSTRINGS if s in source]
            self.assertEqual(hits, [], f"{path} contains a mutating/network-looking construct: {hits}")

    def test_full_healthy_pass_uses_only_get_verbs(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        _classify(cluster)


if __name__ == "__main__":
    unittest.main()
