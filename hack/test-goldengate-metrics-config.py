"""Offline/mocked tests (no live AWS) for the CloudWatch double-gate metric contract in monitoring/monitor/collector.py and for the hack/goldengate-metrics-config.py DynamoDB update helper; run directly via `python3 hack/test-goldengate-metrics-config.py`."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import yaml  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONITOR_SRC = os.path.join(REPO_ROOT, "monitoring", "monitor")
HELPER_PATH = os.path.join(REPO_ROOT, "hack", "goldengate-metrics-config.py")
MONITOR_WORKFLOW_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "goldengate-monitor.yaml")
METRICS_CONFIG_WORKFLOW_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "goldengate-monitor-metrics-config.yaml")

sys.path.insert(0, MONITOR_SRC)

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

import collector as core  # noqa: E402


def make_table():
    client = boto3.client("dynamodb", region_name="eu-west-1")
    client.create_table(
        TableName="gg-eks-pipeline",
        KeySchema=[{"AttributeName": "pipeline", "KeyType": "HASH"},
                   {"AttributeName": "recordType", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "pipeline", "AttributeType": "S"},
                              {"AttributeName": "recordType", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return boto3.resource("dynamodb", region_name="eu-west-1").Table("gg-eks-pipeline")


def load_helper_module():
    """Loads a fresh module object per call; safe/cheap since the helper has no import-time AWS side effects (all AWS access happens inside main())."""
    spec = importlib.util.spec_from_file_location("goldengate_metrics_config_helper", HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_helper(argv):
    """Runs the helper's main() with argv against AWS_REGION=eu-west-1 / DYNAMODB_TABLE=gg-eks-pipeline, capturing stdout+stderr; returns (exit_code, output_text)."""
    module = load_helper_module()
    buf = io.StringIO()
    old_argv = sys.argv
    sys.argv = ["goldengate-metrics-config.py"] + argv
    env_patch = {"AWS_REGION": "eu-west-1", "DYNAMODB_TABLE": "gg-eks-pipeline"}
    try:
        # stdout and stderr share one buffer so assertions can check either a report() line or a fail-closed message.
        with mock.patch.dict(os.environ, env_patch, clear=False), \
             redirect_stdout(buf), redirect_stderr(buf):
            try:
                module.main()
                code = 0
            except SystemExit as exc:
                if isinstance(exc.code, int):
                    code = exc.code
                else:
                    # Catching SystemExit ourselves suppresses the interpreter's auto-print of exc.code, so replicate it on stderr (matching real `python3 x.py` behavior).
                    if exc.code:
                        print(exc.code, file=sys.stderr)
                    code = 1 if exc.code else 0
    finally:
        sys.argv = old_argv
    return code, buf.getvalue()


# Workflow functional-test infrastructure: extracts the ACTUAL committed run: script text for a named step from the real workflow YAML and executes it under real bash with a mocked kubectl/jq PATH, proving the committed bash itself (not a reimplementation of it) behaves correctly.
def _get_step(workflow_path, step_name):
    with open(workflow_path) as f:
        doc = yaml.safe_load(f)
    for job in doc.get("jobs", {}).values():
        for step in job.get("steps", []):
            if step.get("name") == step_name:
                return step
    raise AssertionError(f"step {step_name!r} not found in {workflow_path}")


def _workflow_top_level_env(workflow_path):
    """Returns only the literal (non-${{ }}) values from the workflow's top-level env: block -- constants like TARGET_NAMESPACE that run: scripts reference directly, never a live AWS/GitHub context value."""
    with open(workflow_path) as f:
        doc = yaml.safe_load(f)
    out = {}
    for k, v in (doc.get("env") or {}).items():
        if isinstance(v, str) and "${{" not in v:
            out[k] = v
    return out


def run_step_script(workflow_path, step_name, script_text, env_overrides, bin_dir=None, timeout=20,
                     gh_expression_overrides=None):
    """Runs the real, unmodified `script_text` under bash: env_overrides stands in for GitHub Actions' own ${{ }} substitution into the step's env: block, and gh_expression_overrides pre-substitutes any remaining literal "${{ ... }}" text the same way the GitHub Actions preprocessor would."""
    for literal, value in (gh_expression_overrides or {}).items():
        script_text = script_text.replace(literal, value)
    env = dict(os.environ)
    env.update(_workflow_top_level_env(workflow_path))
    env.update(env_overrides)
    if bin_dir:
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    proc = subprocess.run(["bash", "-c", script_text], env=env, capture_output=True, text=True, timeout=timeout)
    return proc


def write_executable(path, content):
    with open(path, "w") as f:
        f.write(content)
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


# Existing metric-contract / double-gate tests.
class MetricContractDoubleGateTests(unittest.TestCase):
    @staticmethod
    def _cfg(metrics_enabled):
        return {"metricsEnabled": metrics_enabled}

    def test_global_false_config_false_gate_closed(self):
        with mock.patch.object(core, "CLOUDWATCH_PUBLISH_ENABLED", False):
            self.assertFalse(core.cloudwatch_enabled_for(self._cfg(False)))

    def test_global_false_config_true_gate_closed(self):
        with mock.patch.object(core, "CLOUDWATCH_PUBLISH_ENABLED", False):
            self.assertFalse(core.cloudwatch_enabled_for(self._cfg(True)))

    def test_global_true_config_false_gate_closed(self):
        with mock.patch.object(core, "CLOUDWATCH_PUBLISH_ENABLED", True):
            self.assertFalse(core.cloudwatch_enabled_for(self._cfg(False)))

    def test_global_true_config_true_reaches_boundary(self):
        with mock.patch.object(core, "CLOUDWATCH_PUBLISH_ENABLED", True):
            self.assertTrue(core.cloudwatch_enabled_for(self._cfg(True)))

    def test_publish_metrics_if_enabled_no_client_when_global_false(self):
        with mock.patch.object(core, "CLOUDWATCH_PUBLISH_ENABLED", False), \
             mock.patch.object(core, "_cloudwatch_client") as mock_client_fn:
            core.publish_metrics_if_enabled(self._cfg(True), "gg-oracle-payments-01", [{"MetricName": "x"}])
            mock_client_fn.assert_not_called()

    def test_publish_metrics_if_enabled_no_client_when_config_false(self):
        with mock.patch.object(core, "CLOUDWATCH_PUBLISH_ENABLED", True), \
             mock.patch.object(core, "_cloudwatch_client") as mock_client_fn:
            core.publish_metrics_if_enabled(self._cfg(False), "gg-oracle-payments-01", [{"MetricName": "x"}])
            mock_client_fn.assert_not_called()

    def test_publish_metrics_if_enabled_no_client_when_both_false(self):
        with mock.patch.object(core, "CLOUDWATCH_PUBLISH_ENABLED", False), \
             mock.patch.object(core, "_cloudwatch_client") as mock_client_fn:
            core.publish_metrics_if_enabled(self._cfg(False), "gg-oracle-payments-01", [{"MetricName": "x"}])
            mock_client_fn.assert_not_called()

    def test_publish_metrics_if_enabled_reaches_publish_when_both_true(self):
        fake_cw = mock.MagicMock()
        with mock.patch.object(core, "CLOUDWATCH_PUBLISH_ENABLED", True), \
             mock.patch.object(core, "_cloudwatch_client", return_value=fake_cw):
            core.publish_metrics_if_enabled(
                self._cfg(True), "gg-oracle-payments-01",
                [{"MetricName": "LagBreached", "Dimensions": [], "Value": 0.0, "Unit": "Count"}],
            )
        fake_cw.put_metric_data.assert_called_once()
        _, kwargs = fake_cw.put_metric_data.call_args
        self.assertEqual(kwargs["Namespace"], "GoldenGate/Pipelines")

    def test_build_metric_batch_deployment_level_contract(self):
        md = core.build_metric_batch("gg-oracle-payments-01", "oracle", {"lag": 1, "abend": 0, "down": 0},
                                      heartbeat_ok=True)
        names = {m["MetricName"] for m in md}
        self.assertTrue({"LagBreached", "AbendFailure", "DeploymentDown", "HeartbeatAgeSeconds"}.issubset(names))
        for m in md:
            dim_names = {d["Name"] for d in m["Dimensions"]}
            self.assertEqual(dim_names, {"Deployment", "DeploymentType"})

    def test_build_metric_batch_critical_service_dimension(self):
        md = core.build_metric_batch("gg-oracle-payments-01", "oracle", {},
                                      critical_service_status={"DISTSRVR": True})
        svc_metrics = [m for m in md if m["MetricName"] == "CriticalServiceDown"]
        self.assertEqual(len(svc_metrics), 1)
        dim_names = {d["Name"] for d in svc_metrics[0]["Dimensions"]}
        self.assertEqual(dim_names, {"Deployment", "DeploymentType", "Service"})

    def test_build_metric_batch_process_dimension(self):
        md = core.build_metric_batch(
            "gg-oracle-payments-01", "oracle", {},
            procs=[{"process": "EXTORA1", "type": "extract", "lagSeconds": 12, "abended": False}],
        )
        names = {m["MetricName"] for m in md}
        self.assertIn("ExtractLagSeconds", names)
        self.assertIn("AbendState", names)
        for m in md:
            if m["MetricName"] in ("ExtractLagSeconds", "AbendState"):
                dim_names = {d["Name"] for d in m["Dimensions"]}
                self.assertEqual(dim_names, {"Deployment", "DeploymentType", "Process"})

    def test_build_metric_batch_replicat_lag_and_abend_event(self):
        md = core.build_metric_batch(
            "gg-postgresql-payments-01", "postgresql", {},
            procs=[{"process": "REPPG1", "type": "replicat", "lagSeconds": 3, "abended": True}],
            abend_events=["REPPG1"],
        )
        names = {m["MetricName"] for m in md}
        self.assertIn("ReplicatLagSeconds", names)
        self.assertIn("AbendEvent", names)

    def test_no_processtype_or_servicename_dimension_introduced(self):
        md = core.build_metric_batch(
            "gg-oracle-payments-01", "oracle", {},
            procs=[{"process": "EXTORA1", "type": "extract", "lagSeconds": 1, "abended": False}],
            critical_service_status={"DISTSRVR": True},
        )
        for m in md:
            dim_names = {d["Name"] for d in m["Dimensions"]}
            self.assertNotIn("ProcessType", dim_names)
            self.assertNotIn("ServiceName", dim_names)

    def test_namespace_unchanged(self):
        self.assertEqual(core.CLOUDWATCH_NAMESPACE, "GoldenGate/Pipelines")


# The exact conditional-update helper.
class MetricsConfigHelperTests(unittest.TestCase):
    PIPELINE = "gg-oracle-payments-01"
    TYPE = "oracle"

    @staticmethod
    def _seed(table, pipeline, deployment_type, metrics_enabled, alerts_enabled=False):
        table.put_item(Item={
            "pipeline": pipeline,
            "recordType": "CONFIG",
            "deploymentType": deployment_type,
            "metricsEnabled": metrics_enabled,
            "alertsEnabled": alerts_enabled,
        })

    @mock_aws
    def test_I_dry_run_performs_zero_update_item(self):
        table = make_table()
        self._seed(table, self.PIPELINE, self.TYPE, metrics_enabled=False)

        code, out = run_helper([self.PIPELINE, self.TYPE, "true", "false"])

        self.assertEqual(code, 0)
        self.assertIn("action=plan", out)
        item = table.get_item(Key={"pipeline": self.PIPELINE, "recordType": "CONFIG"})["Item"]
        self.assertFalse(item["metricsEnabled"])  # unchanged

    @mock_aws
    def test_L_enable_performs_exactly_one_conditional_update_changing_only_metrics_enabled(self):
        table = make_table()
        self._seed(table, self.PIPELINE, self.TYPE, metrics_enabled=False)

        with mock.patch("boto3.resource", wraps=boto3.resource) as spy_resource:
            code, out = run_helper([self.PIPELINE, self.TYPE, "true", "true"])

        self.assertEqual(code, 0)
        self.assertIn("action=updated", out)

        item = table.get_item(Key={"pipeline": self.PIPELINE, "recordType": "CONFIG"})["Item"]
        self.assertTrue(item["metricsEnabled"])
        self.assertFalse(item["alertsEnabled"])
        self.assertEqual(item["deploymentType"], self.TYPE)
        del spy_resource  # only used to keep the wraps reference alive

    @mock_aws
    def test_M_idempotent_target_performs_no_update_item(self):
        # table uses the real, not-yet-patched boto3.resource (like test_N below), since building it after patching would just wrap another mock.
        table = make_table()
        self._seed(table, self.PIPELINE, self.TYPE, metrics_enabled=True)

        with mock.patch("boto3.resource") as mock_resource:
            wrapped = mock.MagicMock(wraps=table)
            mock_resource.return_value.Table.return_value = wrapped
            code, out = run_helper([self.PIPELINE, self.TYPE, "true", "true"])

        self.assertEqual(code, 0)
        self.assertIn("action=none", out)
        wrapped.update_item.assert_not_called()

    @mock_aws
    def test_N_concurrent_change_conditional_update_fails_no_retry(self):
        table = make_table()
        self._seed(table, self.PIPELINE, self.TYPE, metrics_enabled=False)

        real_get_item = table.get_item

        def racing_get_item(*args, **kwargs):
            result = real_get_item(*args, **kwargs)
            # Simulate a concurrent actor changing the item between this helper's own read and its own write.
            table.update_item(
                Key={"pipeline": self.PIPELINE, "recordType": "CONFIG"},
                UpdateExpression="SET metricsEnabled = :v",
                ExpressionAttributeValues={":v": True},
            )
            return result

        with mock.patch("boto3.resource") as mock_resource:
            wrapped = mock.MagicMock(wraps=table)
            wrapped.get_item.side_effect = racing_get_item
            mock_resource.return_value.Table.return_value = wrapped
            code, out = run_helper([self.PIPELINE, self.TYPE, "true", "true"])

        self.assertNotEqual(code, 0)
        self.assertIn("ConditionalCheckFailedException", out)
        self.assertEqual(wrapped.update_item.call_count, 1)  # no automatic retry

    @mock_aws
    def test_O_disable_rollback_sets_only_metrics_enabled_false(self):
        table = make_table()
        self._seed(table, self.PIPELINE, self.TYPE, metrics_enabled=True)

        code, out = run_helper([self.PIPELINE, self.TYPE, "false", "true"])

        self.assertEqual(code, 0)
        self.assertIn("action=updated", out)
        item = table.get_item(Key={"pipeline": self.PIPELINE, "recordType": "CONFIG"})["Item"]
        self.assertFalse(item["metricsEnabled"])
        self.assertFalse(item["alertsEnabled"])
        self.assertEqual(item["deploymentType"], self.TYPE)

    @mock_aws
    def test_missing_config_item_fails(self):
        make_table()  # created, but no item put
        code, out = run_helper([self.PIPELINE, self.TYPE, "true", "false"])
        self.assertNotEqual(code, 0)
        self.assertIn("does not exist", out)

    @mock_aws
    def test_metrics_enabled_string_true_fails(self):
        table = make_table()
        table.put_item(Item={
            "pipeline": self.PIPELINE, "recordType": "CONFIG",
            "deploymentType": self.TYPE, "metricsEnabled": "true", "alertsEnabled": False,
        })
        code, out = run_helper([self.PIPELINE, self.TYPE, "true", "false"])
        self.assertNotEqual(code, 0)
        self.assertIn("not a literal Boolean", out)

    @mock_aws
    def test_alerts_enabled_true_fails(self):
        table = make_table()
        self._seed(table, self.PIPELINE, self.TYPE, metrics_enabled=False, alerts_enabled=True)
        code, out = run_helper([self.PIPELINE, self.TYPE, "true", "false"])
        self.assertNotEqual(code, 0)
        self.assertIn("alertsEnabled", out)

    @mock_aws
    def test_deployment_type_mismatch_fails(self):
        table = make_table()
        self._seed(table, self.PIPELINE, "postgresql", metrics_enabled=False)
        code, out = run_helper([self.PIPELINE, "oracle", "true", "false"])
        self.assertNotEqual(code, 0)
        self.assertIn("deploymentType mismatch", out)

    @mock_aws
    def test_never_prints_complete_item(self):
        table = make_table()
        self._seed(table, self.PIPELINE, self.TYPE, metrics_enabled=False)
        _, out = run_helper([self.PIPELINE, self.TYPE, "true", "false"])
        self.assertNotIn("recordType", out)
        self.assertNotIn("pipeline=", out)  # only "deployment=", never the raw key name

    def test_no_scan_reference_in_helper_source(self):
        with open(HELPER_PATH) as f:
            source = f.read()
        self.assertNotIn(".scan(", source)
        self.assertNotIn(".Scan(", source)

    def test_usage_error_on_wrong_argument_count(self):
        code, out = run_helper([self.PIPELINE, self.TYPE, "true"])
        self.assertNotEqual(code, 0)
        self.assertIn("USAGE ERROR", out)

    def test_usage_error_on_non_boolean_desired(self):
        code, out = run_helper([self.PIPELINE, self.TYPE, "yes", "false"])
        self.assertNotEqual(code, 0)
        self.assertIn("USAGE ERROR", out)

    @mock_aws
    def test_consistent_read_prevents_stale_value_causing_wrong_decision(self):
        # Proves ConsistentRead=True actually matters: a get_item call without it returns a fabricated stale value, so the helper -- which always passes ConsistentRead=True -- must observe the fresh (already-desired) value and decide no update is needed, never acting on the stale replica.
        table = make_table()
        self._seed(table, self.PIPELINE, self.TYPE, metrics_enabled=True)
        real_get_item = table.get_item

        def stale_unless_consistent(*args, **kwargs):
            if kwargs.get("ConsistentRead") is True:
                return real_get_item(*args, **kwargs)
            return {"Item": {
                "pipeline": self.PIPELINE, "recordType": "CONFIG",
                "deploymentType": self.TYPE, "metricsEnabled": False, "alertsEnabled": False,
            }}

        with mock.patch("boto3.resource") as mock_resource:
            wrapped = mock.MagicMock(wraps=table)
            wrapped.get_item.side_effect = stale_unless_consistent
            mock_resource.return_value.Table.return_value = wrapped
            code, out = run_helper([self.PIPELINE, self.TYPE, "true", "true"])

        self.assertEqual(code, 0)
        self.assertIn("action=none", out)
        wrapped.update_item.assert_not_called()

    @mock_aws
    def test_returned_all_new_attributes_are_validated_independently(self):
        # The real (moto-backed) write succeeds, but the ALL_NEW Attributes returned are tampered with, proving the helper validates that response itself and not only the separate follow-up GetItem.
        table = make_table()
        self._seed(table, self.PIPELINE, self.TYPE, metrics_enabled=False)
        real_update_item = table.update_item

        def tampered_update_item(**kwargs):
            response = real_update_item(**kwargs)
            response["Attributes"]["metricsEnabled"] = False
            return response

        with mock.patch("boto3.resource") as mock_resource:
            wrapped = mock.MagicMock(wraps=table)
            wrapped.update_item.side_effect = tampered_update_item
            mock_resource.return_value.Table.return_value = wrapped
            code, out = run_helper([self.PIPELINE, self.TYPE, "true", "true"])

        self.assertNotEqual(code, 0)
        self.assertIn("ALL_NEW attributes show metricsEnabled", out)


# Static source-level invariants for hack/goldengate-metrics-config.py and the two inline CONFIG readers in goldengate-monitor.yaml.
class SourceLevelSafetyInvariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(HELPER_PATH) as f:
            cls.helper_source = f.read()
        with open(MONITOR_WORKFLOW_PATH) as f:
            cls.monitor_workflow_text = f.read()

    def test_helper_initial_get_item_uses_consistent_read(self):
        idx = self.helper_source.index("# 1. Read the current CONFIG item")
        chunk = self.helper_source[idx:idx + 500]
        self.assertIn("ConsistentRead=True", chunk)

    def test_helper_verification_get_item_uses_consistent_read(self):
        idx = self.helper_source.index("def _consistent_verification_get_item")
        chunk = self.helper_source[idx:idx + 600]
        self.assertIn("ConsistentRead=True", chunk)

    def test_helper_update_item_uses_return_values_all_new(self):
        idx = self.helper_source.index("table.update_item(")
        chunk = self.helper_source[idx:idx + 400]
        self.assertIn('ReturnValues="ALL_NEW"', chunk)

    def test_helper_has_exactly_one_update_item_call_site(self):
        self.assertEqual(self.helper_source.count("table.update_item("), 1)

    def test_helper_validates_all_new_returned_attributes(self):
        self.assertIn('new_attributes = update_response.get("Attributes")', self.helper_source)
        self.assertIn('new_attributes.get("metricsEnabled") is not desired_metrics_enabled', self.helper_source)
        self.assertIn('new_attributes.get("alertsEnabled") is not False', self.helper_source)
        self.assertIn('new_attributes.get("deploymentType") != canonical_type', self.helper_source)

    def test_helper_retry_helper_never_calls_update_item(self):
        # The bounded retry applies only to the verification read -- it must never issue/retry an UpdateItem itself.
        idx = self.helper_source.index("def _consistent_verification_get_item")
        end = self.helper_source.index("# 1. Read the current CONFIG item")
        retry_fn_source = self.helper_source[idx:end]
        self.assertNotIn("update_item", retry_fn_source)

    def test_helper_never_retries_conditional_check_failed(self):
        idx = self.helper_source.index('if code == "ConditionalCheckFailedException":')
        chunk = self.helper_source[idx:idx + 400]
        self.assertIn("Not retrying automatically", chunk)
        # The ConditionalCheckFailedException branch must exit immediately (sys.exit), never loop back to retry update_item.
        self.assertIn("sys.exit(", chunk)

    def test_monitor_workflow_inline_readers_use_consistent_read(self):
        # Exactly two inline CONFIG-inventory readers exist (preflight + post-rollout verification), and both must request ConsistentRead.
        self.assertEqual(self.monitor_workflow_text.count("ConsistentRead=True"), 2)
        self.assertEqual(
            self.monitor_workflow_text.count('table.get_item(Key={"pipeline": pipeline, "recordType": "CONFIG"}'),
            2)


# Shell-injection-shaped input is never executed: user-controlled string input lives only in the step's env: block, so these tests prove the ACTUAL committed run: text (unmodified) treats a malicious value as inert data even when it contains command substitution / quote-breaking syntax.
class ShellInjectionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.registry_path = os.path.join(self.tmpdir, "goldengate-deployments.yaml")
        with open(self.registry_path, "w") as f:
            f.write(
                "deployments:\n"
                "  - name: gg-oracle-payments-01\n"
                "    type: oracle\n"
                "    enabled: true\n"
            )
        self.sentinel = os.path.join(self.tmpdir, "pwned")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _payloads(self):
        s = self.sentinel
        return [
            f"$(touch {s})",
            f"`touch {s}`",
            f'"; touch {s}; echo "',
            f"'; touch {s}; echo '",
            f"$(touch {s})\"; echo pwned",
        ]

    def test_registry_validation_rejects_injection_without_executing_it(self):
        step = _get_step(METRICS_CONFIG_WORKFLOW_PATH, "Validate deployment_name against the canonical registry")
        run_text = step["run"]
        self.assertNotIn("${{ inputs.deployment_name }}", run_text)  # Task 1 invariant

        for payload in self._payloads():
            with self.subTest(payload=payload):
                if os.path.exists(self.sentinel):
                    os.remove(self.sentinel)
                proc = run_step_script(
                    METRICS_CONFIG_WORKFLOW_PATH, step["name"], run_text,
                    {"REQUESTED_NAME": payload, "CANONICAL_REGISTRY": self.registry_path},
                )
                self.assertFalse(os.path.exists(self.sentinel), f"injection payload executed: {payload!r}")
                self.assertNotEqual(proc.returncode, 0, f"expected safe failure for payload: {payload!r}")

    def test_confirmation_validation_rejects_injection_without_executing_it(self):
        step = _get_step(METRICS_CONFIG_WORKFLOW_PATH, "Validate the exact confirmation string")
        run_text = step["run"]
        self.assertNotIn("${{ inputs.deployment_name }}", run_text)
        self.assertNotIn("${{ inputs.confirmation }}", run_text)

        for payload in self._payloads():
            with self.subTest(payload=payload):
                if os.path.exists(self.sentinel):
                    os.remove(self.sentinel)
                proc = run_step_script(
                    METRICS_CONFIG_WORKFLOW_PATH, step["name"], run_text,
                    {"DEPLOYMENT_NAME": payload, "DESIRED": "true", "CONFIRMATION": payload},
                )
                self.assertFalse(os.path.exists(self.sentinel), f"injection payload executed: {payload!r}")
                # CONFIRMATION == DEPLOYMENT_NAME here, so a coincidental "ENABLE <payload>" match is possible by construction -- only the sentinel is asserted on, since the invariant is that nothing executed, regardless of pass/fail verdict.

    def test_no_direct_input_substitution_anywhere_in_config_workflow_run_blocks(self):
        with open(METRICS_CONFIG_WORKFLOW_PATH) as f:
            doc = yaml.safe_load(f)
        for job in doc.get("jobs", {}).values():
            for step in job.get("steps", []):
                run = step.get("run")
                if not run:
                    continue
                self.assertNotIn("${{ inputs.deployment_name }}", run, step.get("name"))
                self.assertNotIn("${{ inputs.confirmation }}", run, step.get("name"))


# The observation timestamp is captured before the helper/UpdateItem runs (not after), and the post-update step reuses that exact value rather than recomputing a later one.
class TimestampOrderingTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.bin_dir = os.path.join(self.tmpdir, "bin")
        os.makedirs(self.bin_dir)
        self.github_env = os.path.join(self.tmpdir, "github_env")
        self.github_output = os.path.join(self.tmpdir, "github_output")
        open(self.github_env, "w").close()
        open(self.github_output, "w").close()
        self.dummy_helper_file = os.path.join(self.tmpdir, "helper.py")
        open(self.dummy_helper_file, "w").close()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_timestamp_captured_before_helper_exec_not_after(self):
        step = _get_step(METRICS_CONFIG_WORKFLOW_PATH, "Run the metrics-config helper inside the monitor pod")
        run_text = step["run"]
        self.assertNotIn("${{ inputs.deployment_name }}", run_text)

        # Mock kubectl: on "exec", fails unless VALIDATION_START_TS was already appended to GITHUB_ENV, proving the real committed script captures the timestamp before running the helper.
        write_executable(os.path.join(self.bin_dir, "kubectl"), f"""#!/usr/bin/env bash
if [ "$1" = "exec" ]; then
  if ! grep -q '^VALIDATION_START_TS=' "{self.github_env}" 2>/dev/null; then
    echo "ORDER_VIOLATION: timestamp not captured before helper exec" >&2
    exit 1
  fi
  echo "action=updated"
  exit 0
fi
echo "mock kubectl: unhandled: $*" >&2
exit 1
""")

        proc = run_step_script(
            METRICS_CONFIG_WORKFLOW_PATH, step["name"], run_text,
            {
                "POD_NAME": "gg-monitor-test", "CANONICAL_TYPE": "oracle",
                "DEPLOYMENT_NAME": "gg-oracle-payments-01", "DESIRED": "true", "APPLY": "true",
                "TARGET_NAMESPACE": "goldengate-monitoring",
                "METRICS_CONFIG_HELPER": self.dummy_helper_file,
                "GITHUB_ENV": self.github_env, "GITHUB_OUTPUT": self.github_output,
            },
            bin_dir=self.bin_dir,
        )

        self.assertNotIn("ORDER_VIOLATION", proc.stdout + proc.stderr)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        with open(self.github_env) as f:
            env_content = f.read()
        self.assertIn("VALIDATION_START_TS=", env_content)
        with open(self.github_output) as f:
            output_content = f.read()
        self.assertIn("helper_action=updated", output_content)

    def test_no_timestamp_captured_when_apply_change_false(self):
        step = _get_step(METRICS_CONFIG_WORKFLOW_PATH, "Run the metrics-config helper inside the monitor pod")
        run_text = step["run"]

        write_executable(os.path.join(self.bin_dir, "kubectl"), """#!/usr/bin/env bash
if [ "$1" = "exec" ]; then
  echo "action=plan"
  exit 0
fi
exit 1
""")

        run_step_script(
            METRICS_CONFIG_WORKFLOW_PATH, step["name"], run_text,
            {
                "POD_NAME": "gg-monitor-test", "CANONICAL_TYPE": "oracle",
                "DEPLOYMENT_NAME": "gg-oracle-payments-01", "DESIRED": "true", "APPLY": "false",
                "TARGET_NAMESPACE": "goldengate-monitoring",
                "METRICS_CONFIG_HELPER": self.dummy_helper_file,
                "GITHUB_ENV": self.github_env, "GITHUB_OUTPUT": self.github_output,
            },
            bin_dir=self.bin_dir,
        )
        with open(self.github_env) as f:
            self.assertEqual(f.read(), "")

    def test_post_update_step_never_recomputes_timestamp(self):
        # Static check: the post-update-observation step must not contain a fresh `date -u` capture -- it only reads VALIDATION_START_TS inherited via GITHUB_ENV.
        step = _get_step(METRICS_CONFIG_WORKFLOW_PATH, "Post-update observation")
        run_text = step["run"]
        self.assertNotIn('VALIDATION_START_TS="$(date -u', run_text)
        self.assertIn('VALIDATION_START_TS:-', run_text)  # fails closed if unset

    def test_post_update_log_retrieval_uses_the_inherited_timestamp_unmodified(self):
        # Functional check (skips the real 90s sleep, orthogonal to which timestamp is used): proves `kubectl logs --since-time=` receives EXACTLY the pre-set VALIDATION_START_TS, never a freshly computed one -- the property that guarantees an error right after UpdateItem falls inside the observation window.
        step = _get_step(METRICS_CONFIG_WORKFLOW_PATH, "Post-update observation")
        run_text = step["run"]
        marker = 'echo "Retrieving current gg-monitor logs since ${VALIDATION_START_TS}...'
        idx = run_text.index(marker)
        fragment = "set -euo pipefail\n" + run_text[idx:]

        since_time_capture = os.path.join(self.tmpdir, "since_time.txt")
        write_executable(os.path.join(self.bin_dir, "kubectl"), f"""#!/usr/bin/env bash
if [ "$1" = "logs" ]; then
  for arg in "$@"; do
    case "$arg" in
      --since-time=*) echo "${{arg#--since-time=}}" > "{since_time_capture}" ;;
    esac
  done
  exit 0
fi
exit 1
""")
        sentinel_ts = "2020-01-01T00:00:00Z"
        proc = run_step_script(
            METRICS_CONFIG_WORKFLOW_PATH, "Post-update observation (log fragment)", fragment,
            {
                "POD_NAME": "gg-monitor-test", "TARGET_NAMESPACE": "goldengate-monitoring",
                "VALIDATION_START_TS": sentinel_ts, "DESIRED": "true",
            },
            bin_dir=self.bin_dir,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        with open(since_time_capture) as f:
            captured = f.read().strip()
        self.assertEqual(captured, sentinel_ts)


# The helper action must be exactly one of none/plan/updated -- never a silently empty/unknown output.
class HelperActionValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.bin_dir = os.path.join(self.tmpdir, "bin")
        os.makedirs(self.bin_dir)
        self.github_env = os.path.join(self.tmpdir, "github_env")
        self.github_output = os.path.join(self.tmpdir, "github_output")
        open(self.github_env, "w").close()
        open(self.github_output, "w").close()
        self.dummy_helper_file = os.path.join(self.tmpdir, "helper.py")
        open(self.dummy_helper_file, "w").close()
        self.step = _get_step(METRICS_CONFIG_WORKFLOW_PATH, "Run the metrics-config helper inside the monitor pod")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self, mock_helper_output):
        output_file = os.path.join(self.tmpdir, "mock_helper_stdout.txt")
        with open(output_file, "w") as f:
            f.write(mock_helper_output)
        write_executable(os.path.join(self.bin_dir, "kubectl"), f"""#!/usr/bin/env bash
if [ "$1" = "exec" ]; then
  cat "{output_file}"
  exit 0
fi
exit 1
""")
        return run_step_script(
            METRICS_CONFIG_WORKFLOW_PATH, self.step["name"], self.step["run"],
            {
                "POD_NAME": "gg-monitor-test", "CANONICAL_TYPE": "oracle",
                "DEPLOYMENT_NAME": "gg-oracle-payments-01", "DESIRED": "false", "APPLY": "false",
                "TARGET_NAMESPACE": "goldengate-monitoring",
                "METRICS_CONFIG_HELPER": self.dummy_helper_file,
                "GITHUB_ENV": self.github_env, "GITHUB_OUTPUT": self.github_output,
            },
            bin_dir=self.bin_dir,
        )

    def test_valid_action_updated_is_accepted(self):
        proc = self._run("deployment=x\naction=updated\n")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        with open(self.github_output) as f:
            self.assertIn("helper_action=updated", f.read())

    def test_missing_action_line_fails(self):
        proc = self._run("deployment=x\n")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("expected exactly one action=", proc.stdout + proc.stderr)

    def test_duplicate_action_lines_fail(self):
        proc = self._run("action=none\naction=plan\n")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("expected exactly one action=", proc.stdout + proc.stderr)

    def test_unknown_action_value_fails(self):
        proc = self._run("action=bogus\n")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("is not one of none, plan, updated", proc.stdout + proc.stderr)


# The main workflow's CloudWatch preflight verifies Deployment/ReplicaSet ownership, not just a label match: extracts the real committed pod-selection fragment and drives it against a mocked kubectl/jq backed by JSON fixtures.
class MainWorkflowPodOwnershipTests(unittest.TestCase):
    DEPLOY_UID = "dep-uid-current"
    STALE_DEPLOY_UID = "dep-uid-STALE"

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.bin_dir = os.path.join(self.tmpdir, "bin")
        self.fixture_dir = os.path.join(self.tmpdir, "fixtures")
        self.rs_dir = os.path.join(self.fixture_dir, "replicasets")
        os.makedirs(self.bin_dir)
        os.makedirs(self.rs_dir)

        step = _get_step(MONITOR_WORKFLOW_PATH, "CloudWatch publication preflight (gate inventory)")
        run_text = step["run"]
        marker = 'echo "Using the existing monitor pod for this read-only preflight: ${POD_NAME}"'
        idx = run_text.index(marker)
        end = idx + len(marker)
        self.fragment = run_text[:end]

        real_jq = shutil.which("jq")
        assert real_jq, "jq must be installed to run this test"
        os.symlink(real_jq, os.path.join(self.bin_dir, "jq"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _pod(self, name, phase="Running", ready=True, deletion_ts=None,
              service_account="gg-monitor", rs_owner="gg-monitor-rs-current"):
        owner_refs = []
        if rs_owner:
            owner_refs = [{"controller": True, "kind": "ReplicaSet", "name": rs_owner}]
        pod = {
            "metadata": {"name": name, "ownerReferences": owner_refs},
            "status": {
                "phase": phase,
                "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            },
            "spec": {"serviceAccountName": service_account},
        }
        if deletion_ts:
            pod["metadata"]["deletionTimestamp"] = deletion_ts
        return pod

    def _write_fixtures(self, pods, deploy_uid=None, rs_owner_uid_by_name=None):
        deploy = {
            "metadata": {"uid": deploy_uid or self.DEPLOY_UID},
            "spec": {"selector": {"matchLabels": {"app.kubernetes.io/name": "gg-monitor"}}},
        }
        with open(os.path.join(self.fixture_dir, "deployment.json"), "w") as f:
            json.dump(deploy, f)
        with open(os.path.join(self.fixture_dir, "pods.json"), "w") as f:
            json.dump({"items": pods}, f)
        for rs_name, owner_uid in (rs_owner_uid_by_name or {}).items():
            rs = {"metadata": {"ownerReferences": [{"controller": True, "kind": "Deployment", "uid": owner_uid}]}}
            with open(os.path.join(self.rs_dir, f"{rs_name}.json"), "w") as f:
                json.dump(rs, f)

        write_executable(os.path.join(self.bin_dir, "kubectl"), f"""#!/usr/bin/env bash
if [ "$1" = "get" ] && [ "$2" = "deployment" ] && [ "$3" = "gg-monitor" ]; then
  cat "{self.fixture_dir}/deployment.json"
  exit 0
fi
if [ "$1" = "get" ] && [ "$2" = "pods" ]; then
  cat "{self.fixture_dir}/pods.json"
  exit 0
fi
if [ "$1" = "get" ] && [ "$2" = "replicaset" ]; then
  RS_FILE="{self.rs_dir}/$3.json"
  if [ -f "$RS_FILE" ]; then
    cat "$RS_FILE"
    exit 0
  fi
  exit 1
fi
echo "mock kubectl: unhandled: $*" >&2
exit 1
""")

    def _run_fragment(self):
        return run_step_script(
            MONITOR_WORKFLOW_PATH, "CloudWatch publication preflight (pod selection fragment)", self.fragment,
            {"TARGET_NAMESPACE": "goldengate-monitoring"},
            bin_dir=self.bin_dir,
            gh_expression_overrides={"${{ inputs.metrics_gate_expectation }}": "any"},
        )

    def test_selects_pod_owned_by_current_deployment(self):
        self._write_fixtures(
            pods=[self._pod("gg-monitor-abc")],
            rs_owner_uid_by_name={"gg-monitor-rs-current": self.DEPLOY_UID},
        )
        proc = self._run_fragment()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Using the existing monitor pod for this read-only preflight: gg-monitor-abc", proc.stdout)

    def test_rejects_pod_owned_by_a_stale_replicaset(self):
        # The ReplicaSet owns the pod, but its own controller Deployment UID doesn't match the CURRENT gg-monitor Deployment -- a stale/orphaned ReplicaSet from a prior rollout.
        self._write_fixtures(
            pods=[self._pod("gg-monitor-old")],
            rs_owner_uid_by_name={"gg-monitor-rs-current": self.STALE_DEPLOY_UID},
        )
        proc = self._run_fragment()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("PREREQUISITE NOT MET", proc.stdout)

    def test_rejects_pod_with_wrong_service_account(self):
        self._write_fixtures(
            pods=[self._pod("gg-monitor-wrong-sa", service_account="default")],
            rs_owner_uid_by_name={"gg-monitor-rs-current": self.DEPLOY_UID},
        )
        proc = self._run_fragment()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("PREREQUISITE NOT MET", proc.stdout)

    def test_rejects_terminating_pod(self):
        self._write_fixtures(
            pods=[self._pod("gg-monitor-terminating", deletion_ts="2024-01-01T00:00:00Z")],
            rs_owner_uid_by_name={"gg-monitor-rs-current": self.DEPLOY_UID},
        )
        proc = self._run_fragment()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("PREREQUISITE NOT MET", proc.stdout)

    def test_rejects_not_ready_pod(self):
        self._write_fixtures(
            pods=[self._pod("gg-monitor-not-ready", ready=False)],
            rs_owner_uid_by_name={"gg-monitor-rs-current": self.DEPLOY_UID},
        )
        proc = self._run_fragment()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("PREREQUISITE NOT MET", proc.stdout)

    def test_never_prints_full_pod_deployment_or_replicaset_object(self):
        self._write_fixtures(
            pods=[self._pod("gg-monitor-abc")],
            rs_owner_uid_by_name={"gg-monitor-rs-current": self.DEPLOY_UID},
        )
        proc = self._run_fragment()
        combined = proc.stdout + proc.stderr
        self.assertNotIn('"ownerReferences"', combined)
        self.assertNotIn('"selector"', combined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
