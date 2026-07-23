import os
import sys
import threading
import unittest
from decimal import Decimal
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# boto3/botocore are runtime dependencies (see requirements.txt) but are not
# required to run this unit-test suite: every test injects a mock DynamoDB
# table / CloudWatch client rather than exercising the real AWS SDK. Stub the
# imports only when the real packages are unavailable in the environment
# running the tests, so `import observer` succeeds either way.
try:
    import boto3  # noqa: F401
except ImportError:
    sys.modules["boto3"] = mock.MagicMock()

try:
    from botocore.config import Config  # noqa: F401
except ImportError:
    sys.modules["botocore"] = mock.MagicMock()
    sys.modules["botocore.config"] = mock.MagicMock()

import observer  # noqa: E402


def make_config(**overrides):
    env = {
        "AWS_REGION": "eu-west-1",
        "DYNAMODB_TABLE": "gg-eks-pipeline",
        "PIPELINE": "gg-payments-ora-to-pg-001-source",
        "DEPLOYMENT_ID": "payments-ora-to-pg-001",
        "COMPONENT": "source",
        "ENGINE": "oracle",
        "POD_NAME": "ogg-oracle-0",
        "POD_NAMESPACE": "gg-dev-payments-ora-to-pg-001",
    }
    env.update(overrides)
    return observer.load_config(env)


class StatusComputationTests(unittest.TestCase):
    def test_healthy_when_all_checks_succeed(self):
        self.assertEqual(observer.compute_status(True, True, True), "HEALTHY")

    def test_down_when_both_ports_fail(self):
        self.assertEqual(observer.compute_status(False, False, True), "DOWN")
        self.assertEqual(observer.compute_status(False, False, False), "DOWN")

    def test_degraded_for_partial_failure(self):
        self.assertEqual(observer.compute_status(True, False, True), "DEGRADED")
        self.assertEqual(observer.compute_status(False, True, True), "DEGRADED")
        self.assertEqual(observer.compute_status(True, True, False), "DEGRADED")


class U02StatsTests(unittest.TestCase):
    def test_byte_and_percentage_calculations(self):
        fake_statvfs = mock.Mock(f_frsize=4096, f_blocks=1000, f_bavail=250)
        stats = observer.get_u02_stats(
            "/u02", statvfs_func=lambda _p: fake_statvfs, isdir_func=lambda _p: True
        )
        total = 4096 * 1000
        free = 4096 * 250
        self.assertEqual(stats["totalBytes"], total)
        self.assertEqual(stats["freeBytes"], free)
        expected_percent = (Decimal(total - free) / Decimal(total) * Decimal(100)).quantize(Decimal("0.01"))
        self.assertEqual(stats["usedPercent"], expected_percent)
        self.assertIsInstance(stats["usedPercent"], Decimal)

    def test_missing_u02(self):
        stats = observer.get_u02_stats("/u02", isdir_func=lambda _p: False)
        self.assertIsNone(stats)

    def test_statvfs_oserror_treated_as_unavailable(self):
        def raiser(_p):
            raise OSError("boom")

        stats = observer.get_u02_stats("/u02", statvfs_func=raiser, isdir_func=lambda _p: True)
        self.assertIsNone(stats)


class DynamoDbItemTests(unittest.TestCase):
    def test_keys(self):
        config = make_config()
        item = observer.build_dynamodb_item(
            config=config, status="HEALTHY", admin_ok=True, metrics_ok=True,
            u02_stats=None, error_summary=None, recorded_at=1700000000,
        )
        self.assertEqual(item["pipeline"], "gg-payments-ora-to-pg-001-source")
        self.assertEqual(item["recordType"], "STATE#_deployment")

    def test_deployment_metadata(self):
        config = make_config()
        item = observer.build_dynamodb_item(
            config=config, status="DEGRADED", admin_ok=True, metrics_ok=False,
            u02_stats=None, error_summary="metrics_endpoint_unreachable",
            recorded_at=1700000000,
        )
        self.assertEqual(item["deploymentId"], "payments-ora-to-pg-001")
        self.assertEqual(item["component"], "source")
        self.assertEqual(item["engine"], "oracle")
        self.assertEqual(item["podName"], "ogg-oracle-0")
        self.assertEqual(item["namespace"], "gg-dev-payments-ora-to-pg-001")
        self.assertIs(item["adminEndpointHealthy"], True)
        self.assertIs(item["metricsEndpointHealthy"], False)
        self.assertEqual(item["recordedAt"], 1700000000)
        self.assertIsInstance(item["recordedAt"], int)

    def test_no_ttl_attribute(self):
        config = make_config()
        item = observer.build_dynamodb_item(
            config=config, status="HEALTHY", admin_ok=True, metrics_ok=True,
            u02_stats=None, error_summary=None, recorded_at=1700000000,
        )
        self.assertNotIn("ttl", item)

    def test_u02_used_percent_omitted_when_unavailable(self):
        config = make_config()
        item = observer.build_dynamodb_item(
            config=config, status="DEGRADED", admin_ok=True, metrics_ok=True,
            u02_stats=None, error_summary="u02_unavailable", recorded_at=1700000000,
        )
        self.assertNotIn("u02UsedPercent", item)
        self.assertNotIn("u02TotalBytes", item)
        self.assertNotIn("u02FreeBytes", item)
        self.assertIs(item["u02Mounted"], False)

    def test_update_item_uses_update_expression_only(self):
        table = mock.Mock()
        item = {
            "pipeline": "gg-payments-ora-to-pg-001-source",
            "recordType": "STATE#_deployment",
            "status": "HEALTHY",
        }
        observer.update_dynamodb_state(table, item)

        table.update_item.assert_called_once()
        table.scan.assert_not_called()
        table.delete_item.assert_not_called()
        table.batch_writer.assert_not_called()

        _, kwargs = table.update_item.call_args
        self.assertEqual(
            kwargs["Key"], {"pipeline": item["pipeline"], "recordType": item["recordType"]}
        )
        self.assertNotIn("pipeline", kwargs["ExpressionAttributeValues"].values())
        self.assertNotIn("REMOVE", kwargs["UpdateExpression"])

    def test_build_dynamodb_remove_attributes_when_u02_unavailable(self):
        self.assertEqual(
            sorted(observer.build_dynamodb_remove_attributes(None)),
            sorted(["u02TotalBytes", "u02FreeBytes", "u02UsedPercent"]),
        )

    def test_build_dynamodb_remove_attributes_when_used_percent_missing(self):
        stats = {"totalBytes": 100, "freeBytes": 100, "usedPercent": None}
        self.assertEqual(observer.build_dynamodb_remove_attributes(stats), ["u02UsedPercent"])

    def test_build_dynamodb_remove_attributes_when_fully_available(self):
        stats = {"totalBytes": 100, "freeBytes": 50, "usedPercent": Decimal("50.00")}
        self.assertEqual(observer.build_dynamodb_remove_attributes(stats), [])

    def test_update_item_removes_stale_u02_attributes_when_unavailable(self):
        table = mock.Mock()
        config = make_config()
        item = observer.build_dynamodb_item(
            config=config, status="DEGRADED", admin_ok=True, metrics_ok=True,
            u02_stats=None, error_summary="u02_unavailable", recorded_at=1700000000,
        )
        remove_attrs = observer.build_dynamodb_remove_attributes(None)

        observer.update_dynamodb_state(table, item, remove_attrs)

        _, kwargs = table.update_item.call_args
        self.assertIn("REMOVE", kwargs["UpdateExpression"])
        removed_names = set(kwargs["ExpressionAttributeNames"].values())
        for attr in ("u02TotalBytes", "u02FreeBytes", "u02UsedPercent"):
            self.assertIn(attr, removed_names)
            # A stale attribute can never also be set in the same request --
            # that's what would let old values survive.
            self.assertNotIn(attr, kwargs["ExpressionAttributeValues"].values())
        table.scan.assert_not_called()
        table.delete_item.assert_not_called()
        table.batch_writer.assert_not_called()
        table.put_item.assert_not_called()

    def test_update_item_removes_only_used_percent_when_total_free_present(self):
        table = mock.Mock()
        config = make_config()
        stats = {"totalBytes": 500, "freeBytes": 500, "usedPercent": None}
        item = observer.build_dynamodb_item(
            config=config, status="DEGRADED", admin_ok=True, metrics_ok=True,
            u02_stats=stats, error_summary=None, recorded_at=1700000000,
        )
        remove_attrs = observer.build_dynamodb_remove_attributes(stats)

        observer.update_dynamodb_state(table, item, remove_attrs)

        _, kwargs = table.update_item.call_args
        self.assertIn("REMOVE", kwargs["UpdateExpression"])
        self.assertIn("u02UsedPercent", kwargs["ExpressionAttributeNames"].values())
        # total/free must remain in SET, not REMOVE.
        self.assertIn(500, kwargs["ExpressionAttributeValues"].values())
        self.assertIn("u02TotalBytes", item)
        self.assertIn("u02FreeBytes", item)
        self.assertNotIn("u02UsedPercent", item)

    def test_update_item_no_remove_clause_when_stats_fully_available(self):
        table = mock.Mock()
        config = make_config()
        stats = {"totalBytes": 500, "freeBytes": 250, "usedPercent": Decimal("50.00")}
        item = observer.build_dynamodb_item(
            config=config, status="HEALTHY", admin_ok=True, metrics_ok=True,
            u02_stats=stats, error_summary=None, recorded_at=1700000000,
        )
        remove_attrs = observer.build_dynamodb_remove_attributes(stats)

        observer.update_dynamodb_state(table, item, remove_attrs)

        _, kwargs = table.update_item.call_args
        self.assertNotIn("REMOVE", kwargs["UpdateExpression"])


class CloudWatchMetricTests(unittest.TestCase):
    def test_namespace_used_on_publish(self):
        config = make_config(CLOUDWATCH_NAMESPACE="GoldenGate/Pipelines")
        cw_client = mock.Mock()
        metric_data = observer.build_cloudwatch_metric_data(
            config=config, status="HEALTHY", admin_ok=True, metrics_ok=True,
            u02_stats=None, timestamp=1700000000,
        )
        observer.publish_cloudwatch_metrics(cw_client, config, metric_data)

        cw_client.put_metric_data.assert_called_once()
        _, kwargs = cw_client.put_metric_data.call_args
        self.assertEqual(kwargs["Namespace"], "GoldenGate/Pipelines")

    def test_dimensions(self):
        config = make_config()
        metric_data = observer.build_cloudwatch_metric_data(
            config=config, status="HEALTHY", admin_ok=True, metrics_ok=True,
            u02_stats=None, timestamp=1700000000,
        )
        expected_dims = [
            {"Name": "Pipeline", "Value": config.pipeline},
            {"Name": "Component", "Value": config.component},
            {"Name": "Engine", "Value": config.engine},
        ]
        for metric in metric_data:
            self.assertEqual(metric["Dimensions"], expected_dims)

    def test_u02_used_percent_metric_omitted_when_unavailable(self):
        config = make_config()
        metric_data = observer.build_cloudwatch_metric_data(
            config=config, status="DEGRADED", admin_ok=True, metrics_ok=True,
            u02_stats=None, timestamp=1700000000,
        )
        names = [m["MetricName"] for m in metric_data]
        self.assertNotIn("U02UsedPercent", names)

    def test_u02_used_percent_metric_present_when_available(self):
        config = make_config()
        u02_stats = {"totalBytes": 100, "freeBytes": 50, "usedPercent": Decimal("50.00")}
        metric_data = observer.build_cloudwatch_metric_data(
            config=config, status="HEALTHY", admin_ok=True, metrics_ok=True,
            u02_stats=u02_stats, timestamp=1700000000,
        )
        names = [m["MetricName"] for m in metric_data]
        self.assertIn("U02UsedPercent", names)


class CycleResilienceTests(unittest.TestCase):
    def test_dynamodb_failure_does_not_prevent_cloudwatch_publish(self):
        config = make_config()
        table = mock.Mock()
        table.update_item.side_effect = RuntimeError("dynamodb unavailable")
        cw_client = mock.Mock()

        status = observer.run_cycle(
            config, table, cw_client,
            tcp_check_fn=lambda *a, **k: True,
            u02_stats_fn=lambda *_a, **_k: None,
        )

        self.assertEqual(status, "DEGRADED")
        cw_client.put_metric_data.assert_called_once()

    def test_cloudwatch_failure_does_not_stop_later_cycles(self):
        config = make_config()
        table = mock.Mock()
        cw_client = mock.Mock()
        cw_client.put_metric_data.side_effect = RuntimeError("cloudwatch unavailable")

        for _ in range(3):
            status = observer.run_cycle(
                config, table, cw_client,
                tcp_check_fn=lambda *a, **k: True,
                u02_stats_fn=lambda *_a, **_k: None,
            )
            self.assertEqual(status, "DEGRADED")

        self.assertEqual(table.update_item.call_count, 3)
        self.assertEqual(cw_client.put_metric_data.call_count, 3)


class ErrorSanitizationTests(unittest.TestCase):
    def test_sanitized_errors_have_no_traceback_or_newlines(self):
        try:
            raise ValueError("bad thing happened\nwith a traceback-looking line")
        except ValueError as exc:
            summary = observer.sanitize_error(exc)

        self.assertNotIn("\n", summary)
        self.assertIn("ValueError", summary)
        self.assertLessEqual(len(summary), 200)

    def test_build_error_summary_is_concise(self):
        summary = observer.build_error_summary(False, False, None)
        self.assertEqual(
            summary,
            "admin_endpoint_unreachable; metrics_endpoint_unreachable; u02_unavailable",
        )
        self.assertIsNone(observer.build_error_summary(True, True, {"totalBytes": 1}))


class FakeClock:
    """Deterministic, manually-advanced stand-in for time.monotonic."""

    def __init__(self, start=0.0):
        self._now = start

    def advance(self, seconds):
        self._now += seconds

    def __call__(self):
        return self._now


class HealthServerTests(unittest.TestCase):
    def test_healthy_recent_loop(self):
        clock = FakeClock()
        state = observer.HealthState("test-version", stale_threshold_seconds=60, clock=clock)
        state.mark_started()
        state.mark_progress(1700000000.0)

        clock.advance(5)
        body, status_code = state.snapshot()

        self.assertEqual(status_code, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["observerVersion"], "test-version")
        self.assertEqual(body["lastCycleAt"], 1700000000.0)
        # Only these three keys -- no configuration or credentials exposed.
        self.assertEqual(set(body.keys()), {"status", "observerVersion", "lastCycleAt"})

    def test_external_monitoring_failures_do_not_cause_staleness(self):
        # A DOWN/DEGRADED GoldenGate status, or DynamoDB/CloudWatch failures,
        # never reach HealthState at all -- mark_progress is called every
        # loop iteration regardless of run_cycle's internal outcome. This
        # test documents that /healthz only cares about progress, not status.
        clock = FakeClock()
        state = observer.HealthState("test-version", stale_threshold_seconds=60, clock=clock)
        state.mark_started()

        for i in range(5):
            clock.advance(10)
            state.mark_progress(1700000000.0 + i)
            body, status_code = state.snapshot()
            self.assertEqual(status_code, 200)
            self.assertEqual(body["status"], "ok")

    def test_stale_loop_returns_503(self):
        clock = FakeClock()
        state = observer.HealthState("test-version", stale_threshold_seconds=60, clock=clock)
        state.mark_started()
        state.mark_progress(1700000000.0)

        # Loop stops progressing -- simulate a deadlock by advancing well
        # past the threshold without another mark_progress call.
        clock.advance(120)
        body, status_code = state.snapshot()

        self.assertEqual(status_code, 503)
        self.assertEqual(body["status"], "stale")
        self.assertEqual(body["lastCycleAt"], 1700000000.0)

    def test_startup_behavior_before_first_cycle(self):
        clock = FakeClock()
        state = observer.HealthState("test-version", stale_threshold_seconds=60, clock=clock)

        # Before mark_started(): "starting", HTTP 200.
        body, status_code = state.snapshot()
        self.assertEqual(status_code, 200)
        self.assertEqual(body["status"], "starting")
        self.assertIsNone(body["lastCycleAt"])

        # After mark_started() but before the first mark_progress(), still
        # within the threshold window -- still "starting", not "stale".
        state.mark_started()
        clock.advance(5)
        body, status_code = state.snapshot()
        self.assertEqual(status_code, 200)
        self.assertEqual(body["status"], "starting")
        self.assertIsNone(body["lastCycleAt"])

    def test_startup_hang_eventually_reports_stale_not_starting_forever(self):
        # Regression test for the fixed startup-ordering bug: if something
        # hangs after mark_started() but before the first mark_progress()
        # (e.g. slow/blocked AWS client construction), /healthz must not
        # stay "starting"/200 indefinitely -- it must eventually flip to
        # "stale"/503 once the stale threshold elapses.
        clock = FakeClock()
        state = observer.HealthState("test-version", stale_threshold_seconds=60, clock=clock)
        state.mark_started()

        # Still within the grace window: starting.
        clock.advance(10)
        body, status_code = state.snapshot()
        self.assertEqual(status_code, 200)
        self.assertEqual(body["status"], "starting")

        # No mark_progress() ever happens (simulating a startup hang) --
        # past the threshold, this must become stale, not stay starting.
        clock.advance(120)
        body, status_code = state.snapshot()
        self.assertEqual(status_code, 503)
        self.assertEqual(body["status"], "stale")

    def test_compute_stale_threshold_has_a_floor(self):
        config = make_config(CHECK_INTERVAL_SECONDS="1", CONNECT_TIMEOUT_SECONDS="1")
        threshold = observer.compute_stale_threshold_seconds(config)
        self.assertGreaterEqual(threshold, observer.MIN_STALE_THRESHOLD_SECONDS)

    def test_compute_stale_threshold_scales_with_config(self):
        small = make_config(CHECK_INTERVAL_SECONDS="30", CONNECT_TIMEOUT_SECONDS="3")
        large = make_config(CHECK_INTERVAL_SECONDS="30", CONNECT_TIMEOUT_SECONDS="30")
        self.assertGreater(
            observer.compute_stale_threshold_seconds(large),
            observer.compute_stale_threshold_seconds(small),
        )


class ConfigValidationTests(unittest.TestCase):
    def test_missing_required_vars_raise_config_error(self):
        with self.assertRaises(observer.ConfigError):
            observer.load_config({})

    def test_invalid_component_raises_config_error(self):
        env = {
            "AWS_REGION": "eu-west-1",
            "DYNAMODB_TABLE": "gg-eks-pipeline",
            "PIPELINE": "gg-payments-ora-to-pg-001-source",
            "DEPLOYMENT_ID": "payments-ora-to-pg-001",
            "COMPONENT": "not-a-real-component",
            "ENGINE": "oracle",
            "POD_NAME": "ogg-oracle-0",
            "POD_NAMESPACE": "gg-dev-payments-ora-to-pg-001",
        }
        with self.assertRaises(observer.ConfigError):
            observer.load_config(env)

    def test_defaults_applied_for_optional_vars(self):
        config = make_config()
        self.assertEqual(config.admin_host, "127.0.0.1")
        self.assertEqual(config.admin_port, 8443)
        self.assertEqual(config.metrics_host, "127.0.0.1")
        self.assertEqual(config.metrics_port, 9015)
        self.assertEqual(config.u02_path, "/u02")
        self.assertEqual(config.check_interval_seconds, 30)
        self.assertEqual(config.cloudwatch_namespace, "GoldenGate/Pipelines")

    def test_admin_port_out_of_range_raises_config_error(self):
        with self.assertRaises(observer.ConfigError):
            make_config(ADMIN_PORT="0")
        with self.assertRaises(observer.ConfigError):
            make_config(ADMIN_PORT="65536")
        with self.assertRaises(observer.ConfigError):
            make_config(ADMIN_PORT="-1")

    def test_metrics_port_out_of_range_raises_config_error(self):
        with self.assertRaises(observer.ConfigError):
            make_config(METRICS_PORT="0")
        with self.assertRaises(observer.ConfigError):
            make_config(METRICS_PORT="70000")

    def test_health_listen_port_out_of_range_raises_config_error(self):
        with self.assertRaises(observer.ConfigError):
            make_config(HEALTH_LISTEN_PORT="0")
        with self.assertRaises(observer.ConfigError):
            make_config(HEALTH_LISTEN_PORT="99999")

    def test_check_interval_seconds_must_be_positive(self):
        with self.assertRaises(observer.ConfigError):
            make_config(CHECK_INTERVAL_SECONDS="0")
        with self.assertRaises(observer.ConfigError):
            make_config(CHECK_INTERVAL_SECONDS="-5")

    def test_connect_timeout_seconds_must_be_positive(self):
        with self.assertRaises(observer.ConfigError):
            make_config(CONNECT_TIMEOUT_SECONDS="0")
        with self.assertRaises(observer.ConfigError):
            make_config(CONNECT_TIMEOUT_SECONDS="-1")

    def test_valid_boundary_ports_are_accepted(self):
        config = make_config(ADMIN_PORT="1", METRICS_PORT="65535", HEALTH_LISTEN_PORT="8080")
        self.assertEqual(config.admin_port, 1)
        self.assertEqual(config.metrics_port, 65535)


class GracefulStopTests(unittest.TestCase):
    def test_stop_event_interrupts_wait_immediately(self):
        stop_event = threading.Event()

        def waiter():
            stop_event.wait(30)

        thread = threading.Thread(target=waiter)
        start = observer.time.time()
        thread.start()
        stop_event.set()
        thread.join(timeout=2)

        elapsed = observer.time.time() - start
        self.assertFalse(thread.is_alive())
        self.assertLess(elapsed, 2)


if __name__ == "__main__":
    unittest.main()
