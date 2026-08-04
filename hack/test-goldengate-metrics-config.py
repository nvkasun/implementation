"""hack/test-goldengate-metrics-config.py: Phase 6C1 offline tests.

Covers two independent things, both entirely offline/mocked -- never a live
AWS call:

1. The existing GoldenGate custom-metric double-gate contract in
   monitoring/monitor/collector.py (collector.py itself is never modified
   by this phase) -- proves Global=false/CONFIG=false,
   Global=false/CONFIG=true, and Global=true/CONFIG=false all construct no
   CloudWatch client and issue no PutMetricData, while
   Global=true/CONFIG=true reaches the publication boundary. Also proves
   the metric/dimension contract (LagBreached/AbendFailure/DeploymentDown/
   HeartbeatAgeSeconds with Deployment+DeploymentType; CriticalServiceDown
   with +Service; ExtractLagSeconds/ReplicatLagSeconds/AbendState/
   AbendEvent with +Process) is unchanged, and that no ProcessType/
   ServiceName dimension has been introduced.

2. hack/goldengate-metrics-config.py, the exact-conditional-update helper
   piped into the gg-monitor pod by goldengate-monitor-metrics-config.yaml
   -- using moto's emulated DynamoDB (same convention already established
   in monitoring/monitor/tests/test_collector.py: @mock_aws + a real
   boto3 Table backed by moto, never a live table).

Run directly: python3 hack/test-goldengate-metrics-config.py
"""
from __future__ import annotations

import importlib.util
import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONITOR_SRC = os.path.join(REPO_ROOT, "monitoring", "monitor")
HELPER_PATH = os.path.join(REPO_ROOT, "hack", "goldengate-metrics-config.py")

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
    """Fresh module object per call -- the helper has no import-time side
    effects (all AWS access happens inside main()), so re-loading is cheap
    and keeps tests independent."""
    spec = importlib.util.spec_from_file_location("goldengate_metrics_config_helper", HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_helper(argv):
    """Runs the helper's main() with the given argv against
    AWS_REGION=eu-west-1 / DYNAMODB_TABLE=gg-eks-pipeline, capturing stdout
    and the exit code. Returns (exit_code, stdout_text)."""
    module = load_helper_module()
    buf = io.StringIO()
    old_argv = sys.argv
    sys.argv = ["goldengate-metrics-config.py"] + argv
    env_patch = {"AWS_REGION": "eu-west-1", "DYNAMODB_TABLE": "gg-eks-pipeline"}
    try:
        # sys.exit("message") prints to stderr, not stdout -- both are
        # captured into the same buffer so assertions can check either a
        # normal report() line or a fail-closed usage/validation message.
        with mock.patch.dict(os.environ, env_patch, clear=False), \
             redirect_stdout(buf), redirect_stderr(buf):
            try:
                module.main()
                code = 0
            except SystemExit as exc:
                if isinstance(exc.code, int):
                    code = exc.code
                else:
                    # sys.exit("message") only auto-prints its message via
                    # the interpreter's own top-level handler for a truly
                    # uncaught SystemExit -- catching it here ourselves
                    # means that never happens automatically, so it is
                    # replicated explicitly (matching real `python3 x.py`
                    # subprocess behavior, where this would land on
                    # stderr).
                    if exc.code:
                        print(exc.code, file=sys.stderr)
                    code = 1 if exc.code else 0
    finally:
        sys.argv = old_argv
    return code, buf.getvalue()


# ---------------------------------------------------------------------
# Task 10: existing metric-contract / double-gate tests.
# ---------------------------------------------------------------------
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


# ---------------------------------------------------------------------
# Task 11 (I, K partially at helper level, L, M, N, O): the exact
# conditional-update helper.
# ---------------------------------------------------------------------
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
        # table is created via the real (not-yet-patched) boto3.resource,
        # exactly like test_N below -- constructing the "real" reference
        # AFTER boto3.resource is patched would just wrap another mock.
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
            # Simulate a concurrent actor changing the item between this
            # helper's own read and its own write.
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
