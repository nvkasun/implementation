"""Offline unit tests for automation/phases/phase2/phase2_prerequisites.py; run directly via `python3 automation/phases/phase2/tests/test_phase2_prerequisites.py`. Every automation/goldengate-environment.py call is scripted through a fake subprocess.run -- this suite never touches live AWS or Terraform."""
from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[4]
TOOL_PATH = REPO_ROOT / "automation" / "phases" / "phase2" / "phase2_prerequisites.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("phase2_prerequisites", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


p2 = _load_tool()


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


class Phase2TestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp_path = Path(self._tmp.name)
        self.state_path = tmp_path / "state.json"
        self.github_output = tmp_path / "github_output.txt"
        self.github_summary = tmp_path / "github_summary.txt"
        self.args = SimpleNamespace(state_path=self.state_path)
        env_patch = {
            "GITHUB_OUTPUT": str(self.github_output),
            "GITHUB_STEP_SUMMARY": str(self.github_summary),
        }
        patcher = mock.patch.dict("os.environ", env_patch)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_subcommand(self, cmd_func, scripted=None, env_overrides=None):
        env_overrides = env_overrides or {}
        with mock.patch.dict("os.environ", env_overrides):
            if scripted is not None:
                with mock.patch.object(p2.subprocess, "run", scripted):
                    cmd_func(self.args)
            else:
                cmd_func(self.args)

    def read_state(self):
        return p2.load_state(self.state_path)

    def read_outputs(self):
        pairs = {}
        if not self.github_output.exists():
            return pairs
        lines = self.github_output.read_text(encoding="utf-8").splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            if "<<" in line:
                key, delim = line.split("<<", 1)
                i += 1
                buf = []
                while i < len(lines) and lines[i] != delim:
                    buf.append(lines[i])
                    i += 1
                pairs[key] = "\n".join(buf)
            elif "=" in line:
                key, val = line.split("=", 1)
                pairs[key] = val
            i += 1
        return pairs


class TestValidateEnvironment(Phase2TestCase):
    def _scripted_validate_ok(self):
        return ScriptedSubprocess().on(lambda argv: "validate" in argv, _ok("OK: envs/dev/environment.yaml is valid"))

    def test_valid_environment_accepted(self):
        self.run_subcommand(p2.cmd_validate_environment, scripted=self._scripted_validate_ok(), env_overrides={"TARGET_ENVIRONMENT": "dev"})
        self.assertEqual(self.read_state()["selected_environment"], "dev")

    def test_unsafe_path_traversal_environment_rejected(self):
        for bad in ("../../etc", "dev/../../etc", "dev; rm -rf /", "$(whoami)", "DEV", "dev_env", ""):
            with self.assertRaises(p2.Phase2Error, msg=repr(bad)):
                self.run_subcommand(p2.cmd_validate_environment, env_overrides={"TARGET_ENVIRONMENT": bad})
            self.assertNotIn("selected_environment", self.read_state())

    def test_environment_validation_invokes_canonical_environment_tool(self):
        scripted = self._scripted_validate_ok()
        self.run_subcommand(p2.cmd_validate_environment, scripted=scripted, env_overrides={"TARGET_ENVIRONMENT": "dev"})
        self.assertEqual(len(scripted.calls), 1)
        call = scripted.calls[0]
        self.assertIn(str(p2.ENVIRONMENT_TOOL), call)
        self.assertIn("--environment", call)
        self.assertIn("dev", call)
        self.assertIn("validate", call)
        self.assertTrue(str(p2.ENVIRONMENT_TOOL).endswith(str(Path("automation") / "goldengate-environment.py")))

    def test_environment_validation_failure_propagates(self):
        scripted = ScriptedSubprocess().on(lambda argv: "validate" in argv, _fail(stdout="FAIL: envs/dev/environment.yaml failed validation"))
        with self.assertRaises(p2.Phase2Error):
            self.run_subcommand(p2.cmd_validate_environment, scripted=scripted, env_overrides={"TARGET_ENVIRONMENT": "dev"})


class TestValidateIamPolicies(Phase2TestCase):
    def test_iam_policy_validation_invokes_render_iam_policies_check(self):
        p2.update_state(self.state_path, {"selected_environment": "dev"})
        scripted = ScriptedSubprocess().on(lambda argv: True, _ok("OK: all generated IAM policies are in sync"))
        self.run_subcommand(p2.cmd_validate_iam_policies, scripted=scripted)
        self.assertEqual(len(scripted.calls), 1)
        call = scripted.calls[0]
        self.assertIn("render-iam-policies", call)
        self.assertIn("--check", call)
        self.assertIn(str(p2.ENVIRONMENT_TOOL), call)

    def test_iam_policy_validation_failure_fails_closed(self):
        p2.update_state(self.state_path, {"selected_environment": "dev"})
        scripted = ScriptedSubprocess().on(lambda argv: True, _fail(stdout="FAIL: policies_1.json is out of sync"))
        with self.assertRaises(p2.Phase2Error):
            self.run_subcommand(p2.cmd_validate_iam_policies, scripted=scripted)


class TestResolveRegion(Phase2TestCase):
    def test_region_read_through_canonical_get_aws_region(self):
        p2.update_state(self.state_path, {"selected_environment": "dev"})
        scripted = ScriptedSubprocess().on(lambda argv: True, _ok("eu-west-1\n"))
        self.run_subcommand(p2.cmd_resolve_region, scripted=scripted)
        self.assertEqual(len(scripted.calls), 1)
        call = scripted.calls[0]
        self.assertIn("get", call)
        self.assertIn("AWS_REGION", call)
        self.assertIn(str(p2.ENVIRONMENT_TOOL), call)
        self.assertEqual(self.read_state()["aws_region"], "eu-west-1")
        self.assertEqual(self.read_outputs()["aws_region"], "eu-west-1")

    def test_empty_region_fails_closed(self):
        p2.update_state(self.state_path, {"selected_environment": "dev"})
        scripted = ScriptedSubprocess().on(lambda argv: True, _ok("\n"))
        with self.assertRaises(p2.Phase2Error):
            self.run_subcommand(p2.cmd_resolve_region, scripted=scripted)
        self.assertNotIn("aws_region", self.read_state())

    def test_region_resolution_does_not_hardcode_a_literal_region(self):
        with open(TOOL_PATH, encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn("eu-west-1", source)


class TestGovernanceOverride(Phase2TestCase):
    def test_override_literal_false_accepted(self):
        self.run_subcommand(p2.cmd_validate_governance, env_overrides={"INPUT_OVERRIDE": "false", "INPUT_REASON": ""})
        state = self.read_state()
        self.assertEqual(state["terraform_governance_override"], "false")
        self.assertEqual(state["terraform_governance_override_reason"], "")

    def test_override_literal_true_with_reason_accepted(self):
        self.run_subcommand(p2.cmd_validate_governance, env_overrides={"INPUT_OVERRIDE": "true", "INPUT_REASON": "Approved DEV POC emergency validation"})
        state = self.read_state()
        self.assertEqual(state["terraform_governance_override"], "true")
        self.assertEqual(state["terraform_governance_override_reason"], "Approved DEV POC emergency validation")

    def test_override_true_with_empty_reason_rejected(self):
        with self.assertRaises(p2.Phase2Error):
            self.run_subcommand(p2.cmd_validate_governance, env_overrides={"INPUT_OVERRIDE": "true", "INPUT_REASON": ""})
        self.assertNotIn("terraform_governance_override", self.read_state())

    def test_override_true_with_whitespace_only_reason_rejected(self):
        with self.assertRaises(p2.Phase2Error):
            self.run_subcommand(p2.cmd_validate_governance, env_overrides={"INPUT_OVERRIDE": "true", "INPUT_REASON": "   "})
        self.assertNotIn("terraform_governance_override", self.read_state())

    def test_invalid_boolean_representations_are_rejected(self):
        for bad in ("TRUE", "False", "yes", "1", "0", "", " true", "true "):
            with self.assertRaises(p2.Phase2Error, msg=repr(bad)):
                self.run_subcommand(p2.cmd_validate_governance, env_overrides={"INPUT_OVERRIDE": bad, "INPUT_REASON": "x"})

    def test_override_false_publishes_empty_reason_even_if_one_was_supplied(self):
        self.run_subcommand(p2.cmd_validate_governance, env_overrides={"INPUT_OVERRIDE": "false", "INPUT_REASON": "informational text"})
        state = self.read_state()
        self.assertEqual(state["terraform_governance_override"], "false")
        self.assertEqual(state["terraform_governance_override_reason"], "")
        self.assertEqual(self.read_outputs()["terraform_governance_override_reason"], "")


class TestGithubOutputSafety(Phase2TestCase):
    def test_multiline_reason_is_safely_emitted_without_output_injection(self):
        malicious_reason = "line one\nrogue_output=injected\nline three"
        self.run_subcommand(p2.cmd_validate_governance, env_overrides={"INPUT_OVERRIDE": "true", "INPUT_REASON": malicious_reason})
        outputs = self.read_outputs()
        self.assertEqual(outputs["terraform_governance_override_reason"], malicious_reason)
        self.assertNotIn("rogue_output", outputs)

    def test_bare_cr_reason_cannot_inject_a_second_output(self):
        malicious_reason = "approved\rrogue_output=injected"
        self.run_subcommand(p2.cmd_validate_governance, env_overrides={"INPUT_OVERRIDE": "true", "INPUT_REASON": malicious_reason})
        outputs = self.read_outputs()
        self.assertNotIn("rogue_output", outputs)
        self.assertEqual(set(outputs.keys()), {"terraform_governance_override", "terraform_governance_override_reason"})

    def test_crlf_reason_cannot_inject_a_second_output(self):
        malicious_reason = "line one\r\nline two"
        self.run_subcommand(p2.cmd_validate_governance, env_overrides={"INPUT_OVERRIDE": "true", "INPUT_REASON": malicious_reason})
        outputs = self.read_outputs()
        self.assertEqual(set(outputs.keys()), {"terraform_governance_override", "terraform_governance_override_reason"})

    def test_delimiter_collision_is_safely_handled(self):
        colliding_value = "line1\nggPhase2Delim_AAAA\nline3"
        with mock.patch.object(p2.secrets, "token_hex", side_effect=["AAAA", "BBBB"]):
            delimiter = p2._github_output_delimiter(colliding_value)
        self.assertNotIn(delimiter, colliding_value)
        self.assertEqual(delimiter, "ggPhase2Delim_BBBB")

    def test_normal_single_line_output_uses_simple_name_equals_value_form(self):
        p2.write_github_output([("aws_region", "eu-west-1")])
        raw = self.github_output.read_text(encoding="utf-8")
        self.assertEqual(raw, "aws_region=eu-west-1\n")

    def test_requires_heredoc_recognizes_lf_cr_and_crlf(self):
        self.assertTrue(p2._requires_heredoc("a\nb"))
        self.assertTrue(p2._requires_heredoc("a\rb"))
        self.assertTrue(p2._requires_heredoc("a\r\nb"))
        self.assertFalse(p2._requires_heredoc("a b"))

    def test_output_names_are_fixed_literals_never_caller_controlled(self):
        with open(TOOL_PATH, encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn("write_github_output([(input_", source.lower())
        self.assertNotIn('write_github_output([(f"', source)


class TestStateSecrecyAndValidation(Phase2TestCase):
    def test_state_contains_only_canonical_non_secret_keys(self):
        p2.update_state(self.state_path, {"selected_environment": "dev"})
        self.run_subcommand(p2.cmd_validate_governance, env_overrides={"INPUT_OVERRIDE": "false", "INPUT_REASON": ""})
        scripted = ScriptedSubprocess().on(lambda argv: True, _ok("eu-west-1\n"))
        self.run_subcommand(p2.cmd_resolve_region, scripted=scripted)
        allowed_keys = {"selected_environment", "aws_region", "terraform_governance_override", "terraform_governance_override_reason"}
        self.assertEqual(set(self.read_state().keys()), allowed_keys)

    def test_no_credential_shaped_values_written_to_state(self):
        with open(TOOL_PATH, encoding="utf-8") as f:
            phase2_lines = f.read().splitlines()
        write_calls = ("update_state(", "save_state(")
        forbidden = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "GITHUB_TOKEN", "PASSWORD", "PRIVATE_KEY", "CERTIFICATE")
        leaking_lines = [line for line in phase2_lines if any(call in line for call in write_calls) and any(k in line.upper() for k in forbidden)]
        self.assertEqual(leaking_lines, [])

    def test_no_credential_shaped_values_written_to_github_output(self):
        with open(TOOL_PATH, encoding="utf-8") as f:
            phase2_lines = f.read().splitlines()
        forbidden = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "GITHUB_TOKEN", "PASSWORD", "PRIVATE_KEY", "CERTIFICATE")
        leaking_lines = [line for line in phase2_lines if "write_github_output(" in line and any(k in line.upper() for k in forbidden)]
        self.assertEqual(leaking_lines, [])

    def test_malformed_state_fails_closed(self):
        self.state_path.write_text("not valid json", encoding="utf-8")
        with self.assertRaises(p2.Phase2Error):
            p2.load_state(self.state_path)

    def test_missing_state_key_fails_closed(self):
        with self.assertRaises(p2.Phase2Error):
            p2.cmd_resolve_region(self.args)
        with self.assertRaises(p2.Phase2Error):
            p2.cmd_publish_outputs(self.args)


class TestPublishOutputs(Phase2TestCase):
    def _full_state(self):
        return {
            "selected_environment": "dev",
            "aws_region": "eu-west-1",
            "terraform_governance_override": "false",
            "terraform_governance_override_reason": "",
        }

    def test_publish_outputs_preserves_values_exactly(self):
        p2.update_state(self.state_path, self._full_state())
        p2.cmd_publish_outputs(self.args)
        outputs = self.read_outputs()
        self.assertEqual(outputs["aws_region"], "eu-west-1")
        self.assertEqual(outputs["terraform_governance_override"], "false")
        self.assertEqual(outputs["terraform_governance_override_reason"], "")


class TestAcceptance(Phase2TestCase):
    def test_acceptance_succeeds_for_valid_normal_governance_state(self):
        p2.update_state(self.state_path, {
            "selected_environment": "dev",
            "aws_region": "eu-west-1",
            "terraform_governance_override": "false",
            "terraform_governance_override_reason": "",
        })
        p2.cmd_acceptance(self.args)
        summary = self.github_summary.read_text(encoding="utf-8")
        self.assertIn("Governance override: disabled", summary)
        self.assertIn("Result: PASSED", summary)

    def test_acceptance_succeeds_for_valid_authorized_override_state(self):
        p2.update_state(self.state_path, {
            "selected_environment": "dev",
            "aws_region": "eu-west-1",
            "terraform_governance_override": "true",
            "terraform_governance_override_reason": "Approved DEV POC emergency validation",
        })
        p2.cmd_acceptance(self.args)
        summary = self.github_summary.read_text(encoding="utf-8")
        self.assertIn("Governance override: ENABLED (written justification supplied)", summary)
        self.assertNotIn("Approved DEV POC emergency validation", summary)

    def test_acceptance_fails_closed_when_override_true_but_reason_empty(self):
        p2.update_state(self.state_path, {
            "selected_environment": "dev",
            "aws_region": "eu-west-1",
            "terraform_governance_override": "true",
            "terraform_governance_override_reason": "",
        })
        with self.assertRaises(p2.Phase2Error):
            p2.cmd_acceptance(self.args)


WORKLOAD_ACCOUNT_ID = "123456789012"
EKS_DEPLOY_ROLE_ARN = f"arn:aws:iam::{WORKLOAD_ACCOUNT_ID}:role/GoldenGateEKSDeployRole-dev"
INVENTORY_ENV = {"TARGET_ENVIRONMENT": "dev", "AWS_REGION": "eu-west-1", "EKS_DEPLOY_ROLE_ARN": EKS_DEPLOY_ROLE_ARN}


def _inventory_scripted(describe=None, guard=None):
    creds = {"Credentials": {"AccessKeyId": "FAKE_ID", "SecretAccessKey": "FAKE_SECRET", "SessionToken": "FAKE_TOKEN"}}
    return (
        ScriptedSubprocess()
        .on(lambda argv: "managed-efs-inventory" in argv, _ok("[]"))
        .on(lambda argv: argv[:3] == ["aws", "sts", "assume-role"], _ok(__import__("json").dumps(creds)))
        .on(lambda argv: argv[:3] == ["aws", "sts", "get-caller-identity"], _ok(WORKLOAD_ACCOUNT_ID + "\n"))
        .on(lambda argv: argv[:3] == ["aws", "efs", "describe-file-systems"], describe or _ok('{"FileSystems": []}'))
        .on(lambda argv: "managed_efs_inventory_guard.py" in " ".join(argv), guard or _ok("OK"))
    )


class TestManagedEfsInventoryGate(Phase2TestCase):
    """phase2 managed-efs-inventory: the Terraform apply gate reuses Phase 1's guard and fails closed."""

    def _run(self, scripted, env=INVENTORY_ENV):
        with mock.patch.dict("os.environ", env), mock.patch("subprocess.run", scripted):
            p2.cmd_managed_efs_inventory(self.args)

    def test_matching_inventory_passes(self):
        scripted = _inventory_scripted()
        self._run(scripted)
        self.assertTrue(any("managed_efs_inventory_guard.py" in " ".join(c) for c in scripted.calls))

    def test_guard_rejection_blocks_apply(self):
        with self.assertRaises(p2.Phase2Error) as ctx:
            self._run(_inventory_scripted(guard=_fail(stdout="FAIL: gg-x-01: An AWS GoldenGate managed EFS exists without a current managed deployment descriptor.")))
        self.assertIn("Terraform apply is blocked", str(ctx.exception))

    def test_access_denied_and_malformed_responses_block_apply(self):
        for describe in (_fail(stderr="An error occurred (AccessDeniedException)", returncode=254), _ok("garbage"), _ok("{}")):
            with self.subTest(describe=describe), self.assertRaises(p2.Phase2Error):
                self._run(_inventory_scripted(describe=describe))

    def test_missing_or_unsafe_inputs_make_no_aws_call(self):
        for env in ({**INVENTORY_ENV, "AWS_REGION": ""}, {**INVENTORY_ENV, "EKS_DEPLOY_ROLE_ARN": ""}, {**INVENTORY_ENV, "TARGET_ENVIRONMENT": "dev;id"}, {**INVENTORY_ENV, "TARGET_ENVIRONMENT": ""}):
            with self.subTest(env=env):
                scripted = _inventory_scripted()
                with self.assertRaises(p2.Phase2Error):
                    self._run(scripted, env=env)
                self.assertEqual(scripted.calls, [])

    def test_cli_exit_status(self):
        with mock.patch.dict("os.environ", INVENTORY_ENV), mock.patch("subprocess.run", _inventory_scripted(describe=_fail(stderr="AccessDenied", returncode=254))):
            self.assertEqual(p2.main(["--state-file", str(self.state_path), "managed-efs-inventory"]), 1)

    def test_authorization_is_rejected_outside_a_manual_dispatch(self):
        for event in ("push", "workflow_call", ""):
            with self.subTest(event=event):
                scripted = _inventory_scripted()
                with self.assertRaises(p2.Phase2Error):
                    self._run(scripted, env={**INVENTORY_ENV, "EVENT_NAME": event, "EFS_REPLACEMENT_AUTHORIZED_IDS": "fs-0123456789abcdef0"})
                self.assertEqual(scripted.calls, [])

    def test_orchestrated_authorization_reaches_the_guard(self):
        ids = "fs-0123456789abcdef0,fs-0123456789abcdef1"
        scripted = _inventory_scripted().on(lambda argv: argv[:3] == ["aws", "efs", "describe-access-points"], _ok('{"AccessPoints": []}'))
        self._run(scripted, env={**INVENTORY_ENV, "EVENT_NAME": "workflow_dispatch", "EFS_REPLACEMENT_AUTHORIZED_IDS": ids})
        guard_calls = [c for c in scripted.calls if "managed_efs_inventory_guard.py" in " ".join(c)]
        self.assertEqual(guard_calls[0][-1], ids)


class TestTerraformApplyEntryPoints(unittest.TestCase):
    """Every Terraform apply in the repository's workflows must depend on the managed-EFS inventory gate."""

    WORKFLOWS = Path(__file__).resolve().parents[4] / ".github" / "workflows"

    def _load(self, name):
        import yaml
        return yaml.safe_load((self.WORKFLOWS / name).read_text(encoding="utf-8"))

    def test_iam_secrets_apply_requires_the_inventory_gate_on_every_trigger(self):
        doc = self._load("10-sub-iam-secrets.yaml")
        triggers = doc.get(True) or doc.get("on")
        self.assertIn("workflow_dispatch", triggers)
        self.assertIn("workflow_call", triggers)
        jobs = doc["jobs"]
        apply = jobs["apply"]
        self.assertIn("verify_managed_efs_inventory", apply["needs"])
        self.assertNotIn("if", apply, "apply must keep the default success() gate on all needs")
        gate = jobs["verify_managed_efs_inventory"]
        self.assertNotIn("if", gate, "the gate must run for orchestrated and standalone runs alike")
        steps = gate["steps"]
        self.assertFalse(any(s.get("continue-on-error") for s in steps))
        self.assertTrue(any("phase2_prerequisites.py managed-efs-inventory" in (s.get("run") or "") for s in steps))
        self.assertTrue(all("set -euo pipefail" in s["run"] for s in steps if "run" in s))

    def test_efs_replacement_authorization_enters_only_through_a_manual_orchestrator_dispatch(self):
        sub = self._load("10-sub-iam-secrets.yaml")
        sub_triggers = sub.get(True) or sub.get("on")
        self.assertIn("efs_replacement_authorized_ids", sub_triggers["workflow_call"]["inputs"])
        self.assertNotIn("efs_replacement_authorized_ids", (sub_triggers["workflow_dispatch"] or {}).get("inputs") or {})
        gate_env = sub["jobs"]["verify_managed_efs_inventory"]["steps"]
        env = {k: v for step in gate_env for k, v in (step.get("env") or {}).items()} | (sub["jobs"]["verify_managed_efs_inventory"].get("env") or {})
        self.assertEqual(env.get("EVENT_NAME"), "${{ github.event_name }}")
        self.assertEqual(env.get("EFS_REPLACEMENT_AUTHORIZED_IDS"), "${{ inputs.efs_replacement_authorized_ids }}")

        main = self._load("00-main-goldengate-orchestrator.yaml")
        main_triggers = main.get(True) or main.get("on")
        self.assertEqual(main_triggers["workflow_dispatch"]["inputs"]["efs_replacement_authorized_ids"].get("default"), "")
        self.assertNotIn("inputs", main_triggers.get("push") or {})
        jobs = main["jobs"]
        self.assertEqual(jobs["validate_model"]["outputs"]["efs_replacement_authorized_ids"], "${{ steps.managed_efs_inventory.outputs.efs_replacement_authorized_ids }}")
        step = next(s for s in jobs["validate_model"]["steps"] if s.get("id") == "managed_efs_inventory")
        self.assertEqual(step["env"]["EVENT_NAME"], "${{ github.event_name }}")
        callers = [j for j in jobs.values() if "10-sub-iam-secrets.yaml" in str(j.get("uses", ""))]
        self.assertEqual(len(callers), 1)
        self.assertEqual(callers[0]["with"]["efs_replacement_authorized_ids"], "${{ needs.validate_model.outputs.efs_replacement_authorized_ids }}")

    def test_no_other_workflow_job_reaches_terraform_apply(self):
        offenders = []
        for path in sorted(self.WORKFLOWS.glob("*.y*ml")):
            jobs = (self._load(path.name) or {}).get("jobs") or {}
            for job_id, job in jobs.items():
                uses = str(job.get("uses", ""))
                if "terraform" in uses.lower() and not (path.name == "10-sub-iam-secrets.yaml" and job_id == "apply"):
                    offenders.append(f"{path.name}:{job_id}")
                for step in job.get("steps") or []:
                    text = f"{step.get('uses', '')} {step.get('run', '')}".lower()
                    if "terraform apply" in text or "terraform destroy" in text or "setup-terraform" in text:
                        offenders.append(f"{path.name}:{job_id}")
        self.assertEqual(offenders, [])


class TestNoLiveExecution(Phase2TestCase):
    def test_no_aws_cli_invocation_in_normal_commands(self):
        with open(TOOL_PATH, encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn('"aws"', source)
        self.assertNotIn("'aws'", source)

    def test_no_terraform_executable_is_invoked(self):
        with open(TOOL_PATH, encoding="utf-8") as f:
            source = f.read()
        for forbidden in ("terraform init", "terraform plan", "terraform apply", "terraform destroy", '"terraform"', "'terraform'"):
            self.assertNotIn(forbidden, source)

    def test_run_never_invokes_subprocess_with_shell_true(self):
        p2.update_state(self.state_path, {"selected_environment": "dev"})
        scripted = ScriptedSubprocess().on(lambda argv: True, _ok(""))
        with mock.patch.object(p2.subprocess, "run", scripted):
            p2.cmd_validate_iam_policies(self.args)
        self.assertEqual(len(scripted.calls), 1)
        self.assertIsInstance(scripted.calls[0], list)
        self.assertTrue(all(isinstance(a, str) for a in scripted.calls[0]))


if __name__ == "__main__":
    unittest.main()
