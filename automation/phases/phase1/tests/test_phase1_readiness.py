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


class TestManagedEfsInventoryFailClosed(Phase1TestCase):
    """verify_managed_efs_inventory(): every AWS failure or malformed response raises -- never an empty inventory, never a guard run on partial data."""

    def _scripted(self, assume=None, identity=None, describe=None, guard=None):
        return (
            ScriptedSubprocess()
            .on(lambda argv: "managed-efs-inventory" in argv, _ok("[]"))
            .on(lambda argv: argv[:3] == ["aws", "sts", "assume-role"], assume or _assume_role_ok())
            .on(lambda argv: argv[:3] == ["aws", "sts", "get-caller-identity"], identity or _ok(WORKLOAD_ACCOUNT_ID + "\n"))
            .on(lambda argv: argv[:3] == ["aws", "efs", "describe-file-systems"], describe or _ok(json.dumps({"FileSystems": []})))
            .on(lambda argv: str(p1.EFS_INVENTORY_GUARD_TOOL) in argv, guard or _ok("OK"))
        )

    def _verify(self, scripted):
        with mock.patch.object(p1.subprocess, "run", scripted):
            p1.verify_managed_efs_inventory("dev", "eu-west-1", EKS_DEPLOY_ROLE_ARN)

    def _guard_calls(self, scripted):
        return [c for c in scripted.calls if str(p1.EFS_INVENTORY_GUARD_TOOL) in c]

    def test_confirmed_empty_inventory_still_runs_the_guard(self):
        scripted = self._scripted()
        self._verify(scripted)
        self.assertEqual(len(self._guard_calls(scripted)), 1)

    def test_aws_failures_fail_closed_before_the_guard(self):
        denied = _fail(stderr="An error occurred (AccessDeniedException) when calling the DescribeFileSystems operation", returncode=254)
        for label, kwargs in (
            ("assume-role denied", {"assume": _fail(stderr="An error occurred (AccessDenied) when calling the AssumeRole operation", returncode=254)}),
            ("assume-role malformed", {"assume": _ok("{}")}),
            ("caller identity failed", {"identity": _fail(stderr="ExpiredToken", returncode=255)}),
            ("wrong account", {"identity": _ok("999999999999\n")}),
            ("describe denied", {"describe": denied}),
            ("describe malformed JSON", {"describe": _ok("not json")}),
            ("describe without FileSystems", {"describe": _ok(json.dumps({"NextMarker": "x"}))}),
            ("describe FileSystems not a list", {"describe": _ok(json.dumps({"FileSystems": {}}))}),
            ("describe non-object entries", {"describe": _ok(json.dumps({"FileSystems": ["fs-1"]}))}),
        ):
            with self.subTest(label=label):
                scripted = self._scripted(**kwargs)
                with self.assertRaises(p1.Phase1Error):
                    self._verify(scripted)
                self.assertEqual(self._guard_calls(scripted), [])

    def test_guard_rejection_fails(self):
        with self.assertRaises(p1.Phase1Error):
            self._verify(self._scripted(guard=_fail(stdout="FAIL: actual EFS 'fs-1' ... does not match")))

    def test_malformed_role_arn_makes_no_aws_call(self):
        scripted = self._scripted()
        with mock.patch.object(p1.subprocess, "run", scripted), self.assertRaises(p1.Phase1Error):
            p1.verify_managed_efs_inventory("dev", "eu-west-1", "not-an-arn")
        self.assertEqual(scripted.calls, [])


class TestEfsReplacementAuthorization(Phase1TestCase):
    """One-time DEV storage-reset authorization: manual dispatch only, detached filesystems only, passed to the guard verbatim."""

    FS = ("fs-0123456789abcdef0", "fs-0123456789abcdef1")

    def _scripted(self, access_points=None):
        return (
            ScriptedSubprocess()
            .on(lambda argv: "managed-efs-inventory" in argv, _ok("[]"))
            .on(lambda argv: argv[:3] == ["aws", "sts", "assume-role"], _assume_role_ok())
            .on(lambda argv: argv[:3] == ["aws", "sts", "get-caller-identity"], _ok(WORKLOAD_ACCOUNT_ID + "\n"))
            .on(lambda argv: argv[:3] == ["aws", "efs", "describe-file-systems"], _ok(json.dumps({"FileSystems": []})))
            .on(lambda argv: argv[:3] == ["aws", "efs", "describe-access-points"], access_points or _ok(json.dumps({"AccessPoints": []})))
            .on(lambda argv: str(p1.EFS_INVENTORY_GUARD_TOOL) in argv, _ok("OK"))
        )

    def _env(self, ids, event):
        return {"AWS_REGION": "eu-west-1", "EKS_DEPLOY_ROLE_ARN": EKS_DEPLOY_ROLE_ARN, "INPUT_EFS_REPLACEMENT_AUTHORIZED_IDS": ids, "EVENT_NAME": event}

    def _guard_calls(self, scripted):
        return [c for c in scripted.calls if str(p1.EFS_INVENTORY_GUARD_TOOL) in c]

    def test_parse_accepts_only_manual_dispatch(self):
        self.assertEqual(p1.parse_efs_replacement_authorization("", "push"), [])
        self.assertEqual(p1.parse_efs_replacement_authorization(" ".join(self.FS), "workflow_dispatch"), list(self.FS))
        for raw, event in ((self.FS[0], "push"), (self.FS[0], "workflow_call"), (self.FS[0], ""), ("fs-bad", "workflow_dispatch"), (f"{self.FS[0]},{self.FS[0]}", "workflow_dispatch")):
            with self.subTest(raw=raw, event=event), self.assertRaises(p1.Phase1Error):
                p1.parse_efs_replacement_authorization(raw, event)

    def test_push_run_with_authorization_makes_no_aws_call(self):
        p1.update_state(self.state_path, {"effective_deploy": "true", "selected_environment": "dev"})
        scripted = self._scripted()
        with self.assertRaises(p1.Phase1Error):
            self.run_subcommand(p1.cmd_managed_efs_inventory, scripted=scripted, env_overrides=self._env(self.FS[0], "push"))
        self.assertEqual(scripted.calls, [])
        self.assertNotIn("managed_efs_inventory_completed", self.read_state())

    def test_normal_run_publishes_an_empty_authorization(self):
        p1.update_state(self.state_path, {"effective_deploy": "true", "selected_environment": "dev"})
        scripted = self._scripted()
        self.run_subcommand(p1.cmd_managed_efs_inventory, scripted=scripted, env_overrides=self._env("", "push"))
        self.assertEqual(self.read_outputs()["efs_replacement_authorized_ids"], "")
        self.assertEqual(self._guard_calls(scripted)[0][-1], "")
        self.assertFalse(any(c[:3] == ["aws", "efs", "describe-access-points"] for c in scripted.calls))

    def test_dispatch_authorization_is_checked_and_passed_to_the_guard(self):
        p1.update_state(self.state_path, {"effective_deploy": "true", "selected_environment": "dev"})
        scripted = self._scripted()
        self.run_subcommand(p1.cmd_managed_efs_inventory, scripted=scripted, env_overrides=self._env(",".join(self.FS), "workflow_dispatch"))
        self.assertEqual(self._guard_calls(scripted)[0][-1], ",".join(self.FS))
        self.assertEqual(self.read_outputs()["efs_replacement_authorized_ids"], ",".join(self.FS))
        checked = [c[c.index("--file-system-id") + 1] for c in scripted.calls if c[:3] == ["aws", "efs", "describe-access-points"]]
        self.assertEqual(checked, list(self.FS))

    def test_attached_or_unverifiable_filesystems_are_refused_before_the_guard(self):
        for label, handler in (
            ("access point present", _ok(json.dumps({"AccessPoints": [{"AccessPointId": "fsap-1"}]}))),
            ("malformed JSON", _ok("not json")),
            ("no AccessPoints list", _ok(json.dumps({"NextToken": "x"}))),
            ("denied", _fail(stderr="An error occurred (AccessDeniedException)", returncode=254)),
        ):
            with self.subTest(label=label):
                scripted = self._scripted(access_points=handler)
                with mock.patch.object(p1.subprocess, "run", scripted), self.assertRaises(p1.Phase1Error):
                    p1.verify_managed_efs_inventory("dev", "eu-west-1", EKS_DEPLOY_ROLE_ARN, list(self.FS))
                self.assertEqual(self._guard_calls(scripted), [])


# Four DEV runtimes: runtime -> (stale-token filesystem, CSI access point). Test data only.
CLEANUP_DEV = {
    "gg-postgresql-repltest-001": ("fs-0d1c409669a15bc3e", "fsap-0bd59bb3c3abcd78d"),
    "gg-mssql-repltest-001": ("fs-03eb68ea5f6317e54", "fsap-0a804c4697816195b"),
    "gg-oracle-repltest-002": ("fs-0abbe15be0c6fb64d", "fsap-0df58f1ff6912f3f1"),
    "gg-postgresql-repltest-002": ("fs-08505eae446b8c918", "fsap-0db2b806b3bdb6563"),
}
CLEANUP_FS = ",".join(fs for fs, _ap in CLEANUP_DEV.values())
CLEANUP_APS = ",".join(ap for _fs, ap in CLEANUP_DEV.values())


class FakeEfsCluster:
    """Stateful stand-in for aws efs/sts and read-only kubectl during an access-point cleanup run."""

    def __init__(self, kube=None, delete_error=None, stuck=()):
        self.access_points = {fs: [ap] for fs, ap in CLEANUP_DEV.values()}
        self.extra = {}
        self.kube = kube or {}
        self.delete_error = delete_error
        self.stuck = set(stuck)
        self.deleted = []
        self.scripted = (
            ScriptedSubprocess()
            .on(lambda argv: "managed-efs-inventory" in argv, _ok(json.dumps([{"deploymentId": rt, "efsCreationToken": f"dev-{rt}-efs"} for rt in CLEANUP_DEV])))
            .on(lambda argv: argv[:3] == ["aws", "sts", "assume-role"], _assume_role_ok())
            .on(lambda argv: argv[:3] == ["aws", "sts", "get-caller-identity"], _ok(WORKLOAD_ACCOUNT_ID + "\n"))
            .on(lambda argv: argv[:3] == ["aws", "efs", "describe-file-systems"], _ok(json.dumps({"FileSystems": [self._fs(rt, fs) for rt, (fs, _ap) in CLEANUP_DEV.items()]})))
            .on(lambda argv: argv[:3] == ["aws", "efs", "describe-access-points"], self._describe)
            .on(lambda argv: argv[:3] == ["aws", "efs", "delete-access-point"], self._delete)
            .on(lambda argv: argv[:2] == ["kubectl", "get"], self._kubectl)
        )

    @staticmethod
    def _fs(rt, fs):
        tags = {"ManagedBy": "goldengate-eks-app", "GoldenGateDeploymentId": rt, "GoldenGateEnvironment": "dev", "GoldenGateStorage": "u02", "Name": "legacy"}
        return {"FileSystemId": fs, "CreationToken": f"dev-{rt}-legacy-efs", "LifeCycleState": "available", "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}

    def _ap(self, fs, ap):
        return {"AccessPointId": ap, "FileSystemId": fs, "OwnerId": WORKLOAD_ACCOUNT_ID, "LifeCycleState": "available",
                "RootDirectory": {"Path": f"/old/{ap}"}, "Tags": [{"Key": "efs.csi.aws.com/cluster", "Value": "true"}]}

    def _describe(self, argv, env):
        if "--file-system-id" in argv:
            fs = argv[argv.index("--file-system-id") + 1]
            items = [self._ap(fs, ap) for ap in self.access_points.get(fs, [])] + self.extra.get(fs, [])
            return subprocess.CompletedProcess(argv, 0, json.dumps({"AccessPoints": items}), "")
        ap = argv[argv.index("--access-point-id") + 1]
        return subprocess.CompletedProcess(argv, 254, "", "An error occurred (AccessPointNotFound) when calling the DescribeAccessPoints operation")

    def _delete(self, argv, env):
        ap = argv[argv.index("--access-point-id") + 1]
        if self.delete_error:
            return subprocess.CompletedProcess(argv, 254, "", f"An error occurred ({self.delete_error}) when calling the DeleteAccessPoint operation")
        self.deleted.append(ap)
        if ap not in self.stuck:
            for aps in self.access_points.values():
                if ap in aps:
                    aps.remove(ap)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def _kubectl(self, argv, env):
        kind = argv[2]
        return subprocess.CompletedProcess(argv, 0, json.dumps({"items": self.kube.get(kind, [])}), "")


class TestEfsAccessPointCleanup(Phase1TestCase):
    """action=efs-access-point-cleanup: separate manual DEV-only run; deletes named CSI access points only, never a filesystem, never reaches Terraform."""

    def _env(self, **overrides):
        env = {"AWS_REGION": "eu-west-1", "EKS_DEPLOY_ROLE_ARN": EKS_DEPLOY_ROLE_ARN, "RUNTIME_NAMESPACE": "goldengate-dev", "ARGOCD_NAMESPACE": "argocd",
               "EVENT_NAME": "workflow_dispatch", "INPUT_ACTION": "efs-access-point-cleanup",
               "INPUT_EFS_REPLACEMENT_AUTHORIZED_IDS": CLEANUP_FS, "INPUT_EFS_ACCESS_POINT_CLEANUP_IDS": CLEANUP_APS}
        env.update(overrides)
        return env

    def _ready_state(self):
        p1.update_state(self.state_path, {"effective_deploy": "false", "efs_access_point_cleanup": "true", "selected_environment": "dev", "eks_preflight_completed": "true"})

    def _run(self, fake, **env):
        with mock.patch.object(p1.time, "sleep"):
            self.run_subcommand(p1.cmd_efs_access_point_cleanup, scripted=fake.scripted, env_overrides=self._env(**env))

    def _mutations(self, fake):
        return [c for c in fake.scripted.calls if c[:2] == ["aws", "efs"] and not c[2].startswith("describe")]

    def test_effective_deploy_enters_cleanup_mode_only_for_the_manual_action(self):
        p1.update_state(self.state_path, {"selected_environment": "dev"})
        self.run_subcommand(p1.cmd_effective_deploy, env_overrides=self._env())
        self.assertEqual(self.read_state()["effective_deploy"], "false")
        self.assertEqual(self.read_outputs()["efs_access_point_cleanup"], "true")

    def test_cleanup_inputs_are_rejected_in_every_other_mode(self):
        p1.update_state(self.state_path, {"selected_environment": "dev"})
        for event, action in (("push", ""), ("workflow_dispatch", "deploy"), ("workflow_dispatch", "validate")):
            with self.subTest(event=event, action=action), self.assertRaises(p1.Phase1Error):
                self.run_subcommand(p1.cmd_effective_deploy, env_overrides=self._env(EVENT_NAME=event, INPUT_ACTION=action))

    def test_cleanup_action_requires_scope_and_dev(self):
        for state_env, overrides in (("dev", {"INPUT_EFS_ACCESS_POINT_CLEANUP_IDS": ""}), ("dev", {"INPUT_EFS_REPLACEMENT_AUTHORIZED_IDS": ""}),
                                     ("dev", {"INPUT_EFS_ACCESS_POINT_CLEANUP_IDS": "fsap-bad"}), ("sit", {})):
            p1.update_state(self.state_path, {"selected_environment": state_env})
            with self.subTest(env=state_env, overrides=overrides), self.assertRaises(p1.Phase1Error):
                self.run_subcommand(p1.cmd_effective_deploy, env_overrides=self._env(**overrides))

    def test_other_modes_keep_the_cleanup_flag_false(self):
        self.run_subcommand(p1.cmd_effective_deploy, env_overrides={"EVENT_NAME": "push", "INPUT_ACTION": ""})
        self.assertEqual(self.read_outputs()["efs_access_point_cleanup"], "false")

    def test_cleanup_deletes_only_the_four_access_points_and_never_a_filesystem(self):
        self._ready_state()
        fake = FakeEfsCluster()
        self._run(fake)
        self.assertEqual(sorted(fake.deleted), sorted(CLEANUP_APS.split(",")))
        self.assertEqual({c[2] for c in self._mutations(fake)}, {"delete-access-point"})
        self.assertEqual(self.read_state()["efs_access_point_cleanup_completed"], "true")

    def test_retry_after_partial_deletion_is_idempotent(self):
        self._ready_state()
        fake = FakeEfsCluster()
        first_fs, first_ap = CLEANUP_DEV["gg-postgresql-repltest-001"]
        fake.access_points[first_fs] = []
        self._run(fake)
        self.assertNotIn(first_ap, fake.deleted)
        self.assertEqual(len(fake.deleted), 3)
        fake2 = FakeEfsCluster()
        fake2.access_points = {fs: [] for fs in fake2.access_points}
        self._run(fake2)
        self.assertEqual(fake2.deleted, [])

    def test_unknown_access_point_blocks_before_any_deletion(self):
        self._ready_state()
        fake = FakeEfsCluster()
        fs = CLEANUP_DEV["gg-oracle-repltest-002"][0]
        fake.access_points[fs].append("fsap-0000000000000000a")
        with self.assertRaises(p1.Phase1Error):
            self._run(fake)
        self.assertEqual(self._mutations(fake), [])

    def test_kubernetes_reference_blocks_before_any_aws_access_point_call(self):
        self._ready_state()
        fs, ap = CLEANUP_DEV["gg-mssql-repltest-001"]
        fake = FakeEfsCluster(kube={"pv": [{"metadata": {"name": "pv-1"}, "spec": {"csi": {"driver": "efs.csi.aws.com", "volumeHandle": f"{fs}::{ap}"}}}]})
        with self.assertRaises(p1.Phase1Error) as ctx:
            self._run(fake)
        self.assertIn("pv-1", str(ctx.exception))
        self.assertFalse(any(c[:3] in (["aws", "efs", "describe-access-points"], ["aws", "efs", "delete-access-point"]) for c in fake.scripted.calls))

    def test_kubectl_failure_is_never_read_as_no_references(self):
        self._ready_state()
        fake = FakeEfsCluster()
        fake.scripted._handlers.insert(0, (lambda argv: argv[:3] == ["kubectl", "get", "pods"], _fail(stderr="Forbidden")))
        with self.assertRaises(p1.Phase1Error):
            self._run(fake)
        self.assertEqual(self._mutations(fake), [])

    def test_canonical_or_unauthorized_filesystem_scope_is_rejected(self):
        self._ready_state()
        for fs_raw in ("fs-0123456789abcdef0", CLEANUP_FS + ",fs-0123456789abcdef0"):
            fake = FakeEfsCluster()
            with self.subTest(fs=fs_raw), self.assertRaises(p1.Phase1Error):
                self._run(fake, INPUT_EFS_REPLACEMENT_AUTHORIZED_IDS=fs_raw)
            self.assertEqual(self._mutations(fake), [])

    def test_denied_delete_stops_the_run(self):
        self._ready_state()
        fake = FakeEfsCluster(delete_error="AccessDeniedException")
        with self.assertRaises(p1.Phase1Error) as ctx:
            self._run(fake)
        self.assertIn("AccessDeniedException", str(ctx.exception))
        self.assertNotIn("efs_access_point_cleanup_completed", self.read_state())

    def test_access_points_that_never_disappear_fail_after_bounded_polling(self):
        self._ready_state()
        fake = FakeEfsCluster(stuck=CLEANUP_APS.split(",")[:1])
        with self.assertRaises(p1.Phase1Error):
            self._run(fake)
        polls = [c for c in fake.scripted.calls if c[:3] == ["aws", "efs", "describe-access-points"] and "--file-system-id" in c]
        self.assertEqual(len(polls), 4 * (1 + p1.EFS_ACCESS_POINT_CLEANUP_ATTEMPTS))

    def test_new_unauthorized_access_point_during_polling_fails(self):
        self._ready_state()
        fake = FakeEfsCluster()
        fs = CLEANUP_DEV["gg-postgresql-repltest-002"][0]
        original = fake._delete

        def delete_then_reprovision(argv, env):
            result = original(argv, env)
            fake.extra[fs] = [fake._ap(fs, "fsap-0000000000000000b")]
            return result
        fake.scripted._handlers.insert(0, (lambda argv: argv[:3] == ["aws", "efs", "delete-access-point"], delete_then_reprovision))
        with self.assertRaises(p1.Phase1Error):
            self._run(fake)

    def test_command_refuses_outside_cleanup_mode(self):
        for state in ({"effective_deploy": "true", "efs_access_point_cleanup": "true"}, {"effective_deploy": "false", "efs_access_point_cleanup": "false"},
                      {"effective_deploy": "false", "efs_access_point_cleanup": "true", "eks_preflight_completed": "false"}):
            p1.save_state(self.state_path, {"selected_environment": "dev", "eks_preflight_completed": "true", **state})
            fake = FakeEfsCluster()
            with self.subTest(state=state), self.assertRaises(p1.Phase1Error):
                self._run(fake)
            self.assertEqual(fake.scripted.calls, [])

    def test_acceptance_requires_cleanup_and_post_cleanup_inventory(self):
        base = {"selected_environment": "dev", "effective_deploy": "false", "efs_access_point_cleanup": "true", "has_active_deployments": "true", "has_changes": "false",
                "has_deletions": "false", "terraform_governance_override": "false", "active_runtime_matrix": "[]", "deployment_matrix": "[]", "deletion_matrix": "[]",
                "has_storage_transition_violations": "false", "storage_transition_violations": "[]", "eks_preflight_completed": "true"}
        p1.save_state(self.state_path, {**base, "efs_access_point_cleanup_completed": "true"})
        with self.assertRaises(p1.Phase1Error):
            p1.cmd_acceptance(self.args)
        p1.save_state(self.state_path, {**base, "efs_access_point_cleanup_completed": "true", "managed_efs_inventory_completed": "true"})
        p1.cmd_acceptance(self.args)
        p1.save_state(self.state_path, {**base, "efs_access_point_cleanup": "false", "efs_access_point_cleanup_completed": "true"})
        with self.assertRaises(p1.Phase1Error):
            p1.cmd_acceptance(self.args)


class TestEfsAccessPointCleanupWiring(unittest.TestCase):
    """The cleanup step exists only in MAIN's Phase 1, only for the manual cleanup action, before the inventory verification; Terraform can never follow it."""

    WORKFLOWS = REPO_ROOT / ".github" / "workflows"

    def _load(self, name):
        import yaml
        return yaml.safe_load((self.WORKFLOWS / name).read_text(encoding="utf-8"))

    def test_main_dispatch_inputs(self):
        inputs = (self._load("00-main-goldengate-orchestrator.yaml").get(True) or {})["workflow_dispatch"]["inputs"]
        self.assertIn(p1.EFS_ACCESS_POINT_CLEANUP_ACTION, inputs["action"]["options"])
        self.assertEqual(inputs["action"]["default"], "validate")
        self.assertEqual(inputs["efs_access_point_cleanup_ids"]["default"], "")

    def test_cleanup_step_is_gated_and_ordered_before_the_inventory_verification(self):
        jobs = self._load("00-main-goldengate-orchestrator.yaml")["jobs"]
        steps = jobs["validate_model"]["steps"]
        ids = [s.get("id") for s in steps]
        cleanup = steps[ids.index("efs_access_point_cleanup")]
        self.assertEqual(cleanup["if"], "${{ steps.compute_effective_deploy.outputs.efs_access_point_cleanup == 'true' }}")
        self.assertIn("phase1_readiness.py efs-access-point-cleanup", cleanup["run"])
        self.assertNotIn("continue-on-error", cleanup)
        self.assertLess(ids.index("efs_access_point_cleanup"), ids.index("managed_efs_inventory"))
        names = [s.get("name") for s in steps]
        self.assertLess(names.index("Verify live EKS cluster + OIDC + Kubernetes API access (read-only)"), ids.index("efs_access_point_cleanup"))
        tf = jobs["terraform_sync_once"]
        self.assertIn("needs.validate_model.outputs.effective_deploy == 'true'", tf["if"])
        self.assertIn("needs.validate_model.result == 'success'", tf["if"])

    def test_no_other_workflow_can_run_the_cleanup(self):
        for path in sorted(self.WORKFLOWS.glob("*.y*ml")):
            text = path.read_text(encoding="utf-8")
            if path.name != "00-main-goldengate-orchestrator.yaml":
                with self.subTest(workflow=path.name):
                    self.assertNotIn("efs-access-point-cleanup", text)
                    self.assertNotIn("efs_access_point_cleanup", text)

    def test_deploy_role_may_delete_only_csi_access_points(self):
        policy = json.loads((REPO_ROOT / "envs" / "dev" / "policies" / "goldengate-eks-deploy-dev" / "policies" / "policies_1.json").read_text())
        efs_writes = [(s, a) for s in policy["Statement"] for a in s["Action"] if a.startswith("elasticfilesystem:") and not a.split(":")[1].startswith("Describe")]
        self.assertEqual([a for _s, a in efs_writes], ["elasticfilesystem:DeleteAccessPoint"])
        statement = efs_writes[0][0]
        self.assertRegex(statement["Resource"], r"^arn:aws:elasticfilesystem:[a-z0-9-]+:[0-9]{12}:access-point/\*$")
        self.assertEqual(statement["Condition"], {"StringEquals": {"aws:ResourceTag/efs.csi.aws.com/cluster": "true"}})


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


class TestAppendGithubEnvRawHardening(Phase1TestCase):
    """append_github_env_raw() is defense-in-depth behind the now-hardened canonical automation/goldengate-environment.py github-env producer -- it must accept the real simple multi-record NAME=value payload that tool emits, and fail closed (writing zero bytes) on anything that does not match that simple-line contract."""

    def test_accepts_a_normal_multi_record_canonical_payload(self):
        payload = "AWS_REGION=eu-west-1\nGG_ENVIRONMENT=dev\n"
        p1.append_github_env_raw(payload)
        self.assertEqual(self.github_env.read_text(encoding="utf-8"), payload)

    def test_rejects_bare_cr(self):
        payload = "AWS_REGION=eu-west-1\rGG_ENVIRONMENT=dev\n"
        with self.assertRaises(p1.Phase1Error):
            p1.append_github_env_raw(payload)
        self.assertFalse(self.github_env.exists())

    def test_rejects_nul(self):
        payload = "AWS_REGION=eu-west-1\x00\n"
        with self.assertRaises(p1.Phase1Error):
            p1.append_github_env_raw(payload)
        self.assertFalse(self.github_env.exists())

    def test_rejects_unsafe_variable_names(self):
        for bad_payload in ("aws_region=eu-west-1\n", "1AWS_REGION=eu-west-1\n", "=eu-west-1\n", "AWS REGION=eu-west-1\n"):
            with self.assertRaises(p1.Phase1Error, msg=repr(bad_payload)):
                p1.append_github_env_raw(bad_payload)
            self.assertFalse(self.github_env.exists(), msg=repr(bad_payload))

    def test_rejects_malformed_lines_and_embedded_blank_lines(self):
        for bad_payload in ("not a valid line at all\n", "AWS_REGION=eu-west-1\n\nGG_ENVIRONMENT=dev\n", "AWS_REGION\n"):
            with self.assertRaises(p1.Phase1Error, msg=repr(bad_payload)):
                p1.append_github_env_raw(bad_payload)
            self.assertFalse(self.github_env.exists(), msg=repr(bad_payload))

    def test_rejects_heredoc_syntax(self):
        payload = "AWS_REGION<<GG_EOF\neu-west-1\nGG_EOF\n"
        with self.assertRaises(p1.Phase1Error):
            p1.append_github_env_raw(payload)
        self.assertFalse(self.github_env.exists())

    def test_writes_zero_bytes_when_validation_fails(self):
        p1.append_github_env_raw("AWS_REGION=eu-west-1\n")
        before = self.github_env.read_text(encoding="utf-8")
        with self.assertRaises(p1.Phase1Error):
            p1.append_github_env_raw("AWS_REGION=eu-west-1\rROGUE=injected\n")
        after = self.github_env.read_text(encoding="utf-8")
        self.assertEqual(before, after, "a failed append must not add any bytes to an already-existing file")

    def test_accepts_the_real_canonical_environment_tool_output(self):
        import subprocess

        environment_tool = p1.REPO_ROOT / "automation" / "goldengate-environment.py"
        proc = subprocess.run([sys.executable, str(environment_tool), "--environment", "dev", "github-env"], capture_output=True, text=True, check=True)
        p1.append_github_env_raw(proc.stdout)
        self.assertIn("AWS_REGION=", self.github_env.read_text(encoding="utf-8"))


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
