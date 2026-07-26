import html as html_module
import json
import os
import sys
import unittest
from decimal import Decimal
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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
        "PIPELINES": "gg-payments-ora-to-pg-001-source,gg-payments-ora-to-pg-001-target",
    }
    env.update(overrides)
    return monitor.load_config(env)


def make_item(**overrides):
    item = {
        "pipeline": "gg-payments-ora-to-pg-001-source",
        "recordType": "STATE#_deployment",
        "deploymentId": "payments-ora-to-pg-001",
        "component": "source",
        "engine": "oracle",
        "status": "HEALTHY",
        "adminEndpointHealthy": True,
        "metricsEndpointHealthy": True,
        "u02Mounted": True,
        "u02TotalBytes": Decimal("1000000"),
        "u02FreeBytes": Decimal("500000"),
        "u02UsedPercent": Decimal("50.00"),
        "podName": "ogg-oracle-0",
        "namespace": "gg-dev-payments-ora-to-pg-001",
        "recordedAt": 1780000000,
        "observerVersion": "obs-abc123456789",
        "errorSummary": None,
    }
    item.update(overrides)
    return item


class ConfigValidationTests(unittest.TestCase):
    def test_valid_configuration(self):
        config = make_config()
        self.assertEqual(config.aws_region, "eu-west-1")
        self.assertEqual(config.dynamodb_table, "gg-eks-pipeline")
        self.assertEqual(
            config.pipelines,
            ("gg-payments-ora-to-pg-001-source", "gg-payments-ora-to-pg-001-target"),
        )
        self.assertEqual(config.port, 8080)
        self.assertEqual(config.stale_after_seconds, 120)
        self.assertEqual(config.refresh_seconds, 30)

    def test_missing_aws_region(self):
        with self.assertRaises(monitor.ConfigError):
            monitor.load_config({
                "DYNAMODB_TABLE": "gg-eks-pipeline",
                "PIPELINES": "gg-payments-ora-to-pg-001-source",
            })

    def test_empty_pipeline_list(self):
        with self.assertRaises(monitor.ConfigError):
            make_config(PIPELINES="")
        with self.assertRaises(monitor.ConfigError):
            make_config(PIPELINES="   ,  ,")

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

    def test_pipelines_are_deduplicated_and_trimmed(self):
        config = make_config(PIPELINES=" gg-a-source , gg-a-source ,gg-b-target")
        self.assertEqual(config.pipelines, ("gg-a-source", "gg-b-target"))


class EffectiveStatusTests(unittest.TestCase):
    def test_fresh_healthy_record(self):
        item = make_item(status="HEALTHY", recordedAt=1780000000)
        status, age, recorded_at = monitor.compute_effective_status(item, now=1780000010, stale_after_seconds=120)
        self.assertEqual(status, "HEALTHY")
        self.assertEqual(age, 10)
        self.assertEqual(recorded_at, 1780000000)

    def test_fresh_degraded_record(self):
        item = make_item(status="DEGRADED", recordedAt=1780000000)
        status, _, _ = monitor.compute_effective_status(item, now=1780000010, stale_after_seconds=120)
        self.assertEqual(status, "DEGRADED")

    def test_fresh_down_record(self):
        item = make_item(status="DOWN", recordedAt=1780000000)
        status, _, _ = monitor.compute_effective_status(item, now=1780000010, stale_after_seconds=120)
        self.assertEqual(status, "DOWN")

    def test_stale_record_regardless_of_raw_status(self):
        item = make_item(status="HEALTHY", recordedAt=1780000000)
        status, age, _ = monitor.compute_effective_status(item, now=1780000000 + 121, stale_after_seconds=120)
        self.assertEqual(status, "STALE")
        self.assertEqual(age, 121)

    def test_missing_record(self):
        status, age, recorded_at = monitor.compute_effective_status(None, now=1780000000, stale_after_seconds=120)
        self.assertEqual(status, "MISSING")
        self.assertIsNone(age)
        self.assertIsNone(recorded_at)

    def test_unknown_raw_status(self):
        item = make_item(status="SOMETHING_ELSE", recordedAt=1780000000)
        status, _, _ = monitor.compute_effective_status(item, now=1780000010, stale_after_seconds=120)
        self.assertEqual(status, "UNKNOWN")


class GroupingAndSeverityTests(unittest.TestCase):
    def test_source_and_target_grouped_by_deployment_id(self):
        source = monitor.build_pipeline_status(
            "gg-payments-ora-to-pg-001-source",
            make_item(component="source", engine="oracle"),
            now=1780000010,
            stale_after_seconds=120,
        )
        target = monitor.build_pipeline_status(
            "gg-payments-ora-to-pg-001-target",
            make_item(
                pipeline="gg-payments-ora-to-pg-001-target",
                component="target",
                engine="postgresql",
                podName="ogg-postgresql-0",
            ),
            now=1780000010,
            stale_after_seconds=120,
        )
        deployments = monitor.group_by_deployment([source, target])
        self.assertEqual(len(deployments), 1)
        self.assertEqual(deployments[0]["deploymentId"], "payments-ora-to-pg-001")
        components = {c["component"] for c in deployments[0]["components"]}
        self.assertEqual(components, {"source", "target"})

    def test_deployment_level_severity_precedence(self):
        healthy = {"deploymentId": "d1", "effectiveStatus": "HEALTHY"}
        degraded = {"deploymentId": "d1", "effectiveStatus": "DEGRADED"}
        down = {"deploymentId": "d1", "effectiveStatus": "DOWN"}
        missing = {"deploymentId": "d1", "effectiveStatus": "MISSING"}

        deployments = monitor.group_by_deployment([healthy, degraded])
        self.assertEqual(deployments[0]["overallStatus"], "DEGRADED")

        deployments = monitor.group_by_deployment([healthy, down])
        self.assertEqual(deployments[0]["overallStatus"], "DOWN")

        deployments = monitor.group_by_deployment([degraded, missing])
        self.assertEqual(deployments[0]["overallStatus"], "MISSING")

        # A deployment must never be reported healthy when one configured
        # component is missing or stale.
        deployments = monitor.group_by_deployment([healthy, missing])
        self.assertNotEqual(deployments[0]["overallStatus"], "HEALTHY")


class DecimalConversionTests(unittest.TestCase):
    def test_integral_decimal_becomes_int(self):
        self.assertEqual(monitor.decimal_to_jsonsafe(Decimal("500000")), 500000)
        self.assertIsInstance(monitor.decimal_to_jsonsafe(Decimal("500000")), int)

    def test_fractional_decimal_becomes_float(self):
        self.assertEqual(monitor.decimal_to_jsonsafe(Decimal("50.25")), 50.25)
        self.assertIsInstance(monitor.decimal_to_jsonsafe(Decimal("50.25")), float)

    def test_non_decimal_passthrough(self):
        self.assertEqual(monitor.decimal_to_jsonsafe("HEALTHY"), "HEALTHY")
        self.assertEqual(monitor.decimal_to_jsonsafe(True), True)

    def test_json_default_serializes_decimal(self):
        payload = {"value": Decimal("12.50")}
        encoded = json.dumps(payload, default=monitor._json_default)
        self.assertEqual(json.loads(encoded)["value"], 12.5)


class NullErrorSummaryTests(unittest.TestCase):
    def test_null_error_summary_round_trips_as_none(self):
        row = monitor.build_pipeline_status(
            "gg-payments-ora-to-pg-001-source",
            make_item(errorSummary=None),
            now=1780000010,
            stale_after_seconds=120,
        )
        self.assertIsNone(row["errorSummary"])


class HtmlEscapingTests(unittest.TestCase):
    def test_malicious_values_are_escaped_in_html(self):
        malicious = '<script>alert(1)</script>'
        row = monitor.build_pipeline_status(
            "gg-payments-ora-to-pg-001-source",
            make_item(podName=malicious, errorSummary=malicious, deploymentId=malicious),
            now=1780000010,
            stale_after_seconds=120,
        )
        payload = {
            "generatedAt": 1780000010,
            "staleAfterSeconds": 120,
            "deployments": monitor.group_by_deployment([row]),
        }
        config = make_config()
        rendered = monitor.render_html(payload, config)
        self.assertNotIn("<script>", rendered)
        self.assertIn(html_module.escape(malicious), rendered)


class ApiSchemaTests(unittest.TestCase):
    def test_api_json_schema(self):
        config = make_config()
        table = mock.Mock()
        table.get_item.side_effect = [
            {"Item": make_item()},
            {"Item": make_item(
                pipeline="gg-payments-ora-to-pg-001-target",
                component="target",
                engine="postgresql",
                podName="ogg-postgresql-0",
            )},
        ]
        payload = monitor.build_status_payload(config, table, clock=lambda: 1780000010)

        self.assertIn("generatedAt", payload)
        self.assertIn("staleAfterSeconds", payload)
        self.assertIn("deployments", payload)
        deployment = payload["deployments"][0]
        self.assertIn("deploymentId", deployment)
        self.assertIn("overallStatus", deployment)
        component = deployment["components"][0]
        for key in (
            "pipeline", "component", "engine", "observedStatus", "effectiveStatus",
            "fresh", "ageSeconds", "recordedAt", "adminEndpointHealthy",
            "metricsEndpointHealthy", "u02Mounted", "podName", "namespace",
            "observerVersion", "errorSummary",
        ):
            self.assertIn(key, component)

    def test_no_unknown_dynamodb_fields_in_output(self):
        item = make_item()
        item["someInternalAttribute"] = "should-not-appear"
        item["adminPassword"] = "should-never-appear"
        row = monitor.build_pipeline_status(
            "gg-payments-ora-to-pg-001-source", item, now=1780000010, stale_after_seconds=120
        )
        self.assertNotIn("someInternalAttribute", row)
        self.assertNotIn("adminPassword", row)

        config = make_config()
        payload = {"generatedAt": 1, "staleAfterSeconds": 120, "deployments": monitor.group_by_deployment([row])}
        rendered_html = monitor.render_html(payload, config)
        self.assertNotIn("someInternalAttribute", rendered_html)
        self.assertNotIn("should-never-appear", rendered_html)


class HealthAndReadyTests(unittest.TestCase):
    def test_healthz_returns_200_when_dynamodb_unavailable(self):
        config = make_config()
        table = mock.Mock()
        handler_cls = monitor._make_handler(config, table)

        request = mock.Mock()
        request.makefile.return_value = mock.Mock()
        handler = handler_cls.__new__(handler_cls)
        handler.path = "/healthz"
        writes = []
        handler._write = lambda status, ctype, body: writes.append((status, ctype, body))
        handler.do_GET()

        self.assertEqual(writes[0][0], 200)
        body = json.loads(writes[0][2])
        self.assertEqual(body["status"], "ok")
        table.get_item.assert_not_called()

    def test_readyz_returns_503_on_dynamodb_failure(self):
        from botocore.exceptions import ClientError

        config = make_config()
        table = mock.Mock()
        table.meta.client.describe_table.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "boom"}}, "DescribeTable"
        ) if _real_botocore() else ClientError()
        handler_cls = monitor._make_handler(config, table)

        handler = handler_cls.__new__(handler_cls)
        handler.path = "/readyz"
        writes = []
        handler._write = lambda status, ctype, body: writes.append((status, ctype, body))
        handler.do_GET()

        self.assertEqual(writes[0][0], 503)


def _real_botocore():
    try:
        import botocore  # noqa: F401
        return not isinstance(sys.modules.get("botocore"), mock.Mock)
    except ImportError:
        return False


class RootPageErrorBannerTests(unittest.TestCase):
    def test_root_page_shows_sanitized_banner_on_dynamodb_failure(self):
        config = make_config()
        table = mock.Mock()

        with mock.patch.object(
            monitor, "build_status_payload", side_effect=monitor.DynamoDbReadError("ClientError: boom")
        ):
            handler_cls = monitor._make_handler(config, table)
            handler = handler_cls.__new__(handler_cls)
            handler.path = "/"
            writes = []
            handler._write = lambda status, ctype, body: writes.append((status, ctype, body))
            handler.do_GET()

        self.assertEqual(writes[0][0], 200)
        body = writes[0][2].decode("utf-8")
        self.assertIn("Unable to read monitoring data", body)
        self.assertNotIn("Traceback", body)


class DynamoDbAccessPatternTests(unittest.TestCase):
    def test_get_item_uses_state_deployment_record_type(self):
        table = mock.Mock()
        table.get_item.return_value = {"Item": make_item()}
        monitor.get_pipeline_item(table, "gg-payments-ora-to-pg-001-source")

        _, kwargs = table.get_item.call_args
        self.assertEqual(kwargs["Key"]["recordType"], "STATE#_deployment")
        self.assertEqual(kwargs["Key"]["pipeline"], "gg-payments-ora-to-pg-001-source")

    def test_no_scan_call_occurs(self):
        config = make_config()
        table = mock.Mock()
        table.get_item.return_value = {"Item": make_item()}
        monitor.build_status_payload(config, table, clock=lambda: 1780000010)
        table.scan.assert_not_called()

    def test_no_dynamodb_write_operation_occurs(self):
        config = make_config()
        table = mock.Mock()
        table.get_item.return_value = {"Item": make_item()}
        monitor.build_status_payload(config, table, clock=lambda: 1780000010)
        table.put_item.assert_not_called()
        table.update_item.assert_not_called()
        table.delete_item.assert_not_called()
        table.batch_writer.assert_not_called()

    def test_dynamodb_read_failure_raises_read_error(self):
        from botocore.exceptions import ClientError

        config = make_config()
        table = mock.Mock()
        table.get_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "boom"}},
            "GetItem",
        ) if _real_botocore() else ClientError()

        with self.assertRaises(monitor.DynamoDbReadError):
            monitor.build_status_payload(config, table, clock=lambda: 1780000010)


if __name__ == "__main__":
    unittest.main()
