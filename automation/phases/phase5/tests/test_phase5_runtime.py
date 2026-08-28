"""Offline tests for automation/phases/phase5/phase5_runtime.py; run directly via `python3 automation/phases/phase5/tests/test_phase5_runtime.py`. No live AWS/Kubernetes/ECR/Argo mutation -- every subprocess call is intercepted via a scripted fake that asserts on the exact argv and returns a fabricated result. Covers: input validation, EFS identity resolution (no/existing/managed/dry-run), private-ECR image verification, local Helm render/package validation (singleRuntime manifest contract, EFS render contract, admin-secret CSI isolation), the ECR repository fail-closed correction, Argo reconciliation, removal (BROKEN/legacyPair/managed-physical-removal fail closed before mutation, application_found-based delete gate), and the critical post-delete false-success regression (Application still exists + state=OWNED must never pass)."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[4]
TOOL_PATH = REPO_ROOT / "automation" / "phases" / "phase5" / "phase5_runtime.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("phase5_runtime", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


phase5_runtime = _load_tool()

ENVIRONMENT = "dev"
DEPLOYMENT_ID = "gg-oracle-payments-01"
ECR_REGISTRY = "229410149234.dkr.ecr.eu-west-1.amazonaws.com"
ECR_ACCOUNT_ID = "229410149234"
WORKLOAD_ACCOUNT_ID = "668311715351"
EKS_DEPLOY_ROLE_ARN = f"arn:aws:iam::{WORKLOAD_ACCOUNT_ID}:role/GoldenGateEksDeployRole-dev"
ARGOCD_ECR_READ_ROLE_ARN = f"arn:aws:iam::{ECR_ACCOUNT_ID}:role/ArgoCdEcrReadRole"


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class ScriptedRun:
    """Replaces phase5_runtime.run with a scripted responder: a list of (predicate, FakeProc) pairs consulted in order (later registrations take precedence), falling back to a default success. Every call is recorded for assertion."""

    def __init__(self, default=None):
        self.rules = []
        self.calls = []
        self.default = default if default is not None else FakeProc(0, "", "")

    def when(self, predicate, proc):
        self.rules.append((predicate, proc))
        return self

    def __call__(self, argv, env=None, cwd=None, check=True, capture_output=True, input_text=None):
        self.calls.append({"argv": list(argv), "env": env, "input_text": input_text})
        # Capture any file://-referenced temp file's content NOW -- production code deletes such temp files (e.g. the ECR repository-policy document) right after this call returns.
        for arg in argv:
            if isinstance(arg, str) and arg.startswith("file://") and Path(arg[len("file://"):]).is_file():
                self.calls[-1]["file_contents"] = Path(arg[len("file://"):]).read_text()
        for predicate, proc in reversed(self.rules):
            if predicate(argv):
                if check and proc.returncode != 0:
                    raise phase5_runtime.Phase5Error(f"{' '.join(str(a) for a in argv)} failed: {proc.stdout}\n{proc.stderr}")
                return proc
        if check and self.default.returncode != 0:
            raise phase5_runtime.Phase5Error(f"{' '.join(str(a) for a in argv)} failed: {self.default.stdout}\n{self.default.stderr}")
        return self.default


def _starts_with(*prefix):
    return lambda argv: list(argv[:len(prefix)]) == list(prefix)


def _contains(*substrs):
    return lambda argv: all(any(s in str(a) for a in argv) for s in substrs)


class argparse_namespace:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _env_patch(**overrides):
    base = {
        "AWS_REGION": "eu-west-1",
        "EKS_CLUSTER_NAME": "gg-dev-cluster",
        "EKS_DEPLOY_ROLE_ARN": EKS_DEPLOY_ROLE_ARN,
        "ECR_REGISTRY": ECR_REGISTRY,
        "ECR_ACCOUNT_ID": ECR_ACCOUNT_ID,
        "RUNTIME_NAMESPACE": "goldengate-dev",
        "ARGOCD_NAMESPACE": "argocd",
        "GITHUB_RUN_NUMBER": "42",
        "DNS_DOMAIN": "goldengate-dev.adcbmis.local",
        "ALB_GROUP_NAME": "goldengate-dev-shared",
        "ACM_CERTIFICATE_ARN": f"arn:aws:acm:eu-west-1:{WORKLOAD_ACCOUNT_ID}:certificate/abc-123",
        "ARGOCD_ECR_READ_ROLE_ARN": ARGOCD_ECR_READ_ROLE_ARN,
    }
    base.update(overrides)
    return mock.patch.dict(os.environ, base, clear=False)


def _run_quiet(func, *args, **kwargs):
    with redirect_stdout(io.StringIO()):
        return func(*args, **kwargs)


class TempStateCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self._tmpdir.name) / "state.json"
        self.args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=self.state_path)

    def tearDown(self):
        self._tmpdir.cleanup()


DESCRIPTOR = {
    "deploymentId": DEPLOYMENT_ID,
    "adminSecretName": "dev/goldengate/source/admin",
    "tlsSecretName": "dev/goldengate/tls-certificate",
    "runtimeServiceAccountName": "gg-runtime-sa",
    "imageRepository": f"{ECR_REGISTRY}/aws-cloud-factory-goldengate-oracle",
    "imageRepositoryName": "aws-cloud-factory-goldengate-oracle",
    "imageTag": "23.4.0.0",
    "efsMode": None,
    "efsFileSystemId": None,
    "efsCreationToken": None,
}


def _descriptor(**overrides):
    d = dict(DESCRIPTOR)
    d.update(overrides)
    return d


RUNTIME_FOOTPRINT_KEYS = frozenset({
    "statefulset", "service", "headless_service", "pvc", "storageclass",
    "admin_secretproviderclass", "certificate_secretproviderclass", "ingress", "admin_secret",
})


def _complete_footprint(**overrides):
    """A complete, schema-valid checks.footprint_found mapping -- every canonical key present as a literal bool, defaulting to False, with explicit overrides."""
    footprint = {key: False for key in RUNTIME_FOOTPRINT_KEYS}
    footprint.update(overrides)
    return footprint


def _classifier_result(state, application_found, **footprint_overrides):
    """A complete, schema-valid runtime_state.py classifier result shape."""
    return {"state": state, "checks": {"application_found": application_found, "footprint_found": _complete_footprint(**footprint_overrides)}}


def _canonical_argocd_app_name(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID):
    return phase5_runtime._canonical_argocd_app_name(environment, deployment_id)


GITHUB_RUN_NUMBER = "42"  # must match _env_patch()'s own GITHUB_RUN_NUMBER default below
CHART_VERSION = f"0.1.{GITHUB_RUN_NUMBER}-{DEPLOYMENT_ID}"
TEMP_CHART_PATH = f"work/charts/{DEPLOYMENT_ID}/goldengate"
SAMPLE_IMAGE_DIGEST = "sha256:" + "ab" * 32


def _reconcile_state_fixture(**overrides):
    """A complete, canonical, schema-valid reconcile-state JSON document -- everything _validate_reconcile_state_identity requires, so a test exercising unrelated behavior does not incidentally also fail identity binding. Override individual fields (including deliberately-wrong ones, for the cross-runtime regression tests) via keyword arguments."""
    base = {
        "environment": ENVIRONMENT, "deployment_id": DEPLOYMENT_ID, "deployment_model": "singleRuntime",
        "deploy": True, "values_file": f"envs/{ENVIRONMENT}/{DEPLOYMENT_ID}/values.yaml",
        "target_namespace": "goldengate-dev", "release_name": DEPLOYMENT_ID,
        "argocd_app_name": _canonical_argocd_app_name(), "helm_ecr_repository": "helm/goldengate",
        "helm_push_url": f"oci://{ECR_REGISTRY}/helm", "helm_chart_ref": f"oci://{ECR_REGISTRY}/helm/goldengate",
        "chart_version": CHART_VERSION, "temp_chart_path": TEMP_CHART_PATH,
    }
    base.update(overrides)
    return base


def _removal_state_fixture(**overrides):
    """A complete, canonical, schema-valid removal-state JSON document -- everything _validate_removal_state_identity requires. Override individual fields (including deliberately-wrong ones, for the cross-runtime regression tests) via keyword arguments."""
    base = {
        "environment": ENVIRONMENT, "deployment_id": DEPLOYMENT_ID, "deployment_model": "singleRuntime",
        "efs_mode": "", "reason": "deployment-disabled", "runtime_namespace": "goldengate-dev",
        "argocd_namespace": "argocd", "argocd_app_name": _canonical_argocd_app_name(),
    }
    base.update(overrides)
    return base


def _resolved_state_fixture(**overrides):
    """A complete, canonical reconcile-state document that also satisfies _validate_resolved_runtime_inputs() against the default _descriptor()/canonical environment fixtures -- i.e. everything _full_reconcile_state() persists, as a plain dict for direct unit-level use. Override individual fields (including deliberately-wrong ones) via keyword arguments."""
    base = {
        **_reconcile_state_fixture(),
        "image_repository": IMAGE_REPOSITORY, "image_repository_name": "aws-cloud-factory-goldengate-oracle", "image_tag": "23.4.0.0",
        "image_digest": SAMPLE_IMAGE_DIGEST, "dns_domain": "goldengate-dev.adcbmis.local", "alb_group_name": "goldengate-dev-shared",
        "certificate_arn": f"arn:aws:acm:eu-west-1:{WORKLOAD_ACCOUNT_ID}:certificate/abc-123",
        "admin_secret_name": "dev/goldengate/source/admin", "tls_secret_name": "dev/goldengate/tls-certificate",
        "runtime_service_account_name": "gg-runtime-sa", "resolved_efs_id": "",
        "efs_mode": "", "efs_file_system_id_declared": "", "efs_creation_token": "",
    }
    base.update(overrides)
    return base


def _resolved_inputs(state, verify_managed_efs_live=False, descriptor=None):
    """Calls the real _validate_resolved_runtime_inputs() with a scripted deployment-model describe() response (default: the matching _descriptor()) -- never a reimplementation."""
    scripted = ScriptedRun()
    scripted.when(_starts_with(sys.executable, str(phase5_runtime.DEPLOYMENT_MODEL_TOOL)), FakeProc(0, json.dumps(descriptor if descriptor is not None else _descriptor())))
    with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
        return phase5_runtime._validate_resolved_runtime_inputs(state, ENVIRONMENT, DEPLOYMENT_ID, verify_managed_efs_live=verify_managed_efs_live)


# ==== PREPARATION TESTS ====

class InputValidationTests(unittest.TestCase):
    def test_unsafe_environment_rejected(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime.require_environment_arg("dev; rm -rf /")

    def test_safe_environment_accepted(self):
        self.assertEqual(phase5_runtime.require_environment_arg("dev"), "dev")

    def test_unsafe_deployment_id_rejected(self):
        for bad in ("../etc/passwd", "/etc/passwd", "gg\\oracle", "gg-oracle\nrm", "gg-oracle\r", "gg-oracle\x00", "GG-ORACLE", "gg oracle", ""):
            with self.assertRaises(phase5_runtime.Phase5Error, msg=bad):
                phase5_runtime.require_deployment_id_arg(bad)

    def test_safe_deployment_id_accepted(self):
        self.assertEqual(phase5_runtime.require_deployment_id_arg("gg-oracle-payments-01"), "gg-oracle-payments-01")


class PrepareDeploymentTests(TempStateCase):
    def test_deployment_model_other_than_singleruntime_rejected(self):
        args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, deployment_model="legacyPair", deploy="true", state_path=self.state_path)
        with _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_prepare_deployment, args)

    def test_target_namespace_uses_canonical_runtime_namespace(self):
        args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, deployment_model="singleRuntime", deploy="true", state_path=self.state_path)
        with _env_patch():
            _run_quiet(phase5_runtime.cmd_prepare_deployment, args)
        state = phase5_runtime.load_state(self.state_path)
        self.assertEqual(state["target_namespace"], "goldengate-dev")

    def test_application_suffix_strips_only_one_leading_gg(self):
        args = argparse_namespace(environment=ENVIRONMENT, deployment_id="gg-gg-oracle-01", deployment_model="singleRuntime", deploy="true", state_path=self.state_path)
        with _env_patch():
            _run_quiet(phase5_runtime.cmd_prepare_deployment, args)
        state = phase5_runtime.load_state(self.state_path)
        self.assertEqual(state["argocd_app_name"], "goldengate-dev-gg-oracle-01")

    def test_release_name_exact(self):
        args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, deployment_model="singleRuntime", deploy="true", state_path=self.state_path)
        with _env_patch():
            _run_quiet(phase5_runtime.cmd_prepare_deployment, args)
        state = phase5_runtime.load_state(self.state_path)
        self.assertEqual(state["release_name"], DEPLOYMENT_ID)

    def test_chart_version_exact(self):
        args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, deployment_model="singleRuntime", deploy="true", state_path=self.state_path)
        with _env_patch(GITHUB_RUN_NUMBER="99"):
            _run_quiet(phase5_runtime.cmd_prepare_deployment, args)
        state = phase5_runtime.load_state(self.state_path)
        self.assertEqual(state["chart_version"], f"0.1.99-{DEPLOYMENT_ID}")

    def test_chart_ecr_paths_exact(self):
        args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, deployment_model="singleRuntime", deploy="true", state_path=self.state_path)
        with _env_patch():
            _run_quiet(phase5_runtime.cmd_prepare_deployment, args)
        state = phase5_runtime.load_state(self.state_path)
        self.assertEqual(state["helm_ecr_repository"], "helm/goldengate")
        self.assertEqual(state["helm_push_url"], f"oci://{ECR_REGISTRY}/helm")
        self.assertEqual(state["helm_chart_ref"], f"oci://{ECR_REGISTRY}/helm/goldengate")
        self.assertEqual(state["temp_chart_path"], f"work/charts/{DEPLOYMENT_ID}/goldengate")

    def test_namespace_too_long_fails(self):
        args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, deployment_model="singleRuntime", deploy="true", state_path=self.state_path)
        with _env_patch(RUNTIME_NAMESPACE="a" * 64):
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_prepare_deployment, args)

    def test_release_name_too_long_fails(self):
        long_id = "gg-" + "a" * 55
        args = argparse_namespace(environment=ENVIRONMENT, deployment_id=long_id, deployment_model="singleRuntime", deploy="true", state_path=self.state_path)
        with _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_prepare_deployment, args)

    def test_deploy_flag_parsed_and_stored(self):
        args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, deployment_model="singleRuntime", deploy="false", state_path=self.state_path)
        with _env_patch():
            _run_quiet(phase5_runtime.cmd_prepare_deployment, args)
        state = phase5_runtime.load_state(self.state_path)
        self.assertIs(state["deploy"], False)


class StateAllowListTests(unittest.TestCase):
    def test_reconcile_state_allow_list_rejects_credential_shaped_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            for bad_key in ("aws_access_key_id", "aws_secret_access_key", "aws_session_token", "ecr_password", "github_token", "kubernetes_bearer_token", "secret_value", "argo_repo_password"):
                with self.assertRaises(phase5_runtime.Phase5Error, msg=bad_key):
                    phase5_runtime.update_state(state_path, {bad_key: "x"}, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)

    def test_removal_state_allow_list_rejects_credential_shaped_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            with self.assertRaises(phase5_runtime.Phase5Error):
                phase5_runtime.update_state(state_path, {"aws_secret_access_key": "x"}, phase5_runtime.REMOVAL_ALLOWED_STATE_KEYS)

    def test_malformed_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text("{not valid json")
            with self.assertRaises(phase5_runtime.Phase5Error):
                phase5_runtime.load_state(state_path)

    def test_non_object_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text("[1, 2, 3]")
            with self.assertRaises(phase5_runtime.Phase5Error):
                phase5_runtime.load_state(state_path)


# ==== EFS TESTS ====

class EfsResolutionTests(unittest.TestCase):
    def _resolve(self, **overrides):
        kwargs = dict(
            efs_mode=None, efs_file_system_id_declared="", efs_creation_token="", deploy=False,
            environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, eks_deploy_role_arn=EKS_DEPLOY_ROLE_ARN, aws_region="eu-west-1",
        )
        kwargs.update(overrides)
        with redirect_stdout(io.StringIO()):
            return phase5_runtime._resolve_efs_filesystem_id(**kwargs)

    def test_no_efs_path_returns_empty_id(self):
        self.assertEqual(self._resolve(efs_mode=None), "")
        self.assertEqual(self._resolve(efs_mode=""), "")

    def test_existing_mode_uses_declared_id(self):
        self.assertEqual(self._resolve(efs_mode="existing", efs_file_system_id_declared="fs-real12345"), "fs-real12345")

    def test_managed_validate_uses_only_placeholder(self):
        result = self._resolve(efs_mode="managed", deploy=False, efs_creation_token="gg-dev-oracle-01-u02")
        self.assertEqual(result, phase5_runtime.EFS_DRY_RUN_PLACEHOLDER)

    def test_placeholder_never_used_in_deploy_reconciliation(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "assume-role"), FakeProc(0, json.dumps({"Credentials": {"AccessKeyId": "AKIA", "SecretAccessKey": "secret", "SessionToken": "token"}})))
        scripted.when(_contains("sts", "get-caller-identity"), FakeProc(0, WORKLOAD_ACCOUNT_ID))
        scripted.when(_starts_with("aws", "efs", "describe-file-systems"), FakeProc(0, json.dumps({"FileSystems": [{"FileSystemId": "fs-managed001", "LifeCycleState": "available", "Tags": [
            {"Key": "ManagedBy", "Value": "goldengate-eks-app"}, {"Key": "GoldenGateDeploymentId", "Value": DEPLOYMENT_ID},
            {"Key": "GoldenGateEnvironment", "Value": ENVIRONMENT}, {"Key": "GoldenGateStorage", "Value": "u02"},
        ]}]})))
        with mock.patch.object(phase5_runtime, "run", scripted):
            result = self._resolve(efs_mode="managed", deploy=True, efs_creation_token="gg-dev-oracle-01-u02")
        self.assertNotEqual(result, phase5_runtime.EFS_DRY_RUN_PLACEHOLDER)
        self.assertEqual(result, "fs-managed001")

    def test_managed_deploy_requires_creation_token(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            self._resolve(efs_mode="managed", deploy=True, efs_creation_token="")

    def test_malformed_eks_role_arn_fails(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            self._resolve(efs_mode="managed", deploy=True, efs_creation_token="tok", eks_deploy_role_arn="not-an-arn")

    def test_assume_role_failure_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "assume-role"), FakeProc(1, "", "AccessDenied"))
        with mock.patch.object(phase5_runtime, "run", scripted):
            with self.assertRaises(phase5_runtime.Phase5Error):
                self._resolve(efs_mode="managed", deploy=True, efs_creation_token="tok")

    def test_wrong_workload_account_fails_before_efs_describe(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "assume-role"), FakeProc(0, json.dumps({"Credentials": {"AccessKeyId": "AKIA", "SecretAccessKey": "s", "SessionToken": "t"}})))
        scripted.when(_contains("sts", "get-caller-identity"), FakeProc(0, "999999999999"))
        scripted.when(_starts_with("aws", "efs", "describe-file-systems"), FakeProc(0, json.dumps({"FileSystems": []})))
        with mock.patch.object(phase5_runtime, "run", scripted):
            with self.assertRaises(phase5_runtime.Phase5Error):
                self._resolve(efs_mode="managed", deploy=True, efs_creation_token="tok")
        describe_calls = [c for c in scripted.calls if c["argv"][:3] == ["aws", "efs", "describe-file-systems"]]
        self.assertEqual(describe_calls, [], "EFS describe must never run after a wrong-account failure")

    def test_zero_filesystems_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "assume-role"), FakeProc(0, json.dumps({"Credentials": {"AccessKeyId": "AKIA", "SecretAccessKey": "s", "SessionToken": "t"}})))
        scripted.when(_contains("sts", "get-caller-identity"), FakeProc(0, WORKLOAD_ACCOUNT_ID))
        scripted.when(_starts_with("aws", "efs", "describe-file-systems"), FakeProc(0, json.dumps({"FileSystems": []})))
        with mock.patch.object(phase5_runtime, "run", scripted):
            with self.assertRaises(phase5_runtime.Phase5Error):
                self._resolve(efs_mode="managed", deploy=True, efs_creation_token="tok")

    def test_multiple_filesystems_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "assume-role"), FakeProc(0, json.dumps({"Credentials": {"AccessKeyId": "AKIA", "SecretAccessKey": "s", "SessionToken": "t"}})))
        scripted.when(_contains("sts", "get-caller-identity"), FakeProc(0, WORKLOAD_ACCOUNT_ID))
        scripted.when(_starts_with("aws", "efs", "describe-file-systems"), FakeProc(0, json.dumps({"FileSystems": [{"FileSystemId": "fs-1"}, {"FileSystemId": "fs-2"}]})))
        with mock.patch.object(phase5_runtime, "run", scripted):
            with self.assertRaises(phase5_runtime.Phase5Error):
                self._resolve(efs_mode="managed", deploy=True, efs_creation_token="tok")

    def test_lifecycle_not_available_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "assume-role"), FakeProc(0, json.dumps({"Credentials": {"AccessKeyId": "AKIA", "SecretAccessKey": "s", "SessionToken": "t"}})))
        scripted.when(_contains("sts", "get-caller-identity"), FakeProc(0, WORKLOAD_ACCOUNT_ID))
        scripted.when(_starts_with("aws", "efs", "describe-file-systems"), FakeProc(0, json.dumps({"FileSystems": [{"FileSystemId": "fs-1", "LifeCycleState": "creating", "Tags": []}]})))
        with mock.patch.object(phase5_runtime, "run", scripted):
            with self.assertRaises(phase5_runtime.Phase5Error):
                self._resolve(efs_mode="managed", deploy=True, efs_creation_token="tok")

    def test_ownership_tag_mismatch_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "assume-role"), FakeProc(0, json.dumps({"Credentials": {"AccessKeyId": "AKIA", "SecretAccessKey": "s", "SessionToken": "t"}})))
        scripted.when(_contains("sts", "get-caller-identity"), FakeProc(0, WORKLOAD_ACCOUNT_ID))
        scripted.when(_starts_with("aws", "efs", "describe-file-systems"), FakeProc(0, json.dumps({"FileSystems": [{"FileSystemId": "fs-1", "LifeCycleState": "available", "Tags": [{"Key": "ManagedBy", "Value": "someone-else"}]}]})))
        with mock.patch.object(phase5_runtime, "run", scripted):
            with self.assertRaises(phase5_runtime.Phase5Error):
                self._resolve(efs_mode="managed", deploy=True, efs_creation_token="tok")

    def test_exact_managed_efs_succeeds(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "assume-role"), FakeProc(0, json.dumps({"Credentials": {"AccessKeyId": "AKIA", "SecretAccessKey": "s", "SessionToken": "t"}})))
        scripted.when(_contains("sts", "get-caller-identity"), FakeProc(0, WORKLOAD_ACCOUNT_ID))
        scripted.when(_starts_with("aws", "efs", "describe-file-systems"), FakeProc(0, json.dumps({"FileSystems": [{"FileSystemId": "fs-managed42", "LifeCycleState": "available", "Tags": [
            {"Key": "ManagedBy", "Value": "goldengate-eks-app"}, {"Key": "GoldenGateDeploymentId", "Value": DEPLOYMENT_ID},
            {"Key": "GoldenGateEnvironment", "Value": ENVIRONMENT}, {"Key": "GoldenGateStorage", "Value": "u02"},
        ]}]})))
        with mock.patch.object(phase5_runtime, "run", scripted):
            resolved = self._resolve(efs_mode="managed", deploy=True, efs_creation_token="tok")
        self.assertEqual(resolved, "fs-managed42")

    def test_temporary_credentials_never_enter_state_environment_or_output(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "assume-role"), FakeProc(0, json.dumps({"Credentials": {"AccessKeyId": "AKIA_SECRET_MARKER", "SecretAccessKey": "SECRET_MARKER", "SessionToken": "TOKEN_MARKER"}})))
        scripted.when(_contains("sts", "get-caller-identity"), FakeProc(0, WORKLOAD_ACCOUNT_ID))
        scripted.when(_starts_with("aws", "efs", "describe-file-systems"), FakeProc(0, json.dumps({"FileSystems": [{"FileSystemId": "fs-1", "LifeCycleState": "available", "Tags": [
            {"Key": "ManagedBy", "Value": "goldengate-eks-app"}, {"Key": "GoldenGateDeploymentId", "Value": DEPLOYMENT_ID},
            {"Key": "GoldenGateEnvironment", "Value": ENVIRONMENT}, {"Key": "GoldenGateStorage", "Value": "u02"},
        ]}]})))
        original_environ = dict(os.environ)
        with mock.patch.object(phase5_runtime, "run", scripted):
            self._resolve(efs_mode="managed", deploy=True, efs_creation_token="tok")
        self.assertNotIn("AKIA_SECRET_MARKER", os.environ.values())
        self.assertEqual(dict(os.environ), original_environ, "the real process environment must never be mutated by the credentials overlay")


# ==== IMAGE TESTS ====

class ImageVerificationTests(unittest.TestCase):
    def _verify(self, image_repository, image_tag, run_stub):
        with mock.patch.object(phase5_runtime, "run", run_stub):
            with redirect_stdout(io.StringIO()):
                return phase5_runtime._verify_image_in_private_ecr(image_repository, image_tag, ECR_REGISTRY, "eu-west-1")

    def test_public_or_outside_canonical_ecr_image_rejected(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            self._verify("public.ecr.aws/x/y", "1.0", lambda *a, **k: FakeProc(0, "{}"))
        with self.assertRaises(phase5_runtime.Phase5Error):
            self._verify("999999999999.dkr.ecr.eu-west-1.amazonaws.com/some-repo", "1.0", lambda *a, **k: FakeProc(0, "{}"))

    def test_exact_private_repository_and_tag_accepted(self):
        def fake_run(argv, **kwargs):
            self.assertIn("aws-cloud-factory-goldengate-oracle", argv)
            self.assertIn("imageTag=23.4.0.0", argv)
            return FakeProc(0, json.dumps({"imageDetails": [{"imageDigest": "sha256:abc"}]}))
        digest = self._verify(f"{ECR_REGISTRY}/aws-cloud-factory-goldengate-oracle", "23.4.0.0", fake_run)
        self.assertEqual(digest, "sha256:abc")

    def test_missing_image_fails(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            self._verify(f"{ECR_REGISTRY}/some-repo", "1.0", lambda *a, **k: FakeProc(1, "", "ImageNotFoundException"))

    def test_missing_image_digest_fails(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            self._verify(f"{ECR_REGISTRY}/some-repo", "1.0", lambda *a, **k: FakeProc(0, json.dumps({"imageDetails": [{}]})))

    def test_no_public_fallback(self):
        source = TOOL_PATH.read_text()
        self.assertNotIn("public.ecr.aws", source)


# ==== LOCAL HELM TESTS (structural manifest-contract validation, no live Helm) ====

VALUES = {
    "runtime": {"containerName": "goldengate", "name": DEPLOYMENT_ID},
    "ingress": {"enabled": False},
}

IMAGE_REPOSITORY = f"{ECR_REGISTRY}/aws-cloud-factory-goldengate-oracle"
IMAGE_TAG = "23.4.0.0"
IMAGE_DIGEST = "sha256:abcdef"
EXPECTED_IMAGE = f"{IMAGE_REPOSITORY}:{IMAGE_TAG}"
TARGET_NAMESPACE = "goldengate-dev"


def _statefulset(container_name="goldengate", image=EXPECTED_IMAGE, init_names=(phase5_runtime.INIT_CONTAINER_NAME,),
                  init_image=EXPECTED_IMAGE, sa_name="gg-runtime-sa", namespace=TARGET_NAMESPACE, extra_containers=None,
                  init_script='rm -f -- "$SERVICE_MANAGER_PID_FILE" # ServiceManager.pid cleanup'):
    containers = [{"name": container_name, "image": image}]
    if extra_containers:
        containers.extend(extra_containers)
    init_containers = []
    for name in init_names:
        init_containers.append({"name": name, "image": init_image, "command": ["sh", "-c", init_script]})
    return {
        "kind": "StatefulSet", "metadata": {"name": DEPLOYMENT_ID, "namespace": namespace},
        "spec": {"template": {"spec": {"serviceAccountName": sa_name, "containers": containers, "initContainers": init_containers,
                                        "volumes": [{"name": "u02", "persistentVolumeClaim": {"claimName": f"{DEPLOYMENT_ID}-u02"}}, {"name": "u03", "emptyDir": {}}]}}},
    }


def _service(name, namespace=TARGET_NAMESPACE, headless=False):
    spec = {"clusterIP": "None"} if headless else {"clusterIP": "10.0.0.1", "type": "ClusterIP"}
    return {"kind": "Service", "metadata": {"name": name, "namespace": namespace}, "spec": spec}


def _admin_spc(admin_secret_name, namespace=TARGET_NAMESPACE):
    return {
        "kind": "SecretProviderClass", "metadata": {"name": f"{DEPLOYMENT_ID}-admin", "namespace": namespace},
        "spec": {"parameters": {"objects": json.dumps([{"objectName": admin_secret_name}])}},
    }


def _minimal_docs(admin_secret_name="dev/goldengate/source/admin"):
    return [
        _statefulset(),
        _service(DEPLOYMENT_ID),
        _service(f"{DEPLOYMENT_ID}-headless", headless=True),
        _admin_spc(admin_secret_name),
    ]


class RequiredFilesTests(unittest.TestCase):
    def test_missing_chart_yaml_fails(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_required_files("envs/dev/gg-oracle-payments-01/values.yaml", Path("/nonexistent/chart"))

    def test_missing_base_values_yaml_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            chart_dir = Path(tmp) / "chart"
            chart_dir.mkdir()
            (chart_dir / "Chart.yaml").write_text("apiVersion: v2\n")
            with self.assertRaises(phase5_runtime.Phase5Error):
                phase5_runtime._validate_required_files("envs/dev/gg-oracle-payments-01/values.yaml", chart_dir)

    def test_missing_deployment_values_fails(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_required_files("envs/dev/does-not-exist/values.yaml", phase5_runtime.HELM_CHART_PATH)


class HelmDependencyToleranceTests(unittest.TestCase):
    def test_dependency_build_failure_is_tolerated(self):
        """The ONE explicitly tolerated Helm failure -- never generalized to lint/template/package."""
        scripted = ScriptedRun()
        scripted.when(_starts_with("helm", "dependency", "build"), FakeProc(1, "", "Error: no repositories configured"))
        scripted.when(_starts_with("helm", "lint"), FakeProc(0, ""))
        with mock.patch.object(phase5_runtime, "run", scripted):
            with redirect_stdout(io.StringIO()):
                dep_proc = scripted(["helm", "dependency", "build", "x"], check=False)
                self.assertEqual(dep_proc.returncode, 1)
                # Confirms lint still runs afterward and is authoritative.
                phase5_runtime.run(["helm", "lint", "x"])


class RenderedManifestContractTests(unittest.TestCase):
    def test_duplicate_rendered_yaml_key_fails(self):
        text = "kind: Foo\nmetadata:\n  name: a\n  name: b\n"
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._parse_rendered_documents(text)

    def test_namespace_document_fails(self):
        docs = [{"kind": "Namespace", "metadata": {"name": "goldengate-dev"}}]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_zero_namespace_documents(docs)

    def test_wrong_runtime_service_account_fails(self):
        docs = [_statefulset(sa_name="wrong-sa")]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_runtime_service_account_used(docs, "gg-runtime-sa")

    def test_correct_runtime_service_account_passes(self):
        docs = [_statefulset(sa_name="gg-runtime-sa")]
        with redirect_stdout(io.StringIO()):
            phase5_runtime._validate_runtime_service_account_used(docs, "gg-runtime-sa")

    def test_wrong_admin_secret_fails(self):
        docs = [_admin_spc("dev/goldengate/target/admin")]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_admin_secret_csi_isolation(docs, "dev/goldengate/source/admin", "dev", DEPLOYMENT_ID)

    def test_opposite_role_admin_secret_fails(self):
        docs = [{
            "kind": "SecretProviderClass", "metadata": {"name": f"{DEPLOYMENT_ID}-admin"},
            "spec": {"parameters": {"objects": json.dumps([{"objectName": "dev/goldengate/source/admin"}, {"objectName": "dev/goldengate/target/admin"}])}},
        }]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_admin_secret_csi_isolation(docs, "dev/goldengate/source/admin", "dev", DEPLOYMENT_ID)

    def test_correct_admin_secret_isolation_passes(self):
        docs = [_admin_spc("dev/goldengate/source/admin")]
        with redirect_stdout(io.StringIO()):
            phase5_runtime._validate_admin_secret_csi_isolation(docs, "dev/goldengate/source/admin", "dev", DEPLOYMENT_ID)

    def test_exactly_one_statefulset_required(self):
        docs = _minimal_docs() + [_statefulset()]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_singleruntime_manifest_contract(docs, VALUES, DEPLOYMENT_ID, TARGET_NAMESPACE, IMAGE_REPOSITORY, IMAGE_TAG, IMAGE_DIGEST)

    def test_exactly_one_regular_container_required(self):
        docs = [_statefulset(extra_containers=[{"name": "sidecar", "image": "x"}]), _service(DEPLOYMENT_ID), _service(f"{DEPLOYMENT_ID}-headless", headless=True), _admin_spc("dev/goldengate/source/admin")]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_singleruntime_manifest_contract(docs, VALUES, DEPLOYMENT_ID, TARGET_NAMESPACE, IMAGE_REPOSITORY, IMAGE_TAG, IMAGE_DIGEST)

    def test_wrong_container_name_fails(self):
        docs = [_statefulset(container_name="wrong-name"), _service(DEPLOYMENT_ID), _service(f"{DEPLOYMENT_ID}-headless", headless=True), _admin_spc("dev/goldengate/source/admin")]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_singleruntime_manifest_contract(docs, VALUES, DEPLOYMENT_ID, TARGET_NAMESPACE, IMAGE_REPOSITORY, IMAGE_TAG, IMAGE_DIGEST)

    def test_wrong_runtime_image_fails(self):
        docs = [_statefulset(image=f"{IMAGE_REPOSITORY}:wrong-tag"), _service(DEPLOYMENT_ID), _service(f"{DEPLOYMENT_ID}-headless", headless=True), _admin_spc("dev/goldengate/source/admin")]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_singleruntime_manifest_contract(docs, VALUES, DEPLOYMENT_ID, TARGET_NAMESPACE, IMAGE_REPOSITORY, IMAGE_TAG, IMAGE_DIGEST)

    def test_observer_regular_container_fails(self):
        docs = [_statefulset(container_name="goldengate-observer"), _service(DEPLOYMENT_ID), _service(f"{DEPLOYMENT_ID}-headless", headless=True), _admin_spc("dev/goldengate/source/admin")]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_singleruntime_manifest_contract(docs, VALUES, DEPLOYMENT_ID, TARGET_NAMESPACE, IMAGE_REPOSITORY, IMAGE_TAG, IMAGE_DIGEST)

    def test_utility_sidecar_name_fails(self):
        docs = [_statefulset(container_name="utility-sidecar"), _service(DEPLOYMENT_ID), _service(f"{DEPLOYMENT_ID}-headless", headless=True), _admin_spc("dev/goldengate/source/admin")]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_singleruntime_manifest_contract(docs, VALUES, DEPLOYMENT_ID, TARGET_NAMESPACE, IMAGE_REPOSITORY, IMAGE_TAG, IMAGE_DIGEST)

    def test_fluent_bit_regular_container_fails(self):
        docs = [_statefulset(container_name="fluent-bit"), _service(DEPLOYMENT_ID), _service(f"{DEPLOYMENT_ID}-headless", headless=True), _admin_spc("dev/goldengate/source/admin")]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_singleruntime_manifest_contract(docs, VALUES, DEPLOYMENT_ID, TARGET_NAMESPACE, IMAGE_REPOSITORY, IMAGE_TAG, IMAGE_DIGEST)

    def test_init_container_missing_fails(self):
        docs = [_statefulset(init_names=()), _service(DEPLOYMENT_ID), _service(f"{DEPLOYMENT_ID}-headless", headless=True), _admin_spc("dev/goldengate/source/admin")]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_singleruntime_manifest_contract(docs, VALUES, DEPLOYMENT_ID, TARGET_NAMESPACE, IMAGE_REPOSITORY, IMAGE_TAG, IMAGE_DIGEST)

    def test_extra_init_container_fails(self):
        docs = [_statefulset(init_names=(phase5_runtime.INIT_CONTAINER_NAME, "extra-init")), _service(DEPLOYMENT_ID), _service(f"{DEPLOYMENT_ID}-headless", headless=True), _admin_spc("dev/goldengate/source/admin")]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_singleruntime_manifest_contract(docs, VALUES, DEPLOYMENT_ID, TARGET_NAMESPACE, IMAGE_REPOSITORY, IMAGE_TAG, IMAGE_DIGEST)

    def test_init_container_wrong_name_fails(self):
        docs = [_statefulset(init_names=("wrong-init-name",)), _service(DEPLOYMENT_ID), _service(f"{DEPLOYMENT_ID}-headless", headless=True), _admin_spc("dev/goldengate/source/admin")]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_singleruntime_manifest_contract(docs, VALUES, DEPLOYMENT_ID, TARGET_NAMESPACE, IMAGE_REPOSITORY, IMAGE_TAG, IMAGE_DIGEST)

    def test_init_container_wrong_image_fails(self):
        docs = [_statefulset(init_image=f"{IMAGE_REPOSITORY}:different"), _service(DEPLOYMENT_ID), _service(f"{DEPLOYMENT_ID}-headless", headless=True), _admin_spc("dev/goldengate/source/admin")]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_singleruntime_manifest_contract(docs, VALUES, DEPLOYMENT_ID, TARGET_NAMESPACE, IMAGE_REPOSITORY, IMAGE_TAG, IMAGE_DIGEST)

    def test_service_manager_pid_cleanup_missing_fails(self):
        docs = [_statefulset(init_script="echo no cleanup here"), _service(DEPLOYMENT_ID), _service(f"{DEPLOYMENT_ID}-headless", headless=True), _admin_spc("dev/goldengate/source/admin")]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_singleruntime_manifest_contract(docs, VALUES, DEPLOYMENT_ID, TARGET_NAMESPACE, IMAGE_REPOSITORY, IMAGE_TAG, IMAGE_DIGEST)

    def test_exactly_two_services_required(self):
        docs = [_statefulset(), _service(DEPLOYMENT_ID), _admin_spc("dev/goldengate/source/admin")]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_singleruntime_manifest_contract(docs, VALUES, DEPLOYMENT_ID, TARGET_NAMESPACE, IMAGE_REPOSITORY, IMAGE_TAG, IMAGE_DIGEST)

    def test_headless_service_contract(self):
        docs = [_statefulset(), _service(DEPLOYMENT_ID), _service(f"{DEPLOYMENT_ID}-headless", headless=False), _service(f"{DEPLOYMENT_ID}-headless2", headless=False), _admin_spc("dev/goldengate/source/admin")]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_singleruntime_manifest_contract(docs, VALUES, DEPLOYMENT_ID, TARGET_NAMESPACE, IMAGE_REPOSITORY, IMAGE_TAG, IMAGE_DIGEST)

    def test_clusterip_service_contract_passes(self):
        docs = _minimal_docs()
        with redirect_stdout(io.StringIO()):
            phase5_runtime._validate_singleruntime_manifest_contract(docs, VALUES, DEPLOYMENT_ID, TARGET_NAMESPACE, IMAGE_REPOSITORY, IMAGE_TAG, IMAGE_DIGEST)

    def test_enabled_ingress_count_host_contract(self):
        values_with_ingress = {**VALUES, "ingress": {"enabled": True}}
        ingress_ok = {"kind": "Ingress", "metadata": {"name": f"{DEPLOYMENT_ID}-ingress", "namespace": TARGET_NAMESPACE}, "spec": {"rules": [{"host": f"{DEPLOYMENT_ID}.example.com"}]}}
        docs = _minimal_docs() + [ingress_ok]
        with redirect_stdout(io.StringIO()):
            phase5_runtime._validate_singleruntime_manifest_contract(docs, values_with_ingress, DEPLOYMENT_ID, TARGET_NAMESPACE, IMAGE_REPOSITORY, IMAGE_TAG, IMAGE_DIGEST)

        ingress_bad = {"kind": "Ingress", "metadata": {"name": f"{DEPLOYMENT_ID}-ingress", "namespace": TARGET_NAMESPACE}, "spec": {"rules": []}}
        docs_bad = _minimal_docs() + [ingress_bad]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_singleruntime_manifest_contract(docs_bad, values_with_ingress, DEPLOYMENT_ID, TARGET_NAMESPACE, IMAGE_REPOSITORY, IMAGE_TAG, IMAGE_DIGEST)

    def test_secretproviderclass_names_runtime_qualified(self):
        docs = _minimal_docs() + [{"kind": "SecretProviderClass", "metadata": {"name": "not-qualified", "namespace": TARGET_NAMESPACE}, "spec": {"parameters": {"objects": "[]"}}}]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_singleruntime_manifest_contract(docs, VALUES, DEPLOYMENT_ID, TARGET_NAMESPACE, IMAGE_REPOSITORY, IMAGE_TAG, IMAGE_DIGEST)

    def test_namespaced_resources_use_shared_runtime_namespace(self):
        docs = [_statefulset(namespace="wrong-namespace"), _service(DEPLOYMENT_ID), _service(f"{DEPLOYMENT_ID}-headless", headless=True), _admin_spc("dev/goldengate/source/admin")]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_singleruntime_manifest_contract(docs, VALUES, DEPLOYMENT_ID, TARGET_NAMESPACE, IMAGE_REPOSITORY, IMAGE_TAG, IMAGE_DIGEST)


# ==== EFS RENDER TESTS ====

def _efs_values(mode="existing", file_system_id="fs-existing1", base_path=None, storage_class=None):
    efs = {"mode": mode, "fileSystemId": file_system_id}
    if storage_class is not None:
        efs["storageClass"] = storage_class
    elif base_path is not None:
        efs["storageClass"] = {"basePath": base_path}
    return {"persistence": {"enabled": True, "provider": "efs", "efs": efs}, "runtime": {"name": DEPLOYMENT_ID}}


def _storageclass_doc(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, resolved_efs_id="fs-existing1", base_path=None):
    base_path = base_path or f"/{deployment_id}"
    return {
        "kind": "StorageClass", "metadata": {"name": f"gg-efs-{environment}-{deployment_id}"},
        "provisioner": "efs.csi.aws.com", "reclaimPolicy": "Retain",
        "parameters": {"provisioningMode": "efs-ap", "fileSystemId": resolved_efs_id, "basePath": base_path,
                        "subPathPattern": "${.PVC.name}", "ensureUniqueDirectory": "true"},
    }


class EfsValuesShapeTests(unittest.TestCase):
    def test_existing_mode_missing_filesystemid_fails(self):
        values = _efs_values(mode="existing", file_system_id="")
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_efs_values_shape(values, DEPLOYMENT_ID, "singleRuntime")

    def test_managed_mode_committed_filesystemid_fails(self):
        values = _efs_values(mode="managed", file_system_id="fs-should-not-be-here")
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_efs_values_shape(values, DEPLOYMENT_ID, "singleRuntime")

    def test_invalid_efs_mode_fails(self):
        values = _efs_values(mode="bogus")
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_efs_values_shape(values, DEPLOYMENT_ID, "singleRuntime")

    def test_invalid_storageclass_shape_fails(self):
        values = _efs_values(storage_class="not-a-mapping")
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_efs_values_shape(values, DEPLOYMENT_ID, "singleRuntime")

    def test_default_base_path_exact(self):
        values = _efs_values()
        enabled, facts = phase5_runtime._validate_efs_values_shape(values, DEPLOYMENT_ID, "singleRuntime")
        self.assertTrue(enabled)
        self.assertEqual(facts["base_path"], f"/{DEPLOYMENT_ID}")

    def test_explicit_base_path_exact(self):
        values = _efs_values(base_path="/custom/path")
        enabled, facts = phase5_runtime._validate_efs_values_shape(values, DEPLOYMENT_ID, "singleRuntime")
        self.assertTrue(enabled)
        self.assertEqual(facts["base_path"], "/custom/path")

    def test_persistence_not_enabled_skips(self):
        enabled, facts = phase5_runtime._validate_efs_values_shape({"persistence": {"enabled": False}}, DEPLOYMENT_ID, "singleRuntime")
        self.assertFalse(enabled)
        self.assertIsNone(facts)


class EfsRenderedManifestTests(unittest.TestCase):
    def test_exactly_one_expected_storageclass(self):
        docs = [_storageclass_doc(), _storageclass_doc()]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_rendered_storageclass_and_pvc(docs, ENVIRONMENT, DEPLOYMENT_ID, "fs-existing1", f"/{DEPLOYMENT_ID}")

    def test_efs_csi_provisioner_exact(self):
        sc = _storageclass_doc()
        sc["provisioner"] = "wrong.csi.driver"
        docs = [sc, {"kind": "PersistentVolumeClaim", "metadata": {"name": f"{DEPLOYMENT_ID}-u02"}}, _statefulset()]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_rendered_storageclass_and_pvc(docs, ENVIRONMENT, DEPLOYMENT_ID, "fs-existing1", f"/{DEPLOYMENT_ID}")

    def test_provisioning_mode_exact(self):
        sc = _storageclass_doc()
        sc["parameters"]["provisioningMode"] = "wrong"
        docs = [sc, {"kind": "PersistentVolumeClaim", "metadata": {"name": f"{DEPLOYMENT_ID}-u02"}}, _statefulset()]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_rendered_storageclass_and_pvc(docs, ENVIRONMENT, DEPLOYMENT_ID, "fs-existing1", f"/{DEPLOYMENT_ID}")

    def test_filesystemid_exact(self):
        docs = [_storageclass_doc(resolved_efs_id="fs-existing1"), {"kind": "PersistentVolumeClaim", "metadata": {"name": f"{DEPLOYMENT_ID}-u02"}}, _statefulset()]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_rendered_storageclass_and_pvc(docs, ENVIRONMENT, DEPLOYMENT_ID, "fs-DIFFERENT", f"/{DEPLOYMENT_ID}")

    def test_base_path_exact(self):
        docs = [_storageclass_doc(base_path="/wrong"), {"kind": "PersistentVolumeClaim", "metadata": {"name": f"{DEPLOYMENT_ID}-u02"}}, _statefulset()]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_rendered_storageclass_and_pvc(docs, ENVIRONMENT, DEPLOYMENT_ID, "fs-existing1", f"/{DEPLOYMENT_ID}")

    def test_subpathpattern_exact(self):
        sc = _storageclass_doc()
        sc["parameters"]["subPathPattern"] = "wrong-pattern"
        docs = [sc, {"kind": "PersistentVolumeClaim", "metadata": {"name": f"{DEPLOYMENT_ID}-u02"}}, _statefulset()]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_rendered_storageclass_and_pvc(docs, ENVIRONMENT, DEPLOYMENT_ID, "fs-existing1", f"/{DEPLOYMENT_ID}")

    def test_ensure_unique_directory_exact(self):
        sc = _storageclass_doc()
        sc["parameters"]["ensureUniqueDirectory"] = "false"
        docs = [sc, {"kind": "PersistentVolumeClaim", "metadata": {"name": f"{DEPLOYMENT_ID}-u02"}}, _statefulset()]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_rendered_storageclass_and_pvc(docs, ENVIRONMENT, DEPLOYMENT_ID, "fs-existing1", f"/{DEPLOYMENT_ID}")

    def test_reclaim_policy_retain(self):
        sc = _storageclass_doc()
        sc["reclaimPolicy"] = "Delete"
        docs = [sc, {"kind": "PersistentVolumeClaim", "metadata": {"name": f"{DEPLOYMENT_ID}-u02"}}, _statefulset()]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_rendered_storageclass_and_pvc(docs, ENVIRONMENT, DEPLOYMENT_ID, "fs-existing1", f"/{DEPLOYMENT_ID}")

    def test_exactly_one_expected_pvc(self):
        docs = [_storageclass_doc(), _statefulset()]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_rendered_storageclass_and_pvc(docs, ENVIRONMENT, DEPLOYMENT_ID, "fs-existing1", f"/{DEPLOYMENT_ID}")

    def test_u02_pvc_claim_wiring(self):
        sts = _statefulset()
        sts["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "wrong-claim"
        docs = [_storageclass_doc(), {"kind": "PersistentVolumeClaim", "metadata": {"name": f"{DEPLOYMENT_ID}-u02"}}, sts]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_rendered_storageclass_and_pvc(docs, ENVIRONMENT, DEPLOYMENT_ID, "fs-existing1", f"/{DEPLOYMENT_ID}")

    def test_u03_emptydir_wiring(self):
        sts = _statefulset()
        sts["spec"]["template"]["spec"]["volumes"][1] = {"name": "u03", "persistentVolumeClaim": {"claimName": "wrong"}}
        docs = [_storageclass_doc(), {"kind": "PersistentVolumeClaim", "metadata": {"name": f"{DEPLOYMENT_ID}-u02"}}, sts]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_rendered_storageclass_and_pvc(docs, ENVIRONMENT, DEPLOYMENT_ID, "fs-existing1", f"/{DEPLOYMENT_ID}")

    def test_full_efs_render_contract_passes(self):
        docs = [_storageclass_doc(), {"kind": "PersistentVolumeClaim", "metadata": {"name": f"{DEPLOYMENT_ID}-u02"}}, _statefulset()]
        with redirect_stdout(io.StringIO()):
            phase5_runtime._validate_rendered_storageclass_and_pvc(docs, ENVIRONMENT, DEPLOYMENT_ID, "fs-existing1", f"/{DEPLOYMENT_ID}")

    def test_persistence_disabled_skips_efs_render_checks(self):
        with redirect_stdout(io.StringIO()):
            phase5_runtime._validate_efs_render_contract({"persistence": {"enabled": False}}, [], ENVIRONMENT, DEPLOYMENT_ID, "singleRuntime", "", "", "")

    def test_existing_mode_resolved_id_mismatch_fails(self):
        values = _efs_values(mode="existing", file_system_id="fs-declared")
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_efs_render_contract(values, [], ENVIRONMENT, DEPLOYMENT_ID, "singleRuntime", "fs-DIFFERENT", "", "fs-declared")


# ==== ECR HELM REPO TESTS ====

def _canonical_chart_file_members(chart_version):
    """Test-side mirror of the CURRENT real helm/goldengate/ source tree, as a list of (goldengate/<relative-path>, bytes) pairs -- with Chart.yaml's version/appVersion overridden to chart_version, exactly what a genuine `helm package --version X --app-version X` invocation of the current chart produces. Reads the real tree at test time -- never a manually-maintained list -- the same non-hardcoded principle as the production _expected_chart_package_members() this exercises."""
    import yaml as _yaml
    members = []
    for path in sorted(phase5_runtime.HELM_CHART_PATH.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(phase5_runtime.HELM_CHART_PATH).as_posix()
        content = path.read_bytes()
        if relative == "Chart.yaml":
            chart_dict = _yaml.safe_load(content)
            chart_dict["version"] = chart_version
            chart_dict["appVersion"] = chart_version
            content = _yaml.safe_dump(chart_dict, default_flow_style=False).encode()
        members.append((f"goldengate/{relative}", content))
    return members


def _write_chart_tar(package_path, file_members, raw_infos=None):
    """Low-level tar builder for test fixtures: file_members is a list of (name, content_bytes) pairs for regular files (a name may legitimately repeat, to construct a duplicate-member test fixture); raw_infos is an optional list of ready-made tarfile.TarInfo objects (symlink/hardlink/device/FIFO/any type) appended as-is, since the regular file_members API cannot express those types."""
    import tarfile
    package_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(package_path, mode="w:gz") as tar:
        for member_name, content_bytes in file_members:
            info = tarfile.TarInfo(name=member_name)
            info.size = len(content_bytes)
            tar.addfile(info, io.BytesIO(content_bytes))
        for info in (raw_infos or []):
            tar.addfile(info)


def _mock_repo_root_with_real_chart(repo_root):
    """Consistently mocks BOTH phase5_runtime.REPO_ROOT and phase5_runtime.HELM_CHART_PATH at repo_root/helm/goldengate -- required by _validate_canonical_chart_source_root()'s HELM_CHART_PATH == REPO_ROOT/helm/goldengate identity contract. Physically copies the REAL current chart tree into that location first (a genuine directory, never a symlink) if not already present, so a test mocking REPO_ROOT to a scratch directory still validates package contents against a real, non-symlinked mirror of the current canonical chart source. Use this instead of mocking REPO_ROOT alone wherever a test exercises package/chart-content validation (cmd_publish_chart, cmd_validate_local, _package_runtime_chart)."""
    import shutil as _shutil
    chart_path = repo_root / "helm" / phase5_runtime.CHART_NAME
    if not chart_path.exists():
        chart_path.parent.mkdir(parents=True, exist_ok=True)
        _shutil.copytree(phase5_runtime.HELM_CHART_PATH, chart_path)
    return mock.patch.multiple(phase5_runtime, REPO_ROOT=repo_root, HELM_CHART_PATH=chart_path)


def _build_fake_chart_package(repo_root, chart_version, environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, values_bytes=b"runtime:\n  containerName: goldengate\n",
                               chart_yaml_name=None, chart_yaml_version=None, chart_yaml_app_version=None,
                               include_values_deployment=True, values_deployment_bytes=None,
                               member_overrides=None, omit_members=(), extra_file_members=(), raw_infos=None):
    """Builds a REAL .tgz that, by default, mirrors the CURRENT real helm/goldengate/ source tree exactly (via _canonical_chart_file_members()) plus goldengate/values-deployment.yaml -- matching the on-disk layout _package_runtime_chart()/helm package actually produce -- plus the CURRENT envs/<environment>/<deployment_id>/values.yaml it must byte-for-byte match. repo_root must already be the mocked phase5_runtime.REPO_ROOT. chart_yaml_name/chart_yaml_version/chart_yaml_app_version independently corrupt ONLY the packaged Chart.yaml's own fields (re-serialized); member_overrides/omit_members/extra_file_members let a test corrupt/omit/add specific archive members (e.g. a modified template, a missing template, an extra file, a non-canonical root) to prove the COMPLETE archive -- not merely Chart.yaml/values-deployment.yaml -- is verified against the current chart source. Returns the package's canonical relative path string."""
    import yaml as _yaml

    values_path = repo_root / "envs" / environment / deployment_id / "values.yaml"
    values_path.parent.mkdir(parents=True, exist_ok=True)
    values_path.write_bytes(values_bytes)

    values_deployment_bytes = values_bytes if values_deployment_bytes is None else values_deployment_bytes
    file_members = _canonical_chart_file_members(chart_version)

    if chart_yaml_name is not None or chart_yaml_version is not None or chart_yaml_app_version is not None:
        current_chart_dict = _yaml.safe_load((phase5_runtime.HELM_CHART_PATH / "Chart.yaml").read_bytes())
        current_chart_dict["name"] = chart_yaml_name if chart_yaml_name is not None else current_chart_dict["name"]
        current_chart_dict["version"] = chart_yaml_version if chart_yaml_version is not None else chart_version
        current_chart_dict["appVersion"] = chart_yaml_app_version if chart_yaml_app_version is not None else chart_version
        corrupted_chart_yaml_bytes = _yaml.safe_dump(current_chart_dict, default_flow_style=False).encode()
        file_members = [(name, corrupted_chart_yaml_bytes if name == "goldengate/Chart.yaml" else content) for name, content in file_members]

    if include_values_deployment:
        file_members.append(("goldengate/values-deployment.yaml", values_deployment_bytes))

    if member_overrides:
        overridden = dict(member_overrides)
        file_members = [(name, overridden.pop(name, content)) for name, content in file_members]
        file_members.extend(overridden.items())
    if omit_members:
        file_members = [(name, content) for name, content in file_members if name not in omit_members]
    file_members = list(file_members) + list(extra_file_members)

    package_path = repo_root / "packaged" / f"goldengate-{chart_version}.tgz"
    _write_chart_tar(package_path, file_members, raw_infos=raw_infos)
    return f"packaged/{package_path.name}"


class EcrRepositoryTests(unittest.TestCase):
    def test_describe_success_no_create(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "ecr", "describe-repositories"), FakeProc(0, ""))
        with mock.patch.object(phase5_runtime, "run", scripted):
            with redirect_stdout(io.StringIO()):
                phase5_runtime._ensure_ecr_repository("helm/goldengate", "eu-west-1")
        create_calls = [c for c in scripted.calls if c["argv"][:3] == ["aws", "ecr", "create-repository"]]
        self.assertEqual(create_calls, [])

    def test_explicit_repository_not_found_creates(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "ecr", "describe-repositories"), FakeProc(1, "", "RepositoryNotFoundException: repo not found"))
        scripted.when(_starts_with("aws", "ecr", "create-repository"), FakeProc(0, ""))
        with mock.patch.object(phase5_runtime, "run", scripted):
            with redirect_stdout(io.StringIO()):
                phase5_runtime._ensure_ecr_repository("helm/goldengate", "eu-west-1")
        create_calls = [c for c in scripted.calls if c["argv"][:3] == ["aws", "ecr", "create-repository"]]
        self.assertEqual(len(create_calls), 1)

    def _fails_closed_zero_create(self, error_text):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "ecr", "describe-repositories"), FakeProc(1, "", error_text))
        with mock.patch.object(phase5_runtime, "run", scripted):
            with self.assertRaises(phase5_runtime.Phase5Error):
                phase5_runtime._ensure_ecr_repository("helm/goldengate", "eu-west-1")
        create_calls = [c for c in scripted.calls if c["argv"][:3] == ["aws", "ecr", "create-repository"]]
        self.assertEqual(create_calls, [], f"must never create for: {error_text}")

    def test_access_denied_fails_zero_create(self):
        self._fails_closed_zero_create("An error occurred (AccessDeniedException)")

    def test_expired_token_fails_zero_create(self):
        self._fails_closed_zero_create("An error occurred (ExpiredTokenException)")

    def test_invalid_client_token_fails_zero_create(self):
        self._fails_closed_zero_create("An error occurred (InvalidClientTokenId)")

    def test_throttling_fails_zero_create(self):
        self._fails_closed_zero_create("An error occurred (ThrottlingException)")

    def test_network_unknown_empty_error_fails_zero_create(self):
        self._fails_closed_zero_create("")
        self._fails_closed_zero_create("connection reset by peer")

    def test_exact_repository_create_settings(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "ecr", "describe-repositories"), FakeProc(1, "", "RepositoryNotFoundException"))
        scripted.when(_starts_with("aws", "ecr", "create-repository"), FakeProc(0, ""))
        with mock.patch.object(phase5_runtime, "run", scripted):
            with redirect_stdout(io.StringIO()):
                phase5_runtime._ensure_ecr_repository("helm/goldengate", "eu-west-1")
        create_call = next(c["argv"] for c in scripted.calls if c["argv"][:3] == ["aws", "ecr", "create-repository"])
        self.assertIn("Key=ApplicationName,Value=CloudFactory", create_call)
        self.assertIn("Key=DataClassification,Value=General", create_call)
        self.assertIn("Key=BusinessCriticality,Value=Low", create_call)
        self.assertIn("Key=BusinessUnit,Value=TechnologyPlatform", create_call)
        self.assertIn("Key=CostCenter,Value=219", create_call)
        self.assertIn("scanOnPush=true", create_call)
        self.assertIn("MUTABLE", create_call)

    def test_race_safe_repository_already_exists_requires_successful_redescribe(self):
        scripted = ScriptedRun()
        calls_seen = {"describe": 0}

        def describe_responder(argv):
            calls_seen["describe"] += 1
            return calls_seen["describe"] == 1

        scripted.when(describe_responder, FakeProc(1, "", "RepositoryNotFoundException"))
        scripted.when(lambda argv: argv[:3] == ["aws", "ecr", "describe-repositories"] and calls_seen["describe"] > 1, FakeProc(0, ""))
        scripted.when(_starts_with("aws", "ecr", "create-repository"), FakeProc(1, "", "RepositoryAlreadyExistsException"))
        with mock.patch.object(phase5_runtime, "run", scripted):
            with redirect_stdout(io.StringIO()):
                phase5_runtime._ensure_ecr_repository("helm/goldengate", "eu-west-1")
        describe_calls = [c for c in scripted.calls if c["argv"][:3] == ["aws", "ecr", "describe-repositories"]]
        self.assertGreaterEqual(len(describe_calls), 2)

    def test_repository_policy_not_found_initializes_empty(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "ecr", "get-repository-policy"), FakeProc(1, "", "RepositoryPolicyNotFoundException"))
        scripted.when(_starts_with("aws", "ecr", "set-repository-policy"), FakeProc(0, ""))
        with mock.patch.object(phase5_runtime, "run", scripted):
            with redirect_stdout(io.StringIO()):
                phase5_runtime._ensure_ecr_repository_policy("helm/goldengate", "eu-west-1", ARGOCD_ECR_READ_ROLE_ARN)
        set_call = next(c for c in scripted.calls if c["argv"][:3] == ["aws", "ecr", "set-repository-policy"])
        policy = json.loads(set_call["file_contents"])
        self.assertEqual(len(policy["Statement"]), 1)

    def test_repository_policy_access_denied_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "ecr", "get-repository-policy"), FakeProc(1, "", "AccessDeniedException"))
        with mock.patch.object(phase5_runtime, "run", scripted):
            with self.assertRaises(phase5_runtime.Phase5Error):
                phase5_runtime._ensure_ecr_repository_policy("helm/goldengate", "eu-west-1", ARGOCD_ECR_READ_ROLE_ARN)

    def test_unrelated_policy_statements_preserved(self):
        existing_policy = {"Version": "2012-10-17", "Statement": [{"Sid": "SomeUnrelatedStatement", "Effect": "Allow", "Principal": {"AWS": "arn:aws:iam::123:role/other"}, "Action": ["ecr:GetDownloadUrlForLayer"]}]}
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "ecr", "get-repository-policy"), FakeProc(0, json.dumps(existing_policy)))
        scripted.when(_starts_with("aws", "ecr", "set-repository-policy"), FakeProc(0, ""))
        with mock.patch.object(phase5_runtime, "run", scripted):
            with redirect_stdout(io.StringIO()):
                phase5_runtime._ensure_ecr_repository_policy("helm/goldengate", "eu-west-1", ARGOCD_ECR_READ_ROLE_ARN)
        set_call = next(c for c in scripted.calls if c["argv"][:3] == ["aws", "ecr", "set-repository-policy"])
        policy = json.loads(set_call["file_contents"])
        sids = {s["Sid"] for s in policy["Statement"]}
        self.assertIn("SomeUnrelatedStatement", sids)
        self.assertIn(phase5_runtime.ARGOCD_ECR_STATEMENT_SID, sids)

    def test_exact_argo_pull_sid_principal_actions(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "ecr", "get-repository-policy"), FakeProc(1, "", "RepositoryPolicyNotFoundException"))
        scripted.when(_starts_with("aws", "ecr", "set-repository-policy"), FakeProc(0, ""))
        with mock.patch.object(phase5_runtime, "run", scripted):
            with redirect_stdout(io.StringIO()):
                phase5_runtime._ensure_ecr_repository_policy("helm/goldengate", "eu-west-1", ARGOCD_ECR_READ_ROLE_ARN)
        set_call = next(c for c in scripted.calls if c["argv"][:3] == ["aws", "ecr", "set-repository-policy"])
        policy = json.loads(set_call["file_contents"])
        statement = policy["Statement"][0]
        self.assertEqual(statement["Sid"], "AllowArgocdEksRolePullGoldengateHelmChart")
        self.assertEqual(statement["Principal"], {"AWS": ARGOCD_ECR_READ_ROLE_ARN})
        self.assertEqual(statement["Action"], phase5_runtime.REPOSITORY_PULL_ACTIONS)

    def test_jq_never_invoked(self):
        source = TOOL_PATH.read_text()
        self.assertNotIn('"jq"', source)
        self.assertNotIn("'jq'", source)

    def test_ecr_password_passes_through_stdin_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            package_path_rel = _build_fake_chart_package(Path(tmp), CHART_VERSION)
            phase5_runtime.update_state(state_path, {
                **_reconcile_state_fixture(), "package_path": package_path_rel,
            }, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)

            scripted = ScriptedRun()
            scripted.when(_starts_with("aws", "ecr", "get-login-password"), FakeProc(0, "SECRET_PASSWORD_VALUE\n"))
            scripted.when(_starts_with("helm", "registry", "login"), FakeProc(0, ""))
            scripted.when(_starts_with("aws", "ecr", "describe-repositories"), FakeProc(0, ""))
            scripted.when(_starts_with("aws", "ecr", "get-repository-policy"), FakeProc(1, "", "RepositoryPolicyNotFoundException"))
            scripted.when(_starts_with("aws", "ecr", "set-repository-policy"), FakeProc(0, ""))
            scripted.when(_starts_with("helm", "push"), FakeProc(0, ""))
            scripted.when(_starts_with("helm", "pull"), FakeProc(0, ""))
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            with _mock_repo_root_with_real_chart(Path(tmp)), mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                _run_quiet(phase5_runtime.cmd_publish_chart, args)

            login_call = next(c for c in scripted.calls if c["argv"][:3] == ["helm", "registry", "login"])
            self.assertEqual(login_call["input_text"], "SECRET_PASSWORD_VALUE")
            self.assertNotIn("SECRET_PASSWORD_VALUE", login_call["argv"])
            for call in scripted.calls:
                self.assertNotIn("SECRET_PASSWORD_VALUE", call["argv"])

    def test_password_never_stored_or_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            package_path_rel = _build_fake_chart_package(Path(tmp), CHART_VERSION)
            phase5_runtime.update_state(state_path, {
                **_reconcile_state_fixture(), "package_path": package_path_rel,
            }, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)

            scripted = ScriptedRun()
            scripted.when(_starts_with("aws", "ecr", "get-login-password"), FakeProc(0, "SECRET_PASSWORD_VALUE\n"))
            scripted.when(_starts_with("helm", "registry", "login"), FakeProc(0, ""))
            scripted.when(_starts_with("aws", "ecr", "describe-repositories"), FakeProc(0, ""))
            scripted.when(_starts_with("aws", "ecr", "get-repository-policy"), FakeProc(1, "", "RepositoryPolicyNotFoundException"))
            scripted.when(_starts_with("aws", "ecr", "set-repository-policy"), FakeProc(0, ""))
            scripted.when(_starts_with("helm", "push"), FakeProc(0, ""))
            scripted.when(_starts_with("helm", "pull"), FakeProc(0, ""))
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            buf = io.StringIO()
            with _mock_repo_root_with_real_chart(Path(tmp)), mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                with redirect_stdout(buf):
                    phase5_runtime.cmd_publish_chart(args)
            self.assertNotIn("SECRET_PASSWORD_VALUE", buf.getvalue())
            self.assertNotIn("SECRET_PASSWORD_VALUE", json.dumps(phase5_runtime.load_state(state_path)))

    def test_chart_push_failure_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            package_path_rel = _build_fake_chart_package(Path(tmp), CHART_VERSION)
            phase5_runtime.update_state(state_path, {
                **_reconcile_state_fixture(), "package_path": package_path_rel,
            }, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)

            scripted = ScriptedRun()
            scripted.when(_starts_with("aws", "ecr", "get-login-password"), FakeProc(0, "pw"))
            scripted.when(_starts_with("helm", "registry", "login"), FakeProc(0, ""))
            scripted.when(_starts_with("aws", "ecr", "describe-repositories"), FakeProc(0, ""))
            scripted.when(_starts_with("aws", "ecr", "get-repository-policy"), FakeProc(1, "", "RepositoryPolicyNotFoundException"))
            scripted.when(_starts_with("aws", "ecr", "set-repository-policy"), FakeProc(0, ""))
            scripted.when(_starts_with("helm", "push"), FakeProc(1, "", "push failed"))
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            with _mock_repo_root_with_real_chart(Path(tmp)), mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error):
                    _run_quiet(phase5_runtime.cmd_publish_chart, args)
            self.assertEqual(len([c for c in scripted.calls if c["argv"][:2] == ["helm", "push"]]), 1, "the canonical package must pass all artifact validation and genuinely reach helm push before this scripted failure")

    def test_version_pullback_failure_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            package_path_rel = _build_fake_chart_package(Path(tmp), CHART_VERSION)
            phase5_runtime.update_state(state_path, {
                **_reconcile_state_fixture(), "package_path": package_path_rel,
            }, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)

            scripted = ScriptedRun()
            scripted.when(_starts_with("aws", "ecr", "get-login-password"), FakeProc(0, "pw"))
            scripted.when(_starts_with("helm", "registry", "login"), FakeProc(0, ""))
            scripted.when(_starts_with("aws", "ecr", "describe-repositories"), FakeProc(0, ""))
            scripted.when(_starts_with("aws", "ecr", "get-repository-policy"), FakeProc(1, "", "RepositoryPolicyNotFoundException"))
            scripted.when(_starts_with("aws", "ecr", "set-repository-policy"), FakeProc(0, ""))
            scripted.when(_starts_with("helm", "push"), FakeProc(0, ""))
            scripted.when(_starts_with("helm", "pull"), FakeProc(1, "", "pull failed"))
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            with _mock_repo_root_with_real_chart(Path(tmp)), mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error):
                    _run_quiet(phase5_runtime.cmd_publish_chart, args)
            self.assertEqual(len([c for c in scripted.calls if c["argv"][:2] == ["helm", "pull"]]), 1, "the canonical package must pass all artifact validation and genuinely reach helm pull before this scripted failure")


# ==== CLUSTER PREREQUISITE TESTS ====

def _base_prereq_scripted():
    scripted = ScriptedRun()
    scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
    scripted.when(_starts_with("kubectl", "config", "current-context"), FakeProc(0, ""))
    scripted.when(_starts_with("kubectl", "get", "csidriver"), FakeProc(0, ""))
    scripted.when(_starts_with("kubectl", "get", "crd", "secretproviderclasses.secrets-store.csi.x-k8s.io"), FakeProc(0, ""))
    scripted.when(lambda argv: argv[:4] == ["kubectl", "get", "csidriver", "secrets-store.csi.k8s.io"] and "-o" in argv,
                  FakeProc(0, '["sts.amazonaws.com","pods.eks.amazonaws.com"]'))
    scripted.when(_starts_with("helm", "status", "secrets-store-csi-driver"), FakeProc(1, "", "Error: release: not found"))
    scripted.when(_starts_with("kubectl", "get", "crd", "applications.argoproj.io"), FakeProc(0, ""))
    return scripted


class ClusterPrerequisiteTests(unittest.TestCase):
    def test_update_kubeconfig_exact_role_semantics(self):
        scripted = _base_prereq_scripted()
        args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID)
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            _run_quiet(phase5_runtime.cmd_validate_cluster_prerequisites, args)
        call = next(c["argv"] for c in scripted.calls if c["argv"][:3] == ["aws", "eks", "update-kubeconfig"])
        self.assertIn("--role-arn", call)
        self.assertIn(EKS_DEPLOY_ROLE_ARN, call)
        self.assertIn("--assume-role-arn", call)

    def test_missing_csidriver_fails(self):
        scripted = _base_prereq_scripted()
        scripted.when(_starts_with("kubectl", "get", "csidriver"), FakeProc(1, "", "not found"))
        args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID)
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_validate_cluster_prerequisites, args)

    def test_missing_secretproviderclass_crd_fails(self):
        scripted = _base_prereq_scripted()
        scripted.when(_starts_with("kubectl", "get", "crd", "secretproviderclasses.secrets-store.csi.x-k8s.io"), FakeProc(1, "", "not found"))
        args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID)
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_validate_cluster_prerequisites, args)

    def test_missing_sts_audience_fails(self):
        scripted = _base_prereq_scripted()
        scripted.when(lambda argv: argv[:4] == ["kubectl", "get", "csidriver", "secrets-store.csi.k8s.io"] and "-o" in argv, FakeProc(0, '["pods.eks.amazonaws.com"]'))
        args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID)
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_validate_cluster_prerequisites, args)

    def test_missing_pods_eks_audience_fails(self):
        scripted = _base_prereq_scripted()
        scripted.when(lambda argv: argv[:4] == ["kubectl", "get", "csidriver", "secrets-store.csi.k8s.io"] and "-o" in argv, FakeProc(0, '["sts.amazonaws.com"]'))
        args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID)
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_validate_cluster_prerequisites, args)

    def test_syncsecret_literal_true_passes(self):
        self.assertIsNone(_run_quiet(phase5_runtime._validate_sync_secret_enabled, json.dumps({"syncSecret": {"enabled": True}})))

    def test_syncsecret_false_fails(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_sync_secret_enabled(json.dumps({"syncSecret": {"enabled": False}}))

    def test_syncsecret_string_true_fails(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_sync_secret_enabled(json.dumps({"syncSecret": {"enabled": "true"}}))

    def test_syncsecret_missing_fails(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_sync_secret_enabled(json.dumps({}))
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_sync_secret_enabled(json.dumps({"syncSecret": {}}))

    def test_malformed_helm_json_fails(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_sync_secret_enabled("{not valid json")

    def test_csi_helm_release_absent_preserves_skip_behavior(self):
        scripted = _base_prereq_scripted()
        args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID)
        buf = io.StringIO()
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            with redirect_stdout(buf):
                phase5_runtime.cmd_validate_cluster_prerequisites(args)
        self.assertIn("Skipping syncSecret check", buf.getvalue())

    def test_missing_argo_application_crd_fails(self):
        scripted = _base_prereq_scripted()
        scripted.when(_starts_with("kubectl", "get", "crd", "applications.argoproj.io"), FakeProc(1, "", "not found"))
        args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID)
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_validate_cluster_prerequisites, args)


class HelmReleaseStatusClassificationTests(unittest.TestCase):
    """Issue 1: _classify_helm_release_status()/_validate_csi_prerequisites() must never infer Helm release absence from a generic non-zero exit code -- only an EXPLICIT 'release: not found' authorizes skipping the syncSecret Helm-values check."""

    def _classify(self, status_proc):
        scripted = ScriptedRun()
        scripted.when(_starts_with("helm", "status", "secrets-store-csi-driver"), status_proc)
        with mock.patch.object(phase5_runtime, "run", scripted):
            return phase5_runtime._classify_helm_release_status("secrets-store-csi-driver", "kube-system"), scripted

    def test_1_helm_release_exists_syncsecret_true_passes(self):
        scripted = _base_prereq_scripted()
        scripted.when(_starts_with("helm", "status", "secrets-store-csi-driver"), FakeProc(0, "STATUS: deployed"))
        scripted.when(_starts_with("helm", "get", "values", "secrets-store-csi-driver"), FakeProc(0, json.dumps({"syncSecret": {"enabled": True}})))
        args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID)
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            _run_quiet(phase5_runtime.cmd_validate_cluster_prerequisites, args)
        values_calls = [c for c in scripted.calls if c["argv"][:3] == ["helm", "get", "values"]]
        self.assertEqual(len(values_calls), 1)

    def test_2_explicit_release_not_found_skips_helm_values_check_safely(self):
        status, scripted = self._classify(FakeProc(1, "", "Error: release: not found"))
        self.assertEqual(status, "not-found")

    def test_3_kubernetes_cluster_unreachable_fails_closed(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            self._classify(FakeProc(1, "", "Error: Kubernetes cluster unreachable: context deadline exceeded"))

    def test_4_forbidden_fails_closed(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            self._classify(FakeProc(1, "", "Error: secrets is forbidden: User cannot list resource"))

    def test_5_unauthorized_fails_closed(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            self._classify(FakeProc(1, "", "error: You must be logged in to the server (Unauthorized)"))

    def test_6_timeout_context_deadline_fails_closed(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            self._classify(FakeProc(1, "", "Error: context deadline exceeded"))

    def test_7_network_tls_failure_fails_closed(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            self._classify(FakeProc(1, "", "Error: Get \"https://x\": dial tcp: connection refused"))
        with self.assertRaises(phase5_runtime.Phase5Error):
            self._classify(FakeProc(1, "", "Error: tls: handshake failure"))

    def test_8_unknown_error_fails_closed(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            self._classify(FakeProc(1, "", "Error: some completely unrecognized failure mode"))

    def test_9_empty_error_with_nonzero_fails_closed(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            self._classify(FakeProc(1, "", ""))

    def test_10_error_failure_never_invokes_helm_get_values(self):
        scripted = _base_prereq_scripted()
        scripted.when(_starts_with("helm", "status", "secrets-store-csi-driver"), FakeProc(1, "", "Error: Kubernetes cluster unreachable: context deadline exceeded"))
        args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID)
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_validate_cluster_prerequisites, args)
        values_calls = [c for c in scripted.calls if c["argv"][:3] == ["helm", "get", "values"]]
        self.assertEqual(values_calls, [])


# ==== ARGO RECONCILIATION TESTS ====

class ArgoApplicationManifestTests(unittest.TestCase):
    def _manifest(self, **overrides):
        kwargs = dict(
            argocd_app_name="goldengate-dev-oracle-payments-01", argocd_namespace="argocd", environment=ENVIRONMENT,
            deployment_id=DEPLOYMENT_ID, helm_chart_ref=f"oci://{ECR_REGISTRY}/helm/goldengate", chart_version="0.1.1-gg-x",
            release_name=DEPLOYMENT_ID, target_namespace="goldengate-dev", image_repository=IMAGE_REPOSITORY,
            dns_domain="goldengate-dev.adcbmis.local", alb_group_name="goldengate-dev-shared",
            certificate_arn="arn:aws:acm:eu-west-1:668311715351:certificate/abc", admin_secret_name="dev/goldengate/source/admin",
            tls_secret_name="dev/goldengate/tls-certificate", aws_region="eu-west-1", runtime_service_account_name="gg-runtime-sa",
            resolved_efs_id="",
        )
        kwargs.update(overrides)
        return phase5_runtime._build_runtime_application_manifest(**kwargs)

    def test_manifest_exact_name_labels_finalizer(self):
        m = self._manifest()
        self.assertEqual(m["metadata"]["name"], "goldengate-dev-oracle-payments-01")
        self.assertEqual(m["metadata"]["finalizers"], ["resources-finalizer.argocd.argoproj.io"])
        self.assertEqual(m["metadata"]["labels"], {
            "app.kubernetes.io/name": "goldengate", "app.kubernetes.io/managed-by": "argocd",
            "goldengate.adcb/environment": ENVIRONMENT, "goldengate.adcb/deployment-id": DEPLOYMENT_ID,
        })

    def test_source_repourl_exact(self):
        self.assertEqual(self._manifest()["spec"]["source"]["repoURL"], f"oci://{ECR_REGISTRY}/helm/goldengate")

    def test_target_revision_exact(self):
        self.assertEqual(self._manifest()["spec"]["source"]["targetRevision"], "0.1.1-gg-x")

    def test_release_name_exact(self):
        self.assertEqual(self._manifest()["spec"]["source"]["helm"]["releaseName"], DEPLOYMENT_ID)

    def test_values_deployment_yaml_exact(self):
        self.assertEqual(self._manifest()["spec"]["source"]["helm"]["valueFiles"], ["values-deployment.yaml"])

    def test_all_current_helm_parameter_overrides_preserved(self):
        params = {p["name"]: p["value"] for p in self._manifest()["spec"]["source"]["helm"]["parameters"]}
        expected_keys = {"global.environment", "runtime.image.repository", "ingress.hostDomain", "ingress.alb.groupName",
                          "ingress.alb.certificateArn", "runtime.csi.admin.objectName", "runtime.csi.certificate.objectName",
                          "runtime.csi.region", "runtime.serviceAccount.create", "runtime.serviceAccount.name", "persistence.efs.fileSystemId"}
        self.assertEqual(set(params), expected_keys)
        self.assertEqual(params["runtime.serviceAccount.create"], "false")

    def test_destination_namespace_exact(self):
        self.assertEqual(self._manifest()["spec"]["destination"]["namespace"], "goldengate-dev")
        self.assertEqual(self._manifest()["spec"]["destination"]["server"], "https://kubernetes.default.svc")

    def test_no_createnamespace_ownership(self):
        m = self._manifest()
        sync_policy = m["spec"]["syncPolicy"]
        self.assertNotIn("syncOptions", sync_policy)
        self.assertNotIn("managedNamespaceMetadata", sync_policy)

    def test_automated_prune_selfheal_exact(self):
        self.assertEqual(self._manifest()["spec"]["syncPolicy"]["automated"], {"prune": True, "selfHeal": True})
        self.assertEqual(self._manifest()["spec"]["revisionHistoryLimit"], 10)


class ArgoWaitTests(unittest.TestCase):
    def _base_scripted(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("kubectl", "get", "application"), FakeProc(0, ""))
        return scripted

    def test_immediate_synced_healthy_succeeds(self):
        scripted = self._base_scripted()
        scripted.when(lambda argv: "jsonpath={.status.sync.status}" in argv, FakeProc(0, "Synced"))
        scripted.when(lambda argv: "jsonpath={.status.health.status}" in argv, FakeProc(0, "Healthy"))
        with mock.patch.object(phase5_runtime, "run", scripted):
            _run_quiet(phase5_runtime._wait_for_runtime_argo_application, "app", "argocd", 1200, 30)

    def test_degraded_fails_immediately(self):
        scripted = self._base_scripted()
        scripted.when(lambda argv: "jsonpath={.status.sync.status}" in argv, FakeProc(0, "OutOfSync"))
        scripted.when(lambda argv: "jsonpath={.status.health.status}" in argv, FakeProc(0, "Degraded"))
        with mock.patch.object(phase5_runtime, "run", scripted), mock.patch.object(phase5_runtime.time, "sleep") as sleep_mock:
            with self.assertRaises(phase5_runtime.Phase5Error) as ctx:
                _run_quiet(phase5_runtime._wait_for_runtime_argo_application, "app", "argocd", 1200, 30)
            self.assertIn("Degraded", str(ctx.exception))
        sleep_mock.assert_not_called()

    def test_operation_failed_fails(self):
        scripted = self._base_scripted()
        scripted.when(lambda argv: "jsonpath={.status.sync.status}" in argv, FakeProc(0, "OutOfSync"))
        scripted.when(lambda argv: "jsonpath={.status.health.status}" in argv, FakeProc(0, "Progressing"))
        scripted.when(lambda argv: "jsonpath={.status.operationState.phase}" in argv, FakeProc(0, "Failed"))
        scripted.when(_starts_with("kubectl", "describe"), FakeProc(0, ""))
        with mock.patch.object(phase5_runtime, "run", scripted), mock.patch.object(phase5_runtime.time, "sleep"):
            with self.assertRaises(phase5_runtime.Phase5Error) as ctx:
                _run_quiet(phase5_runtime._wait_for_runtime_argo_application, "app", "argocd", 1200, 30)
            self.assertIn("Failed", str(ctx.exception))

    def test_operation_error_fails(self):
        scripted = self._base_scripted()
        scripted.when(lambda argv: "jsonpath={.status.sync.status}" in argv, FakeProc(0, "OutOfSync"))
        scripted.when(lambda argv: "jsonpath={.status.health.status}" in argv, FakeProc(0, "Progressing"))
        scripted.when(lambda argv: "jsonpath={.status.operationState.phase}" in argv, FakeProc(0, "Error"))
        scripted.when(_starts_with("kubectl", "describe"), FakeProc(0, ""))
        with mock.patch.object(phase5_runtime, "run", scripted), mock.patch.object(phase5_runtime.time, "sleep"):
            with self.assertRaises(phase5_runtime.Phase5Error) as ctx:
                _run_quiet(phase5_runtime._wait_for_runtime_argo_application, "app", "argocd", 1200, 30)
            self.assertIn("Error", str(ctx.exception))

    def test_timeout_occurs_at_1200s(self):
        scripted = self._base_scripted()
        scripted.when(lambda argv: "jsonpath={.status.sync.status}" in argv, FakeProc(0, "OutOfSync"))
        scripted.when(lambda argv: "jsonpath={.status.health.status}" in argv, FakeProc(0, "Progressing"))
        scripted.when(_starts_with("kubectl", "describe"), FakeProc(0, ""))
        with mock.patch.object(phase5_runtime, "run", scripted), mock.patch.object(phase5_runtime.time, "sleep") as sleep_mock:
            with self.assertRaises(phase5_runtime.Phase5Error) as ctx:
                _run_quiet(phase5_runtime._wait_for_runtime_argo_application, "app", "argocd", 1200, 30)
            self.assertIn("Timed out after 1200s", str(ctx.exception))
        total_slept = sum(c.args[0] for c in sleep_mock.call_args_list)
        self.assertGreaterEqual(total_slept, 1200)

    def test_final_boundary_probe_preserved(self):
        """At the timeout boundary, a final kubectl get/describe probe still runs (bounded, not an unbounded loop)."""
        scripted = self._base_scripted()
        scripted.when(lambda argv: "jsonpath={.status.sync.status}" in argv, FakeProc(0, "OutOfSync"))
        scripted.when(lambda argv: "jsonpath={.status.health.status}" in argv, FakeProc(0, "Progressing"))
        scripted.when(_starts_with("kubectl", "describe"), FakeProc(0, ""))
        with mock.patch.object(phase5_runtime, "run", scripted), mock.patch.object(phase5_runtime.time, "sleep"):
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime._wait_for_runtime_argo_application, "app", "argocd", 1200, 30)
        describe_calls = [c for c in scripted.calls if c["argv"][:2] == ["kubectl", "describe"]]
        self.assertGreaterEqual(len(describe_calls), 1)


# ==== EMERGENCY FALLBACK TESTS (part of reconcile-runtime) ====

def _full_reconcile_state(state_path):
    """Includes every field _validate_resolved_runtime_inputs() cross-checks against a FRESH deployment-model descriptor -- see _reconcile_scripted_ok(), which scripts that describe call to return the matching DESCRIPTOR/_descriptor() fixture."""
    phase5_runtime.update_state(state_path, {
        **_reconcile_state_fixture(),
        "image_repository": IMAGE_REPOSITORY, "image_repository_name": "aws-cloud-factory-goldengate-oracle", "image_tag": "23.4.0.0",
        "dns_domain": "goldengate-dev.adcbmis.local", "alb_group_name": "goldengate-dev-shared",
        "certificate_arn": f"arn:aws:acm:eu-west-1:{WORKLOAD_ACCOUNT_ID}:certificate/abc-123",
        "admin_secret_name": "dev/goldengate/source/admin", "tls_secret_name": "dev/goldengate/tls-certificate",
        "runtime_service_account_name": "gg-runtime-sa", "resolved_efs_id": "", "image_digest": SAMPLE_IMAGE_DIGEST,
        "efs_mode": "", "efs_file_system_id_declared": "", "efs_creation_token": "",
    }, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)


def _reconcile_scripted_ok():
    scripted = ScriptedRun()
    scripted.when(_starts_with(sys.executable, str(phase5_runtime.DEPLOYMENT_MODEL_TOOL)), FakeProc(0, json.dumps(_descriptor())))
    scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
    scripted.when(_starts_with("kubectl", "get", "crd", "applications.argoproj.io"), FakeProc(0, ""))
    scripted.when(_starts_with("kubectl", "apply", "-f", "-"), FakeProc(0, ""))
    scripted.when(_starts_with("kubectl", "annotate", "application"), FakeProc(0, ""))
    scripted.when(_starts_with("kubectl", "get", "application"), FakeProc(0, ""))
    scripted.when(lambda argv: "jsonpath={.status.sync.status}" in argv, FakeProc(0, "Synced"))
    scripted.when(lambda argv: "jsonpath={.status.health.status}" in argv, FakeProc(0, "Healthy"))
    return scripted


class EmergencyFallbackTests(TempStateCase):
    def test_disabled_fallback_creates_no_secret(self):
        _full_reconcile_state(self.state_path)
        scripted = _reconcile_scripted_ok()
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch(ENABLE_TEMP_ARGOCD_ECR_PASSWORD_INJECTION="false"):
            _run_quiet(phase5_runtime.cmd_reconcile_runtime, self.args)
        secret_applies = [c for c in scripted.calls if c["input_text"] and "argocd-ecr-goldengate-oci" in c["input_text"]]
        self.assertEqual(secret_applies, [])

    def test_enabled_fallback_creates_exact_repository_secret(self):
        _full_reconcile_state(self.state_path)
        scripted = _reconcile_scripted_ok()
        scripted.when(_starts_with("aws", "ecr", "get-login-password"), FakeProc(0, "SHORTLIVED_PASSWORD\n"))
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch(ENABLE_TEMP_ARGOCD_ECR_PASSWORD_INJECTION="true"):
            _run_quiet(phase5_runtime.cmd_reconcile_runtime, self.args)
        secret_applies = [c for c in scripted.calls if c["input_text"] and "argocd-ecr-goldengate-oci" in c["input_text"]]
        self.assertEqual(len(secret_applies), 1)
        import yaml as _yaml
        manifest = _yaml.safe_load(secret_applies[0]["input_text"])
        self.assertEqual(manifest["kind"], "Secret")
        self.assertEqual(manifest["metadata"]["name"], "argocd-ecr-goldengate-oci")
        self.assertEqual(manifest["metadata"]["labels"], {"argocd.argoproj.io/secret-type": "repository"})
        self.assertEqual(manifest["stringData"]["type"], "helm")
        self.assertEqual(manifest["stringData"]["enableOCI"], "true")
        self.assertEqual(manifest["stringData"]["url"], f"{ECR_REGISTRY}/helm/goldengate")
        self.assertEqual(manifest["stringData"]["username"], "AWS")
        self.assertEqual(manifest["stringData"]["password"], "SHORTLIVED_PASSWORD")

    def test_password_never_appears_in_argv_log_state(self):
        _full_reconcile_state(self.state_path)
        scripted = _reconcile_scripted_ok()
        scripted.when(_starts_with("aws", "ecr", "get-login-password"), FakeProc(0, "SHORTLIVED_PASSWORD\n"))
        buf = io.StringIO()
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch(ENABLE_TEMP_ARGOCD_ECR_PASSWORD_INJECTION="true"):
            with redirect_stdout(buf):
                phase5_runtime.cmd_reconcile_runtime(self.args)
        for call in scripted.calls:
            self.assertNotIn("SHORTLIVED_PASSWORD", call["argv"])
        self.assertNotIn("SHORTLIVED_PASSWORD", buf.getvalue())
        self.assertNotIn("SHORTLIVED_PASSWORD", json.dumps(phase5_runtime.load_state(self.state_path)))

    def test_password_supplied_only_through_manifest_process_stdin(self):
        _full_reconcile_state(self.state_path)
        scripted = _reconcile_scripted_ok()
        scripted.when(_starts_with("aws", "ecr", "get-login-password"), FakeProc(0, "SHORTLIVED_PASSWORD\n"))
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch(ENABLE_TEMP_ARGOCD_ECR_PASSWORD_INJECTION="true"):
            _run_quiet(phase5_runtime.cmd_reconcile_runtime, self.args)
        apply_calls = [c for c in scripted.calls if c["argv"][:3] == ["kubectl", "apply", "-f"]]
        self.assertTrue(all(c["input_text"] is not None for c in apply_calls))


# ==== REMOVAL TESTS ====

class PrepareRemovalTests(TempStateCase):
    def _args(self, deployment_model="singleRuntime", efs_mode="", reason="deployment-disabled"):
        return argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, deployment_model=deployment_model,
                                   efs_mode=efs_mode, reason=reason, state_path=self.state_path)

    def test_invalid_removal_deployment_model_fails_before_mutation(self):
        with _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_prepare_removal, self._args(deployment_model="bogusModel"))

    def test_legacypair_fails_before_mutation(self):
        with _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_prepare_removal, self._args(deployment_model="legacyPair"))

    def test_unknown_deletion_reason_fails(self):
        with _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_prepare_removal, self._args(reason="bogus-reason"))

    def test_managed_physical_removal_fails_before_mutation(self):
        with _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_prepare_removal, self._args(efs_mode="managed", reason="physical-removal"))
        self.assertFalse(self.state_path.exists(), "no state (and therefore no downstream step) should be written when this fails closed")

    def test_physical_removal_existing_is_allowed(self):
        with _env_patch():
            _run_quiet(phase5_runtime.cmd_prepare_removal, self._args(efs_mode="existing", reason="physical-removal"))
        state = phase5_runtime.load_state(self.state_path)
        self.assertEqual(state["reason"], "physical-removal")
        self.assertEqual(state["efs_mode"], "existing")

    def test_unrecognized_efs_mode_fails(self):
        with _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_prepare_removal, self._args(efs_mode="bogus"))


class RemovalPreflightTests(TempStateCase):
    def setUp(self):
        super().setUp()
        phase5_runtime.update_state(self.state_path, {
            "environment": ENVIRONMENT, "deployment_id": DEPLOYMENT_ID, "deployment_model": "singleRuntime",
            "efs_mode": "", "reason": "deployment-disabled", "runtime_namespace": "goldengate-dev",
            "argocd_namespace": "argocd", "argocd_app_name": "goldengate-dev-oracle-payments-01",
        }, phase5_runtime.REMOVAL_ALLOWED_STATE_KEYS)

    def _run_preflight(self, classifier_result, classifier_rc=0):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
        scripted.when(_starts_with(sys.executable, str(phase5_runtime.RUNTIME_STATE_TOOL)), FakeProc(classifier_rc, json.dumps(classifier_result) if classifier_rc == 0 else "", "" if classifier_rc == 0 else "inspection error"))
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            _run_quiet(phase5_runtime.cmd_removal_preflight, self.args)
        return scripted

    def test_broken_preflight_fails_before_mutation(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
        scripted.when(_starts_with(sys.executable, str(phase5_runtime.RUNTIME_STATE_TOOL)), FakeProc(0, json.dumps({"state": "BROKEN", "checks": {}})))
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_removal_preflight, self.args)

    def test_inspection_failure_fails_before_mutation(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
        scripted.when(_starts_with(sys.executable, str(phase5_runtime.RUNTIME_STATE_TOOL)), FakeProc(1, "", "inspection error"))
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_removal_preflight, self.args)

    def test_absent_preflight_captures_checks(self):
        self._run_preflight(_classifier_result("ABSENT", False))
        state = phase5_runtime.load_state(self.state_path)
        self.assertEqual(state["ownership_state"], "ABSENT")
        self.assertIs(state["application_found"], False)

    def test_owned_preflight_captures_checks(self):
        self._run_preflight(_classifier_result("OWNED", True, statefulset=True))
        state = phase5_runtime.load_state(self.state_path)
        self.assertEqual(state["ownership_state"], "OWNED")
        self.assertIs(state["application_found"], True)

    def test_retained_pvc_expected_flag_passed_when_efs_mode_set(self):
        phase5_runtime.update_state(self.state_path, {"efs_mode": "existing"}, phase5_runtime.REMOVAL_ALLOWED_STATE_KEYS)
        scripted = self._run_preflight(_classifier_result("OWNED", False, pvc=True))
        classifier_call = next(c["argv"] for c in scripted.calls if str(phase5_runtime.RUNTIME_STATE_TOOL) in c["argv"])
        self.assertIn("--retained-pvc-expected", classifier_call)

    def test_29_malformed_classifier_checks_fails_before_removal_state_persisted(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
        scripted.when(_starts_with(sys.executable, str(phase5_runtime.RUNTIME_STATE_TOOL)), FakeProc(0, json.dumps({"state": "ABSENT", "checks": {"application_found": False}})))
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_removal_preflight, self.args)
        self.assertNotIn("ownership_state", phase5_runtime.load_state(self.state_path))

    def test_30_application_found_string_false_is_rejected(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
        scripted.when(_starts_with(sys.executable, str(phase5_runtime.RUNTIME_STATE_TOOL)), FakeProc(0, json.dumps({"state": "ABSENT", "checks": {"application_found": "false", "footprint_found": _complete_footprint()}})))
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_removal_preflight, self.args)

    def test_31_incomplete_footprint_mapping_fails_before_mutation(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
        scripted.when(_starts_with(sys.executable, str(phase5_runtime.RUNTIME_STATE_TOOL)), FakeProc(0, json.dumps({"state": "ABSENT", "checks": {"application_found": False, "footprint_found": {"statefulset": False}}})))
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_removal_preflight, self.args)

    def test_32_valid_complete_owned_classifier_result_persisted_exactly(self):
        result = _classifier_result("OWNED", True, statefulset=True, service=True)
        self._run_preflight(result)
        state = phase5_runtime.load_state(self.state_path)
        self.assertEqual(state["ownership_state"], "OWNED")
        self.assertIs(state["application_found"], True)
        self.assertEqual(state["footprint_found"], result["checks"]["footprint_found"])


class RemoveRuntimeTests(TempStateCase):
    def _set_state(self, ownership_state, application_found, footprint_found=None):
        phase5_runtime.update_state(self.state_path, {
            **_removal_state_fixture(),
            "ownership_state": ownership_state, "application_found": application_found,
            "footprint_found": _complete_footprint() if footprint_found is None else footprint_found,
        }, phase5_runtime.REMOVAL_ALLOWED_STATE_KEYS)

    def test_broken_cannot_mutate(self):
        self._set_state("BROKEN", True)
        with self.assertRaises(phase5_runtime.Phase5Error):
            _run_quiet(phase5_runtime.cmd_remove_runtime, self.args)

    def test_absent_performs_no_application_mutation(self):
        self._set_state("ABSENT", False)
        scripted = ScriptedRun()
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            _run_quiet(phase5_runtime.cmd_remove_runtime, self.args)
        self.assertEqual(scripted.calls, [])

    def test_owned_application_found_false_no_redundant_get_no_mutation(self):
        self._set_state("OWNED", False)
        scripted = ScriptedRun()
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            _run_quiet(phase5_runtime.cmd_remove_runtime, self.args)
        self.assertEqual(scripted.calls, [], "no kubectl get/patch/delete calls at all -- preflight's own application_found is authoritative")

    def test_owned_application_found_true_patch_delete_allowed(self):
        self._set_state("OWNED", True)
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
        scripted.when(_starts_with("kubectl", "patch", "application"), FakeProc(0, ""))
        scripted.when(_starts_with("kubectl", "delete", "application"), FakeProc(0, ""))
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            _run_quiet(phase5_runtime.cmd_remove_runtime, self.args)
        patch_calls = [c for c in scripted.calls if c["argv"][:2] == ["kubectl", "patch"]]
        delete_calls = [c for c in scripted.calls if c["argv"][:2] == ["kubectl", "delete"]]
        self.assertEqual(len(patch_calls), 1)
        self.assertEqual(len(delete_calls), 1)
        self.assertIn("--wait=true", delete_calls[0]["argv"])
        self.assertIn("--timeout=10m", delete_calls[0]["argv"])

    def test_delete_patch_failure_fails(self):
        self._set_state("OWNED", True)
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
        scripted.when(_starts_with("kubectl", "patch", "application"), FakeProc(1, "", "Forbidden"))
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_remove_runtime, self.args)

    def test_delete_failure_fails(self):
        self._set_state("OWNED", True)
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
        scripted.when(_starts_with("kubectl", "patch", "application"), FakeProc(0, ""))
        scripted.when(_starts_with("kubectl", "delete", "application"), FakeProc(1, "", "timed out"))
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_remove_runtime, self.args)

    def _mutating_call_argvs(self):
        """Behavioral proof (not text-scanning a print/log message, which legitimately documents this guarantee in prose): drives cmd_remove_runtime() down its one real mutating path (OWNED + application_found=True) and returns every actual subprocess argv it issued."""
        self._set_state("OWNED", True)
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
        scripted.when(_starts_with("kubectl", "patch", "application"), FakeProc(0, ""))
        scripted.when(_starts_with("kubectl", "delete", "application"), FakeProc(0, ""))
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            _run_quiet(phase5_runtime.cmd_remove_runtime, self.args)
        return [c["argv"] for c in scripted.calls]

    def test_shared_runtime_namespace_never_deleted(self):
        argvs = self._mutating_call_argvs()
        self.assertFalse(any("namespace" in argv for argv in argvs), "no subprocess call ever targets a Kubernetes namespace resource")

    def test_pvc_never_deleted(self):
        argvs = self._mutating_call_argvs()
        self.assertFalse(any("persistentvolumeclaim" in argv or "pvc" in argv for argv in argvs))

    def test_efs_never_deleted(self):
        argvs = self._mutating_call_argvs()
        self.assertFalse(any("efs" in " ".join(str(a) for a in argv).lower() for argv in argvs))


# ==== CRITICAL DELETE FALSE-SUCCESS REGRESSION TESTS ====

class ClassifierOutputSchemaTests(unittest.TestCase):
    """Issue 2: _validate_runtime_state_classifier_output() must never let a malformed/incomplete classifier shape be silently treated as "everything reads as absent"."""

    def test_11_complete_absent_classifier_result_passes_structural_schema(self):
        state, application_found, footprint_found = phase5_runtime._validate_runtime_state_classifier_output(_classifier_result("ABSENT", False))
        self.assertEqual(state, "ABSENT")
        self.assertIs(application_found, False)
        self.assertEqual(set(footprint_found), RUNTIME_FOOTPRINT_KEYS)

    def test_12_missing_checks_fails(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_runtime_state_classifier_output({"state": "ABSENT"})

    def test_13_missing_application_found_fails(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_runtime_state_classifier_output({"state": "ABSENT", "checks": {"footprint_found": _complete_footprint()}})

    def test_14_application_found_string_false_fails(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_runtime_state_classifier_output({"state": "ABSENT", "checks": {"application_found": "false", "footprint_found": _complete_footprint()}})

    def test_15_application_found_zero_fails(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_runtime_state_classifier_output({"state": "ABSENT", "checks": {"application_found": 0, "footprint_found": _complete_footprint()}})

    def test_16_missing_footprint_found_fails(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_runtime_state_classifier_output({"state": "ABSENT", "checks": {"application_found": False}})

    def test_17_empty_footprint_found_fails(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_runtime_state_classifier_output({"state": "ABSENT", "checks": {"application_found": False, "footprint_found": {}}})

    def test_18_one_missing_expected_footprint_key_fails(self):
        incomplete = _complete_footprint()
        del incomplete["ingress"]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_runtime_state_classifier_output({"state": "ABSENT", "checks": {"application_found": False, "footprint_found": incomplete}})

    def test_19_partial_footprint_cannot_certify_deletion_complete(self):
        result = {"state": "ABSENT", "checks": {"application_found": False, "footprint_found": {"statefulset": False}}}
        ok, why = phase5_runtime._post_delete_positively_absent(result, retained_pvc_expected=False)
        self.assertFalse(ok)

    def test_20_footprint_value_string_false_fails(self):
        bad = _complete_footprint()
        bad["statefulset"] = "false"
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_runtime_state_classifier_output({"state": "ABSENT", "checks": {"application_found": False, "footprint_found": bad}})

    def test_21_footprint_value_zero_fails(self):
        bad = _complete_footprint()
        bad["service"] = 0
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_runtime_state_classifier_output({"state": "ABSENT", "checks": {"application_found": False, "footprint_found": bad}})

    def test_22_unknown_state_fails(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_runtime_state_classifier_output(_classifier_result("SOMETHING_ELSE", False))

    def test_23_missing_state_fails(self):
        result = _classifier_result("ABSENT", False)
        del result["state"]
        with self.assertRaises(phase5_runtime.Phase5Error):
            phase5_runtime._validate_runtime_state_classifier_output(result)

    def test_24_broken_remains_not_complete(self):
        ok, why = phase5_runtime._post_delete_positively_absent(_classifier_result("BROKEN", False), retained_pvc_expected=False)
        self.assertFalse(ok)

    def test_25_complete_absent_every_footprint_false_passes(self):
        ok, why = phase5_runtime._post_delete_positively_absent(_classifier_result("ABSENT", False), retained_pvc_expected=False)
        self.assertTrue(ok, why)

    def test_26_complete_owned_retained_pvc_only_passes_with_hint(self):
        ok, why = phase5_runtime._post_delete_positively_absent(_classifier_result("OWNED", False, pvc=True), retained_pvc_expected=True)
        self.assertTrue(ok, why)

    def test_27_same_owned_retained_pvc_shape_without_hint_fails(self):
        ok, why = phase5_runtime._post_delete_positively_absent(_classifier_result("OWNED", False, pvc=True), retained_pvc_expected=False)
        self.assertFalse(ok)

    def test_28_any_non_pvc_footprint_true_fails(self):
        for key in RUNTIME_FOOTPRINT_KEYS - {"pvc"}:
            ok, why = phase5_runtime._post_delete_positively_absent(_classifier_result("OWNED", False, **{key: True}), retained_pvc_expected=True)
            self.assertFalse(ok, f"footprint key {key!r} unexpectedly allowed completion")

    def test_footprint_key_schema_matches_runtime_state_module(self):
        """Synchronization proof: this test file's own RUNTIME_FOOTPRINT_KEYS constant (used to build fixtures) must never independently drift from the real runtime_state.py classifier's own canonical key set."""
        runtime_state = phase5_runtime._load_runtime_state_module()
        self.assertEqual(RUNTIME_FOOTPRINT_KEYS, set(runtime_state.RUNTIME_FOOTPRINT_KEYS))


class PostDeletePositiveProofTests(unittest.TestCase):
    def test_140_application_still_exists_state_owned_must_not_pass(self):
        """Confirmed reproduction of the current bug: Application exists, labels/destination/repoURL/releaseName all correct, classifier state=OWNED. `state != BROKEN` would incorrectly pass this -- the fixed positive-proof check must not."""
        result = {"state": "OWNED", "checks": {"application_found": True, "footprint_found": {"statefulset": True, "service": True, "headless_service": True, "pvc": False, "storageclass": True, "admin_secretproviderclass": True, "certificate_secretproviderclass": True, "ingress": False, "admin_secret": True}}}
        ok, why = phase5_runtime._post_delete_positively_absent(result, retained_pvc_expected=False)
        self.assertFalse(ok)
        self.assertIn("application_found", why)

    def test_141_all_absent_no_pvc_passes(self):
        result = {"state": "ABSENT", "checks": {"application_found": False, "footprint_found": {"statefulset": False, "service": False, "headless_service": False, "pvc": False, "storageclass": False, "admin_secretproviderclass": False, "certificate_secretproviderclass": False, "ingress": False, "admin_secret": False}}}
        ok, why = phase5_runtime._post_delete_positively_absent(result, retained_pvc_expected=False)
        self.assertTrue(ok, why)

    def test_142_statefulset_still_present_fails(self):
        result = _classifier_result("BROKEN", False, statefulset=True)
        ok, why = phase5_runtime._post_delete_positively_absent(result, retained_pvc_expected=False)
        self.assertFalse(ok)
        self.assertIn("statefulset", why)

    def test_143_service_still_present_fails(self):
        result = _classifier_result("BROKEN", False, service=True)
        ok, why = phase5_runtime._post_delete_positively_absent(result, retained_pvc_expected=False)
        self.assertFalse(ok)
        self.assertIn("service", why)

    def test_144_storageclass_still_present_fails(self):
        result = _classifier_result("BROKEN", False, storageclass=True)
        ok, why = phase5_runtime._post_delete_positively_absent(result, retained_pvc_expected=False)
        self.assertFalse(ok)
        self.assertIn("storageclass", why)

    def test_145_secretproviderclass_still_present_fails(self):
        result = _classifier_result("BROKEN", False, admin_secretproviderclass=True)
        ok, why = phase5_runtime._post_delete_positively_absent(result, retained_pvc_expected=False)
        self.assertFalse(ok)
        self.assertIn("admin_secretproviderclass", why)

    def test_146_admin_synced_secret_still_present_fails(self):
        result = _classifier_result("BROKEN", False, admin_secret=True)
        ok, why = phase5_runtime._post_delete_positively_absent(result, retained_pvc_expected=False)
        self.assertFalse(ok)
        self.assertIn("admin_secret", why)

    def test_147_inspection_errors_never_count_as_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            phase5_runtime.update_state(state_path, _removal_state_fixture(), phase5_runtime.REMOVAL_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
            scripted.when(_starts_with(sys.executable, str(phase5_runtime.RUNTIME_STATE_TOOL)), FakeProc(1, "", "inspection error"))
            with mock.patch.object(phase5_runtime, "run", scripted), mock.patch.object(phase5_runtime.time, "sleep"), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error):
                    _run_quiet(phase5_runtime.cmd_post_delete_acceptance, args)

    def test_148_post_delete_bound_remains_180s_15s(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            phase5_runtime.update_state(state_path, _removal_state_fixture(), phase5_runtime.REMOVAL_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
            scripted.when(_starts_with(sys.executable, str(phase5_runtime.RUNTIME_STATE_TOOL)), FakeProc(0, json.dumps(_classifier_result("OWNED", True, statefulset=True))))
            with mock.patch.object(phase5_runtime, "run", scripted), mock.patch.object(phase5_runtime.time, "sleep") as sleep_mock, _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error) as ctx:
                    _run_quiet(phase5_runtime.cmd_post_delete_acceptance, args)
            self.assertIn("180s", str(ctx.exception))
            total_slept = sum(c.args[0] for c in sleep_mock.call_args_list)
            self.assertGreaterEqual(total_slept, 180)
            for c in sleep_mock.call_args_list:
                self.assertEqual(c.args[0], 15)

    def test_149_final_boundary_probe_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            phase5_runtime.update_state(state_path, _removal_state_fixture(), phase5_runtime.REMOVAL_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
            scripted.when(_starts_with(sys.executable, str(phase5_runtime.RUNTIME_STATE_TOOL)), FakeProc(0, json.dumps(_classifier_result("OWNED", True, statefulset=True))))
            with mock.patch.object(phase5_runtime, "run", scripted), mock.patch.object(phase5_runtime.time, "sleep"), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error):
                    _run_quiet(phase5_runtime.cmd_post_delete_acceptance, args)
            classifier_calls = [c for c in scripted.calls if str(phase5_runtime.RUNTIME_STATE_TOOL) in c["argv"]]
            self.assertGreaterEqual(len(classifier_calls), 2, "at least an initial and a final-boundary probe")


# ==== RETAINED PVC TESTS ====

class RetainedPvcOrchestrationTests(unittest.TestCase):
    def test_150_deployment_disabled_retained_pvc_safe(self):
        result = _classifier_result("OWNED", False, pvc=True)
        ok, why = phase5_runtime._post_delete_positively_absent(result, retained_pvc_expected=phase5_runtime._retained_pvc_expected_for_removal("existing"))
        self.assertTrue(ok, why)

    def test_151_physical_removal_existing_retained_pvc_safe(self):
        self.assertTrue(phase5_runtime._retained_pvc_expected_for_removal("existing"))
        result = _classifier_result("OWNED", False, pvc=True)
        ok, why = phase5_runtime._post_delete_positively_absent(result, retained_pvc_expected=True)
        self.assertTrue(ok, why)

    def test_152_physical_removal_foreign_mislabeled_pvc_fails(self):
        # The classifier itself (runtime_state.py) already returns BROKEN for a foreign/mislabeled PVC -- the orchestration layer must not override that.
        result = _classifier_result("BROKEN", False, pvc=True)
        ok, why = phase5_runtime._post_delete_positively_absent(result, retained_pvc_expected=True)
        self.assertFalse(ok)
        self.assertIn("BROKEN", why)

    def test_153_physical_removal_efs_mode_empty_with_pvc_fails(self):
        self.assertFalse(phase5_runtime._retained_pvc_expected_for_removal(""))
        result = _classifier_result("OWNED", False, pvc=True)
        ok, why = phase5_runtime._post_delete_positively_absent(result, retained_pvc_expected=phase5_runtime._retained_pvc_expected_for_removal(""))
        self.assertFalse(ok)
        self.assertIn("retained PVC is expected", why)

    def test_154_physical_removal_managed_fails_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, deployment_model="singleRuntime", efs_mode="managed", reason="physical-removal", state_path=state_path)
            with _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error):
                    _run_quiet(phase5_runtime.cmd_prepare_removal, args)

    def test_155_default_runtime_state_behavior_without_hint_unchanged(self):
        """Defense-in-depth cross-check: the classifier's own default parameter value is unchanged (False) -- see also automation/phases/phase5/tests/test_runtime_state.py's dedicated coverage of this."""
        spec = importlib.util.spec_from_file_location("runtime_state", REPO_ROOT / "automation" / "phases" / "phase5" / "runtime_state.py")
        runtime_state = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime_state)
        self.assertEqual(runtime_state.classify.__defaults__[-1], False)


# ==== STRICT ACCEPTANCE TESTS ====

class StrictAcceptanceTests(TempStateCase):
    def test_156_active_runtime_healthy_accepted(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
        scripted.when(_starts_with(sys.executable, str(phase5_runtime.DEPLOYMENT_MODEL_TOOL)), FakeProc(0, json.dumps(_descriptor())))
        scripted.when(_starts_with(sys.executable, str(phase5_runtime.RUNTIME_ACCEPTANCE_TOOL)), FakeProc(0, json.dumps({"state": "HEALTHY", "checks": {}})))
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            _run_quiet(phase5_runtime.cmd_strict_acceptance, self.args)

    def test_157_broken_rejected(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
        scripted.when(_starts_with(sys.executable, str(phase5_runtime.DEPLOYMENT_MODEL_TOOL)), FakeProc(0, json.dumps(_descriptor())))
        scripted.when(_starts_with(sys.executable, str(phase5_runtime.RUNTIME_ACCEPTANCE_TOOL)), FakeProc(0, json.dumps({"state": "BROKEN", "checks": {}})))
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_strict_acceptance, self.args)

    def test_158_classifier_inspection_error_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
        scripted.when(_starts_with(sys.executable, str(phase5_runtime.DEPLOYMENT_MODEL_TOOL)), FakeProc(0, json.dumps(_descriptor())))
        scripted.when(_starts_with(sys.executable, str(phase5_runtime.RUNTIME_ACCEPTANCE_TOOL)), FakeProc(1, "", "inspection error"))
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_strict_acceptance, self.args)

    def test_159_expected_managed_efs_id_passed_to_runtime_acceptance(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
        scripted.when(_starts_with(sys.executable, str(phase5_runtime.DEPLOYMENT_MODEL_TOOL)), FakeProc(0, json.dumps(_descriptor(efsMode="managed", efsCreationToken="gg-tok"))))
        scripted.when(_starts_with("aws", "sts", "assume-role"), FakeProc(0, json.dumps({"Credentials": {"AccessKeyId": "AKIA", "SecretAccessKey": "s", "SessionToken": "t"}})))
        scripted.when(_contains("sts", "get-caller-identity"), FakeProc(0, WORKLOAD_ACCOUNT_ID))
        scripted.when(_starts_with("aws", "efs", "describe-file-systems"), FakeProc(0, json.dumps({"FileSystems": [{"FileSystemId": "fs-acceptance1", "LifeCycleState": "available", "Tags": [
            {"Key": "ManagedBy", "Value": "goldengate-eks-app"}, {"Key": "GoldenGateDeploymentId", "Value": DEPLOYMENT_ID},
            {"Key": "GoldenGateEnvironment", "Value": ENVIRONMENT}, {"Key": "GoldenGateStorage", "Value": "u02"},
        ]}]})))
        scripted.when(_starts_with(sys.executable, str(phase5_runtime.RUNTIME_ACCEPTANCE_TOOL)), FakeProc(0, json.dumps({"state": "HEALTHY", "checks": {}})))
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            _run_quiet(phase5_runtime.cmd_strict_acceptance, self.args)
        acceptance_call = next(c["argv"] for c in scripted.calls if str(phase5_runtime.RUNTIME_ACCEPTANCE_TOOL) in c["argv"])
        self.assertIn("--expected-efs-file-system-id", acceptance_call)
        self.assertIn("fs-acceptance1", acceptance_call)

    def test_160_no_active_runtime_acceptance_mutation_commands_exist(self):
        """Structural proof: no mutating kubectl/helm verb ever appears as a literal argv token inside cmd_strict_acceptance()."""
        import ast
        tree = ast.parse(TOOL_PATH.read_text())
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "cmd_strict_acceptance")
        string_literals = {n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        forbidden_tokens = {"apply", "create", "patch", "delete", "annotate", "label", "push", "install", "uninstall"}
        self.assertEqual(forbidden_tokens.intersection(string_literals), set())


# ==== INTEGRATION TESTS (full command flow, mocked subprocesses) ====

class ResolveLiveInputsIntegrationTests(TempStateCase):
    def test_full_resolve_live_inputs_flow(self):
        phase5_runtime.update_state(self.state_path, _reconcile_state_fixture(deploy=False), phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
        scripted = ScriptedRun()
        scripted.when(_starts_with(sys.executable, str(phase5_runtime.DEPLOYMENT_MODEL_TOOL)), FakeProc(0, json.dumps(_descriptor())))
        scripted.when(_starts_with("aws", "ecr", "describe-images"), FakeProc(0, json.dumps({"imageDetails": [{"imageDigest": "sha256:deadbeef"}]})))
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            _run_quiet(phase5_runtime.cmd_resolve_live_inputs, self.args)
        state = phase5_runtime.load_state(self.state_path)
        self.assertEqual(state["admin_secret_name"], "dev/goldengate/source/admin")
        self.assertEqual(state["image_digest"], "sha256:deadbeef")
        self.assertEqual(state["resolved_efs_id"], "")

    def test_resolve_live_inputs_missing_identity_fails(self):
        phase5_runtime.update_state(self.state_path, _reconcile_state_fixture(deploy=False), phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
        scripted = ScriptedRun()
        scripted.when(_starts_with(sys.executable, str(phase5_runtime.DEPLOYMENT_MODEL_TOOL)), FakeProc(0, json.dumps(_descriptor(adminSecretName=None))))
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_resolve_live_inputs, self.args)


class ValidateLocalIntegrationTests(unittest.TestCase):
    def test_full_validate_local_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            chart_dir = repo_root / "helm" / "goldengate"
            chart_dir.mkdir(parents=True)
            (chart_dir / "Chart.yaml").write_text("apiVersion: v2\nname: goldengate\n")
            (chart_dir / "values.yaml").write_text("runtime:\n  containerName: goldengate\n")

            values_rel = f"envs/{ENVIRONMENT}/{DEPLOYMENT_ID}/values.yaml"
            values_path = repo_root / values_rel
            values_path.parent.mkdir(parents=True)
            values_path.write_text("runtime:\n  containerName: goldengate\n  name: " + DEPLOYMENT_ID + "\ningress:\n  enabled: false\n")

            state_path = repo_root / "state.json"
            phase5_runtime.update_state(state_path, {
                **_reconcile_state_fixture(), "target_namespace": TARGET_NAMESPACE,
                "admin_secret_name": "dev/goldengate/source/admin", "tls_secret_name": "dev/goldengate/tls-certificate",
                "runtime_service_account_name": "gg-runtime-sa", "image_repository": IMAGE_REPOSITORY,
                "image_repository_name": "aws-cloud-factory-goldengate-oracle", "image_tag": IMAGE_TAG,
                "image_digest": "sha256:" + "ab" * 32, "dns_domain": "goldengate-dev.adcbmis.local",
                "alb_group_name": "goldengate-dev-shared", "certificate_arn": f"arn:aws:acm:eu-west-1:{WORKLOAD_ACCOUNT_ID}:certificate/abc-123",
                "resolved_efs_id": "", "efs_mode": "", "efs_file_system_id_declared": "", "efs_creation_token": "",
            }, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)

            import yaml as _yaml
            rendered_yaml = "\n---\n".join(_yaml.safe_dump(d) for d in (
                _statefulset(), _service(DEPLOYMENT_ID), _service(f"{DEPLOYMENT_ID}-headless", headless=True), _admin_spc("dev/goldengate/source/admin"),
            ))

            package_dir = repo_root / "packaged"

            scripted = ScriptedRun()
            scripted.when(_starts_with(sys.executable, str(phase5_runtime.DEPLOYMENT_MODEL_TOOL)), FakeProc(0, json.dumps(_descriptor())))
            scripted.when(_starts_with("helm", "dependency", "build"), FakeProc(0, ""))
            scripted.when(_starts_with("helm", "lint"), FakeProc(0, ""))
            scripted.when(_starts_with("helm", "template"), FakeProc(0, rendered_yaml))

            def fake_helm_package(argv, **kwargs):
                # helm package must actually create the archive on disk for _package_runtime_chart's own existence check.
                package_dir.mkdir(parents=True, exist_ok=True)
                (package_dir / f"{phase5_runtime.CHART_NAME}-{CHART_VERSION}.tgz").write_bytes(b"fake")
                return FakeProc(0, "")

            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)

            with mock.patch.object(phase5_runtime, "REPO_ROOT", repo_root), \
                 mock.patch.object(phase5_runtime, "HELM_CHART_PATH", chart_dir), \
                 mock.patch.object(phase5_runtime, "run") as run_mock, _env_patch():
                def route(argv, **kwargs):
                    if argv[:2] == ["helm", "package"]:
                        return fake_helm_package(argv, **kwargs)
                    return scripted(argv, **kwargs)
                run_mock.side_effect = route
                _run_quiet(phase5_runtime.cmd_validate_local, args)

            state = phase5_runtime.load_state(state_path)
            self.assertIn("rendered_manifest", state)
            self.assertIn("package_path", state)


class PostDeleteSemanticShapeRegressionTests(unittest.TestCase):
    """Confirmed reproductions of the OWNED-with-zero-footprint and ABSENT-with-retained-PVC false-success gaps: the structural schema validator alone accepted these shapes, so _post_delete_positively_absent() must apply a second, state-specific semantic layer on top of it."""

    def test_161_owned_all_footprint_false_without_hint_fails(self):
        """Case A: OWNED + application_found=false + every footprint key false + retained_pvc_expected=false previously returned (True, None) -- an OWNED state with literally nothing found is not one of the two canonical deletion-complete shapes."""
        ok, why = phase5_runtime._post_delete_positively_absent(_classifier_result("OWNED", False), retained_pvc_expected=False)
        self.assertFalse(ok, why)

    def test_162_owned_all_footprint_false_with_hint_fails(self):
        """Case B: identical shape to Case A but with retained_pvc_expected=true -- still fails because footprint["pvc"] itself is false, so there is no retained PVC to justify accepting an OWNED state as deletion-complete."""
        ok, why = phase5_runtime._post_delete_positively_absent(_classifier_result("OWNED", False), retained_pvc_expected=True)
        self.assertFalse(ok, why)

    def test_163_absent_with_retained_pvc_hint_and_pvc_present_fails(self):
        """Case C: ABSENT + pvc=true + retained_pvc_expected=true previously returned (True, None) -- the ABSENT shape requires EVERY footprint key false, including pvc; a retained PVC is only ever acceptable under the distinct OWNED shape."""
        ok, why = phase5_runtime._post_delete_positively_absent(_classifier_result("ABSENT", False, pvc=True), retained_pvc_expected=True)
        self.assertFalse(ok, why)

    def test_164_absent_with_any_footprint_key_true_fails(self):
        for key in RUNTIME_FOOTPRINT_KEYS:
            ok, why = phase5_runtime._post_delete_positively_absent(_classifier_result("ABSENT", False, **{key: True}), retained_pvc_expected=True)
            self.assertFalse(ok, f"ABSENT with footprint key {key!r} true unexpectedly allowed completion")

    def test_165_application_found_true_never_completes_regardless_of_state(self):
        for state in ("ABSENT", "OWNED", "BROKEN"):
            ok, why = phase5_runtime._post_delete_positively_absent({"state": state, "checks": {"application_found": True, "footprint_found": _complete_footprint()}}, retained_pvc_expected=True)
            self.assertFalse(ok, f"state={state} with application_found=true unexpectedly allowed completion")

    def test_166_complete_absent_still_passes_after_fix(self):
        """Confirms the legitimate SHAPE 1 (complete absence) still succeeds -- this fix must not regress the already-correct case."""
        ok, why = phase5_runtime._post_delete_positively_absent(_classifier_result("ABSENT", False), retained_pvc_expected=False)
        self.assertTrue(ok, why)

    def test_167_complete_retained_pvc_owned_still_passes_after_fix(self):
        """Confirms the legitimate SHAPE 2 (retained-PVC-only OWNED, explicit hint) still succeeds -- this fix must not regress the already-correct case."""
        ok, why = phase5_runtime._post_delete_positively_absent(_classifier_result("OWNED", False, pvc=True), retained_pvc_expected=True)
        self.assertTrue(ok, why)


class RemovalMutationStateTests(TempStateCase):
    """Issue 2: cmd_remove_runtime() must never truthiness-coerce a persisted state value (bool("false") is True in Python) into removal-mutation authorization, and must validate the ENTIRE removal state before any cluster connection or mutating kubectl call."""

    def _persist(self, **fields):
        base = {
            **_removal_state_fixture(),
            "ownership_state": "OWNED", "application_found": True, "footprint_found": _complete_footprint(),
        }
        base.update(fields)
        phase5_runtime.update_state(self.state_path, base, phase5_runtime.REMOVAL_ALLOWED_STATE_KEYS)

    def _assert_fails_before_any_mutation(self):
        scripted = ScriptedRun()
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_remove_runtime, self.args)
        self.assertEqual(scripted.calls, [], "a malformed removal state must result in ZERO subprocess calls (no _connect_to_eks, no kubectl patch/delete)")

    def test_11_application_found_string_false_reproduces_confirmed_mutation_bug(self):
        """Confirmed reproduction of the current bug before the fix: bool("false") == True in Python, so a string "false" application_found value previously authorized kubectl patch + kubectl delete against the cluster."""
        self._persist(application_found="false")
        self._assert_fails_before_any_mutation()

    def test_12_application_found_zero_fails_before_mutation(self):
        self._persist(application_found=0)
        self._assert_fails_before_any_mutation()

    def test_13_application_found_null_fails_before_mutation(self):
        self._persist(application_found=None)
        self._assert_fails_before_any_mutation()

    def test_14_application_found_missing_fails_before_mutation(self):
        state = {**_removal_state_fixture(), "ownership_state": "OWNED", "footprint_found": _complete_footprint()}
        phase5_runtime.update_state(self.state_path, state, phase5_runtime.REMOVAL_ALLOWED_STATE_KEYS)
        self._assert_fails_before_any_mutation()

    def test_15_footprint_found_missing_fails_before_mutation(self):
        state = {**_removal_state_fixture(), "ownership_state": "OWNED", "application_found": True}
        phase5_runtime.update_state(self.state_path, state, phase5_runtime.REMOVAL_ALLOWED_STATE_KEYS)
        self._assert_fails_before_any_mutation()

    def test_16_footprint_found_partial_fails_before_mutation(self):
        self._persist(footprint_found={"statefulset": False})
        self._assert_fails_before_any_mutation()

    def test_17_footprint_value_string_false_fails_before_mutation(self):
        self._persist(footprint_found=_complete_footprint(statefulset="false"))
        self._assert_fails_before_any_mutation()

    def test_18_ownership_state_unknown_fails_before_mutation(self):
        self._persist(ownership_state="SOMETHING_ELSE")
        self._assert_fails_before_any_mutation()

    def test_19_absent_with_application_found_true_fails_before_mutation(self):
        """Internally inconsistent shape: ABSENT means the classifier itself found no Application, so application_found=true for the same result can never be legitimate -- must fail closed rather than mutate anything."""
        self._persist(ownership_state="ABSENT", application_found=True)
        self._assert_fails_before_any_mutation()

    def test_20_valid_absent_application_false_performs_no_mutation(self):
        self._persist(ownership_state="ABSENT", application_found=False)
        scripted = ScriptedRun()
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            _run_quiet(phase5_runtime.cmd_remove_runtime, self.args)
        self.assertEqual(scripted.calls, [])

    def test_21_valid_owned_application_false_performs_no_application_mutation(self):
        self._persist(ownership_state="OWNED", application_found=False)
        scripted = ScriptedRun()
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            _run_quiet(phase5_runtime.cmd_remove_runtime, self.args)
        self.assertEqual(scripted.calls, [])

    def test_22_valid_owned_application_true_still_executes_exact_patch_delete_contract(self):
        self._persist(ownership_state="OWNED", application_found=True)
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
        scripted.when(_starts_with("kubectl", "patch", "application"), FakeProc(0, ""))
        scripted.when(_starts_with("kubectl", "delete", "application"), FakeProc(0, ""))
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            _run_quiet(phase5_runtime.cmd_remove_runtime, self.args)
        patch_calls = [c for c in scripted.calls if c["argv"][:2] == ["kubectl", "patch"]]
        delete_calls = [c for c in scripted.calls if c["argv"][:2] == ["kubectl", "delete"]]
        self.assertEqual(len(patch_calls), 1)
        self.assertEqual(len(delete_calls), 1)
        self.assertIn("--wait=true", delete_calls[0]["argv"])
        self.assertIn("--timeout=10m", delete_calls[0]["argv"])


class LiteralDeployBooleanTests(unittest.TestCase):
    """Issue 3: cmd_resolve_live_inputs() no longer contains `deploy = bool(state.get("deploy"))` -- bool("false") == True in Python. _validate_reconcile_state_identity() now requires state["deploy"] to already be a literal JSON boolean."""

    def _validate(self, deploy_value):
        state = _reconcile_state_fixture(deploy=deploy_value)
        with _env_patch():
            return phase5_runtime._validate_reconcile_state_identity(state, ENVIRONMENT, DEPLOYMENT_ID)

    def test_1_deploy_false_accepted_as_literal_false(self):
        self.assertIs(self._validate(False), False)

    def test_2_deploy_true_accepted_as_literal_true(self):
        self.assertIs(self._validate(True), True)

    def test_3_deploy_string_false_rejected(self):
        """Confirmed reproduction of the historical bug: bool("false") == True in Python -- the OLD cmd_resolve_live_inputs code would have silently treated this Validate-mode state as deploy=True."""
        with self.assertRaises(phase5_runtime.Phase5Error):
            self._validate("false")

    def test_4_deploy_string_true_rejected(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            self._validate("true")

    def test_5_deploy_zero_rejected(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            self._validate(0)

    def test_6_deploy_one_rejected(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            self._validate(1)

    def test_7_deploy_none_rejected(self):
        with self.assertRaises(phase5_runtime.Phase5Error):
            self._validate(None)

    def test_8_deploy_missing_rejected(self):
        state = _reconcile_state_fixture()
        del state["deploy"]
        with self.assertRaises(phase5_runtime.Phase5Error):
            with _env_patch():
                phase5_runtime._validate_reconcile_state_identity(state, ENVIRONMENT, DEPLOYMENT_ID)

    def test_9_managed_efs_deploy_false_uses_synthetic_placeholder_zero_live_calls(self):
        """Integration-level proof via cmd_resolve_live_inputs: deploy=false + managed EFS mode must reach the dry-run placeholder with ZERO sts:AssumeRole/efs:DescribeFileSystems calls."""
        state = _reconcile_state_fixture(deploy=False)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            phase5_runtime.update_state(state_path, state, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            scripted.when(_starts_with(sys.executable, str(phase5_runtime.DEPLOYMENT_MODEL_TOOL)),
                          FakeProc(0, json.dumps(_descriptor(efsMode="managed", efsFileSystemId=None, efsCreationToken="gg-dev-oracle-01-u02"))))
            scripted.when(_starts_with("aws", "ecr", "describe-images"), FakeProc(0, json.dumps({"imageDetails": [{"imageDigest": "sha256:deadbeef"}]})))
            with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                _run_quiet(phase5_runtime.cmd_resolve_live_inputs, args)
            result_state = phase5_runtime.load_state(state_path)
            self.assertEqual(result_state["resolved_efs_id"], phase5_runtime.EFS_DRY_RUN_PLACEHOLDER)
            self.assertEqual([c for c in scripted.calls if c["argv"][:3] == ["aws", "sts", "assume-role"]], [])
            self.assertEqual([c for c in scripted.calls if c["argv"][:3] == ["aws", "efs", "describe-file-systems"]], [])


class CrossRuntimeReconcileStateTests(unittest.TestCase):
    """State identity/target binding: a reconcile-state JSON document whose mutation-target fields do not match the CURRENT matrix environment/deployment_id (or the canonical Phase 5 naming/config rules) must never be trusted by resolve-live-inputs/publish-chart/reconcile-runtime -- each must fail BEFORE any AWS/Kubernetes call."""

    def _resolve_live_inputs_fails_zero_calls(self, **state_overrides):
        state = _reconcile_state_fixture(**state_overrides)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            phase5_runtime.update_state(state_path, state, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error):
                    _run_quiet(phase5_runtime.cmd_resolve_live_inputs, args)
            self.assertEqual(scripted.calls, [], "a cross-runtime/malformed reconcile state must result in ZERO calls (no deployment-model describe, no ECR describe-images)")

    def test_10_environment_mismatch_fails_before_mutation(self):
        self._resolve_live_inputs_fails_zero_calls(environment="staging")

    def test_11_deployment_id_mismatch_fails_before_mutation(self):
        self._resolve_live_inputs_fails_zero_calls(deployment_id="gg-mssql-repltest-01")

    def test_12_wrong_release_name_fails(self):
        self._resolve_live_inputs_fails_zero_calls(release_name="totally-unrelated-release")

    def _reconcile_runtime_fails_zero_kubernetes_calls(self, **state_overrides):
        state = {
            **_reconcile_state_fixture(**state_overrides), "chart_version": "0.1.1-gg-x",
            "image_repository": IMAGE_REPOSITORY, "dns_domain": "goldengate-dev.adcbmis.local",
            "alb_group_name": "goldengate-dev-shared", "certificate_arn": "arn:aws:acm:eu-west-1:668311715351:certificate/abc",
            "admin_secret_name": "dev/goldengate/source/admin", "tls_secret_name": "dev/goldengate/tls-certificate",
            "runtime_service_account_name": "gg-runtime-sa", "resolved_efs_id": "",
        }
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            phase5_runtime.update_state(state_path, state, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error) as ctx:
                    _run_quiet(phase5_runtime.cmd_reconcile_runtime, args)
            self.assertEqual(scripted.calls, [], "a cross-runtime/malformed reconcile state must result in ZERO Kubernetes calls (no _connect_to_eks, no kubectl apply/annotate)")
            return str(ctx.exception)

    def test_13_wrong_argocd_app_name_fails_before_connect_to_eks(self):
        """Confirmed reproduction: state.argocd_app_name="totally-unrelated-app" from the confirmed current bug must now fail closed before any Kubernetes call."""
        message = self._reconcile_runtime_fails_zero_kubernetes_calls(argocd_app_name="totally-unrelated-app")
        self.assertIn("totally-unrelated-app", message)

    def test_14_wrong_target_namespace_fails_before_connect_to_eks(self):
        """Confirmed reproduction: state.target_namespace="some-other-runtime-ns" from the confirmed current bug must now fail closed before any Kubernetes call."""
        message = self._reconcile_runtime_fails_zero_kubernetes_calls(target_namespace="some-other-runtime-ns")
        self.assertIn("some-other-runtime-ns", message)

    def test_15_wrong_helm_chart_ref_fails(self):
        self._reconcile_runtime_fails_zero_kubernetes_calls(helm_chart_ref="oci://evil-registry.example.com/helm/goldengate")

    def _publish_chart_fails_zero_aws_calls(self, **state_overrides):
        state = {**_reconcile_state_fixture(**state_overrides), "chart_version": "0.1.1-gg-x", "package_path": "packaged/goldengate-0.1.1-gg-x.tgz"}
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            phase5_runtime.update_state(state_path, state, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            with _mock_repo_root_with_real_chart(Path(tmp)), mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error):
                    _run_quiet(phase5_runtime.cmd_publish_chart, args)
            self.assertEqual(scripted.calls, [], "a cross-runtime/malformed reconcile state must result in ZERO AWS/ECR calls")

    def test_16_wrong_helm_push_url_fails_before_ecr_publish(self):
        self._publish_chart_fails_zero_aws_calls(helm_push_url="oci://evil-registry.example.com/helm")

    def test_17_wrong_helm_ecr_repository_fails(self):
        self._publish_chart_fails_zero_aws_calls(helm_ecr_repository="helm/some-other-chart")

    def test_18_wrong_values_file_fails(self):
        self._resolve_live_inputs_fails_zero_calls(values_file="envs/dev/some-other-deployment/values.yaml")

    def test_19_deploy_false_direct_publish_chart_invocation_fails_before_any_aws_call(self):
        self._publish_chart_fails_zero_aws_calls(deploy=False)

    def test_20_deploy_string_false_direct_publish_chart_invocation_fails_before_any_aws_call(self):
        self._publish_chart_fails_zero_aws_calls(deploy="false")

    def test_21_deploy_false_direct_reconcile_runtime_invocation_fails_before_any_kubernetes_call(self):
        self._reconcile_runtime_fails_zero_kubernetes_calls(deploy=False)

    def test_22_valid_canonical_reconcile_state_still_performs_exact_application_reconciliation_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            _full_reconcile_state(state_path)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = _reconcile_scripted_ok()
            with mock.patch.object(phase5_runtime, "run", scripted), _env_patch(ENABLE_TEMP_ARGOCD_ECR_PASSWORD_INJECTION="false"):
                _run_quiet(phase5_runtime.cmd_reconcile_runtime, args)
            apply_calls = [c for c in scripted.calls if c["argv"][:3] == ["kubectl", "apply", "-f"]]
            annotate_calls = [c for c in scripted.calls if c["argv"][:2] == ["kubectl", "annotate"]]
            self.assertEqual(len(apply_calls), 1)
            self.assertEqual(len(annotate_calls), 1)
            import yaml as _yaml
            manifest = _yaml.safe_load(apply_calls[0]["input_text"])
            self.assertEqual(manifest["metadata"]["name"], _canonical_argocd_app_name())


class CrossRuntimeRemovalStateTests(unittest.TestCase):
    """State identity/target binding: a removal-state JSON document whose mutation-target fields do not match the CURRENT matrix environment/deployment_id (or the canonical Phase 5 naming/config rules) must never be trusted by removal-preflight/remove-runtime/post-delete-acceptance -- each must fail BEFORE any cluster connection or mutating call."""

    def _removal_preflight_fails_zero_calls(self, **state_overrides):
        state = _removal_state_fixture(**state_overrides)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            phase5_runtime.update_state(state_path, state, phase5_runtime.REMOVAL_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error):
                    _run_quiet(phase5_runtime.cmd_removal_preflight, args)
            self.assertEqual(scripted.calls, [], "a cross-runtime/malformed removal state must result in ZERO calls (no _connect_to_eks, no ownership classifier invocation)")

    def test_23_environment_mismatch_fails_before_cluster_connection(self):
        self._removal_preflight_fails_zero_calls(environment="staging")

    def test_24_deployment_id_mismatch_fails(self):
        self._removal_preflight_fails_zero_calls(deployment_id="gg-mssql-repltest-01")

    def _remove_runtime_fails_zero_calls(self, **state_overrides):
        state = {**_removal_state_fixture(**state_overrides), "ownership_state": "OWNED", "application_found": True, "footprint_found": _complete_footprint()}
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            phase5_runtime.update_state(state_path, state, phase5_runtime.REMOVAL_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error) as ctx:
                    _run_quiet(phase5_runtime.cmd_remove_runtime, args)
            self.assertEqual(scripted.calls, [], "a cross-runtime/malformed removal state must result in ZERO kubectl patch/delete calls")
            return str(ctx.exception)

    def test_25_wrong_argocd_app_name_fails_zero_patch_delete(self):
        """Confirmed reproduction: state.argocd_app_name="totally-unrelated-app" from the confirmed current bug must now fail closed before any kubectl patch/delete."""
        message = self._remove_runtime_fails_zero_calls(argocd_app_name="totally-unrelated-app")
        self.assertIn("totally-unrelated-app", message)

    def test_26_wrong_argocd_namespace_fails_zero_kubernetes_calls(self):
        """Confirmed reproduction: state.argocd_namespace="other-namespace" from the confirmed current bug must now fail closed before any Kubernetes call."""
        message = self._remove_runtime_fails_zero_calls(argocd_namespace="other-namespace")
        self.assertIn("other-namespace", message)

    def test_27_wrong_runtime_namespace_fails(self):
        self._removal_preflight_fails_zero_calls(runtime_namespace="some-other-ns")

    def test_28_wrong_deployment_model_fails(self):
        self._removal_preflight_fails_zero_calls(deployment_model="legacyPair")

    def test_29_invalid_reason_fails(self):
        self._removal_preflight_fails_zero_calls(reason="bogus-reason")

    def test_30_invalid_efs_mode_fails(self):
        self._removal_preflight_fails_zero_calls(efs_mode="bogus")

    def test_31_physical_removal_plus_managed_fails_before_cluster_connection(self):
        self._removal_preflight_fails_zero_calls(reason="physical-removal", efs_mode="managed")

    def test_32_valid_canonical_owned_application_true_exact_patch_delete_still_occurs(self):
        state = {**_removal_state_fixture(), "ownership_state": "OWNED", "application_found": True, "footprint_found": _complete_footprint()}
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            phase5_runtime.update_state(state_path, state, phase5_runtime.REMOVAL_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
            scripted.when(_starts_with("kubectl", "patch", "application"), FakeProc(0, ""))
            scripted.when(_starts_with("kubectl", "delete", "application"), FakeProc(0, ""))
            with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                _run_quiet(phase5_runtime.cmd_remove_runtime, args)
            patch_calls = [c for c in scripted.calls if c["argv"][:2] == ["kubectl", "patch"]]
            delete_calls = [c for c in scripted.calls if c["argv"][:2] == ["kubectl", "delete"]]
            self.assertEqual(len(patch_calls), 1)
            self.assertEqual(len(delete_calls), 1)

    def test_33_valid_canonical_absent_application_false_no_application_mutation(self):
        state = {**_removal_state_fixture(), "ownership_state": "ABSENT", "application_found": False, "footprint_found": _complete_footprint()}
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            phase5_runtime.update_state(state_path, state, phase5_runtime.REMOVAL_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                _run_quiet(phase5_runtime.cmd_remove_runtime, args)
            self.assertEqual(scripted.calls, [])

    def test_34_valid_canonical_retained_pvc_removal_state_post_delete_behavior_unchanged(self):
        state = _removal_state_fixture(efs_mode="existing")
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            phase5_runtime.update_state(state_path, state, phase5_runtime.REMOVAL_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
            scripted.when(_starts_with(sys.executable, str(phase5_runtime.RUNTIME_STATE_TOOL)), FakeProc(0, json.dumps(_classifier_result("OWNED", False, pvc=True))))
            with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                _run_quiet(phase5_runtime.cmd_post_delete_acceptance, args)
            classifier_call = next(c["argv"] for c in scripted.calls if str(phase5_runtime.RUNTIME_STATE_TOOL) in c["argv"])
            self.assertIn("--retained-pvc-expected", classifier_call)


class ChartVersionAndPackageBindingTests(unittest.TestCase):
    """The Python state file is a transport/cache, never a second mutable source of truth for the chart version or the local package it publishes -- see _canonical_chart_version()/_validate_package_path_and_containment()/_validate_packaged_chart_contents()."""

    def test_1_canonical_chart_version_helper(self):
        with _env_patch(GITHUB_RUN_NUMBER="777"):
            self.assertEqual(phase5_runtime._canonical_chart_version(DEPLOYMENT_ID), f"0.1.777-{DEPLOYMENT_ID}")

    def test_2_wrong_chart_version_fails_static_identity(self):
        state = _reconcile_state_fixture(chart_version="9.9.9-EVIL")
        with _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                phase5_runtime._validate_reconcile_state_identity(state, ENVIRONMENT, DEPLOYMENT_ID)

    def test_3_missing_chart_version_fails(self):
        state = _reconcile_state_fixture()
        del state["chart_version"]
        with _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                phase5_runtime._validate_reconcile_state_identity(state, ENVIRONMENT, DEPLOYMENT_ID)

    def test_4_wrong_package_path_fails_before_any_aws_call(self):
        """Confirmed reproduction of the current bug: a canonical reconcile identity with package_path=packaged/totally-unrelated-chart.tgz previously reached helm push/aws ecr calls."""
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            (Path(tmp) / "packaged").mkdir()
            (Path(tmp) / "packaged" / "totally-unrelated-chart.tgz").write_bytes(b"unrelated")
            phase5_runtime.update_state(state_path, {**_reconcile_state_fixture(), "package_path": "packaged/totally-unrelated-chart.tgz"}, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            with _mock_repo_root_with_real_chart(Path(tmp)), mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error) as ctx:
                    _run_quiet(phase5_runtime.cmd_publish_chart, args)
            self.assertEqual(scripted.calls, [])
            self.assertIn("totally-unrelated-chart.tgz", str(ctx.exception))

    def test_5_package_path_traversal_escape_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            phase5_runtime.update_state(state_path, {**_reconcile_state_fixture(), "package_path": f"packaged/../../etc/goldengate-{CHART_VERSION}.tgz"}, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            with _mock_repo_root_with_real_chart(Path(tmp)), mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error):
                    _run_quiet(phase5_runtime.cmd_publish_chart, args)
            self.assertEqual(scripted.calls, [])

    def test_6_absolute_package_path_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            phase5_runtime.update_state(state_path, {**_reconcile_state_fixture(), "package_path": "/etc/passwd"}, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            with _mock_repo_root_with_real_chart(Path(tmp)), mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error):
                    _run_quiet(phase5_runtime.cmd_publish_chart, args)
            self.assertEqual(scripted.calls, [])

    def test_7_symlink_escaping_packaged_dir_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside_dir = Path(tmp) / "outside"
            outside_dir.mkdir()
            evil_target = outside_dir / "evil.tgz"
            evil_target.write_bytes(b"evil")

            repo_root = Path(tmp) / "repo"
            (repo_root / "packaged").mkdir(parents=True)
            package_path_rel = f"packaged/goldengate-{CHART_VERSION}.tgz"
            try:
                (repo_root / package_path_rel).symlink_to(evil_target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not supported on this platform/filesystem")

            state_path = repo_root / "state.json"
            phase5_runtime.update_state(state_path, {**_reconcile_state_fixture(), "package_path": package_path_rel}, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            with _mock_repo_root_with_real_chart(repo_root), mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error):
                    _run_quiet(phase5_runtime.cmd_publish_chart, args)
            self.assertEqual(scripted.calls, [])

    def test_8_missing_expected_package_fails_before_aws(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            phase5_runtime.update_state(state_path, {**_reconcile_state_fixture(), "package_path": f"packaged/goldengate-{CHART_VERSION}.tgz"}, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            with _mock_repo_root_with_real_chart(Path(tmp)), mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error):
                    _run_quiet(phase5_runtime.cmd_publish_chart, args)
            self.assertEqual(scripted.calls, [])

    def _publish_with_package(self, **package_overrides):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            package_path_rel = _build_fake_chart_package(Path(tmp), CHART_VERSION, **package_overrides)
            phase5_runtime.update_state(state_path, {**_reconcile_state_fixture(), "package_path": package_path_rel}, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            scripted.when(_starts_with("aws", "ecr", "get-login-password"), FakeProc(0, "pw"))
            scripted.when(_starts_with("helm", "registry", "login"), FakeProc(0, ""))
            scripted.when(_starts_with("aws", "ecr", "describe-repositories"), FakeProc(0, ""))
            scripted.when(_starts_with("aws", "ecr", "get-repository-policy"), FakeProc(1, "", "RepositoryPolicyNotFoundException"))
            scripted.when(_starts_with("aws", "ecr", "set-repository-policy"), FakeProc(0, ""))
            scripted.when(_starts_with("helm", "push"), FakeProc(0, ""))
            scripted.when(_starts_with("helm", "pull"), FakeProc(0, ""))
            with _mock_repo_root_with_real_chart(Path(tmp)), mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                _run_quiet(phase5_runtime.cmd_publish_chart, args)
            return scripted

    def test_9_packaged_chart_yaml_wrong_name_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            package_path_rel = _build_fake_chart_package(Path(tmp), CHART_VERSION, chart_yaml_name="not-goldengate")
            phase5_runtime.update_state(state_path, {**_reconcile_state_fixture(), "package_path": package_path_rel}, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            with _mock_repo_root_with_real_chart(Path(tmp)), mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error):
                    _run_quiet(phase5_runtime.cmd_publish_chart, args)
            self.assertEqual(scripted.calls, [])

    def test_10_packaged_chart_yaml_wrong_version_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            package_path_rel = _build_fake_chart_package(Path(tmp), CHART_VERSION, chart_yaml_version="9.9.9-EVIL")
            phase5_runtime.update_state(state_path, {**_reconcile_state_fixture(), "package_path": package_path_rel}, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            with _mock_repo_root_with_real_chart(Path(tmp)), mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error):
                    _run_quiet(phase5_runtime.cmd_publish_chart, args)
            self.assertEqual(scripted.calls, [])

    def test_11_packaged_chart_yaml_wrong_app_version_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            package_path_rel = _build_fake_chart_package(Path(tmp), CHART_VERSION, chart_yaml_app_version="9.9.9-EVIL")
            phase5_runtime.update_state(state_path, {**_reconcile_state_fixture(), "package_path": package_path_rel}, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            with _mock_repo_root_with_real_chart(Path(tmp)), mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error):
                    _run_quiet(phase5_runtime.cmd_publish_chart, args)
            self.assertEqual(scripted.calls, [])

    def test_12_packaged_missing_values_deployment_yaml_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            package_path_rel = _build_fake_chart_package(Path(tmp), CHART_VERSION, include_values_deployment=False)
            phase5_runtime.update_state(state_path, {**_reconcile_state_fixture(), "package_path": package_path_rel}, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            with _mock_repo_root_with_real_chart(Path(tmp)), mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error):
                    _run_quiet(phase5_runtime.cmd_publish_chart, args)
            self.assertEqual(scripted.calls, [])

    def test_13_packaged_values_deployment_belongs_to_another_deployment_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            package_path_rel = _build_fake_chart_package(Path(tmp), CHART_VERSION, values_deployment_bytes=b"runtime:\n  containerName: totally-different\n")
            phase5_runtime.update_state(state_path, {**_reconcile_state_fixture(), "package_path": package_path_rel}, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            with _mock_repo_root_with_real_chart(Path(tmp)), mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error):
                    _run_quiet(phase5_runtime.cmd_publish_chart, args)
            self.assertEqual(scripted.calls, [])

    def test_14_canonical_package_containing_exact_current_values_passes(self):
        scripted = self._publish_with_package()
        push_calls = [c for c in scripted.calls if c["argv"][:2] == ["helm", "push"]]
        self.assertEqual(len(push_calls), 1)


class ResolvedDescriptorBindingTests(unittest.TestCase):
    """A source runtime must never accept a target admin secret (or vice versa); every resolve-live-inputs RESULT cached in state must be re-bound to a FRESH deployment-model descriptor before it can influence a mutation -- see _validate_resolved_runtime_inputs()."""

    def _assert_field_mismatch_fails(self, **override):
        state = _resolved_state_fixture(**override)
        with self.assertRaises(phase5_runtime.Phase5Error):
            _resolved_inputs(state)

    def test_15_wrong_admin_secret_name_fails(self):
        self._assert_field_mismatch_fails(admin_secret_name="dev/goldengate/other/admin")

    def test_16_source_runtime_with_target_admin_secret_fails_before_kubernetes_mutation(self):
        """Mandatory regression: the real canonical descriptor for a source runtime (e.g. gg-postgresql-repltest-01) declares adminSecretName=dev/goldengate/source/admin -- a state claiming dev/goldengate/target/admin must fail before any Kubernetes mutation."""
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = _resolved_state_fixture(admin_secret_name="dev/goldengate/target/admin")
            phase5_runtime.update_state(state_path, state, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            scripted.when(_starts_with(sys.executable, str(phase5_runtime.DEPLOYMENT_MODEL_TOOL)), FakeProc(0, json.dumps(_descriptor())))
            with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error) as ctx:
                    _run_quiet(phase5_runtime.cmd_reconcile_runtime, args)
            self.assertIn("target/admin", str(ctx.exception))
            self.assertEqual([c for c in scripted.calls if c["argv"][:1] == ["kubectl"]], [])

    def test_17_target_runtime_with_source_admin_secret_fails(self):
        target_descriptor = _descriptor(adminSecretName="dev/goldengate/target/admin")
        state = _resolved_state_fixture(admin_secret_name="dev/goldengate/source/admin")
        with self.assertRaises(phase5_runtime.Phase5Error):
            _resolved_inputs(state, descriptor=target_descriptor)

    def test_18_wrong_tls_secret_name_fails(self):
        self._assert_field_mismatch_fails(tls_secret_name="dev/goldengate/other-cert")

    def test_19_wrong_runtime_service_account_name_fails(self):
        self._assert_field_mismatch_fails(runtime_service_account_name="other-sa")

    def test_20_wrong_image_repository_fails(self):
        self._assert_field_mismatch_fails(image_repository=f"{ECR_REGISTRY}/some-other-repo")

    def test_21_wrong_image_repository_name_fails(self):
        self._assert_field_mismatch_fails(image_repository_name="some-other-repo")

    def test_22_wrong_image_tag_fails(self):
        self._assert_field_mismatch_fails(image_tag="99.99.99")

    def test_23_wrong_dns_domain_fails(self):
        self._assert_field_mismatch_fails(dns_domain="wrong.example.internal")

    def test_24_wrong_alb_group_name_fails(self):
        self._assert_field_mismatch_fails(alb_group_name="wrong-group")

    def test_25_wrong_certificate_arn_fails(self):
        self._assert_field_mismatch_fails(certificate_arn=f"arn:aws:acm:eu-west-1:{WORKLOAD_ACCOUNT_ID}:certificate/different")

    def test_26_wrong_efs_mode_fails(self):
        self._assert_field_mismatch_fails(efs_mode="managed")

    def test_27_wrong_declared_existing_efs_id_fails(self):
        descriptor = _descriptor(efsMode="existing", efsFileSystemId="fs-real12345")
        state = _resolved_state_fixture(efs_mode="existing", efs_file_system_id_declared="fs-WRONG00000", resolved_efs_id="fs-WRONG00000")
        with self.assertRaises(phase5_runtime.Phase5Error):
            _resolved_inputs(state, descriptor=descriptor)

    def test_28_wrong_efs_creation_token_fails(self):
        descriptor = _descriptor(efsMode="managed", efsCreationToken="gg-dev-oracle-01-u02")
        state = _resolved_state_fixture(deploy=False, efs_mode="managed", efs_creation_token="gg-dev-WRONG-TOKEN", resolved_efs_id=phase5_runtime.EFS_DRY_RUN_PLACEHOLDER)
        with self.assertRaises(phase5_runtime.Phase5Error):
            _resolved_inputs(state, descriptor=descriptor)

    def test_29_malformed_image_digest_fails(self):
        for bad_digest in ("", "sha256:short", "not-a-digest", "SHA256:" + "a" * 64):
            state = _resolved_state_fixture(image_digest=bad_digest)
            with self.assertRaises(phase5_runtime.Phase5Error, msg=bad_digest):
                _resolved_inputs(state)


class ResolvedEfsBindingTests(unittest.TestCase):
    """Deterministic per-mode resolved_efs_id contract (CASE 1-5) -- see _validate_resolved_runtime_inputs()."""

    _MANAGED_TAGS = [
        {"Key": "ManagedBy", "Value": "goldengate-eks-app"}, {"Key": "GoldenGateDeploymentId", "Value": DEPLOYMENT_ID},
        {"Key": "GoldenGateEnvironment", "Value": ENVIRONMENT}, {"Key": "GoldenGateStorage", "Value": "u02"},
    ]

    def test_30_no_efs_empty_resolved_id_passes(self):
        state = _resolved_state_fixture(efs_mode="", resolved_efs_id="")
        _resolved_inputs(state)

    def test_31_no_efs_nonempty_resolved_id_fails(self):
        state = _resolved_state_fixture(efs_mode="", resolved_efs_id="fs-shouldnotbehere")
        with self.assertRaises(phase5_runtime.Phase5Error):
            _resolved_inputs(state)

    def test_32_existing_efs_exact_descriptor_id_passes(self):
        descriptor = _descriptor(efsMode="existing", efsFileSystemId="fs-real12345")
        state = _resolved_state_fixture(efs_mode="existing", efs_file_system_id_declared="fs-real12345", resolved_efs_id="fs-real12345")
        _resolved_inputs(state, descriptor=descriptor)

    def test_33_existing_efs_different_state_id_fails(self):
        descriptor = _descriptor(efsMode="existing", efsFileSystemId="fs-real12345")
        state = _resolved_state_fixture(efs_mode="existing", efs_file_system_id_declared="fs-real12345", resolved_efs_id="fs-DIFFERENT")
        with self.assertRaises(phase5_runtime.Phase5Error):
            _resolved_inputs(state, descriptor=descriptor)

    def test_34_managed_validate_placeholder_passes(self):
        descriptor = _descriptor(efsMode="managed", efsCreationToken="gg-dev-oracle-01-u02")
        state = _resolved_state_fixture(deploy=False, efs_mode="managed", efs_creation_token="gg-dev-oracle-01-u02", resolved_efs_id=phase5_runtime.EFS_DRY_RUN_PLACEHOLDER)
        _resolved_inputs(state, descriptor=descriptor)

    def test_35_managed_validate_real_fs_id_fails(self):
        descriptor = _descriptor(efsMode="managed", efsCreationToken="gg-dev-oracle-01-u02")
        state = _resolved_state_fixture(deploy=False, efs_mode="managed", efs_creation_token="gg-dev-oracle-01-u02", resolved_efs_id="fs-managed42")
        with self.assertRaises(phase5_runtime.Phase5Error):
            _resolved_inputs(state, descriptor=descriptor)

    def test_36_managed_deploy_local_validation_accepts_shape_zero_aws_calls(self):
        descriptor = _descriptor(efsMode="managed", efsCreationToken="gg-dev-oracle-01-u02")
        state = _resolved_state_fixture(deploy=True, efs_mode="managed", efs_creation_token="gg-dev-oracle-01-u02", resolved_efs_id="fs-managed42")
        scripted = ScriptedRun()
        scripted.when(_starts_with(sys.executable, str(phase5_runtime.DEPLOYMENT_MODEL_TOOL)), FakeProc(0, json.dumps(descriptor)))
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            phase5_runtime._validate_resolved_runtime_inputs(state, ENVIRONMENT, DEPLOYMENT_ID, verify_managed_efs_live=False)
        self.assertEqual([c for c in scripted.calls if c["argv"][:1] == ["aws"]], [])

    def _managed_live_scripted(self, descriptor, fs_id="fs-managed42"):
        scripted = ScriptedRun()
        scripted.when(_starts_with(sys.executable, str(phase5_runtime.DEPLOYMENT_MODEL_TOOL)), FakeProc(0, json.dumps(descriptor)))
        scripted.when(_starts_with("aws", "sts", "assume-role"), FakeProc(0, json.dumps({"Credentials": {"AccessKeyId": "AKIA", "SecretAccessKey": "s", "SessionToken": "t"}})))
        scripted.when(_contains("sts", "get-caller-identity"), FakeProc(0, WORKLOAD_ACCOUNT_ID))
        scripted.when(_starts_with("aws", "efs", "describe-file-systems"), FakeProc(0, json.dumps({"FileSystems": [{"FileSystemId": fs_id, "LifeCycleState": "available", "Tags": self._MANAGED_TAGS}]})))
        return scripted

    def test_37_managed_deploy_reconciliation_performs_fresh_live_resolution(self):
        descriptor = _descriptor(efsMode="managed", efsCreationToken="gg-dev-oracle-01-u02")
        state = _resolved_state_fixture(deploy=True, efs_mode="managed", efs_creation_token="gg-dev-oracle-01-u02", resolved_efs_id="fs-managed42")
        scripted = self._managed_live_scripted(descriptor)
        with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            phase5_runtime._validate_resolved_runtime_inputs(state, ENVIRONMENT, DEPLOYMENT_ID, verify_managed_efs_live=True)
        self.assertEqual(len([c for c in scripted.calls if c["argv"][:3] == ["aws", "efs", "describe-file-systems"]]), 1)

    def test_38_fresh_managed_efs_id_mismatch_fails_before_kubernetes(self):
        descriptor = _descriptor(efsMode="managed", efsCreationToken="gg-dev-oracle-01-u02")
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = _resolved_state_fixture(deploy=True, efs_mode="managed", efs_creation_token="gg-dev-oracle-01-u02", resolved_efs_id="fs-0wrongwrongwrong")
            phase5_runtime.update_state(state_path, state, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = self._managed_live_scripted(descriptor)
            with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error):
                    _run_quiet(phase5_runtime.cmd_reconcile_runtime, args)
            self.assertEqual([c for c in scripted.calls if c["argv"][:1] == ["kubectl"]], [])

    def test_39_fresh_managed_efs_id_match_continues_reconciliation(self):
        descriptor = _descriptor(efsMode="managed", efsCreationToken="gg-dev-oracle-01-u02")
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = _resolved_state_fixture(deploy=True, efs_mode="managed", efs_creation_token="gg-dev-oracle-01-u02", resolved_efs_id="fs-managed42")
            phase5_runtime.update_state(state_path, state, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = _reconcile_scripted_ok()
            for predicate, proc in self._managed_live_scripted(descriptor).rules:
                scripted.when(predicate, proc)
            with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                _run_quiet(phase5_runtime.cmd_reconcile_runtime, args)
            self.assertEqual(len([c for c in scripted.calls if c["argv"][:3] == ["kubectl", "apply", "-f"]]), 1)


class ReconcileMutationPayloadTests(unittest.TestCase):
    """cmd_reconcile_runtime() must build its Application payload from freshly-validated canonical values -- never unvalidated state copies -- and must fail before any cluster access/kubectl call when those values do not match canonical sources."""

    def _run_reconcile(self, state_overrides=None, descriptor=None, expect_error=True):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = _resolved_state_fixture(**(state_overrides or {}))
            phase5_runtime.update_state(state_path, state, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = _reconcile_scripted_ok()
            scripted.when(_starts_with(sys.executable, str(phase5_runtime.DEPLOYMENT_MODEL_TOOL)), FakeProc(0, json.dumps(descriptor if descriptor is not None else _descriptor())))
            with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                if expect_error:
                    with self.assertRaises(phase5_runtime.Phase5Error):
                        _run_quiet(phase5_runtime.cmd_reconcile_runtime, args)
                else:
                    _run_quiet(phase5_runtime.cmd_reconcile_runtime, args)
            return scripted

    def test_40_wrong_chart_version_fails_before_cluster_access(self):
        scripted = self._run_reconcile(state_overrides={"chart_version": "9.9.9-EVIL"})
        self.assertEqual(scripted.calls, [])

    def test_41_wrong_admin_secret_fails_before_cluster_access(self):
        scripted = self._run_reconcile(state_overrides={"admin_secret_name": "dev/goldengate/target/admin"})
        self.assertEqual([c for c in scripted.calls if c["argv"][:1] == ["kubectl"]], [])
        self.assertEqual([c for c in scripted.calls if c["argv"][:2] == ["aws", "eks"]], [])

    def test_42_wrong_efs_id_fails_before_kubectl_apply(self):
        scripted = self._run_reconcile(
            state_overrides={"efs_mode": "existing", "efs_file_system_id_declared": "fs-real12345", "resolved_efs_id": "fs-0wrongwrongwrong"},
            descriptor=_descriptor(efsMode="existing", efsFileSystemId="fs-real12345"),
        )
        self.assertEqual([c for c in scripted.calls if c["argv"][:3] == ["kubectl", "apply", "-f"]], [])

    def test_43_wrong_environment_ingress_identity_fails_before_kubectl_apply(self):
        scripted = self._run_reconcile(state_overrides={"dns_domain": "wrong.example.internal"})
        self.assertEqual([c for c in scripted.calls if c["argv"][:3] == ["kubectl", "apply", "-f"]], [])

    def test_44_valid_canonical_state_builds_exact_application(self):
        scripted = self._run_reconcile(expect_error=False)
        apply_calls = [c for c in scripted.calls if c["argv"][:3] == ["kubectl", "apply", "-f"]]
        self.assertEqual(len(apply_calls), 1)
        import yaml as _yaml
        manifest = _yaml.safe_load(apply_calls[0]["input_text"])
        self.assertEqual(manifest["metadata"]["name"], _canonical_argocd_app_name())
        self.assertEqual(manifest["spec"]["source"]["targetRevision"], CHART_VERSION)

    def test_45_application_uses_freshly_canonical_values_not_unvalidated_state_copies(self):
        scripted = self._run_reconcile(expect_error=False)
        apply_calls = [c for c in scripted.calls if c["argv"][:3] == ["kubectl", "apply", "-f"]]
        import yaml as _yaml
        manifest = _yaml.safe_load(apply_calls[0]["input_text"])
        params = {p["name"]: p["value"] for p in manifest["spec"]["source"]["helm"]["parameters"]}
        self.assertEqual(params["runtime.csi.admin.objectName"], "dev/goldengate/source/admin")
        self.assertEqual(params["ingress.hostDomain"], "goldengate-dev.adcbmis.local")
        self.assertEqual(params["ingress.alb.certificateArn"], f"arn:aws:acm:eu-west-1:{WORKLOAD_ACCOUNT_ID}:certificate/abc-123")


class ValidateLocalDiagnosticsTests(unittest.TestCase):
    def test_46_validate_local_mismatched_identity_fails_before_helm_or_file_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            state_path = repo_root / "state.json"
            state = _resolved_state_fixture(deployment_id="gg-mssql-repltest-01")
            phase5_runtime.update_state(state_path, state, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            rendered_marker = repo_root / "rendered" / f"{DEPLOYMENT_ID}.yaml"
            with _mock_repo_root_with_real_chart(repo_root), mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error):
                    _run_quiet(phase5_runtime.cmd_validate_local, args)
            self.assertEqual(scripted.calls, [])
            self.assertFalse(rendered_marker.exists(), "no rendered output must ever be written for a mismatched-identity state")

    def test_47_validate_local_wrong_descriptor_admin_secret_fails_before_helm_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            state_path = repo_root / "state.json"
            state = _resolved_state_fixture(admin_secret_name="dev/goldengate/target/admin")
            phase5_runtime.update_state(state_path, state, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            scripted.when(_starts_with(sys.executable, str(phase5_runtime.DEPLOYMENT_MODEL_TOOL)), FakeProc(0, json.dumps(_descriptor())))
            with _mock_repo_root_with_real_chart(repo_root), mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error):
                    _run_quiet(phase5_runtime.cmd_validate_local, args)
            self.assertEqual([c for c in scripted.calls if c["argv"][:1] == ["helm"]], [])

    def test_48_post_deploy_diagnostics_cross_runtime_fails_before_eks_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = _reconcile_state_fixture(argocd_app_name="totally-unrelated-app")
            phase5_runtime.update_state(state_path, state, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error):
                    _run_quiet(phase5_runtime.cmd_post_deploy_diagnostics, args)
            self.assertEqual(scripted.calls, [])

    def test_49_valid_post_deploy_diagnostics_non_authoritative_check_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = _reconcile_state_fixture()
            phase5_runtime.update_state(state_path, state, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun(default=FakeProc(1, "", "not found"))
            scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
            with mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                _run_quiet(phase5_runtime.cmd_post_deploy_diagnostics, args)


class PackagedChartIntegrityTests(unittest.TestCase):
    """Confirmed reproduction target: the package validator must prove the COMPLETE archive -- exact canonical root, exact regular-file member set, every non-Chart.yaml/values-deployment.yaml member byte-identical to the current helm/goldengate/ source -- not merely Chart.yaml + values-deployment.yaml in isolation."""

    def _publish_with_package(self, repo_root, package_path_rel, expect_error=True):
        state_path = repo_root / "state.json"
        phase5_runtime.update_state(state_path, {**_reconcile_state_fixture(), "package_path": package_path_rel}, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
        args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "ecr", "get-login-password"), FakeProc(0, "pw"))
        scripted.when(_starts_with("helm", "registry", "login"), FakeProc(0, ""))
        scripted.when(_starts_with("aws", "ecr", "describe-repositories"), FakeProc(0, ""))
        scripted.when(_starts_with("aws", "ecr", "get-repository-policy"), FakeProc(1, "", "RepositoryPolicyNotFoundException"))
        scripted.when(_starts_with("aws", "ecr", "set-repository-policy"), FakeProc(0, ""))
        scripted.when(_starts_with("helm", "push"), FakeProc(0, ""))
        scripted.when(_starts_with("helm", "pull"), FakeProc(0, ""))
        with _mock_repo_root_with_real_chart(repo_root), mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            if expect_error:
                with self.assertRaises(phase5_runtime.Phase5Error) as ctx:
                    _run_quiet(phase5_runtime.cmd_publish_chart, args)
                return scripted, ctx.exception
            _run_quiet(phase5_runtime.cmd_publish_chart, args)
            return scripted, None

    def _assert_zero_network(self, scripted):
        self.assertEqual([c for c in scripted.calls if c["argv"][:1] in (["aws"], ["helm"])], [], "package/artifact mismatch must produce ZERO AWS/ECR/Helm-network calls")

    def test_1_canonical_full_package_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            package_path_rel = _build_fake_chart_package(repo_root, CHART_VERSION)
            scripted, _ = self._publish_with_package(repo_root, package_path_rel, expect_error=False)
            self.assertEqual(len([c for c in scripted.calls if c["argv"][:2] == ["helm", "push"]]), 1)

    def test_2_modified_runtime_statefulset_fails(self):
        """Confirmed reproduction of the current bug: a canonical package with a genuine Chart.yaml/values-deployment.yaml but a replaced runtime-statefulset.yaml previously reached aws ecr/helm registry login/helm push/helm pull."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            malicious = b"apiVersion: apps/v1\nkind: StatefulSet\nmetadata:\n  name: malicious-replacement\n"
            package_path_rel = _build_fake_chart_package(repo_root, CHART_VERSION, member_overrides={"goldengate/templates/runtime-statefulset.yaml": malicious})
            scripted, _ = self._publish_with_package(repo_root, package_path_rel)
            self._assert_zero_network(scripted)

    def test_3_modified_runtime_service_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            package_path_rel = _build_fake_chart_package(repo_root, CHART_VERSION, member_overrides={"goldengate/templates/runtime-service.yaml": b"kind: Service\nmetadata:\n  name: different\n"})
            scripted, _ = self._publish_with_package(repo_root, package_path_rel)
            self._assert_zero_network(scripted)

    def test_4_extra_template_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            package_path_rel = _build_fake_chart_package(repo_root, CHART_VERSION, extra_file_members=[("goldengate/templates/evil.yaml", b"evil: true\n")])
            scripted, _ = self._publish_with_package(repo_root, package_path_rel)
            self._assert_zero_network(scripted)

    def test_5_missing_canonical_template_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            package_path_rel = _build_fake_chart_package(repo_root, CHART_VERSION, omit_members=["goldengate/templates/runtime-statefulset.yaml"])
            scripted, _ = self._publish_with_package(repo_root, package_path_rel)
            self._assert_zero_network(scripted)

    def test_6_chart_yaml_under_evilroot_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            canonical = dict(_canonical_chart_file_members(CHART_VERSION))
            package_path_rel = _build_fake_chart_package(repo_root, CHART_VERSION, omit_members=["goldengate/Chart.yaml"], extra_file_members=[("evilroot/Chart.yaml", canonical["goldengate/Chart.yaml"])])
            scripted, _ = self._publish_with_package(repo_root, package_path_rel)
            self._assert_zero_network(scripted)

    def test_7_values_deployment_under_differentroot_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            values_bytes = b"runtime:\n  containerName: goldengate\n"
            package_path_rel = _build_fake_chart_package(repo_root, CHART_VERSION, values_bytes=values_bytes, omit_members=["goldengate/values-deployment.yaml"], extra_file_members=[("differentroot/values-deployment.yaml", values_bytes)])
            scripted, _ = self._publish_with_package(repo_root, package_path_rel)
            self._assert_zero_network(scripted)

    def test_8_different_roots_for_chart_yaml_and_values_deployment_fails(self):
        """Confirmed reproduction of the current root bug: evilroot/Chart.yaml + differentroot/values-deployment.yaml, otherwise-valid metadata/bytes."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            values_bytes = b"runtime:\n  containerName: goldengate\n"
            canonical = dict(_canonical_chart_file_members(CHART_VERSION))
            package_path_rel = _build_fake_chart_package(
                repo_root, CHART_VERSION, values_bytes=values_bytes,
                omit_members=["goldengate/Chart.yaml", "goldengate/values-deployment.yaml"],
                extra_file_members=[("evilroot/Chart.yaml", canonical["goldengate/Chart.yaml"]), ("differentroot/values-deployment.yaml", values_bytes)],
            )
            scripted, _ = self._publish_with_package(repo_root, package_path_rel)
            self._assert_zero_network(scripted)

    def test_9_path_traversal_member_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            package_path_rel = _build_fake_chart_package(repo_root, CHART_VERSION, extra_file_members=[("goldengate/../evil.yaml", b"evil\n")])
            scripted, _ = self._publish_with_package(repo_root, package_path_rel)
            self._assert_zero_network(scripted)

    def test_10_absolute_member_name_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            package_path_rel = _build_fake_chart_package(repo_root, CHART_VERSION, extra_file_members=[("/etc/passwd", b"evil\n")])
            scripted, _ = self._publish_with_package(repo_root, package_path_rel)
            self._assert_zero_network(scripted)

    def _link_style_package(self, repo_root, tarinfo_type, linkname=None):
        import tarfile
        file_members = [(n, c) for n, c in _canonical_chart_file_members(CHART_VERSION) if n != "goldengate/templates/runtime-statefulset.yaml"]
        info = tarfile.TarInfo(name="goldengate/templates/runtime-statefulset.yaml")
        info.type = tarinfo_type
        if linkname is not None:
            info.linkname = linkname
        values_bytes = b"runtime:\n  containerName: goldengate\n"
        values_path = repo_root / "envs" / ENVIRONMENT / DEPLOYMENT_ID / "values.yaml"
        values_path.parent.mkdir(parents=True, exist_ok=True)
        values_path.write_bytes(values_bytes)
        file_members.append(("goldengate/values-deployment.yaml", values_bytes))
        package_path = repo_root / "packaged" / f"goldengate-{CHART_VERSION}.tgz"
        _write_chart_tar(package_path, file_members, raw_infos=[info])
        return f"packaged/{package_path.name}"

    def test_11_archive_symlink_member_fails(self):
        import tarfile
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            package_path_rel = self._link_style_package(repo_root, tarfile.SYMTYPE, linkname="/etc/passwd")
            scripted, _ = self._publish_with_package(repo_root, package_path_rel)
            self._assert_zero_network(scripted)

    def test_12_archive_hardlink_member_fails(self):
        import tarfile
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            package_path_rel = self._link_style_package(repo_root, tarfile.LNKTYPE, linkname="goldengate/Chart.yaml")
            scripted, _ = self._publish_with_package(repo_root, package_path_rel)
            self._assert_zero_network(scripted)

    def test_13_archive_fifo_device_member_fails(self):
        import tarfile
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            package_path_rel = self._link_style_package(repo_root, tarfile.FIFOTYPE)
            scripted, _ = self._publish_with_package(repo_root, package_path_rel)
            self._assert_zero_network(scripted)

    def test_14_duplicate_canonical_member_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            canonical = dict(_canonical_chart_file_members(CHART_VERSION))
            package_path_rel = _build_fake_chart_package(repo_root, CHART_VERSION, extra_file_members=[("goldengate/Chart.yaml", canonical["goldengate/Chart.yaml"])])
            scripted, _ = self._publish_with_package(repo_root, package_path_rel)
            self._assert_zero_network(scripted)

    def test_15_packaged_chart_yaml_duplicate_key_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            duplicate_key_chart_yaml = f"apiVersion: v2\nname: goldengate\nversion: {CHART_VERSION}\nversion: {CHART_VERSION}\nappVersion: {CHART_VERSION}\n".encode()
            package_path_rel = _build_fake_chart_package(repo_root, CHART_VERSION, member_overrides={"goldengate/Chart.yaml": duplicate_key_chart_yaml})
            scripted, exc = self._publish_with_package(repo_root, package_path_rel)
            self.assertIn("duplicate", str(exc).lower())
            self._assert_zero_network(scripted)

    def test_16_packaged_chart_yaml_description_type_apiversion_changed_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            import yaml as _yaml
            current = _yaml.safe_load(phase5_runtime.HELM_CHART_PATH.joinpath("Chart.yaml").read_bytes())
            current["version"] = CHART_VERSION
            current["appVersion"] = CHART_VERSION
            current["description"] = "a completely different description"
            corrupted_bytes = _yaml.safe_dump(current, default_flow_style=False).encode()
            package_path_rel = _build_fake_chart_package(repo_root, CHART_VERSION, member_overrides={"goldengate/Chart.yaml": corrupted_bytes})
            scripted, _ = self._publish_with_package(repo_root, package_path_rel)
            self._assert_zero_network(scripted)

    def test_17_chart_yaml_adjusted_only_for_version_app_version_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            package_path_rel = _build_fake_chart_package(repo_root, CHART_VERSION)
            scripted, _ = self._publish_with_package(repo_root, package_path_rel, expect_error=False)
            self.assertEqual(len([c for c in scripted.calls if c["argv"][:2] == ["helm", "push"]]), 1)

    def test_18_packaged_values_yaml_differs_from_current_chart_source_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            package_path_rel = _build_fake_chart_package(repo_root, CHART_VERSION, member_overrides={"goldengate/values.yaml": b"totally: different\n"})
            scripted, _ = self._publish_with_package(repo_root, package_path_rel)
            self._assert_zero_network(scripted)

    def test_19_any_current_template_differs_byte_wise_fails(self):
        canonical_names = [n for n, _ in _canonical_chart_file_members(CHART_VERSION) if n not in ("goldengate/Chart.yaml",)]
        for member_name in canonical_names:
            with tempfile.TemporaryDirectory() as tmp:
                repo_root = Path(tmp)
                package_path_rel = _build_fake_chart_package(repo_root, CHART_VERSION, member_overrides={member_name: b"corrupted content that does not match the current source\n"})
                scripted, _ = self._publish_with_package(repo_root, package_path_rel)
                self._assert_zero_network(scripted)

    def test_20_every_regular_canonical_chart_file_represented_exactly_once(self):
        """Independently re-derives the expected file set directly from the CURRENT filesystem tree (never a hand-maintained list of today's filenames, and never by tautologically re-calling the same production function under test) -- so this test requires no manual edit whenever a legitimate chart file is added/removed under helm/goldengate/."""
        expected = phase5_runtime._expected_chart_package_members()
        independently_derived = {
            f"goldengate/{p.relative_to(phase5_runtime.HELM_CHART_PATH).as_posix()}"
            for p in phase5_runtime.HELM_CHART_PATH.rglob("*") if p.is_file()
        }
        independently_derived.add("goldengate/values-deployment.yaml")
        self.assertEqual(expected, independently_derived)
        self.assertGreater(len(expected), 1, "sanity check: the canonical chart source must contain at least one real file")

    def test_source_side_symlink_under_chart_path_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            fake_chart_path = repo_root / "helm" / "goldengate"
            fake_chart_path.mkdir(parents=True)
            (fake_chart_path / "Chart.yaml").write_text("apiVersion: v2\nname: goldengate\nversion: 0.0.0\n")
            try:
                (fake_chart_path / "evil-symlink.yaml").symlink_to(fake_chart_path / "Chart.yaml")
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not supported on this platform/filesystem")
            with mock.patch.multiple(phase5_runtime, REPO_ROOT=repo_root, HELM_CHART_PATH=fake_chart_path):
                with self.assertRaises(phase5_runtime.Phase5Error) as ctx:
                    phase5_runtime._expected_chart_package_members()
            self.assertIn("evil-symlink.yaml", str(ctx.exception))


class PackageDirectoryContainmentTests(unittest.TestCase):
    def test_21_normal_real_packaged_dir_canonical_package_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "packaged").mkdir()
            package_path_rel = f"packaged/goldengate-{CHART_VERSION}.tgz"
            (repo_root / package_path_rel).write_bytes(b"fake")
            with mock.patch.object(phase5_runtime, "REPO_ROOT", repo_root):
                resolved = phase5_runtime._validate_package_path_and_containment(package_path_rel, CHART_VERSION)
            self.assertEqual(resolved, (repo_root / package_path_rel).resolve())

    def test_23_packaged_directory_itself_symlinked_outside_fails_zero_network(self):
        """Confirmed reproduction of the newly-confirmed gap: REPO_ROOT/packaged -> an external directory, with the exact canonical package placed inside it. Proven end-to-end via cmd_publish_chart with a call-recording stub."""
        with tempfile.TemporaryDirectory() as outer:
            outer_path = Path(outer)
            build_root = outer_path / "build"
            build_root.mkdir()
            package_path_rel = _build_fake_chart_package(build_root, CHART_VERSION)
            built_package = build_root / package_path_rel

            repo_root = outer_path / "repo"
            repo_root.mkdir()
            values_src = build_root / "envs" / ENVIRONMENT / DEPLOYMENT_ID / "values.yaml"
            values_dst = repo_root / "envs" / ENVIRONMENT / DEPLOYMENT_ID / "values.yaml"
            values_dst.parent.mkdir(parents=True)
            values_dst.write_bytes(values_src.read_bytes())

            external_dir = outer_path / "external"
            external_dir.mkdir()
            (external_dir / built_package.name).write_bytes(built_package.read_bytes())
            try:
                (repo_root / "packaged").symlink_to(external_dir, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not supported on this platform/filesystem")

            state_path = repo_root / "state.json"
            phase5_runtime.update_state(state_path, {**_reconcile_state_fixture(), "package_path": package_path_rel}, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            with _mock_repo_root_with_real_chart(repo_root), mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error):
                    _run_quiet(phase5_runtime.cmd_publish_chart, args)
            self.assertEqual(scripted.calls, [])

    def test_24_canonical_package_path_missing_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "packaged").mkdir()
            package_path_rel = f"packaged/goldengate-{CHART_VERSION}.tgz"
            with mock.patch.object(phase5_runtime, "REPO_ROOT", repo_root):
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._validate_package_path_and_containment(package_path_rel, CHART_VERSION)

    def test_25_canonical_package_path_is_directory_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            package_path_rel = f"packaged/goldengate-{CHART_VERSION}.tgz"
            (repo_root / package_path_rel).mkdir(parents=True)
            with mock.patch.object(phase5_runtime, "REPO_ROOT", repo_root):
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._validate_package_path_and_containment(package_path_rel, CHART_VERSION)

    def test_26_package_path_traversal_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "packaged").mkdir()
            with mock.patch.object(phase5_runtime, "REPO_ROOT", repo_root):
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._validate_package_path_and_containment(f"packaged/../../etc/goldengate-{CHART_VERSION}.tgz", CHART_VERSION)

    def test_27_absolute_package_path_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "packaged").mkdir()
            with mock.patch.object(phase5_runtime, "REPO_ROOT", repo_root):
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._validate_package_path_and_containment("/etc/passwd", CHART_VERSION)


def _mirror_real_chart_tree(dest):
    """Copies the REAL current helm/goldengate/ source tree to an arbitrary external destination -- used to build a symlinked-chart-root fixture whose CONTENT is otherwise perfectly canonical, so the tests below prove the root-symlink rejection fires independently of content correctness."""
    import shutil as _shutil
    _shutil.copytree(phase5_runtime.HELM_CHART_PATH, dest)
    return dest


class ChartSourceRootIntegrityTests(unittest.TestCase):
    """Confirmed reproduction target: HELM_CHART_PATH itself being a symlink to an external directory was accepted by _expected_chart_package_members()/_validate_packaged_chart_contents() -- the canonical chart source root must be the physical repository-owned directory REPO_ROOT/helm/goldengate, never reachable through a directory-level symlink (the same protection already applied to REPO_ROOT/packaged)."""

    def test_1_real_repository_owned_chart_path_passes(self):
        resolved = phase5_runtime._validate_canonical_chart_source_root()
        self.assertEqual(resolved, phase5_runtime.HELM_CHART_PATH)
        self.assertFalse(resolved.is_symlink())

    def test_2_symlinked_chart_root_to_external_directory_fails(self):
        """Confirmed reproduction of the current bug before the fix."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            external_chart = _mirror_real_chart_tree(Path(tmp) / "external-chart")
            (repo_root / "helm").mkdir()
            try:
                (repo_root / "helm" / "goldengate").symlink_to(external_chart, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not supported on this platform/filesystem")
            with mock.patch.multiple(phase5_runtime, REPO_ROOT=repo_root, HELM_CHART_PATH=repo_root / "helm" / "goldengate"):
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._validate_canonical_chart_source_root()

    def test_3_symlinked_chart_root_with_perfectly_canonical_tree_still_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            external_chart = _mirror_real_chart_tree(Path(tmp) / "external-chart")
            (repo_root / "helm").mkdir()
            try:
                (repo_root / "helm" / "goldengate").symlink_to(external_chart, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not supported on this platform/filesystem")
            with mock.patch.multiple(phase5_runtime, REPO_ROOT=repo_root, HELM_CHART_PATH=repo_root / "helm" / "goldengate"):
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._expected_chart_package_members()

    def test_4_symlinked_chart_root_with_matching_package_fails_validate_packaged_chart_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_root = Path(tmp) / "build"
            build_root.mkdir()
            package_path_rel = _build_fake_chart_package(build_root, CHART_VERSION)
            resolved_package_path = (build_root / package_path_rel).resolve()

            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            external_chart = _mirror_real_chart_tree(Path(tmp) / "external-chart")
            (repo_root / "helm").mkdir()
            try:
                (repo_root / "helm" / "goldengate").symlink_to(external_chart, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not supported on this platform/filesystem")

            values_src = build_root / "envs" / ENVIRONMENT / DEPLOYMENT_ID / "values.yaml"
            values_dst = repo_root / "envs" / ENVIRONMENT / DEPLOYMENT_ID / "values.yaml"
            values_dst.parent.mkdir(parents=True)
            values_dst.write_bytes(values_src.read_bytes())

            with mock.patch.multiple(phase5_runtime, REPO_ROOT=repo_root, HELM_CHART_PATH=repo_root / "helm" / "goldengate"):
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._validate_packaged_chart_contents(resolved_package_path, CHART_VERSION, ENVIRONMENT, DEPLOYMENT_ID)

    def test_5_symlinked_root_fails_before_rglob_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            external_chart = _mirror_real_chart_tree(Path(tmp) / "external-chart")
            # A descendant that would itself independently trip the (later, still-correct) child-symlink defense if the walk ever reached it -- proves the root check fires FIRST, never getting this far.
            poison_name = "poison-descendant-should-never-be-reached.yaml"
            (external_chart / poison_name).symlink_to(external_chart / "Chart.yaml")
            (repo_root / "helm").mkdir()
            try:
                (repo_root / "helm" / "goldengate").symlink_to(external_chart, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not supported on this platform/filesystem")
            with mock.patch.multiple(phase5_runtime, REPO_ROOT=repo_root, HELM_CHART_PATH=repo_root / "helm" / "goldengate"):
                with self.assertRaises(phase5_runtime.Phase5Error) as ctx:
                    phase5_runtime._expected_chart_package_members()
            self.assertNotIn(poison_name, str(ctx.exception))

    def test_6_chart_root_missing_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with mock.patch.multiple(phase5_runtime, REPO_ROOT=repo_root, HELM_CHART_PATH=repo_root / "helm" / "goldengate"):
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._validate_canonical_chart_source_root()

    def test_7_chart_root_is_regular_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "helm").mkdir()
            (repo_root / "helm" / "goldengate").write_text("not a directory")
            with mock.patch.multiple(phase5_runtime, REPO_ROOT=repo_root, HELM_CHART_PATH=repo_root / "helm" / "goldengate"):
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._validate_canonical_chart_source_root()

    def test_8_helm_chart_path_points_to_another_directory_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            other_dir = Path(tmp) / "other-chart-dir"
            other_dir.mkdir()
            (other_dir / "Chart.yaml").write_text("apiVersion: v2\nname: goldengate\nversion: 0.0.0\n")
            with mock.patch.multiple(phase5_runtime, REPO_ROOT=repo_root, HELM_CHART_PATH=other_dir):
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._validate_canonical_chart_source_root()

    def test_9_normal_root_with_descendant_symlink_still_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            chart_path = repo_root / "helm" / "goldengate"
            chart_path.mkdir(parents=True)
            (chart_path / "Chart.yaml").write_text("apiVersion: v2\nname: goldengate\nversion: 0.0.0\n")
            try:
                (chart_path / "evil.yaml").symlink_to(chart_path / "Chart.yaml")
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not supported on this platform/filesystem")
            with mock.patch.multiple(phase5_runtime, REPO_ROOT=repo_root, HELM_CHART_PATH=chart_path):
                with self.assertRaises(phase5_runtime.Phase5Error) as ctx:
                    phase5_runtime._expected_chart_package_members()
            self.assertIn("evil.yaml", str(ctx.exception))

    def test_10_package_runtime_chart_refuses_symlinked_root_before_copytree(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            external_chart = _mirror_real_chart_tree(Path(tmp) / "external-chart")
            (repo_root / "helm").mkdir()
            try:
                (repo_root / "helm" / "goldengate").symlink_to(external_chart, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not supported on this platform/filesystem")
            values_rel = f"envs/{ENVIRONMENT}/{DEPLOYMENT_ID}/values.yaml"
            (repo_root / values_rel).parent.mkdir(parents=True)
            (repo_root / values_rel).write_text("runtime:\n  containerName: goldengate\n")
            temp_chart_marker = repo_root / phase5_runtime._canonical_temp_chart_path(DEPLOYMENT_ID)
            with mock.patch.multiple(phase5_runtime, REPO_ROOT=repo_root, HELM_CHART_PATH=repo_root / "helm" / "goldengate"):
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._package_runtime_chart(DEPLOYMENT_ID, values_rel, CHART_VERSION)
            self.assertFalse(temp_chart_marker.exists(), "copytree must never run against an untrusted symlinked chart root")

    def test_11_valid_canonical_root_allows_normal_package_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            package_path_rel = _build_fake_chart_package(repo_root, CHART_VERSION)
            state_path = repo_root / "state.json"
            phase5_runtime.update_state(state_path, {**_reconcile_state_fixture(), "package_path": package_path_rel}, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            scripted.when(_starts_with("aws", "ecr", "get-login-password"), FakeProc(0, "pw"))
            scripted.when(_starts_with("helm", "registry", "login"), FakeProc(0, ""))
            scripted.when(_starts_with("aws", "ecr", "describe-repositories"), FakeProc(0, ""))
            scripted.when(_starts_with("aws", "ecr", "get-repository-policy"), FakeProc(1, "", "RepositoryPolicyNotFoundException"))
            scripted.when(_starts_with("aws", "ecr", "set-repository-policy"), FakeProc(0, ""))
            scripted.when(_starts_with("helm", "push"), FakeProc(0, ""))
            scripted.when(_starts_with("helm", "pull"), FakeProc(0, ""))
            with _mock_repo_root_with_real_chart(repo_root), mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                _run_quiet(phase5_runtime.cmd_publish_chart, args)
            self.assertEqual(len([c for c in scripted.calls if c["argv"][:2] == ["helm", "push"]]), 1)

    def test_zero_network_calls_when_helm_chart_path_symlinked(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_root = Path(tmp) / "build"
            build_root.mkdir()
            package_path_rel = _build_fake_chart_package(build_root, CHART_VERSION)

            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            external_chart = _mirror_real_chart_tree(Path(tmp) / "external-chart")
            (repo_root / "helm").mkdir()
            try:
                (repo_root / "helm" / "goldengate").symlink_to(external_chart, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not supported on this platform/filesystem")

            values_src = build_root / "envs" / ENVIRONMENT / DEPLOYMENT_ID / "values.yaml"
            values_dst = repo_root / "envs" / ENVIRONMENT / DEPLOYMENT_ID / "values.yaml"
            values_dst.parent.mkdir(parents=True)
            values_dst.write_bytes(values_src.read_bytes())

            packaged_dst = repo_root / package_path_rel
            packaged_dst.parent.mkdir(parents=True)
            packaged_dst.write_bytes((build_root / package_path_rel).read_bytes())

            state_path = repo_root / "state.json"
            phase5_runtime.update_state(state_path, {**_reconcile_state_fixture(), "package_path": package_path_rel}, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            with mock.patch.multiple(phase5_runtime, REPO_ROOT=repo_root, HELM_CHART_PATH=repo_root / "helm" / "goldengate"), mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error):
                    _run_quiet(phase5_runtime.cmd_publish_chart, args)
            self.assertEqual(scripted.calls, [])


class ValidateLocalChartRootOrderingTests(unittest.TestCase):
    """Confirmed reproduction target: cmd_validate_local() previously ran helm dependency build/lint/template against HELM_CHART_PATH BEFORE the canonical chart-root check (previously only inside _package_runtime_chart) ever fired -- an untrusted symlinked chart root was inspected/executed by Helm before the filesystem trust boundary ran."""

    def _symlinked_root_fixture(self, tmp):
        repo_root = Path(tmp) / "repo"
        repo_root.mkdir()
        external_chart = Path(tmp) / "external-chart"
        external_chart.mkdir()
        (external_chart / "Chart.yaml").write_text("apiVersion: v2\nname: goldengate\nversion: 0.0.0\n")
        (external_chart / "values.yaml").write_text("runtime:\n  containerName: goldengate\n")
        (repo_root / "helm").mkdir()
        try:
            (repo_root / "helm" / "goldengate").symlink_to(external_chart, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not supported on this platform/filesystem")

        values_rel = f"envs/{ENVIRONMENT}/{DEPLOYMENT_ID}/values.yaml"
        (repo_root / values_rel).parent.mkdir(parents=True)
        (repo_root / values_rel).write_text("runtime:\n  containerName: goldengate\n")

        state_path = repo_root / "state.json"
        phase5_runtime.update_state(state_path, _resolved_state_fixture(), phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
        args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
        return repo_root, args

    def _run_with_symlinked_root(self, tmp):
        repo_root, args = self._symlinked_root_fixture(tmp)
        scripted = ScriptedRun()
        scripted.when(_starts_with(sys.executable, str(phase5_runtime.DEPLOYMENT_MODEL_TOOL)), FakeProc(0, json.dumps(_descriptor())))
        chart_path = repo_root / "helm" / "goldengate"
        with mock.patch.multiple(phase5_runtime, REPO_ROOT=repo_root, HELM_CHART_PATH=chart_path), \
             mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_validate_local, args)
        return repo_root, scripted

    def test_1_normal_canonical_chart_root_still_resolves(self):
        """Confirms the fix does not regress the happy path -- see ValidateLocalIntegrationTests.test_full_validate_local_flow for the full end-to-end contract."""
        chart_root = phase5_runtime._validate_canonical_chart_source_root()
        self.assertEqual(chart_root, phase5_runtime.HELM_CHART_PATH)

    def test_2_symlinked_chart_root_fails_before_any_helm_command(self):
        """Confirmed reproduction of the current gap before the fix."""
        with tempfile.TemporaryDirectory() as tmp:
            _repo_root, scripted = self._run_with_symlinked_root(tmp)
            self.assertEqual([c for c in scripted.calls if c["argv"][:1] == ["helm"]], [])

    def test_3_symlinked_chart_root_writes_no_rendered_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root, _scripted = self._run_with_symlinked_root(tmp)
            rendered_marker = repo_root / phase5_runtime._canonical_rendered_manifest_path(DEPLOYMENT_ID)
            self.assertFalse(rendered_marker.exists())

    def test_4_validate_required_files_never_reached_for_symlinked_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root, args = self._symlinked_root_fixture(tmp)
            scripted = ScriptedRun()
            scripted.when(_starts_with(sys.executable, str(phase5_runtime.DEPLOYMENT_MODEL_TOOL)), FakeProc(0, json.dumps(_descriptor())))
            chart_path = repo_root / "helm" / "goldengate"
            with mock.patch.multiple(phase5_runtime, REPO_ROOT=repo_root, HELM_CHART_PATH=chart_path), \
                 mock.patch.object(phase5_runtime, "run", scripted), \
                 mock.patch.object(phase5_runtime, "_validate_required_files") as required_files_mock, \
                 _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error):
                    _run_quiet(phase5_runtime.cmd_validate_local, args)
            required_files_mock.assert_not_called()

    def _normal_root_fixture(self, tmp):
        repo_root = Path(tmp)
        chart_dir = repo_root / "helm" / "goldengate"
        chart_dir.mkdir(parents=True)
        (chart_dir / "Chart.yaml").write_text("apiVersion: v2\nname: goldengate\n")
        (chart_dir / "values.yaml").write_text("runtime:\n  containerName: goldengate\n")

        values_rel = f"envs/{ENVIRONMENT}/{DEPLOYMENT_ID}/values.yaml"
        values_path = repo_root / values_rel
        values_path.parent.mkdir(parents=True)
        values_path.write_text("runtime:\n  containerName: goldengate\n  name: " + DEPLOYMENT_ID + "\ningress:\n  enabled: false\n")

        state_path = repo_root / "state.json"
        phase5_runtime.update_state(state_path, {
            **_reconcile_state_fixture(), "target_namespace": TARGET_NAMESPACE,
            "admin_secret_name": "dev/goldengate/source/admin", "tls_secret_name": "dev/goldengate/tls-certificate",
            "runtime_service_account_name": "gg-runtime-sa", "image_repository": IMAGE_REPOSITORY,
            "image_repository_name": "aws-cloud-factory-goldengate-oracle", "image_tag": IMAGE_TAG,
            "image_digest": "sha256:" + "ab" * 32, "dns_domain": "goldengate-dev.adcbmis.local",
            "alb_group_name": "goldengate-dev-shared", "certificate_arn": f"arn:aws:acm:eu-west-1:{WORKLOAD_ACCOUNT_ID}:certificate/abc-123",
            "resolved_efs_id": "", "efs_mode": "", "efs_file_system_id_declared": "", "efs_creation_token": "",
        }, phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
        args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
        return repo_root, chart_dir, args

    def test_5_normal_chart_root_tolerates_only_dependency_build_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root, chart_dir, args = self._normal_root_fixture(tmp)
            import yaml as _yaml
            rendered_yaml = "\n---\n".join(_yaml.safe_dump(d) for d in (
                _statefulset(), _service(DEPLOYMENT_ID), _service(f"{DEPLOYMENT_ID}-headless", headless=True), _admin_spc("dev/goldengate/source/admin"),
            ))
            package_dir = repo_root / "packaged"
            scripted = ScriptedRun()
            scripted.when(_starts_with(sys.executable, str(phase5_runtime.DEPLOYMENT_MODEL_TOOL)), FakeProc(0, json.dumps(_descriptor())))
            scripted.when(_starts_with("helm", "dependency", "build"), FakeProc(1, "", "Error: no repositories configured"))
            scripted.when(_starts_with("helm", "lint"), FakeProc(0, ""))
            scripted.when(_starts_with("helm", "template"), FakeProc(0, rendered_yaml))

            def fake_helm_package(argv, **kwargs):
                package_dir.mkdir(parents=True, exist_ok=True)
                (package_dir / f"{phase5_runtime.CHART_NAME}-{CHART_VERSION}.tgz").write_bytes(b"fake")
                return FakeProc(0, "")

            with mock.patch.object(phase5_runtime, "REPO_ROOT", repo_root), \
                 mock.patch.object(phase5_runtime, "HELM_CHART_PATH", chart_dir), \
                 mock.patch.object(phase5_runtime, "run") as run_mock, _env_patch():
                def route(argv, **kwargs):
                    if argv[:2] == ["helm", "package"]:
                        return fake_helm_package(argv, **kwargs)
                    return scripted(argv, **kwargs)
                run_mock.side_effect = route
                _run_quiet(phase5_runtime.cmd_validate_local, args)  # must NOT raise -- dependency-build failure remains tolerated

            dep_calls = [c["argv"] for c in scripted.calls if c["argv"][:3] == ["helm", "dependency", "build"]]
            lint_calls = [c["argv"] for c in scripted.calls if c["argv"][:2] == ["helm", "lint"]]
            self.assertEqual(len(dep_calls), 1)
            self.assertEqual(len(lint_calls), 1, "lint must still run and remain authoritative despite the tolerated dependency-build failure")

    def test_6_normal_chart_root_lint_and_template_use_validated_chart_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root, chart_dir, args = self._normal_root_fixture(tmp)
            import yaml as _yaml
            rendered_yaml = "\n---\n".join(_yaml.safe_dump(d) for d in (
                _statefulset(), _service(DEPLOYMENT_ID), _service(f"{DEPLOYMENT_ID}-headless", headless=True), _admin_spc("dev/goldengate/source/admin"),
            ))
            package_dir = repo_root / "packaged"
            scripted = ScriptedRun()
            scripted.when(_starts_with(sys.executable, str(phase5_runtime.DEPLOYMENT_MODEL_TOOL)), FakeProc(0, json.dumps(_descriptor())))
            scripted.when(_starts_with("helm", "dependency", "build"), FakeProc(0, ""))
            scripted.when(_starts_with("helm", "lint"), FakeProc(0, ""))
            scripted.when(_starts_with("helm", "template"), FakeProc(0, rendered_yaml))

            def fake_helm_package(argv, **kwargs):
                package_dir.mkdir(parents=True, exist_ok=True)
                (package_dir / f"{phase5_runtime.CHART_NAME}-{CHART_VERSION}.tgz").write_bytes(b"fake")
                return FakeProc(0, "")

            with mock.patch.object(phase5_runtime, "REPO_ROOT", repo_root), \
                 mock.patch.object(phase5_runtime, "HELM_CHART_PATH", chart_dir), \
                 mock.patch.object(phase5_runtime, "run") as run_mock, _env_patch():
                def route(argv, **kwargs):
                    if argv[:2] == ["helm", "package"]:
                        return fake_helm_package(argv, **kwargs)
                    return scripted(argv, **kwargs)
                run_mock.side_effect = route
                _run_quiet(phase5_runtime.cmd_validate_local, args)

            lint_call = next(c["argv"] for c in scripted.calls if c["argv"][:2] == ["helm", "lint"])
            template_call = next(c["argv"] for c in scripted.calls if c["argv"][:2] == ["helm", "template"])
            self.assertIn(str(chart_dir), lint_call)
            self.assertIn(str(chart_dir), template_call)


class PackagedOutputRootTests(unittest.TestCase):
    """Confirmed reproduction target: REPO_ROOT/packaged symlinked to an external directory let helm package's generated archive land entirely outside the repository, only discovered (if at all) by the LATER publish-time containment check -- _prepare_canonical_packaged_output_root() must validate/create the canonical packaged/ directory BEFORE helm package (or even copytree) ever runs."""

    def test_7_packaged_absent_helper_creates_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with mock.patch.object(phase5_runtime, "REPO_ROOT", repo_root):
                result = phase5_runtime._prepare_canonical_packaged_output_root()
            self.assertEqual(result, repo_root / "packaged")
            self.assertTrue(result.is_dir())
            self.assertFalse(result.is_symlink())

    def test_8_packaged_existing_real_directory_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "packaged").mkdir()
            with mock.patch.object(phase5_runtime, "REPO_ROOT", repo_root):
                result = phase5_runtime._prepare_canonical_packaged_output_root()
            self.assertEqual(result, repo_root / "packaged")

    def test_9_packaged_symlink_to_external_directory_fails(self):
        """Confirmed reproduction of the current gap before the fix."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            external_dir = Path(tmp) / "external"
            external_dir.mkdir()
            try:
                (repo_root / "packaged").symlink_to(external_dir, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not supported on this platform/filesystem")
            with mock.patch.object(phase5_runtime, "REPO_ROOT", repo_root):
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._prepare_canonical_packaged_output_root()

    def test_10_packaged_exists_as_regular_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "packaged").write_text("not a directory")
            with mock.patch.object(phase5_runtime, "REPO_ROOT", repo_root):
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._prepare_canonical_packaged_output_root()

    def test_11_packaged_resolves_elsewhere_fails(self):
        """Defense-in-depth proof for the resolved-equality requirement, independent of platform symlink support: Path.resolve() is monkeypatched (scoped to this test only) to make packaged/ resolve somewhere unexpected even though it is not itself a symlink."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "packaged").mkdir()
            elsewhere = Path(tmp) / "elsewhere"
            original_resolve = Path.resolve

            def fake_resolve(self, *a, **kw):
                if self == repo_root / "packaged":
                    return elsewhere
                return original_resolve(self, *a, **kw)

            with mock.patch.object(phase5_runtime, "REPO_ROOT", repo_root), mock.patch.object(Path, "resolve", fake_resolve):
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._prepare_canonical_packaged_output_root()

    def test_12_package_runtime_chart_with_packaged_symlink_zero_helm_package_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            external_dir = Path(tmp) / "external"
            external_dir.mkdir()
            try:
                (repo_root / "packaged").symlink_to(external_dir, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not supported on this platform/filesystem")
            values_rel = f"envs/{ENVIRONMENT}/{DEPLOYMENT_ID}/values.yaml"
            (repo_root / values_rel).parent.mkdir(parents=True)
            (repo_root / values_rel).write_text("runtime:\n  containerName: goldengate\n")
            scripted = ScriptedRun()
            with _mock_repo_root_with_real_chart(repo_root), mock.patch.object(phase5_runtime, "run", scripted):
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._package_runtime_chart(DEPLOYMENT_ID, values_rel, CHART_VERSION)
            self.assertEqual([c for c in scripted.calls if c["argv"][:2] == ["helm", "package"]], [])

    def test_13_package_runtime_chart_with_packaged_symlink_writes_nothing_externally(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            external_dir = Path(tmp) / "external"
            external_dir.mkdir()
            try:
                (repo_root / "packaged").symlink_to(external_dir, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not supported on this platform/filesystem")
            values_rel = f"envs/{ENVIRONMENT}/{DEPLOYMENT_ID}/values.yaml"
            (repo_root / values_rel).parent.mkdir(parents=True)
            (repo_root / values_rel).write_text("runtime:\n  containerName: goldengate\n")
            scripted = ScriptedRun()
            with _mock_repo_root_with_real_chart(repo_root), mock.patch.object(phase5_runtime, "run", scripted):
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._package_runtime_chart(DEPLOYMENT_ID, values_rel, CHART_VERSION)
            self.assertEqual(list(external_dir.iterdir()), [])

    def test_14_package_runtime_chart_uses_validated_absolute_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            values_rel = f"envs/{ENVIRONMENT}/{DEPLOYMENT_ID}/values.yaml"
            (repo_root / values_rel).parent.mkdir(parents=True)
            (repo_root / values_rel).write_text("runtime:\n  containerName: goldengate\n")

            def fake_helm_package(argv, **kwargs):
                dest_index = argv.index("--destination")
                dest = Path(argv[dest_index + 1])
                self.assertTrue(dest.is_absolute(), "helm package --destination must be the validated ABSOLUTE packaged directory, never a bare 'packaged' literal")
                self.assertEqual(dest, (repo_root / "packaged").resolve())
                dest.mkdir(parents=True, exist_ok=True)
                (dest / f"{phase5_runtime.CHART_NAME}-{CHART_VERSION}.tgz").write_bytes(b"fake")
                return FakeProc(0, "")

            with _mock_repo_root_with_real_chart(repo_root), mock.patch.object(phase5_runtime, "run") as run_mock:
                run_mock.side_effect = lambda argv, **kwargs: fake_helm_package(argv, **kwargs) if argv[:2] == ["helm", "package"] else FakeProc(0, "")
                phase5_runtime._package_runtime_chart(DEPLOYMENT_ID, values_rel, CHART_VERSION)

    def test_15_post_package_containment_revalidated(self):
        """Even if helm package itself produced a symlink as the output file (a compromised/buggy helm), _package_runtime_chart() must still reject it -- proving genuine defense in depth, not blind trust of a successful helm package exit code."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            values_rel = f"envs/{ENVIRONMENT}/{DEPLOYMENT_ID}/values.yaml"
            (repo_root / values_rel).parent.mkdir(parents=True)
            (repo_root / values_rel).write_text("runtime:\n  containerName: goldengate\n")
            outside_evil = Path(tmp) / "evil-package.tgz"
            outside_evil.write_bytes(b"evil")

            def fake_helm_package(argv, **kwargs):
                dest = Path(argv[argv.index("--destination") + 1])
                dest.mkdir(parents=True, exist_ok=True)
                target = dest / f"{phase5_runtime.CHART_NAME}-{CHART_VERSION}.tgz"
                try:
                    target.symlink_to(outside_evil)
                except (OSError, NotImplementedError):
                    self.skipTest("symlinks are not supported on this platform/filesystem")
                return FakeProc(0, "")

            with _mock_repo_root_with_real_chart(repo_root), mock.patch.object(phase5_runtime, "run") as run_mock:
                run_mock.side_effect = lambda argv, **kwargs: fake_helm_package(argv, **kwargs) if argv[:2] == ["helm", "package"] else FakeProc(0, "")
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._package_runtime_chart(DEPLOYMENT_ID, values_rel, CHART_VERSION)

    def test_16_package_file_symlink_defense_remains(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "packaged").mkdir()
            outside_evil = Path(tmp).parent / f"outside-{os.getpid()}-defense.tgz"
            outside_evil.write_bytes(b"evil")
            package_path_rel = f"packaged/goldengate-{CHART_VERSION}.tgz"
            try:
                (repo_root / package_path_rel).symlink_to(outside_evil)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not supported on this platform/filesystem")
            with mock.patch.object(phase5_runtime, "REPO_ROOT", repo_root):
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._validate_package_path_and_containment(package_path_rel, CHART_VERSION)
            outside_evil.unlink(missing_ok=True)

    def test_17_normal_package_runtime_chart_still_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            values_rel = f"envs/{ENVIRONMENT}/{DEPLOYMENT_ID}/values.yaml"
            (repo_root / values_rel).parent.mkdir(parents=True)
            (repo_root / values_rel).write_text("runtime:\n  containerName: goldengate\n")

            def fake_helm_package(argv, **kwargs):
                dest = Path(argv[argv.index("--destination") + 1])
                dest.mkdir(parents=True, exist_ok=True)
                (dest / f"{phase5_runtime.CHART_NAME}-{CHART_VERSION}.tgz").write_bytes(b"fake")
                return FakeProc(0, "")

            with _mock_repo_root_with_real_chart(repo_root), mock.patch.object(phase5_runtime, "run") as run_mock:
                run_mock.side_effect = lambda argv, **kwargs: fake_helm_package(argv, **kwargs) if argv[:2] == ["helm", "package"] else FakeProc(0, "")
                temp_chart_path, package_path = phase5_runtime._package_runtime_chart(DEPLOYMENT_ID, values_rel, CHART_VERSION)
            self.assertTrue(package_path.is_file())
            self.assertEqual(package_path.name, f"{phase5_runtime.CHART_NAME}-{CHART_VERSION}.tgz")
            self.assertTrue(temp_chart_path.is_dir())


EXTERNAL_DESCENDANT_SYMLINK_CONTENT = b"TOP-SECRET-EXTERNAL-CONTENT-OUTSIDE-CHART\n"


def _real_chart_with_descendant_file_symlink(test_case, repo_root, relative="templates/runtime-statefulset.yaml", content=EXTERNAL_DESCENDANT_SYMLINK_CONTENT):
    """Builds a REAL, non-symlink chart root (a genuine mirrored copy of the current helm/goldengate/ tree, via _mock_repo_root_with_real_chart()) whose single descendant at `relative` has been replaced by a symlink to an external file OUTSIDE the chart tree entirely -- the exact confirmed-bug shape: the chart ROOT itself passes _validate_canonical_chart_source_root(), but a DESCENDANT does not. Returns (patcher, chart_path, external_file) -- patcher is the still-unentered mock.patch.multiple(...) context manager from _mock_repo_root_with_real_chart(); the caller enters it only after this helper has already mutated the filesystem, since REPO_ROOT/HELM_CHART_PATH must point at the real (unpatched) production chart while the mirrored copy is built."""
    patcher = _mock_repo_root_with_real_chart(repo_root)
    chart_path = repo_root / "helm" / phase5_runtime.CHART_NAME
    target = chart_path / relative
    target.unlink()
    external_file = repo_root.parent / f"external-{Path(relative).name}"
    external_file.write_bytes(content)
    try:
        target.symlink_to(external_file)
    except (OSError, NotImplementedError):
        test_case.skipTest("symlinks are not supported on this platform/filesystem")
    return patcher, chart_path, external_file


def _real_chart_with_descendant_dir_symlink(test_case, repo_root, relative="templates/linked-dir"):
    """Directory-symlink counterpart to _real_chart_with_descendant_file_symlink() -- a REAL, non-symlink chart root whose descendant at `relative` is a symlink to an external DIRECTORY (never created inside the chart tree itself). Returns (patcher, chart_path, external_dir)."""
    patcher = _mock_repo_root_with_real_chart(repo_root)
    chart_path = repo_root / "helm" / phase5_runtime.CHART_NAME
    external_dir = repo_root.parent / "external-linked-dir"
    external_dir.mkdir()
    (external_dir / "poison.yaml").write_text("poison: true\n")
    target = chart_path / relative
    try:
        target.symlink_to(external_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        test_case.skipTest("symlinks are not supported on this platform/filesystem")
    return patcher, chart_path, external_dir


class ChartSourceTreeIntegrityTests(unittest.TestCase):
    """Confirmed reproduction target: REPO_ROOT/helm/goldengate is a REAL, non-symlink directory, but a DESCENDANT (e.g. templates/runtime-statefulset.yaml) is a symlink to an external file -- the chart root passed _validate_canonical_chart_source_root(), yet cmd_validate_local() ran helm dependency build/lint/template, and _package_runtime_chart()'s shutil.copytree() silently dereferenced the symlink into a regular copied file, entirely BEFORE the only existing descendant-symlink check (inside _expected_chart_package_members(), reached only during later package-publication validation) ever fired. _validate_canonical_chart_source_tree() closes this gap by proving the COMPLETE tree -- not merely the root -- before any Helm command, copy, or file read ever trusts it."""

    def _validate_local_fixture_with_descendant_file_symlink(self, tmp, relative="templates/runtime-statefulset.yaml"):
        repo_root = Path(tmp) / "repo"
        repo_root.mkdir()
        patcher, chart_path, external_file = _real_chart_with_descendant_file_symlink(self, repo_root, relative=relative)
        values_rel = f"envs/{ENVIRONMENT}/{DEPLOYMENT_ID}/values.yaml"
        (repo_root / values_rel).parent.mkdir(parents=True)
        (repo_root / values_rel).write_text("runtime:\n  containerName: goldengate\n")
        state_path = repo_root / "state.json"
        phase5_runtime.update_state(state_path, _resolved_state_fixture(), phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
        args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
        return repo_root, chart_path, patcher, args, external_file

    def _run_validate_local_with_descendant_symlink(self, tmp):
        repo_root, _chart_path, patcher, args, _external_file = self._validate_local_fixture_with_descendant_file_symlink(tmp)
        scripted = ScriptedRun()
        scripted.when(_starts_with(sys.executable, str(phase5_runtime.DEPLOYMENT_MODEL_TOOL)), FakeProc(0, json.dumps(_descriptor())))
        with patcher, mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
            with self.assertRaises(phase5_runtime.Phase5Error):
                _run_quiet(phase5_runtime.cmd_validate_local, args)
        return repo_root, scripted

    def test_1_normal_canonical_chart_tree_passes(self):
        chart_root, source_files = phase5_runtime._validate_canonical_chart_source_tree()
        self.assertEqual(chart_root, phase5_runtime.HELM_CHART_PATH)
        self.assertIn("Chart.yaml", source_files)
        self.assertIn("values.yaml", source_files)
        self.assertIn("templates/runtime-statefulset.yaml", source_files)

    def test_2_descendant_file_symlink_to_external_file_fails(self):
        """Confirmed reproduction of the current bug before the fix -- the exact scenario reported: REPO_ROOT/helm/goldengate is real, templates/runtime-statefulset.yaml is a descendant symlink."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            patcher, chart_path, _external_file = _real_chart_with_descendant_file_symlink(self, repo_root)
            self.assertFalse(chart_path.is_symlink(), "the chart ROOT itself must remain a real, non-symlink directory for this to be the confirmed-bug shape")
            self.assertTrue((chart_path / "templates" / "runtime-statefulset.yaml").is_symlink())
            with patcher:
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._validate_canonical_chart_source_tree()

    def test_3_cmd_validate_local_zero_helm_calls_for_descendant_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            _repo_root, scripted = self._run_validate_local_with_descendant_symlink(tmp)
        self.assertEqual([c for c in scripted.calls if c["argv"][:1] == ["helm"]], [])

    def test_4_cmd_validate_local_no_rendered_manifest_for_descendant_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root, _scripted = self._run_validate_local_with_descendant_symlink(tmp)
            rendered_marker = repo_root / phase5_runtime._canonical_rendered_manifest_path(DEPLOYMENT_ID)
            self.assertFalse(rendered_marker.exists())

    def test_5_cmd_validate_local_no_package_archive_for_descendant_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root, _scripted = self._run_validate_local_with_descendant_symlink(tmp)
            self.assertFalse((repo_root / "packaged").exists())

    def test_6_descendant_directory_symlink_fails_before_helm(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            patcher, chart_path, _external_dir = _real_chart_with_descendant_dir_symlink(self, repo_root)
            self.assertFalse(chart_path.is_symlink())
            self.assertTrue((chart_path / "templates" / "linked-dir").is_symlink())
            with patcher:
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._validate_canonical_chart_source_tree()

        with tempfile.TemporaryDirectory() as tmp2:
            repo_root2 = Path(tmp2) / "repo"
            repo_root2.mkdir()
            patcher2, chart_path2, _external_dir2 = _real_chart_with_descendant_dir_symlink(self, repo_root2)
            values_rel = f"envs/{ENVIRONMENT}/{DEPLOYMENT_ID}/values.yaml"
            (repo_root2 / values_rel).parent.mkdir(parents=True)
            (repo_root2 / values_rel).write_text("runtime:\n  containerName: goldengate\n")
            state_path = repo_root2 / "state.json"
            phase5_runtime.update_state(state_path, _resolved_state_fixture(), phase5_runtime.RECONCILE_ALLOWED_STATE_KEYS)
            args = argparse_namespace(environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, state_path=state_path)
            scripted = ScriptedRun()
            scripted.when(_starts_with(sys.executable, str(phase5_runtime.DEPLOYMENT_MODEL_TOOL)), FakeProc(0, json.dumps(_descriptor())))
            with patcher2, mock.patch.object(phase5_runtime, "run", scripted), _env_patch():
                with self.assertRaises(phase5_runtime.Phase5Error):
                    _run_quiet(phase5_runtime.cmd_validate_local, args)
            self.assertEqual([c for c in scripted.calls if c["argv"][:1] == ["helm"]], [])

    def test_7_chart_yaml_itself_symlink_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            patcher, _chart_path, _external_file = _real_chart_with_descendant_file_symlink(self, repo_root, relative="Chart.yaml")
            with patcher:
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._validate_canonical_chart_source_tree()

    def test_8_values_yaml_itself_symlink_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            patcher, _chart_path, _external_file = _real_chart_with_descendant_file_symlink(self, repo_root, relative="values.yaml")
            with patcher:
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._validate_canonical_chart_source_tree()

    def test_9_nested_template_symlink_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            patcher, _chart_path, _external_file = _real_chart_with_descendant_file_symlink(self, repo_root, relative="templates/_helpers.tpl")
            with patcher:
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._validate_canonical_chart_source_tree()

    def test_10_fifo_special_object_under_chart_tree_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            patcher = _mock_repo_root_with_real_chart(repo_root)
            chart_path = repo_root / "helm" / phase5_runtime.CHART_NAME
            fifo_path = chart_path / "templates" / "special-fifo"
            try:
                os.mkfifo(fifo_path)
            except (AttributeError, OSError, NotImplementedError):
                self.skipTest("FIFOs are not supported on this platform/filesystem")
            with patcher:
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._validate_canonical_chart_source_tree()

    def test_11_package_runtime_chart_refuses_descendant_symlink_zero_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            patcher, _chart_path, _external_file = _real_chart_with_descendant_file_symlink(self, repo_root)
            values_rel = f"envs/{ENVIRONMENT}/{DEPLOYMENT_ID}/values.yaml"
            (repo_root / values_rel).parent.mkdir(parents=True)
            (repo_root / values_rel).write_text("runtime:\n  containerName: goldengate\n")
            temp_chart_marker = repo_root / phase5_runtime._canonical_temp_chart_path(DEPLOYMENT_ID)
            scripted = ScriptedRun()
            with patcher, mock.patch.object(phase5_runtime, "run", scripted):
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._package_runtime_chart(DEPLOYMENT_ID, values_rel, CHART_VERSION)
            self.assertFalse(temp_chart_marker.exists(), "copytree must never run when a descendant chart-source symlink is present")
            self.assertEqual(scripted.calls, [], "helm package must never run when a descendant chart-source symlink is present")
            self.assertFalse((repo_root / "packaged").exists())

    def test_12_retired_copytree_behavior_would_dereference_but_current_code_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Reproduce the RETIRED (pre-fix) copytree behavior in an isolated fixture -- proves what the confirmed bug actually did to a source symlink.
            old_src = Path(tmp) / "old-src"
            old_src.mkdir()
            (old_src / "Chart.yaml").write_text("apiVersion: v2\nname: goldengate\nversion: 0.0.0\n")
            external_target = Path(tmp) / "external-target.yaml"
            external_target.write_bytes(b"TOP-SECRET-EXTERNAL-CONTENT\n")
            try:
                (old_src / "linked.yaml").symlink_to(external_target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not supported on this platform/filesystem")
            old_dst = Path(tmp) / "old-dst"
            shutil.copytree(old_src, old_dst, dirs_exist_ok=True)  # retired: default (dereferencing) semantics, as _package_runtime_chart() used before this fix
            self.assertFalse((old_dst / "linked.yaml").is_symlink(), "retired copytree default silently dereferences a source symlink into a regular copied file")
            self.assertEqual((old_dst / "linked.yaml").read_bytes(), b"TOP-SECRET-EXTERNAL-CONTENT\n")

            # Now prove the CURRENT _package_runtime_chart() refuses the same shape instead of ever reaching copytree.
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            patcher, _chart_path, _external_file = _real_chart_with_descendant_file_symlink(self, repo_root)
            values_rel = f"envs/{ENVIRONMENT}/{DEPLOYMENT_ID}/values.yaml"
            (repo_root / values_rel).parent.mkdir(parents=True)
            (repo_root / values_rel).write_text("runtime:\n  containerName: goldengate\n")
            temp_chart_marker = repo_root / phase5_runtime._canonical_temp_chart_path(DEPLOYMENT_ID)
            with patcher:
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._package_runtime_chart(DEPLOYMENT_ID, values_rel, CHART_VERSION)
            self.assertFalse(temp_chart_marker.exists())

    def test_13_copytree_uses_non_dereferencing_symlink_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            values_rel = f"envs/{ENVIRONMENT}/{DEPLOYMENT_ID}/values.yaml"
            (repo_root / values_rel).parent.mkdir(parents=True)
            (repo_root / values_rel).write_text("runtime:\n  containerName: goldengate\n")

            def fake_helm_package(argv, **kwargs):
                dest = Path(argv[argv.index("--destination") + 1])
                dest.mkdir(parents=True, exist_ok=True)
                (dest / f"{phase5_runtime.CHART_NAME}-{CHART_VERSION}.tgz").write_bytes(b"fake")
                return FakeProc(0, "")

            with _mock_repo_root_with_real_chart(repo_root), \
                 mock.patch.object(phase5_runtime, "run") as run_mock, \
                 mock.patch.object(phase5_runtime.shutil, "copytree", wraps=phase5_runtime.shutil.copytree) as copytree_mock:
                run_mock.side_effect = lambda argv, **kwargs: fake_helm_package(argv, **kwargs) if argv[:2] == ["helm", "package"] else FakeProc(0, "")
                phase5_runtime._package_runtime_chart(DEPLOYMENT_ID, values_rel, CHART_VERSION)
            # copytree recurses into subdirectories through the same (wrapped) global name, so call_args_list[0] -- the OUTERMOST, top-level call made directly by _package_runtime_chart() -- is the one that must carry symlinks=True; later entries are copytree's own internal per-subdirectory recursion.
            _outer_args, outer_kwargs = copytree_mock.call_args_list[0]
            self.assertTrue(outer_kwargs.get("symlinks"), "copytree must be called with symlinks=True so it never silently dereferences a chart-source symlink")

    def test_14_temporary_chart_unexpected_symlink_before_helm_package_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            values_rel = f"envs/{ENVIRONMENT}/{DEPLOYMENT_ID}/values.yaml"
            (repo_root / values_rel).parent.mkdir(parents=True)
            (repo_root / values_rel).write_text("runtime:\n  containerName: goldengate\n")

            real_copy = phase5_runtime.shutil.copy

            def poisoning_copy(src, dst, *args, **kwargs):
                # shutil.copy (used ONLY for values-deployment.yaml, never recursively) is the injection point here -- simulates a symlink appearing in the TEMPORARY copied chart between copytree and helm package (e.g. a race/change), never something the real copytree(symlinks=True) call itself would introduce.
                result = real_copy(src, dst, *args, **kwargs)
                external = Path(tmp) / "external-race-target.yaml"
                external.write_bytes(b"RACE-CONDITION-CONTENT\n")
                try:
                    (Path(dst).parent / "templates" / "race-symlink.yaml").symlink_to(external)
                except (OSError, NotImplementedError):
                    self.skipTest("symlinks are not supported on this platform/filesystem")
                return result

            scripted = ScriptedRun()
            with _mock_repo_root_with_real_chart(repo_root), \
                 mock.patch.object(phase5_runtime, "run", scripted), \
                 mock.patch.object(phase5_runtime.shutil, "copy", side_effect=poisoning_copy):
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._package_runtime_chart(DEPLOYMENT_ID, values_rel, CHART_VERSION)
            self.assertEqual(scripted.calls, [], "helm package must never run against a temporary chart tree that gained a symlink after copytree")

    def test_15_temporary_chart_special_object_before_helm_package_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            values_rel = f"envs/{ENVIRONMENT}/{DEPLOYMENT_ID}/values.yaml"
            (repo_root / values_rel).parent.mkdir(parents=True)
            (repo_root / values_rel).write_text("runtime:\n  containerName: goldengate\n")

            real_copy = phase5_runtime.shutil.copy

            def poisoning_copy(src, dst, *args, **kwargs):
                result = real_copy(src, dst, *args, **kwargs)
                try:
                    os.mkfifo(Path(dst).parent / "templates" / "race-fifo")
                except (AttributeError, OSError, NotImplementedError):
                    self.skipTest("FIFOs are not supported on this platform/filesystem")
                return result

            scripted = ScriptedRun()
            with _mock_repo_root_with_real_chart(repo_root), \
                 mock.patch.object(phase5_runtime, "run", scripted), \
                 mock.patch.object(phase5_runtime.shutil, "copy", side_effect=poisoning_copy):
                with self.assertRaises(phase5_runtime.Phase5Error):
                    phase5_runtime._package_runtime_chart(DEPLOYMENT_ID, values_rel, CHART_VERSION)
            self.assertEqual(scripted.calls, [])

    def test_16_normal_canonical_source_still_packages_successfully(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            values_rel = f"envs/{ENVIRONMENT}/{DEPLOYMENT_ID}/values.yaml"
            (repo_root / values_rel).parent.mkdir(parents=True)
            (repo_root / values_rel).write_text("runtime:\n  containerName: goldengate\n")

            def fake_helm_package(argv, **kwargs):
                dest = Path(argv[argv.index("--destination") + 1])
                dest.mkdir(parents=True, exist_ok=True)
                (dest / f"{phase5_runtime.CHART_NAME}-{CHART_VERSION}.tgz").write_bytes(b"fake")
                return FakeProc(0, "")

            with _mock_repo_root_with_real_chart(repo_root), mock.patch.object(phase5_runtime, "run") as run_mock:
                run_mock.side_effect = lambda argv, **kwargs: fake_helm_package(argv, **kwargs) if argv[:2] == ["helm", "package"] else FakeProc(0, "")
                temp_chart_path, package_path = phase5_runtime._package_runtime_chart(DEPLOYMENT_ID, values_rel, CHART_VERSION)
            self.assertTrue(package_path.is_file())
            self.assertTrue(temp_chart_path.is_dir())
            self.assertFalse((temp_chart_path / "templates" / "runtime-statefulset.yaml").is_symlink())

    def test_17_expected_chart_package_members_still_dynamically_derived(self):
        members = phase5_runtime._expected_chart_package_members()
        expected = {f"goldengate/{name.split('goldengate/', 1)[1]}" for name, _content in _canonical_chart_file_members(CHART_VERSION)}
        expected.add("goldengate/values-deployment.yaml")
        self.assertEqual(members, expected)


if __name__ == "__main__":
    unittest.main()
