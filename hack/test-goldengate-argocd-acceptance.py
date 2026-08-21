"""Offline tests for hack/orchestration/argocd_acceptance.py (post-reconciliation acceptance: HEALTHY/BROKEN); run directly via `python3 hack/test-goldengate-argocd-acceptance.py`. No live Kubernetes -- every kubectl response is a fake, injected fixture. Exercises the classifier's actual logic (never merely greps its source). Pre-reconciliation ownership safety (ABSENT/OWNED/BROKEN) is a separate module -- see hack/test-goldengate-argocd-state.py."""
from __future__ import annotations

import importlib.util
import json
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL_PATH = os.path.join(REPO_ROOT, "hack", "orchestration", "argocd_acceptance.py")


def _load_tool():
    spec = importlib.util.spec_from_file_location("argocd_acceptance", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


argocd_acceptance = _load_tool()

NAMESPACE = "argocd"
ECR_REGISTRY = "229410149234.dkr.ecr.eu-west-1.amazonaws.com"
ARGOCD_ECR_READ_ROLE_ARN = "arn:aws:iam::668311715351:role/GoldenGateArgocdECRRead-dev"
ARGOCD_HOST = "argocd.goldengate-dev.adcbmis.local"
ALB_GROUP_NAME = "gg-poc-dev-alb"
ACM_CERTIFICATE_ARN = "arn:aws:acm:eu-west-1:668311715351:certificate/9e53e28e-3243-47fc-85a1-50f9a94acde7"

INGRESS_VALUES_DISABLED = {"enabled": False}
INGRESS_VALUES_ENABLED = {
    "enabled": True,
    "mode": "standalone",
    "ingressClassName": "alb",
    "serviceName": "argocd-server",
    "servicePort": 443,
    "groupOrder": "50",
    "targetType": "ip",
    "backendProtocol": "HTTPS",
    "listenPorts": '[{"HTTPS":443}]',
    "healthcheckProtocol": "HTTPS",
    "healthcheckPath": "/healthz",
    "healthcheckPort": "traffic-port",
    "scheme": "internal",
}


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
            elif args[idx] == "-o":
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


def _ready_replicaset_like(name, replicas=1, generation=3):
    return {
        "metadata": {"name": name, "generation": generation},
        "spec": {"replicas": replicas},
        "status": {
            "observedGeneration": generation,
            "updatedReplicas": replicas,
            "readyReplicas": replicas,
            "availableReplicas": replicas,
            "currentReplicas": replicas,
        },
    }


def _sa_obj(role_arn):
    return {"metadata": {"annotations": {"eks.amazonaws.com/role-arn": role_arn}}}


def _secret_obj(helm_repo, ecr_registry=ECR_REGISTRY, labeled=True, url_override=None):
    import base64
    url = url_override if url_override is not None else f"oci://{ecr_registry}/{helm_repo}"
    labels = {"argocd.argoproj.io/secret-type": "repository"} if labeled else {}
    return {"metadata": {"labels": labels}, "data": {"url": base64.b64encode(url.encode("utf-8")).decode("ascii")}}


def _correct_ingress_obj():
    return {
        "metadata": {
            "labels": dict(argocd_acceptance.INGRESS_OWNERSHIP_LABELS),
            "annotations": {
                "alb.ingress.kubernetes.io/group.name": ALB_GROUP_NAME,
                "alb.ingress.kubernetes.io/group.order": "50",
                "alb.ingress.kubernetes.io/certificate-arn": ACM_CERTIFICATE_ARN,
                "alb.ingress.kubernetes.io/listen-ports": '[{"HTTPS":443}]',
                "alb.ingress.kubernetes.io/target-type": "ip",
                "alb.ingress.kubernetes.io/backend-protocol": "HTTPS",
                "alb.ingress.kubernetes.io/healthcheck-protocol": "HTTPS",
                "alb.ingress.kubernetes.io/healthcheck-path": "/healthz",
                "alb.ingress.kubernetes.io/healthcheck-port": "traffic-port",
                "alb.ingress.kubernetes.io/scheme": "internal",
            },
        },
        "spec": {
            "ingressClassName": "alb",
            "rules": [{"host": ARGOCD_HOST, "http": {"paths": [{"backend": {"service": {"name": "argocd-server", "port": {"number": 443}}}}]}}],
        },
        "status": {"loadBalancer": {"ingress": [{"hostname": "internal-abc123.eu-west-1.elb.amazonaws.com"}]}},
    }


def _populate_healthy_cluster(cluster):
    cluster.put("namespace", NAMESPACE, None, {"metadata": {}})
    for crd in argocd_acceptance.REQUIRED_CRDS:
        cluster.put("crd", crd, None, {"metadata": {}})
    for name in argocd_acceptance.REQUIRED_DEPLOYMENTS:
        cluster.put("deployment", name, NAMESPACE, _ready_replicaset_like(name))
    for name in argocd_acceptance.REQUIRED_STATEFULSETS:
        cluster.put("statefulset", name, NAMESPACE, _ready_replicaset_like(name))
    for name in argocd_acceptance.REQUIRED_SERVICES:
        cluster.put("service", name, NAMESPACE, {"metadata": {}})
    cluster.put("serviceaccount", argocd_acceptance.ECR_TOKEN_SYNC_NAME, NAMESPACE, _sa_obj(ARGOCD_ECR_READ_ROLE_ARN))
    cluster.put("role", argocd_acceptance.ECR_TOKEN_SYNC_NAME, NAMESPACE, {"metadata": {}})
    cluster.put("rolebinding", argocd_acceptance.ECR_TOKEN_SYNC_NAME, NAMESPACE, {"metadata": {}})
    cluster.put("cronjob", argocd_acceptance.ECR_TOKEN_SYNC_NAME, NAMESPACE, {"spec": {"suspend": False}})
    for secret_name, helm_repo in argocd_acceptance.REQUIRED_REPO_SECRETS.items():
        cluster.put("secret", secret_name, NAMESPACE, _secret_obj(helm_repo))
    return cluster


def _classify(cluster, ingress_values=None):
    return argocd_acceptance.classify(
        cluster,
        environment="dev",
        namespace=NAMESPACE,
        ecr_registry=ECR_REGISTRY,
        argocd_ecr_read_role_arn=ARGOCD_ECR_READ_ROLE_ARN,
        argocd_host=ARGOCD_HOST,
        alb_group_name=ALB_GROUP_NAME,
        acm_certificate_arn=ACM_CERTIFICATE_ARN,
        ingress_values=ingress_values if ingress_values is not None else INGRESS_VALUES_DISABLED,
    )


class ArgoCdAcceptanceTests(unittest.TestCase):
    def test_all_required_components_healthy(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_acceptance.STATE_HEALTHY)
        self.assertEqual(result["reasons"], [])

    def test_missing_crd_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.objects.pop(("crd", argocd_acceptance.REQUIRED_CRDS[0], None))
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_acceptance.STATE_BROKEN)
        self.assertTrue(any("missing required CRD" in r for r in result["reasons"]))

    def test_deployment_not_ready_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        not_ready = _ready_replicaset_like("argocd-server")
        not_ready["status"]["readyReplicas"] = 0
        cluster.put("deployment", "argocd-server", NAMESPACE, not_ready)
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_acceptance.STATE_BROKEN)
        self.assertTrue(any("deployment/argocd-server not ready" in r for r in result["reasons"]))

    def test_wrong_ecr_token_sync_role_arn_is_broken(self):
        # Post-reconciliation, this is now a strict correctness check (unlike ownership, which never inspects it).
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("serviceaccount", argocd_acceptance.ECR_TOKEN_SYNC_NAME, NAMESPACE, _sa_obj("arn:aws:iam::668311715351:role/SomeOtherRole"))
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_acceptance.STATE_BROKEN)

    def test_missing_repository_secret_is_broken(self):
        # Unlike ownership (which never flags a missing Secret at all), acceptance strictly requires all four to exist.
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.objects.pop(("secret", "argocd-ecr-goldengate-platform-oci", NAMESPACE))
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_acceptance.STATE_BROKEN)
        self.assertTrue(any("Secret argocd-ecr-goldengate-platform-oci does not exist" in r for r in result["reasons"]))

    def test_wrong_repository_secret_url_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("secret", "argocd-ecr-goldengate-oci", NAMESPACE, _secret_obj("helm/goldengate", url_override="oci://wrong.example.com/helm/goldengate"))
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_acceptance.STATE_BROKEN)

    def test_ingress_disabled_and_absent_is_healthy(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        result = _classify(cluster, ingress_values=INGRESS_VALUES_DISABLED)
        self.assertEqual(result["state"], argocd_acceptance.STATE_HEALTHY)

    def test_ingress_enabled_and_correct_is_healthy(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("ingress", argocd_acceptance.INGRESS_NAME, NAMESPACE, _correct_ingress_obj())
        result = _classify(cluster, ingress_values=INGRESS_VALUES_ENABLED)
        self.assertEqual(result["state"], argocd_acceptance.STATE_HEALTHY)
        self.assertEqual(result["reasons"], [])

    def test_ingress_enabled_and_missing_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        result = _classify(cluster, ingress_values=INGRESS_VALUES_ENABLED)
        self.assertEqual(result["state"], argocd_acceptance.STATE_BROKEN)
        self.assertTrue(any(f"ingress/{argocd_acceptance.INGRESS_NAME}" in r and "does not exist" in r for r in result["reasons"]))

    def test_ingress_no_load_balancer_address_is_broken(self):
        # Unlike the (removed) old RECONCILABLE tolerance, acceptance is strict here -- 20-sub-argocd.yaml's own bounded wait has already had its chance by the time this runs.
        cluster = _populate_healthy_cluster(FakeCluster())
        not_provisioned = _correct_ingress_obj()
        not_provisioned["status"] = {"loadBalancer": {}}
        cluster.put("ingress", argocd_acceptance.INGRESS_NAME, NAMESPACE, not_provisioned)
        result = _classify(cluster, ingress_values=INGRESS_VALUES_ENABLED)
        self.assertEqual(result["state"], argocd_acceptance.STATE_BROKEN)
        self.assertTrue(any("status.loadBalancer.ingress is empty" in r for r in result["reasons"]))

    def test_ingress_wrong_host_is_broken(self):
        cluster = _populate_healthy_cluster(FakeCluster())
        wrong = _correct_ingress_obj()
        wrong["spec"]["rules"][0]["host"] = "wrong-host.example.com"
        cluster.put("ingress", argocd_acceptance.INGRESS_NAME, NAMESPACE, wrong)
        result = _classify(cluster, ingress_values=INGRESS_VALUES_ENABLED)
        self.assertEqual(result["state"], argocd_acceptance.STATE_BROKEN)

    def test_true_to_false_pruning_proof_still_present_ingress_is_broken(self):
        # The exact true->false pruning proof: once argocdServerIngress.enabled=false, a still-present Ingress means Helm/Argo CD's own selfHeal/prune has not actually completed -- never silently tolerated as harmless leftover state.
        cluster = _populate_healthy_cluster(FakeCluster())
        cluster.put("ingress", argocd_acceptance.INGRESS_NAME, NAMESPACE, _correct_ingress_obj())
        result = _classify(cluster, ingress_values=INGRESS_VALUES_DISABLED)
        self.assertEqual(result["state"], argocd_acceptance.STATE_BROKEN)
        self.assertTrue(any("expected pruned" in r for r in result["reasons"]))

    def test_kubectl_command_error_raises_inspection_error(self):
        cluster = FakeCluster()
        cluster.fail("namespace", NAMESPACE, None, "Unable to connect to the server: dial tcp: i/o timeout")
        with self.assertRaises(argocd_acceptance.ClassifierInspectionError):
            _classify(cluster)


class ArgoCdAcceptanceNoMutationSourceSweepTests(unittest.TestCase):
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
