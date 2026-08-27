"""Offline tests for automation/phases/phase4/phase4_shared_secrets.py; run directly via `python3 automation/phases/phase4/tests/test_phase4_shared_secrets.py`. No live AWS -- every subprocess call is intercepted via a scripted fake. Covers the two-role assume-role chain, the mandatory workload-account identity check BEFORE any Secrets Manager call, the canonical-secret-name delegation to automation/goldengate-deployment-model.py, the DescribeSecret/ListSecretVersionIds-only contract, and the AWSCURRENT requirement."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[4]
TOOL_PATH = REPO_ROOT / "automation" / "phases" / "phase4" / "phase4_shared_secrets.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("phase4_shared_secrets", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


phase4_shared_secrets = _load_tool()

ENVIRONMENT = "dev"
WORKLOAD_ACCOUNT_ID = "668311715351"
EKS_DEPLOY_ROLE_ARN = f"arn:aws:iam::{WORKLOAD_ACCOUNT_ID}:role/GoldenGateEksDeployRole-dev"
SECRET_NAMES = [
    "goldengate/dev/oracle-source-admin",
    "goldengate/dev/oracle-target-admin",
    "goldengate/dev/tls-bundle",
]


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class ScriptedRun:
    def __init__(self, default=None):
        self.rules = []
        self.calls = []
        self.default = default if default is not None else FakeProc(0, "", "")

    def when(self, predicate, proc):
        self.rules.append((predicate, proc))
        return self

    def __call__(self, argv, env=None, check=True):
        self.calls.append({"argv": list(argv), "env": env})
        for predicate, proc in reversed(self.rules):
            if predicate(argv):
                if check and proc.returncode != 0:
                    raise phase4_shared_secrets.Phase4Error(f"{' '.join(str(a) for a in argv)} failed: {proc.stderr}")
                return proc
        if check and self.default.returncode != 0:
            raise phase4_shared_secrets.Phase4Error(f"{' '.join(str(a) for a in argv)} failed")
        return self.default


def _starts_with(*prefix):
    return lambda argv: list(argv[:len(prefix)]) == list(prefix)


def _run_quiet(func, *args, **kwargs):
    with redirect_stdout(io.StringIO()):
        return func(*args, **kwargs)


def _assume_role_response(account_id="000000000000"):
    return FakeProc(0, json.dumps({"Credentials": {"AccessKeyId": "AKIAFAKE", "SecretAccessKey": "fake-secret", "SessionToken": "fake-session-token"}}))


class SafeTokenTests(unittest.TestCase):
    def test_unsafe_environment_rejected(self):
        with self.assertRaises(phase4_shared_secrets.Phase4Error):
            phase4_shared_secrets.require_environment_arg("dev; rm -rf /")

    def test_safe_environment_accepted(self):
        self.assertEqual(phase4_shared_secrets.require_environment_arg("dev"), "dev")


class WorkloadAccountExtractionTests(unittest.TestCase):
    def test_malformed_role_arn_fails(self):
        with self.assertRaises(phase4_shared_secrets.Phase4Error):
            phase4_shared_secrets.parse_expected_workload_account("not-an-arn")

    def test_non_iam_role_arn_fails(self):
        with self.assertRaises(phase4_shared_secrets.Phase4Error):
            phase4_shared_secrets.parse_expected_workload_account("arn:aws:s3:::some-bucket")

    def test_exact_account_extraction(self):
        self.assertEqual(phase4_shared_secrets.parse_expected_workload_account(EKS_DEPLOY_ROLE_ARN), WORKLOAD_ACCOUNT_ID)


class AssumeRoleTests(unittest.TestCase):
    def test_assume_role_failure_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "assume-role"), FakeProc(1, "", "AccessDenied"))
        with mock.patch.object(phase4_shared_secrets, "run", scripted):
            with self.assertRaises(phase4_shared_secrets.Phase4Error):
                _run_quiet(phase4_shared_secrets.assume_eks_deploy_role, EKS_DEPLOY_ROLE_ARN, "123", "1", dict(os.environ))

    def test_assume_role_malformed_response_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "assume-role"), FakeProc(0, "not json"))
        with mock.patch.object(phase4_shared_secrets, "run", scripted):
            with self.assertRaises(phase4_shared_secrets.Phase4Error):
                _run_quiet(phase4_shared_secrets.assume_eks_deploy_role, EKS_DEPLOY_ROLE_ARN, "123", "1", dict(os.environ))

    def test_assume_role_success_returns_isolated_env(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "assume-role"), _assume_role_response())
        base_env = dict(os.environ)
        base_env["AWS_ACCESS_KEY_ID"] = "SOURCE_KEY"
        with mock.patch.object(phase4_shared_secrets, "run", scripted):
            assumed_env = _run_quiet(phase4_shared_secrets.assume_eks_deploy_role, EKS_DEPLOY_ROLE_ARN, "123", "1", base_env)
        self.assertEqual(assumed_env["AWS_ACCESS_KEY_ID"], "AKIAFAKE")
        self.assertEqual(base_env["AWS_ACCESS_KEY_ID"], "SOURCE_KEY")  # source-account env dict is never mutated in place

    def test_assume_role_session_name_includes_run_context(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "assume-role"), _assume_role_response())
        with mock.patch.object(phase4_shared_secrets, "run", scripted):
            _run_quiet(phase4_shared_secrets.assume_eks_deploy_role, EKS_DEPLOY_ROLE_ARN, "999", "2", dict(os.environ))
        call = next(c for c in scripted.calls if c["argv"][:3] == ["aws", "sts", "assume-role"])
        session_name = call["argv"][call["argv"].index("--role-session-name") + 1]
        self.assertIn("999", session_name)
        self.assertIn("2", session_name)


class VerifyAssumedIdentityTests(unittest.TestCase):
    def test_wrong_account_fails_before_secrets_manager(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "get-caller-identity"), FakeProc(0, "999999999999\n"))
        scripted.when(_starts_with("aws", "secretsmanager"), FakeProc(0, "{}"))
        with mock.patch.object(phase4_shared_secrets, "run", scripted):
            with self.assertRaises(phase4_shared_secrets.Phase4Error):
                _run_quiet(phase4_shared_secrets.verify_assumed_identity, {}, WORKLOAD_ACCOUNT_ID)
        secretsmanager_calls = [c for c in scripted.calls if c["argv"][:2] == ["aws", "secretsmanager"]]
        self.assertEqual(secretsmanager_calls, [], "Secrets Manager must never be called before identity verification succeeds.")

    def test_correct_account_passes(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "get-caller-identity"), FakeProc(0, WORKLOAD_ACCOUNT_ID + "\n"))
        with mock.patch.object(phase4_shared_secrets, "run", scripted):
            _run_quiet(phase4_shared_secrets.verify_assumed_identity, {}, WORKLOAD_ACCOUNT_ID)

    def test_get_caller_identity_failure_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "get-caller-identity"), FakeProc(1, "", "ExpiredToken"))
        with mock.patch.object(phase4_shared_secrets, "run", scripted):
            with self.assertRaises(phase4_shared_secrets.Phase4Error):
                _run_quiet(phase4_shared_secrets.verify_assumed_identity, {}, WORKLOAD_ACCOUNT_ID)

    def test_uses_assumed_env_not_ambient_env(self):
        scripted = ScriptedRun()
        captured_envs = []

        def fake_run(argv, env=None, check=True):
            captured_envs.append(env)
            return FakeProc(0, WORKLOAD_ACCOUNT_ID + "\n")

        assumed_env = {"AWS_ACCESS_KEY_ID": "ASSUMED"}
        with mock.patch.object(phase4_shared_secrets, "run", fake_run):
            _run_quiet(phase4_shared_secrets.verify_assumed_identity, assumed_env, WORKLOAD_ACCOUNT_ID)
        self.assertIs(captured_envs[0], assumed_env)


class CanonicalSecretNamesTests(unittest.TestCase):
    def test_retrieved_through_deployment_model_tool(self):
        scripted = ScriptedRun()
        scripted.when(lambda argv: str(phase4_shared_secrets.DEPLOYMENT_MODEL_TOOL) in argv, FakeProc(0, "\n".join(SECRET_NAMES) + "\n"))
        with mock.patch.object(phase4_shared_secrets, "run", scripted):
            names = _run_quiet(phase4_shared_secrets.canonical_secret_names, ENVIRONMENT)
        self.assertEqual(names, SECRET_NAMES)
        call = next(c for c in scripted.calls if str(phase4_shared_secrets.DEPLOYMENT_MODEL_TOOL) in c["argv"])
        self.assertIn("--environment", call["argv"])
        self.assertIn("shared-secrets", call["argv"])

    def test_expects_exactly_three_unique_secrets(self):
        scripted = ScriptedRun()
        scripted.when(lambda argv: str(phase4_shared_secrets.DEPLOYMENT_MODEL_TOOL) in argv, FakeProc(0, "\n".join(SECRET_NAMES[:2]) + "\n"))
        with mock.patch.object(phase4_shared_secrets, "run", scripted):
            with self.assertRaises(phase4_shared_secrets.Phase4Error):
                _run_quiet(phase4_shared_secrets.canonical_secret_names, ENVIRONMENT)

    def test_duplicate_secret_names_fail(self):
        scripted = ScriptedRun()
        scripted.when(lambda argv: str(phase4_shared_secrets.DEPLOYMENT_MODEL_TOOL) in argv, FakeProc(0, "\n".join(SECRET_NAMES[:2] + [SECRET_NAMES[0]]) + "\n"))
        with mock.patch.object(phase4_shared_secrets, "run", scripted):
            with self.assertRaises(phase4_shared_secrets.Phase4Error):
                _run_quiet(phase4_shared_secrets.canonical_secret_names, ENVIRONMENT)

    def test_empty_output_fails(self):
        scripted = ScriptedRun()
        scripted.when(lambda argv: str(phase4_shared_secrets.DEPLOYMENT_MODEL_TOOL) in argv, FakeProc(0, ""))
        with mock.patch.object(phase4_shared_secrets, "run", scripted):
            with self.assertRaises(phase4_shared_secrets.Phase4Error):
                _run_quiet(phase4_shared_secrets.canonical_secret_names, ENVIRONMENT)


class SecretVersionValidationTests(unittest.TestCase):
    def test_missing_secret_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "secretsmanager", "describe-secret"), FakeProc(1, "", "ResourceNotFoundException"))
        with mock.patch.object(phase4_shared_secrets, "run", scripted):
            with self.assertRaises(phase4_shared_secrets.Phase4Error):
                _run_quiet(phase4_shared_secrets.verify_secret_has_current_version, SECRET_NAMES[0], {})

    def test_access_denied_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "secretsmanager", "describe-secret"), FakeProc(1, "", "AccessDeniedException"))
        with mock.patch.object(phase4_shared_secrets, "run", scripted):
            with self.assertRaises(phase4_shared_secrets.Phase4Error):
                _run_quiet(phase4_shared_secrets.verify_secret_has_current_version, SECRET_NAMES[0], {})

    def test_no_awscurrent_version_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "secretsmanager", "describe-secret"), FakeProc(0, "{}"))
        scripted.when(_starts_with("aws", "secretsmanager", "list-secret-version-ids"), FakeProc(0, json.dumps({"Versions": [{"VersionStages": ["AWSPREVIOUS"]}]})))
        with mock.patch.object(phase4_shared_secrets, "run", scripted):
            with self.assertRaises(phase4_shared_secrets.Phase4Error):
                _run_quiet(phase4_shared_secrets.verify_secret_has_current_version, SECRET_NAMES[0], {})

    def test_awscurrent_present_passes(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "secretsmanager", "describe-secret"), FakeProc(0, "{}"))
        scripted.when(_starts_with("aws", "secretsmanager", "list-secret-version-ids"), FakeProc(0, json.dumps({"Versions": [{"VersionStages": ["AWSCURRENT"]}]})))
        with mock.patch.object(phase4_shared_secrets, "run", scripted):
            _run_quiet(phase4_shared_secrets.verify_secret_has_current_version, SECRET_NAMES[0], {})

    def test_getsecretvalue_never_invoked(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "secretsmanager", "describe-secret"), FakeProc(0, "{}"))
        scripted.when(_starts_with("aws", "secretsmanager", "list-secret-version-ids"), FakeProc(0, json.dumps({"Versions": [{"VersionStages": ["AWSCURRENT"]}]})))
        with mock.patch.object(phase4_shared_secrets, "run", scripted):
            _run_quiet(phase4_shared_secrets.verify_secret_has_current_version, SECRET_NAMES[0], {})
        for call in scripted.calls:
            self.assertNotIn("get-secret-value", call["argv"])

    def test_uses_only_allow_listed_api_actions(self):
        # "GetSecretValue" may appear in comments documenting what is never called; only actual run([...]) argvs matter.
        source = TOOL_PATH.read_text()
        run_call_argvs = re.findall(r'run\(\[([^\]]*)\]', source)
        for argv_text in run_call_argvs:
            self.assertNotIn("get-secret-value", argv_text)


class FullValidateFlowTests(unittest.TestCase):
    def _base_scripted(self, secret_names=None):
        secret_names = secret_names if secret_names is not None else SECRET_NAMES
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "assume-role"), _assume_role_response())
        scripted.when(_starts_with("aws", "sts", "get-caller-identity"), FakeProc(0, WORKLOAD_ACCOUNT_ID + "\n"))
        scripted.when(lambda argv: str(phase4_shared_secrets.DEPLOYMENT_MODEL_TOOL) in argv, FakeProc(0, "\n".join(secret_names) + "\n"))
        scripted.when(_starts_with("aws", "secretsmanager", "describe-secret"), FakeProc(0, "{}"))
        scripted.when(_starts_with("aws", "secretsmanager", "list-secret-version-ids"), FakeProc(0, json.dumps({"Versions": [{"VersionStages": ["AWSCURRENT"]}]})))
        return scripted

    def _env(self):
        return mock.patch.dict(os.environ, {"EKS_DEPLOY_ROLE_ARN": EKS_DEPLOY_ROLE_ARN, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}, clear=False)

    def _args(self):
        class Args:
            environment = ENVIRONMENT
        return Args()

    def test_all_three_pass_end_to_end(self):
        scripted = self._base_scripted()
        with mock.patch.object(phase4_shared_secrets, "run", scripted), self._env():
            _run_quiet(phase4_shared_secrets.cmd_validate, self._args())

    def test_malformed_role_arn_fails_before_anything_else(self):
        scripted = self._base_scripted()
        with mock.patch.object(phase4_shared_secrets, "run", scripted), mock.patch.dict(os.environ, {"EKS_DEPLOY_ROLE_ARN": "not-an-arn"}, clear=False):
            with self.assertRaises(phase4_shared_secrets.Phase4Error):
                _run_quiet(phase4_shared_secrets.cmd_validate, self._args())
        self.assertEqual(scripted.calls, [])

    def test_unsafe_environment_fails(self):
        args = self._args()
        args.environment = "dev; rm -rf /"
        with self._env():
            with self.assertRaises(phase4_shared_secrets.Phase4Error):
                _run_quiet(phase4_shared_secrets.cmd_validate, args)

    def test_missing_secret_fails_end_to_end(self):
        scripted = self._base_scripted()
        scripted.when(_starts_with("aws", "secretsmanager", "describe-secret"), FakeProc(1, "", "ResourceNotFoundException"))
        with mock.patch.object(phase4_shared_secrets, "run", scripted), self._env():
            with self.assertRaises(phase4_shared_secrets.Phase4Error):
                _run_quiet(phase4_shared_secrets.cmd_validate, self._args())

    def test_wrong_assumed_account_fails_before_secrets_manager_end_to_end(self):
        scripted = self._base_scripted()
        scripted.when(_starts_with("aws", "sts", "get-caller-identity"), FakeProc(0, "111111111111\n"))
        with mock.patch.object(phase4_shared_secrets, "run", scripted), self._env():
            with self.assertRaises(phase4_shared_secrets.Phase4Error):
                _run_quiet(phase4_shared_secrets.cmd_validate, self._args())
        secretsmanager_calls = [c for c in scripted.calls if c["argv"][:2] == ["aws", "secretsmanager"]]
        self.assertEqual(secretsmanager_calls, [])

    def test_no_credentials_written_to_github_env_or_output(self):
        scripted = self._base_scripted()
        with tempfile_env() as (env_path, output_path):
            with mock.patch.object(phase4_shared_secrets, "run", scripted), mock.patch.dict(
                os.environ, {"EKS_DEPLOY_ROLE_ARN": EKS_DEPLOY_ROLE_ARN, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_ENV": str(env_path), "GITHUB_OUTPUT": str(output_path)}, clear=False,
            ):
                _run_quiet(phase4_shared_secrets.cmd_validate, self._args())
            self.assertEqual(env_path.read_text(), "")
            self.assertEqual(output_path.read_text(), "")

    def test_no_secret_value_ever_read_or_logged(self):
        scripted = self._base_scripted()
        buf = io.StringIO()
        with mock.patch.object(phase4_shared_secrets, "run", scripted), self._env(), redirect_stdout(buf):
            phase4_shared_secrets.cmd_validate(self._args())
        self.assertNotIn("AKIAFAKE", buf.getvalue())
        self.assertNotIn("fake-secret", buf.getvalue())
        self.assertNotIn("fake-session-token", buf.getvalue())


import tempfile
from contextlib import contextmanager


@contextmanager
def tempfile_env():
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / "env"
        output_path = Path(tmp) / "output"
        env_path.write_text("")
        output_path.write_text("")
        yield env_path, output_path


if __name__ == "__main__":
    unittest.main()
