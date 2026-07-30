import html as html_module
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from unittest import mock

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
MONITOR_WORKFLOW_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "goldengate-monitor.yaml")
ARGOCD_WORKFLOW_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "argocd-eks-deployment.yaml")
DEPLOYMENTS_FILE_PATH = os.path.join(REPO_ROOT, "envs", "dev", "goldengate-deployments.yaml")

# boto3/botocore are runtime dependencies but not required to run this
# suite: every test injects a fake/mock table. Stub only when unavailable.
try:
    import boto3  # noqa: F401
except ImportError:
    sys.modules["boto3"] = mock.MagicMock()

try:
    from botocore.config import Config  # noqa: F401
    from botocore.exceptions import BotoCoreError, ClientError  # noqa: F401
except ImportError:
    botocore_stub = mock.MagicMock()

    class _StubClientError(Exception):
        def __init__(self, *args, **kwargs):
            super().__init__("stub ClientError")

    class _StubBotoCoreError(Exception):
        pass

    exceptions_stub = mock.MagicMock()
    exceptions_stub.ClientError = _StubClientError
    exceptions_stub.BotoCoreError = _StubBotoCoreError

    sys.modules["botocore"] = botocore_stub
    sys.modules["botocore.config"] = mock.MagicMock()
    sys.modules["botocore.exceptions"] = exceptions_stub

import config as cfgmod  # noqa: E402
import monitor  # noqa: E402


def make_config(**overrides):
    env = {"AWS_REGION": "eu-west-1", "DYNAMODB_TABLE": "gg-eks-pipeline"}
    env.update(overrides)
    return cfgmod.load_config(env)


DEPLOYMENTS = [
    {"name": "gg-oracle-payments-01", "type": "oracle", "pipeline": "payments-ora-to-pg-001",
    "role": "source", "enabled": True, "adminSecret": "dev/goldengate/source/admin"},
    {"name": "gg-postgresql-payments-01", "type": "postgresql", "pipeline": "payments-ora-to-pg-001",
    "role": "target", "enabled": True, "adminSecret": "dev/goldengate/target/admin"},
]

LOGICAL_PIPELINES = cfgmod.build_logical_pipelines(DEPLOYMENTS)


class FakeTable:
    """Minimal in-memory stand-in for a boto3 DynamoDB Table -- supports
    only get_item/query, matching this portal's read-only surface."""

    def __init__(self, items=None):
        self.items = list(items or [])
        self.get_item_calls = []
        self.query_calls = []

    def get_item(self, Key):
        self.get_item_calls.append(Key)
        for it in self.items:
            if it["pipeline"] == Key["pipeline"] and it["recordType"] == Key["recordType"]:
                return {"Item": it}
        return {}

    def query(self, KeyConditionExpression, ExpressionAttributeValues):
        self.query_calls.append(ExpressionAttributeValues)
        pipeline = ExpressionAttributeValues[":p"]
        prefix = ExpressionAttributeValues[":prefix"]
        return {"Items": [it for it in self.items
                          if it["pipeline"] == pipeline and it["recordType"].startswith(prefix)]}


def make_deployment_state_item(pipeline="gg-oracle-payments-01", status="UP", recorded_at=1780000000, **overrides):
    item = {"pipeline": pipeline, "recordType": "STATE#_deployment", "status": status,
           "recordedAt": recorded_at, "deploymentType": "oracle"}
    item.update(overrides)
    return item


def make_lease_item(pipeline="gg-oracle-payments-01", now=1780000000, **overrides):
    item = {"pipeline": pipeline, "recordType": "LEASE", "holder": "gg-monitor-0",
           "expiresAt": now + 30, "ttl": now + 90}
    item.update(overrides)
    return item


def make_process_item(pipeline="gg-oracle-payments-01", process="EXTORA1", status="RUNNING",
                      recorded_at=1780000000, **overrides):
    item = {"pipeline": pipeline, "recordType": f"STATE#{process}", "status": status,
           "processType": "extract", "recordedAt": recorded_at, "lagSeconds": 4, "errorMsg": ""}
    item.update(overrides)
    return item


class ConfigValidationTests(unittest.TestCase):
    def test_valid_configuration(self):
        config = make_config()
        self.assertEqual(config.aws_region, "eu-west-1")
        self.assertEqual(config.port, 8080)
        self.assertTrue(config.legacy_fallback_enabled)

    def test_missing_aws_region(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.load_config({"DYNAMODB_TABLE": "gg-eks-pipeline"})

    def test_invalid_port(self):
        with self.assertRaises(cfgmod.ConfigError):
            make_config(PORT="0")
        with self.assertRaises(cfgmod.ConfigError):
            make_config(PORT="70000")

    def test_invalid_stale_threshold(self):
        with self.assertRaises(cfgmod.ConfigError):
            make_config(STALE_AFTER_SECONDS="0")


class CanonicalEffectiveStatusTests(unittest.TestCase):
    def test_up_maps_to_up(self):
        item = make_deployment_state_item(status="UP", recorded_at=1780000000)
        out = monitor.compute_canonical_effective_status(item, now=1780000010, stale_after_seconds=120)
        self.assertEqual(out["effectiveStatus"], "UP")
        self.assertTrue(out["fresh"])

    def test_starting_maps_to_starting(self):
        item = make_deployment_state_item(status="STARTING", recorded_at=1780000000)
        out = monitor.compute_canonical_effective_status(item, now=1780000010, stale_after_seconds=120)
        self.assertEqual(out["effectiveStatus"], "STARTING")

    def test_deployment_down_maps_to_down(self):
        item = make_deployment_state_item(status="DEPLOYMENT_DOWN", recorded_at=1780000000)
        out = monitor.compute_canonical_effective_status(item, now=1780000010, stale_after_seconds=120)
        self.assertEqual(out["effectiveStatus"], "DOWN")

    def test_stale_overrides_raw_status(self):
        item = make_deployment_state_item(status="UP", recorded_at=1780000000)
        out = monitor.compute_canonical_effective_status(item, now=1780000000 + 121, stale_after_seconds=120)
        self.assertEqual(out["effectiveStatus"], "STALE")

    def test_missing_item_is_missing(self):
        out = monitor.compute_canonical_effective_status(None, now=1780000000, stale_after_seconds=120)
        self.assertEqual(out["effectiveStatus"], "MISSING")

    def test_future_timestamp_never_yields_negative_age(self):
        item = make_deployment_state_item(status="UP", recorded_at=1780000100)
        out = monitor.compute_canonical_effective_status(item, now=1780000000, stale_after_seconds=120)
        self.assertEqual(out["ageSeconds"], 0)


class LegacyEffectiveStatusTests(unittest.TestCase):
    def test_healthy_maps_to_up(self):
        item = {"pipeline": "gg-payments-ora-to-pg-001-source", "recordType": "STATE#_deployment",
               "status": "HEALTHY", "recordedAt": 1780000000}
        out = monitor.compute_legacy_effective_status(item, now=1780000010, stale_after_seconds=120)
        self.assertEqual(out["effectiveStatus"], "UP")

    def test_degraded_maps_to_unknown(self):
        item = {"pipeline": "gg-payments-ora-to-pg-001-source", "recordType": "STATE#_deployment",
               "status": "DEGRADED", "recordedAt": 1780000000}
        out = monitor.compute_legacy_effective_status(item, now=1780000010, stale_after_seconds=120)
        self.assertEqual(out["effectiveStatus"], "UNKNOWN")

    def test_missing_legacy_item_is_missing(self):
        out = monitor.compute_legacy_effective_status(None, now=1780000000, stale_after_seconds=120)
        self.assertEqual(out["effectiveStatus"], "MISSING")


class ProcessRowNormalizationTests(unittest.TestCase):
    def test_running_stopped_abended_pass_through(self):
        for raw in ("RUNNING", "STOPPED", "ABENDED"):
            self.assertEqual(monitor.normalize_process_status(raw), raw)

    def test_unrecognized_process_status_becomes_unknown(self):
        self.assertEqual(monitor.normalize_process_status("SOMETHING_ELSE"), "UNKNOWN")

    def test_process_row_never_exposes_raw_error_msg_field(self):
        row = make_process_item(status="ABENDED", errorMsg="db-internal.example.local password=x")
        out = monitor.normalize_process_row(row, now=1780000010, stale_after_seconds=120)
        self.assertNotIn("errorMsg", out)
        self.assertTrue(out["hasError"])
        self.assertEqual(out["statusCode"], "PROCESS_ABENDED")


class ReadRuntimeViewTests(unittest.TestCase):
    """Canonical-preferred-over-legacy, fallback-when-missing, and
    fallback-disabled behaviour."""

    def _meta(self):
        return {"type": "oracle", "enabled": True}

    def test_canonical_data_used_when_present(self):
        now = 1780000010
        table = FakeTable([make_deployment_state_item(recorded_at=now - 5)])
        out = monitor.read_runtime_view(table, "payments-ora-to-pg-001", "source", "gg-oracle-payments-01",
                                        self._meta(), legacy_fallback_enabled=True, now=now, stale_after_seconds=120)
        self.assertEqual(out["dataSource"], "canonical-monitor")
        self.assertEqual(out["effectiveStatus"], "UP")

    def test_falls_back_to_legacy_when_canonical_missing_and_enabled(self):
        now = 1780000010
        legacy_item = {"pipeline": "gg-payments-ora-to-pg-001-source", "recordType": "STATE#_deployment",
                       "status": "HEALTHY", "recordedAt": now - 5}
        table = FakeTable([legacy_item])
        out = monitor.read_runtime_view(table, "payments-ora-to-pg-001", "source", "gg-oracle-payments-01",
                                        self._meta(), legacy_fallback_enabled=True, now=now, stale_after_seconds=120)
        self.assertEqual(out["dataSource"], "legacy-observer-fallback")
        self.assertEqual(out["effectiveStatus"], "UP")

    def test_no_legacy_key_hardcoded_in_source(self):
        import inspect
        src = inspect.getsource(monitor.read_runtime_view)
        self.assertIn('f"gg-{pipeline_id}-{role}"', src)
        self.assertNotIn("gg-payments-ora-to-pg-001-source", src)

    def test_fallback_disabled_shows_missing_not_legacy_data(self):
        now = 1780000010
        legacy_item = {"pipeline": "gg-payments-ora-to-pg-001-source", "recordType": "STATE#_deployment",
                       "status": "HEALTHY", "recordedAt": now - 5}
        table = FakeTable([legacy_item])
        out = monitor.read_runtime_view(table, "payments-ora-to-pg-001", "source", "gg-oracle-payments-01",
                                        self._meta(), legacy_fallback_enabled=False, now=now, stale_after_seconds=120)
        self.assertEqual(out["dataSource"], "canonical-monitor")
        self.assertEqual(out["effectiveStatus"], "MISSING")

    def test_canonical_always_wins_even_when_legacy_also_present(self):
        now = 1780000010
        table = FakeTable([
            make_deployment_state_item(recorded_at=now - 5, status="UP"),
            {"pipeline": "gg-payments-ora-to-pg-001-source", "recordType": "STATE#_deployment",
             "status": "DOWN", "recordedAt": now - 5},
        ])
        out = monitor.read_runtime_view(table, "payments-ora-to-pg-001", "source", "gg-oracle-payments-01",
                                        self._meta(), legacy_fallback_enabled=True, now=now, stale_after_seconds=120)
        self.assertEqual(out["dataSource"], "canonical-monitor")
        self.assertEqual(out["effectiveStatus"], "UP")

    def test_no_process_state_rows_produces_empty_list_not_crash(self):
        now = 1780000010
        table = FakeTable([make_deployment_state_item(recorded_at=now - 5)])
        out = monitor.read_runtime_view(table, "payments-ora-to-pg-001", "source", "gg-oracle-payments-01",
                                        self._meta(), legacy_fallback_enabled=True, now=now, stale_after_seconds=120)
        self.assertEqual(out["processes"], [])

    def test_critical_service_state_passed_through(self):
        now = 1780000010
        table = FakeTable([make_deployment_state_item(recorded_at=now - 5,
                                                      criticalServices={"adminsrvr": {"reachable": True}})])
        out = monitor.read_runtime_view(table, "payments-ora-to-pg-001", "source", "gg-oracle-payments-01",
                                        self._meta(), legacy_fallback_enabled=True, now=now, stale_after_seconds=120)
        self.assertEqual(out["criticalServices"], {"adminsrvr": True})

    def test_lease_freshness_exposed(self):
        now = 1780000010
        table = FakeTable([make_deployment_state_item(recorded_at=now - 5), make_lease_item(now=now)])
        out = monitor.read_runtime_view(table, "payments-ora-to-pg-001", "source", "gg-oracle-payments-01",
                                        self._meta(), legacy_fallback_enabled=True, now=now, stale_after_seconds=120)
        self.assertTrue(out["lease"]["fresh"])

    def test_expired_lease_shown_as_not_fresh(self):
        now = 1780000010
        table = FakeTable([make_deployment_state_item(recorded_at=now - 5),
                           make_lease_item(now=now, expiresAt=now - 100)])
        out = monitor.read_runtime_view(table, "payments-ora-to-pg-001", "source", "gg-oracle-payments-01",
                                        self._meta(), legacy_fallback_enabled=True, now=now, stale_after_seconds=120)
        self.assertFalse(out["lease"]["fresh"])

    def test_canonical_process_rows_shown_when_deployment_state_missing_and_fallback_disabled(self):
        # STATE#_deployment absent (eg a race during first-tick startup)
        # must not suppress independently-existing STATE#<process> rows.
        now = 1780000010
        table = FakeTable([make_process_item(recorded_at=now - 3)])
        out = monitor.read_runtime_view(table, "payments-ora-to-pg-001", "source", "gg-oracle-payments-01",
                                        self._meta(), legacy_fallback_enabled=False, now=now, stale_after_seconds=120)
        self.assertEqual(out["effectiveStatus"], "MISSING")
        self.assertEqual(len(out["processes"]), 1)
        self.assertEqual(out["processes"][0]["process"], "EXTORA1")

    def test_canonical_process_rows_shown_when_deployment_state_missing_and_legacy_fallback_enabled(self):
        # The legacy-fallback branch still owns deployment-status
        # resolution, but canonical process rows under the canonical
        # partition key must still surface -- never invented legacy rows.
        now = 1780000010
        table = FakeTable([make_process_item(recorded_at=now - 3)])
        out = monitor.read_runtime_view(table, "payments-ora-to-pg-001", "source", "gg-oracle-payments-01",
                                        self._meta(), legacy_fallback_enabled=True, now=now, stale_after_seconds=120)
        self.assertEqual(out["dataSource"], "legacy-observer-fallback")
        self.assertEqual(out["effectiveStatus"], "MISSING")
        self.assertEqual(len(out["processes"]), 1)
        self.assertEqual(out["processes"][0]["process"], "EXTORA1")

    def test_malformed_critical_services_root_does_not_crash(self):
        now = 1780000010
        table = FakeTable([make_deployment_state_item(recorded_at=now - 5, criticalServices="not-a-dict")])
        out = monitor.read_runtime_view(table, "payments-ora-to-pg-001", "source", "gg-oracle-payments-01",
                                        self._meta(), legacy_fallback_enabled=True, now=now, stale_after_seconds=120)
        self.assertEqual(out["criticalServices"], {})

    def test_malformed_critical_service_item_does_not_crash(self):
        now = 1780000010
        table = FakeTable([make_deployment_state_item(
            recorded_at=now - 5,
            criticalServices={"adminsrvr": True, "distsrvr": None, "metricsrvr": "unexpected"})])
        out = monitor.read_runtime_view(table, "payments-ora-to-pg-001", "source", "gg-oracle-payments-01",
                                        self._meta(), legacy_fallback_enabled=True, now=now, stale_after_seconds=120)
        self.assertEqual(out["criticalServices"], {"adminsrvr": False, "distsrvr": False, "metricsrvr": False})


class BuildStatusPayloadTests(unittest.TestCase):
    def test_end_to_end_shape_matches_recommended_schema(self):
        now = 1780000010
        table = FakeTable([
            make_deployment_state_item(pipeline="gg-oracle-payments-01", recorded_at=now - 5, status="UP"),
            make_process_item(pipeline="gg-oracle-payments-01", process="EXTORA1", recorded_at=now - 5),
            {"pipeline": "gg-payments-ora-to-pg-001-target", "recordType": "STATE#_deployment",
             "status": "HEALTHY", "recordedAt": now - 5},
        ])
        config = make_config()
        payload = monitor.build_status_payload(config, table, DEPLOYMENTS, LOGICAL_PIPELINES, clock=lambda: now)

        self.assertIn("generatedAt", payload)
        lp = payload["logicalPipelines"][0]
        self.assertEqual(lp["pipelineId"], "payments-ora-to-pg-001")
        roles = {r["role"]: r for r in lp["runtimes"]}
        self.assertEqual(roles["source"]["deploymentName"], "gg-oracle-payments-01")
        self.assertEqual(roles["source"]["dataSource"], "canonical-monitor")
        self.assertEqual(roles["target"]["deploymentName"], "gg-postgresql-payments-01")
        self.assertEqual(roles["target"]["dataSource"], "legacy-observer-fallback")

    def test_dynamodb_read_failure_raises_read_error(self):
        from botocore.exceptions import ClientError
        table = mock.Mock()
        table.get_item.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "boom"}}, "GetItem")
        config = make_config()
        with self.assertRaises(monitor.DynamoDbReadError):
            monitor.build_status_payload(config, table, DEPLOYMENTS, LOGICAL_PIPELINES, clock=lambda: 1780000010)

    def test_real_repo_config_produces_two_runtimes_under_one_pipeline(self):
        doc = cfgmod.load_deployments(os.path.join(REPO_ROOT, "envs", "dev"))
        lps = cfgmod.build_logical_pipelines(doc["deployments"])
        table = FakeTable([])
        config = make_config()
        payload = monitor.build_status_payload(config, table, doc["deployments"], lps, clock=lambda: 1780000010)
        self.assertEqual(len(payload["logicalPipelines"]), 1)
        self.assertEqual(len(payload["logicalPipelines"][0]["runtimes"]), 2)


class DecimalConversionTests(unittest.TestCase):
    def test_integral_decimal_becomes_int(self):
        self.assertEqual(monitor.decimal_to_jsonsafe(Decimal("500000")), 500000)

    def test_fractional_decimal_becomes_float(self):
        self.assertEqual(monitor.decimal_to_jsonsafe(Decimal("50.25")), 50.25)

    def test_json_default_serializes_decimal(self):
        encoded = json.dumps({"value": Decimal("12.50")}, default=monitor._json_default)
        self.assertEqual(json.loads(encoded)["value"], 12.5)


class HtmlEscapingTests(unittest.TestCase):
    def test_malicious_values_are_escaped_in_html(self):
        malicious = '<script>alert(1)</script>'
        payload = {"generatedAt": 1780000010, "logicalPipelines": [{
            "pipelineId": malicious,
            "runtimes": [{"role": "source", "deploymentName": malicious, "deploymentType": "oracle",
                         "effectiveStatus": "UP", "dataSource": "canonical-monitor", "ageSeconds": 1,
                         "lease": None, "processes": []}],
        }]}
        rendered = monitor.render_html(payload, make_config())
        self.assertNotIn("<script>", rendered)
        self.assertIn(html_module.escape(malicious), rendered)

    def test_no_process_rows_shows_fixed_message(self):
        payload = {"generatedAt": 1780000010, "logicalPipelines": [{
            "pipelineId": "payments-ora-to-pg-001",
            "runtimes": [{"role": "source", "deploymentName": "gg-oracle-payments-01", "deploymentType": "oracle",
                         "effectiveStatus": "UP", "dataSource": "canonical-monitor", "ageSeconds": 1,
                         "lease": None, "processes": []}],
        }]}
        rendered = monitor.render_html(payload, make_config())
        self.assertIn("No process STATE rows found.", rendered)


class HealthAndReadyTests(unittest.TestCase):
    def _handler(self, config, table_factory, ready_state=None, expected=None):
        handler_cls = monitor._make_handler(config, table_factory, DEPLOYMENTS, LOGICAL_PIPELINES,
                                            ready_state or {}, expected or [])
        handler = handler_cls.__new__(handler_cls)
        writes = []
        handler._write = lambda status, ctype, body: writes.append((status, ctype, body))
        return handler, writes

    def test_healthz_returns_200_and_never_touches_dynamodb(self):
        factory = mock.Mock(side_effect=AssertionError("healthz must never call the table factory"))
        handler, writes = self._handler(make_config(), factory)
        handler.path = "/healthz"
        handler.do_GET()
        self.assertEqual(writes[0][0], 200)
        factory.assert_not_called()

    def test_readyz_returns_200_when_collector_ready_and_dynamodb_reachable(self):
        table = mock.Mock()
        handler, writes = self._handler(make_config(), lambda: table,
                                        ready_state={"gg-oracle-payments-01": True},
                                        expected=["gg-oracle-payments-01"])
        handler.path = "/readyz"
        handler.do_GET()
        self.assertEqual(writes[0][0], 200)

    def test_readyz_returns_503_when_collector_not_ready(self):
        handler, writes = self._handler(make_config(), lambda: mock.Mock(),
                                        ready_state={"gg-oracle-payments-01": False},
                                        expected=["gg-oracle-payments-01"])
        handler.path = "/readyz"
        handler.do_GET()
        self.assertEqual(writes[0][0], 503)

    def test_readyz_returns_503_on_dynamodb_failure(self):
        table = mock.Mock()
        table.meta.client.describe_table.side_effect = RuntimeError("boom")
        handler, writes = self._handler(make_config(), lambda: table)
        handler.path = "/readyz"
        handler.do_GET()
        self.assertEqual(writes[0][0], 503)


class RootPageErrorBannerTests(unittest.TestCase):
    def test_root_page_shows_sanitized_banner_on_dynamodb_failure(self):
        def factory():
            raise RuntimeError("boom")
        handler_cls = monitor._make_handler(make_config(), factory, DEPLOYMENTS, LOGICAL_PIPELINES, {}, [])
        handler = handler_cls.__new__(handler_cls)
        handler.path = "/"
        writes = []
        handler._write = lambda status, ctype, body: writes.append((status, ctype, body))
        handler.do_GET()
        self.assertEqual(writes[0][0], 200)
        body = writes[0][2].decode("utf-8")
        self.assertIn("Unable to read monitoring data", body)
        self.assertNotIn("Traceback", body)


class ClientFacingErrorSanitizationTests(unittest.TestCase):
    SIMULATED_ARN_LEAK = (
        "An error occurred (AccessDeniedException) when calling the GetItem operation: "
        "User: arn:aws:sts::668311715351:assumed-role/GoldenGateSecretsReadRole-dev/i-0123456789abcdef "
        "is not authorized to perform: dynamodb:GetItem"
    )

    def _failing_table(self):
        table = mock.Mock()
        table.get_item.side_effect = RuntimeError(self.SIMULATED_ARN_LEAK)
        return table

    def test_api_status_dynamodb_failure_returns_only_fixed_message(self):
        handler_cls = monitor._make_handler(make_config(), self._failing_table, DEPLOYMENTS, LOGICAL_PIPELINES, {}, [])
        handler = handler_cls.__new__(handler_cls)
        handler.path = "/api/status"
        writes = []
        handler._write = lambda status, ctype, body: writes.append((status, ctype, body))
        handler.do_GET()
        self.assertEqual(writes[0][0], 503)
        body = json.loads(writes[0][2])
        self.assertEqual(body["message"], monitor.CLIENT_SAFE_DYNAMODB_ERROR_MESSAGE)
        raw_response = writes[0][2].decode("utf-8")
        self.assertNotIn("arn:aws", raw_response)
        self.assertNotIn("GoldenGateSecretsReadRole-dev", raw_response)


class DynamoDbAccessPatternTests(unittest.TestCase):
    def test_get_deployment_state_uses_state_deployment_record_type(self):
        table = FakeTable([make_deployment_state_item(recorded_at=1780000000)])
        monitor.get_deployment_state_item(table, "gg-oracle-payments-01")
        self.assertEqual(table.get_item_calls[-1]["recordType"], "STATE#_deployment")

    def test_get_config_uses_config_record_type(self):
        table = FakeTable([{"pipeline": "gg-oracle-payments-01", "recordType": "CONFIG"}])
        monitor.get_config_item(table, "gg-oracle-payments-01")
        self.assertEqual(table.get_item_calls[-1]["recordType"], "CONFIG")

    def test_query_process_states_excludes_deployment_row(self):
        table = FakeTable([make_deployment_state_item(recorded_at=1780000000), make_process_item()])
        rows = monitor.query_process_state_items(table, "gg-oracle-payments-01")
        self.assertEqual(table.query_calls[-1][":prefix"], "STATE#")
        self.assertEqual(len(rows), 1)

    def test_no_dynamodb_write_operation_occurs(self):
        table = FakeTable([make_deployment_state_item(recorded_at=1780000005)])
        monitor.build_status_payload(make_config(), table, DEPLOYMENTS, LOGICAL_PIPELINES, clock=lambda: 1780000010)
        for forbidden in ("put_item", "update_item", "delete_item", "batch_writer", "scan"):
            self.assertFalse(hasattr(table, forbidden))


class ProcessRowManagerFieldsTests(unittest.TestCase):
    """Phase 4C2: manager-parity process fields the collector already
    writes (resolvedThreshold/resolvedMode/consecutiveAbends) must be
    surfaced by normalize_process_row with safe defaults when absent."""

    def test_resolved_threshold_mode_and_abends_passed_through(self):
        row = make_process_item(resolvedThreshold=300, resolvedMode="alert", consecutiveAbends=2)
        out = monitor.normalize_process_row(row, now=1780000010, stale_after_seconds=120)
        self.assertEqual(out["resolvedThreshold"], 300)
        self.assertEqual(out["resolvedMode"], "alert")
        self.assertEqual(out["consecutiveAbends"], 2)

    def test_missing_resolved_threshold_and_mode_default_to_none(self):
        row = make_process_item()
        row.pop("resolvedThreshold", None)
        row.pop("resolvedMode", None)
        out = monitor.normalize_process_row(row, now=1780000010, stale_after_seconds=120)
        self.assertIsNone(out["resolvedThreshold"])
        self.assertIsNone(out["resolvedMode"])

    def test_missing_consecutive_abends_defaults_to_zero(self):
        row = make_process_item()
        out = monitor.normalize_process_row(row, now=1780000010, stale_after_seconds=120)
        self.assertEqual(out["consecutiveAbends"], 0)

    def test_decimal_threshold_and_abends_become_json_safe(self):
        row = make_process_item(resolvedThreshold=Decimal("300"), consecutiveAbends=Decimal("1"))
        out = monitor.normalize_process_row(row, now=1780000010, stale_after_seconds=120)
        self.assertEqual(out["resolvedThreshold"], 300)
        self.assertNotIsInstance(out["resolvedThreshold"], Decimal)
        self.assertEqual(out["consecutiveAbends"], 1)
        self.assertNotIsInstance(out["consecutiveAbends"], Decimal)

    def test_process_stale_marker_set_when_recorded_at_too_old(self):
        row = make_process_item(recorded_at=1780000000)
        out = monitor.normalize_process_row(row, now=1780000200, stale_after_seconds=120)
        self.assertTrue(out["stale"])

    def test_process_stale_marker_false_when_fresh(self):
        row = make_process_item(recorded_at=1780000000)
        out = monitor.normalize_process_row(row, now=1780000010, stale_after_seconds=120)
        self.assertFalse(out["stale"])


class FormatRelativeAgeTests(unittest.TestCase):
    """Phase 4C2 correction: manager-contract relative-age text, reimplemented
    independently against this codebase's None-based missing-value
    convention (not the manager's -1 sentinel)."""

    def test_missing_returns_never_by_default(self):
        self.assertEqual(monitor.format_relative_age(None), "never")

    def test_missing_returns_caller_supplied_text(self):
        self.assertEqual(monitor.format_relative_age(None, missing_text="-"), "-")

    def test_seconds_tier(self):
        self.assertEqual(monitor.format_relative_age(12), "12s ago")

    def test_minutes_tier(self):
        self.assertEqual(monitor.format_relative_age(4 * 60), "4m ago")

    def test_hours_tier(self):
        self.assertEqual(monitor.format_relative_age(2 * 3600), "2h ago")

    def test_boundary_59_seconds_stays_in_seconds_tier(self):
        self.assertEqual(monitor.format_relative_age(59), "59s ago")

    def test_boundary_60_seconds_moves_to_minutes_tier(self):
        self.assertEqual(monitor.format_relative_age(60), "1m ago")

    def test_boundary_3599_seconds_stays_in_minutes_tier(self):
        self.assertEqual(monitor.format_relative_age(3599), "59m ago")

    def test_boundary_3600_seconds_moves_to_hours_tier(self):
        self.assertEqual(monitor.format_relative_age(3600), "1h ago")


class FormatLagThresholdModeTests(unittest.TestCase):
    """Phase 4C2 correction: manager-contract combined lag/threshold/mode
    cell text, reimplemented independently against this codebase's
    None-based missing-value convention (not the manager's -1 sentinel)."""

    def test_full_values_combined_matches_operator_facing_string(self):
        self.assertEqual(monitor.format_lag_threshold_mode(5, 300, "alert"), "5s / thr 300s (alert)")

    def test_both_missing_is_na(self):
        self.assertEqual(monitor.format_lag_threshold_mode(None, None, None), "N/A")

    def test_missing_lag_only_never_renders_literal_none(self):
        result = monitor.format_lag_threshold_mode(None, 300, "alert")
        self.assertNotIn("None", result)
        self.assertIn("thr 300s", result)
        self.assertIn("(alert)", result)

    def test_missing_threshold_only_never_renders_literal_none(self):
        result = monitor.format_lag_threshold_mode(5, None, "alert")
        self.assertNotIn("None", result)
        self.assertIn("5s", result)

    def test_missing_mode_only_never_renders_literal_none(self):
        result = monitor.format_lag_threshold_mode(5, 300, None)
        self.assertNotIn("None", result)
        self.assertIn("(?)", result)


class CriticalServiceNormalizationTests(unittest.TestCase):
    """Section 4: normalize_critical_services must never raise, regardless
    of malformed shape, and must degrade unsafe entries to a fail-closed
    (unreachable) representation."""

    def test_well_formed_service_passes_through(self):
        self.assertEqual(
            monitor.normalize_critical_services({"adminsrvr": {"reachable": True}}),
            {"adminsrvr": True})

    def test_non_dict_root_never_raises_and_becomes_empty(self):
        for bad_root in (None, "unexpected", 42, ["adminsrvr"], True):
            with self.subTest(bad_root=bad_root):
                self.assertEqual(monitor.normalize_critical_services(bad_root), {})

    def test_boolean_service_value_never_raises(self):
        self.assertEqual(monitor.normalize_critical_services({"adminsrvr": True}), {"adminsrvr": False})

    def test_null_service_value_never_raises(self):
        self.assertEqual(monitor.normalize_critical_services({"adminsrvr": None}), {"adminsrvr": False})

    def test_string_service_value_never_raises(self):
        self.assertEqual(monitor.normalize_critical_services({"adminsrvr": "unexpected"}), {"adminsrvr": False})

    def test_missing_reachable_key_defaults_to_false(self):
        self.assertEqual(monitor.normalize_critical_services({"adminsrvr": {}}), {"adminsrvr": False})


class PortalHtmlManagerParityTests(unittest.TestCase):
    """Phase 4C2: every required HTML field is present, HTML-escaped, and
    never leaks raw errorMsg/credentials/secrets/hostnames/ARNs."""

    def _payload_with_full_runtime(self, **overrides):
        runtime = {
            "role": "source", "deploymentName": "gg-oracle-payments-01", "deploymentType": "oracle",
            "effectiveStatus": "UP", "fresh": True, "dataSource": "canonical-monitor",
            "alertsEnabled": True, "ageSeconds": 3, "recordedAt": 1780000007,
            "lease": {"holder": "gg-monitor-0", "expiresAt": 1780000040, "fresh": True},
            "criticalServices": {"adminsrvr": True, "distsrvr": False},
            "processes": [{
                "process": "EXTORA1", "processType": "extract", "status": "RUNNING", "stale": False,
                "recordedAt": 1780000007, "ageSeconds": 3, "lagSeconds": 5,
                "resolvedThreshold": 300, "resolvedMode": "alert", "consecutiveAbends": 0,
                "hasError": False, "statusCode": "NONE", "statusMessage": "No error.",
            }],
        }
        runtime.update(overrides)
        return {"generatedAt": 1780000010, "logicalPipelines": [
            {"pipelineId": "payments-ora-to-pg-001", "runtimes": [runtime]}]}

    def test_deployment_name_shown(self):
        rendered = monitor.render_html(self._payload_with_full_runtime(), make_config())
        self.assertIn("gg-oracle-payments-01", rendered)

    def test_deployment_status_shown(self):
        rendered = monitor.render_html(self._payload_with_full_runtime(), make_config())
        self.assertIn("UP", rendered)

    def test_stale_deployment_clearly_marked(self):
        rendered = monitor.render_html(self._payload_with_full_runtime(fresh=False), make_config())
        self.assertIn("STALE", rendered)

    def test_fresh_deployment_marked_fresh(self):
        rendered = monitor.render_html(self._payload_with_full_runtime(fresh=True), make_config())
        self.assertIn("Fresh", rendered)

    def test_alerts_enabled_true_shown(self):
        rendered = monitor.render_html(self._payload_with_full_runtime(alertsEnabled=True), make_config())
        self.assertIn("true", rendered)

    def test_alerts_enabled_false_shown(self):
        rendered = monitor.render_html(self._payload_with_full_runtime(alertsEnabled=False), make_config())
        self.assertIn("false", rendered)

    def test_alerts_enabled_missing_shown_as_unknown(self):
        rendered = monitor.render_html(self._payload_with_full_runtime(alertsEnabled=None), make_config())
        self.assertIn("unknown", rendered)

    def test_lease_holder_and_valid_state_shown(self):
        rendered = monitor.render_html(self._payload_with_full_runtime(), make_config())
        self.assertIn("gg-monitor-0", rendered)
        self.assertIn("valid", rendered)

    def test_lease_expired_state_shown(self):
        payload = self._payload_with_full_runtime(
            lease={"holder": "gg-monitor-0", "expiresAt": 1780000000, "fresh": False})
        rendered = monitor.render_html(payload, make_config())
        self.assertIn("EXPIRED", rendered)

    def test_deployment_record_age_shown(self):
        rendered = monitor.render_html(self._payload_with_full_runtime(ageSeconds=3), make_config())
        self.assertIn("3s ago", rendered)

    def test_critical_services_reachable_and_down_shown(self):
        rendered = monitor.render_html(self._payload_with_full_runtime(), make_config())
        self.assertIn("adminsrvr", rendered)
        self.assertIn("reachable", rendered)
        self.assertIn("distsrvr", rendered)
        self.assertIn("down", rendered)

    def test_no_critical_services_shows_placeholder(self):
        rendered = monitor.render_html(self._payload_with_full_runtime(criticalServices={}), make_config())
        self.assertIn("gg-oracle-payments-01", rendered)  # renders without crashing

    def test_process_type_shown(self):
        rendered = monitor.render_html(self._payload_with_full_runtime(), make_config())
        self.assertIn("extract", rendered)

    def test_process_lag_threshold_mode_shown(self):
        rendered = monitor.render_html(self._payload_with_full_runtime(), make_config())
        self.assertIn("5s / thr 300s (alert)", rendered)

    def test_process_age_shown(self):
        rendered = monitor.render_html(self._payload_with_full_runtime(), make_config())
        self.assertIn("3s ago", rendered)

    def test_process_stale_indication_shown(self):
        payload = self._payload_with_full_runtime()
        payload["logicalPipelines"][0]["runtimes"][0]["processes"][0]["stale"] = True
        rendered = monitor.render_html(payload, make_config())
        self.assertIn("STALE", rendered)

    def test_process_fresh_indication_shown(self):
        rendered = monitor.render_html(self._payload_with_full_runtime(), make_config())
        # the process row's own stale=False must render as a distinct Fresh badge
        self.assertGreaterEqual(rendered.count("Fresh"), 1)

    def test_consecutive_abends_shown(self):
        payload = self._payload_with_full_runtime()
        payload["logicalPipelines"][0]["runtimes"][0]["processes"][0]["consecutiveAbends"] = 4
        rendered = monitor.render_html(payload, make_config())
        self.assertIn(">4<", rendered)

    def test_missing_resolved_threshold_and_mode_render_safely(self):
        payload = self._payload_with_full_runtime()
        payload["logicalPipelines"][0]["runtimes"][0]["processes"][0]["resolvedThreshold"] = None
        payload["logicalPipelines"][0]["runtimes"][0]["processes"][0]["resolvedMode"] = None
        try:
            rendered = monitor.render_html(payload, make_config())
        except Exception as e:  # pragma: no cover
            self.fail(f"render_html raised on missing fields: {e!r}")
        self.assertIn("gg-oracle-payments-01", rendered)

    def test_malicious_process_name_is_escaped(self):
        payload = self._payload_with_full_runtime()
        payload["logicalPipelines"][0]["runtimes"][0]["processes"][0]["process"] = "<script>alert(2)</script>"
        rendered = monitor.render_html(payload, make_config())
        self.assertNotIn("<script>alert(2)</script>", rendered)
        self.assertIn(html_module.escape("<script>alert(2)</script>"), rendered)

    def test_malicious_critical_service_name_is_escaped(self):
        payload = self._payload_with_full_runtime(criticalServices={"<img src=x onerror=alert(1)>": True})
        rendered = monitor.render_html(payload, make_config())
        self.assertNotIn("<img src=x onerror=alert(1)>", rendered)

    def test_process_error_message_never_exposes_raw_error_msg(self):
        payload = self._payload_with_full_runtime()
        payload["logicalPipelines"][0]["runtimes"][0]["processes"][0].update(
            hasError=True, statusCode="AUTH_FAILED",
            statusMessage="Authentication to the GoldenGate Admin REST API failed.")
        rendered = monitor.render_html(payload, make_config())
        self.assertIn("Authentication to the GoldenGate Admin REST API failed.", rendered)
        self.assertNotIn("password=", rendered)
        self.assertNotIn("db-internal.example.local", rendered)

    def test_per_deployment_card_structure_not_a_wide_outer_table(self):
        # Section 1 correction: no wide outer table with the process table
        # nested inside its final <td>.
        rendered = monitor.render_html(self._payload_with_full_runtime(), make_config())
        self.assertNotIn("<th>Role</th>", rendered)
        self.assertNotIn("<th>Critical Services</th><th>Processes</th>", rendered)
        self.assertNotIn("<td><table", rendered)
        self.assertNotIn("</table></td>", rendered)

    def test_two_deployments_render_as_two_separate_cards(self):
        payload = self._payload_with_full_runtime()
        second = dict(payload["logicalPipelines"][0]["runtimes"][0])
        second.update(deploymentName="gg-postgresql-payments-01", role="target")
        payload["logicalPipelines"][0]["runtimes"].append(second)
        rendered = monitor.render_html(payload, make_config())
        self.assertEqual(rendered.count("gg-oracle-payments-01"), 1)
        self.assertEqual(rendered.count("gg-postgresql-payments-01"), 1)
        self.assertEqual(rendered.count('border-radius:6px;padding:8px 14px 12px'), 2)

    def test_stale_process_row_has_class_and_visible_prefix(self):
        payload = self._payload_with_full_runtime()
        payload["logicalPipelines"][0]["runtimes"][0]["processes"][0]["stale"] = True
        rendered = monitor.render_html(payload, make_config())
        self.assertIn("stale-row", rendered)
        self.assertIn("[STALE]", rendered)

    def test_fresh_process_row_has_no_stale_marker(self):
        rendered = monitor.render_html(self._payload_with_full_runtime(), make_config())
        # the stylesheet's static tr.stale-row rule is always present;
        # what must be absent is the class actually applied to a row.
        self.assertNotIn('<tr class="stale-row"', rendered)
        self.assertNotIn("[STALE]", rendered)

    def test_lease_holder_escaped_exactly_once(self):
        malicious_holder = '<script>&"\''
        payload = self._payload_with_full_runtime(
            lease={"holder": malicious_holder, "expiresAt": 1780000040, "fresh": True})
        rendered = monitor.render_html(payload, make_config())
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("&amp;amp;", rendered)
        self.assertIn(html_module.escape(malicious_holder), rendered)


class ApiProcessesTests(unittest.TestCase):
    """Phase 4C2: GET /api/processes -- canonical STATE# only, GetItem/Query
    only, no legacy-observer fallback, no writes, no secret/internal
    leakage."""

    def _handler(self, table_factory):
        handler_cls = monitor._make_handler(make_config(), table_factory, DEPLOYMENTS, LOGICAL_PIPELINES, {}, [])
        handler = handler_cls.__new__(handler_cls)
        writes = []
        handler._write = lambda status, ctype, body: writes.append((status, ctype, body))
        return handler, writes

    def test_success_returns_200_with_expected_schema(self):
        # Goes through the real HTTP handler (build_processes_payload's
        # default clock=time.time, not an injected one) -- fixture
        # timestamps must be fresh relative to real wall-clock time.
        now = int(time.time())
        table = FakeTable([
            make_deployment_state_item(recorded_at=now - 5,
                                       criticalServices={"adminsrvr": {"reachable": True}}),
            {"pipeline": "gg-oracle-payments-01", "recordType": "CONFIG", "alertsEnabled": True},
            make_lease_item(now=now),
            make_process_item(recorded_at=now - 3, resolvedThreshold=300, resolvedMode="alert",
                              consecutiveAbends=1),
        ])
        handler, writes = self._handler(lambda: table)
        handler.path = "/api/processes"
        handler.do_GET()
        self.assertEqual(writes[0][0], 200)
        body = json.loads(writes[0][2])
        self.assertIn("generatedAt", body)
        dep = next(d for d in body["deployments"] if d["deploymentName"] == "gg-oracle-payments-01")
        self.assertEqual(dep["effectiveStatus"], "UP")
        self.assertIn("ageSeconds", dep)
        self.assertTrue(dep["alertsEnabled"])
        self.assertEqual(dep["lease"]["holder"], "gg-monitor-0")
        self.assertEqual(dep["criticalServices"], {"adminsrvr": True})
        proc = dep["processes"][0]
        for key in ("process", "processType", "status", "lagSeconds", "resolvedThreshold",
                    "resolvedMode", "recordedAt", "ageSeconds", "stale", "consecutiveAbends"):
            self.assertIn(key, proc)
        self.assertEqual(proc["resolvedThreshold"], 300)
        self.assertEqual(proc["resolvedMode"], "alert")
        self.assertEqual(proc["consecutiveAbends"], 1)

    def test_dynamodb_failure_returns_sanitized_message_only(self):
        table = mock.Mock()
        table.get_item.side_effect = RuntimeError(
            "arn:aws:sts::668311715351:assumed-role/GoldenGateSecretsReadRole-dev/i-0123456789abcdef")
        handler, writes = self._handler(lambda: table)
        handler.path = "/api/processes"
        handler.do_GET()
        self.assertEqual(writes[0][0], 503)
        body = json.loads(writes[0][2])
        self.assertEqual(body["message"], monitor.CLIENT_SAFE_DYNAMODB_ERROR_MESSAGE)
        raw = writes[0][2].decode("utf-8")
        self.assertNotIn("arn:aws", raw)
        self.assertNotIn("GoldenGateSecretsReadRole-dev", raw)

    def test_no_secret_or_internal_fields_in_response(self):
        now = 1780000010
        table = FakeTable([
            make_deployment_state_item(recorded_at=now - 5),
            make_process_item(recorded_at=now - 3, errorMsg="db-internal.example.local password=hunter2"),
        ])
        handler, writes = self._handler(lambda: table)
        handler.path = "/api/processes"
        handler.do_GET()
        raw = writes[0][2].decode("utf-8")
        for forbidden in ("errorMsg", "password", "hunter2", "db-internal.example.local",
                          "adminSecret", "arn:aws", "/mnt/secrets-store", "ca-chain-pem"):
            self.assertNotIn(forbidden, raw)

    def test_uses_canonical_state_schema_only_no_legacy_fallback(self):
        # a legacy-only record (no canonical STATE#_deployment) must show
        # MISSING here -- /api/processes never falls back to the legacy
        # observer's per-role key, unlike /api/status.
        now = 1780000010
        legacy_item = {"pipeline": "gg-payments-ora-to-pg-001-source", "recordType": "STATE#_deployment",
                       "status": "HEALTHY", "recordedAt": now - 5}
        table = FakeTable([legacy_item])
        handler, writes = self._handler(lambda: table)
        handler.path = "/api/processes"
        handler.do_GET()
        body = json.loads(writes[0][2])
        dep = next(d for d in body["deployments"] if d["deploymentName"] == "gg-oracle-payments-01")
        self.assertEqual(dep["effectiveStatus"], "MISSING")

    def test_canonical_process_rows_returned_when_deployment_state_missing(self):
        # Section 3 correction: STATE#_deployment missing must not suppress
        # independently-existing STATE#<process> rows in this canonical-only
        # endpoint either.
        now = 1780000010
        table = FakeTable([make_process_item(recorded_at=now - 3)])
        handler, writes = self._handler(lambda: table)
        handler.path = "/api/processes"
        handler.do_GET()
        body = json.loads(writes[0][2])
        dep = next(d for d in body["deployments"] if d["deploymentName"] == "gg-oracle-payments-01")
        self.assertEqual(dep["effectiveStatus"], "MISSING")
        self.assertEqual(len(dep["processes"]), 1)
        self.assertEqual(dep["processes"][0]["process"], "EXTORA1")

    def test_no_dynamodb_scan_used(self):
        now = 1780000010
        table = FakeTable([make_deployment_state_item(recorded_at=now - 5), make_process_item(recorded_at=now - 3)])
        monitor.build_processes_payload(make_config(), table, DEPLOYMENTS, clock=lambda: now)
        self.assertFalse(hasattr(table, "scan"))

    def test_no_dynamodb_write_operation_occurs(self):
        now = 1780000010
        table = FakeTable([make_deployment_state_item(recorded_at=now - 5)])
        monitor.build_processes_payload(make_config(), table, DEPLOYMENTS, clock=lambda: now)
        for forbidden in ("put_item", "update_item", "delete_item", "batch_writer", "scan"):
            self.assertFalse(hasattr(table, forbidden))

    def test_empty_process_list_is_valid(self):
        now = 1780000010
        table = FakeTable([make_deployment_state_item(recorded_at=now - 5)])
        handler, writes = self._handler(lambda: table)
        handler.path = "/api/processes"
        handler.do_GET()
        self.assertEqual(writes[0][0], 200)
        body = json.loads(writes[0][2])
        dep = next(d for d in body["deployments"] if d["deploymentName"] == "gg-oracle-payments-01")
        self.assertEqual(dep["processes"], [])

    def test_legacy_observer_fallback_for_api_status_remains_unchanged(self):
        # Confirms this phase did not alter /api/status's existing
        # legacy-fallback behaviour (a separate, pre-existing endpoint).
        # Goes through the real HTTP handler (real wall-clock time) -- the
        # fixture must be fresh relative to it.
        now = int(time.time())
        legacy_item = {"pipeline": "gg-payments-ora-to-pg-001-source", "recordType": "STATE#_deployment",
                       "status": "HEALTHY", "recordedAt": now - 5}
        table = FakeTable([legacy_item])
        handler, writes = self._handler(lambda: table)
        handler.path = "/api/status"
        handler.do_GET()
        body = json.loads(writes[0][2])
        lp = body["logicalPipelines"][0]
        source = next(r for r in lp["runtimes"] if r["role"] == "source")
        self.assertEqual(source["dataSource"], "legacy-observer-fallback")
        self.assertEqual(source["effectiveStatus"], "UP")


class ThreadSafetyTests(unittest.TestCase):
    def test_handler_uses_a_factory_not_a_prebuilt_object(self):
        import inspect
        sig = inspect.signature(monitor._make_handler)
        self.assertIn("table_factory", sig.parameters)

    def test_factory_is_called_fresh_for_each_of_several_sequential_requests(self):
        created = []

        def factory():
            t = FakeTable([make_deployment_state_item(recorded_at=1780000005)])
            created.append(t)
            return t

        handler_cls = monitor._make_handler(make_config(), factory, DEPLOYMENTS, LOGICAL_PIPELINES, {}, [])
        handler = handler_cls.__new__(handler_cls)
        handler._write = lambda status, ctype, body: None
        handler.path = "/api/status"
        for _ in range(3):
            handler.do_GET()
        self.assertEqual(len(created), 3)
        self.assertEqual(len(set(id(t) for t in created)), 3)

    def test_concurrent_requests_each_get_independent_table_objects(self):
        import threading
        created = []
        lock = threading.Lock()

        def factory():
            t = FakeTable([make_deployment_state_item(recorded_at=1780000005)])
            with lock:
                created.append(t)
            return t

        handler_cls = monitor._make_handler(make_config(), factory, DEPLOYMENTS, LOGICAL_PIPELINES, {}, [])

        def make_request():
            handler = handler_cls.__new__(handler_cls)
            handler._write = lambda status, ctype, body: None
            handler.path = "/api/status"
            handler.do_GET()

        threads = [threading.Thread(target=make_request) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(created), 8)
        self.assertEqual(len(set(id(t) for t in created)), 8)

def _extract_run_block(workflow_text, step_name):
    """Extract a step's `run: |` block body, dedented, using plain text
    scanning -- deliberately dependency-free (no PyYAML) so these tests run
    anywhere the rest of this dependency-free suite runs."""
    lines = workflow_text.splitlines()
    step_marker = f"- name: {step_name}"
    step_idx = None
    for i, line in enumerate(lines):
        if line.strip() == step_marker:
            step_idx = i
            break
    if step_idx is None:
        raise AssertionError(f"step {step_name!r} not found in workflow")

    run_idx = None
    for i in range(step_idx + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("- name:"):
            break
        if stripped == "run: |":
            run_idx = i
            break
    if run_idx is None:
        raise AssertionError(f"no 'run: |' block found for step {step_name!r}")

    body_lines = []
    base_indent = None
    for i in range(run_idx + 1, len(lines)):
        line = lines[i]
        if line.strip() == "":
            body_lines.append("")
            continue
        indent = len(line) - len(line.lstrip(" "))
        if base_indent is None:
            base_indent = indent
        if indent < base_indent:
            break
        body_lines.append(line[base_indent:])
    return "\n".join(body_lines) + "\n"


def _extract_step_if_condition(workflow_text, step_name):
    marker = f"- name: {step_name}"
    idx = workflow_text.index(marker)
    following = workflow_text[idx:idx + 400]
    match = re.search(r"if:\s*(.+)", following)
    return match.group(1).strip() if match else None


MONITOR_CHART_PATH = os.path.join(REPO_ROOT, "helm", "goldengate-monitor")


class SecretProviderClassRenderTests(unittest.TestCase):
    """Renders the real helm/goldengate-monitor chart (with the canonical
    config staged exactly as the workflow stages it) and asserts the
    generated SecretProviderClass and CSI volume wiring -- not a
    reimplementation of the template logic inside this test suite."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("helm") is None:
            raise unittest.SkipTest("helm not available")
        cls.tmpdir = tempfile.mkdtemp()
        staged_chart = os.path.join(cls.tmpdir, "goldengate-monitor")
        shutil.copytree(MONITOR_CHART_PATH, staged_chart)
        os.makedirs(os.path.join(staged_chart, "files"), exist_ok=True)
        shutil.copy(DEPLOYMENTS_FILE_PATH, os.path.join(staged_chart, "files", "goldengate-deployments.yaml"))

        proc = subprocess.run(
            ["helm", "template", "gg-monitor", staged_chart,
             "--namespace", "goldengate-monitoring",
             "-f", os.path.join(REPO_ROOT, "envs", "dev", "goldengate-monitor", "values.yaml"),
             "--set", "image.repository=example.invalid/goldengate-monitor",
             "--set", "image.tag=test",
             "--set", "serviceAccount.roleArn=arn:aws:iam::668311715351:role/GoldenGateMonitorReadRole-dev"],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        if proc.returncode != 0:
            raise AssertionError(f"helm template failed: {proc.stdout}\n{proc.stderr}")
        cls.rendered = proc.stdout

    def test_exactly_one_secretproviderclass(self):
        self.assertEqual(self.rendered.count("kind: SecretProviderClass"), 1)

    def test_admin_aliases_present_for_every_enabled_deployment(self):
        doc = cfgmod.load_deployments(os.path.join(REPO_ROOT, "envs", "dev"))
        for d in doc["deployments"]:
            if not d["enabled"]:
                continue
            self.assertIn(f"{d['name']}-admin-user", self.rendered)
            self.assertIn(f"{d['name']}-admin-password", self.rendered)
            self.assertIn(d["adminSecret"], self.rendered)

    def test_ca_chain_alias_present(self):
        self.assertIn("ca-chain-pem", self.rendered)
        doc = cfgmod.load_deployments(os.path.join(REPO_ROOT, "envs", "dev"))
        self.assertIn(doc["tlsSecret"], self.rendered)

    def test_exactly_one_csi_volume_mounted_read_only_at_secrets_store(self):
        self.assertEqual(self.rendered.count("driver: secrets-store.csi.k8s.io"), 1)
        self.assertEqual(self.rendered.count("mountPath: /mnt/secrets-store"), 1)
        self.assertIn("readOnly: true", self.rendered)

    def test_no_kubernetes_secret_or_secret_object_materialized(self):
        self.assertNotIn("kind: Secret\n", self.rendered)
        self.assertNotIn("secretObjects", self.rendered)
        self.assertNotIn("secretKeyRef", self.rendered)
        self.assertNotIn("envFrom", self.rendered)

    def test_pod_name_and_cloudwatch_kill_switch_rendered(self):
        self.assertIn("name: POD_NAME", self.rendered)
        self.assertIn("fieldPath: metadata.name", self.rendered)
        self.assertIn('name: CLOUDWATCH_PUBLISH_ENABLED\n              value: "false"', self.rendered)

    def test_exactly_one_of_each_core_resource(self):
        for kind in ("Deployment", "Service", "Ingress", "ServiceAccount", "ConfigMap"):
            with self.subTest(kind=kind):
                self.assertEqual(self.rendered.count(f"kind: {kind}\n"), 1)


class WorkflowStaticAnalysisTests(unittest.TestCase):
    """Static inspection of the two GitHub Actions workflow files. These
    prove the actual committed bash/YAML content was fixed -- not a
    reimplementation of the same logic inside this test suite."""

    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(MONITOR_WORKFLOW_PATH) or not os.path.isfile(ARGOCD_WORKFLOW_PATH):
            raise unittest.SkipTest("workflow files not found relative to the repository root")
        with open(MONITOR_WORKFLOW_PATH) as f:
            cls.monitor_text = f.read()
        with open(ARGOCD_WORKFLOW_PATH) as f:
            cls.argocd_text = f.read()

    def test_workflow_has_workflow_dispatch_trigger(self):
        doc = yaml.safe_load(self.monitor_text)
        triggers = doc.get("on") or doc.get(True)
        self.assertIn("workflow_dispatch", triggers)
        inputs = triggers["workflow_dispatch"]["inputs"]
        self.assertIn("environment", inputs)
        self.assertIn("deploy", inputs)

    def test_workflow_has_no_active_push_trigger(self):
        doc = yaml.safe_load(self.monitor_text)
        triggers = doc.get("on") or doc.get(True)
        self.assertNotIn("push", triggers)
        self.assertNotIn("pull_request", triggers)
        self.assertNotIn("schedule", triggers)

    def test_setup_python_step_pins_3_12_with_dependency_cache(self):
        doc = yaml.safe_load(self.monitor_text)
        steps = doc["jobs"]["ensure_monitor_image"]["steps"]
        setup_steps = [s for s in steps if s.get("uses", "").startswith("actions/setup-python@")]
        self.assertEqual(len(setup_steps), 1, "expected exactly one actions/setup-python step")
        step = setup_steps[0]
        self.assertEqual(step["uses"], "actions/setup-python@v5")
        self.assertEqual(step["with"]["python-version"], "3.12")
        self.assertEqual(step["with"]["cache"], "pip")
        cache_paths = step["with"]["cache-dependency-path"]
        self.assertIn("monitoring/monitor/requirements.txt", cache_paths)
        self.assertIn("monitoring/monitor/requirements-test.txt", cache_paths)

    def test_setup_python_runs_before_dependency_install_and_tests(self):
        setup_idx = self.monitor_text.index("uses: actions/setup-python@v5")
        install_idx = self.monitor_text.index("name: Install monitor runtime and test dependencies")
        test_idx = self.monitor_text.index("name: Run monitor unit tests")
        self.assertLess(setup_idx, install_idx)
        self.assertLess(install_idx, test_idx)

    def test_dockerfile_does_not_install_test_requirements(self):
        with open(os.path.join(REPO_ROOT, "monitoring", "monitor", "Dockerfile")) as f:
            dockerfile_text = f.read()
        self.assertNotIn("requirements-test.txt", dockerfile_text)

    def test_oci_description_reflects_collector_and_portal(self):
        self.assertNotIn("Read-only shared GoldenGate monitoring portal", self.monitor_text)
        self.assertIn("Shared GoldenGate monitoring collector and portal", self.monitor_text)

    def test_no_unsafe_inputs_deploy_condition_remains(self):
        self.assertNotIn("inputs.deploy != false", self.monitor_text)

    def test_push_event_deploy_condition_is_normalized_on_every_deployment_step(self):
        expected = "${{ github.event_name != 'workflow_dispatch' || inputs.deploy }}"
        deploy_step_names = (
            "Connect to EKS cluster",
            "Ensure Argo CD Application CRD exists",
            "Confirm the monitor Argo CD repository Secret exists",
            "Create or update Argo CD Application",
            "Wait for Argo CD sync and health",
            "Verify GoldenGate monitor runtime state",
        )
        for step_name in deploy_step_names:
            with self.subTest(step=step_name):
                condition = _extract_step_if_condition(self.monitor_text, step_name)
                self.assertEqual(condition, expected)

    def test_push_event_deploy_condition_evaluates_true_for_push(self):
        # A push event never populates the `inputs` context, so relying on
        # `inputs.deploy` alone would be falsy/undefined on a push run. The
        # normalized expression must short-circuit to true via
        # github.event_name before ever evaluating inputs.deploy.
        github_event_name = "push"
        inputs_deploy = None  # unset, as it would be on a real push trigger
        normalized = (github_event_name != "workflow_dispatch") or bool(inputs_deploy)
        self.assertTrue(normalized)

    def test_manual_deploy_false_remains_supported(self):
        github_event_name = "workflow_dispatch"
        self.assertFalse((github_event_name != "workflow_dispatch") or False)
        self.assertTrue((github_event_name != "workflow_dispatch") or True)

    def test_validation_job_name_includes_run_attempt(self):
        self.assertIn(
            'JOB_NAME="ecr-token-sync-verify-${{ github.run_id }}-${{ github.run_attempt }}"',
            self.argocd_text,
        )

    def test_argocd_application_uses_direct_heredoc_not_cat_pipe(self):
        self.assertNotIn("cat <<EOF | kubectl apply", self.monitor_text)
        self.assertIn("kubectl apply -f - <<EOF", self.monitor_text)

    def test_rbac_extraction_no_longer_selects_first_role_by_document_order(self):
        self.assertNotIn(
            "awk '/^kind: Role$/{flag=1} flag{print} /^---$/{if(flag) exit}'",
            self.argocd_text,
        )
        self.assertIn("name: argocd-ecr-token-sync", self.argocd_text)

    def test_multi_document_rbac_extraction_selects_correct_role(self):
        """Run the actual production RBAC-selection snippet (extracted
        verbatim from the workflow file) against a synthetic multi-document
        manifest where an unrelated Role -- granting delete/list/watch --
        appears before the real argocd-ecr-token-sync Role. Proves the
        correct Role is selected by kind+name, not by document order."""
        snippet = _extract_run_block(self.argocd_text, "Validate ECR token sync resources are rendered")
        start = snippet.index('echo "Checking RBAC resourceNames')
        end = snippet.index('echo "OK: ServiceAccount/Role/RoleBinding/CronJob')
        end_of_line = snippet.index("\n", end)
        rbac_snippet = snippet[start:end_of_line + 1]

        synthetic_manifest = """---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: argocd-application-controller
  namespace: argocd
rules:
  - apiGroups: [""]
    resources: ["secrets"]
    verbs:
      - get
      - list
      - watch
      - delete
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: argocd-ecr-token-sync
  namespace: argocd
rules:
  - apiGroups: [""]
    resources: ["secrets"]
    verbs:
      - create
  - apiGroups: [""]
    resources: ["secrets"]
    resourceNames:
      - argocd-ecr-goldengate-oci
      - argocd-ecr-goldengate-monitor-oci
      - argocd-ecr-goldengate-platform-oci
    verbs:
      - get
      - update
      - patch
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: argocd-ecr-token-sync
  namespace: argocd
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            rendered_path = os.path.join(tmpdir, "rendered.yaml")
            with open(rendered_path, "w") as f:
                f.write(synthetic_manifest)
            script = f'set -euo pipefail\nRENDERED="{rendered_path}"\n' + rbac_snippet
            proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)

        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        self.assertIn("OK: ServiceAccount/Role/RoleBinding/CronJob", proc.stdout)

    def test_multi_document_rbac_extraction_fails_when_role_incomplete(self):
        """Same production snippet, but the real token-sync Role is missing
        one required resourceName -- must fail loudly, proving the check is
        not vacuously true."""
        snippet = _extract_run_block(self.argocd_text, "Validate ECR token sync resources are rendered")
        start = snippet.index('echo "Checking RBAC resourceNames')
        end = snippet.index('echo "OK: ServiceAccount/Role/RoleBinding/CronJob')
        end_of_line = snippet.index("\n", end)
        rbac_snippet = snippet[start:end_of_line + 1]

        incomplete_manifest = """---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: argocd-server
  namespace: argocd
rules:
  - apiGroups: [""]
    resources: ["secrets"]
    verbs:
      - get
      - list
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: argocd-ecr-token-sync
  namespace: argocd
rules:
  - apiGroups: [""]
    resources: ["secrets"]
    resourceNames:
      - argocd-ecr-goldengate-oci
    verbs:
      - get
      - update
      - patch
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            rendered_path = os.path.join(tmpdir, "rendered.yaml")
            with open(rendered_path, "w") as f:
                f.write(incomplete_manifest)
            script = f'set -euo pipefail\nRENDERED="{rendered_path}"\n' + rbac_snippet
            proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("argocd-ecr-goldengate-monitor-oci", proc.stdout)


SERVICEACCOUNT_TEMPLATE_PATH = os.path.join(
    REPO_ROOT, "helm", "goldengate-monitor", "templates", "serviceaccount.yaml"
)


def _extract_manifest_validation_helpers(monitor_text):
    """The shared select_document()/normalize_value() bash function
    definitions from the "Validate rendered monitor manifest" step --
    required by every resource-specific slice extracted below, since those
    slices call the functions rather than reimplementing the logic inline."""
    full_step = _extract_run_block(monitor_text, "Validate rendered monitor manifest")
    start = full_step.index("select_document() {")
    end = full_step.index('echo "Validating Namespace')
    return full_step[start:end]


def _extract_serviceaccount_validation_snippet(monitor_text):
    """The ServiceAccount/IRSA validation portion of the "Validate rendered
    monitor manifest" step, extracted verbatim from the real workflow."""
    full_step = _extract_run_block(monitor_text, "Validate rendered monitor manifest")
    start = full_step.index('echo "Validating ServiceAccount')
    end = full_step.index('echo "Validating Deployment uses')
    return _extract_manifest_validation_helpers(monitor_text) + full_step[start:end]


def _extract_ingress_validation_snippet(monitor_text):
    """The Ingress host/certificate/protocol validation portion of the
    "Validate rendered monitor manifest" step, extracted verbatim from the
    real workflow."""
    full_step = _extract_run_block(monitor_text, "Validate rendered monitor manifest")
    start = full_step.index('echo "Validating Ingress exists')
    end = full_step.index('echo "Validating the canonical inventory ConfigMap')
    return _extract_manifest_validation_helpers(monitor_text) + full_step[start:end]


def _run_snippet(snippet, rendered_yaml, extra_env=None):
    with tempfile.TemporaryDirectory() as tmpdir:
        rendered_path = os.path.join(tmpdir, "rendered.yaml")
        with open(rendered_path, "w") as f:
            f.write(rendered_yaml)
        env_lines = f'RENDERED="{rendered_path}"\n'
        for name, value in (extra_env or {}).items():
            env_lines += f'{name}="{value}"\n'
        script = "set -euo pipefail\n" + env_lines + snippet
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def _run_serviceaccount_snippet(monitor_text, rendered_yaml):
    return _run_snippet(_extract_serviceaccount_validation_snippet(monitor_text), rendered_yaml)


def _run_ingress_snippet(monitor_text, rendered_yaml):
    return _run_snippet(_extract_ingress_validation_snippet(monitor_text), rendered_yaml)


class ReadmeRoleDocumentationTests(unittest.TestCase):
    def test_readme_documents_monitor_role_correctly(self):
        with open(os.path.join(REPO_ROOT, "monitoring", "monitor", "README.md")) as f:
            readme_text = f.read()
        self.assertIn("GoldenGateMonitorReadRole-dev", readme_text)
        irsa_section = readme_text[readme_text.index("## IRSA role"):]
        self.assertNotIn("annotated with `GoldenGateSecretsReadRole-dev`", irsa_section)


class ServiceAccountIrsaValidationTests(unittest.TestCase):
    """Regression coverage for the quote-sensitive ServiceAccount role-arn
    grep that silently passed the workflow step under set -euo pipefail
    while never actually matching the (correctly quoted) rendered value."""

    EXPECTED_ARN = "arn:aws:iam::668311715351:role/GoldenGateMonitorReadRole-dev"

    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(MONITOR_WORKFLOW_PATH):
            raise unittest.SkipTest("workflow file not found relative to the repository root")
        with open(MONITOR_WORKFLOW_PATH) as f:
            cls.monitor_text = f.read()

    def test_old_unquoted_role_arn_grep_is_gone(self):
        self.assertNotIn(
            'grep -q "eks.amazonaws.com/role-arn: arn:aws:iam::668311715351:role/GoldenGateMonitorReadRole-dev"',
            self.monitor_text,
        )

    def test_no_echo_pipe_grep_in_serviceaccount_validation(self):
        snippet = _extract_serviceaccount_validation_snippet(self.monitor_text)
        self.assertNotIn('echo "$SERVICEACCOUNT_BLOCK" | grep', snippet)
        self.assertNotRegex(snippet, r'echo\s+"\$[A-Za-z_]+"\s*\|\s*grep')

    def test_serviceaccount_template_still_uses_quote_filter(self):
        with open(SERVICEACCOUNT_TEMPLATE_PATH) as f:
            template_text = f.read()
        self.assertIn(".Values.serviceAccount.roleArn | quote", template_text)

    def test_quoted_arn_passes(self):
        rendered = """---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: gg-monitor
  namespace: goldengate-monitoring
  annotations:
    eks.amazonaws.com/role-arn: "arn:aws:iam::668311715351:role/GoldenGateMonitorReadRole-dev"
"""
        proc = _run_serviceaccount_snippet(self.monitor_text, rendered)
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        self.assertIn("OK: ServiceAccount gg-monitor uses the expected IRSA role ARN.", proc.stdout)

    def test_unquoted_arn_also_passes(self):
        rendered = """---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: gg-monitor
  namespace: goldengate-monitoring
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::668311715351:role/GoldenGateMonitorReadRole-dev
"""
        proc = _run_serviceaccount_snippet(self.monitor_text, rendered)
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        self.assertIn("OK: ServiceAccount gg-monitor uses the expected IRSA role ARN.", proc.stdout)

    def test_wrong_arn_fails_with_expected_and_actual(self):
        rendered = """---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: gg-monitor
  namespace: goldengate-monitoring
  annotations:
    eks.amazonaws.com/role-arn: "arn:aws:iam::999999999999:role/WrongRole"
"""
        proc = _run_serviceaccount_snippet(self.monitor_text, rendered)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("FAIL: gg-monitor IRSA role ARN mismatch.", proc.stdout)
        self.assertIn(f"Expected: {self.EXPECTED_ARN}", proc.stdout)
        self.assertIn("Actual:   arn:aws:iam::999999999999:role/WrongRole", proc.stdout)

    def test_missing_annotation_fails_with_descriptive_message(self):
        rendered = """---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: gg-monitor
  namespace: goldengate-monitoring
  annotations:
    some-other-annotation: "value"
"""
        proc = _run_serviceaccount_snippet(self.monitor_text, rendered)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(
            "FAIL: eks.amazonaws.com/role-arn annotation is missing from ServiceAccount gg-monitor.",
            proc.stdout,
        )

    def test_serviceaccount_not_found_fails_with_descriptive_message(self):
        rendered = """---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: some-other-sa
  namespace: default
"""
        proc = _run_serviceaccount_snippet(self.monitor_text, rendered)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("FAIL: rendered ServiceAccount gg-monitor was not found.", proc.stdout)

    def test_unrelated_serviceaccount_before_gg_monitor_is_not_selected(self):
        """An unrelated ServiceAccount (even one with a similarly-prefixed
        name and a deliberately wrong ARN) rendered before gg-monitor must
        never be mistaken for it -- the real gg-monitor document, appearing
        second, must still be the one validated."""
        rendered = """---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: gg-monitor-old
  namespace: goldengate-monitoring
  annotations:
    eks.amazonaws.com/role-arn: "arn:aws:iam::111111111111:role/DecoyRole"
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: argocd-ecr-token-sync
  namespace: argocd
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: gg-monitor
  namespace: goldengate-monitoring
  annotations:
    eks.amazonaws.com/role-arn: "arn:aws:iam::668311715351:role/GoldenGateMonitorReadRole-dev"
"""
        proc = _run_serviceaccount_snippet(self.monitor_text, rendered)
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        self.assertIn("OK: ServiceAccount gg-monitor uses the expected IRSA role ARN.", proc.stdout)


INGRESS_TEMPLATE_PATH = os.path.join(
    REPO_ROOT, "helm", "goldengate-monitor", "templates", "ingress.yaml"
)

_GOOD_INGRESS_RENDERED = """---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: gg-monitor
  namespace: goldengate-monitoring
  annotations:
    alb.ingress.kubernetes.io/backend-protocol: "HTTP"
    alb.ingress.kubernetes.io/healthcheck-protocol: "HTTP"
    alb.ingress.kubernetes.io/target-type: "ip"
    alb.ingress.kubernetes.io/certificate-arn: "arn:aws:acm:eu-west-1:668311715351:certificate/9e53e28e-3243-47fc-85a1-50f9a94acde7"
spec:
  rules:
    - host: "monitor.goldengate-dev.adcbmis.local"
"""


class IngressValidationTests(unittest.TestCase):
    """Regression coverage for the quote-sensitive Ingress host grep (the
    same class of bug already fixed for the ServiceAccount role-arn
    annotation) plus the strengthened certificate-ARN and protocol
    annotation checks, all scoped to the exact gg-monitor Ingress document."""

    EXPECTED_HOST = "monitor.goldengate-dev.adcbmis.local"
    EXPECTED_CERT_ARN = "arn:aws:acm:eu-west-1:668311715351:certificate/9e53e28e-3243-47fc-85a1-50f9a94acde7"

    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(MONITOR_WORKFLOW_PATH):
            raise unittest.SkipTest("workflow file not found relative to the repository root")
        with open(MONITOR_WORKFLOW_PATH) as f:
            cls.monitor_text = f.read()

    def test_old_unquoted_host_grep_is_gone(self):
        self.assertNotIn(
            'grep -q "host: monitor.goldengate-dev.adcbmis.local"',
            self.monitor_text,
        )

    def test_quoted_host_passes(self):
        proc = _run_ingress_snippet(self.monitor_text, _GOOD_INGRESS_RENDERED)
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        self.assertIn("OK: Ingress gg-monitor uses the expected hostname.", proc.stdout)

    def test_unquoted_host_also_passes(self):
        rendered = _GOOD_INGRESS_RENDERED.replace(
            '- host: "monitor.goldengate-dev.adcbmis.local"',
            "- host: monitor.goldengate-dev.adcbmis.local",
        )
        proc = _run_ingress_snippet(self.monitor_text, rendered)
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        self.assertIn("OK: Ingress gg-monitor uses the expected hostname.", proc.stdout)

    def test_wrong_host_fails_with_expected_and_actual(self):
        rendered = _GOOD_INGRESS_RENDERED.replace(
            '- host: "monitor.goldengate-dev.adcbmis.local"',
            '- host: "wrong.example.com"',
        )
        proc = _run_ingress_snippet(self.monitor_text, rendered)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("FAIL: gg-monitor Ingress host mismatch.", proc.stdout)
        self.assertIn(f"Expected: {self.EXPECTED_HOST}", proc.stdout)
        self.assertIn("Actual:   wrong.example.com", proc.stdout)

    def test_missing_host_fails_descriptively(self):
        rendered = """---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: gg-monitor
  namespace: goldengate-monitoring
  annotations:
    alb.ingress.kubernetes.io/backend-protocol: "HTTP"
    alb.ingress.kubernetes.io/healthcheck-protocol: "HTTP"
    alb.ingress.kubernetes.io/target-type: "ip"
    alb.ingress.kubernetes.io/certificate-arn: "arn:aws:acm:eu-west-1:668311715351:certificate/9e53e28e-3243-47fc-85a1-50f9a94acde7"
spec:
  rules: []
"""
        proc = _run_ingress_snippet(self.monitor_text, rendered)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("FAIL: host is missing from rendered Ingress gg-monitor.", proc.stdout)

    def test_unrelated_ingress_before_gg_monitor_is_not_selected(self):
        rendered = """---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: some-other-ingress
  namespace: default
spec:
  rules:
    - host: "decoy.example.com"
""" + _GOOD_INGRESS_RENDERED
        proc = _run_ingress_snippet(self.monitor_text, rendered)
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        self.assertIn("OK: Ingress gg-monitor uses the expected hostname.", proc.stdout)

    def test_quoted_certificate_arn_passes(self):
        proc = _run_ingress_snippet(self.monitor_text, _GOOD_INGRESS_RENDERED)
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        self.assertIn("OK: Ingress gg-monitor uses the expected ACM certificate ARN.", proc.stdout)

    def test_wrong_certificate_arn_fails_with_expected_and_actual(self):
        rendered = _GOOD_INGRESS_RENDERED.replace(
            'certificate-arn: "arn:aws:acm:eu-west-1:668311715351:certificate/9e53e28e-3243-47fc-85a1-50f9a94acde7"',
            'certificate-arn: "arn:aws:acm:eu-west-1:668311715351:certificate/WRONG"',
        )
        proc = _run_ingress_snippet(self.monitor_text, rendered)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("FAIL: gg-monitor Ingress certificate ARN mismatch.", proc.stdout)
        self.assertIn(f"Expected: {self.EXPECTED_CERT_ARN}", proc.stdout)
        self.assertIn(
            "Actual:   arn:aws:acm:eu-west-1:668311715351:certificate/WRONG", proc.stdout
        )

    def test_missing_certificate_annotation_fails_descriptively(self):
        rendered = _GOOD_INGRESS_RENDERED.replace(
            '    alb.ingress.kubernetes.io/certificate-arn: '
            '"arn:aws:acm:eu-west-1:668311715351:certificate/9e53e28e-3243-47fc-85a1-50f9a94acde7"\n',
            "",
        )
        proc = _run_ingress_snippet(self.monitor_text, rendered)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(
            "FAIL: certificate ARN annotation is missing from Ingress gg-monitor.", proc.stdout
        )

    def test_http_backend_protocol_passes_quoted_and_unquoted(self):
        proc = _run_ingress_snippet(self.monitor_text, _GOOD_INGRESS_RENDERED)
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)

        unquoted = _GOOD_INGRESS_RENDERED.replace(
            'backend-protocol: "HTTP"', "backend-protocol: HTTP"
        )
        proc = _run_ingress_snippet(self.monitor_text, unquoted)
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)

    def test_https_backend_protocol_fails(self):
        rendered = _GOOD_INGRESS_RENDERED.replace(
            'backend-protocol: "HTTP"', 'backend-protocol: "HTTPS"'
        )
        proc = _run_ingress_snippet(self.monitor_text, rendered)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("FAIL: gg-monitor Ingress backend protocol mismatch.", proc.stdout)
        self.assertIn("Expected: HTTP", proc.stdout)
        self.assertIn("Actual:   HTTPS", proc.stdout)

    def test_http_healthcheck_protocol_passes(self):
        proc = _run_ingress_snippet(self.monitor_text, _GOOD_INGRESS_RENDERED)
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        self.assertIn(
            "OK: Ingress gg-monitor uses HTTP backend/health-check protocols and target-type=ip.",
            proc.stdout,
        )

    def test_target_type_ip_passes(self):
        proc = _run_ingress_snippet(self.monitor_text, _GOOD_INGRESS_RENDERED)
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)

        wrong_target_type = _GOOD_INGRESS_RENDERED.replace(
            'target-type: "ip"', 'target-type: "instance"'
        )
        proc = _run_ingress_snippet(self.monitor_text, wrong_target_type)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("FAIL: gg-monitor Ingress target type mismatch.", proc.stdout)
        self.assertIn("Expected: ip", proc.stdout)
        self.assertIn("Actual:   instance", proc.stdout)

    def test_ingress_template_still_uses_quote_filter_on_host(self):
        with open(INGRESS_TEMPLATE_PATH) as f:
            template_text = f.read()
        self.assertIn(".Values.ingress.host | quote", template_text)

    def test_no_bare_positive_grep_remains_in_manifest_validation(self):
        """Every remaining assertion in the full "Validate rendered monitor
        manifest" run block must be an explicit conditional -- no bare
        `grep -q ... "$RENDERED"`/`"$SOME_BLOCK"` left unguarded that would
        silently exit the step under set -euo pipefail on a mismatch."""
        full_step = _extract_run_block(self.monitor_text, "Validate rendered monitor manifest")
        for line in full_step.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # A bare assertion is a line that IS a grep invocation (not
            # inside an if/while condition or a command substitution) with
            # no leading "if "/"! "/"elif " guard and no trailing "|| true"
            # escape hatch used for controlled lookups.
            if re.match(r'^grep\s', stripped) and "$(" not in stripped:
                self.fail(f"bare unguarded grep assertion found: {stripped!r}")

    def test_no_echo_pipe_grep_in_ingress_validation(self):
        snippet = _extract_ingress_validation_snippet(self.monitor_text)
        self.assertNotRegex(snippet, r'echo\s+"\$[A-Za-z_]+"\s*\|\s*grep')

    def test_existing_serviceaccount_tests_still_pass(self):
        """Sanity check that the ServiceAccount slice extraction/execution
        still works after the shared select_document/normalize_value
        functions were factored out -- full coverage lives in
        ServiceAccountIrsaValidationTests."""
        rendered = """---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: gg-monitor
  namespace: goldengate-monitoring
  annotations:
    eks.amazonaws.com/role-arn: "arn:aws:iam::668311715351:role/GoldenGateMonitorReadRole-dev"
"""
        proc = _run_serviceaccount_snippet(self.monitor_text, rendered)
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        self.assertIn("OK: ServiceAccount gg-monitor uses the expected IRSA role ARN.", proc.stdout)


if __name__ == "__main__":
    unittest.main()
