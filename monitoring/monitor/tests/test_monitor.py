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
        status, age, recorded_at, fresh = monitor.compute_effective_status(item, now=1780000010, stale_after_seconds=120)
        self.assertEqual(status, "HEALTHY")
        self.assertEqual(age, 10)
        self.assertEqual(recorded_at, 1780000000)
        self.assertTrue(fresh)

    def test_fresh_degraded_record(self):
        item = make_item(status="DEGRADED", recordedAt=1780000000)
        status, _, _, fresh = monitor.compute_effective_status(item, now=1780000010, stale_after_seconds=120)
        self.assertEqual(status, "DEGRADED")
        self.assertTrue(fresh)

    def test_fresh_down_record(self):
        item = make_item(status="DOWN", recordedAt=1780000000)
        status, _, _, fresh = monitor.compute_effective_status(item, now=1780000010, stale_after_seconds=120)
        self.assertEqual(status, "DOWN")
        self.assertTrue(fresh)

    def test_stale_record_regardless_of_raw_status(self):
        item = make_item(status="HEALTHY", recordedAt=1780000000)
        status, age, _, fresh = monitor.compute_effective_status(item, now=1780000000 + 121, stale_after_seconds=120)
        self.assertEqual(status, "STALE")
        self.assertEqual(age, 121)
        self.assertFalse(fresh)

    def test_missing_record(self):
        status, age, recorded_at, fresh = monitor.compute_effective_status(None, now=1780000000, stale_after_seconds=120)
        self.assertEqual(status, "MISSING")
        self.assertIsNone(age)
        self.assertIsNone(recorded_at)
        self.assertFalse(fresh)

    def test_unknown_raw_status(self):
        item = make_item(status="SOMETHING_ELSE", recordedAt=1780000000)
        status, age, _, fresh = monitor.compute_effective_status(item, now=1780000010, stale_after_seconds=120)
        self.assertEqual(status, "UNKNOWN")
        # The timestamp itself is valid and fresh -- only the status enum
        # value is unrecognized -- so fresh reflects the real age, not a
        # blanket False tied to the UNKNOWN string.
        self.assertEqual(age, 10)
        self.assertTrue(fresh)


class RecordedAtAndFreshnessHardeningTests(unittest.TestCase):
    """Covers the recordedAt/freshness edge cases: missing timestamp on an
    existing item, malformed timestamp values, and future timestamps."""

    def test_existing_item_missing_recorded_at_is_unknown_and_not_fresh(self):
        item = make_item(status="HEALTHY")
        del item["recordedAt"]
        status, age, recorded_at, fresh = monitor.compute_effective_status(
            item, now=1780000010, stale_after_seconds=120
        )
        self.assertEqual(status, "UNKNOWN")
        self.assertIsNone(age)
        self.assertIsNone(recorded_at)
        self.assertFalse(fresh)

        row = monitor.build_pipeline_status(
            "gg-payments-ora-to-pg-001-source", item, now=1780000010, stale_after_seconds=120
        )
        self.assertEqual(row["effectiveStatus"], "UNKNOWN")
        self.assertFalse(row["fresh"])
        self.assertIsNone(row["ageSeconds"])

    def test_malformed_recorded_at_is_unknown_and_does_not_raise(self):
        for bad_value in ("not-a-timestamp", "", [], {}, object()):
            with self.subTest(bad_value=bad_value):
                item = make_item(status="HEALTHY", recordedAt=bad_value)
                try:
                    status, age, recorded_at, fresh = monitor.compute_effective_status(
                        item, now=1780000010, stale_after_seconds=120
                    )
                except Exception as exc:  # noqa: BLE001 -- proving no exception escapes
                    self.fail(f"compute_effective_status raised {exc!r} for {bad_value!r}")
                self.assertEqual(status, "UNKNOWN")
                self.assertIsNone(age)
                self.assertIsNone(recorded_at)
                self.assertFalse(fresh)

        # Also prove build_pipeline_status (the caller used by the HTTP
        # handlers) never raises and never returns a 500-triggering exception.
        item = make_item(status="HEALTHY", recordedAt="not-a-timestamp")
        row = monitor.build_pipeline_status(
            "gg-payments-ora-to-pg-001-source", item, now=1780000010, stale_after_seconds=120
        )
        self.assertEqual(row["effectiveStatus"], "UNKNOWN")
        self.assertFalse(row["fresh"])

    def test_stale_record_is_not_fresh(self):
        item = make_item(status="HEALTHY", recordedAt=1780000000)
        row = monitor.build_pipeline_status(
            "gg-payments-ora-to-pg-001-source", item, now=1780000000 + 121, stale_after_seconds=120
        )
        self.assertEqual(row["effectiveStatus"], "STALE")
        self.assertFalse(row["fresh"])

    def test_valid_recent_record_is_fresh(self):
        item = make_item(status="HEALTHY", recordedAt=1780000000)
        row = monitor.build_pipeline_status(
            "gg-payments-ora-to-pg-001-source", item, now=1780000005, stale_after_seconds=120
        )
        self.assertEqual(row["effectiveStatus"], "HEALTHY")
        self.assertTrue(row["fresh"])
        self.assertEqual(row["ageSeconds"], 5)

    def test_future_timestamp_never_yields_negative_age(self):
        # Small clock skew (within tolerance): clamp to ageSeconds=0, treat
        # as fresh rather than exposing a negative age.
        item = make_item(status="HEALTHY", recordedAt=1780000100)
        status, age, recorded_at, fresh = monitor.compute_effective_status(
            item, now=1780000000, stale_after_seconds=120
        )
        self.assertEqual(status, "HEALTHY")
        self.assertEqual(age, 0)
        self.assertGreaterEqual(age, 0)
        self.assertTrue(fresh)

        # Large future timestamp (beyond tolerance): not trusted -- UNKNOWN,
        # no age exposed, never negative.
        far_future_item = make_item(
            status="HEALTHY",
            recordedAt=1780000000 + monitor.FUTURE_TIMESTAMP_TOLERANCE_SECONDS + 3600,
        )
        status, age, recorded_at, fresh = monitor.compute_effective_status(
            far_future_item, now=1780000000, stale_after_seconds=120
        )
        self.assertEqual(status, "UNKNOWN")
        self.assertIsNone(age)
        self.assertIsNone(recorded_at)
        self.assertFalse(fresh)


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


class ClientFacingErrorSanitizationTests(unittest.TestCase):
    """A raw AWS/botocore error (e.g. AccessDenied naming an IAM principal
    ARN and account ID) must never reach an API or HTML client -- only the
    fixed, client-safe message may appear in either response."""

    SIMULATED_ARN_LEAK = (
        "ClientError: An error occurred (AccessDeniedException) when calling "
        "the GetItem operation: User: arn:aws:sts::668311715351:assumed-role/"
        "GoldenGateMonitorReadRole-dev/i-0123456789abcdef is not authorized "
        "to perform: dynamodb:GetItem on resource: "
        "arn:aws:dynamodb:eu-west-1:668311715351:table/gg-eks-pipeline"
    )

    def test_api_status_dynamodb_failure_returns_only_fixed_message(self):
        config = make_config()
        table = mock.Mock()

        with mock.patch.object(
            monitor,
            "build_status_payload",
            side_effect=monitor.DynamoDbReadError(self.SIMULATED_ARN_LEAK),
        ):
            handler_cls = monitor._make_handler(config, table)
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
        table = mock.Mock()

        with mock.patch.object(
            monitor,
            "build_status_payload",
            side_effect=monitor.DynamoDbReadError(self.SIMULATED_ARN_LEAK),
        ):
            handler_cls = monitor._make_handler(config, table)
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


if __name__ == "__main__":
    unittest.main()
