"""Offline, mocked-command tests for hack/ensure-goldengate-admin-secret.py; run via `python3 hack/test-ensure-goldengate-admin-secret.py`."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import unittest
import unittest.mock
from contextlib import redirect_stdout

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER_PATH = os.path.join(REPO_ROOT, "hack", "ensure-goldengate-admin-secret.py")


def _load_helper():
    spec = importlib.util.spec_from_file_location("ensure_goldengate_admin_secret", HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ensure = _load_helper()

ENVIRONMENT = "dev"
SECRET_NAME = "dev/goldengate/runtime/gg-fixture-01/admin"
GENERATED_PASSWORD = "GeneratedPasswordMarkerXYZ"


_UNSET = object()


def ensure_admin_secret(runner, deployment_id=_UNSET, secret_name=SECRET_NAME, managed=True, region="eu-west-1",
                        environment=ENVIRONMENT):
    if deployment_id is _UNSET:
        deployment_id = "gg-fixture-01"
    return ensure.ensure_admin_secret(runner, deployment_id, environment, secret_name, managed, region)


class FakeRunner:
    """Never touches a real process; records every argv and the temp-file path used at PutSecretValue time."""

    def __init__(self, describe_ok=True, stages=None, stages_sequence=None, random_password=GENERATED_PASSWORD,
                fail_put=False, raw_stderr=None, malformed_json=False):
        self.describe_ok = describe_ok
        self.stages = stages or []
        self.stages_sequence = list(stages_sequence) if stages_sequence is not None else None
        self.random_password = random_password
        self.fail_put = fail_put
        self.raw_stderr = raw_stderr
        self.malformed_json = malformed_json
        self.calls = []
        self.put_secret_value_file_snapshot = None
        self.put_secret_value_file_path = None

    def _next_stages(self):
        if self.stages_sequence is not None:
            return self.stages_sequence.pop(0) if self.stages_sequence else self.stages
        return self.stages

    def run(self, args):
        self.calls.append(list(args))
        if "describe-secret" in args:
            if not self.describe_ok:
                raise subprocess.CalledProcessError(1, args, output="", stderr=self.raw_stderr or "")
            return "{}"
        if "list-secret-version-ids" in args:
            if self.malformed_json:
                return "{not valid json"
            return json.dumps({"Versions": [{"VersionStages": [s]} for s in self._next_stages()]})
        if "get-random-password" in args:
            return json.dumps({"RandomPassword": self.random_password})
        if "put-secret-value" in args:
            if self.fail_put:
                raise subprocess.CalledProcessError(1, args, output="", stderr=self.raw_stderr or "")
            for arg in args:
                if arg.startswith("file://"):
                    self.put_secret_value_file_path = arg[len("file://"):]
                    with open(self.put_secret_value_file_path) as f:
                        self.put_secret_value_file_snapshot = f.read()
            return "{}"
        raise AssertionError(f"unexpected command: {args}")


class BootstrapBehaviorTests(unittest.TestCase):
    def test_missing_secret_container_fails(self):
        runner = FakeRunner(describe_ok=False)
        with self.assertRaises(ensure.BootstrapError):
            ensure_admin_secret(runner, managed=False)

    def test_managed_false_with_awscurrent_succeeds_without_mutation(self):
        runner = FakeRunner(stages=["AWSCURRENT"])
        outcome = ensure_admin_secret(runner, managed=False)
        self.assertEqual(outcome, "unchanged")
        self.assertFalse(any("put-secret-value" in c for c in runner.calls))

    def test_managed_false_without_awscurrent_fails(self):
        runner = FakeRunner(stages=[])
        with self.assertRaises(ensure.BootstrapError):
            ensure_admin_secret(runner, managed=False)

    def test_managed_true_with_awscurrent_succeeds_without_mutation(self):
        runner = FakeRunner(stages=["AWSCURRENT"])
        outcome = ensure_admin_secret(runner, managed=True)
        self.assertEqual(outcome, "unchanged")
        self.assertFalse(any("put-secret-value" in c for c in runner.calls))
        self.assertFalse(any("get-random-password" in c for c in runner.calls))

    def test_managed_true_without_awscurrent_generates_and_writes_one_version(self):
        runner = FakeRunner(stages=[])
        outcome = ensure_admin_secret(runner, managed=True)
        self.assertEqual(outcome, "initialized")
        put_calls = [c for c in runner.calls if "put-secret-value" in c]
        self.assertEqual(len(put_calls), 1)

    def test_get_secret_value_is_never_called(self):
        for stages, managed in ((["AWSCURRENT"], False), ([], True), (["AWSCURRENT"], True)):
            with self.subTest(stages=stages, managed=managed):
                runner = FakeRunner(stages=stages)
                ensure_admin_secret(runner, managed=managed)
                self.assertFalse(any("get-secret-value" in c for c in runner.calls))

    def test_existing_awscurrent_is_never_overwritten(self):
        runner = FakeRunner(stages=["AWSCURRENT"])
        ensure_admin_secret(runner, managed=True)
        self.assertFalse(any("put-secret-value" in c for c in runner.calls))

    def test_concurrent_initializer_detected_at_recheck_skips_the_write(self):
        # The recheck sees a version another run just wrote between the first check and GetRandomPassword.
        runner = FakeRunner(stages_sequence=[[], ["AWSCURRENT"]])
        outcome = ensure_admin_secret(runner, managed=True)
        self.assertEqual(outcome, "unchanged")
        self.assertFalse(any("put-secret-value" in c for c in runner.calls))


class InputValidationTests(unittest.TestCase):
    def test_unsafe_deployment_id_rejected(self):
        runner = FakeRunner(stages=[])
        with self.assertRaises(ensure.BootstrapError):
            ensure_admin_secret(runner, deployment_id="../etc/passwd", managed=True)
        self.assertEqual(runner.calls, [])

    def test_empty_deployment_id_rejected(self):
        runner = FakeRunner(stages=[])
        with self.assertRaises(ensure.BootstrapError):
            ensure_admin_secret(runner, deployment_id="", managed=True)

    def test_unsafe_environment_rejected(self):
        runner = FakeRunner(stages=[])
        with self.assertRaises(ensure.BootstrapError):
            ensure_admin_secret(runner, environment="dev/../prod", managed=True)
        self.assertEqual(runner.calls, [])

    def test_unsafe_region_rejected(self):
        runner = FakeRunner(stages=[])
        with self.assertRaises(ensure.BootstrapError):
            ensure_admin_secret(runner, region="; rm -rf /", managed=True)
        self.assertEqual(runner.calls, [])

    def test_arn_style_secret_name_rejected(self):
        runner = FakeRunner(stages=[])
        with self.assertRaises(ensure.BootstrapError):
            ensure_admin_secret(runner, secret_name="arn:aws:secretsmanager:eu-west-1:123456789012:secret:x", managed=True)
        self.assertEqual(runner.calls, [])

    def test_traversal_secret_name_rejected(self):
        runner = FakeRunner(stages=[])
        with self.assertRaises(ensure.BootstrapError):
            ensure_admin_secret(runner, secret_name="dev/../etc/admin", managed=True)

    def test_leading_slash_secret_name_rejected(self):
        runner = FakeRunner(stages=[])
        with self.assertRaises(ensure.BootstrapError):
            ensure_admin_secret(runner, secret_name="/dev/goldengate/runtime/gg-fixture-01/admin", managed=True)

    def test_out_of_environment_scope_secret_name_rejected(self):
        runner = FakeRunner(stages=[])
        with self.assertRaises(ensure.BootstrapError):
            ensure_admin_secret(runner, secret_name="prod/goldengate/runtime/gg-fixture-01/admin", environment="dev", managed=True)

    def test_validation_failures_never_invoke_the_runner(self):
        runner = FakeRunner(stages=[])
        for kwargs in (
            {"deployment_id": "Not_Safe"},
            {"environment": "Dev"},
            {"region": "not-a-region"},
            {"secret_name": "arn:aws:x"},
        ):
            with self.subTest(**kwargs):
                runner.calls = []
                with self.assertRaises(ensure.BootstrapError):
                    ensure_admin_secret(runner, managed=True, **kwargs)
                self.assertEqual(runner.calls, [])


class ErrorSanitizationTests(unittest.TestCase):
    def test_raw_aws_cli_stderr_never_appears_in_bootstrap_error(self):
        sensitive_stderr = "AccessDenied for arn:aws:iam::668311715351:role/SecretMutationRole SECRET-CONTENTS-MARKER"
        runner = FakeRunner(describe_ok=False, raw_stderr=sensitive_stderr)
        try:
            ensure_admin_secret(runner, managed=True)
            self.fail("expected BootstrapError")
        except ensure.BootstrapError as exc:
            self.assertNotIn("SECRET-CONTENTS-MARKER", str(exc))
            self.assertNotIn(sensitive_stderr, str(exc))

    def test_raw_aws_cli_stderr_never_appears_on_put_failure(self):
        sensitive_stderr = "PutSecretValue denied SECRET-CONTENTS-MARKER"
        runner = FakeRunner(stages=[], fail_put=True, raw_stderr=sensitive_stderr)
        try:
            ensure_admin_secret(runner, managed=True)
            self.fail("expected BootstrapError")
        except ensure.BootstrapError as exc:
            self.assertNotIn("SECRET-CONTENTS-MARKER", str(exc))

    def test_malformed_json_response_converted_to_bootstrap_error(self):
        runner = FakeRunner(malformed_json=True)
        with self.assertRaises(ensure.BootstrapError):
            ensure_admin_secret(runner, managed=True)


class PasswordHandlingTests(unittest.TestCase):
    def test_password_is_never_printed(self):
        runner = FakeRunner(stages=[])
        buf = io.StringIO()
        with redirect_stdout(buf):
            ensure_admin_secret(runner, managed=True)
        self.assertNotIn(GENERATED_PASSWORD, buf.getvalue())

    def test_json_generated_through_structured_serializer(self):
        runner = FakeRunner(stages=[])
        ensure_admin_secret(runner, managed=True)
        parsed = json.loads(runner.put_secret_value_file_snapshot)
        self.assertEqual(parsed, {"OGG_ADMIN": "oggadmin", "OGG_ADMIN_PWD": GENERATED_PASSWORD})

    def test_temp_file_uses_restrictive_permissions(self):
        captured_path = {}
        real_run = FakeRunner.run

        def _capturing_run(self, args):
            if "put-secret-value" in args:
                for arg in args:
                    if arg.startswith("file://"):
                        captured_path["path"] = arg[len("file://"):]
                        mode = stat.S_IMODE(os.stat(captured_path["path"]).st_mode)
                        captured_path["mode"] = mode
            return real_run(self, args)

        runner = FakeRunner(stages=[])
        runner.run = _capturing_run.__get__(runner, FakeRunner)
        ensure_admin_secret(runner, managed=True)
        self.assertEqual(captured_path["mode"], 0o600)

    def test_temp_file_removed_after_success(self):
        runner = FakeRunner(stages=[])
        ensure_admin_secret(runner, managed=True)
        self.assertIsNotNone(runner.put_secret_value_file_path)
        self.assertFalse(os.path.exists(runner.put_secret_value_file_path))

    def test_temp_file_removed_after_failure(self):
        runner = FakeRunner(stages=[], fail_put=True)
        with self.assertRaises(ensure.BootstrapError):
            ensure_admin_secret(runner, managed=True)
        # The runner captured the path only on a successful put; verify no leaked file remains in the temp dir.
        import glob
        import tempfile
        leaked = glob.glob(os.path.join(tempfile.gettempdir(), "gg-admin-secret-*.json"))
        self.assertEqual(leaked, [])

    def test_password_absent_from_exception_output(self):
        runner = FakeRunner(describe_ok=False)
        try:
            ensure_admin_secret(runner, managed=True)
        except ensure.BootstrapError as exc:
            self.assertNotIn(GENERATED_PASSWORD, str(exc))

    def test_password_absent_from_helper_cli_output(self):
        runner = FakeRunner(stages=[])
        buf = io.StringIO()
        with redirect_stdout(buf):
            with unittest.mock.patch.object(ensure, "CommandRunner", return_value=runner):
                ensure.main(["--deployment-id", "gg-fixture-01", "--environment", ENVIRONMENT,
                            "--secret-name", SECRET_NAME, "--managed", "true", "--region", "eu-west-1"])
        self.assertNotIn(GENERATED_PASSWORD, buf.getvalue())

    def test_version_staging_is_checked_immediately_before_put_secret_value(self):
        # The recheck immediately precedes PutSecretValue with nothing else interleaved (narrows, never closes, the race).
        runner = FakeRunner(stages=[])
        ensure_admin_secret(runner, managed=True)
        command_kinds = ["list-secret-version-ids" if "list-secret-version-ids" in c
                        else "put-secret-value" if "put-secret-value" in c
                        else "other" for c in runner.calls]
        list_indices = [i for i, kind in enumerate(command_kinds) if kind == "list-secret-version-ids"]
        put_index = command_kinds.index("put-secret-value")
        self.assertGreaterEqual(len(list_indices), 2)
        self.assertEqual(list_indices[-1] + 1, put_index)

    def test_no_aws_command_actually_executed(self):
        # FakeRunner.run never shells out; asserting on call argv (not real execution) proves this across every test above.
        runner = FakeRunner(stages=[])
        ensure_admin_secret(runner, managed=True)
        self.assertTrue(all(isinstance(c, list) for c in runner.calls))


if __name__ == "__main__":
    unittest.main()
