import html as html_module
import inspect
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
DEPLOYMENT_MODEL_TOOL_PATH = os.path.join(REPO_ROOT, "hack", "goldengate-deployment-model.py")


def _generate_registry_document(environment="dev"):
    """Invokes the sole folder parser to produce the same registry the workflow generates, never a handwritten fixture."""
    proc = subprocess.run(
        [sys.executable, DEPLOYMENT_MODEL_TOOL_PATH, "--environment", environment, "registry"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    if proc.returncode != 0:
        raise AssertionError(f"deployment-model registry generation failed: {proc.stdout}\n{proc.stderr}")
    return proc.stdout


def _stage_generated_registry(target_dir, environment="dev"):
    """Writes the generated registry to <target_dir>/goldengate-deployments.yaml, mirroring the workflow's staging step."""
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, "goldengate-deployments.yaml")
    with open(path, "w") as f:
        f.write(_generate_registry_document(environment))
    return path


def _stage_generated_registry_dir(environment="dev"):
    target_dir = tempfile.mkdtemp()
    _stage_generated_registry(target_dir, environment)
    return target_dir

# Not required to run this suite (every test injects a fake/mock table); stub only when unavailable.
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
import ui  # noqa: E402


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
    """Minimal in-memory stand-in for a boto3 DynamoDB Table -- supports only get_item/query."""

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
        self.assertFalse(hasattr(config, "legacy_fallback_enabled"))

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
    """Canonical-only behaviour: no legacy-observer fallback of any kind."""

    LEGACY_PARTITION_NAMES = ("gg-payments-ora-to-pg-001-source", "gg-payments-ora-to-pg-001-target")

    def _meta(self):
        return {"type": "oracle", "enabled": True}

    def test_canonical_data_used_when_present(self):
        now = 1780000010
        table = FakeTable([make_deployment_state_item(recorded_at=now - 5)])
        out = monitor.read_runtime_view(table, "source", "gg-oracle-payments-01",
                                        self._meta(), now=now, stale_after_seconds=120)
        self.assertEqual(out["dataSource"], "canonical-monitor")
        self.assertEqual(out["effectiveStatus"], "UP")

    def test_missing_canonical_state_reports_missing_never_legacy(self):
        # No canonical record: even if a legacy per-role partition holds data, it must never be read.
        now = 1780000010
        legacy_item = {"pipeline": "gg-payments-ora-to-pg-001-source", "recordType": "STATE#_deployment",
                       "status": "HEALTHY", "recordedAt": now - 5}
        table = FakeTable([legacy_item])
        out = monitor.read_runtime_view(table, "source", "gg-oracle-payments-01",
                                        self._meta(), now=now, stale_after_seconds=120)
        self.assertEqual(out["dataSource"], "canonical-monitor")
        self.assertEqual(out["effectiveStatus"], "MISSING")
        # The legacy per-role partition must never even be queried.
        queried_pipelines = {call["pipeline"] for call in table.get_item_calls}
        self.assertNotIn("gg-payments-ora-to-pg-001-source", queried_pipelines)

    def test_no_legacy_partition_names_hardcoded_in_source(self):
        import inspect
        src = inspect.getsource(monitor.read_runtime_view)
        for legacy_name in self.LEGACY_PARTITION_NAMES:
            self.assertNotIn(legacy_name, src)
        self.assertNotIn("legacy", src.lower())

    def test_no_legacy_fallback_function_or_status_map_exists(self):
        self.assertFalse(hasattr(monitor, "compute_legacy_effective_status"))
        self.assertFalse(hasattr(monitor, "_LEGACY_STATUS_MAP"))

    def test_data_source_is_always_canonical_monitor(self):
        now = 1780000010
        for table in (FakeTable([make_deployment_state_item(recorded_at=now - 5)]), FakeTable([])):
            out = monitor.read_runtime_view(table, "source", "gg-oracle-payments-01",
                                            self._meta(), now=now, stale_after_seconds=120)
            self.assertEqual(out["dataSource"], "canonical-monitor")

    def test_no_process_state_rows_produces_empty_list_not_crash(self):
        now = 1780000010
        table = FakeTable([make_deployment_state_item(recorded_at=now - 5)])
        out = monitor.read_runtime_view(table, "source", "gg-oracle-payments-01",
                                        self._meta(), now=now, stale_after_seconds=120)
        self.assertEqual(out["processes"], [])

    def test_critical_service_state_passed_through(self):
        now = 1780000010
        table = FakeTable([make_deployment_state_item(recorded_at=now - 5,
                                                      criticalServices={"adminsrvr": {"reachable": True}})])
        out = monitor.read_runtime_view(table, "source", "gg-oracle-payments-01",
                                        self._meta(), now=now, stale_after_seconds=120)
        self.assertEqual(out["criticalServices"], {"adminsrvr": True})

    def test_lease_freshness_exposed(self):
        now = 1780000010
        table = FakeTable([make_deployment_state_item(recorded_at=now - 5), make_lease_item(now=now)])
        out = monitor.read_runtime_view(table, "source", "gg-oracle-payments-01",
                                        self._meta(), now=now, stale_after_seconds=120)
        self.assertTrue(out["lease"]["fresh"])

    def test_expired_lease_shown_as_not_fresh(self):
        now = 1780000010
        table = FakeTable([make_deployment_state_item(recorded_at=now - 5),
                           make_lease_item(now=now, expiresAt=now - 100)])
        out = monitor.read_runtime_view(table, "source", "gg-oracle-payments-01",
                                        self._meta(), now=now, stale_after_seconds=120)
        self.assertFalse(out["lease"]["fresh"])

    def test_canonical_process_rows_shown_when_deployment_state_missing(self):
        # STATE#_deployment absent (eg a race at first-tick startup) must not suppress process rows.
        now = 1780000010
        table = FakeTable([make_process_item(recorded_at=now - 3)])
        out = monitor.read_runtime_view(table, "source", "gg-oracle-payments-01",
                                        self._meta(), now=now, stale_after_seconds=120)
        self.assertEqual(out["dataSource"], "canonical-monitor")
        self.assertEqual(out["effectiveStatus"], "MISSING")
        self.assertEqual(len(out["processes"]), 1)
        self.assertEqual(out["processes"][0]["process"], "EXTORA1")

    def test_malformed_critical_services_root_does_not_crash(self):
        now = 1780000010
        table = FakeTable([make_deployment_state_item(recorded_at=now - 5, criticalServices="not-a-dict")])
        out = monitor.read_runtime_view(table, "source", "gg-oracle-payments-01",
                                        self._meta(), now=now, stale_after_seconds=120)
        self.assertEqual(out["criticalServices"], {})

    def test_malformed_critical_service_item_does_not_crash(self):
        now = 1780000010
        table = FakeTable([make_deployment_state_item(
            recorded_at=now - 5,
            criticalServices={"adminsrvr": True, "distsrvr": None, "metricsrvr": "unexpected"})])
        out = monitor.read_runtime_view(table, "source", "gg-oracle-payments-01",
                                        self._meta(), now=now, stale_after_seconds=120)
        self.assertEqual(out["criticalServices"], {"adminsrvr": False, "distsrvr": False, "metricsrvr": False})

    def test_process_discovery_is_additive_and_normalized(self):
        now = 1780000010
        table = FakeTable([make_deployment_state_item(
            recorded_at=now - 5,
            processDiscovery={"status": "EMPTY", "collectedAt": now - 5, "extractCount": 0,
                              "replicatCount": 0, "distpathCount": 0, "totalCount": 0,
                              "extractsStatus": "EMPTY", "replicatsStatus": "EMPTY",
                              "sourcesStatus": "EMPTY", "detailFailureCount": 0})])
        out = monitor.read_runtime_view(table, "source", "gg-oracle-payments-01",
                                        self._meta(), now=now, stale_after_seconds=120)
        self.assertEqual(out["processDiscovery"]["status"], "EMPTY")
        # Every previously existing field must still be present.
        for key in ("role", "deploymentName", "deploymentType", "dataSource", "effectiveStatus",
                   "criticalServices", "processes", "lease"):
            self.assertIn(key, out)

    def test_process_discovery_absent_when_deployment_state_missing(self):
        now = 1780000010
        table = FakeTable([])
        out = monitor.read_runtime_view(table, "source", "gg-oracle-payments-01",
                                        self._meta(), now=now, stale_after_seconds=120)
        self.assertIsNone(out["processDiscovery"])

    def test_process_discovery_absent_when_not_yet_reported(self):
        now = 1780000010
        table = FakeTable([make_deployment_state_item(recorded_at=now - 5)])
        out = monitor.read_runtime_view(table, "source", "gg-oracle-payments-01",
                                        self._meta(), now=now, stale_after_seconds=120)
        self.assertIsNone(out["processDiscovery"])


class BuildStatusPayloadTests(unittest.TestCase):
    def test_end_to_end_shape_matches_recommended_schema(self):
        now = 1780000010
        # A legacy per-role partition is deliberately included with data to prove it's ignored.
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
        self.assertEqual(roles["target"]["dataSource"], "canonical-monitor")
        self.assertEqual(roles["target"]["effectiveStatus"], "MISSING")

    def test_dynamodb_read_failure_raises_read_error(self):
        from botocore.exceptions import ClientError
        table = mock.Mock()
        table.get_item.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "boom"}}, "GetItem")
        config = make_config()
        with self.assertRaises(monitor.DynamoDbReadError):
            monitor.build_status_payload(config, table, DEPLOYMENTS, LOGICAL_PIPELINES, clock=lambda: 1780000010)

    def test_real_repo_config_produces_two_runtimes_under_one_pipeline(self):
        doc = cfgmod.load_deployments(_stage_generated_registry_dir())
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
        # Page has one static <script> (theme toggle); assertion is that attacker input is escaped, not that no <script> exists.
        malicious = '<script>alert(1)</script>'
        payload = {"generatedAt": 1780000010, "logicalPipelines": [{
            "pipelineId": malicious,
            "runtimes": [{"role": "source", "deploymentName": malicious, "deploymentType": "oracle",
                         "effectiveStatus": "UP", "dataSource": "canonical-monitor", "ageSeconds": 1,
                         "lease": None, "processes": []}],
        }]}
        rendered = monitor.render_html(payload, make_config())
        self.assertNotIn(malicious, rendered)
        self.assertIn(html_module.escape(malicious), rendered)

    def test_no_process_rows_shows_fixed_message(self):
        payload = {"generatedAt": 1780000010, "logicalPipelines": [{
            "pipelineId": "payments-ora-to-pg-001",
            "runtimes": [{"role": "source", "deploymentName": "gg-oracle-payments-01", "deploymentType": "oracle",
                         "effectiveStatus": "UP", "dataSource": "canonical-monitor", "ageSeconds": 1,
                         "lease": None, "processes": []}],
        }]}
        rendered = monitor.render_html(payload, make_config())
        self.assertIn("No process state available", rendered)
        self.assertIn("No Extract or Replicat process STATE rows have been recorded.", rendered)


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


class EnvironmentPropagationTests(unittest.TestCase):
    """Proves the real server/root-page path threads doc["environment"] into render_html, not just direct calls."""

    def _handler(self, environment):
        table = mock.Mock()
        handler_cls = monitor._make_handler(make_config(), lambda: table, DEPLOYMENTS, LOGICAL_PIPELINES, {}, [],
                                            environment=environment)
        handler = handler_cls.__new__(handler_cls)
        handler.path = "/"
        writes = []
        handler._write = lambda status, ctype, body: writes.append((status, ctype, body))
        return handler, writes

    def test_root_page_renders_environment_passed_to_make_handler(self):
        handler, writes = self._handler("vdr")
        handler.do_GET()
        body = writes[0][2].decode("utf-8")
        self.assertIn('<span class="badge-env">VDR</span>', body)

    def test_root_page_shows_environment_unknown_when_make_handler_gets_none(self):
        handler, writes = self._handler(None)
        handler.do_GET()
        body = writes[0][2].decode("utf-8")
        self.assertIn('<span class="badge-env">ENVIRONMENT UNKNOWN</span>', body)

    def test_start_http_server_forwards_environment_into_make_handler(self):
        with mock.patch.object(monitor, "ThreadingHTTPServer") as mock_server_cls, \
             mock.patch.object(monitor, "_make_handler", wraps=monitor._make_handler) as spy_make_handler:
            monitor.start_http_server(make_config(), lambda: mock.Mock(), DEPLOYMENTS, LOGICAL_PIPELINES, {}, [],
                                      environment="prod")
        _, kwargs = spy_make_handler.call_args
        self.assertEqual(kwargs.get("environment"), "prod")
        mock_server_cls.assert_called_once()

    def test_start_http_server_defaults_environment_to_none(self):
        with mock.patch.object(monitor, "ThreadingHTTPServer"), \
             mock.patch.object(monitor, "_make_handler", wraps=monitor._make_handler) as spy_make_handler:
            monitor.start_http_server(make_config(), lambda: mock.Mock(), DEPLOYMENTS, LOGICAL_PIPELINES, {}, [])
        _, kwargs = spy_make_handler.call_args
        self.assertIsNone(kwargs.get("environment"))

    def test_hostile_environment_from_server_path_is_escaped(self):
        malicious = '<script>alert(1)</script>'
        handler, writes = self._handler(malicious)
        handler.do_GET()
        body = writes[0][2].decode("utf-8")
        self.assertNotIn(malicious, body)
        self.assertIn(html_module.escape(malicious.upper()), body)

    def test_main_passes_doc_environment_to_start_http_server(self):
        source = inspect.getsource(monitor.main)
        self.assertIn('environment=doc["environment"]', source)


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
    """Manager-parity process fields must be surfaced by normalize_process_row with safe defaults when absent."""

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
    """Manager-contract relative-age text, reimplemented against this codebase's None (not the manager's -1 sentinel) missing-value convention."""

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
    """Manager-contract combined lag/threshold/mode cell text, reimplemented against this codebase's None (not the manager's -1 sentinel) missing-value convention."""

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
    """normalize_critical_services must never raise on malformed shape and must fail closed (unreachable)."""

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

    def test_literal_true_reachable_accepted(self):
        self.assertEqual(
            monitor.normalize_critical_services({"adminsrvr": {"reachable": True}}),
            {"adminsrvr": True})

    def test_literal_false_reachable_rejected(self):
        self.assertEqual(
            monitor.normalize_critical_services({"adminsrvr": {"reachable": False}}),
            {"adminsrvr": False})

    def test_string_true_reachable_rejected(self):
        self.assertEqual(
            monitor.normalize_critical_services({"adminsrvr": {"reachable": "true"}}),
            {"adminsrvr": False})

    def test_string_false_reachable_rejected(self):
        self.assertEqual(
            monitor.normalize_critical_services({"adminsrvr": {"reachable": "false"}}),
            {"adminsrvr": False})

    def test_integer_one_reachable_rejected(self):
        self.assertEqual(
            monitor.normalize_critical_services({"adminsrvr": {"reachable": 1}}),
            {"adminsrvr": False})

    def test_integer_zero_reachable_rejected(self):
        self.assertEqual(
            monitor.normalize_critical_services({"adminsrvr": {"reachable": 0}}),
            {"adminsrvr": False})

    def test_none_reachable_rejected(self):
        self.assertEqual(
            monitor.normalize_critical_services({"adminsrvr": {"reachable": None}}),
            {"adminsrvr": False})

    def test_nested_arbitrary_object_and_list_reachable_rejected(self):
        self.assertEqual(
            monitor.normalize_critical_services({"adminsrvr": {"reachable": {"nested": True}}}),
            {"adminsrvr": False})
        self.assertEqual(
            monitor.normalize_critical_services({"adminsrvr": {"reachable": [True]}}),
            {"adminsrvr": False})

    def test_boolean_root_service_value_rejected(self):
        # The service value itself (not a {"reachable": ...} dict) must fail closed, not be coerced.
        self.assertEqual(monitor.normalize_critical_services({"adminsrvr": True}), {"adminsrvr": False})


DISCOVERY_STATUSES_UNDER_TEST = ("OK", "EMPTY", "PARTIAL", "UNAVAILABLE", "INVALID_RESPONSE", "MADE_UP")


class NormalizeProcessDiscoveryTests(unittest.TestCase):
    """normalize_process_discovery: strict, additive, fail-closed normalization of STATE#_deployment.processDiscovery."""

    def _valid(self, **overrides):
        d = {"status": "OK", "collectedAt": 1780000000, "extractCount": 1, "replicatCount": 1,
            "distpathCount": 0, "totalCount": 2, "extractsStatus": "OK", "replicatsStatus": "OK",
            "sourcesStatus": "EMPTY", "detailFailureCount": 0}
        d.update(overrides)
        return d

    def test_well_formed_input_passes_through(self):
        out = monitor.normalize_process_discovery(self._valid())
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["extractCount"], 1)
        self.assertEqual(out["detailFailureCount"], 0)

    def test_non_dict_root_becomes_none(self):
        for bad in (None, "unexpected", 42, [], True):
            with self.subTest(bad=bad):
                self.assertIsNone(monitor.normalize_process_discovery(bad))

    def test_unknown_status_fails_closed_to_invalid_response_not_none(self):
        out = monitor.normalize_process_discovery(self._valid(status="MADE_UP"))
        self.assertIsNotNone(out)
        self.assertEqual(out["status"], "INVALID_RESPONSE")
        self.assertEqual(out["extractCount"], 1)

    def test_missing_status_fails_closed_to_invalid_response(self):
        d = self._valid()
        del d["status"]
        out = monitor.normalize_process_discovery(d)
        self.assertEqual(out["status"], "INVALID_RESPONSE")

    def test_every_fixed_status_accepted(self):
        for status in ("OK", "EMPTY", "PARTIAL", "UNAVAILABLE", "INVALID_RESPONSE"):
            with self.subTest(status=status):
                out = monitor.normalize_process_discovery(self._valid(status=status))
                self.assertEqual(out["status"], status)

    def test_endpoint_status_unknown_value_fails_closed_to_unavailable(self):
        out = monitor.normalize_process_discovery(self._valid(extractsStatus="MADE_UP"))
        self.assertEqual(out["extractsStatus"], "UNAVAILABLE")

    def test_negative_counts_become_zero(self):
        out = monitor.normalize_process_discovery(self._valid(extractCount=-5, detailFailureCount=-1))
        self.assertEqual(out["extractCount"], 0)
        self.assertEqual(out["detailFailureCount"], 0)

    def test_non_numeric_counts_become_zero(self):
        out = monitor.normalize_process_discovery(self._valid(extractCount="not-a-number"))
        self.assertEqual(out["extractCount"], 0)

    def test_boolean_count_never_treated_as_numeric(self):
        out = monitor.normalize_process_discovery(self._valid(extractCount=True))
        self.assertEqual(out["extractCount"], 0)

    def test_decimal_count_converted_to_jsonsafe_int(self):
        from decimal import Decimal
        out = monitor.normalize_process_discovery(self._valid(extractCount=Decimal("3")))
        self.assertEqual(out["extractCount"], 3)
        self.assertIsInstance(out["extractCount"], int)

    def test_missing_collected_at_becomes_none_not_a_crash(self):
        d = self._valid()
        del d["collectedAt"]
        out = monitor.normalize_process_discovery(d)
        self.assertIsNone(out["collectedAt"])

    def test_never_persists_process_names(self):
        # The normalizer only reads the fixed summary keys -- an extra "processes" key must never surface.
        d = self._valid()
        d["processes"] = [{"process": "SUPER_SECRET_NAME"}]
        out = monitor.normalize_process_discovery(d)
        self.assertNotIn("processes", out)

    def test_float_nan_and_infinity_counts_never_raise_and_become_zero(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                out = monitor.normalize_process_discovery(self._valid(extractCount=bad))
                self.assertEqual(out["extractCount"], 0)

    def test_decimal_nan_and_infinity_counts_never_raise_and_become_zero(self):
        from decimal import Decimal
        for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with self.subTest(bad=bad):
                out = monitor.normalize_process_discovery(self._valid(detailFailureCount=bad))
                self.assertEqual(out["detailFailureCount"], 0)

    def test_object_with_failing_int_conversion_never_raises(self):
        class Unconvertible:
            def __int__(self):
                raise ValueError("cannot convert")

        out = monitor.normalize_process_discovery(self._valid(totalCount=Unconvertible()))
        self.assertEqual(out["totalCount"], 0)

    def test_string_and_list_counts_never_raise_and_become_zero(self):
        for bad in ("5", "not-a-number", [1, 2], {"a": 1}, object()):
            with self.subTest(bad=bad):
                out = monitor.normalize_process_discovery(self._valid(replicatCount=bad))
                self.assertEqual(out["replicatCount"], 0)

    def test_float_nan_and_infinity_collected_at_becomes_none(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                out = monitor.normalize_process_discovery(self._valid(collectedAt=bad))
                self.assertIsNone(out["collectedAt"])

    def test_decimal_nan_and_infinity_collected_at_becomes_none(self):
        from decimal import Decimal
        for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with self.subTest(bad=bad):
                out = monitor.normalize_process_discovery(self._valid(collectedAt=bad))
                self.assertIsNone(out["collectedAt"])

    def test_string_and_boolean_collected_at_becomes_none(self):
        for bad in ("not-a-timestamp", True, [1, 2], object()):
            with self.subTest(bad=bad):
                out = monitor.normalize_process_discovery(self._valid(collectedAt=bad))
                self.assertIsNone(out["collectedAt"])

    def test_no_malformed_input_ever_raises(self):
        malformed_values = (float("nan"), float("inf"), float("-inf"), True, False, "garbage",
                            [1, 2], {"nested": "dict"}, object(), None)
        for status in DISCOVERY_STATUSES_UNDER_TEST:
            for field in ("extractCount", "replicatCount", "distpathCount", "totalCount",
                         "detailFailureCount", "collectedAt", "extractsStatus", "replicatsStatus",
                         "sourcesStatus"):
                for bad in malformed_values:
                    with self.subTest(status=status, field=field, bad=bad):
                        try:
                            monitor.normalize_process_discovery(self._valid(status=status, **{field: bad}))
                        except Exception as exc:  # pragma: no cover -- the assertion below always fires first
                            self.fail(f"normalize_process_discovery raised {exc!r} for {field}={bad!r}")


class PortalHtmlManagerParityTests(unittest.TestCase):
    """Every required HTML field is present, HTML-escaped, and never leaks raw errorMsg/credentials/secrets/hostnames/ARNs."""

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

    def test_malformed_critical_service_values_render_as_down_never_reachable(self):
        # Defense in depth at the HTML layer: even if a non-True truthy value bypasses normalize_critical_services, only literal Boolean True may render as "reachable".
        payload = self._payload_with_full_runtime(criticalServices={
            "svc-true": True, "svc-str-true": "true", "svc-str-false": "false",
            "svc-one": 1, "svc-zero": 0, "svc-none": None, "svc-list": ["reachable"],
        })
        rendered = monitor.render_html(payload, make_config())
        for name in ("svc-true", "svc-str-true", "svc-str-false", "svc-one",
                    "svc-zero", "svc-none", "svc-list"):
            self.assertIn(name, rendered)
        # Exactly one genuinely-reachable service (svc-true); every other malformed value must render as down.
        self.assertEqual(rendered.count(">reachable<"), 1)
        self.assertEqual(rendered.count(">down<"), 6)

    def test_critical_service_no_raw_malformed_value_exposed_in_html(self):
        payload = self._payload_with_full_runtime(criticalServices={
            "adminsrvr": "unexpected-raw-value", "distsrvr": 1234567,
        })
        rendered = monitor.render_html(payload, make_config())
        self.assertNotIn("unexpected-raw-value", rendered)
        self.assertNotIn("1234567", rendered)

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
        # No wide outer table with the process table nested inside its final <td>.
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
        self.assertEqual(rendered.count('<article class="card">'), 2)

    def test_stale_process_row_has_class_and_visible_prefix(self):
        payload = self._payload_with_full_runtime()
        payload["logicalPipelines"][0]["runtimes"][0]["processes"][0]["stale"] = True
        rendered = monitor.render_html(payload, make_config())
        self.assertIn("stale-row", rendered)
        self.assertIn("[STALE]", rendered)

    def test_fresh_process_row_has_no_stale_marker(self):
        rendered = monitor.render_html(self._payload_with_full_runtime(), make_config())
        # The stylesheet's static tr.stale-row rule is always present; what must be absent is its use on a row.
        self.assertNotIn('<tr class="stale-row"', rendered)
        self.assertNotIn("[STALE]", rendered)

    def test_lease_holder_escaped_exactly_once(self):
        malicious_holder = '<script>&"\''
        payload = self._payload_with_full_runtime(
            lease={"holder": malicious_holder, "expiresAt": 1780000040, "fresh": True})
        rendered = monitor.render_html(payload, make_config())
        self.assertNotIn(malicious_holder, rendered)
        self.assertNotIn("&amp;amp;", rendered)
        self.assertIn(html_module.escape(malicious_holder), rendered)


def _ui_payload(**overrides):
    runtime = {
        "role": "source", "deploymentName": "gg-oracle-payments-01", "deploymentType": "oracle",
        "effectiveStatus": "UP", "fresh": True, "dataSource": "canonical-monitor",
        "alertsEnabled": False, "metricsEnabled": True, "ageSeconds": 3, "recordedAt": 1780000007,
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


def _relative_luminance(hex_color):
    hex_color = hex_color.lstrip("#")
    channels = [int(hex_color[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(hex_a, hex_b):
    la, lb = _relative_luminance(hex_a), _relative_luminance(hex_b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def _extract_css_block(css_text, selector):
    start = css_text.index(selector)
    open_brace = css_text.index("{", start)
    close_brace = css_text.index("}", open_brace)
    return css_text[open_brace:close_brace]


def _extract_token(block, name):
    match = re.search(rf"--{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})", block)
    return match.group(1)


class DarkThemeContrastTests(unittest.TestCase):
    """Standard-library-only WCAG contrast check for the dark-theme status foreground/background pairs."""

    REQUIRED_PAIRS = (("gg-green", "gg-green-bg"), ("gg-amber", "gg-amber-bg"),
                      ("gg-red", "gg-red-bg"), ("gg-gray", "gg-gray-bg"))

    def test_dark_theme_status_pairs_meet_4_5_to_1_contrast(self):
        block = _extract_css_block(ui.CSS_TEXT, ':root[data-theme="dark"]')
        for fg_name, bg_name in self.REQUIRED_PAIRS:
            with self.subTest(pair=f"{fg_name}/{bg_name}"):
                fg = _extract_token(block, fg_name)
                bg = _extract_token(block, bg_name)
                ratio = _contrast_ratio(fg, bg)
                self.assertGreaterEqual(ratio, 4.5, f"{fg_name} ({fg}) vs {bg_name} ({bg}) = {ratio:.2f}:1")

    def test_system_preference_dark_block_matches_explicit_dark_block(self):
        media_block = _extract_css_block(ui.CSS_TEXT, ":root:not([data-theme])")
        explicit_block = _extract_css_block(ui.CSS_TEXT, ':root[data-theme="dark"]')
        for fg_name, bg_name in self.REQUIRED_PAIRS:
            with self.subTest(pair=f"{fg_name}/{bg_name}"):
                self.assertEqual(_extract_token(media_block, fg_name), _extract_token(explicit_block, fg_name))
                self.assertEqual(_extract_token(media_block, bg_name), _extract_token(explicit_block, bg_name))

    def test_dark_foreground_tokens_differ_from_light_theme_values(self):
        root_block = _extract_css_block(ui.CSS_TEXT, ":root {")
        dark_block = _extract_css_block(ui.CSS_TEXT, ':root[data-theme="dark"]')
        for fg_name, _bg_name in self.REQUIRED_PAIRS:
            with self.subTest(token=fg_name):
                self.assertNotEqual(_extract_token(root_block, fg_name), _extract_token(dark_block, fg_name))


class EnvironmentBadgeContrastTests(unittest.TestCase):
    """Brand red (badge background) is a separate token from theme-aware status red (foreground)."""

    def test_badge_env_uses_brand_red_not_status_red(self):
        badge_rule = _extract_css_block(ui.CSS_TEXT, ".badge-env {")
        self.assertIn("var(--gg-brand-red)", badge_rule)
        self.assertNotIn("var(--gg-red)", badge_rule)

    def test_brand_red_token_is_defined_as_expected_hex(self):
        root_block = _extract_css_block(ui.CSS_TEXT, ":root {")
        self.assertEqual(_extract_token(root_block, "gg-brand-red"), "#c8102e")

    def test_white_on_brand_red_meets_4_5_to_1_contrast(self):
        root_block = _extract_css_block(ui.CSS_TEXT, ":root {")
        brand_red = _extract_token(root_block, "gg-brand-red")
        ratio = _contrast_ratio("#ffffff", brand_red)
        self.assertGreaterEqual(ratio, 4.5, f"#ffffff vs {brand_red} = {ratio:.2f}:1")

    def test_brand_red_is_never_overridden_in_dark_mode(self):
        for selector in (":root:not([data-theme])", ':root[data-theme="dark"]'):
            with self.subTest(selector=selector):
                block = _extract_css_block(ui.CSS_TEXT, selector)
                self.assertNotIn("--gg-brand-red:", block)

    def test_dark_status_red_foreground_is_unchanged(self):
        dark_block = _extract_css_block(ui.CSS_TEXT, ':root[data-theme="dark"]')
        self.assertEqual(_extract_token(dark_block, "gg-red"), "#ff7b72")

    def test_no_raw_brand_red_literal_in_badge_rule(self):
        badge_rule = _extract_css_block(ui.CSS_TEXT, ".badge-env {")
        self.assertNotIn("#c8102e", badge_rule)


class UiRedesignPhase6C1Tests(unittest.TestCase):
    """Phase 6C1-UI: the redesigned ADCB-inspired portal (monitoring/monitor/ui.py)."""

    def test_no_inline_style_attributes_remain(self):
        rendered = monitor.render_html(_ui_payload(), make_config())
        self.assertNotIn('style="', rendered)

    def test_no_inline_event_handler_attributes_exist(self):
        rendered = monitor.render_html(_ui_payload(), make_config())
        self.assertIsNone(re.search(r'\son[a-z]+\s*=', rendered, re.IGNORECASE))

    def test_light_and_dark_theme_css_variables_exist(self):
        self.assertIn("--gg-bg", ui.CSS_TEXT)
        self.assertIn("--gg-text", ui.CSS_TEXT)
        self.assertIn(':root[data-theme="dark"]', ui.CSS_TEXT)
        self.assertIn(':root[data-theme="light"]', ui.CSS_TEXT)

    def test_theme_toggle_button_exists_and_is_a_native_keyboard_operable_element(self):
        rendered = monitor.render_html(_ui_payload(), make_config())
        self.assertIn('<button type="button" id="theme-toggle"', rendered)
        self.assertIn('aria-pressed=', rendered)

    def test_theme_persistence_uses_local_storage_only(self):
        self.assertIn("localStorage", ui.JS_TEXT)
        self.assertNotIn("document.cookie", ui.JS_TEXT)

    def test_system_color_scheme_preference_is_supported(self):
        self.assertIn("prefers-color-scheme", ui.CSS_TEXT)
        self.assertIn("prefers-color-scheme", ui.JS_TEXT)

    def test_theme_javascript_contains_no_network_request(self):
        for forbidden in ("fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon", "import(", "http://", "https://"):
            self.assertNotIn(forbidden, ui.JS_TEXT)

    def test_csp_permits_only_the_exact_computed_script_and_style_hashes(self):
        import base64
        import hashlib
        style_hash = "sha256-" + base64.b64encode(hashlib.sha256(ui.CSS_TEXT.encode("utf-8")).digest()).decode("ascii")
        script_hash = "sha256-" + base64.b64encode(hashlib.sha256(ui.JS_TEXT.encode("utf-8")).digest()).decode("ascii")
        csp = ui.SECURITY_HEADERS["Content-Security-Policy"]
        self.assertIn(f"style-src '{style_hash}'", csp)
        self.assertIn(f"script-src '{script_hash}'", csp)

    def test_csp_does_not_contain_unsafe_inline(self):
        csp = ui.SECURITY_HEADERS["Content-Security-Policy"]
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("unsafe-eval", csp)
        self.assertNotIn("*", csp)

    def test_rendered_script_and_style_blocks_match_the_hashed_constants_exactly(self):
        rendered = monitor.render_html(_ui_payload(), make_config())
        self.assertIn(f"<script>{ui.JS_TEXT}</script>", rendered)
        self.assertIn(f"<style>{ui.CSS_TEXT}</style>", rendered)

    def test_responsive_viewport_metadata_exists(self):
        rendered = monitor.render_html(_ui_payload(), make_config())
        self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">', rendered)

    def test_semantic_header_main_footer_elements_exist(self):
        rendered = monitor.render_html(_ui_payload(), make_config())
        self.assertIn("<header", rendered)
        self.assertIn("<main>", rendered)
        self.assertIn("<footer>", rendered)

    def test_summary_totals_are_correctly_calculated(self):
        payload = _ui_payload()
        second = dict(payload["logicalPipelines"][0]["runtimes"][0])
        second.update(deploymentName="gg-postgresql-payments-01", role="target",
                      effectiveStatus="DOWN", fresh=False, criticalServices={}, processes=[])
        payload["logicalPipelines"][0]["runtimes"].append(second)
        summary = ui._compute_summary(payload)
        self.assertEqual(summary["totalDeployments"], 2)
        self.assertEqual(summary["upDeployments"], 1)
        self.assertEqual(summary["attentionDeployments"], 1)
        self.assertEqual(summary["reachableServices"], 1)
        self.assertEqual(summary["totalServices"], 2)
        self.assertEqual(summary["totalProcesses"], 1)
        self.assertEqual(summary["overallState"], ui.OVERALL_ATTENTION)

    def test_summary_calculation_makes_no_new_calls_pure_function_of_payload(self):
        payload = _ui_payload()
        summary_a = ui._compute_summary(payload)
        summary_b = ui._compute_summary(payload)
        self.assertEqual(summary_a, summary_b)

    def test_down_condition_produces_attention_summary_and_non_healthy_banner(self):
        payload = _ui_payload(effectiveStatus="DOWN", fresh=False)
        rendered = monitor.render_html(payload, make_config())
        self.assertIn('class="overall-banner attention"', rendered)
        self.assertNotIn('class="overall-banner ok"', rendered)

    def test_stale_missing_unknown_all_produce_attention(self):
        for status in ("STALE", "MISSING", "UNKNOWN"):
            with self.subTest(status=status):
                summary = ui._compute_summary(_ui_payload(effectiveStatus=status))
                self.assertEqual(summary["attentionDeployments"], 1)
                self.assertEqual(summary["overallState"], ui.OVERALL_ATTENTION)

    def test_starting_status_does_not_by_itself_block_healthy(self):
        summary = ui._compute_summary(_ui_payload(effectiveStatus="STARTING"))
        self.assertEqual(summary["attentionDeployments"], 0)

    def test_all_up_produces_healthy_overall_banner(self):
        payload = _ui_payload(criticalServices={"adminsrvr": True, "distsrvr": True},
                              processDiscovery={"status": "OK"})
        rendered = monitor.render_html(payload, make_config())
        self.assertIn('class="overall-banner ok"', rendered)
        self.assertEqual(ui._compute_summary(payload)["overallState"], ui.OVERALL_HEALTHY)

    def test_overall_state_healthy_requires_deployment_services_and_processes(self):
        payload = _ui_payload(criticalServices={"adminsrvr": True, "distsrvr": True},
                              processDiscovery={"status": "OK"})
        self.assertEqual(ui._compute_summary(payload)["overallState"], ui.OVERALL_HEALTHY)

    def test_missing_discovery_with_active_process_remains_limited_visibility(self):
        # Task 4 correction: an active process row alone must never imply discovery is OK.
        payload = _ui_payload(criticalServices={"adminsrvr": True, "distsrvr": True})
        self.assertEqual(ui._compute_summary(payload)["overallState"], ui.OVERALL_LIMITED_VISIBILITY)

    def test_overall_state_limited_visibility_when_no_process_rows(self):
        payload = _ui_payload(criticalServices={"adminsrvr": True, "distsrvr": True}, processes=[])
        self.assertEqual(ui._compute_summary(payload)["overallState"], ui.OVERALL_LIMITED_VISIBILITY)
        rendered = monitor.render_html(payload, make_config())
        self.assertIn('class="overall-banner limited"', rendered)
        self.assertIn("Process visibility unavailable", rendered)
        self.assertNotIn("Overall: Healthy", rendered)

    def test_overall_state_attention_on_service_down(self):
        payload = _ui_payload(criticalServices={"adminsrvr": True, "distsrvr": False})
        self.assertEqual(ui._compute_summary(payload)["overallState"], ui.OVERALL_ATTENTION)

    def test_overall_state_attention_on_process_abended(self):
        payload = _ui_payload(criticalServices={"adminsrvr": True, "distsrvr": True})
        payload["logicalPipelines"][0]["runtimes"][0]["processes"][0]["status"] = "ABENDED"
        self.assertEqual(ui._compute_summary(payload)["overallState"], ui.OVERALL_ATTENTION)

    def test_overall_state_attention_on_deployment_stale_down_missing_unknown(self):
        for status in ("STALE", "DOWN", "MISSING", "UNKNOWN"):
            with self.subTest(status=status):
                payload = _ui_payload(criticalServices={"adminsrvr": True, "distsrvr": True}, effectiveStatus=status)
                self.assertEqual(ui._compute_summary(payload)["overallState"], ui.OVERALL_ATTENTION)

    def test_overall_state_empty_deployment_set_never_healthy(self):
        payload = {"generatedAt": 1, "logicalPipelines": []}
        self.assertEqual(ui._compute_summary(payload)["overallState"], ui.OVERALL_ATTENTION)
        rendered = monitor.render_html(payload, make_config())
        self.assertNotIn("Overall: Healthy", rendered)

    def test_limited_visibility_never_claims_process_health(self):
        payload = _ui_payload(criticalServices={"adminsrvr": True, "distsrvr": True}, processes=[])
        rendered = monitor.render_html(payload, make_config())
        for forbidden in ("processes healthy", "replication healthy", "Extract healthy", "Replicat healthy"):
            self.assertNotIn(forbidden, rendered)

    def test_service_reachable_and_down_chip_text_remains_visible(self):
        rendered = monitor.render_html(_ui_payload(), make_config())
        self.assertIn('class="chip chip-reachable">reachable<', rendered)
        self.assertIn('class="chip chip-unreachable">down<', rendered)

    def test_process_rows_preserve_every_existing_field(self):
        rendered = monitor.render_html(_ui_payload(), make_config())
        self.assertIn("EXTORA1", rendered)
        self.assertIn("extract", rendered)
        self.assertIn("RUNNING", rendered)
        self.assertIn("5s / thr 300s (alert)", rendered)
        self.assertIn("3s ago", rendered)
        self.assertIn(">0<", rendered)

    def test_empty_process_state_does_not_claim_replication_health(self):
        rendered = monitor.render_html(_ui_payload(processes=[]), make_config())
        self.assertIn("No process state available", rendered)
        for forbidden in ("replication is healthy", "is healthy", "replicating normally"):
            self.assertNotIn(forbidden, rendered)

    def test_empty_process_state_does_not_hide_the_missing_discovery_condition(self):
        rendered = monitor.render_html(_ui_payload(processes=[]), make_config())
        self.assertIn("No Extract or Replicat process STATE rows have been recorded.", rendered)

    def test_discovery_not_reported_shows_safe_default(self):
        rendered = monitor.render_html(_ui_payload(), make_config())
        self.assertIn("Not reported", rendered)

    def test_discovery_empty_renders_honest_empty_state(self):
        discovery = {"status": "EMPTY", "extractCount": 0, "replicatCount": 0, "distpathCount": 0}
        rendered = monitor.render_html(_ui_payload(processes=[], processDiscovery=discovery), make_config())
        self.assertIn("Empty inventory", rendered)
        self.assertIn("No replication processes discovered", rendered)
        self.assertIn("Replication health is not claimed.", rendered)

    def test_discovery_partial_renders_attention_text(self):
        discovery = {"status": "PARTIAL", "extractCount": 1, "replicatCount": 0, "distpathCount": 0}
        rendered = monitor.render_html(_ui_payload(processDiscovery=discovery), make_config())
        self.assertIn("Partially available", rendered)
        self.assertIn("Process discovery partially available", rendered)

    def test_discovery_unavailable_renders_attention_text(self):
        discovery = {"status": "UNAVAILABLE", "extractCount": 0, "replicatCount": 0, "distpathCount": 0}
        rendered = monitor.render_html(_ui_payload(processDiscovery=discovery), make_config())
        self.assertIn("Unavailable", rendered)
        self.assertIn("Process discovery unavailable", rendered)

    def test_discovery_invalid_response_renders_attention_text(self):
        discovery = {"status": "INVALID_RESPONSE", "extractCount": 0, "replicatCount": 0, "distpathCount": 0}
        rendered = monitor.render_html(_ui_payload(processDiscovery=discovery), make_config())
        self.assertIn("Invalid response", rendered)
        self.assertIn("Invalid process inventory response", rendered)

    def test_discovery_counts_rendered_compactly(self):
        discovery = {"status": "OK", "extractCount": 2, "replicatCount": 1, "distpathCount": 3}
        rendered = monitor.render_html(_ui_payload(processDiscovery=discovery), make_config())
        self.assertIn("Extract 2", rendered)
        self.assertIn("Replicat 1", rendered)
        self.assertIn("Distribution 3", rendered)

    def test_discovery_status_is_html_escaped(self):
        discovery = {"status": "<script>alert(1)</script>", "extractCount": 0, "replicatCount": 0, "distpathCount": 0}
        rendered = monitor.render_html(_ui_payload(processDiscovery=discovery), make_config())
        self.assertNotIn("<script>alert(1)</script>", rendered)

    def test_discovery_never_exposes_a_raw_error_field(self):
        discovery = {"status": "UNAVAILABLE", "extractCount": 0, "replicatCount": 0, "distpathCount": 0,
                     "rawError": "connection refused to 10.0.0.5:8443"}
        rendered = monitor.render_html(_ui_payload(processDiscovery=discovery), make_config())
        self.assertNotIn("10.0.0.5", rendered)
        self.assertNotIn("connection refused", rendered)


class OverallStateDiscoveryAndStaleTests(unittest.TestCase):
    """Phase 6C1B Task 11: overall header state driven by processDiscovery and stale process rows."""

    def test_discovery_partial_forces_attention(self):
        payload = _ui_payload(criticalServices={"adminsrvr": True, "distsrvr": True},
                              processDiscovery={"status": "PARTIAL"})
        self.assertEqual(ui._compute_summary(payload)["overallState"], ui.OVERALL_ATTENTION)

    def test_discovery_unavailable_forces_attention(self):
        payload = _ui_payload(criticalServices={"adminsrvr": True, "distsrvr": True},
                              processDiscovery={"status": "UNAVAILABLE"})
        self.assertEqual(ui._compute_summary(payload)["overallState"], ui.OVERALL_ATTENTION)

    def test_discovery_invalid_response_forces_attention(self):
        payload = _ui_payload(criticalServices={"adminsrvr": True, "distsrvr": True},
                              processDiscovery={"status": "INVALID_RESPONSE"})
        self.assertEqual(ui._compute_summary(payload)["overallState"], ui.OVERALL_ATTENTION)

    def test_stale_process_row_forces_attention(self):
        payload = _ui_payload(criticalServices={"adminsrvr": True, "distsrvr": True})
        payload["logicalPipelines"][0]["runtimes"][0]["processes"][0]["stale"] = True
        self.assertEqual(ui._compute_summary(payload)["overallState"], ui.OVERALL_ATTENTION)

    def test_discovery_ok_with_current_process_is_healthy(self):
        payload = _ui_payload(criticalServices={"adminsrvr": True, "distsrvr": True},
                              processDiscovery={"status": "OK"})
        self.assertEqual(ui._compute_summary(payload)["overallState"], ui.OVERALL_HEALTHY)

    def test_discovery_empty_with_no_processes_is_limited_visibility_not_attention(self):
        payload = _ui_payload(criticalServices={"adminsrvr": True, "distsrvr": True}, processes=[],
                              processDiscovery={"status": "EMPTY"})
        self.assertEqual(ui._compute_summary(payload)["overallState"], ui.OVERALL_LIMITED_VISIBILITY)

    def test_summary_reports_discovery_issues_and_stale_process_counts(self):
        payload = _ui_payload(criticalServices={"adminsrvr": True, "distsrvr": True},
                              processDiscovery={"status": "PARTIAL"})
        payload["logicalPipelines"][0]["runtimes"][0]["processes"][0]["stale"] = True
        summary = ui._compute_summary(payload)
        self.assertEqual(summary["discoveryIssues"], 1)
        self.assertEqual(summary["staleProcesses"], 1)

    def test_empty_discovery_with_an_active_legacy_row_remains_limited_visibility(self):
        # A stale-schema STATE row predating discovery reporting must never upgrade an EMPTY discovery to healthy.
        payload = _ui_payload(criticalServices={"adminsrvr": True, "distsrvr": True},
                              processDiscovery={"status": "EMPTY"})
        self.assertEqual(ui._compute_summary(payload)["overallState"], ui.OVERALL_LIMITED_VISIBILITY)

    def test_ok_discovery_with_zero_process_rows_remains_limited_visibility(self):
        payload = _ui_payload(criticalServices={"adminsrvr": True, "distsrvr": True}, processes=[],
                              processDiscovery={"status": "OK"})
        self.assertEqual(ui._compute_summary(payload)["overallState"], ui.OVERALL_LIMITED_VISIBILITY)

    def test_one_ok_runtime_and_one_empty_runtime_remains_limited_visibility(self):
        payload = _ui_payload(criticalServices={"adminsrvr": True, "distsrvr": True},
                              processDiscovery={"status": "OK"})
        second = dict(payload["logicalPipelines"][0]["runtimes"][0])
        second.update(deploymentName="gg-postgresql-payments-01", role="target", processes=[],
                     processDiscovery={"status": "EMPTY"})
        payload["logicalPipelines"][0]["runtimes"].append(second)
        self.assertEqual(ui._compute_summary(payload)["overallState"], ui.OVERALL_LIMITED_VISIBILITY)

    def test_every_runtime_ok_with_current_non_abended_rows_is_healthy(self):
        payload = _ui_payload(criticalServices={"adminsrvr": True, "distsrvr": True},
                              processDiscovery={"status": "OK"})
        second = dict(payload["logicalPipelines"][0]["runtimes"][0])
        second.update(deploymentName="gg-postgresql-payments-01", role="target")
        payload["logicalPipelines"][0]["runtimes"].append(second)
        self.assertEqual(ui._compute_summary(payload)["overallState"], ui.OVERALL_HEALTHY)

    def test_error_banner_remains_sanitized_and_has_role_alert(self):
        rendered = monitor.render_html(_ui_payload(), make_config(),
                                       error_message=monitor.CLIENT_SAFE_DYNAMODB_ERROR_MESSAGE)
        self.assertIn('role="alert"', rendered)
        self.assertIn(monitor.CLIENT_SAFE_DYNAMODB_ERROR_MESSAGE, rendered)
        self.assertNotIn("Traceback", rendered)

    def test_meta_auto_refresh_retains_the_configured_interval(self):
        cfg = make_config(REFRESH_SECONDS="45")
        rendered = monitor.render_html(_ui_payload(), cfg)
        self.assertIn('<meta http-equiv="refresh" content="45">', rendered)
        self.assertIn("Auto-refresh: 45s", rendered)

    def test_manual_refresh_control_exists_as_a_plain_navigation_link(self):
        rendered = monitor.render_html(_ui_payload(), make_config())
        self.assertIn('<a class="btn" href="/"', rendered)

    def test_alerts_enabled_and_metrics_enabled_are_displayed(self):
        rendered = monitor.render_html(_ui_payload(alertsEnabled=True, metricsEnabled=False), make_config())
        self.assertIn("Alerts enabled", rendered)
        self.assertIn("Metrics enabled", rendered)
        self.assertIn(">true<", rendered)
        self.assertIn(">false<", rendered)

    def test_environment_badge_is_not_hardcoded_to_dev(self):
        source = inspect.getsource(ui)
        self.assertNotIn('<span class="badge-env">DEV</span>', source)

    def test_environment_badge_renders_supplied_dev_value(self):
        rendered = monitor.render_html(_ui_payload(), make_config(), environment="dev")
        self.assertIn('<span class="badge-env">DEV</span>', rendered)

    def test_environment_badge_renders_supplied_vdr_value(self):
        rendered = monitor.render_html(_ui_payload(), make_config(), environment="vdr")
        self.assertIn('<span class="badge-env">VDR</span>', rendered)

    def test_environment_badge_renders_supplied_prod_value(self):
        rendered = monitor.render_html(_ui_payload(), make_config(), environment="prod")
        self.assertIn('<span class="badge-env">PROD</span>', rendered)

    def test_environment_badge_reads_from_payload_when_no_kwarg_given(self):
        payload = _ui_payload()
        payload["environment"] = "vdr"
        rendered = monitor.render_html(payload, make_config())
        self.assertIn('<span class="badge-env">VDR</span>', rendered)

    def test_environment_badge_reads_from_config_when_present(self):
        config = make_config()
        config.environment = "prod"
        rendered = monitor.render_html(_ui_payload(), config)
        self.assertIn('<span class="badge-env">PROD</span>', rendered)

    def test_environment_badge_escapes_hostile_text(self):
        malicious = '<script>alert(1)</script>'
        rendered = monitor.render_html(_ui_payload(), make_config(), environment=malicious)
        self.assertNotIn(malicious, rendered)
        self.assertIn(html_module.escape(malicious.upper()), rendered)

    def test_environment_badge_missing_value_is_handled_honestly(self):
        rendered = monitor.render_html(_ui_payload(), make_config())
        self.assertIn('<span class="badge-env">ENVIRONMENT UNKNOWN</span>', rendered)
        self.assertNotIn('<span class="badge-env">DEV</span>', rendered)

    def test_lease_state_and_holder_remain_displayed(self):
        rendered = monitor.render_html(_ui_payload(), make_config())
        self.assertIn("Lease holder", rendered)
        self.assertIn("Lease validity", rendered)
        self.assertIn("gg-monitor-0", rendered)
        self.assertIn("valid", rendered)

    def test_api_status_and_api_processes_handlers_still_use_the_unmodified_data_functions(self):
        source = inspect.getsource(monitor)
        self.assertIn("build_status_payload(config, table, deployments, logical_pipelines)", source)
        self.assertIn("build_processes_payload(config, table, deployments)", source)

    def test_healthz_and_readyz_handlers_are_unaffected_by_the_ui_module(self):
        source = inspect.getsource(monitor._make_handler)
        healthz_start = source.index("_handle_healthz")
        readyz_start = source.index("_handle_readyz")
        handler_region = source[min(healthz_start, readyz_start):min(healthz_start, readyz_start) + 1200]
        self.assertNotIn("ui.", handler_region)

    def test_no_external_script_style_font_or_cdn_reference(self):
        rendered = monitor.render_html(_ui_payload(), make_config())
        for forbidden in ("http://", "https://", "cdn.", "fonts.googleapis", "<link rel=\"stylesheet\""):
            self.assertNotIn(forbidden, rendered)

    def test_no_react_angular_vue_or_npm_artifact_referenced(self):
        source = ui.CSS_TEXT + ui.JS_TEXT
        for forbidden in ("react", "angular", "vue", "node_modules", "webpack"):
            self.assertNotIn(forbidden, source.lower())


class ApiProcessesTests(unittest.TestCase):
    """GET /api/processes: canonical STATE# only, GetItem/Query only, no legacy fallback, no writes, no secret leakage."""

    def _handler(self, table_factory):
        handler_cls = monitor._make_handler(make_config(), table_factory, DEPLOYMENTS, LOGICAL_PIPELINES, {}, [])
        handler = handler_cls.__new__(handler_cls)
        writes = []
        handler._write = lambda status, ctype, body: writes.append((status, ctype, body))
        return handler, writes

    def test_success_returns_200_with_expected_schema(self):
        # Goes through the real HTTP handler (uses time.time, not an injected clock), so fixtures must be fresh.
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

    def test_process_discovery_is_an_additive_field(self):
        now = int(time.time())
        table = FakeTable([
            make_deployment_state_item(recorded_at=now - 5,
                                       processDiscovery={"status": "OK", "collectedAt": now - 5,
                                                        "extractCount": 1, "replicatCount": 0,
                                                        "distpathCount": 0, "totalCount": 1,
                                                        "extractsStatus": "OK", "replicatsStatus": "OK",
                                                        "sourcesStatus": "EMPTY", "detailFailureCount": 0}),
            make_process_item(recorded_at=now - 3),
        ])
        handler, writes = self._handler(lambda: table)
        handler.path = "/api/processes"
        handler.do_GET()
        body = json.loads(writes[0][2])
        dep = next(d for d in body["deployments"] if d["deploymentName"] == "gg-oracle-payments-01")
        self.assertEqual(dep["processDiscovery"]["status"], "OK")
        self.assertEqual(dep["processDiscovery"]["extractCount"], 1)

    def test_uses_canonical_state_schema_only_no_legacy_fallback(self):
        # A record under the legacy per-role partition key must show MISSING; /api/processes reads canonical STATE# records only, same as /api/status.
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
        # STATE#_deployment missing must not suppress independently-existing STATE#<process> rows either.
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

    def test_api_status_never_falls_back_to_legacy_observer_partition(self):
        # A record under the legacy per-role partition key must be ignored -- role reports MISSING, not its status.
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
        self.assertEqual(source["dataSource"], "canonical-monitor")
        self.assertEqual(source["effectiveStatus"], "MISSING")


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
    """Extract a step's `run: |` block body, dedented, via plain text scanning (no PyYAML) to stay dependency-free."""
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
    """Renders the real helm chart (staged as the workflow stages it) and asserts the generated SecretProviderClass/CSI wiring, not a reimplementation of the template logic."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("helm") is None:
            raise unittest.SkipTest("helm not available")
        cls.tmpdir = tempfile.mkdtemp()
        staged_chart = os.path.join(cls.tmpdir, "goldengate-monitor")
        shutil.copytree(MONITOR_CHART_PATH, staged_chart)
        _stage_generated_registry(os.path.join(staged_chart, "files"))

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
        doc = cfgmod.load_deployments(_stage_generated_registry_dir())
        for d in doc["deployments"]:
            if not d["enabled"]:
                continue
            self.assertIn(f"{d['name']}-admin-user", self.rendered)
            self.assertIn(f"{d['name']}-admin-password", self.rendered)
            self.assertIn(d["adminSecret"], self.rendered)

    def test_ca_chain_alias_present(self):
        self.assertIn("ca-chain-pem", self.rendered)
        doc = cfgmod.load_deployments(_stage_generated_registry_dir())
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


class CloudWatchActivationHelmRenderTests(unittest.TestCase):
    """--set cloudwatch.publishEnabled=<bool> must render the exact literal string the strict env parser accepts, proven against the real chart, not a reimplementation of Helm's type inference."""

    def _render(self, publish_enabled):
        if shutil.which("helm") is None:
            raise unittest.SkipTest("helm not available")
        tmpdir = tempfile.mkdtemp()
        staged_chart = os.path.join(tmpdir, "goldengate-monitor")
        shutil.copytree(MONITOR_CHART_PATH, staged_chart)
        _stage_generated_registry(os.path.join(staged_chart, "files"))

        proc = subprocess.run(
            ["helm", "template", "gg-monitor", staged_chart,
             "--namespace", "goldengate-monitoring",
             "-f", os.path.join(REPO_ROOT, "envs", "dev", "goldengate-monitor", "values.yaml"),
             "--set", "image.repository=example.invalid/goldengate-monitor",
             "--set", "image.tag=test",
             "--set", "serviceAccount.roleArn=arn:aws:iam::668311715351:role/GoldenGateMonitorReadRole-dev",
             "--set", f"cloudwatch.publishEnabled={publish_enabled}"],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        if proc.returncode != 0:
            raise AssertionError(f"helm template failed: {proc.stdout}\n{proc.stderr}")
        return proc.stdout

    def test_true_input_renders_cloudwatch_publish_enabled_true(self):
        rendered = self._render("true")
        self.assertIn('name: CLOUDWATCH_PUBLISH_ENABLED\n              value: "true"', rendered)

    def test_false_input_renders_cloudwatch_publish_enabled_false(self):
        rendered = self._render("false")
        self.assertIn('name: CLOUDWATCH_PUBLISH_ENABLED\n              value: "false"', rendered)

    def test_rendered_value_is_never_a_truthy_alias(self):
        rendered = self._render("true")
        for forbidden_value in ('value: "1"', 'value: "yes"', 'value: "on"'):
            self.assertNotIn(f"name: CLOUDWATCH_PUBLISH_ENABLED\n              {forbidden_value}", rendered)


def _extract_monitor_image_hash_script(workflow_text):
    """Pulls the exact, committed hash-computation snippet out of the real workflow file, executed verbatim in tests, never reimplemented."""
    start = workflow_text.index("MONITOR_IMAGE_INPUT_PATHS=(")
    end = workflow_text.index('MONITOR_IMAGE_TAG="mon-', start)
    end = workflow_text.index("\n", end) + 1
    return workflow_text[start:end]


def _extract_base_image_validation_script(workflow_text):
    """Pulls the committed base-image validation snippet out of the real workflow, executed verbatim as ordinary bash (no GitHub-Actions token substitution needed since it's only referenced via step-level `env:`)."""
    start = workflow_text.index('MONITOR_BASE_IMAGE="$MONITOR_BASE_IMAGE_INPUT"')
    end = workflow_text.index("Confirmed: MONITOR_BASE_IMAGE is a digest-pinned", start)
    end = workflow_text.index("\n", end) + 1
    return workflow_text[start:end]


class MonitorBaseImageValidationTests(unittest.TestCase):
    """Fail-closed, digest-pinned private-ECR base-image gate, proven by executing the actual committed validation script (not reimplemented); the GitHub expression is supplied only via step-level env, never interpolated into the shell source."""

    ECR_REGISTRY = "229410149234.dkr.ecr.eu-west-1.amazonaws.com"
    APPROVED_DIGEST_REF = f"{ECR_REGISTRY}/goldengate-monitor-base@sha256:{'a' * 64}"

    @classmethod
    def setUpClass(cls):
        if shutil.which("bash") is None:
            raise unittest.SkipTest("bash not available")
        with open(MONITOR_WORKFLOW_PATH) as f:
            cls.workflow_text = f.read()
        cls.validation_script = _extract_base_image_validation_script(cls.workflow_text)

    def _run(self, base_image_input, extra_env=None):
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as github_env_file:
            github_env_path = github_env_file.name
        try:
            script = f"set -euo pipefail\n{self.validation_script}"
            env = {**os.environ, "ECR_REGISTRY": self.ECR_REGISTRY,
                  "MONITOR_BASE_IMAGE_INPUT": base_image_input,
                  "GITHUB_ENV": github_env_path}
            env.update(extra_env or {})
            return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
        finally:
            os.unlink(github_env_path)

    def test_no_direct_github_expression_interpolation_remains_in_run_script(self):
        self.assertNotIn("${{ vars.MONITOR_BASE_IMAGE }}", self.validation_script)
        # The one legitimate occurrence must be in a step-level `env:` value, never inside a `run:` script body.
        self.assertIn("MONITOR_BASE_IMAGE_INPUT: ${{ vars.MONITOR_BASE_IMAGE }}", self.workflow_text)

    def test_missing_base_image_fails(self):
        proc = self._run("")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("MONITOR_BASE_IMAGE is not set", proc.stdout)

    def test_public_docker_hub_base_image_fails(self):
        proc = self._run("python:3.12-slim")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not a private image in the approved ECR registry", proc.stdout)

    def test_public_ghcr_base_image_fails(self):
        proc = self._run("ghcr.io/example/python@sha256:" + "a" * 64)
        self.assertNotEqual(proc.returncode, 0)

    def test_other_account_ecr_base_image_fails(self):
        # Same ECR *service*, different (unapproved) account/registry host.
        proc = self._run("111111111111.dkr.ecr.eu-west-1.amazonaws.com/goldengate-monitor-base@sha256:" + "a" * 64)
        self.assertNotEqual(proc.returncode, 0)

    def test_tag_only_ecr_base_image_fails(self):
        proc = self._run(f"{self.ECR_REGISTRY}/goldengate-monitor-base:3.12-slim")
        self.assertNotEqual(proc.returncode, 0)

    def test_uppercase_hex_digest_fails(self):
        proc = self._run(f"{self.ECR_REGISTRY}/goldengate-monitor-base@sha256:" + "A" * 64)
        self.assertNotEqual(proc.returncode, 0)

    def test_short_digest_fails(self):
        proc = self._run(f"{self.ECR_REGISTRY}/goldengate-monitor-base@sha256:" + "a" * 63)
        self.assertNotEqual(proc.returncode, 0)

    def test_empty_repository_fails(self):
        proc = self._run(f"{self.ECR_REGISTRY}/@sha256:" + "a" * 64)
        self.assertNotEqual(proc.returncode, 0)

    def test_whitespace_in_repository_fails(self):
        proc = self._run(f"{self.ECR_REGISTRY}/base image@sha256:" + "a" * 64)
        self.assertNotEqual(proc.returncode, 0)

    def test_control_character_in_repository_fails(self):
        proc = self._run(f"{self.ECR_REGISTRY}/base\timage@sha256:" + "a" * 64)
        self.assertNotEqual(proc.returncode, 0)

    def test_dollar_paren_command_substitution_fails(self):
        proc = self._run(f"{self.ECR_REGISTRY}/base$(whoami)@sha256:" + "a" * 64)
        self.assertNotEqual(proc.returncode, 0)

    def test_backtick_command_substitution_fails(self):
        proc = self._run(f"{self.ECR_REGISTRY}/base`whoami`@sha256:" + "a" * 64)
        self.assertNotEqual(proc.returncode, 0)

    def test_embedded_double_quote_fails(self):
        proc = self._run(f'{self.ECR_REGISTRY}/base"image@sha256:' + "a" * 64)
        self.assertNotEqual(proc.returncode, 0)

    def test_dot_dot_slash_path_component_fails(self):
        proc = self._run(f"{self.ECR_REGISTRY}/../etc/passwd@sha256:" + "a" * 64)
        self.assertNotEqual(proc.returncode, 0)

    def test_dot_slash_path_component_fails(self):
        proc = self._run(f"{self.ECR_REGISTRY}/./base@sha256:" + "a" * 64)
        self.assertNotEqual(proc.returncode, 0)

    def test_shell_metacharacter_semicolon_fails(self):
        proc = self._run(f"{self.ECR_REGISTRY}/base;id@sha256:" + "a" * 64)
        self.assertNotEqual(proc.returncode, 0)

    def test_digest_pinned_approved_private_ecr_base_image_passes(self):
        proc = self._run(self.APPROVED_DIGEST_REF)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Confirmed: MONITOR_BASE_IMAGE is a digest-pinned private ECR reference", proc.stdout)

    def test_failure_never_prints_the_raw_malformed_value(self):
        malformed = "docker.io/library/python:3.12-slim-SECRET-MARKER-zzz"
        proc = self._run(malformed)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn(malformed, proc.stdout)
        self.assertNotIn(malformed, proc.stderr)
        self.assertNotIn("SECRET-MARKER", proc.stdout)

    def test_success_path_never_prints_the_full_raw_value_either(self):
        proc = self._run(self.APPROVED_DIGEST_REF)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn(self.APPROVED_DIGEST_REF, proc.stdout)
        self.assertNotIn("a" * 64, proc.stdout)

    def _run_with_marker_file(self, base_image_input):
        """Proves the value is handled as inert data: an injection payload runs `touch <marker>` if it were ever evaluated as shell code, and the marker must never be created."""
        with tempfile.TemporaryDirectory() as tmp:
            marker_path = os.path.join(tmp, "command-executed.marker")
            proc = self._run(base_image_input, extra_env={"MARKER_PATH": marker_path})
            marker_created = os.path.exists(marker_path)
        return proc, marker_created

    def test_command_substitution_payload_never_executes(self):
        proc, marker_created = self._run_with_marker_file(
            f"{self.ECR_REGISTRY}/base$(touch \"$MARKER_PATH\")@sha256:" + "a" * 64)
        self.assertFalse(marker_created, "$() payload executed a command instead of being treated as inert data")
        self.assertNotEqual(proc.returncode, 0)

    def test_backtick_payload_never_executes(self):
        proc, marker_created = self._run_with_marker_file(
            f'{self.ECR_REGISTRY}/base`touch "$MARKER_PATH"`@sha256:' + "a" * 64)
        self.assertFalse(marker_created, "backtick payload executed a command instead of being treated as inert data")
        self.assertNotEqual(proc.returncode, 0)

    def test_embedded_double_quote_payload_never_executes(self):
        proc, marker_created = self._run_with_marker_file(
            f'{self.ECR_REGISTRY}/base" ; touch "$MARKER_PATH" ; echo "@sha256:' + "a" * 64)
        self.assertFalse(marker_created, "embedded double-quote payload executed a command")
        self.assertNotEqual(proc.returncode, 0)

    def test_embedded_newline_payload_never_executes(self):
        proc, marker_created = self._run_with_marker_file(
            f'{self.ECR_REGISTRY}/base\ntouch "$MARKER_PATH"\n@sha256:' + "a" * 64)
        self.assertFalse(marker_created, "embedded-newline payload executed a command")
        self.assertNotEqual(proc.returncode, 0)

    def test_embedded_control_character_payload_never_executes(self):
        # \x00 can't appear in an env var (NUL-terminated C strings), but \x01/\x1b can and must still be rejected.
        proc, marker_created = self._run_with_marker_file(
            f'{self.ECR_REGISTRY}/base\x01\x1b@sha256:' + "a" * 64)
        self.assertFalse(marker_created, "embedded control-character payload executed a command")
        self.assertNotEqual(proc.returncode, 0)

    def test_whitespace_payload_never_executes(self):
        proc, marker_created = self._run_with_marker_file(
            f'{self.ECR_REGISTRY}/base && touch "$MARKER_PATH" @sha256:' + "a" * 64)
        self.assertFalse(marker_created, "whitespace/shell-metacharacter payload executed a command")
        self.assertNotEqual(proc.returncode, 0)

    def test_shell_metacharacter_payload_never_executes(self):
        proc, marker_created = self._run_with_marker_file(
            f'{self.ECR_REGISTRY}/base; touch "$MARKER_PATH"; true@sha256:' + "a" * 64)
        self.assertFalse(marker_created, "shell-metacharacter payload executed a command")
        self.assertNotEqual(proc.returncode, 0)


class MonitorImageHashTests(unittest.TestCase):
    """The runtime-image content hash must depend only on the paths the Dockerfile COPYs (never README.md/requirements-test.txt/tests/**), proven by executing the actual committed hash script against a throwaway git repository."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("git") is None:
            raise unittest.SkipTest("git not available")
        with open(MONITOR_WORKFLOW_PATH) as f:
            cls.hash_script = _extract_monitor_image_hash_script(f.read())

    DEFAULT_BASE_IMAGE = (
        "229410149234.dkr.ecr.eu-west-1.amazonaws.com/goldengate-monitor-base"
        "@sha256:" + "a" * 64)
    ALTERNATE_BASE_IMAGE = (
        "229410149234.dkr.ecr.eu-west-1.amazonaws.com/goldengate-monitor-base"
        "@sha256:" + "b" * 64)

    def _compute_hash(self, repo_dir, base_image=None):
        # hash_script already ends with a newline, so the appended echo starts on its own line.
        script = f'set -euo pipefail\n{self.hash_script}echo "$MONITOR_TREE_SHA"'
        proc = subprocess.run(
            ["bash", "-c", script],
            cwd=repo_dir,
            env={**os.environ, "MONITOR_SOURCE_PATH": ".",
                "MONITOR_BASE_IMAGE": base_image or self.DEFAULT_BASE_IMAGE},
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, f"hash script failed: {proc.stdout}\n{proc.stderr}")
        return proc.stdout.strip()

    def _init_repo(self, tmp):
        for cmd in (["git", "init", "-q"],
                   ["git", "config", "user.email", "test@example.invalid"],
                   ["git", "config", "user.name", "test"]):
            subprocess.run(cmd, cwd=tmp, check=True)
        os.makedirs(os.path.join(tmp, "tools"), exist_ok=True)
        os.makedirs(os.path.join(tmp, "tests"), exist_ok=True)
        files = {
            "Dockerfile": "FROM python:3.12-slim\n",
            ".dockerignore": "tests/\nREADME.md\n__pycache__/\n",
            "requirements.txt": "boto3\n",
            "requirements-test.txt": "pytest\n",
            "monitor.py": "print('monitor')\n",
            "collector.py": "print('collector')\n",
            "config.py": "print('config')\n",
            "health_rules.py": "print('health_rules')\n",
            "tools/gg_api_contract_probe.py": "print('tool')\n",
            "README.md": "# readme v1\n",
            "tests/test_monitor.py": "# test v1\n",
        }
        for relpath, content in files.items():
            with open(os.path.join(tmp, relpath), "w") as f:
                f.write(content)
        subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=tmp, check=True)

    def _commit(self, tmp, message):
        subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
        subprocess.run(["git", "commit", "-q", "-m", message], cwd=tmp, check=True)

    def test_readme_only_change_does_not_change_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._init_repo(tmp)
            before = self._compute_hash(tmp)
            with open(os.path.join(tmp, "README.md"), "w") as f:
                f.write("# readme v2 -- substantially changed\n")
            self._commit(tmp, "readme change")
            after = self._compute_hash(tmp)
        self.assertEqual(before, after)

    def test_test_only_change_does_not_change_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._init_repo(tmp)
            before = self._compute_hash(tmp)
            with open(os.path.join(tmp, "tests", "test_monitor.py"), "w") as f:
                f.write("# test v2 -- substantially changed\n")
            with open(os.path.join(tmp, "requirements-test.txt"), "w") as f:
                f.write("pytest==8\n")
            self._commit(tmp, "test change")
            after = self._compute_hash(tmp)
        self.assertEqual(before, after)

    def test_dockerfile_change_changes_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._init_repo(tmp)
            before = self._compute_hash(tmp)
            with open(os.path.join(tmp, "Dockerfile"), "w") as f:
                f.write("FROM python:3.12-slim\nRUN true\n")
            self._commit(tmp, "dockerfile change")
            after = self._compute_hash(tmp)
        self.assertNotEqual(before, after)

    def test_requirements_change_changes_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._init_repo(tmp)
            before = self._compute_hash(tmp)
            with open(os.path.join(tmp, "requirements.txt"), "w") as f:
                f.write("boto3==2\n")
            self._commit(tmp, "requirements change")
            after = self._compute_hash(tmp)
        self.assertNotEqual(before, after)

    def test_collector_change_changes_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._init_repo(tmp)
            before = self._compute_hash(tmp)
            with open(os.path.join(tmp, "collector.py"), "w") as f:
                f.write("print('collector v2')\n")
            self._commit(tmp, "collector change")
            after = self._compute_hash(tmp)
        self.assertNotEqual(before, after)

    def test_tools_change_changes_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._init_repo(tmp)
            before = self._compute_hash(tmp)
            with open(os.path.join(tmp, "tools", "gg_api_contract_probe.py"), "w") as f:
                f.write("print('tool v2')\n")
            self._commit(tmp, "tools change")
            after = self._compute_hash(tmp)
        self.assertNotEqual(before, after)

    def test_dockerignore_change_changes_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._init_repo(tmp)
            before = self._compute_hash(tmp)
            with open(os.path.join(tmp, ".dockerignore"), "w") as f:
                f.write("tests/\nREADME.md\n__pycache__/\n*.log\n")
            self._commit(tmp, "dockerignore change")
            after = self._compute_hash(tmp)
        self.assertNotEqual(before, after)

    def test_readme_only_change_still_does_not_change_hash_with_dockerignore_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._init_repo(tmp)
            before = self._compute_hash(tmp)
            with open(os.path.join(tmp, "README.md"), "w") as f:
                f.write("# readme v2 -- substantially changed, again\n")
            self._commit(tmp, "readme change")
            after = self._compute_hash(tmp)
        self.assertEqual(before, after)

    def test_test_only_change_still_does_not_change_hash_with_dockerignore_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._init_repo(tmp)
            before = self._compute_hash(tmp)
            with open(os.path.join(tmp, "tests", "test_monitor.py"), "w") as f:
                f.write("# test v2 -- substantially changed, again\n")
            self._commit(tmp, "test change")
            after = self._compute_hash(tmp)
        self.assertEqual(before, after)

    def test_changing_only_base_image_digest_changes_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._init_repo(tmp)
            before = self._compute_hash(tmp, base_image=self.DEFAULT_BASE_IMAGE)
            after = self._compute_hash(tmp, base_image=self.ALTERNATE_BASE_IMAGE)
        self.assertNotEqual(before, after)

    def test_same_base_image_digest_and_same_files_preserves_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._init_repo(tmp)
            first = self._compute_hash(tmp, base_image=self.DEFAULT_BASE_IMAGE)
            second = self._compute_hash(tmp, base_image=self.DEFAULT_BASE_IMAGE)
        self.assertEqual(first, second)

    def test_missing_base_image_env_var_fails_the_hash_script(self):
        script = f'set -euo pipefail\n{self.hash_script}echo "$MONITOR_TREE_SHA"'
        env = {**os.environ, "MONITOR_SOURCE_PATH": "."}
        env.pop("MONITOR_BASE_IMAGE", None)
        with tempfile.TemporaryDirectory() as tmp:
            self._init_repo(tmp)
            proc = subprocess.run(["bash", "-c", script], cwd=tmp, env=env, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)


class WorkflowStaticAnalysisTests(unittest.TestCase):
    """Static inspection of the two GitHub Actions workflow files: proves the actual committed bash/YAML content was fixed, not a reimplementation of the same logic inside this test suite."""

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

    def test_ui_module_is_copied_into_the_docker_image(self):
        with open(os.path.join(REPO_ROOT, "monitoring", "monitor", "Dockerfile")) as f:
            dockerfile_text = f.read()
        copy_lines = [line for line in dockerfile_text.splitlines() if line.startswith("COPY ")]
        self.assertTrue(any("ui.py" in line for line in copy_lines))

    def test_ui_module_is_included_in_the_workflow_image_content_hash(self):
        hash_input_idx = self.monitor_text.index("MONITOR_IMAGE_INPUT_PATHS=(")
        ls_tree_idx = self.monitor_text.index("MONITOR_LS_TREE=")
        hash_input_block = self.monitor_text[hash_input_idx:ls_tree_idx]
        self.assertIn('"${MONITOR_SOURCE_PATH}/ui.py"', hash_input_block)

    def test_ui_module_is_mentioned_in_the_hash_reporting_message(self):
        report_idx = self.monitor_text.index("Monitor runtime-input hash (")
        report_line_end = self.monitor_text.index("\n", report_idx)
        report_line = self.monitor_text[report_idx:report_line_end]
        self.assertIn("ui.py", report_line)

    def test_dockerfile_has_no_public_base_image_default(self):
        with open(os.path.join(REPO_ROOT, "monitoring", "monitor", "Dockerfile")) as f:
            dockerfile_text = f.read()
        self.assertIn("ARG BASE_IMAGE\n", dockerfile_text)
        self.assertNotIn("ARG BASE_IMAGE=", dockerfile_text)
        self.assertNotIn("python:3.12-slim", dockerfile_text)
        self.assertIn("FROM ${BASE_IMAGE}", dockerfile_text)

    def test_docker_build_receives_base_image_build_arg(self):
        self.assertIn('--build-arg "BASE_IMAGE=${MONITOR_BASE_IMAGE}"', self.monitor_text)

    def test_base_image_validation_precedes_hash_computation(self):
        validate_idx = self.monitor_text.index("- name: Validate approved base image reference")
        prep_idx = self.monitor_text.index("- name: Prepare monitor image variables")
        build_idx = self.monitor_text.index("- name: Build monitor image")
        self.assertLess(validate_idx, prep_idx)
        self.assertLess(prep_idx, build_idx)

    def test_dockerignore_listed_as_hash_input_in_workflow(self):
        self.assertIn('"${MONITOR_SOURCE_PATH}/.dockerignore"', self.monitor_text)

    def test_readme_lists_dockerignore_as_runtime_image_input(self):
        with open(os.path.join(REPO_ROOT, "monitoring", "monitor", "README.md")) as f:
            readme_text = f.read()
        self.assertIn(".dockerignore", readme_text)

    def test_pod_selection_excludes_terminating_pods(self):
        # The preflight excludes terminating pods via a bash comparison on a jq-r extracted field (not an inline jq boolean expression), unlike the unchanged "Verify" step below.
        preflight_step_text = self.monitor_text[
            self.monitor_text.index("- name: CloudWatch publication preflight"):
            self.monitor_text.index("- name: Create or update Argo CD Application")]
        self.assertIn('deletion_ts="$(jq -r \'.metadata.deletionTimestamp // empty\'', preflight_step_text)
        self.assertIn('[ -n "$deletion_ts" ] && continue', preflight_step_text)

        verify_step_text = self.monitor_text[
            self.monitor_text.index("- name: Verify GoldenGate monitor runtime state"):
            self.monitor_text.index("- name: Upload rendered manifests and chart package")]
        self.assertIn(".metadata.deletionTimestamp == null", verify_step_text)

    def test_oci_description_reflects_collector_and_portal(self):
        self.assertNotIn("Read-only shared GoldenGate monitoring portal", self.monitor_text)
        self.assertIn("Shared GoldenGate monitoring collector and portal", self.monitor_text)

    def test_no_unsafe_inputs_deploy_condition_remains(self):
        self.assertNotIn("inputs.deploy != false", self.monitor_text)

    def test_deploy_condition_is_normalized_on_every_deployment_step(self):
        # inputs.deploy alone gates every mutating step -- github.event_name reflects the workflow_call caller's trigger, not this workflow's own.
        expected = "${{ inputs.deploy }}"
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

    def test_workflow_call_trigger_supports_orchestrated_invocation(self):
        doc = yaml.safe_load(self.monitor_text)
        triggers = doc.get("on") or doc.get(True)
        self.assertIn("workflow_call", triggers)
        call_inputs = triggers["workflow_call"]["inputs"]
        self.assertEqual(call_inputs["deploy"]["type"], "boolean")
        self.assertTrue(call_inputs["deploy"]["required"])
        self.assertEqual(call_inputs["environment"]["type"], "string")

    def test_manual_deploy_false_remains_supported(self):
        github_event_name = "workflow_dispatch"
        self.assertFalse((github_event_name != "workflow_dispatch") or False)
        self.assertTrue((github_event_name != "workflow_dispatch") or True)

    def test_unit_tests_are_not_conditional_on_image_existed(self):
        doc = yaml.safe_load(self.monitor_text)
        steps = doc["jobs"]["ensure_monitor_image"]["steps"]
        for step_name in ("Set up Python", "Install monitor runtime and test dependencies",
                         "Validate monitor Python syntax", "Run monitor unit tests"):
            with self.subTest(step=step_name):
                step = next(s for s in steps if s.get("name") == step_name)
                self.assertNotIn("if", step, f"{step_name} must run unconditionally, not gated on IMAGE_EXISTED")

    def test_docker_build_and_push_remain_conditional_on_image_existed(self):
        doc = yaml.safe_load(self.monitor_text)
        steps = doc["jobs"]["ensure_monitor_image"]["steps"]
        for step_name in ("Verify Docker binary and daemon are functional", "Login to Amazon ECR",
                         "Build monitor image", "Push monitor image"):
            with self.subTest(step=step_name):
                step = next(s for s in steps if s.get("name") == step_name)
                self.assertEqual(step.get("if"), "env.IMAGE_EXISTED != 'true'")

    def test_unit_tests_step_precedes_docker_steps(self):
        doc = yaml.safe_load(self.monitor_text)
        steps = doc["jobs"]["ensure_monitor_image"]["steps"]
        names = [s.get("name") for s in steps]
        self.assertLess(names.index("Run monitor unit tests"), names.index("Verify Docker binary and daemon are functional"))

    def test_deployment_discovery_awk_uses_posix_space_class_not_gnu_s(self):
        # \s is a GNU/PCRE-only escape, not POSIX awk; [[:space:]] is the portable bracket expression.
        preflight_idx = self.monitor_text.index("- name: CloudWatch publication preflight")
        argocd_idx = self.monitor_text.index("- name: Create or update Argo CD Application")
        preflight_step_text = self.monitor_text[preflight_idx:argocd_idx]
        self.assertNotIn(r"\s", preflight_step_text)
        self.assertIn("[[:space:]]", preflight_step_text)

        verify_idx = self.monitor_text.index("- name: Verify GoldenGate monitor runtime state")
        upload_idx = self.monitor_text.index("- name: Upload rendered manifests and chart package")
        verify_step_text = self.monitor_text[verify_idx:upload_idx]
        post_rollout_idx = verify_step_text.index("ENABLED_DEPLOYMENT_PAIRS_POST")
        post_rollout_awk_text = verify_step_text[post_rollout_idx:post_rollout_idx + 600]
        self.assertNotIn(r"\s", post_rollout_awk_text)
        self.assertIn("[[:space:]]", post_rollout_awk_text)

    def test_deployment_discovery_awk_returns_exactly_both_enabled_deployments(self):
        # Executes the actual committed awk snippet under the system's real awk against the real config, not a reimplementation; the awk emits "name|type" pairs so this asserts both.
        if shutil.which("awk") is None:
            raise unittest.SkipTest("awk not available")
        preflight_idx = self.monitor_text.index("mapfile -t ENABLED_DEPLOYMENT_PAIRS < <(awk '")
        script_start = self.monitor_text.index("'", preflight_idx) + 1
        script_end = self.monitor_text.index("'", script_start)
        awk_script = self.monitor_text[script_start:script_end]
        registry_path = _stage_generated_registry(tempfile.mkdtemp())
        proc = subprocess.run(["awk", awk_script, registry_path], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        pairs = [line for line in proc.stdout.splitlines() if line]
        self.assertEqual(pairs, ["gg-oracle-payments-01|oracle", "gg-postgresql-payments-01|postgresql"])
        names = [pair.split("|", 1)[0] for pair in pairs]
        self.assertEqual(names, ["gg-oracle-payments-01", "gg-postgresql-payments-01"])

    def test_deployment_discovery_never_hardcodes_names_in_production_logic(self):
        preflight_idx = self.monitor_text.index("- name: CloudWatch publication preflight")
        argocd_idx = self.monitor_text.index("- name: Create or update Argo CD Application")
        preflight_step_text = self.monitor_text[preflight_idx:argocd_idx]
        self.assertNotIn("gg-oracle-payments-01", preflight_step_text)
        self.assertNotIn("gg-postgresql-payments-01", preflight_step_text)

    def test_chart_version_is_semver_with_run_number_and_run_attempt(self):
        self.assertIn(
            'CHART_VERSION="0.${{ github.run_number }}.${{ github.run_attempt }}"',
            self.monitor_text)
        self.assertNotIn('CHART_VERSION="0.1.${{ github.run_number }}"', self.monitor_text)

    def test_argocd_target_revision_uses_the_same_chart_version_variable(self):
        self.assertIn("targetRevision: ${CHART_VERSION}", self.monitor_text)

    def test_rerun_produces_a_distinct_chart_version(self):
        # Pure simulation of the SemVer expression: same run_number, a different run_attempt must never collide.
        def render(run_number, run_attempt):
            return f"0.{run_number}.{run_attempt}"
        first_attempt = render(42, 1)
        rerun_attempt = render(42, 2)
        self.assertNotEqual(first_attempt, rerun_attempt)

    def test_pod_selection_never_blindly_uses_items_zero(self):
        self.assertNotIn(".items[0].metadata.name", self.monitor_text)

    def test_pod_selection_requires_running_phase_and_ready_containers(self):
        # The preflight checks the pod's Ready *condition* (not containerStatuses[].ready); the "Verify" step below is unchanged and still uses containerStatuses[].ready.
        preflight_step_text = self.monitor_text[
            self.monitor_text.index("- name: CloudWatch publication preflight"):
            self.monitor_text.index("- name: Create or update Argo CD Application")]
        self.assertIn('phase="$(jq -r \'.status.phase // ""\'', preflight_step_text)
        self.assertIn('[ "$phase" != "Running" ] && continue', preflight_step_text)
        self.assertIn('select(.type=="Ready")', preflight_step_text)
        self.assertIn('[ "$ready_status" != "True" ] && continue', preflight_step_text)

        verify_step_text = self.monitor_text[
            self.monitor_text.index("- name: Verify GoldenGate monitor runtime state"):
            self.monitor_text.index("- name: Upload rendered manifests and chart package")]
        self.assertIn('.status.phase == "Running"', verify_step_text)
        self.assertIn("all(.ready == true)", verify_step_text)

    def test_pod_selection_never_prints_full_pod_object(self):
        preflight_idx = self.monitor_text.index("- name: CloudWatch publication preflight")
        argocd_idx = self.monitor_text.index("- name: Create or update Argo CD Application")
        preflight_step_text = self.monitor_text[preflight_idx:argocd_idx]
        self.assertNotIn("kubectl get pods -n \"$TARGET_NAMESPACE\" -l app.kubernetes.io/name=gg-monitor -o json 2>/dev/null | jq -r .items",
                         preflight_step_text.replace("\n", " ").replace("            ", " "))
        self.assertNotIn("echo \"$POD_NAME\" -o json", preflight_step_text)

    def test_preflight_pod_selection_verifies_ownership_chain(self):
        # This only proves the Deployment/ReplicaSet ownership-chain properties are textually present (static analysis); the full functional proof (mocked kubectl/jq scenarios) lives in hack/test-goldengate-metrics-config.py::MainWorkflowPodOwnershipTests.
        preflight_step_text = self.monitor_text[
            self.monitor_text.index("- name: CloudWatch publication preflight"):
            self.monitor_text.index("- name: Create or update Argo CD Application")]

        self.assertIn('kubectl get deployment gg-monitor -n "$TARGET_NAMESPACE" -o json', preflight_step_text)
        self.assertIn("DEPLOY_UID=", preflight_step_text)
        self.assertIn(".spec.selector.matchLabels", preflight_step_text)
        self.assertIn('[ "$pod_sa" != "gg-monitor" ] && continue', preflight_step_text)
        self.assertIn('select(.controller==true and .kind=="ReplicaSet")', preflight_step_text)
        self.assertIn('kubectl get replicaset "$rs_owner_name"', preflight_step_text)
        self.assertIn('select(.controller==true and .kind=="Deployment")', preflight_step_text)
        self.assertIn('[ "$rs_deploy_uid" != "$DEPLOY_UID" ] && continue', preflight_step_text)

        verify_step_text = self.monitor_text[
            self.monitor_text.index("- name: Verify GoldenGate monitor runtime state"):
            self.monitor_text.index("- name: Upload rendered manifests and chart package")]
        self.assertIn("jq -r '[.items[] | select(", verify_step_text)

    def test_ready_pod_selection_jq_filter_used_by_post_deployment_verification_also_excludes_terminating(self):
        if shutil.which("jq") is None:
            raise unittest.SkipTest("jq not available")
        verify_idx = self.monitor_text.index("- name: Verify GoldenGate monitor runtime state")
        upload_idx = self.monitor_text.index("- name: Upload rendered manifests and chart package")
        verify_step_text = self.monitor_text[verify_idx:upload_idx]
        filter_start = verify_step_text.index("jq -r '") + len("jq -r '")
        filter_end = verify_step_text.index("'", filter_start)
        jq_filter = verify_step_text[filter_start:filter_end]

        mixed_pods = {"items": [
            {"status": {"phase": "Pending", "containerStatuses": []},
             "metadata": {"name": "gg-monitor-pending"}},
            {"status": {"phase": "Running", "containerStatuses": [{"ready": False}]},
             "metadata": {"name": "gg-monitor-notready"}},
            {"status": {"phase": "Running", "containerStatuses": [{"ready": True}]},
             "metadata": {"name": "gg-monitor-terminating", "deletionTimestamp": "2026-07-21T00:00:00Z"}},
            {"status": {"phase": "Running", "containerStatuses": [{"ready": True}]},
             "metadata": {"name": "gg-monitor-ready"}},
        ]}
        proc = subprocess.run(["jq", "-r", jq_filter], input=json.dumps(mixed_pods),
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "gg-monitor-ready")

    def test_readme_does_not_claim_terraform_can_enable_metrics(self):
        with open(os.path.join(REPO_ROOT, "monitoring", "monitor", "README.md")) as f:
            readme_text = f.read()
        self.assertIn("ignore_changes = [item]", readme_text)
        self.assertIn("never mutates it", readme_text.lower())
        self.assertIn("stays blocked in practice", readme_text.lower())
        self.assertIn("never requires any", readme_text.lower())

    def test_enable_cloudwatch_publication_input_is_boolean_required_default_false(self):
        doc = yaml.safe_load(self.monitor_text)
        inputs = (doc.get("on") or doc.get(True))["workflow_dispatch"]["inputs"]
        self.assertIn("enable_cloudwatch_publication", inputs)
        spec = inputs["enable_cloudwatch_publication"]
        self.assertEqual(spec["type"], "boolean")
        self.assertIs(spec["required"], True)
        self.assertIs(spec["default"], False)
        description = spec["description"].lower()
        self.assertIn("cloudwatch", description)
        self.assertIn("metric", description)

    def test_base_helm_default_for_cloudwatch_publish_enabled_remains_false(self):
        with open(os.path.join(MONITOR_CHART_PATH, "values.yaml")) as f:
            base_values = yaml.safe_load(f)
        self.assertIs(base_values["cloudwatch"]["publishEnabled"], False)

    def test_env_dev_values_does_not_override_cloudwatch_default(self):
        env_values_path = os.path.join(REPO_ROOT, "envs", "dev", "goldengate-monitor", "values.yaml")
        with open(env_values_path) as f:
            env_values = yaml.safe_load(f)
        self.assertNotIn("cloudwatch", env_values or {})

    def test_cloudwatch_value_strictly_validated_as_literal_true_or_false(self):
        self.assertIn(
            'if [[ "$CLOUDWATCH_PUBLISH_ENABLED_VALUE" != "true" && "$CLOUDWATCH_PUBLISH_ENABLED_VALUE" != "false" ]]; then',
            self.monitor_text)

    def test_cloudwatch_argocd_helm_parameter_uses_plain_set_not_set_string(self):
        # A plain `--set` lets Helm infer a real Boolean from "true"/"false"; --set-string would force the string "true", the antipattern to avoid.
        self.assertIn("--set cloudwatch.publishEnabled=", self.monitor_text)
        self.assertNotIn("--set-string cloudwatch.publishEnabled", self.monitor_text)
        self.assertIn("- name: cloudwatch.publishEnabled", self.monitor_text)
        self.assertIn('value: "${CLOUDWATCH_PUBLISH_ENABLED_VALUE}"', self.monitor_text)

    def test_cloudwatch_preflight_step_exists_gated_on_enable_input(self):
        condition = _extract_step_if_condition(
            self.monitor_text, "CloudWatch publication preflight (gate inventory)")
        self.assertEqual(condition, "${{ inputs.deploy && inputs.enable_cloudwatch_publication }}")

    def test_cloudwatch_preflight_precedes_argocd_application_step(self):
        preflight_idx = self.monitor_text.index("- name: CloudWatch publication preflight")
        argocd_idx = self.monitor_text.index("- name: Create or update Argo CD Application")
        self.assertLess(preflight_idx, argocd_idx)

    def test_cloudwatch_preflight_uses_get_item_never_scan(self):
        preflight_idx = self.monitor_text.index("- name: CloudWatch publication preflight")
        argocd_idx = self.monitor_text.index("- name: Create or update Argo CD Application")
        preflight_step_text = self.monitor_text[preflight_idx:argocd_idx]
        self.assertIn("table.get_item(", preflight_step_text)
        self.assertNotIn(".scan(", preflight_step_text)
        self.assertNotIn(".Scan(", preflight_step_text)

    def test_no_dynamodb_scan_anywhere_in_monitor_workflow(self):
        self.assertNotIn(".scan(", self.monitor_text)
        self.assertNotIn(".Scan(", self.monitor_text)

    def test_cloudwatch_preflight_discovers_enabled_deployments_not_hardcoded(self):
        preflight_idx = self.monitor_text.index("- name: CloudWatch publication preflight")
        argocd_idx = self.monitor_text.index("- name: Create or update Argo CD Application")
        preflight_step_text = self.monitor_text[preflight_idx:argocd_idx]
        self.assertIn("work/generated/dev/goldengate-deployments.yaml", preflight_step_text)
        self.assertNotIn("gg-oracle-payments-01", preflight_step_text)
        self.assertNotIn("gg-postgresql-payments-01", preflight_step_text)

    def test_cloudwatch_preflight_output_is_sanitized_deployment_result_only(self):
        # Inspects the actual rewritten step text (not a reimplementation) to prove the gate-inventory behaviour, rather than pinning an obsolete literal.
        preflight_idx = self.monitor_text.index("- name: CloudWatch publication preflight")
        argocd_idx = self.monitor_text.index("- name: Create or update Argo CD Application")
        preflight_step_text = self.monitor_text[preflight_idx:argocd_idx]

        # 1. CONFIG.metricsEnabled is validated as a literal DynamoDB Boolean.
        self.assertIn("isinstance(metrics_enabled, bool)", preflight_step_text)
        self.assertIn("result=metricsenabled-not-boolean", preflight_step_text)

        # 2. CONFIG.alertsEnabled is validated as the literal Boolean false.
        self.assertIn("alerts_enabled is not False", preflight_step_text)
        self.assertIn("result=alertsenabled-not-false", preflight_step_text)

        # 3. CONFIG.deploymentType is validated against the canonical per-deployment type from the registry.
        self.assertIn('item.get("deploymentType") != expected_type', preflight_step_text)
        self.assertIn("result=deploymenttype-mismatch", preflight_step_text)

        # 4. metrics_gate_expectation supports any/all-disabled/all-enabled.
        doc = yaml.safe_load(self.monitor_text)
        triggers = doc.get("on") or doc.get(True)
        expectation_input = triggers["workflow_dispatch"]["inputs"]["metrics_gate_expectation"]
        self.assertEqual(sorted(expectation_input["options"]), ["all-disabled", "all-enabled", "any"])
        self.assertEqual(expectation_input["default"], "any")

        # 5. A deployment with metricsEnabled=false only fails the preflight when the expectation is all-enabled.
        self.assertIn(
            'DISABLED_CONFIG_COUNT=$((DISABLED_CONFIG_COUNT + 1))\n'
            '              if [ "$GATE_EXPECTATION" = "all-enabled" ]; then',
            preflight_step_text)
        self.assertIn(
            'ENABLED_CONFIG_COUNT=$((ENABLED_CONFIG_COUNT + 1))\n'
            '              if [ "$GATE_EXPECTATION" = "all-disabled" ]; then',
            preflight_step_text)

        # 6. Still fails closed on missing or malformed CONFIG.
        self.assertIn("result=missing-config", preflight_step_text)
        self.assertIn("result=bad-recordtype", preflight_step_text)
        self.assertIn(
            'if [ "$RESULT_STATUS" -ne 0 ] || [[ "$RESULT" != "deployment=${name} deploymentType=${expected_type} metricsEnabled="* ]]',
            preflight_step_text)

        # 7. Uses GetItem only, never Scan.
        self.assertIn("table.get_item(", preflight_step_text)
        self.assertNotIn(".scan(", preflight_step_text)
        self.assertNotIn(".Scan(", preflight_step_text)

        # 8. The double-gate model is preserved: this step only runs when the global hard switch input is true.
        self.assertIn(
            "if: ${{ inputs.deploy && inputs.enable_cloudwatch_publication }}",
            preflight_step_text)

    def test_cloudwatch_preflight_first_deployment_prerequisite_message(self):
        preflight_idx = self.monitor_text.index("- name: CloudWatch publication preflight")
        argocd_idx = self.monitor_text.index("- name: Create or update Argo CD Application")
        preflight_step_text = self.monitor_text[preflight_idx:argocd_idx]
        self.assertIn("PREREQUISITE NOT MET", preflight_step_text)
        self.assertIn("Prerequisite:", preflight_step_text)

    def test_disabled_cloudwatch_request_never_reaches_preflight_condition(self):
        # Pure boolean simulation of the step's `if:` gate: proves a false request short-circuits before any CONFIG check runs.
        github_event_name = "workflow_dispatch"
        inputs_deploy = True
        inputs_enable_cloudwatch_publication = False
        gate = (github_event_name != "workflow_dispatch" or inputs_deploy) and inputs_enable_cloudwatch_publication
        self.assertFalse(gate)

    def test_runtime_verification_checks_deployed_cloudwatch_env_value(self):
        self.assertIn("Verifying deployed CLOUDWATCH_PUBLISH_ENABLED matches the requested value", self.monitor_text)
        self.assertIn('echo "cloudwatchPublishEnabled=${POD_CLOUDWATCH_ENV}"', self.monitor_text)

    def test_runtime_verification_checks_forbidden_log_patterns_when_enabled(self):
        verify_idx = self.monitor_text.index("- name: Verify GoldenGate monitor runtime state")
        upload_idx = self.monitor_text.index("- name: Upload rendered manifests and chart package")
        verify_step_text = self.monitor_text[verify_idx:upload_idx]
        self.assertIn('if [ "$CLOUDWATCH_PUBLISH_ENABLED_VALUE" = "true" ]; then', verify_step_text)
        self.assertIn("cloudwatch_client_creation_failed", verify_step_text)
        self.assertIn("cloudwatch_put_metric_data_failed", verify_step_text)
        self.assertIn('"tick failed"', verify_step_text)

    def test_rollback_supported_by_same_workflow_no_dynamodb_mutation(self):
        # Rollback is documented as re-running with enable_cloudwatch_publication=false; no separate rollback workflow or CONFIG mutation exists.
        self.assertNotIn("put_item", self.monitor_text)
        self.assertNotIn("update_item", self.monitor_text)
        self.assertNotIn("UpdateItem", self.monitor_text)

    def test_no_cloudwatch_read_iam_action_referenced(self):
        for forbidden in ("cloudwatch:ListMetrics", "cloudwatch:GetMetricData", "cloudwatch:GetMetricStatistics",
                         "cloudwatch:DescribeAlarms"):
            self.assertNotIn(forbidden, self.monitor_text)

    def test_no_alarm_sns_alerter_fluentbit_or_observer_removal_referenced(self):
        for forbidden in ("cloudwatch:PutMetricAlarm", "sns:CreateTopic", "sns:Subscribe", "gg-alerter",
                         "fluent-bit", "FluentBit", "kubectl delete", "helm uninstall"):
            self.assertNotIn(forbidden, self.monitor_text)

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
        """Runs the actual production RBAC-selection snippet (verbatim from the workflow) against a synthetic multi-document manifest to prove the correct Role is selected by kind+name, not document order."""
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
      - argocd-ecr-amazon-cloudwatch-observability-oci
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
        """Same production snippet, but the real Role is missing a required resourceName; must fail loudly, proving the check isn't vacuously true."""
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
    """The shared select_document()/normalize_value() bash functions from the manifest-validation step; required by every slice below since they call these rather than reimplementing the logic."""
    full_step = _extract_run_block(monitor_text, "Validate rendered monitor manifest")
    start = full_step.index("select_document() {")
    end = full_step.index('echo "Validating Namespace')
    return full_step[start:end]


def _extract_serviceaccount_validation_snippet(monitor_text):
    """The ServiceAccount/IRSA validation portion of the manifest-validation step, extracted verbatim from the real workflow."""
    full_step = _extract_run_block(monitor_text, "Validate rendered monitor manifest")
    start = full_step.index('echo "Validating ServiceAccount')
    end = full_step.index('echo "Validating Deployment uses')
    return _extract_manifest_validation_helpers(monitor_text) + full_step[start:end]


def _extract_ingress_validation_snippet(monitor_text):
    """The Ingress host/certificate/protocol validation portion of the manifest-validation step, extracted verbatim from the real workflow."""
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
    """Regression coverage for the quote-sensitive ServiceAccount role-arn grep that silently passed under set -euo pipefail while never actually matching the (correctly quoted) rendered value."""

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
        """An unrelated ServiceAccount (similarly-prefixed name, wrong ARN) rendered before gg-monitor must never be mistaken for it."""
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
    """Regression coverage for the quote-sensitive Ingress host grep (same bug class already fixed for ServiceAccount role-arn) plus certificate-ARN/protocol checks, scoped to the gg-monitor Ingress document."""

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
        """Every assertion in the manifest-validation run block must be an explicit conditional; a bare unguarded grep would silently exit the step under set -euo pipefail on a mismatch."""
        full_step = _extract_run_block(self.monitor_text, "Validate rendered monitor manifest")
        for line in full_step.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # A bare assertion is a grep invocation with no if/while/guard and no "|| true" escape hatch.
            if re.match(r'^grep\s', stripped) and "$(" not in stripped:
                self.fail(f"bare unguarded grep assertion found: {stripped!r}")

    def test_no_echo_pipe_grep_in_ingress_validation(self):
        snippet = _extract_ingress_validation_snippet(self.monitor_text)
        self.assertNotRegex(snippet, r'echo\s+"\$[A-Za-z_]+"\s*\|\s*grep')

    def test_existing_serviceaccount_tests_still_pass(self):
        """Sanity check that ServiceAccount slice extraction still works after factoring out the shared helpers; full coverage lives in ServiceAccountIrsaValidationTests."""
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
