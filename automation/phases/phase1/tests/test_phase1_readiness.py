"""Offline unit tests for automation/phases/phase1/phase1_readiness.py; run directly via `python3 automation/phases/phase1/tests/test_phase1_readiness.py`. Every aws/kubectl/curl call is scripted through a fake subprocess.run -- this suite never touches live AWS, EKS, or Kubernetes."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[4]
TOOL_PATH = REPO_ROOT / "automation" / "phases" / "phase1" / "phase1_readiness.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("phase1_readiness", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


p1 = _load_tool()

WORKLOAD_ACCOUNT_ID = "123456789012"
EKS_DEPLOY_ROLE_ARN = f"arn:aws:iam::{WORKLOAD_ACCOUNT_ID}:role/GoldenGateEKSDeployRole-dev"


class ScriptedSubprocess:
    """A stand-in for subprocess.run: dispatches on argv to a registered handler, records every call, and raises AssertionError on any unscripted invocation."""

    def __init__(self):
        self.calls = []
        self._handlers = []

    def on(self, predicate, handler):
        self._handlers.append((predicate, handler))
        return self

    def __call__(self, argv, cwd=None, env=None, capture_output=True, text=True):
        self.calls.append(argv)
        for predicate, handler in self._handlers:
            if predicate(argv):
                return handler(argv, env)
        raise AssertionError(f"unscripted subprocess call: {argv!r}")


def _ok(stdout=""):
    return lambda argv, env: subprocess.CompletedProcess(argv, 0, stdout, "")


def _fail(stdout="", stderr="", returncode=1):
    return lambda argv, env: subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _assume_role_ok(access_key="FAKE_ACCESS_KEY_ID", secret_key="FAKE_SECRET_ACCESS_KEY", session_token="FAKE_SESSION_TOKEN"):
    creds = {"Credentials": {"AccessKeyId": access_key, "SecretAccessKey": secret_key, "SessionToken": session_token}}
    return _ok(json.dumps(creds))


def _parse_github_special_file(path):
    """Parses a GITHUB_OUTPUT/GITHUB_ENV-shaped file, heredoc form included, using splitlines() -- which (unlike plain "\\n"-only splitting) recognizes LF, CRLF, and bare CR as line breaks, exactly like GitHub's own line-oriented special-file parser. Using anything narrower here would hide the bare-CR injection this suite specifically guards against."""
    if not Path(path).exists():
        return {}
    text = Path(path).read_text(encoding="utf-8")
    lines = text.splitlines()
    pairs = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if "<<" in line:
            key, delimiter = line.split("<<", 1)
            i += 1
            buf = []
            while i < len(lines) and lines[i] != delimiter:
                buf.append(lines[i])
                i += 1
            pairs[key] = "\n".join(buf)
        elif "=" in line:
            key, _, value = line.partition("=")
            pairs[key] = value
        i += 1
    return pairs


class Phase1TestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp_path = Path(self._tmp.name)
        self.state_path = tmp_path / "state.json"
        self.github_output = tmp_path / "github_output.txt"
        self.github_env = tmp_path / "github_env.txt"
        self.github_summary = tmp_path / "github_summary.txt"
        self.args = SimpleNamespace(state_path=self.state_path)
        env_patch = {
            "GITHUB_OUTPUT": str(self.github_output),
            "GITHUB_ENV": str(self.github_env),
            "GITHUB_STEP_SUMMARY": str(self.github_summary),
        }
        patcher = mock.patch.dict(os.environ, env_patch)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_subcommand(self, cmd_func, scripted=None, env_overrides=None):
        env_overrides = env_overrides or {}
        with mock.patch.dict(os.environ, env_overrides):
            if scripted is not None:
                with mock.patch.object(p1.subprocess, "run", scripted):
                    cmd_func(self.args)
            else:
                cmd_func(self.args)

    def read_state(self):
        return p1.load_state(self.state_path)

    def read_outputs(self):
        return _parse_github_special_file(self.github_output)

    def read_env(self):
        return _parse_github_special_file(self.github_env)


class TestEnvironmentResolution(Phase1TestCase):
    def test_workflow_dispatch_selects_the_requested_environment(self):
        self.run_subcommand(p1.cmd_resolve_environment, env_overrides={"EVENT_NAME": "workflow_dispatch", "INPUT_ENVIRONMENT": "sit"})
        self.assertEqual(self.read_state()["selected_environment"], "sit")
        self.assertEqual(self.read_outputs()["environment"], "sit")

    def test_push_event_always_selects_dev(self):
        self.run_subcommand(p1.cmd_resolve_environment, env_overrides={"EVENT_NAME": "push", "INPUT_ENVIRONMENT": ""})
        self.assertEqual(self.read_state()["selected_environment"], "dev")
        self.assertEqual(self.read_outputs()["environment"], "dev")


class TestEffectiveDeploy(Phase1TestCase):
    def test_action_deploy_yields_true(self):
        self.run_subcommand(p1.cmd_effective_deploy, env_overrides={"EVENT_NAME": "workflow_dispatch", "INPUT_ACTION": "deploy"})
        self.assertEqual(self.read_state()["effective_deploy"], "true")
        self.assertEqual(self.read_outputs()["effective_deploy"], "true")

    def test_action_validate_yields_false(self):
        self.run_subcommand(p1.cmd_effective_deploy, env_overrides={"EVENT_NAME": "workflow_dispatch", "INPUT_ACTION": "validate"})
        self.assertEqual(self.read_state()["effective_deploy"], "false")
        self.assertEqual(self.read_outputs()["effective_deploy"], "false")

    def test_unsupported_action_fails_closed(self):
        with self.assertRaises(p1.Phase1Error):
            self.run_subcommand(p1.cmd_effective_deploy, env_overrides={"EVENT_NAME": "workflow_dispatch", "INPUT_ACTION": "teardown"})
        self.assertNotIn("effective_deploy", self.read_state())


class TestTerraformGovernance(Phase1TestCase):
    def test_override_false_is_accepted(self):
        self.run_subcommand(p1.cmd_terraform_governance, env_overrides={"EVENT_NAME": "workflow_dispatch", "INPUT_OVERRIDE": "false", "INPUT_REASON": ""})
        state = self.read_state()
        self.assertEqual(state["terraform_governance_override"], "false")
        self.assertEqual(state["terraform_governance_override_reason"], "")

    def test_override_true_with_reason_is_accepted(self):
        self.run_subcommand(p1.cmd_terraform_governance, env_overrides={"EVENT_NAME": "workflow_dispatch", "INPUT_OVERRIDE": "true", "INPUT_REASON": "INC-4821 approved by on-call"})
        state = self.read_state()
        self.assertEqual(state["terraform_governance_override"], "true")
        self.assertEqual(state["terraform_governance_override_reason"], "INC-4821 approved by on-call")

    def test_override_true_with_empty_reason_is_rejected(self):
        with self.assertRaises(p1.Phase1Error):
            self.run_subcommand(p1.cmd_terraform_governance, env_overrides={"EVENT_NAME": "workflow_dispatch", "INPUT_OVERRIDE": "true", "INPUT_REASON": "   "})
        self.assertNotIn("terraform_governance_override", self.read_state())


class TestActiveRuntimeState(Phase1TestCase):
    REGISTRY_YAML = "deployments:\n  - name: gg-mysql-billing-02\n  - name: gg-postgresql-orders-01\n"
    EMPTY_REGISTRY_YAML = "deployments: []\n"

    def _scripted_registry(self, registry_yaml):
        def write_registry(argv, env):
            output_path = argv[argv.index("--output") + 1]
            with open(output_path, "w") as f:
                f.write(registry_yaml)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return ScriptedSubprocess().on(lambda argv: "registry" in argv, write_registry)

    def test_two_active_deployments_produce_the_exact_sorted_matrix_shape(self):
        p1.update_state(self.state_path, {"selected_environment": "dev"})
        self.run_subcommand(p1.cmd_active_runtime_state, scripted=self._scripted_registry(self.REGISTRY_YAML))
        state = self.read_state()
        self.assertEqual(state["has_active_deployments"], "true")
        matrix = json.loads(state["active_runtime_matrix"])
        self.assertEqual(matrix, [
            {"environment": "dev", "deployment_id": "gg-mysql-billing-02"},
            {"environment": "dev", "deployment_id": "gg-postgresql-orders-01"},
        ])

    def test_zero_active_deployments_produce_an_empty_matrix(self):
        p1.update_state(self.state_path, {"selected_environment": "dev"})
        self.run_subcommand(p1.cmd_active_runtime_state, scripted=self._scripted_registry(self.EMPTY_REGISTRY_YAML))
        state = self.read_state()
        self.assertEqual(state["has_active_deployments"], "false")
        self.assertEqual(json.loads(state["active_runtime_matrix"]), [])


class TestBooleanAndArrayValidation(unittest.TestCase):
    def test_malformed_booleans_are_rejected(self):
        for bad in ("TRUE", "False", "yes", "1", "0", "", "true ", " false", None):
            with self.assertRaises(p1.Phase1Error, msg=repr(bad)):
                p1.require_literal_bool("x", bad)

    def test_literal_booleans_are_accepted(self):
        self.assertEqual(p1.require_literal_bool("x", "true"), "true")
        self.assertEqual(p1.require_literal_bool("x", "false"), "false")

    def test_malformed_json_arrays_are_rejected(self):
        for bad in ("not json", "{}", '{"a": 1}', "42", "null", None):
            with self.assertRaises(p1.Phase1Error, msg=repr(bad)):
                p1.require_json_array("x", bad)

    def test_valid_json_arrays_are_accepted(self):
        self.assertEqual(p1.require_json_array("x", "[]"), [])
        self.assertEqual(p1.require_json_array("x", '[{"a": 1}]'), [{"a": 1}])


class TestDetectDeployments(Phase1TestCase):
    def test_detector_subprocess_uses_the_canonical_phase1_script_path(self):
        p1.update_state(self.state_path, {"effective_deploy": "true"})

        def run_detector(argv, env):
            with open(env["GITHUB_OUTPUT"], "w") as f:
                f.write("has_changes=true\n")
                f.write('deployment_matrix=[{"environment": "dev", "deployment_id": "gg-x"}]\n')
                f.write("has_deletions=false\n")
                f.write("deletion_matrix=[]\n")
                f.write("has_storage_transition_violations=false\n")
                f.write("storage_transition_violations=[]\n")
            return subprocess.CompletedProcess(argv, 0, "", "")

        scripted = ScriptedSubprocess().on(lambda argv: argv[0] == "bash", run_detector)
        self.run_subcommand(p1.cmd_detect_deployments, scripted=scripted)

        self.assertEqual(len(scripted.calls), 1)
        self.assertEqual(scripted.calls[0], ["bash", str(p1.DETECT_SCRIPT)])
        self.assertTrue(str(p1.DETECT_SCRIPT).endswith(str(Path("automation") / "phases" / "phase1" / "detect-goldengate-deployments.sh")))
        self.assertEqual(self.read_state()["has_changes"], "true")


class TestManagedEfsInventory(Phase1TestCase):
    def _base_env(self):
        return {"AWS_REGION": "eu-west-1", "EKS_DEPLOY_ROLE_ARN": EKS_DEPLOY_ROLE_ARN}

    def _scripted(self, guard_handler):
        return (
            ScriptedSubprocess()
            .on(lambda argv: "managed-efs-inventory" in argv, _ok("[]"))
            .on(lambda argv: argv[:3] == ["aws", "sts", "assume-role"], _assume_role_ok())
            .on(lambda argv: argv[:3] == ["aws", "sts", "get-caller-identity"], _ok(WORKLOAD_ACCOUNT_ID + "\n"))
            .on(lambda argv: argv[:3] == ["aws", "efs", "describe-file-systems"], _ok(json.dumps({"FileSystems": []})))
            .on(lambda argv: str(p1.EFS_INVENTORY_GUARD_TOOL) in argv, guard_handler)
        )

    def test_guard_tool_invocation_uses_the_canonical_phase1_utility_path(self):
        p1.update_state(self.state_path, {"effective_deploy": "true", "selected_environment": "dev"})
        scripted = self._scripted(_ok("OK: no orphan detected."))
        self.run_subcommand(p1.cmd_managed_efs_inventory, scripted=scripted, env_overrides=self._base_env())

        guard_calls = [c for c in scripted.calls if str(p1.EFS_INVENTORY_GUARD_TOOL) in c]
        self.assertEqual(len(guard_calls), 1)
        self.assertEqual(guard_calls[0][0], sys.executable)
        self.assertEqual(guard_calls[0][1], str(p1.EFS_INVENTORY_GUARD_TOOL))
        self.assertTrue(str(p1.EFS_INVENTORY_GUARD_TOOL).endswith(str(Path("automation") / "phases" / "phase1" / "managed_efs_inventory_guard.py")))
        self.assertEqual(self.read_state()["managed_efs_inventory_completed"], "true")

    def test_orphaned_aws_side_efs_fails_closed(self):
        p1.update_state(self.state_path, {"effective_deploy": "true", "selected_environment": "dev"})
        scripted = self._scripted(_fail(stdout="FAIL: gg-orphan-01: An AWS GoldenGate managed EFS exists without a current managed deployment descriptor."))
        with self.assertRaises(p1.Phase1Error) as ctx:
            self.run_subcommand(p1.cmd_managed_efs_inventory, scripted=scripted, env_overrides=self._base_env())
        self.assertIn("gg-orphan-01", str(ctx.exception))
        self.assertNotIn("managed_efs_inventory_completed", self.read_state())


class TestShellInjectionSafety(Phase1TestCase):
    def test_is_safe_token_rejects_shell_metacharacters(self):
        for bad in ("dev; rm -rf /", "dev`whoami`", "dev$(id)", "../../etc/passwd", "dev && echo pwned", "dev|nc", "", None, 123):
            self.assertFalse(p1.is_safe_token(bad), msg=repr(bad))

    def test_resolve_environment_rejects_an_injection_payload(self):
        with self.assertRaises(p1.Phase1Error):
            self.run_subcommand(p1.cmd_resolve_environment, env_overrides={"EVENT_NAME": "workflow_dispatch", "INPUT_ENVIRONMENT": "dev; rm -rf /"})
        self.assertNotIn("selected_environment", self.read_state())

    def test_run_never_invokes_subprocess_with_shell_true(self):
        p1.update_state(self.state_path, {"selected_environment": "dev"})
        scripted = ScriptedSubprocess().on(lambda argv: True, _ok(""))
        with mock.patch.object(p1.subprocess, "run", scripted):
            p1.cmd_validate_model(self.args)
        self.assertEqual(len(scripted.calls), 1)
        self.assertIsInstance(scripted.calls[0], list)
        self.assertTrue(all(isinstance(a, str) for a in scripted.calls[0]))


class TestStateFileSecrecy(Phase1TestCase):
    def test_eks_preflight_never_persists_assumed_role_credentials_to_state(self):
        p1.update_state(self.state_path, {"effective_deploy": "true", "selected_environment": "dev"})
        cluster = {"cluster": {"name": "gg-poc-dev", "status": "ACTIVE", "arn": "arn:aws:eks:eu-west-1:123456789012:cluster/gg-poc-dev", "identity": {"oidc": {"issuer": "https://oidc.example/id/ABC"}}}}
        scripted = (
            ScriptedSubprocess()
            .on(lambda argv: argv[:3] == ["aws", "sts", "assume-role"], _assume_role_ok())
            .on(lambda argv: argv[:3] == ["aws", "sts", "get-caller-identity"], _ok(WORKLOAD_ACCOUNT_ID + "\n"))
            .on(lambda argv: argv[:3] == ["aws", "eks", "describe-cluster"], _ok(json.dumps(cluster)))
            .on(lambda argv: argv == ["bash", "-c", "command -v kubectl"], _ok(""))
            .on(lambda argv: argv[:2] == ["kubectl", "version"], _ok(""))
            .on(lambda argv: argv[:3] == ["aws", "eks", "update-kubeconfig"], _ok(""))
            .on(lambda argv: argv == ["kubectl", "config", "current-context"], _ok("fake-context\n"))
            .on(lambda argv: argv[:3] == ["kubectl", "get", "namespace"], _ok(""))
        )
        env_overrides = {
            "AWS_REGION": "eu-west-1",
            "EKS_CLUSTER_NAME": "gg-poc-dev",
            "EKS_CLUSTER_ARN": cluster["cluster"]["arn"],
            "EKS_OIDC_ISSUER": cluster["cluster"]["identity"]["oidc"]["issuer"],
            "EKS_DEPLOY_ROLE_ARN": EKS_DEPLOY_ROLE_ARN,
            "WORKLOAD_ACCOUNT_ID": WORKLOAD_ACCOUNT_ID,
        }
        self.run_subcommand(p1.cmd_eks_preflight, scripted=scripted, env_overrides=env_overrides)

        self.assertEqual(self.read_state()["eks_preflight_completed"], "true")
        raw_state_text = self.state_path.read_text(encoding="utf-8")
        for secret in ("FAKE_ACCESS_KEY_ID", "FAKE_SECRET_ACCESS_KEY", "FAKE_SESSION_TOKEN"):
            self.assertNotIn(secret, raw_state_text)

    def test_canonical_output_keys_contain_no_secret_looking_names(self):
        forbidden_substrings = ("secret", "access_key", "session_token", "password", "private_key", "certificate")
        for key in p1.CANONICAL_OUTPUT_KEYS:
            for forbidden in forbidden_substrings:
                self.assertNotIn(forbidden, key.lower(), msg=key)


class TestBuildAccountCredentialScope(Phase1TestCase):
    """Since the Configure AWS credentials step now injects the build-account credentials only as step-local env: (never job-wide), these tests prove that ambient AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/AWS_SESSION_TOKEN values -- however they reach os.environ -- are used by _assume_role_env purely as the source identity for sts:AssumeRole and never echoed into the Phase 1 state file, GITHUB_OUTPUT, or GITHUB_ENV."""

    BUILD_ACCESS_KEY_ID = "BUILD_ACCOUNT_ACCESS_KEY_ID"
    BUILD_SECRET_ACCESS_KEY = "BUILD_ACCOUNT_SECRET_ACCESS_KEY"
    BUILD_SESSION_TOKEN = "BUILD_ACCOUNT_SESSION_TOKEN"

    def _assert_no_credential_leak(self):
        for path in (self.state_path, self.github_output, self.github_env):
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            for secret in (self.BUILD_ACCESS_KEY_ID, self.BUILD_SECRET_ACCESS_KEY, self.BUILD_SESSION_TOKEN, "FAKE_ACCESS_KEY_ID", "FAKE_SECRET_ACCESS_KEY", "FAKE_SESSION_TOKEN"):
                self.assertNotIn(secret, text, msg=f"{secret!r} leaked into {path}")

    def test_eks_preflight_never_leaks_build_or_workload_credentials(self):
        p1.update_state(self.state_path, {"effective_deploy": "true", "selected_environment": "dev"})
        cluster = {"cluster": {"name": "gg-poc-dev", "status": "ACTIVE", "arn": "arn:aws:eks:eu-west-1:123456789012:cluster/gg-poc-dev", "identity": {"oidc": {"issuer": "https://oidc.example/id/ABC"}}}}
        scripted = (
            ScriptedSubprocess()
            .on(lambda argv: argv[:3] == ["aws", "sts", "assume-role"], _assume_role_ok())
            .on(lambda argv: argv[:3] == ["aws", "sts", "get-caller-identity"], _ok(WORKLOAD_ACCOUNT_ID + "\n"))
            .on(lambda argv: argv[:3] == ["aws", "eks", "describe-cluster"], _ok(json.dumps(cluster)))
            .on(lambda argv: argv == ["bash", "-c", "command -v kubectl"], _ok(""))
            .on(lambda argv: argv[:2] == ["kubectl", "version"], _ok(""))
            .on(lambda argv: argv[:3] == ["aws", "eks", "update-kubeconfig"], _ok(""))
            .on(lambda argv: argv == ["kubectl", "config", "current-context"], _ok("fake-context\n"))
            .on(lambda argv: argv[:3] == ["kubectl", "get", "namespace"], _ok(""))
        )
        env_overrides = {
            "AWS_REGION": "eu-west-1",
            "EKS_CLUSTER_NAME": "gg-poc-dev",
            "EKS_CLUSTER_ARN": cluster["cluster"]["arn"],
            "EKS_OIDC_ISSUER": cluster["cluster"]["identity"]["oidc"]["issuer"],
            "EKS_DEPLOY_ROLE_ARN": EKS_DEPLOY_ROLE_ARN,
            "WORKLOAD_ACCOUNT_ID": WORKLOAD_ACCOUNT_ID,
            # Simulates the step-local env: this step now receives from steps.aws_build_credentials.outputs.* -- present in os.environ exactly as GitHub Actions would inject it, never routed through GITHUB_ENV or the state file.
            "AWS_ACCESS_KEY_ID": self.BUILD_ACCESS_KEY_ID,
            "AWS_SECRET_ACCESS_KEY": self.BUILD_SECRET_ACCESS_KEY,
            "AWS_SESSION_TOKEN": self.BUILD_SESSION_TOKEN,
        }
        self.run_subcommand(p1.cmd_eks_preflight, scripted=scripted, env_overrides=env_overrides)
        self.assertEqual(self.read_state()["eks_preflight_completed"], "true")
        self._assert_no_credential_leak()

    def test_managed_efs_inventory_never_leaks_build_or_workload_credentials(self):
        p1.update_state(self.state_path, {"effective_deploy": "true", "selected_environment": "dev"})
        scripted = (
            ScriptedSubprocess()
            .on(lambda argv: "managed-efs-inventory" in argv, _ok("[]"))
            .on(lambda argv: argv[:3] == ["aws", "sts", "assume-role"], _assume_role_ok())
            .on(lambda argv: argv[:3] == ["aws", "sts", "get-caller-identity"], _ok(WORKLOAD_ACCOUNT_ID + "\n"))
            .on(lambda argv: argv[:3] == ["aws", "efs", "describe-file-systems"], _ok(json.dumps({"FileSystems": []})))
            .on(lambda argv: str(p1.EFS_INVENTORY_GUARD_TOOL) in argv, _ok("OK: no orphan detected."))
        )
        env_overrides = {
            "AWS_REGION": "eu-west-1",
            "EKS_DEPLOY_ROLE_ARN": EKS_DEPLOY_ROLE_ARN,
            "AWS_ACCESS_KEY_ID": self.BUILD_ACCESS_KEY_ID,
            "AWS_SECRET_ACCESS_KEY": self.BUILD_SECRET_ACCESS_KEY,
            "AWS_SESSION_TOKEN": self.BUILD_SESSION_TOKEN,
        }
        self.run_subcommand(p1.cmd_managed_efs_inventory, scripted=scripted, env_overrides=env_overrides)
        self.assertEqual(self.read_state()["managed_efs_inventory_completed"], "true")
        self._assert_no_credential_leak()

    def test_assume_role_env_keeps_workload_credentials_subprocess_local(self):
        os.environ["AWS_ACCESS_KEY_ID"] = self.BUILD_ACCESS_KEY_ID
        try:
            with mock.patch.object(p1, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps({"Credentials": {"AccessKeyId": "WORKLOAD_KEY", "SecretAccessKey": "WORKLOAD_SECRET", "SessionToken": "WORKLOAD_TOKEN"}}), "")):
                role_env = p1._assume_role_env(EKS_DEPLOY_ROLE_ARN, "test-session")
            self.assertEqual(role_env["AWS_ACCESS_KEY_ID"], "WORKLOAD_KEY")
            self.assertEqual(role_env["AWS_SECRET_ACCESS_KEY"], "WORKLOAD_SECRET")
            self.assertEqual(role_env["AWS_SESSION_TOKEN"], "WORKLOAD_TOKEN")
            self.assertNotIn("WORKLOAD_KEY", os.environ.get("AWS_ACCESS_KEY_ID", ""))
        finally:
            del os.environ["AWS_ACCESS_KEY_ID"]

    def test_write_github_output_and_append_github_env_never_called_with_credential_shaped_keys(self):
        with open(TOOL_PATH, encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn('write_github_output([("aws', source.lower())
        self.assertNotIn("append_github_env([(\"aws_access", source.lower())
        self.assertNotIn("append_github_env([(\"aws_secret", source.lower())
        self.assertNotIn("append_github_env([(\"aws_session", source.lower())


class TestOutputWriterFidelity(Phase1TestCase):
    def test_write_github_output_preserves_json_without_shell_mangling(self):
        tricky_value = json.dumps([{"deployment_id": "gg-x", "note": "quote\" dollar$ backtick` semicolon;"}])
        p1.write_github_output([("deployment_matrix", tricky_value)])
        outputs = self.read_outputs()
        self.assertEqual(outputs["deployment_matrix"], tricky_value)
        self.assertEqual(json.loads(outputs["deployment_matrix"]), json.loads(tricky_value))


class TestGithubSpecialFileInjectionHardening(Phase1TestCase):
    """Reproduces and guards against the bare-CR and fixed-delimiter special-file injection findings for write_github_output()/append_github_env() -- a caller-supplied value (e.g. the manual workflow_dispatch governance reason) must never be able to smuggle a second, independent NAME=value output/env fragment."""

    def test_write_github_output_bare_cr_cannot_inject_a_second_output(self):
        malicious = "approved\rrogue_output=injected"
        p1.write_github_output([("terraform_governance_override_reason", malicious)])
        outputs = self.read_outputs()
        self.assertNotIn("rogue_output", outputs)
        self.assertEqual(set(outputs.keys()), {"terraform_governance_override_reason"})

    def test_write_github_output_crlf_cannot_inject_a_second_output(self):
        malicious = "line one\r\nrogue_output=injected"
        p1.write_github_output([("terraform_governance_override_reason", malicious)])
        outputs = self.read_outputs()
        self.assertNotIn("rogue_output", outputs)
        self.assertEqual(set(outputs.keys()), {"terraform_governance_override_reason"})

    def test_write_github_output_lf_cannot_inject_a_second_output(self):
        malicious = "line one\nrogue_output=injected"
        p1.write_github_output([("terraform_governance_override_reason", malicious)])
        outputs = self.read_outputs()
        self.assertNotIn("rogue_output", outputs)
        self.assertEqual(set(outputs.keys()), {"terraform_governance_override_reason"})

    def test_write_github_output_fixed_delimiter_collision_attack_is_defeated(self):
        old_deterministic_delimiter = "GG_EOF_terraform_governance_override_reason"
        attack = f"line1\n{old_deterministic_delimiter}\nrogue_output=injected\nline4"
        p1.write_github_output([("terraform_governance_override_reason", attack)])
        outputs = self.read_outputs()
        self.assertNotIn("rogue_output", outputs)
        self.assertEqual(outputs["terraform_governance_override_reason"], attack)

    def test_write_github_output_retries_on_forced_random_delimiter_collision(self):
        value = "line1\nggPhase1Delim_aaaa\nline3"
        with mock.patch.object(p1.secrets, "token_hex", side_effect=["aaaa", "bbbb"]):
            delimiter = p1._github_file_delimiter(value)
        self.assertNotIn(delimiter, value)
        self.assertEqual(delimiter, "ggPhase1Delim_bbbb")

    def test_write_github_output_normal_single_line_value_is_unaffected(self):
        p1.write_github_output([("effective_deploy", "true")])
        raw = self.github_output.read_text(encoding="utf-8")
        self.assertEqual(raw, "effective_deploy=true\n")

    def test_append_github_env_bare_cr_cannot_inject_a_second_variable(self):
        malicious = "approved\rROGUE_VAR=injected"
        p1.append_github_env([("GG_SELECTED_ENVIRONMENT", malicious)])
        env = self.read_env()
        self.assertNotIn("ROGUE_VAR", env)
        self.assertEqual(set(env.keys()), {"GG_SELECTED_ENVIRONMENT"})

    def test_append_github_env_crlf_cannot_inject_a_second_variable(self):
        malicious = "line one\r\nROGUE_VAR=injected"
        p1.append_github_env([("GG_SELECTED_ENVIRONMENT", malicious)])
        env = self.read_env()
        self.assertNotIn("ROGUE_VAR", env)

    def test_append_github_env_fixed_delimiter_collision_attack_is_defeated(self):
        old_deterministic_delimiter = "GG_EOF_GG_SELECTED_ENVIRONMENT"
        attack = f"line1\n{old_deterministic_delimiter}\nROGUE_VAR=injected\nline4"
        p1.append_github_env([("GG_SELECTED_ENVIRONMENT", attack)])
        env = self.read_env()
        self.assertNotIn("ROGUE_VAR", env)
        self.assertEqual(env["GG_SELECTED_ENVIRONMENT"], attack)

    def test_append_github_env_normal_single_line_value_is_unaffected(self):
        p1.append_github_env([("GG_SELECTED_ENVIRONMENT", "dev")])
        raw = self.github_env.read_text(encoding="utf-8")
        self.assertEqual(raw, "GG_SELECTED_ENVIRONMENT=dev\n")

    def test_requires_heredoc_recognizes_lf_cr_and_crlf(self):
        self.assertTrue(p1._requires_heredoc("a\nb"))
        self.assertTrue(p1._requires_heredoc("a\rb"))
        self.assertTrue(p1._requires_heredoc("a\r\nb"))
        self.assertFalse(p1._requires_heredoc("a b"))


class TestAcceptance(Phase1TestCase):
    def _base_state(self, effective_deploy):
        return {
            "selected_environment": "dev",
            "effective_deploy": effective_deploy,
            "has_active_deployments": "true",
            "active_runtime_matrix": "[]",
            "terraform_governance_override": "false",
            "terraform_governance_override_reason": "",
            "has_changes": "false",
            "deployment_matrix": "[]",
            "has_deletions": "false",
            "deletion_matrix": "[]",
            "has_storage_transition_violations": "false",
            "storage_transition_violations": "[]",
        }

    def test_validate_mode_succeeds_with_eks_and_inventory_marked_not_applicable(self):
        p1.update_state(self.state_path, self._base_state("false"))
        p1.cmd_acceptance(self.args)
        summary = self.github_summary.read_text(encoding="utf-8")
        self.assertIn("not applicable (Validate mode)", summary)

    def test_deploy_mode_requires_both_live_checks_recorded_successful(self):
        p1.update_state(self.state_path, self._base_state("true"))
        with self.assertRaises(p1.Phase1Error):
            p1.cmd_acceptance(self.args)
        p1.update_state(self.state_path, {"eks_preflight_completed": "true"})
        with self.assertRaises(p1.Phase1Error):
            p1.cmd_acceptance(self.args)
        p1.update_state(self.state_path, {"managed_efs_inventory_completed": "true"})
        p1.cmd_acceptance(self.args)
        summary = self.github_summary.read_text(encoding="utf-8")
        self.assertIn("validated", summary)


class TestEksPreflight(Phase1TestCase):
    def test_cluster_not_active_fails_closed(self):
        p1.update_state(self.state_path, {"effective_deploy": "true", "selected_environment": "dev"})
        cluster = {"cluster": {"name": "gg-poc-dev", "status": "CREATING", "arn": "arn:aws:eks:eu-west-1:123456789012:cluster/gg-poc-dev", "identity": {"oidc": {"issuer": "https://oidc.example/id/ABC"}}}}
        scripted = (
            ScriptedSubprocess()
            .on(lambda argv: argv[:3] == ["aws", "sts", "assume-role"], _assume_role_ok())
            .on(lambda argv: argv[:3] == ["aws", "sts", "get-caller-identity"], _ok(WORKLOAD_ACCOUNT_ID + "\n"))
            .on(lambda argv: argv[:3] == ["aws", "eks", "describe-cluster"], _ok(json.dumps(cluster)))
        )
        env_overrides = {
            "AWS_REGION": "eu-west-1",
            "EKS_CLUSTER_NAME": "gg-poc-dev",
            "EKS_CLUSTER_ARN": cluster["cluster"]["arn"],
            "EKS_OIDC_ISSUER": cluster["cluster"]["identity"]["oidc"]["issuer"],
            "EKS_DEPLOY_ROLE_ARN": EKS_DEPLOY_ROLE_ARN,
            "WORKLOAD_ACCOUNT_ID": WORKLOAD_ACCOUNT_ID,
        }
        with self.assertRaises(p1.Phase1Error) as ctx:
            self.run_subcommand(p1.cmd_eks_preflight, scripted=scripted, env_overrides=env_overrides)
        self.assertIn("CREATING", str(ctx.exception))
        self.assertNotIn("eks_preflight_completed", self.read_state())


class TestManagedEfsDeletionGuard(Phase1TestCase):
    def test_physical_removal_of_a_managed_deployment_fails_closed(self):
        p1.update_state(self.state_path, {"deletion_matrix": json.dumps([{"deployment_id": "gg-x", "efs_mode": "managed", "reason": "physical-removal"}])})
        with self.assertRaises(p1.Phase1Error):
            p1.cmd_managed_efs_deletion_guard(self.args)

    def test_deployment_disabled_managed_efs_is_allowed(self):
        p1.update_state(self.state_path, {"deletion_matrix": json.dumps([{"deployment_id": "gg-x", "efs_mode": "managed", "reason": "deployment-disabled"}])})
        p1.cmd_managed_efs_deletion_guard(self.args)


class TestStorageTransitionGuard(Phase1TestCase):
    def test_unsafe_storage_identity_transition_fails_closed(self):
        violations = [{"deployment_id": "gg-x", "violation": "managed->existing"}]
        p1.update_state(self.state_path, {"storage_transition_violations": json.dumps(violations)})
        with self.assertRaises(p1.Phase1Error):
            p1.cmd_storage_transition_guard(self.args)

    def test_no_violations_passes(self):
        p1.update_state(self.state_path, {"storage_transition_violations": "[]"})
        p1.cmd_storage_transition_guard(self.args)


if __name__ == "__main__":
    unittest.main()
