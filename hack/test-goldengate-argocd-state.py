"""Offline tests for hack/orchestration/argocd_state.py; run directly via `python3 hack/test-goldengate-argocd-state.py`. No live Kubernetes -- every kubectl response is a fake, injected fixture. Exercises the classifier's actual logic (never merely greps its source)."""
from __future__ import annotations

import importlib.util
import json
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL_PATH = os.path.join(REPO_ROOT, "hack", "orchestration", "argocd_state.py")


def _load_tool():
    spec = importlib.util.spec_from_file_location("argocd_state", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


argocd_state = _load_tool()

NAMESPACE = "argocd"
ECR_REGISTRY = "229410149234.dkr.ecr.eu-west-1.amazonaws.com"
ARGOCD_ECR_READ_ROLE_ARN = "arn:aws:iam::668311715351:role/GoldenGateArgocdECRRead-dev"

# Fresh-Cluster Platform + Argo Ingress Self-Recovery Fix: environment-specific identity, exactly as it would be resolved from envs/dev/environment.yaml via hack/goldengate-environment.py -- passed into classify() as explicit arguments, never hardcoded inside argocd_state.py itself.
ARGOCD_HOST = "argocd.goldengate-dev.adcbmis.local"
ALB_GROUP_NAME = "gg-poc-dev-alb"
ACM_CERTIFICATE_ARN = "arn:aws:acm:eu-west-1:668311715351:certificate/9e53e28e-3243-47fc-85a1-50f9a94acde7"

# Application-constant contract, exactly as committed in envs/dev/argocd/values.yaml's argocdServerIngress block -- passed into classify() as ingress_values, never hardcoded inside argocd_state.py itself.
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
    """Models exactly the subset of `kubectl get <resource> [name] [-n ns] -o json` behavior the classifier depends on -- never a real kubectl process."""

    def __init__(self):
        self.objects = {}  # (resource, name, namespace) -> dict
        self.force_errors = {}  # (resource, name, namespace) -> stderr text

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


def _namespace_obj(name):
    return {"metadata": {"name": name}}


def _crd_obj(name):
    return {"metadata": {"name": name}}


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


def _service_obj(name):
    return {"metadata": {"name": name}}


def _sa_obj(name, role_arn=ARGOCD_ECR_READ_ROLE_ARN):
    return {"metadata": {"name": name, "annotations": {"eks.amazonaws.com/role-arn": role_arn}}}


def _role_obj(name):
    return {"metadata": {"name": name}}


def _cronjob_obj(name, suspended=False):
    return {"metadata": {"name": name}, "spec": {"suspend": suspended}}


def _secret_obj(name, helm_repo, ecr_registry=ECR_REGISTRY, labeled=True, url_override=None):
    import base64
    url = url_override if url_override is not None else f"oci://{ecr_registry}/{helm_repo}"
    labels = {"argocd.argoproj.io/secret-type": "repository"} if labeled else {}
    return {
        "metadata": {"name": name, "labels": labels},
        "data": {"url": base64.b64encode(url.encode("utf-8")).decode("ascii")},
    }


def _correct_ingress_obj():
    """The exact shape helm/argocd/templates/argocd-server-ingress.yaml renders for envs/dev/argocd/values.yaml's own committed standalone-mode contract, plus a populated status.loadBalancer.ingress (the AWS Load Balancer Controller has published an address)."""
    return {
        "metadata": {
            "labels": dict(argocd_state.INGRESS_OWNERSHIP_LABELS),
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
            "rules": [{
                "host": ARGOCD_HOST,
                "http": {"paths": [{"backend": {"service": {"name": "argocd-server", "port": {"number": 443}}}}]},
            }],
        },
        "status": {"loadBalancer": {"ingress": [{"hostname": "internal-abc123.eu-west-1.elb.amazonaws.com"}]}},
    }


def _populate_healthy_cluster(cluster):
    cluster.put("namespace", NAMESPACE, None, _namespace_obj(NAMESPACE))
    for crd in argocd_state.REQUIRED_CRDS:
        cluster.put("crd", crd, None, _crd_obj(crd))
    for name in argocd_state.REQUIRED_DEPLOYMENTS:
        cluster.put("deployment", name, NAMESPACE, _ready_replicaset_like(name))
    for name in argocd_state.REQUIRED_STATEFULSETS:
        cluster.put("statefulset", name, NAMESPACE, _ready_replicaset_like(name))
    for name in argocd_state.REQUIRED_SERVICES:
        cluster.put("service", name, NAMESPACE, _service_obj(name))
    cluster.put("serviceaccount", argocd_state.ECR_TOKEN_SYNC_NAME, NAMESPACE, _sa_obj(argocd_state.ECR_TOKEN_SYNC_NAME))
    cluster.put("role", argocd_state.ECR_TOKEN_SYNC_NAME, NAMESPACE, _role_obj(argocd_state.ECR_TOKEN_SYNC_NAME))
    cluster.put("rolebinding", argocd_state.ECR_TOKEN_SYNC_NAME, NAMESPACE, _role_obj(argocd_state.ECR_TOKEN_SYNC_NAME))
    cluster.put("cronjob", argocd_state.ECR_TOKEN_SYNC_NAME, NAMESPACE, _cronjob_obj(argocd_state.ECR_TOKEN_SYNC_NAME))
    for secret_name, helm_repo in argocd_state.REQUIRED_REPO_SECRETS.items():
        cluster.put("secret", secret_name, NAMESPACE, _secret_obj(secret_name, helm_repo))
    return cluster


def _classify(cluster, ingress_values=None):
    return argocd_state.classify(
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


class ArgoCdStateClassifierTests(unittest.TestCase):
    def test_completely_clean_cluster_is_absent(self):
        cluster = FakeCluster()
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_ABSENT)
        self.assertEqual(result["reasons"], [])

    def test_namespace_absent_but_one_crd_exists_is_broken(self):
        cluster = FakeCluster()
        cluster.put("crd", argocd_state.REQUIRED_CRDS[0], None, _crd_obj(argocd_state.REQUIRED_CRDS[0]))
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_BROKEN)
        self.assertTrue(any("namespace" in r for r in result["reasons"]))

    def test_namespace_exists_but_crds_missing_is_broken(self):
        cluster = FakeCluster()
        cluster.put("namespace", NAMESPACE, None, _namespace_obj(NAMESPACE))
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_BROKEN)
        self.assertTrue(any("CRD" in r for r in result["reasons"]))

    def test_partial_deployments_is_broken(self):
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        cluster.objects.pop(("deployment", "argocd-repo-server", NAMESPACE))
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_BROKEN)
        self.assertTrue(any("deployment/argocd-repo-server does not exist" in r for r in result["reasons"]))

    def test_required_deployment_not_ready_is_broken(self):
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        not_ready = _ready_replicaset_like("argocd-server")
        not_ready["status"]["readyReplicas"] = 0
        cluster.put("deployment", "argocd-server", NAMESPACE, not_ready)
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_BROKEN)
        self.assertTrue(any("deployment/argocd-server not ready" in r for r in result["reasons"]))

    def test_application_controller_statefulset_not_ready_is_broken(self):
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        not_ready = _ready_replicaset_like("argocd-application-controller")
        not_ready["status"]["currentReplicas"] = 0
        cluster.put("statefulset", "argocd-application-controller", NAMESPACE, not_ready)
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_BROKEN)
        self.assertTrue(any("statefulset/argocd-application-controller not ready" in r for r in result["reasons"]))

    def test_token_sync_serviceaccount_missing_is_broken(self):
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        cluster.objects.pop(("serviceaccount", argocd_state.ECR_TOKEN_SYNC_NAME, NAMESPACE))
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_BROKEN)
        self.assertTrue(any("serviceaccount/argocd-ecr-token-sync does not exist" in r for r in result["reasons"]))

    def test_token_sync_serviceaccount_wrong_irsa_role_is_broken(self):
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        cluster.put("serviceaccount", argocd_state.ECR_TOKEN_SYNC_NAME, NAMESPACE, _sa_obj(argocd_state.ECR_TOKEN_SYNC_NAME, role_arn="arn:aws:iam::668311715351:role/SomeOtherRole"))
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_BROKEN)
        self.assertTrue(any("eks.amazonaws.com/role-arn" in r for r in result["reasons"]))

    def test_token_sync_cronjob_suspended_is_broken(self):
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        cluster.put("cronjob", argocd_state.ECR_TOKEN_SYNC_NAME, NAMESPACE, _cronjob_obj(argocd_state.ECR_TOKEN_SYNC_NAME, suspended=True))
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_BROKEN)
        self.assertTrue(any("is suspended" in r for r in result["reasons"]))

    def test_repository_secret_missing_is_reconcilable(self):
        # Live Argo Recovery Fix: a single missing repository Secret, with everything else in the cluster structurally healthy, is exactly the class of drift the reusable Argo specialist workflow's own immediate bounded ECR token-sync validation step already safely repairs -- RECONCILABLE, never terminal BROKEN.
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        cluster.objects.pop(("secret", "argocd-ecr-goldengate-platform-oci", NAMESPACE))
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_RECONCILABLE)
        self.assertTrue(any("Secret argocd-ecr-goldengate-platform-oci does not exist" in r for r in result["reasons"]))

    def test_repository_secret_wrong_url_is_reconcilable(self):
        # Live Argo Recovery Fix: a drifted repository Secret URL, with everything else healthy, is the same safely-repairable class as a missing Secret.
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        cluster.put("secret", "argocd-ecr-goldengate-oci", NAMESPACE, _secret_obj("argocd-ecr-goldengate-oci", "helm/goldengate", url_override="oci://wrong.example.com/helm/goldengate"))
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_RECONCILABLE)
        self.assertTrue(any("url=" in r for r in result["reasons"]))

    def test_real_incident_all_four_repository_secrets_missing_is_reconcilable(self):
        # Live Argo Recovery Fix: reproduces the actual live incident exactly -- namespace present, CRDs present, core Deployments/StatefulSet ready, Services present, ecr-token-sync ServiceAccount/Role/RoleBinding/CronJob all correct (including IRSA role-arn and non-suspended), ingress correctly absent because disabled, but all four repository Secrets are absent (the earlier STS/regional-endpoint failure meant the token-sync CronJob never ran to create them). Must classify RECONCILABLE, never BROKEN -- this is the minimum required recovery case MAIN must now auto-repair through the reusable Argo specialist workflow instead of dead-ending.
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        for secret_name in argocd_state.REQUIRED_REPO_SECRETS:
            cluster.objects.pop(("secret", secret_name, NAMESPACE))
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_RECONCILABLE)
        self.assertEqual(len(result["reasons"]), 4)
        for secret_name in argocd_state.REQUIRED_REPO_SECRETS:
            self.assertTrue(any(f"Secret {secret_name} does not exist" in r for r in result["reasons"]))

    def test_reconcilable_secret_drift_plus_unrelated_broken_component_stays_broken(self):
        # Live Argo Recovery Fix: RECONCILABLE requires EVERY collected reason to be in the safe subset -- a missing Secret alongside ANY other unsafe drift (here, a not-ready core Deployment) must still classify BROKEN, proving this is not a broader "any drift is fine as long as a Secret is also missing" carve-out.
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        cluster.objects.pop(("secret", "argocd-ecr-goldengate-oci", NAMESPACE))
        not_ready = _ready_replicaset_like("argocd-server")
        not_ready["status"]["readyReplicas"] = 0
        cluster.put("deployment", "argocd-server", NAMESPACE, not_ready)
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_BROKEN)
        self.assertTrue(any("Secret argocd-ecr-goldengate-oci does not exist" in r for r in result["reasons"]))
        self.assertTrue(any("deployment/argocd-server not ready" in r for r in result["reasons"]))

    def test_all_four_repository_secrets_missing_plus_one_unsafe_component_stays_broken(self):
        # Live Argo Self-Recovery Fix: the exact broader shape called out explicitly -- all four repository Secrets missing (the real incident's own Secret drift) PLUS one unhealthy core component (here, the application-controller StatefulSet) must still classify BROKEN, never RECONCILABLE, no matter how much of the drift is otherwise safe.
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        for secret_name in argocd_state.REQUIRED_REPO_SECRETS:
            cluster.objects.pop(("secret", secret_name, NAMESPACE))
        not_ready = _ready_replicaset_like("argocd-application-controller")
        not_ready["status"]["currentReplicas"] = 0
        cluster.put("statefulset", "argocd-application-controller", NAMESPACE, not_ready)
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_BROKEN)
        self.assertEqual(len(result["reasons"]), 5)
        for secret_name in argocd_state.REQUIRED_REPO_SECRETS:
            self.assertTrue(any(f"Secret {secret_name} does not exist" in r for r in result["reasons"]))
        self.assertTrue(any("statefulset/argocd-application-controller not ready" in r for r in result["reasons"]))

    def test_all_required_components_healthy(self):
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_HEALTHY)
        self.assertEqual(result["reasons"], [])

    def test_ingress_disabled_and_absent_is_still_healthy(self):
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        result = _classify(cluster, ingress_values=INGRESS_VALUES_DISABLED)
        self.assertEqual(result["state"], argocd_state.STATE_HEALTHY)

    def test_kubectl_command_error_raises_inspection_error_not_absent(self):
        cluster = FakeCluster()
        cluster.fail("namespace", NAMESPACE, None, "Unable to connect to the server: dial tcp: i/o timeout")
        with self.assertRaises(argocd_state.ClassifierInspectionError):
            _classify(cluster)

    def test_permission_denied_raises_inspection_error_not_absent(self):
        cluster = FakeCluster()
        cluster.fail("namespace", NAMESPACE, None, "Error from server (Forbidden): namespaces is forbidden: User \"x\" cannot get resource \"namespaces\"")
        with self.assertRaises(argocd_state.ClassifierInspectionError):
            _classify(cluster)

    def test_malformed_json_raises_inspection_error(self):
        cluster = FakeCluster()

        def bad_run(args):
            if args[:2] == ["get", "namespace"]:
                return 0, "not valid json{{{", ""
            return cluster(args)

        with self.assertRaises(argocd_state.ClassifierInspectionError):
            argocd_state.classify(
                bad_run,
                environment="dev",
                namespace=NAMESPACE,
                ecr_registry=ECR_REGISTRY,
                argocd_ecr_read_role_arn=ARGOCD_ECR_READ_ROLE_ARN,
                argocd_host=ARGOCD_HOST,
                alb_group_name=ALB_GROUP_NAME,
                acm_certificate_arn=ACM_CERTIFICATE_ARN,
                ingress_values=INGRESS_VALUES_DISABLED,
            )

    def test_secret_missing_repository_label_is_reconcilable(self):
        # Live Argo Recovery Fix: a mislabeled repository Secret, with everything else healthy, is the same safely-repairable class as a missing Secret.
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        cluster.put("secret", "argocd-ecr-goldengate-oci", NAMESPACE, _secret_obj("argocd-ecr-goldengate-oci", "helm/goldengate", labeled=False))
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_RECONCILABLE)
        self.assertTrue(any("secret-type=repository" in r for r in result["reasons"]))

    def test_classifier_contract_marker_present_on_every_result(self):
        # Live Argo Self-Recovery Fix: the classifier always stamps its own stable compatibility marker (CLASSIFIER_CONTRACT) onto every result shape, across every state -- ABSENT (the early-return path), HEALTHY, and RECONCILABLE (the two different late-return paths) -- so a caller can fail closed on a version-skewed classifier before ever trusting its state value. This is a compatibility guard only, never itself the operational recovery mechanism for repository Secret/Ingress drift.
        self.assertTrue(argocd_state.CLASSIFIER_CONTRACT)
        absent_result = _classify(FakeCluster())
        self.assertEqual(absent_result["contract"], argocd_state.CLASSIFIER_CONTRACT)
        healthy_cluster = FakeCluster()
        _populate_healthy_cluster(healthy_cluster)
        healthy_result = _classify(healthy_cluster)
        self.assertEqual(healthy_result["contract"], argocd_state.CLASSIFIER_CONTRACT)
        reconcilable_cluster = FakeCluster()
        _populate_healthy_cluster(reconcilable_cluster)
        reconcilable_cluster.objects.pop(("secret", "argocd-ecr-goldengate-oci", NAMESPACE))
        reconcilable_result = _classify(reconcilable_cluster)
        self.assertEqual(reconcilable_result["state"], argocd_state.STATE_RECONCILABLE)
        self.assertEqual(reconcilable_result["contract"], argocd_state.CLASSIFIER_CONTRACT)


class ArgoCdIngressStateClassifierTests(unittest.TestCase):
    """Fresh-Cluster Platform + Argo Ingress Self-Recovery Fix: the argocd-server-ingress RECONCILABLE/BROKEN contract. Test naming matches the task's own A-numbering (A1-A10) for traceability."""

    def test_A1_healthy_core_plus_correct_ingress_is_healthy(self):
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        cluster.put("ingress", argocd_state.INGRESS_NAME, NAMESPACE, _correct_ingress_obj())
        result = _classify(cluster, ingress_values=INGRESS_VALUES_ENABLED)
        self.assertEqual(result["state"], argocd_state.STATE_HEALTHY)
        self.assertEqual(result["reasons"], [])

    def test_A2_healthy_core_plus_missing_required_ingress_is_reconcilable(self):
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        result = _classify(cluster, ingress_values=INGRESS_VALUES_ENABLED)
        self.assertEqual(result["state"], argocd_state.STATE_RECONCILABLE)
        self.assertTrue(any(f"ingress/{argocd_state.INGRESS_NAME}" in r and "does not exist" in r for r in result["reasons"]))

    def test_A3_healthy_core_plus_owned_ingress_safe_drift_is_reconcilable(self):
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        drifted = _correct_ingress_obj()
        drifted["metadata"]["annotations"]["alb.ingress.kubernetes.io/group.order"] = "99"
        drifted["spec"]["ingressClassName"] = "nginx"
        cluster.put("ingress", argocd_state.INGRESS_NAME, NAMESPACE, drifted)
        result = _classify(cluster, ingress_values=INGRESS_VALUES_ENABLED)
        self.assertEqual(result["state"], argocd_state.STATE_RECONCILABLE)
        self.assertTrue(any("group.order" in r for r in result["reasons"]))
        self.assertTrue(any("ingressClassName" in r for r in result["reasons"]))

    def test_A4_foreign_or_ambiguous_same_name_ingress_is_broken(self):
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        foreign = _correct_ingress_obj()
        foreign["metadata"]["labels"] = {"app.kubernetes.io/managed-by": "some-other-team"}
        cluster.put("ingress", argocd_state.INGRESS_NAME, NAMESPACE, foreign)
        result = _classify(cluster, ingress_values=INGRESS_VALUES_ENABLED)
        self.assertEqual(result["state"], argocd_state.STATE_BROKEN)
        self.assertTrue(any("foreign/ambiguous ownership" in r for r in result["reasons"]))

    def test_A4b_foreign_ingress_drift_never_mixed_into_reconcilable_subset(self):
        # Even though a foreign Ingress ALSO happens to have a "wrong" host (irrelevant once ownership itself is foreign), only the ownership reason is ever recorded -- this module never inspects/reports field-level drift on an object it does not own.
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        foreign = _correct_ingress_obj()
        foreign["metadata"]["labels"] = {}
        foreign["spec"]["rules"][0]["host"] = "totally-different.example.com"
        cluster.put("ingress", argocd_state.INGRESS_NAME, NAMESPACE, foreign)
        result = _classify(cluster, ingress_values=INGRESS_VALUES_ENABLED)
        self.assertEqual(result["state"], argocd_state.STATE_BROKEN)
        self.assertEqual(len(result["reasons"]), 1)
        self.assertIn("foreign/ambiguous ownership", result["reasons"][0])

    def test_A5_four_missing_repository_secrets_plus_missing_ingress_is_reconcilable(self):
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        for secret_name in argocd_state.REQUIRED_REPO_SECRETS:
            cluster.objects.pop(("secret", secret_name, NAMESPACE))
        result = _classify(cluster, ingress_values=INGRESS_VALUES_ENABLED)
        self.assertEqual(result["state"], argocd_state.STATE_RECONCILABLE)
        self.assertEqual(len(result["reasons"]), 5)
        self.assertTrue(any(f"ingress/{argocd_state.INGRESS_NAME}" in r for r in result["reasons"]))

    def test_A6_repository_secret_drift_plus_missing_ingress_plus_unhealthy_core_is_broken(self):
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        cluster.objects.pop(("secret", "argocd-ecr-goldengate-oci", NAMESPACE))
        not_ready = _ready_replicaset_like("argocd-repo-server")
        not_ready["status"]["readyReplicas"] = 0
        cluster.put("deployment", "argocd-repo-server", NAMESPACE, not_ready)
        result = _classify(cluster, ingress_values=INGRESS_VALUES_ENABLED)
        self.assertEqual(result["state"], argocd_state.STATE_BROKEN)
        self.assertTrue(any("deployment/argocd-repo-server not ready" in r for r in result["reasons"]))

    def test_A7_wrong_canonical_host_group_certificate_are_each_detected(self):
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)

        wrong_host = _correct_ingress_obj()
        wrong_host["spec"]["rules"][0]["host"] = "wrong-host.example.com"
        cluster.put("ingress", argocd_state.INGRESS_NAME, NAMESPACE, wrong_host)
        result = _classify(cluster, ingress_values=INGRESS_VALUES_ENABLED)
        self.assertEqual(result["state"], argocd_state.STATE_RECONCILABLE)
        self.assertTrue(any("spec.rules[0].host" in r for r in result["reasons"]))

        wrong_group = _correct_ingress_obj()
        wrong_group["metadata"]["annotations"]["alb.ingress.kubernetes.io/group.name"] = "wrong-group"
        cluster.put("ingress", argocd_state.INGRESS_NAME, NAMESPACE, wrong_group)
        result = _classify(cluster, ingress_values=INGRESS_VALUES_ENABLED)
        self.assertEqual(result["state"], argocd_state.STATE_RECONCILABLE)
        self.assertTrue(any("group.name" in r for r in result["reasons"]))

        wrong_cert = _correct_ingress_obj()
        wrong_cert["metadata"]["annotations"]["alb.ingress.kubernetes.io/certificate-arn"] = "arn:aws:acm:eu-west-1:000000000000:certificate/wrong"
        cluster.put("ingress", argocd_state.INGRESS_NAME, NAMESPACE, wrong_cert)
        result = _classify(cluster, ingress_values=INGRESS_VALUES_ENABLED)
        self.assertEqual(result["state"], argocd_state.STATE_RECONCILABLE)
        self.assertTrue(any("certificate-arn" in r for r in result["reasons"]))

    def test_A8_final_state_cannot_be_healthy_while_desired_ingress_is_missing(self):
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        result = _classify(cluster, ingress_values=INGRESS_VALUES_ENABLED)
        self.assertNotEqual(result["state"], argocd_state.STATE_HEALTHY)

    def test_A9_no_load_balancer_address_after_reconciliation_window_is_reconcilable_not_healthy(self):
        # Correct Ingress spec/annotations but the AWS Load Balancer Controller has not yet published status.loadBalancer.ingress -- never falsely reported HEALTHY; classifies RECONCILABLE so 20-sub-argocd.yaml's own bounded wait gets a chance, but MAIN's final validate_argocd_ready step still requires the classifier to independently confirm HEALTHY afterward (never assumed from "reconciliation succeeded").
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        not_provisioned = _correct_ingress_obj()
        not_provisioned["status"] = {"loadBalancer": {}}
        cluster.put("ingress", argocd_state.INGRESS_NAME, NAMESPACE, not_provisioned)
        result = _classify(cluster, ingress_values=INGRESS_VALUES_ENABLED)
        self.assertEqual(result["state"], argocd_state.STATE_RECONCILABLE)
        self.assertNotEqual(result["state"], argocd_state.STATE_HEALTHY)
        self.assertTrue(any("status.loadBalancer.ingress is empty" in r for r in result["reasons"]))

    def test_A9b_missing_load_balancer_status_key_entirely_is_also_reconcilable(self):
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        no_status = _correct_ingress_obj()
        del no_status["status"]
        cluster.put("ingress", argocd_state.INGRESS_NAME, NAMESPACE, no_status)
        result = _classify(cluster, ingress_values=INGRESS_VALUES_ENABLED)
        self.assertEqual(result["state"], argocd_state.STATE_RECONCILABLE)

    def test_A10_classifier_contract_marker_is_v2(self):
        self.assertEqual(argocd_state.CLASSIFIER_CONTRACT, "argocd-recovery-v2")

    def test_shared_mode_never_expects_or_flags_absence_of_scheme_annotation(self):
        # A shared-mode Ingress must NOT carry alb.ingress.kubernetes.io/scheme at all (it would collide with the resident anchor's own value) -- absence there is correct, never drift.
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        shared_ingress = _correct_ingress_obj()
        del shared_ingress["metadata"]["annotations"]["alb.ingress.kubernetes.io/scheme"]
        cluster.put("ingress", argocd_state.INGRESS_NAME, NAMESPACE, shared_ingress)
        shared_values = dict(INGRESS_VALUES_ENABLED)
        shared_values["mode"] = "shared"
        result = _classify(cluster, ingress_values=shared_values)
        self.assertEqual(result["state"], argocd_state.STATE_HEALTHY)

    def test_backend_service_name_and_port_drift_are_each_detected(self):
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        wrong_backend = _correct_ingress_obj()
        wrong_backend["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["name"] = "some-other-service"
        cluster.put("ingress", argocd_state.INGRESS_NAME, NAMESPACE, wrong_backend)
        result = _classify(cluster, ingress_values=INGRESS_VALUES_ENABLED)
        self.assertEqual(result["state"], argocd_state.STATE_RECONCILABLE)
        self.assertTrue(any("backend service name" in r for r in result["reasons"]))

        wrong_port = _correct_ingress_obj()
        wrong_port["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["port"]["number"] = 8080
        cluster.put("ingress", argocd_state.INGRESS_NAME, NAMESPACE, wrong_port)
        result = _classify(cluster, ingress_values=INGRESS_VALUES_ENABLED)
        self.assertEqual(result["state"], argocd_state.STATE_RECONCILABLE)
        self.assertTrue(any("backend service port" in r for r in result["reasons"]))

    def test_ingress_config_from_values_reads_the_real_committed_dev_file(self):
        config = argocd_state.ingress_config_from_values("dev")
        self.assertTrue(config.get("enabled"))
        self.assertEqual(config.get("mode"), "standalone")
        self.assertEqual(config.get("scheme"), "internal")
        self.assertEqual(config.get("groupOrder"), "50")


class ArgoCdStateNoMutationSourceSweepTests(unittest.TestCase):
    """Static source-safety proof: the classifier module must never construct a mutating kubectl/helm command."""

    FORBIDDEN_SUBSTRINGS = (
        "kubectl apply", "kubectl create", "kubectl delete", "kubectl patch",
        "kubectl annotate", "kubectl label",
        "helm install", "helm upgrade", "helm uninstall",
        '"apply"', "'apply'", '"create"', "'create'", '"delete"', "'delete'",
        '"patch"', "'patch'", '"annotate"', "'annotate'", '"label"', "'label'",
    )

    def test_source_contains_no_mutating_command(self):
        with open(TOOL_PATH) as f:
            source = f.read()
        hits = [s for s in self.FORBIDDEN_SUBSTRINGS if s in source]
        self.assertEqual(hits, [], f"classifier source contains a mutating-looking construct: {hits}")

    def test_every_kubectl_get_json_call_uses_get_verb_only(self):
        # KubectlRunner itself only ever receives ["get", ...] argument lists from _get_json -- proven behaviorally above (FakeCluster asserts args[0] == "get" on every call across all scenarios), not merely by source inspection.
        cluster = FakeCluster()
        _populate_healthy_cluster(cluster)
        cluster.put("ingress", argocd_state.INGRESS_NAME, NAMESPACE, _correct_ingress_obj())
        _classify(cluster, ingress_values=INGRESS_VALUES_ENABLED)  # exercises every _get_json call site, including ingress


if __name__ == "__main__":
    unittest.main()
