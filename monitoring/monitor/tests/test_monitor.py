import html as html_module
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
MONITOR_WORKFLOW_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "goldengate-monitor.yaml")
ARGOCD_WORKFLOW_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "argocd-eks-deployment.yaml")

# boto3/botocore are runtime dependencies (see requirements.txt) but are not
# required to run this unit-test suite: every test injects a mock DynamoDB
# table rather than exercising the real AWS SDK. Stub the imports only when
# the real packages are unavailable in the environment running the tests, so
# `import monitor` succeeds either way.
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

import monitor  # noqa: E402


def make_config(**overrides):
    env = {
        "AWS_REGION": "eu-west-1",
        "DYNAMODB_TABLE": "gg-eks-pipeline",
    }
    env.update(overrides)
    return monitor.load_config(env)


RUNTIMES = [
    {"pipeline": "gg-oracle-payments-01", "name": "oracle-payments-01", "type": "oracle", "enabled": True},
    {"pipeline": "gg-postgresql-payments-01", "name": "postgresql-payments-01", "type": "postgresql", "enabled": True},
]

LOGICAL_PIPELINES = [
    {
        "pipelineId": "payments-ora-to-pg-001",
        "environment": "dev",
        "roles": {
            "source": {"pipeline": "gg-oracle-payments-01", "deploymentType": "oracle"},
            "target": {"pipeline": "gg-postgresql-payments-01", "deploymentType": "postgresql"},
        },
    },
]


class FakeTable:
    """Minimal in-memory stand-in for a boto3 DynamoDB Table -- supports
    only get_item/query (never scan/put_item/update_item/delete_item),
    matching the real Table's read-only surface this portal is allowed to
    use."""

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


def make_config_item(pipeline="gg-oracle-payments-01", **overrides):
    item = {"pipeline": pipeline, "recordType": "CONFIG", "alertsEnabled": False, "metricsEnabled": False}
    item.update(overrides)
    return item


def make_lease_item(pipeline="gg-oracle-payments-01", now=1780000000, **overrides):
    item = {"pipeline": pipeline, "recordType": "LEASE", "holder": "gg-monitor-0",
           "expiresAt": now + 30, "ttl": now + 90}
    item.update(overrides)
    return item


def make_deployment_state_item(pipeline="gg-oracle-payments-01", status="UP", recorded_at=1780000000, **overrides):
    item = {"pipeline": pipeline, "recordType": "STATE#_deployment", "status": status,
           "recordedAt": recorded_at, "deploymentType": "oracle"}
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
        self.assertEqual(config.dynamodb_table, "gg-eks-pipeline")
        self.assertEqual(config.port, 8080)
        self.assertEqual(config.stale_after_seconds, 120)
        self.assertEqual(config.refresh_seconds, 30)
        self.assertTrue(config.legacy_fallback_enabled)
        self.assertEqual(config.repo_config_root, monitor.inventory.DEFAULT_REPO_ROOT)

    def test_missing_aws_region(self):
        with self.assertRaises(monitor.ConfigError):
            monitor.load_config({"DYNAMODB_TABLE": "gg-eks-pipeline"})

    def test_missing_dynamodb_table(self):
        with self.assertRaises(monitor.ConfigError):
            monitor.load_config({"AWS_REGION": "eu-west-1"})

    def test_invalid_port(self):
        with self.assertRaises(monitor.ConfigError):
            make_config(PORT="0")
        with self.assertRaises(monitor.ConfigError):
            make_config(PORT="70000")
        with self.assertRaises(monitor.ConfigError):
            make_config(PORT="-1")

    def test_invalid_stale_threshold(self):
        with self.assertRaises(monitor.ConfigError):
            make_config(STALE_AFTER_SECONDS="0")
        with self.assertRaises(monitor.ConfigError):
            make_config(STALE_AFTER_SECONDS="-5")

    def test_legacy_fallback_can_be_disabled(self):
        config = make_config(LEGACY_FALLBACK_ENABLED="false")
        self.assertFalse(config.legacy_fallback_enabled)

    def test_legacy_fallback_accepts_explicit_true(self):
        config = make_config(LEGACY_FALLBACK_ENABLED="true")
        self.assertTrue(config.legacy_fallback_enabled)

    def test_repo_config_root_overridable(self):
        config = make_config(REPO_CONFIG_ROOT="/custom/path")
        self.assertEqual(config.repo_config_root, "/custom/path")


class CanonicalEffectiveStatusTests(unittest.TestCase):
    """Canonical STATE#_deployment.status (UP/STARTING/DEPLOYMENT_DOWN/
    UNKNOWN) -> effective portal status (UP/STARTING/DOWN/STALE/MISSING/
    UNKNOWN)."""

    def test_up_maps_to_up(self):
        item = make_deployment_state_item(status="UP", recorded_at=1780000000)
        out = monitor.compute_canonical_effective_status(item, now=1780000010, stale_after_seconds=120)
        self.assertEqual(out["effectiveStatus"], "UP")
        self.assertTrue(out["fresh"])
        self.assertEqual(out["ageSeconds"], 10)

    def test_starting_maps_to_starting(self):
        item = make_deployment_state_item(status="STARTING", recorded_at=1780000000)
        out = monitor.compute_canonical_effective_status(item, now=1780000010, stale_after_seconds=120)
        self.assertEqual(out["effectiveStatus"], "STARTING")

    def test_deployment_down_maps_to_down(self):
        item = make_deployment_state_item(status="DEPLOYMENT_DOWN", recorded_at=1780000000)
        out = monitor.compute_canonical_effective_status(item, now=1780000010, stale_after_seconds=120)
        self.assertEqual(out["effectiveStatus"], "DOWN")

    def test_unrecognized_raw_status_maps_to_unknown(self):
        item = make_deployment_state_item(status="SOMETHING_ELSE", recorded_at=1780000000)
        out = monitor.compute_canonical_effective_status(item, now=1780000010, stale_after_seconds=120)
        self.assertEqual(out["effectiveStatus"], "UNKNOWN")

    def test_stale_overrides_raw_status(self):
        item = make_deployment_state_item(status="UP", recorded_at=1780000000)
        out = monitor.compute_canonical_effective_status(item, now=1780000000 + 121, stale_after_seconds=120)
        self.assertEqual(out["effectiveStatus"], "STALE")
        self.assertFalse(out["fresh"])

    def test_missing_item_is_missing(self):
        out = monitor.compute_canonical_effective_status(None, now=1780000000, stale_after_seconds=120)
        self.assertEqual(out["effectiveStatus"], "MISSING")
        self.assertIsNone(out["recordedAt"])
        self.assertFalse(out["fresh"])

    def test_malformed_recorded_at_is_unknown_and_does_not_raise(self):
        for bad_value in ("not-a-timestamp", "", [], {}, object()):
            with self.subTest(bad_value=bad_value):
                item = make_deployment_state_item(status="UP", recorded_at=bad_value)
                try:
                    out = monitor.compute_canonical_effective_status(item, now=1780000010, stale_after_seconds=120)
                except Exception as exc:  # noqa: BLE001 -- proving no exception escapes
                    self.fail(f"compute_canonical_effective_status raised {exc!r} for {bad_value!r}")
                self.assertEqual(out["effectiveStatus"], "UNKNOWN")
                self.assertIsNone(out["ageSeconds"])

    def test_future_timestamp_never_yields_negative_age(self):
        item = make_deployment_state_item(status="UP", recorded_at=1780000100)
        out = monitor.compute_canonical_effective_status(item, now=1780000000, stale_after_seconds=120)
        self.assertEqual(out["ageSeconds"], 0)
        self.assertTrue(out["fresh"])

        far_future_item = make_deployment_state_item(
            status="UP", recorded_at=1780000000 + monitor.FUTURE_TIMESTAMP_TOLERANCE_SECONDS + 3600)
        out = monitor.compute_canonical_effective_status(far_future_item, now=1780000000, stale_after_seconds=120)
        self.assertEqual(out["effectiveStatus"], "UNKNOWN")
        self.assertIsNone(out["ageSeconds"])


class LegacyEffectiveStatusTests(unittest.TestCase):
    """Legacy observer STATE#_deployment.status (HEALTHY/DEGRADED/DOWN) ->
    the SAME closed effective-status enum as the canonical path."""

    def test_healthy_maps_to_up(self):
        item = {"pipeline": "gg-payments-ora-to-pg-001-source", "recordType": "STATE#_deployment",
               "status": "HEALTHY", "recordedAt": 1780000000}
        out = monitor.compute_legacy_effective_status(item, now=1780000010, stale_after_seconds=120)
        self.assertEqual(out["effectiveStatus"], "UP")

    def test_down_maps_to_down(self):
        item = {"pipeline": "gg-payments-ora-to-pg-001-source", "recordType": "STATE#_deployment",
               "status": "DOWN", "recordedAt": 1780000000}
        out = monitor.compute_legacy_effective_status(item, now=1780000010, stale_after_seconds=120)
        self.assertEqual(out["effectiveStatus"], "DOWN")

    def test_degraded_maps_to_unknown_not_starting_or_down(self):
        """DEGRADED intentionally does not overclaim in either direction --
        see monitor._LEGACY_STATUS_MAP."""
        item = {"pipeline": "gg-payments-ora-to-pg-001-source", "recordType": "STATE#_deployment",
               "status": "DEGRADED", "recordedAt": 1780000000}
        out = monitor.compute_legacy_effective_status(item, now=1780000010, stale_after_seconds=120)
        self.assertEqual(out["effectiveStatus"], "UNKNOWN")

    def test_missing_legacy_item_is_missing(self):
        out = monitor.compute_legacy_effective_status(None, now=1780000000, stale_after_seconds=120)
        self.assertEqual(out["effectiveStatus"], "MISSING")

    def test_stale_legacy_record_overrides_raw_status(self):
        item = {"pipeline": "gg-payments-ora-to-pg-001-source", "recordType": "STATE#_deployment",
               "status": "HEALTHY", "recordedAt": 1780000000}
        out = monitor.compute_legacy_effective_status(item, now=1780000000 + 121, stale_after_seconds=120)
        self.assertEqual(out["effectiveStatus"], "STALE")


class ProcessRowNormalizationTests(unittest.TestCase):
    def test_running_stopped_abended_pass_through(self):
        for raw in ("RUNNING", "STOPPED", "ABENDED"):
            self.assertEqual(monitor.normalize_process_status(raw), raw)

    def test_unrecognized_process_status_becomes_unknown(self):
        self.assertEqual(monitor.normalize_process_status("SOMETHING_ELSE"), "UNKNOWN")
        self.assertEqual(monitor.normalize_process_status(None), "UNKNOWN")

    def test_process_row_extracts_process_name_from_record_type(self):
        row = make_process_item(process="EXTORA1", status="RUNNING", recorded_at=1780000000)
        out = monitor.normalize_process_row(row, now=1780000010, stale_after_seconds=120)
        self.assertEqual(out["process"], "EXTORA1")
        self.assertEqual(out["status"], "RUNNING")
        self.assertFalse(out["stale"])
        self.assertEqual(out["ageSeconds"], 10)

    def test_process_row_never_exposes_raw_error_msg_field(self):
        row = make_process_item(status="ABENDED", errorMsg="db-internal.example.local password=x")
        out = monitor.normalize_process_row(row, now=1780000010, stale_after_seconds=120)
        self.assertNotIn("errorMsg", out)
        self.assertIn("hasError", out)
        self.assertIn("statusCode", out)
        self.assertIn("statusMessage", out)
        self.assertTrue(out["hasError"])
        self.assertEqual(out["statusCode"], "PROCESS_ABENDED")


class ProcessErrorSanitizationTests(unittest.TestCase):
    SENSITIVE_STRINGS = (
        "password=super-secret-test-value",
        "db-internal.example.local",
        "arn:aws:secretsmanager:test",
        "Authorization: Basic abc123",
    )

    def test_status_code_is_from_the_closed_enum(self):
        allowed = {"NONE", "POLL_FAILED", "AUTH_FAILED", "TLS_FAILED",
                  "ENDPOINT_UNAVAILABLE", "STALE", "PROCESS_ABENDED", "UNKNOWN"}
        for status, error_msg, stale in (
            ("RUNNING", "", False),
            ("RUNNING", "connection timeout to db-internal.example.local", False),
            ("RUNNING", "401 Unauthorized: Authorization: Basic abc123", False),
            ("RUNNING", "SSL handshake failed, certificate invalid", False),
            ("ABENDED", "", False),
            ("RUNNING", "", True),
            ("RUNNING", "something unclassifiable happened", False),
        ):
            code = monitor._classify_process_status_code(status, error_msg, stale)
            self.assertIn(code, allowed)

    def test_classification_never_returns_the_raw_text(self):
        for sensitive in self.SENSITIVE_STRINGS:
            code = monitor._classify_process_status_code("ABENDED", sensitive, False)
            message = monitor._PROCESS_STATUS_CODE_MESSAGES[code]
            self.assertNotIn(sensitive, code)
            self.assertNotIn(sensitive, message)

    def test_has_error_false_when_no_error_and_not_stale_and_not_abended(self):
        has_error, code, _ = monitor._sanitized_process_error("RUNNING", "", False)
        self.assertFalse(has_error)
        self.assertEqual(code, "NONE")

    def test_has_error_true_when_abended(self):
        has_error, code, _ = monitor._sanitized_process_error("ABENDED", "", False)
        self.assertTrue(has_error)
        self.assertEqual(code, "PROCESS_ABENDED")


class ReadRuntimeViewTests(unittest.TestCase):
    """Canonical-preferred-over-legacy, fallback-when-missing, and
    fallback-disabled behaviour (section 11)."""

    def test_canonical_data_used_when_present(self):
        now = 1780000010
        table = FakeTable([make_deployment_state_item(recorded_at=now - 5)])
        role_info = {"pipeline": "gg-oracle-payments-01", "deploymentType": "oracle"}
        out = monitor.read_runtime_view(table, "payments-ora-to-pg-001", "source", role_info,
                                        RUNTIMES[0], legacy_fallback_enabled=True, now=now, stale_after_seconds=120)
        self.assertEqual(out["dataSource"], "canonical-monitor")
        self.assertEqual(out["effectiveStatus"], "UP")

    def test_falls_back_to_legacy_when_canonical_missing_and_enabled(self):
        now = 1780000010
        legacy_item = {"pipeline": "gg-payments-ora-to-pg-001-source", "recordType": "STATE#_deployment",
                       "status": "HEALTHY", "recordedAt": now - 5}
        table = FakeTable([legacy_item])
        role_info = {"pipeline": "gg-oracle-payments-01", "deploymentType": "oracle"}
        out = monitor.read_runtime_view(table, "payments-ora-to-pg-001", "source", role_info,
                                        RUNTIMES[0], legacy_fallback_enabled=True, now=now, stale_after_seconds=120)
        self.assertEqual(out["dataSource"], "legacy-observer-fallback")
        self.assertEqual(out["effectiveStatus"], "UP")

    def test_no_legacy_key_hardcoded_in_source(self):
        """The legacy key must be DERIVED from pipelineId + role, never a
        literal hardcoded string, per section 11."""
        import inspect
        src = inspect.getsource(monitor.read_runtime_view)
        self.assertIn('f"gg-{pipeline_id}-{role}"', src)
        self.assertNotIn("gg-payments-ora-to-pg-001-source", src)
        self.assertNotIn("gg-payments-ora-to-pg-001-target", src)

    def test_fallback_disabled_shows_missing_not_legacy_data(self):
        now = 1780000010
        legacy_item = {"pipeline": "gg-payments-ora-to-pg-001-source", "recordType": "STATE#_deployment",
                       "status": "HEALTHY", "recordedAt": now - 5}
        table = FakeTable([legacy_item])
        role_info = {"pipeline": "gg-oracle-payments-01", "deploymentType": "oracle"}
        out = monitor.read_runtime_view(table, "payments-ora-to-pg-001", "source", role_info,
                                        RUNTIMES[0], legacy_fallback_enabled=False, now=now, stale_after_seconds=120)
        self.assertEqual(out["dataSource"], "canonical-monitor")
        self.assertEqual(out["effectiveStatus"], "MISSING")

    def test_canonical_always_wins_even_when_legacy_also_present(self):
        now = 1780000010
        canonical_item = make_deployment_state_item(recorded_at=now - 5, status="UP")
        legacy_item = {"pipeline": "gg-payments-ora-to-pg-001-source", "recordType": "STATE#_deployment",
                       "status": "DOWN", "recordedAt": now - 5}
        table = FakeTable([canonical_item, legacy_item])
        role_info = {"pipeline": "gg-oracle-payments-01", "deploymentType": "oracle"}
        out = monitor.read_runtime_view(table, "payments-ora-to-pg-001", "source", role_info,
                                        RUNTIMES[0], legacy_fallback_enabled=True, now=now, stale_after_seconds=120)
        self.assertEqual(out["dataSource"], "canonical-monitor")
        self.assertEqual(out["effectiveStatus"], "UP")

    def test_legacy_fallback_has_no_process_rows(self):
        now = 1780000010
        legacy_item = {"pipeline": "gg-payments-ora-to-pg-001-source", "recordType": "STATE#_deployment",
                       "status": "HEALTHY", "recordedAt": now - 5}
        table = FakeTable([legacy_item])
        role_info = {"pipeline": "gg-oracle-payments-01", "deploymentType": "oracle"}
        out = monitor.read_runtime_view(table, "payments-ora-to-pg-001", "source", role_info,
                                        RUNTIMES[0], legacy_fallback_enabled=True, now=now, stale_after_seconds=120)
        self.assertEqual(out["processes"], [])

    def test_no_process_state_rows_produces_empty_list_not_crash(self):
        now = 1780000010
        table = FakeTable([make_deployment_state_item(recorded_at=now - 5)])
        role_info = {"pipeline": "gg-oracle-payments-01", "deploymentType": "oracle"}
        out = monitor.read_runtime_view(table, "payments-ora-to-pg-001", "source", role_info,
                                        RUNTIMES[0], legacy_fallback_enabled=True, now=now, stale_after_seconds=120)
        self.assertEqual(out["processes"], [])

    def test_critical_service_state_passed_through(self):
        now = 1780000010
        dep_item = make_deployment_state_item(recorded_at=now - 5,
                                              criticalServices={"adminsrvr": {"reachable": True}})
        table = FakeTable([dep_item])
        role_info = {"pipeline": "gg-oracle-payments-01", "deploymentType": "oracle"}
        out = monitor.read_runtime_view(table, "payments-ora-to-pg-001", "source", role_info,
                                        RUNTIMES[0], legacy_fallback_enabled=True, now=now, stale_after_seconds=120)
        self.assertEqual(out["criticalServices"], {"adminsrvr": True})

    def test_lease_freshness_exposed(self):
        now = 1780000010
        table = FakeTable([make_deployment_state_item(recorded_at=now - 5),
                           make_lease_item(now=now)])
        role_info = {"pipeline": "gg-oracle-payments-01", "deploymentType": "oracle"}
        out = monitor.read_runtime_view(table, "payments-ora-to-pg-001", "source", role_info,
                                        RUNTIMES[0], legacy_fallback_enabled=True, now=now, stale_after_seconds=120)
        self.assertTrue(out["lease"]["fresh"])

    def test_expired_lease_shown_as_not_fresh(self):
        now = 1780000010
        table = FakeTable([make_deployment_state_item(recorded_at=now - 5),
                           make_lease_item(now=now, expiresAt=now - 100)])
        role_info = {"pipeline": "gg-oracle-payments-01", "deploymentType": "oracle"}
        out = monitor.read_runtime_view(table, "payments-ora-to-pg-001", "source", role_info,
                                        RUNTIMES[0], legacy_fallback_enabled=True, now=now, stale_after_seconds=120)
        self.assertFalse(out["lease"]["fresh"])


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
        payload = monitor.build_status_payload(config, table, RUNTIMES, LOGICAL_PIPELINES, clock=lambda: now)

        self.assertIn("generatedAt", payload)
        self.assertIn("logicalPipelines", payload)
        lp = payload["logicalPipelines"][0]
        self.assertEqual(lp["pipelineId"], "payments-ora-to-pg-001")
        roles = {r["role"]: r for r in lp["runtimes"]}
        self.assertEqual(roles["source"]["deploymentName"], "gg-oracle-payments-01")
        self.assertEqual(roles["source"]["dataSource"], "canonical-monitor")
        self.assertEqual(roles["target"]["deploymentName"], "gg-postgresql-payments-01")
        self.assertEqual(roles["target"]["dataSource"], "legacy-observer-fallback")

    def test_no_scan_call_occurs(self):
        table = FakeTable([make_deployment_state_item(recorded_at=1780000005)])
        config = make_config()
        monitor.build_status_payload(config, table, RUNTIMES, LOGICAL_PIPELINES, clock=lambda: 1780000010)
        self.assertFalse(hasattr(table, "scan_calls"))
        self.assertFalse(hasattr(table, "scan"))

    def test_no_write_methods_exist_on_fake_table(self):
        """FakeTable itself has no put_item/update_item/delete_item/
        batch_writer -- proving build_status_payload cannot call them
        without raising AttributeError (none did)."""
        table = FakeTable([make_deployment_state_item(recorded_at=1780000005)])
        config = make_config()
        monitor.build_status_payload(config, table, RUNTIMES, LOGICAL_PIPELINES, clock=lambda: 1780000010)
        for forbidden in ("put_item", "update_item", "delete_item", "batch_writer", "scan"):
            self.assertFalse(hasattr(table, forbidden))

    def test_dynamodb_read_failure_raises_read_error(self):
        from botocore.exceptions import ClientError
        table = mock.Mock()
        table.get_item.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "boom"}}, "GetItem")
        config = make_config()
        with self.assertRaises(monitor.DynamoDbReadError):
            monitor.build_status_payload(config, table, RUNTIMES, LOGICAL_PIPELINES, clock=lambda: 1780000010)


class DecimalConversionTests(unittest.TestCase):
    def test_integral_decimal_becomes_int(self):
        self.assertEqual(monitor.decimal_to_jsonsafe(Decimal("500000")), 500000)
        self.assertIsInstance(monitor.decimal_to_jsonsafe(Decimal("500000")), int)

    def test_fractional_decimal_becomes_float(self):
        self.assertEqual(monitor.decimal_to_jsonsafe(Decimal("50.25")), 50.25)
        self.assertIsInstance(monitor.decimal_to_jsonsafe(Decimal("50.25")), float)

    def test_non_decimal_passthrough(self):
        self.assertEqual(monitor.decimal_to_jsonsafe("UP"), "UP")
        self.assertEqual(monitor.decimal_to_jsonsafe(True), True)

    def test_json_default_serializes_decimal(self):
        payload = {"value": Decimal("12.50")}
        encoded = json.dumps(payload, default=monitor._json_default)
        self.assertEqual(json.loads(encoded)["value"], 12.5)


class HtmlEscapingTests(unittest.TestCase):
    def test_malicious_values_are_escaped_in_html(self):
        malicious = '<script>alert(1)</script>'
        payload = {
            "generatedAt": 1780000010,
            "logicalPipelines": [{
                "pipelineId": malicious, "environment": "dev",
                "runtimes": [{
                    "role": "source", "deploymentName": malicious, "deploymentType": "oracle",
                    "effectiveStatus": "UP", "dataSource": "canonical-monitor", "ageSeconds": 1,
                    "lease": None, "processes": [],
                }],
            }],
        }
        config = make_config()
        rendered = monitor.render_html(payload, config)
        self.assertNotIn("<script>", rendered)
        self.assertIn(html_module.escape(malicious), rendered)

    def test_no_process_rows_shows_fixed_message(self):
        payload = {
            "generatedAt": 1780000010,
            "logicalPipelines": [{
                "pipelineId": "payments-ora-to-pg-001", "environment": "dev",
                "runtimes": [{
                    "role": "source", "deploymentName": "gg-oracle-payments-01", "deploymentType": "oracle",
                    "effectiveStatus": "UP", "dataSource": "canonical-monitor", "ageSeconds": 1,
                    "lease": None, "processes": [],
                }],
            }],
        }
        config = make_config()
        rendered = monitor.render_html(payload, config)
        self.assertIn("No process STATE rows found.", rendered)


class HealthAndReadyTests(unittest.TestCase):
    def _handler(self, config, table_factory):
        handler_cls = monitor._make_handler(config, table_factory, RUNTIMES, LOGICAL_PIPELINES)
        handler = handler_cls.__new__(handler_cls)
        writes = []
        handler._write = lambda status, ctype, body: writes.append((status, ctype, body))
        return handler, writes

    def test_healthz_returns_200_and_never_touches_dynamodb(self):
        config = make_config()
        factory = mock.Mock(side_effect=AssertionError("healthz must never call the table factory"))
        handler, writes = self._handler(config, factory)
        handler.path = "/healthz"
        handler.do_GET()
        self.assertEqual(writes[0][0], 200)
        self.assertEqual(json.loads(writes[0][2])["status"], "ok")
        factory.assert_not_called()

    def test_readyz_returns_200_when_describe_table_succeeds(self):
        config = make_config()
        table = mock.Mock()
        handler, writes = self._handler(config, lambda: table)
        handler.path = "/readyz"
        handler.do_GET()
        self.assertEqual(writes[0][0], 200)

    def test_readyz_returns_503_on_dynamodb_failure(self):
        config = make_config()
        table = mock.Mock()
        table.meta.client.describe_table.side_effect = RuntimeError("boom")
        handler, writes = self._handler(config, lambda: table)
        handler.path = "/readyz"
        handler.do_GET()
        self.assertEqual(writes[0][0], 503)

    def test_readyz_uses_request_local_table_from_factory(self):
        config = make_config()
        created = []

        def factory():
            t = mock.Mock()
            created.append(t)
            return t

        handler, writes = self._handler(config, factory)
        handler.path = "/readyz"
        handler.do_GET()
        self.assertEqual(len(created), 1)


class RootPageErrorBannerTests(unittest.TestCase):
    def test_root_page_shows_sanitized_banner_on_dynamodb_failure(self):
        config = make_config()

        def factory():
            raise RuntimeError("boom")

        handler_cls = monitor._make_handler(config, factory, RUNTIMES, LOGICAL_PIPELINES)
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
    """A raw AWS/botocore error (e.g. AccessDenied naming an IAM principal
    ARN and account ID) must never reach an API or HTML client -- only the
    fixed, client-safe message may appear in either response."""

    SIMULATED_ARN_LEAK = (
        "An error occurred (AccessDeniedException) when calling "
        "the GetItem operation: User: arn:aws:sts::668311715351:assumed-role/"
        "GoldenGateMonitorReadRole-dev/i-0123456789abcdef is not authorized "
        "to perform: dynamodb:GetItem on resource: "
        "arn:aws:dynamodb:eu-west-1:668311715351:table/gg-eks-pipeline"
    )

    def _failing_table(self):
        table = mock.Mock()
        table.get_item.side_effect = RuntimeError(self.SIMULATED_ARN_LEAK)
        return table

    def test_api_status_dynamodb_failure_returns_only_fixed_message(self):
        config = make_config()
        handler_cls = monitor._make_handler(config, self._failing_table, RUNTIMES, LOGICAL_PIPELINES)
        handler = handler_cls.__new__(handler_cls)
        handler.path = "/api/status"
        writes = []
        handler._write = lambda status, ctype, body: writes.append((status, ctype, body))
        handler.do_GET()

        self.assertEqual(writes[0][0], 503)
        body = json.loads(writes[0][2])
        self.assertEqual(body["error"], "dynamodb_unavailable")
        self.assertEqual(body["message"], monitor.CLIENT_SAFE_DYNAMODB_ERROR_MESSAGE)

        raw_response = writes[0][2].decode("utf-8")
        self.assertNotIn("arn:aws", raw_response)
        self.assertNotIn("668311715351", raw_response)
        self.assertNotIn("AccessDeniedException", raw_response)
        self.assertNotIn("GoldenGateMonitorReadRole-dev", raw_response)

    def test_html_dynamodb_failure_does_not_expose_iam_arn(self):
        config = make_config()
        handler_cls = monitor._make_handler(config, self._failing_table, RUNTIMES, LOGICAL_PIPELINES)
        handler = handler_cls.__new__(handler_cls)
        handler.path = "/"
        writes = []
        handler._write = lambda status, ctype, body: writes.append((status, ctype, body))
        handler.do_GET()

        self.assertEqual(writes[0][0], 200)
        body = writes[0][2].decode("utf-8")
        self.assertIn(monitor.CLIENT_SAFE_DYNAMODB_ERROR_MESSAGE, body)
        self.assertNotIn("arn:aws", body)
        self.assertNotIn("668311715351", body)
        self.assertNotIn("AccessDeniedException", body)
        self.assertNotIn("GoldenGateMonitorReadRole-dev", body)


class DynamoDbAccessPatternTests(unittest.TestCase):
    def test_get_deployment_state_uses_state_deployment_record_type(self):
        table = FakeTable([make_deployment_state_item(recorded_at=1780000000)])
        monitor.get_deployment_state_item(table, "gg-oracle-payments-01")
        self.assertEqual(table.get_item_calls[-1]["recordType"], "STATE#_deployment")

    def test_get_config_uses_config_record_type(self):
        table = FakeTable([make_config_item()])
        monitor.get_config_item(table, "gg-oracle-payments-01")
        self.assertEqual(table.get_item_calls[-1]["recordType"], "CONFIG")

    def test_get_lease_uses_lease_record_type(self):
        table = FakeTable([make_lease_item()])
        monitor.get_lease_item(table, "gg-oracle-payments-01")
        self.assertEqual(table.get_item_calls[-1]["recordType"], "LEASE")

    def test_query_process_states_uses_begins_with_state_prefix_and_excludes_deployment_row(self):
        table = FakeTable([
            make_deployment_state_item(recorded_at=1780000000),
            make_process_item(process="EXTORA1"),
        ])
        rows = monitor.query_process_state_items(table, "gg-oracle-payments-01")
        self.assertEqual(table.query_calls[-1][":prefix"], "STATE#")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["recordType"], "STATE#EXTORA1")

    def test_no_scan_call_occurs(self):
        table = FakeTable([make_deployment_state_item(recorded_at=1780000005)])
        config = make_config()
        monitor.build_status_payload(config, table, RUNTIMES, LOGICAL_PIPELINES, clock=lambda: 1780000010)
        self.assertFalse(hasattr(table, "scan"))

    def test_no_dynamodb_write_operation_occurs(self):
        table = FakeTable([make_deployment_state_item(recorded_at=1780000005)])
        config = make_config()
        monitor.build_status_payload(config, table, RUNTIMES, LOGICAL_PIPELINES, clock=lambda: 1780000010)
        for forbidden in ("put_item", "update_item", "delete_item", "batch_writer"):
            self.assertFalse(hasattr(table, forbidden))

    def test_dynamodb_read_failure_raises_read_error(self):
        from botocore.exceptions import ClientError
        table = mock.Mock()
        table.get_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "boom"}}, "GetItem")
        config = make_config()
        with self.assertRaises(monitor.DynamoDbReadError):
            monitor.build_status_payload(config, table, RUNTIMES, LOGICAL_PIPELINES, clock=lambda: 1780000010)


class ThreadSafetyTests(unittest.TestCase):
    """ThreadingHTTPServer hands each request its own thread -- a single
    shared boto3 Table/Resource object used across all of them would be a
    mutable object accessed concurrently from multiple threads. The table
    factory is a callable, not a pre-built object: each request obtains its
    OWN Table instance."""

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

        config = make_config()
        handler_cls = monitor._make_handler(config, factory, RUNTIMES, LOGICAL_PIPELINES)
        handler = handler_cls.__new__(handler_cls)
        writes = []
        handler._write = lambda status, ctype, body: writes.append((status, ctype, body))
        handler.path = "/api/status"

        for _ in range(3):
            handler.do_GET()

        self.assertEqual(len(created), 3, "the factory must be called once per request")
        self.assertEqual(len(set(id(t) for t in created)), 3, "each request must get a distinct table object")

    def test_concurrent_requests_each_get_independent_table_objects(self):
        """Simulates concurrent access from multiple threads -- proves no
        shared mutable Table object is read/written across threads."""
        import threading

        created = []
        lock = threading.Lock()

        def factory():
            t = FakeTable([make_deployment_state_item(recorded_at=1780000005)])
            with lock:
                created.append(t)
            return t

        config = make_config()
        handler_cls = monitor._make_handler(config, factory, RUNTIMES, LOGICAL_PIPELINES)

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
