"""Offline unit tests for automation/phases/phase3/phase3_argocd.py; run directly via `python3 automation/phases/phase3/tests/test_phase3_argocd.py`. Every aws/helm/kubectl call is scripted through a fake subprocess.run and time.sleep is stubbed to a no-op -- this suite never touches live AWS, ECR, EKS, or Kubernetes."""
from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[4]
TOOL_PATH = REPO_ROOT / "automation" / "phases" / "phase3" / "phase3_argocd.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("phase3_argocd", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


p3 = _load_tool()

ENVIRONMENT = "dev"
AWS_REGION = "eu-west-1"
ECR_ACCOUNT_ID = "229410149234"
WORKLOAD_ACCOUNT_ID = "668311715351"
ECR_REGISTRY = f"{ECR_ACCOUNT_ID}.dkr.ecr.{AWS_REGION}.amazonaws.com"
ARGOCD_ECR_READ_ROLE_ARN = f"arn:aws:iam::{WORKLOAD_ACCOUNT_ID}:role/GoldenGateArgocdECRRead-dev"
ARGOCD_HOST = "argocd.goldengate-dev.adcbmis.local"
ALB_GROUP_NAME = "gg-poc-dev-alb"
ACM_CERTIFICATE_ARN = f"arn:aws:acm:{AWS_REGION}:{WORKLOAD_ACCOUNT_ID}:certificate/9e53e28e-3243-47fc-85a1-50f9a94acde7"
ARGOCD_NAMESPACE = "argocd"
EKS_CLUSTER_NAME = "goldengate-dev"
EKS_CLUSTER_ARN = f"arn:aws:eks:{AWS_REGION}:{WORKLOAD_ACCOUNT_ID}:cluster/{EKS_CLUSTER_NAME}"
EKS_DEPLOY_ROLE_ARN = f"arn:aws:iam::{WORKLOAD_ACCOUNT_ID}:role/GoldenGateEKSDeployRole-dev"

BASE_ENV = {
    "AWS_REGION": AWS_REGION,
    "ECR_ACCOUNT_ID": ECR_ACCOUNT_ID,
    "ECR_REGISTRY": ECR_REGISTRY,
    "ARGOCD_ECR_READ_ROLE_ARN": ARGOCD_ECR_READ_ROLE_ARN,
    "ARGOCD_HOST": ARGOCD_HOST,
    "ALB_GROUP_NAME": ALB_GROUP_NAME,
    "ACM_CERTIFICATE_ARN": ACM_CERTIFICATE_ARN,
    "ARGOCD_NAMESPACE": ARGOCD_NAMESPACE,
    "EKS_CLUSTER_NAME": EKS_CLUSTER_NAME,
    "EKS_CLUSTER_ARN": EKS_CLUSTER_ARN,
    "WORKLOAD_ACCOUNT_ID": WORKLOAD_ACCOUNT_ID,
    "EKS_DEPLOY_ROLE_ARN": EKS_DEPLOY_ROLE_ARN,
    "GITHUB_RUN_NUMBER": "42",
    "GITHUB_RUN_ID": "1000",
    "GITHUB_RUN_ATTEMPT": "1",
}


class ScriptedSubprocess:
    """A stand-in for subprocess.run: dispatches on argv to a registered handler, records every call (plus its stdin `input`), and raises AssertionError on any unscripted invocation."""

    def __init__(self):
        self.calls = []
        self.inputs = []
        self._handlers = []

    def on(self, predicate, handler):
        self._handlers.append((predicate, handler))
        return self

    def __call__(self, argv, cwd=None, env=None, capture_output=True, text=True, input=None):
        self.calls.append(argv)
        self.inputs.append(input)
        for predicate, handler in self._handlers:
            if predicate(argv):
                return handler(argv, input)
        raise AssertionError(f"unscripted subprocess call: {argv!r}")


def _ok(stdout=""):
    return lambda argv, stdin: subprocess.CompletedProcess(argv, 0, stdout, "")


def _fail(stdout="", stderr="", returncode=1):
    return lambda argv, stdin: subprocess.CompletedProcess(argv, returncode, stdout, stderr)


GOOD_INGRESS_VALUES = {
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

DISABLED_INGRESS_VALUES = {"enabled": False}


def _rendered_manifest_text(ingress_enabled=True):
    ingress_doc = f"""
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: argocd-server-ingress
  namespace: {ARGOCD_NAMESPACE}
  annotations:
    alb.ingress.kubernetes.io/group.name: {ALB_GROUP_NAME}
    alb.ingress.kubernetes.io/group.order: "50"
    alb.ingress.kubernetes.io/certificate-arn: {ACM_CERTIFICATE_ARN}
    alb.ingress.kubernetes.io/listen-ports: '[{{"HTTPS":443}}]'
    alb.ingress.kubernetes.io/target-type: ip
    alb.ingress.kubernetes.io/backend-protocol: HTTPS
    alb.ingress.kubernetes.io/healthcheck-protocol: HTTPS
    alb.ingress.kubernetes.io/healthcheck-path: /healthz
    alb.ingress.kubernetes.io/healthcheck-port: traffic-port
    alb.ingress.kubernetes.io/scheme: internal
spec:
  ingressClassName: alb
  rules:
    - host: {ARGOCD_HOST}
      http:
        paths:
          - backend:
              service:
                name: argocd-server
                port:
                  number: 443
""" if ingress_enabled else ""
    return f"""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: argocd-server
spec:
  template:
    spec:
      containers:
        - name: argocd-server
          image: {ECR_REGISTRY}/aws-cloud-factory-infra-argocd:3.2.12
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: argocd-ecr-token-sync
spec:
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: argocd-ecr-token-sync
          containers:
            - name: ecr-token-sync
              image: {ECR_REGISTRY}/aws-cloud-factory-infra-aws-kubectl:1.33.13
              env:
                - name: REPOSITORIES
                  value: |
                    "helm/goldengate"
                    "helm/goldengate-monitor"
                    "helm/goldengate-platform"
                    "helm/amazon-cloudwatch-observability"
                - name: SECRETS
                  value: |
                    argocd-ecr-goldengate-oci
                    argocd-ecr-goldengate-monitor-oci
                    argocd-ecr-goldengate-platform-oci
                    argocd-ecr-amazon-cloudwatch-observability-oci
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: argocd-ecr-token-sync
  annotations:
    eks.amazonaws.com/role-arn: {ARGOCD_ECR_READ_ROLE_ARN}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: some-unrelated-role
rules:
  - resources: ["secrets"]
    resourceNames: ["something-else"]
    verbs: ["get", "delete", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: argocd-ecr-token-sync
rules:
  - resources: ["secrets"]
    resourceNames:
      - argocd-ecr-goldengate-oci
      - argocd-ecr-goldengate-monitor-oci
      - argocd-ecr-goldengate-platform-oci
      - argocd-ecr-amazon-cloudwatch-observability-oci
    verbs:
      - get
      - update
      - patch
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: argocd-ecr-token-sync
{ingress_doc}
"""


GOOD_VALUES_YAML = """
ecrTokenSync:
  enabled: true
  image:
    tag: "1.33.13"

argocdServerIngress:
  enabled: true
  mode: standalone
  ingressClassName: alb
  serviceName: argocd-server
  servicePort: 443
  groupOrder: "50"
  targetType: ip
  backendProtocol: HTTPS
  listenPorts: '[{"HTTPS":443}]'
  healthcheckProtocol: HTTPS
  healthcheckPath: /healthz
  healthcheckPort: traffic-port
  scheme: internal
"""

GOOD_POLICY_JSON = json.dumps({
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": f"AllowRead{name.replace('/', '').replace('-', '')}",
            "Effect": "Allow",
            "Action": sorted(p3.REQUIRED_ECR_POLICY_ACTIONS),
            "Resource": f"arn:aws:ecr:{AWS_REGION}:{ECR_ACCOUNT_ID}:repository/{name}",
        }
        for name in p3.REQUIRED_ECR_POLICY_REPOS
    ],
})


def _build_fake_repo(root, values_yaml=GOOD_VALUES_YAML, policy_json=GOOD_POLICY_JSON, declare_vendored=True):
    root = Path(root)
    argocd_chart = root / "helm" / "argocd"
    argocd_chart.mkdir(parents=True)
    (argocd_chart / "values.yaml").write_text("global: {}\n")
    chart_yaml_body = 'apiVersion: v2\nname: argocd\nversion: 1.0.1\n'
    if declare_vendored:
        chart_yaml_body += 'dependencies:\n  - name: argo-cd\n    repository: "file://charts/argo-cd"\n'
    (argocd_chart / "Chart.yaml").write_text(chart_yaml_body)

    vendored = argocd_chart / "charts" / "argo-cd"
    vendored.mkdir(parents=True)
    (vendored / "Chart.yaml").write_text("apiVersion: v2\nname: argo-cd\nversion: 9.3.7\n")

    values_dir = root / "envs" / ENVIRONMENT / "argocd"
    values_dir.mkdir(parents=True)
    (values_dir / "values.yaml").write_text(values_yaml)

    policy_dir = root / "envs" / ENVIRONMENT / "policies" / f"argocd-ecr-oci-read-{ENVIRONMENT}" / "policies"
    policy_dir.mkdir(parents=True)
    (policy_dir / "policies_1.json").write_text(policy_json)

    return root


class Phase3TestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)
        self.state_path = self.repo_root / "state.json"
        self.github_output = self.repo_root / "github_output.txt"
        self.github_summary = self.repo_root / "github_summary.txt"
        self.args = SimpleNamespace(state_path=self.state_path)

        repo_root_patch = mock.patch.object(p3, "REPO_ROOT", self.repo_root)
        repo_root_patch.start()
        self.addCleanup(repo_root_patch.stop)

        sleep_patch = mock.patch.object(p3.time, "sleep", lambda seconds: None)
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

        env_patch = {"GITHUB_OUTPUT": str(self.github_output), "GITHUB_STEP_SUMMARY": str(self.github_summary)}
        patcher = mock.patch.dict("os.environ", env_patch)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_subcommand(self, cmd_func, scripted=None, env_overrides=None, environment=ENVIRONMENT):
        env_overrides = dict(env_overrides or {})
        args = SimpleNamespace(state_path=self.state_path, environment=environment)
        with mock.patch.dict("os.environ", env_overrides):
            if scripted is not None:
                with mock.patch.object(p3.subprocess, "run", scripted):
                    cmd_func(args)
            else:
                cmd_func(args)
        return args

    def read_state(self):
        return p3.load_state(self.state_path)

    def read_outputs(self):
        pairs = {}
        if not self.github_output.exists():
            return pairs
        for line in self.github_output.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                pairs[key] = value
        return pairs

    def build_fake_repo(self, **kwargs):
        return _build_fake_repo(self.repo_root, **kwargs)


class TestEnvironmentSafety(Phase3TestCase):
    def test_unsafe_environment_rejected_by_every_environment_subcommand(self):
        for bad in ("../../etc", "dev/../../etc", "dev; rm -rf /", "$(whoami)", "DEV", "dev_env", "", "dev/x"):
            with self.assertRaises(p3.Phase3Error, msg=repr(bad)):
                p3.require_environment_arg(bad)

    def test_safe_environment_accepted(self):
        self.assertEqual(p3.require_environment_arg("dev"), "dev")


class TestStateFile(Phase3TestCase):
    def test_disallowed_state_key_rejected(self):
        with self.assertRaises(p3.Phase3Error):
            p3.update_state(self.state_path, {"AWS_ACCESS_KEY_ID": "should-never-be-written"})
        self.assertEqual(self.read_state(), {})

    def test_malformed_state_fails_closed(self):
        self.state_path.write_text("not valid json", encoding="utf-8")
        with self.assertRaises(p3.Phase3Error):
            p3.load_state(self.state_path)

    def test_state_is_not_a_json_object_fails_closed(self):
        self.state_path.write_text("[1, 2, 3]", encoding="utf-8")
        with self.assertRaises(p3.Phase3Error):
            p3.load_state(self.state_path)

    def test_state_write_is_atomic_via_tmp_replace(self):
        p3.update_state(self.state_path, {"environment": "dev"})
        self.assertFalse(self.state_path.with_suffix(".json.tmp").exists())
        self.assertEqual(self.read_state()["environment"], "dev")

    def test_missing_required_state_key_fails_closed(self):
        with self.assertRaises(p3.Phase3Error):
            p3.require_state_value({}, "values_file")

    def test_no_credential_shaped_values_ever_written_to_state(self):
        with open(TOOL_PATH, encoding="utf-8") as f:
            lines = f.read().splitlines()
        forbidden = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "GITHUB_TOKEN", "PASSWORD", "PRIVATE_KEY")
        leaking = [line for line in lines if "update_state(" in line and any(k in line.upper() for k in forbidden)]
        self.assertEqual(leaking, [])

    def test_no_credential_shaped_values_ever_written_to_github_output(self):
        with open(TOOL_PATH, encoding="utf-8") as f:
            lines = f.read().splitlines()
        forbidden = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "GITHUB_TOKEN", "PASSWORD", "PRIVATE_KEY")
        leaking = [line for line in lines if "write_github_output(" in line and any(k in line.upper() for k in forbidden)]
        self.assertEqual(leaking, [])


class TestOwnershipPreflight(Phase3TestCase):
    def _scripted(self, classifier_result):
        return (ScriptedSubprocess()
                .on(lambda argv: argv[:3] == ["aws", "eks", "update-kubeconfig"], _ok(""))
                .on(lambda argv: str(p3.ARGOCD_STATE_TOOL) in argv, classifier_result))

    def test_absent_and_owned_are_accepted_and_published(self):
        for state in ("ABSENT", "OWNED"):
            scripted = self._scripted(_ok(json.dumps({"state": state, "environment": ENVIRONMENT, "namespace": ARGOCD_NAMESPACE, "reasons": [], "checks": {}})))
            self.run_subcommand(p3.cmd_ownership_preflight, scripted=scripted, env_overrides=BASE_ENV)
            self.assertEqual(self.read_outputs()["state"], state)
            self.github_output.unlink()

    def test_broken_fails_closed(self):
        scripted = self._scripted(_ok(json.dumps({"state": "BROKEN", "environment": ENVIRONMENT, "namespace": ARGOCD_NAMESPACE, "reasons": ["foreign ownership"], "checks": {}})))
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_ownership_preflight, scripted=scripted, env_overrides=BASE_ENV)
        self.assertNotIn("state", self.read_outputs())

    def test_classifier_inspection_error_fails_closed_never_absent(self):
        scripted = self._scripted(_fail(stderr="INSPECTION ERROR: kubectl: connection refused", returncode=1))
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_ownership_preflight, scripted=scripted, env_overrides=BASE_ENV)
        self.assertNotIn("state", self.read_outputs())

    def test_unparseable_classifier_output_fails_closed(self):
        scripted = self._scripted(_ok("not valid json"))
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_ownership_preflight, scripted=scripted, env_overrides=BASE_ENV)

    def test_unrecognized_state_fails_closed(self):
        scripted = self._scripted(_ok(json.dumps({"state": "SOMETHING_ELSE", "environment": ENVIRONMENT, "namespace": ARGOCD_NAMESPACE, "reasons": [], "checks": {}})))
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_ownership_preflight, scripted=scripted, env_overrides=BASE_ENV)

    def test_connects_to_eks_using_cross_account_deploy_role_before_classifying(self):
        scripted = self._scripted(_ok(json.dumps({"state": "OWNED", "environment": ENVIRONMENT, "namespace": ARGOCD_NAMESPACE, "reasons": [], "checks": {}})))
        self.run_subcommand(p3.cmd_ownership_preflight, scripted=scripted, env_overrides=BASE_ENV)
        update_kubeconfig_call = scripted.calls[0]
        self.assertEqual(update_kubeconfig_call[:3], ["aws", "eks", "update-kubeconfig"])
        self.assertIn(EKS_DEPLOY_ROLE_ARN, update_kubeconfig_call)
        self.assertIn(EKS_CLUSTER_NAME, update_kubeconfig_call)
        self.assertIn("--role-arn", update_kubeconfig_call)
        self.assertIn("--assume-role-arn", update_kubeconfig_call)

    def test_only_the_canonical_state_output_is_ever_published(self):
        scripted = self._scripted(_ok(json.dumps({"state": "OWNED", "environment": ENVIRONMENT, "namespace": ARGOCD_NAMESPACE, "reasons": [], "checks": {}})))
        self.run_subcommand(p3.cmd_ownership_preflight, scripted=scripted, env_overrides=BASE_ENV)
        self.assertEqual(set(self.read_outputs().keys()), {"state"})


class TestStrictAcceptance(Phase3TestCase):
    def _scripted(self, classifier_result):
        return (ScriptedSubprocess()
                .on(lambda argv: argv[:3] == ["aws", "eks", "update-kubeconfig"], _ok(""))
                .on(lambda argv: str(p3.ARGOCD_ACCEPTANCE_TOOL) in argv, classifier_result))

    def test_healthy_is_accepted(self):
        scripted = self._scripted(_ok(json.dumps({"state": "HEALTHY", "environment": ENVIRONMENT, "namespace": ARGOCD_NAMESPACE, "reasons": [], "checks": {}})))
        self.run_subcommand(p3.cmd_strict_acceptance, scripted=scripted, env_overrides=BASE_ENV)

    def test_broken_is_rejected(self):
        scripted = self._scripted(_ok(json.dumps({"state": "BROKEN", "environment": ENVIRONMENT, "namespace": ARGOCD_NAMESPACE, "reasons": ["deployment/argocd-server not ready"], "checks": {}})))
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_strict_acceptance, scripted=scripted, env_overrides=BASE_ENV)

    def test_reconciliation_success_alone_is_never_sufficient(self):
        # Even though this call simulates a scenario where reconcile_argocd "succeeded", a fresh BROKEN re-classification must still fail closed.
        scripted = self._scripted(_ok(json.dumps({"state": "BROKEN", "environment": ENVIRONMENT, "namespace": ARGOCD_NAMESPACE, "reasons": ["ingress/argocd-server-ingress status.loadBalancer.ingress is empty"], "checks": {}})))
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_strict_acceptance, scripted=scripted, env_overrides=BASE_ENV)

    def test_classifier_inspection_error_fails_closed(self):
        scripted = self._scripted(_fail(stderr="INSPECTION ERROR: permission denied", returncode=1))
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_strict_acceptance, scripted=scripted, env_overrides=BASE_ENV)


class TestPrepareDeployment(Phase3TestCase):
    def test_chart_version_format(self):
        self.run_subcommand(p3.cmd_prepare_deployment, env_overrides=BASE_ENV)
        state = self.read_state()
        self.assertEqual(state["chart_version"], "0.1.42")
        self.assertEqual(state["values_file"], "envs/dev/argocd/values.yaml")
        self.assertEqual(state["helm_ecr_repository"], "helm/argocd")
        self.assertEqual(state["helm_push_url"], f"oci://{ECR_REGISTRY}/helm")
        self.assertEqual(state["helm_chart_ref"], f"oci://{ECR_REGISTRY}/helm/argocd")

    def test_missing_ecr_registry_fails_closed(self):
        env = dict(BASE_ENV)
        env.pop("ECR_REGISTRY")
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_prepare_deployment, env_overrides=env)

    def test_state_contains_only_allowed_keys(self):
        self.run_subcommand(p3.cmd_prepare_deployment, env_overrides=BASE_ENV)
        self.assertTrue(set(self.read_state().keys()).issubset(p3.ALLOWED_STATE_KEYS))


class TestValidateLocal(Phase3TestCase):
    def _prime_state(self):
        p3.update_state(self.state_path, {"values_file": f"envs/{ENVIRONMENT}/argocd/values.yaml", "chart_version": "0.1.42"})

    def _scripted_ok(self, rendered_text=None):
        rendered_text = rendered_text if rendered_text is not None else _rendered_manifest_text()

        def package_side_effect(argv, stdin):
            dest_idx = argv.index("--destination")
            dest = Path(argv[dest_idx + 1])
            (self.repo_root / dest).mkdir(parents=True, exist_ok=True)
            (self.repo_root / dest / "argocd-0.1.42.tgz").write_bytes(b"fake-chart")
            return subprocess.CompletedProcess(argv, 0, "", "")

        return (ScriptedSubprocess()
                .on(lambda argv: argv[:3] == ["helm", "dependency", "build"], _ok(""))
                .on(lambda argv: argv[:2] == ["helm", "lint"], _ok(""))
                .on(lambda argv: argv[:2] == ["helm", "template"], _ok(rendered_text))
                .on(lambda argv: argv[:2] == ["helm", "package"], package_side_effect))

    def test_missing_vendored_dependency_fails_closed(self):
        self.build_fake_repo()
        import shutil
        shutil.rmtree(self.repo_root / "helm" / "argocd" / "charts")
        self._prime_state()
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_validate_local, scripted=self._scripted_ok(), env_overrides=BASE_ENV)

    def test_wrapper_chart_missing_file_dependency_declaration_fails_closed(self):
        self.build_fake_repo(declare_vendored=False)
        self._prime_state()
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_validate_local, scripted=self._scripted_ok(), env_overrides=BASE_ENV)

    def test_missing_environment_values_file_fails_closed(self):
        self.build_fake_repo()
        (self.repo_root / "envs" / ENVIRONMENT / "argocd" / "values.yaml").unlink()
        self._prime_state()
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_validate_local, scripted=self._scripted_ok(), env_overrides=BASE_ENV)

    def test_ecr_policy_missing_action_fails_closed(self):
        bad_policy = json.dumps({"Statement": [
            {"Resource": f"arn:aws:ecr:{AWS_REGION}:{ECR_ACCOUNT_ID}:repository/helm/goldengate", "Action": ["ecr:BatchGetImage"]},
        ]})
        self.build_fake_repo(policy_json=bad_policy)
        self._prime_state()
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_validate_local, scripted=self._scripted_ok(), env_overrides=BASE_ENV)

    def test_ecr_policy_wildcard_resource_fails_closed(self):
        bad_policy = json.dumps({"Statement": [
            {"Resource": "*", "Action": sorted(p3.REQUIRED_ECR_POLICY_ACTIONS)},
        ]})
        self.build_fake_repo(policy_json=bad_policy)
        self._prime_state()
        # A wildcard "*" never matches an expected exact-repo ARN, so this manifests as a missing-repo failure -- still fail-closed either way.
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_validate_local, scripted=self._scripted_ok(), env_overrides=BASE_ENV)

    def test_ecr_policy_missing_repo_fails_closed(self):
        policy = json.loads(GOOD_POLICY_JSON)
        policy["Statement"] = policy["Statement"][:-1]
        self.build_fake_repo(policy_json=json.dumps(policy))
        self._prime_state()
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_validate_local, scripted=self._scripted_ok(), env_overrides=BASE_ENV)

    def test_ecr_policy_exact_four_repos_pass(self):
        self.build_fake_repo()
        self._prime_state()
        self.run_subcommand(p3.cmd_validate_local, scripted=self._scripted_ok(), env_overrides=BASE_ENV)
        self.assertEqual(self.read_state()["package_path"], "packaged/argocd-0.1.42.tgz")

    def test_helm_dependency_build_failure_is_tolerated_not_shell_or_true(self):
        self.build_fake_repo()
        self._prime_state()
        scripted = self._scripted_ok()
        scripted._handlers[0] = (lambda argv: argv[:3] == ["helm", "dependency", "build"], _fail(stderr="no repositories configured", returncode=1))
        self.run_subcommand(p3.cmd_validate_local, scripted=scripted, env_overrides=BASE_ENV)
        with open(TOOL_PATH) as f:
            source = f.read()
        self.assertNotIn("|| true", source)

    def test_helm_lint_failure_fails_closed(self):
        self.build_fake_repo()
        self._prime_state()
        scripted = self._scripted_ok()
        scripted._handlers[1] = (lambda argv: argv[:2] == ["helm", "lint"], _fail(stderr="lint error", returncode=1))
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_validate_local, scripted=scripted, env_overrides=BASE_ENV)

    def test_helm_template_failure_fails_closed(self):
        self.build_fake_repo()
        self._prime_state()
        scripted = self._scripted_ok()
        scripted._handlers[2] = (lambda argv: argv[:2] == ["helm", "template"], _fail(stderr="template error", returncode=1))
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_validate_local, scripted=scripted, env_overrides=BASE_ENV)

    def test_rendered_manifest_missing_cronjob_fails_closed(self):
        self.build_fake_repo()
        self._prime_state()
        broken_manifest = _rendered_manifest_text().replace("kind: CronJob", "kind: Something")
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_validate_local, scripted=self._scripted_ok(broken_manifest), env_overrides=BASE_ENV)

    def test_rendered_manifest_uses_the_correctly_named_role_not_first_role(self):
        self.build_fake_repo()
        self._prime_state()
        # GOOD_RENDERED_MANIFEST deliberately renders an unrelated Role first -- proves the extractor selects the argocd-ecr-token-sync Role specifically, not merely "the first Role".
        self.run_subcommand(p3.cmd_validate_local, scripted=self._scripted_ok(), env_overrides=BASE_ENV)

    def test_rendered_role_missing_resource_name_fails_closed(self):
        self.build_fake_repo()
        self._prime_state()
        broken_manifest = _rendered_manifest_text().replace("      - argocd-ecr-goldengate-oci\n", "")
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_validate_local, scripted=self._scripted_ok(broken_manifest), env_overrides=BASE_ENV)

    def test_rendered_role_forbidden_verb_fails_closed(self):
        self.build_fake_repo()
        self._prime_state()
        broken_manifest = _rendered_manifest_text().replace("      - patch\n", "      - patch\n      - delete\n")
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_validate_local, scripted=self._scripted_ok(broken_manifest), env_overrides=BASE_ENV)

    def test_ingress_disabled_skips_rendered_ingress_validation(self):
        self.build_fake_repo(values_yaml=GOOD_VALUES_YAML.replace("enabled: true", "enabled: false", 1).replace(
            "argocdServerIngress:\n  enabled: false", "argocdServerIngress:\n  enabled: false"))
        self._prime_state()
        disabled_values_yaml = GOOD_VALUES_YAML.replace(
            "argocdServerIngress:\n  enabled: true", "argocdServerIngress:\n  enabled: false")
        (self.repo_root / "envs" / ENVIRONMENT / "argocd" / "values.yaml").write_text(disabled_values_yaml)
        manifest_without_ingress = _rendered_manifest_text(ingress_enabled=False)
        self.run_subcommand(p3.cmd_validate_local, scripted=self._scripted_ok(manifest_without_ingress), env_overrides=BASE_ENV)
        self.assertFalse(self.read_state()["ingress_enabled"])

    def test_ingress_enabled_and_correct_passes(self):
        self.build_fake_repo()
        self._prime_state()
        self.run_subcommand(p3.cmd_validate_local, scripted=self._scripted_ok(), env_overrides=BASE_ENV)
        self.assertTrue(self.read_state()["ingress_enabled"])

    def test_ingress_host_mismatch_fails_closed(self):
        self.build_fake_repo()
        self._prime_state()
        broken_manifest = _rendered_manifest_text().replace(ARGOCD_HOST, "wrong-host.example.com")
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_validate_local, scripted=self._scripted_ok(broken_manifest), env_overrides=BASE_ENV)

    def test_ingress_alb_group_mismatch_fails_closed(self):
        self.build_fake_repo()
        self._prime_state()
        broken_manifest = _rendered_manifest_text().replace(ALB_GROUP_NAME, "some-other-group")
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_validate_local, scripted=self._scripted_ok(broken_manifest), env_overrides=BASE_ENV)

    def test_ingress_certificate_arn_mismatch_fails_closed(self):
        self.build_fake_repo()
        self._prime_state()
        broken_manifest = _rendered_manifest_text().replace(ACM_CERTIFICATE_ARN, "arn:aws:acm:eu-west-1:000000000000:certificate/wrong")
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_validate_local, scripted=self._scripted_ok(broken_manifest), env_overrides=BASE_ENV)

    def test_standalone_scheme_mismatch_fails_closed(self):
        self.build_fake_repo()
        self._prime_state()
        broken_manifest = _rendered_manifest_text().replace("alb.ingress.kubernetes.io/scheme: internal", "alb.ingress.kubernetes.io/scheme: internet-facing")
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_validate_local, scripted=self._scripted_ok(broken_manifest), env_overrides=BASE_ENV)

    def test_public_registry_image_rejected(self):
        self.build_fake_repo()
        self._prime_state()
        broken_manifest = _rendered_manifest_text().replace(f"{ECR_REGISTRY}/aws-cloud-factory-infra-argocd:3.2.12", "quay.io/argoproj/argocd:v3.2.12")
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_validate_local, scripted=self._scripted_ok(broken_manifest), env_overrides=BASE_ENV)

    def test_unresolved_placeholder_image_rejected(self):
        self.build_fake_repo()
        self._prime_state()
        broken_manifest = _rendered_manifest_text().replace("3.2.12", "<ARGOCD_IMAGE_TAG>")
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_validate_local, scripted=self._scripted_ok(broken_manifest), env_overrides=BASE_ENV)

    def test_private_ecr_only_images_pass(self):
        self.build_fake_repo()
        self._prime_state()
        self.run_subcommand(p3.cmd_validate_local, scripted=self._scripted_ok(), env_overrides=BASE_ENV)

    def test_no_image_references_found_fails_closed(self):
        self.build_fake_repo()
        self._prime_state()
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_validate_local, scripted=self._scripted_ok("kind: ConfigMap\n"), env_overrides=BASE_ENV)

    def test_no_live_aws_or_kubectl_calls_during_validate_local(self):
        self.build_fake_repo()
        self._prime_state()
        scripted = self._scripted_ok()
        self.run_subcommand(p3.cmd_validate_local, scripted=scripted, env_overrides=BASE_ENV)
        for call in scripted.calls:
            self.assertNotEqual(call[0], "aws")
            self.assertNotEqual(call[0], "kubectl")


class TestPublishChart(Phase3TestCase):
    def _prime_state(self):
        (self.repo_root / "packaged").mkdir()
        (self.repo_root / "packaged" / "argocd-0.1.42.tgz").write_bytes(b"fake-chart")
        p3.update_state(self.state_path, {
            "chart_version": "0.1.42",
            "package_path": "packaged/argocd-0.1.42.tgz",
            "helm_push_url": f"oci://{ECR_REGISTRY}/helm",
            "helm_ecr_repository": "helm/argocd",
            "helm_chart_ref": f"oci://{ECR_REGISTRY}/helm/argocd",
        })

    def _scripted(self, repo_exists=True):
        return (ScriptedSubprocess()
                .on(lambda argv: argv[:3] == ["aws", "sts", "get-caller-identity"], _ok("{}"))
                .on(lambda argv: argv[:3] == ["aws", "ecr", "get-login-password"], _ok("super-secret-password\n"))
                .on(lambda argv: argv[:3] == ["helm", "registry", "login"], _ok(""))
                .on(lambda argv: argv[:3] == ["aws", "ecr", "describe-repositories"], _ok("") if repo_exists else _fail(returncode=1))
                .on(lambda argv: argv[:3] == ["aws", "ecr", "create-repository"], _ok(""))
                .on(lambda argv: argv[:2] == ["helm", "push"], _ok(""))
                .on(lambda argv: argv[:2] == ["helm", "pull"], _ok("")))

    def test_password_passed_only_via_stdin_never_as_an_argument(self):
        self._prime_state()
        scripted = self._scripted()
        self.run_subcommand(p3.cmd_publish_chart, scripted=scripted, env_overrides=BASE_ENV)
        login_call = next(c for c in scripted.calls if c[:3] == ["helm", "registry", "login"])
        self.assertNotIn("super-secret-password", login_call)
        login_index = scripted.calls.index(login_call)
        self.assertEqual(scripted.inputs[login_index], "super-secret-password")

    def test_password_never_appears_in_state(self):
        self._prime_state()
        self.run_subcommand(p3.cmd_publish_chart, scripted=self._scripted(), env_overrides=BASE_ENV)
        state_text = self.state_path.read_text()
        self.assertNotIn("super-secret-password", state_text)

    def test_existing_ecr_repository_is_not_recreated(self):
        self._prime_state()
        scripted = self._scripted(repo_exists=True)
        self.run_subcommand(p3.cmd_publish_chart, scripted=scripted, env_overrides=BASE_ENV)
        self.assertFalse(any(c[:3] == ["aws", "ecr", "create-repository"] for c in scripted.calls))

    def test_missing_ecr_repository_is_created_with_expected_tags_and_settings(self):
        self._prime_state()
        scripted = self._scripted(repo_exists=False)
        self.run_subcommand(p3.cmd_publish_chart, scripted=scripted, env_overrides=BASE_ENV)
        create_call = next(c for c in scripted.calls if c[:3] == ["aws", "ecr", "create-repository"])
        self.assertIn("scanOnPush=true", create_call)
        self.assertIn("MUTABLE", create_call)

    def test_publish_failure_fails_closed(self):
        self._prime_state()
        scripted = self._scripted()
        scripted._handlers[5] = (lambda argv: argv[:2] == ["helm", "push"], _fail(stderr="push failed", returncode=1))
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_publish_chart, scripted=scripted, env_overrides=BASE_ENV)

    def test_pulls_back_the_exact_published_version(self):
        self._prime_state()
        scripted = self._scripted()
        self.run_subcommand(p3.cmd_publish_chart, scripted=scripted, env_overrides=BASE_ENV)
        pull_call = next(c for c in scripted.calls if c[:2] == ["helm", "pull"])
        self.assertIn("0.1.42", pull_call)
        self.assertIn(f"oci://{ECR_REGISTRY}/helm/argocd", pull_call)


class TestReconcileCluster(Phase3TestCase):
    def _prime_state(self):
        p3.update_state(self.state_path, {
            "values_file": f"envs/{ENVIRONMENT}/argocd/values.yaml",
            "chart_version": "0.1.42",
            "helm_chart_ref": f"oci://{ECR_REGISTRY}/helm/argocd",
            "namespace": ARGOCD_NAMESPACE,
        })

    def _scripted(self):
        return (ScriptedSubprocess()
                .on(lambda argv: argv[:3] == ["aws", "sts", "get-caller-identity"], _ok("{}"))
                .on(lambda argv: argv[:3] == ["aws", "eks", "update-kubeconfig"], _ok(""))
                .on(lambda argv: argv[:2] == ["kubectl", "config"], _ok(""))
                .on(lambda argv: argv[:2] == ["kubectl", "version"], _ok(""))
                .on(lambda argv: argv[:3] == ["kubectl", "auth", "can-i"], _ok(""))
                .on(lambda argv: argv[:3] == ["kubectl", "create", "namespace"], _ok("kind: Namespace\n"))
                .on(lambda argv: argv[:3] == ["kubectl", "apply", "-f"], _ok(""))
                .on(lambda argv: argv[:2] == ["kubectl", "label"], _ok(""))
                .on(lambda argv: argv[:2] == ["kubectl", "get"], _ok(""))
                .on(lambda argv: argv[:2] == ["helm", "upgrade"], _ok("")))

    def test_connects_using_exact_role_cluster_and_region(self):
        self._prime_state()
        scripted = self._scripted()
        self.run_subcommand(p3.cmd_reconcile_cluster, scripted=scripted, env_overrides=BASE_ENV)
        update_kubeconfig_call = next(c for c in scripted.calls if c[:3] == ["aws", "eks", "update-kubeconfig"])
        self.assertIn(AWS_REGION, update_kubeconfig_call)
        self.assertIn(EKS_CLUSTER_NAME, update_kubeconfig_call)
        self.assertEqual(update_kubeconfig_call.count(EKS_DEPLOY_ROLE_ARN), 2)

    def test_namespace_created_via_apply_stdin_never_a_shell_pipe(self):
        self._prime_state()
        scripted = self._scripted()
        self.run_subcommand(p3.cmd_reconcile_cluster, scripted=scripted, env_overrides=BASE_ENV)
        apply_index = next(i for i, c in enumerate(scripted.calls) if c[:3] == ["kubectl", "apply", "-f"])
        self.assertEqual(scripted.inputs[apply_index], "kind: Namespace\n")
        with open(TOOL_PATH) as f:
            source = f.read()
        self.assertNotIn("shell=True", source)

    def test_namespace_labeled_with_exact_labels(self):
        self._prime_state()
        scripted = self._scripted()
        self.run_subcommand(p3.cmd_reconcile_cluster, scripted=scripted, env_overrides=BASE_ENV)
        label_call = next(c for c in scripted.calls if c[:2] == ["kubectl", "label"])
        self.assertIn("app.kubernetes.io/name=argocd", label_call)
        self.assertIn("app.kubernetes.io/managed-by=github-actions", label_call)
        self.assertIn(f"goldengate.adcb/environment={ENVIRONMENT}", label_call)
        self.assertIn("--overwrite", label_call)

    def test_helm_upgrade_uses_exact_preserved_flags(self):
        self._prime_state()
        scripted = self._scripted()
        self.run_subcommand(p3.cmd_reconcile_cluster, scripted=scripted, env_overrides=BASE_ENV)
        upgrade_call = next(c for c in scripted.calls if c[:2] == ["helm", "upgrade"])
        for flag in ("--install", "--wait", "--atomic", "--cleanup-on-fail"):
            self.assertIn(flag, upgrade_call)
        self.assertIn("--timeout", upgrade_call)
        self.assertIn("15m", upgrade_call)
        self.assertIn("0.1.42", upgrade_call)

    def test_helm_upgrade_failure_fails_closed(self):
        self._prime_state()
        scripted = self._scripted()
        scripted._handlers[-1] = (lambda argv: argv[:2] == ["helm", "upgrade"], _fail(stderr="upgrade failed", returncode=1))
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_reconcile_cluster, scripted=scripted, env_overrides=BASE_ENV)


class TestPostDeployValidation(Phase3TestCase):
    def _prime_state(self, ingress_enabled=True):
        self.build_fake_repo(values_yaml=GOOD_VALUES_YAML if ingress_enabled else GOOD_VALUES_YAML.replace(
            "argocdServerIngress:\n  enabled: true", "argocdServerIngress:\n  enabled: false"))
        p3.update_state(self.state_path, {"namespace": ARGOCD_NAMESPACE, "values_file": f"envs/{ENVIRONMENT}/argocd/values.yaml"})

    def _base_scripted(self):
        return (ScriptedSubprocess()
                .on(lambda argv: argv[:3] == ["kubectl", "rollout", "status"], _ok(""))
                .on(lambda argv: argv[:2] == ["kubectl", "get"] and argv[2] in ("pods", "svc", "deploy", "statefulset", "crd"), _ok(""))
                .on(lambda argv: argv[:3] == ["kubectl", "get", "serviceaccount"], _ok(""))
                .on(lambda argv: argv[:3] == ["kubectl", "get", "role"], _ok(""))
                .on(lambda argv: argv[:3] == ["kubectl", "get", "rolebinding"], _ok(""))
                .on(lambda argv: argv[:3] == ["kubectl", "get", "cronjob"], _ok(""))
                .on(lambda argv: argv[:3] == ["helm", "get", "values"], _ok(""))
                .on(lambda argv: argv[:3] == ["helm", "get", "manifest"], _ok("")))

    def test_rollout_failure_fails_closed(self):
        self._prime_state(ingress_enabled=False)
        scripted = self._base_scripted()
        scripted._handlers[0] = (lambda argv: argv[:3] == ["kubectl", "rollout", "status"], _fail(stderr="deployment exceeded progress deadline", returncode=1))
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_post_deploy_validation, scripted=scripted, env_overrides=BASE_ENV)

    def test_missing_ecr_token_sync_resource_fails_closed(self):
        self._prime_state(ingress_enabled=False)
        scripted = self._base_scripted()
        scripted._handlers[3] = (lambda argv: argv[:3] == ["kubectl", "get", "role"], _fail(returncode=1))
        with self.assertRaises(p3.Phase3Error):
            self.run_subcommand(p3.cmd_post_deploy_validation, scripted=scripted, env_overrides=BASE_ENV)

    def _secret_handler(self, argv, stdin):
        if "labels.argocd" in "".join(argv):
            return subprocess.CompletedProcess(argv, 0, "repository", "")
        if argv[-2:] == ["-o", "jsonpath={.data.url}"]:
            for name, repo in p3.REQUIRED_REPO_SECRETS.items():
                if name in argv:
                    return subprocess.CompletedProcess(argv, 0, base64.b64encode(f"oci://{ECR_REGISTRY}/{repo}".encode()).decode(), "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def test_ingress_disabled_skips_wait(self):
        self._prime_state(ingress_enabled=False)
        scripted = self._base_scripted()
        scripted.on(lambda argv: argv[:3] == ["kubectl", "create", "job"], _ok(""))
        scripted.on(lambda argv: "jsonpath={.status.succeeded}" in argv, _ok("1"))
        scripted.on(lambda argv: "jsonpath={.status.failed}" in argv, _ok(""))
        scripted.on(lambda argv: argv[:2] == ["kubectl", "logs"], _ok(""))
        scripted.on(lambda argv: argv[:3] == ["kubectl", "get", "secret"], self._secret_handler)
        scripted.on(lambda argv: argv[:3] == ["kubectl", "delete", "job"], _ok(""))
        scripted.on(lambda argv: argv[:3] == ["kubectl", "get", "ingress"], _ok(""))
        self.run_subcommand(p3.cmd_post_deploy_validation, scripted=scripted, env_overrides=BASE_ENV)
        self.assertFalse(any(c[:3] == ["kubectl", "get", "ingress"] for c in scripted.calls))


class TestWaitForIngressReady(Phase3TestCase):
    def _run_wait(self, kubectl_handler):
        scripted = ScriptedSubprocess().on(lambda argv: argv[0] == "kubectl", kubectl_handler)
        with mock.patch.object(p3.subprocess, "run", scripted):
            p3._wait_for_ingress_ready(ARGOCD_NAMESPACE, ARGOCD_HOST, GOOD_INGRESS_VALUES)
        return scripted

    def test_disabled_ingress_never_calls_kubectl(self):
        scripted = ScriptedSubprocess()
        with mock.patch.object(p3.subprocess, "run", scripted):
            p3._wait_for_ingress_ready(ARGOCD_NAMESPACE, ARGOCD_HOST, DISABLED_INGRESS_VALUES)
        self.assertEqual(scripted.calls, [])

    def test_address_appears_immediately_zero_sleeps(self):
        sleep_calls = []
        with mock.patch.object(p3.time, "sleep", lambda s: sleep_calls.append(s)):
            def handler(argv, stdin):
                if "jsonpath={.spec.rules[0].host}" in argv:
                    return subprocess.CompletedProcess(argv, 0, ARGOCD_HOST, "")
                if any("loadBalancer" in a for a in argv):
                    return subprocess.CompletedProcess(argv, 0, "lb.example.com", "")
                return subprocess.CompletedProcess(argv, 0, "", "")
            self._run_wait(handler)
        self.assertEqual(sleep_calls, [])

    def test_host_mismatch_fails_immediately_with_zero_sleeps(self):
        sleep_calls = []
        with mock.patch.object(p3.time, "sleep", lambda s: sleep_calls.append(s)):
            def handler(argv, stdin):
                if "jsonpath={.spec.rules[0].host}" in argv:
                    return subprocess.CompletedProcess(argv, 0, "wrong.example.com", "")
                return subprocess.CompletedProcess(argv, 0, "", "")
            with self.assertRaises(p3.Phase3Error) as ctx:
                self._run_wait(handler)
        self.assertIn("live desired-state mismatch, never a transient readiness gap", str(ctx.exception))
        self.assertEqual(sleep_calls, [])

    def test_never_appears_times_out_after_exactly_60_sleeps(self):
        sleep_calls = []
        with mock.patch.object(p3.time, "sleep", lambda s: sleep_calls.append(s)):
            def handler(argv, stdin):
                if "jsonpath={.spec.rules[0].host}" in argv:
                    return subprocess.CompletedProcess(argv, 0, ARGOCD_HOST, "")
                return subprocess.CompletedProcess(argv, 0, "", "")
            with self.assertRaises(p3.Phase3Error):
                self._run_wait(handler)
        self.assertEqual(len(sleep_calls), 60)
        self.assertTrue(all(s == 15 for s in sleep_calls))

    def test_address_appears_exactly_on_final_probe_at_t900(self):
        poll_count = {"n": 0}
        with mock.patch.object(p3.time, "sleep", lambda s: None):
            def handler(argv, stdin):
                if "jsonpath={.spec.rules[0].host}" in argv:
                    return subprocess.CompletedProcess(argv, 0, ARGOCD_HOST, "")
                if any("loadBalancer" in a for a in argv):
                    poll_count["n"] += 1
                    if poll_count["n"] >= 61:
                        return subprocess.CompletedProcess(argv, 0, "lb.example.com", "")
                    return subprocess.CompletedProcess(argv, 0, "", "")
                return subprocess.CompletedProcess(argv, 0, "", "")
            self._run_wait(handler)
        self.assertEqual(poll_count["n"], 61)


class TestEcrTokenSyncVerification(Phase3TestCase):
    def _good_secret_handler(self, argv, stdin):
        if "labels.argocd" in "".join(argv):
            return subprocess.CompletedProcess(argv, 0, "repository", "")
        if argv[-2:] == ["-o", "jsonpath={.data.url}"]:
            for name, repo in p3.REQUIRED_REPO_SECRETS.items():
                if name in argv:
                    return subprocess.CompletedProcess(argv, 0, base64.b64encode(f"oci://{ECR_REGISTRY}/{repo}".encode()).decode(), "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def test_job_name_sanitized_lowercase_bounded_and_includes_run_attempt(self):
        scripted = (ScriptedSubprocess()
                    .on(lambda argv: argv[:3] == ["kubectl", "create", "job"], _ok(""))
                    .on(lambda argv: "jsonpath={.status.succeeded}" in argv, _ok("1"))
                    .on(lambda argv: "jsonpath={.status.failed}" in argv, _ok(""))
                    .on(lambda argv: argv[:2] == ["kubectl", "logs"], _ok(""))
                    .on(lambda argv: argv[:3] == ["kubectl", "get", "secret"], self._good_secret_handler)
                    .on(lambda argv: argv[:3] == ["kubectl", "delete", "job"], _ok("")))
        with mock.patch.object(p3.subprocess, "run", scripted):
            p3._run_ecr_token_sync_verification(ARGOCD_NAMESPACE, ECR_REGISTRY, "99999999999999999999", "7")
        create_call = next(c for c in scripted.calls if c[:3] == ["kubectl", "create", "job"])
        job_name = create_call[3]
        self.assertTrue(job_name.islower())
        self.assertLessEqual(len(job_name), 63)
        self.assertIn("-7", job_name)
        self.assertNotIn("_", job_name)

    def test_success_deletes_job_and_verifies_all_four_secrets(self):
        scripted = (ScriptedSubprocess()
                    .on(lambda argv: argv[:3] == ["kubectl", "create", "job"], _ok(""))
                    .on(lambda argv: "jsonpath={.status.succeeded}" in argv, _ok("1"))
                    .on(lambda argv: "jsonpath={.status.failed}" in argv, _ok(""))
                    .on(lambda argv: argv[:2] == ["kubectl", "logs"], _ok(""))
                    .on(lambda argv: argv[:3] == ["kubectl", "get", "secret"], self._good_secret_handler)
                    .on(lambda argv: argv[:3] == ["kubectl", "delete", "job"], _ok("")))
        with mock.patch.object(p3.subprocess, "run", scripted):
            p3._run_ecr_token_sync_verification(ARGOCD_NAMESPACE, ECR_REGISTRY, "1000", "1")
        self.assertTrue(any(c[:3] == ["kubectl", "delete", "job"] for c in scripted.calls))
        secret_gets = [c for c in scripted.calls if c[:3] == ["kubectl", "get", "secret"]]
        self.assertEqual({c[3] for c in secret_gets}, set(p3.REQUIRED_REPO_SECRETS.keys()))

    def test_failure_retains_job_never_deletes(self):
        scripted = (ScriptedSubprocess()
                    .on(lambda argv: argv[:3] == ["kubectl", "create", "job"], _ok(""))
                    .on(lambda argv: "jsonpath={.status.succeeded}" in argv, _ok(""))
                    .on(lambda argv: "jsonpath={.status.failed}" in argv, _ok("1"))
                    .on(lambda argv: argv[:3] == ["kubectl", "get", "job"] and "-o" in argv and "wide" in argv, _ok(""))
                    .on(lambda argv: argv[:2] == ["kubectl", "describe"], _ok(""))
                    .on(lambda argv: argv[:2] == ["kubectl", "logs"], _ok("")))
        with mock.patch.object(p3.subprocess, "run", scripted):
            with self.assertRaises(p3.Phase3Error):
                p3._run_ecr_token_sync_verification(ARGOCD_NAMESPACE, ECR_REGISTRY, "1000", "1")
        self.assertFalse(any(c[:3] == ["kubectl", "delete", "job"] for c in scripted.calls))

    def test_timeout_retains_job_never_deletes(self):
        with mock.patch.object(p3.time, "sleep", lambda s: None):
            scripted = (ScriptedSubprocess()
                        .on(lambda argv: argv[:3] == ["kubectl", "create", "job"], _ok(""))
                        .on(lambda argv: "jsonpath={.status.succeeded}" in argv, _ok(""))
                        .on(lambda argv: "jsonpath={.status.failed}" in argv, _ok(""))
                        .on(lambda argv: argv[:3] == ["kubectl", "get", "job"] and "-o" in argv and "wide" in argv, _ok(""))
                        .on(lambda argv: argv[:2] == ["kubectl", "describe"], _ok(""))
                        .on(lambda argv: argv[:2] == ["kubectl", "logs"], _ok("")))
            with mock.patch.object(p3.subprocess, "run", scripted):
                with self.assertRaises(p3.Phase3Error):
                    p3._run_ecr_token_sync_verification(ARGOCD_NAMESPACE, ECR_REGISTRY, "1000", "1")
        self.assertFalse(any(c[:3] == ["kubectl", "delete", "job"] for c in scripted.calls))

    def test_secret_label_mismatch_fails_closed(self):
        def handler(argv, stdin):
            if "labels.argocd" in "".join(argv):
                return subprocess.CompletedProcess(argv, 0, "not-a-repository", "")
            return subprocess.CompletedProcess(argv, 0, "", "")
        scripted = (ScriptedSubprocess()
                    .on(lambda argv: argv[:3] == ["kubectl", "create", "job"], _ok(""))
                    .on(lambda argv: "jsonpath={.status.succeeded}" in argv, _ok("1"))
                    .on(lambda argv: "jsonpath={.status.failed}" in argv, _ok(""))
                    .on(lambda argv: argv[:2] == ["kubectl", "logs"], _ok(""))
                    .on(lambda argv: argv[:3] == ["kubectl", "get", "secret"], handler))
        with mock.patch.object(p3.subprocess, "run", scripted):
            with self.assertRaises(p3.Phase3Error):
                p3._run_ecr_token_sync_verification(ARGOCD_NAMESPACE, ECR_REGISTRY, "1000", "1")

    def test_secret_url_mismatch_fails_closed(self):
        def handler(argv, stdin):
            if "labels.argocd" in "".join(argv):
                return subprocess.CompletedProcess(argv, 0, "repository", "")
            if argv[-2:] == ["-o", "jsonpath={.data.url}"]:
                return subprocess.CompletedProcess(argv, 0, base64.b64encode(b"oci://wrong.example.com/helm/goldengate").decode(), "")
            return subprocess.CompletedProcess(argv, 0, "", "")
        scripted = (ScriptedSubprocess()
                    .on(lambda argv: argv[:3] == ["kubectl", "create", "job"], _ok(""))
                    .on(lambda argv: "jsonpath={.status.succeeded}" in argv, _ok("1"))
                    .on(lambda argv: "jsonpath={.status.failed}" in argv, _ok(""))
                    .on(lambda argv: argv[:2] == ["kubectl", "logs"], _ok(""))
                    .on(lambda argv: argv[:3] == ["kubectl", "get", "secret"], handler))
        with mock.patch.object(p3.subprocess, "run", scripted):
            with self.assertRaises(p3.Phase3Error):
                p3._run_ecr_token_sync_verification(ARGOCD_NAMESPACE, ECR_REGISTRY, "1000", "1")

    def test_secret_password_is_never_read(self):
        with open(TOOL_PATH) as f:
            source = f.read()
        self.assertNotIn("data.password", source)
        self.assertNotIn(".password}", source)


class TestSummary(Phase3TestCase):
    def test_tolerates_completely_missing_state(self):
        args = SimpleNamespace(state_path=self.state_path, environment=ENVIRONMENT)
        p3.cmd_summary(args)
        summary = self.github_summary.read_text(encoding="utf-8")
        self.assertIn("unknown", summary)

    def test_tolerates_partial_state(self):
        p3.update_state(self.state_path, {"namespace": ARGOCD_NAMESPACE})
        args = SimpleNamespace(state_path=self.state_path, environment=ENVIRONMENT)
        p3.cmd_summary(args)
        summary = self.github_summary.read_text(encoding="utf-8")
        self.assertIn(ARGOCD_NAMESPACE, summary)

    def test_no_credential_exposure(self):
        p3.update_state(self.state_path, {"namespace": ARGOCD_NAMESPACE, "helm_chart_ref": f"oci://{ECR_REGISTRY}/helm/argocd"})
        args = SimpleNamespace(state_path=self.state_path, environment=ENVIRONMENT)
        p3.cmd_summary(args)
        summary = self.github_summary.read_text(encoding="utf-8")
        for forbidden in ("AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "password"):
            self.assertNotIn(forbidden, summary)

    def test_corrected_ingress_exposure_wording_present(self):
        args = SimpleNamespace(state_path=self.state_path, environment=ENVIRONMENT)
        p3.cmd_summary(args)
        summary = self.github_summary.read_text(encoding="utf-8")
        self.assertIn("Server Service remains ClusterIP", summary)
        self.assertIn("argocdServerIngress.enabled", summary)
        self.assertNotIn("Expose Argo CD outside the cluster (server Service stays ClusterIP)", summary)


class TestNoLiveExecution(Phase3TestCase):
    def test_never_uses_shell_true(self):
        with open(TOOL_PATH) as f:
            source = f.read()
        # Looks for the actual keyword-argument call-site shape (shell=True followed by ',' or ')'), never the docstring's own prose warning against it ("...never shell=True...").
        self.assertNotIn("shell=True)", source)
        self.assertNotIn("shell=True,", source)

    def test_never_uses_eval(self):
        with open(TOOL_PATH) as f:
            source = f.read()
        self.assertNotIn("eval(", source)

    def test_ecr_password_login_never_uses_a_shell_pipeline(self):
        with open(TOOL_PATH) as f:
            source = f.read()
        self.assertNotIn("| helm registry login", source)


if __name__ == "__main__":
    unittest.main()
