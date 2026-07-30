import json
import logging
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import boto3
from moto import mock_aws

import collector as core
import health_rules as gh


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


class LeaseManagerTests(unittest.TestCase):
    @mock_aws
    def test_acquire_and_renew(self):
        table = make_table()
        mgr = core.LeaseManager(table, "gg-oracle-payments-01", "gg-monitor-0", ttl=30)
        self.assertTrue(mgr.acquire())
        self.assertTrue(mgr.renew())

    @mock_aws
    def test_second_holder_cannot_acquire_active_lease(self):
        table = make_table()
        first = core.LeaseManager(table, "gg-oracle-payments-01", "gg-monitor-0", ttl=30)
        self.assertTrue(first.acquire())
        second = core.LeaseManager(table, "gg-oracle-payments-01", "gg-monitor-1", ttl=30)
        self.assertFalse(second.acquire())

    @mock_aws
    def test_renew_fails_once_lease_expired_and_taken_by_another(self):
        table = make_table()
        first = core.LeaseManager(table, "gg-oracle-payments-01", "gg-monitor-0", ttl=30,
                                  clock=lambda: 1000)
        self.assertTrue(first.acquire())
        second = core.LeaseManager(table, "gg-oracle-payments-01", "gg-monitor-1", ttl=30,
                                   clock=lambda: 2000)
        self.assertTrue(second.acquire())
        self.assertFalse(first.renew())


class WriteProcessStateTests(unittest.TestCase):
    @mock_aws
    def test_write_and_read_process_state(self):
        table = make_table()
        mgr = core.LeaseManager(table, "gg-oracle-payments-01", "gg-monitor-0", ttl=30)
        self.assertTrue(mgr.acquire())
        ok = core.write_process_state(
            table, mgr, "gg-oracle-payments-01", "oracle", "EXTORA1",
            {"status": "RUNNING", "recordedAt": 1000, "processType": "extract"},
            lambda: True)
        self.assertTrue(ok)
        row = core.read_process_state(table, "gg-oracle-payments-01", "EXTORA1")
        self.assertEqual(row["status"], "RUNNING")
        self.assertEqual(row["deploymentType"], "oracle")

    def test_write_refused_when_not_leader(self):
        table = MagicMock()
        mgr = MagicMock()
        ok = core.write_process_state(table, mgr, "p", "oracle", "X", {"status": "RUNNING"}, lambda: False)
        self.assertFalse(ok)
        table.update_item.assert_not_called()

    def test_write_refused_when_lease_renew_fails(self):
        table = MagicMock()
        mgr = MagicMock()
        mgr.renew.return_value = False
        ok = core.write_process_state(table, mgr, "p", "oracle", "X", {"status": "RUNNING"}, lambda: True)
        self.assertFalse(ok)
        table.update_item.assert_not_called()

    @mock_aws
    def test_config_never_written_by_collector(self):
        """Terraform owns CONFIG -- the collector module has no code path
        that writes it."""
        import inspect
        src = inspect.getsource(core)
        self.assertNotIn('"CONFIG"', src.replace('recordType": "CONFIG"', ""))

    def test_read_config_uses_get_item_only(self):
        table = MagicMock()
        table.get_item.return_value = {"Item": {"deploymentType": "oracle"}}
        core.read_config(table, "gg-oracle-payments-01")
        table.get_item.assert_called_once()
        table.scan.assert_not_called()


class CheckStaticPrerequisitesTests(unittest.TestCase):
    def test_missing_config_item_not_ready(self):
        table = MagicMock()
        table.get_item.return_value = {}
        deployment = {"name": "gg-oracle-payments-01", "type": "oracle"}
        with mock.patch.object(core, "_read_secret_file", return_value="secret"), \
             mock.patch.object(core, "_build_ssl_context"):
            ok, reason = core.check_static_prerequisites(deployment, table)
        self.assertFalse(ok)
        self.assertIn("CONFIG", reason)

    def test_never_calls_lease_apis(self):
        """check_static_prerequisites must not call acquire/renew -- an
        early test-acquire would desync LeaseState.is_leader()."""
        import inspect
        src = inspect.getsource(core.check_static_prerequisites)
        self.assertNotIn(".acquire(", src)
        self.assertNotIn(".renew(", src)


class PrerequisiteReasonSanitizationTests(unittest.TestCase):
    """check_static_prerequisites reasons -- and the warning run_pipeline
    logs from them on every retry -- must never carry a credential/CA path,
    secret value, or raw AWS exception. The canonical deployment name may
    appear."""

    DEPLOYMENT = {"name": "gg-oracle-payments-01", "type": "oracle"}
    SYNTHETIC_USER = "synthetic-test-oggadmin"
    SYNTHETIC_PASSWORD = "synthetic-test-P@ssw0rd!"
    FORBIDDEN_SUBSTRINGS = (
        "/mnt/secrets-store", "-admin-user", "-admin-password", "ca-chain-pem",
        SYNTHETIC_USER, SYNTHETIC_PASSWORD,
        "AccessDeniedException", "arn:aws:iam", "arn:aws:sts", "Traceback",
    )

    def _assert_reason_clean(self, reason):
        for forbidden in self.FORBIDDEN_SUBSTRINGS:
            self.assertNotIn(forbidden, reason)

    def test_missing_username_reason_is_generic(self):
        table = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            user_file = os.path.join(tmp, "does-not-exist-user")
            pwd_file = os.path.join(tmp, "pwd")
            with open(pwd_file, "w") as f:
                f.write(self.SYNTHETIC_PASSWORD)
            with mock.patch.object(core.cfgmod, "credential_paths", return_value=(user_file, pwd_file)):
                ok, reason = core.check_static_prerequisites(self.DEPLOYMENT, table)
        self.assertFalse(ok)
        self.assertEqual(reason, "admin username credential unavailable")
        self._assert_reason_clean(reason)

    def test_missing_password_reason_is_generic(self):
        table = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            user_file = os.path.join(tmp, "user")
            pwd_file = os.path.join(tmp, "does-not-exist-pwd")
            with open(user_file, "w") as f:
                f.write(self.SYNTHETIC_USER)
            with mock.patch.object(core.cfgmod, "credential_paths", return_value=(user_file, pwd_file)):
                ok, reason = core.check_static_prerequisites(self.DEPLOYMENT, table)
        self.assertFalse(ok)
        self.assertEqual(reason, "admin password credential unavailable")
        self._assert_reason_clean(reason)

    def test_missing_ca_reason_is_generic(self):
        table = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            user_file = os.path.join(tmp, "user")
            pwd_file = os.path.join(tmp, "pwd")
            with open(user_file, "w") as f:
                f.write(self.SYNTHETIC_USER)
            with open(pwd_file, "w") as f:
                f.write(self.SYNTHETIC_PASSWORD)
            with mock.patch.object(core.cfgmod, "credential_paths", return_value=(user_file, pwd_file)), \
                 mock.patch.object(core, "_build_ssl_context",
                                   side_effect=RuntimeError("CA_FILE '/mnt/secrets-store/ca-chain-pem' not found")):
                ok, reason = core.check_static_prerequisites(self.DEPLOYMENT, table)
        self.assertFalse(ok)
        self.assertEqual(reason, "TLS trust bundle unavailable")
        self._assert_reason_clean(reason)

    def test_dynamodb_config_exception_reason_is_generic(self):
        table = MagicMock()
        table.get_item.side_effect = RuntimeError(
            "AccessDeniedException: User: arn:aws:sts::668311715351:assumed-role/"
            "GoldenGateMonitorReadRole-dev/i-0123456789abcdef is not authorized")
        with tempfile.TemporaryDirectory() as tmp:
            user_file = os.path.join(tmp, "user")
            pwd_file = os.path.join(tmp, "pwd")
            with open(user_file, "w") as f:
                f.write(self.SYNTHETIC_USER)
            with open(pwd_file, "w") as f:
                f.write(self.SYNTHETIC_PASSWORD)
            with mock.patch.object(core.cfgmod, "credential_paths", return_value=(user_file, pwd_file)), \
                 mock.patch.object(core, "_build_ssl_context", return_value=MagicMock()):
                ok, reason = core.check_static_prerequisites(self.DEPLOYMENT, table)
        self.assertFalse(ok)
        self.assertEqual(reason, "DynamoDB CONFIG unavailable")
        self._assert_reason_clean(reason)

    def test_run_pipeline_retry_warning_is_generic(self):
        """The actual logger.warning(...) call in run_pipeline's retry loop
        -- not just check_static_prerequisites's return value -- must stay
        clean on every retry."""
        stop_event = threading.Event()

        fake_table = MagicMock()
        fake_table.get_item.side_effect = RuntimeError(
            "AccessDeniedException: arn:aws:iam::668311715351:role/GoldenGateMonitorReadRole-dev")

        def fake_resource(*a, **k):
            resource = MagicMock()
            resource.Table.return_value = fake_table
            return resource

        real_check = core.check_static_prerequisites

        def fake_check(dep, table):
            stop_event.set()
            return real_check(dep, table)

        with tempfile.TemporaryDirectory() as tmp:
            user_file = os.path.join(tmp, "user")
            pwd_file = os.path.join(tmp, "pwd")
            with open(user_file, "w") as f:
                f.write(self.SYNTHETIC_USER)
            with open(pwd_file, "w") as f:
                f.write(self.SYNTHETIC_PASSWORD)

            with mock.patch.object(core.boto3, "resource", side_effect=fake_resource), \
                 mock.patch.object(core, "check_static_prerequisites", side_effect=fake_check), \
                 mock.patch.object(core.cfgmod, "credential_paths", return_value=(user_file, pwd_file)), \
                 mock.patch.object(core, "_build_ssl_context", return_value=MagicMock()):
                with self.assertLogs(core.logger, level="WARNING") as log_ctx:
                    core.run_pipeline(self.DEPLOYMENT, stop_event, {}, "eu-west-1", "gg-eks-pipeline", "gg-monitor-0")

        combined = "\n".join(log_ctx.output)
        self.assertIn("gg-oracle-payments-01", combined)
        self.assertIn("DynamoDB CONFIG unavailable", combined)
        self._assert_reason_clean(combined)


class CloudWatchGateTests(unittest.TestCase):
    def test_disabled_by_default(self):
        core.CLOUDWATCH_PUBLISH_ENABLED = False
        self.assertFalse(core.cloudwatch_enabled_for({"metricsEnabled": True}))

    def test_requires_both_flags(self):
        core.CLOUDWATCH_PUBLISH_ENABLED = True
        self.assertFalse(core.cloudwatch_enabled_for({"metricsEnabled": False}))
        self.assertTrue(core.cloudwatch_enabled_for({"metricsEnabled": True}))
        core.CLOUDWATCH_PUBLISH_ENABLED = False


class NoActiveHealingTests(unittest.TestCase):
    def test_no_kubernetes_client_import(self):
        with open(core.__file__) as f:
            src = f.read()
        self.assertNotIn("import kubernetes", src)
        self.assertNotIn("client.CoreV1Api", src)

    def test_failover_flag_computed_but_never_acted_on(self):
        counters, act = gh.abend_step(
            status="ABENDED", state={}, now=1000,
            rule={"abendRecheckSeconds": 1, "maxConsecutiveAbends": 1, "alertEachAbend": False,
                  "failoverEnabled": True},
            alerts_enabled=True)
        self.assertIn("failover", act)
        import inspect
        polling_src = inspect.getsource(core.polling_loop)
        self.assertIn('act["failover"] is never acted on', polling_src)


class CredentialFailClosedTests(unittest.TestCase):
    """Missing/empty admin credential files must fail closed: no GoldenGate
    HTTP call, no Basic auth attempt, no fallback username, readiness false
    for that deployment, and no credential value in the log."""

    SYNTHETIC_USER = "synthetic-test-oggadmin"
    SYNTHETIC_PASSWORD = "synthetic-test-P@ssw0rd!"

    def _run_one_tick(self, user_file, pwd_file):
        """Runs polling_loop for exactly one tick: checkIntervalSeconds=0
        makes every sleep instantaneous, and a side effect on the first
        CONFIG read sets stop_event so the loop body runs exactly once."""
        deployment = {
            "name": "gg-oracle-payments-01",
            "type": "oracle",
            "adminHost": "gg-oracle-payments-01.goldengate-dev.svc.cluster.local",
            "adminPort": 8443,
            "tlsServerName": "gg-oracle-payments-01.goldengate-dev.adcbmis.local",
            "pipeline": "payments-ora-to-pg-001",
        }
        stop_event = threading.Event()

        def fake_get_item(Key):
            stop_event.set()
            return {"Item": {"deploymentType": "oracle", "checkIntervalSeconds": 0, "alertsEnabled": False}}

        table = MagicMock()
        table.get_item.side_effect = fake_get_item

        mgr = MagicMock()
        mgr.renew.return_value = True

        state = core.LeaseState()
        state.set_leader(True)

        fetch_calls = []
        opener_calls = []

        with mock.patch.object(core.cfgmod, "credential_paths", return_value=(user_file, pwd_file)), \
             mock.patch.object(core, "fetch_gg_processes", side_effect=lambda *a, **k: fetch_calls.append(1) or []), \
             mock.patch.object(core, "_basic_opener", side_effect=lambda *a, **k: opener_calls.append(1) or MagicMock()), \
             mock.patch.object(core, "_build_ssl_context", return_value=MagicMock()):
            with self.assertLogs(core.logger, level="INFO") as log_ctx:
                core.polling_loop(deployment, table, mgr, state, stop_event)

        return state, fetch_calls, opener_calls, log_ctx.output

    def _assert_fails_closed(self, state, fetch_calls, opener_calls, log_output):
        self.assertEqual(fetch_calls, [], "GoldenGate Admin REST must never be called")
        self.assertEqual(opener_calls, [], "no Basic-auth opener may be constructed")
        self.assertFalse(state.credentials_ok())
        combined_log = "\n".join(log_output)
        self.assertIn("gg-oracle-payments-01", combined_log)
        for forbidden in (self.SYNTHETIC_USER, self.SYNTHETIC_PASSWORD,
                         "-admin-user", "-admin-password", "oggadmin"):
            self.assertNotIn(forbidden, combined_log)

    def test_username_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_file = os.path.join(tmp, "does-not-exist-user")
            pwd_file = os.path.join(tmp, "pwd")
            with open(pwd_file, "w") as f:
                f.write(self.SYNTHETIC_PASSWORD)
            state, fetch_calls, opener_calls, log_output = self._run_one_tick(user_file, pwd_file)
        self._assert_fails_closed(state, fetch_calls, opener_calls, log_output)

    def test_username_file_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_file = os.path.join(tmp, "user")
            pwd_file = os.path.join(tmp, "pwd")
            open(user_file, "w").close()
            with open(pwd_file, "w") as f:
                f.write(self.SYNTHETIC_PASSWORD)
            state, fetch_calls, opener_calls, log_output = self._run_one_tick(user_file, pwd_file)
        self._assert_fails_closed(state, fetch_calls, opener_calls, log_output)

    def test_password_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_file = os.path.join(tmp, "user")
            pwd_file = os.path.join(tmp, "does-not-exist-pwd")
            with open(user_file, "w") as f:
                f.write(self.SYNTHETIC_USER)
            state, fetch_calls, opener_calls, log_output = self._run_one_tick(user_file, pwd_file)
        self._assert_fails_closed(state, fetch_calls, opener_calls, log_output)

    def test_password_file_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_file = os.path.join(tmp, "user")
            pwd_file = os.path.join(tmp, "pwd")
            with open(user_file, "w") as f:
                f.write(self.SYNTHETIC_USER)
            open(pwd_file, "w").close()
            state, fetch_calls, opener_calls, log_output = self._run_one_tick(user_file, pwd_file)
        self._assert_fails_closed(state, fetch_calls, opener_calls, log_output)

    def test_both_credentials_present_polling_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_file = os.path.join(tmp, "user")
            pwd_file = os.path.join(tmp, "pwd")
            with open(user_file, "w") as f:
                f.write(self.SYNTHETIC_USER)
            with open(pwd_file, "w") as f:
                f.write(self.SYNTHETIC_PASSWORD)
            state, fetch_calls, opener_calls, log_output = self._run_one_tick(user_file, pwd_file)
        self.assertEqual(len(fetch_calls), 1, "GoldenGate Admin REST must be polled once credentials are present")
        self.assertEqual(len(opener_calls), 1)
        self.assertTrue(state.credentials_ok())
        combined_log = "\n".join(log_output)
        self.assertNotIn(self.SYNTHETIC_USER, combined_log)
        self.assertNotIn(self.SYNTHETIC_PASSWORD, combined_log)

    def test_no_oggadmin_fallback_in_source(self):
        import inspect
        src = inspect.getsource(core.polling_loop)
        self.assertNotIn("oggadmin", src)
        self.assertNotIn('or "oggadmin"', src)


class ProcessDiscoveryTests(unittest.TestCase):
    """fetch_gg_processes hardening: no STATE#unknown, no exception on
    malformed data, no duplicate/synthetic process rows, exact real names
    preserved, empty results always valid."""

    BASE = "https://gg-test:8443"

    def _fetch(self, responses):
        def _stub(url, opener, timeout=5):
            for suffix, payload in responses.items():
                if url == self.BASE + suffix:
                    return payload
            raise AssertionError(f"unexpected URL requested in test: {url}")
        with mock.patch.object(core, "_http_json", side_effect=_stub):
            return core.fetch_gg_processes(self.BASE, opener=MagicMock())

    def test_extracts_list_detail_normalization(self):
        procs = self._fetch({
            "/services/v2/deployments": {},
            "/services/v2/extracts": {"response": {"items": [{"name": "EXT1"}]}},
            "/services/v2/extracts/EXT1": {"response": {"status": "running", "lag": 12}},
            "/services/v2/replicats": {"response": {"items": []}},
            "/services/v2/sources": {"response": {"items": []}},
        })
        self.assertEqual(len(procs), 1)
        p = procs[0]
        self.assertEqual(p["process"], "EXT1")
        self.assertEqual(p["type"], "extract")
        self.assertEqual(p["status"], "RUNNING")
        self.assertEqual(p["lagSeconds"], 12.0)
        self.assertFalse(p["abended"])

    def test_replicats_list_detail_normalization(self):
        procs = self._fetch({
            "/services/v2/deployments": {},
            "/services/v2/extracts": {"response": {"items": []}},
            "/services/v2/replicats": {"response": {"items": [{"name": "REP1"}]}},
            "/services/v2/replicats/REP1": {"response": {"status": "ABENDED", "lagSeconds": 30}},
            "/services/v2/sources": {"response": {"items": []}},
        })
        self.assertEqual(len(procs), 1)
        p = procs[0]
        self.assertEqual(p["process"], "REP1")
        self.assertEqual(p["type"], "replicat")
        self.assertEqual(p["status"], "ABENDED")
        self.assertTrue(p["abended"])

    def test_distpath_normalization(self):
        procs = self._fetch({
            "/services/v2/deployments": {},
            "/services/v2/extracts": {"response": {"items": []}},
            "/services/v2/replicats": {"response": {"items": []}},
            "/services/v2/sources": {"response": {
                "items": [{"name": "DP1", "status": "running", "bytesSent": 500}]}},
        })
        self.assertEqual(len(procs), 1)
        p = procs[0]
        self.assertEqual(p["process"], "DP1")
        self.assertEqual(p["type"], "distpath")
        self.assertEqual(p["status"], "RUNNING")
        self.assertEqual(p["bytes"], 500)

    def test_valid_process_names_preserved_exactly(self):
        procs = self._fetch({
            "/services/v2/deployments": {},
            "/services/v2/extracts": {"response": {"items": [{"name": "EXT_ORA_PAYMENTS_01"}]}},
            "/services/v2/extracts/EXT_ORA_PAYMENTS_01": {"response": {"status": "RUNNING"}},
            "/services/v2/replicats": {"response": {"items": []}},
            "/services/v2/sources": {"response": {"items": []}},
        })
        self.assertEqual(procs[0]["process"], "EXT_ORA_PAYMENTS_01")

    def test_missing_process_name_is_skipped(self):
        procs = self._fetch({
            "/services/v2/deployments": {},
            "/services/v2/extracts": {"response": {"items": [{"status": "RUNNING"}, {"name": ""}, {"name": None}]}},
            "/services/v2/replicats": {"response": {"items": []}},
            "/services/v2/sources": {"response": {"items": [{"status": "RUNNING"}]}},
        })
        self.assertEqual(procs, [])

    def test_id_only_item_never_becomes_synthetic_unknown(self):
        procs = self._fetch({
            "/services/v2/deployments": {},
            "/services/v2/extracts": {"response": {"items": [{"$id": 42, "status": "RUNNING"}]}},
            "/services/v2/replicats": {"response": {"items": []}},
            "/services/v2/sources": {"response": {"items": []}},
        })
        self.assertEqual(procs, [])
        self.assertNotIn("unknown", [p["process"] for p in procs])

    def test_malformed_list_items_do_not_crash_tick(self):
        procs = self._fetch({
            "/services/v2/deployments": {},
            "/services/v2/extracts": {"response": {"items": [None, "garbage", 123, {"name": "EXT1"}]}},
            "/services/v2/extracts/EXT1": {"response": {"status": "RUNNING"}},
            "/services/v2/replicats": {"response": {"items": [{"name": "REP1"}]}},
            "/services/v2/replicats/REP1": {"response": {"status": "RUNNING"}},
            "/services/v2/sources": {"response": {"items": "not-a-list"}},
        })
        self.assertEqual(sorted(p["process"] for p in procs), ["EXT1", "REP1"])

    def test_malformed_lag_becomes_safe_value(self):
        procs = self._fetch({
            "/services/v2/deployments": {},
            "/services/v2/extracts": {"response": {"items": [{"name": "EXT1"}]}},
            "/services/v2/extracts/EXT1": {"response": {"status": "RUNNING", "lag": "not-a-number"}},
            "/services/v2/replicats": {"response": {"items": [{"name": "REP1"}]}},
            "/services/v2/replicats/REP1": {"response": {"status": "RUNNING", "lagSeconds": -50}},
            "/services/v2/sources": {"response": {"items": []}},
        })
        lags = {p["process"]: p["lagSeconds"] for p in procs}
        self.assertEqual(lags["EXT1"], 0.0)
        self.assertEqual(lags["REP1"], 0.0)

    def test_empty_process_lists_are_valid(self):
        procs = self._fetch({
            "/services/v2/deployments": {},
            "/services/v2/extracts": {"response": {"items": []}},
            "/services/v2/replicats": {"response": {"items": []}},
            "/services/v2/sources": {"response": {"items": []}},
        })
        self.assertEqual(procs, [])

    def test_duplicate_process_items_deduplicated(self):
        procs = self._fetch({
            "/services/v2/deployments": {},
            "/services/v2/extracts": {"response": {"items": [{"name": "EXT1"}, {"name": "EXT1"}]}},
            "/services/v2/extracts/EXT1": {"response": {"status": "RUNNING"}},
            "/services/v2/replicats": {"response": {"items": []}},
            "/services/v2/sources": {"response": {"items": []}},
        })
        self.assertEqual(len(procs), 1)

    def test_raw_response_payloads_not_logged(self):
        def _stub(url, opener, timeout=5):
            if url.endswith("/services/v2/deployments"):
                return {}
            raise RuntimeError("SECRET-MARKER-zzz raw body <html>should not appear</html>")
        with mock.patch.object(core, "_http_json", side_effect=_stub):
            with self.assertLogs(core.logger, level="WARNING") as log_ctx:
                procs = core.fetch_gg_processes(self.BASE, opener=MagicMock())
        self.assertEqual(procs, [])
        combined = "\n".join(log_ctx.output)
        self.assertNotIn("SECRET-MARKER-zzz", combined)
        self.assertNotIn("<html>", combined)

    def test_discovery_counts_and_summary_log(self):
        procs = [
            {"process": "E1", "type": "extract"},
            {"process": "E2", "type": "extract"},
            {"process": "R1", "type": "replicat"},
            {"process": "D1", "type": "distpath"},
        ]
        self.assertEqual(core.discovery_counts(procs), {"extract": 2, "replicat": 1, "distpath": 1})
        with self.assertLogs(core.logger, level="INFO") as log_ctx:
            core.log_discovery_summary("gg-oracle-payments-01", procs)
        combined = "\n".join(log_ctx.output)
        self.assertIn('"event": "process_discovery_summary"', combined)
        self.assertIn('"deployment": "gg-oracle-payments-01"', combined)
        self.assertIn('"extractCount": 2', combined)
        self.assertIn('"replicatCount": 1', combined)
        self.assertIn('"distpathCount": 1', combined)
        self.assertIn('"totalCount": 4', combined)

    def test_zero_process_discovery_summary_is_valid(self):
        with self.assertLogs(core.logger, level="INFO") as log_ctx:
            core.log_discovery_summary("gg-oracle-payments-01", [])
        combined = "\n".join(log_ctx.output)
        self.assertIn('"totalCount": 0', combined)


class BuildMetricBatchTests(unittest.TestCase):
    """build_metric_batch: pure, no boto3, exact manager-compatible metric
    names/dimensions/units."""

    def test_namespace_constant(self):
        self.assertEqual(core.CLOUDWATCH_NAMESPACE, "GoldenGate/Pipelines")

    def test_deployment_dimensions_and_units(self):
        md = core.build_metric_batch("gg-oracle-payments-01", "oracle", {"lag": 1, "abend": 1, "down": 1})
        by_name = {m["MetricName"]: m for m in md}
        for name in ("LagBreached", "AbendFailure", "DeploymentDown"):
            self.assertEqual(by_name[name]["Dimensions"],
                             [{"Name": "Deployment", "Value": "gg-oracle-payments-01"},
                              {"Name": "DeploymentType", "Value": "oracle"}])
            self.assertEqual(by_name[name]["Unit"], "Count")
            self.assertEqual(by_name[name]["Value"], 1.0)

    def test_heartbeat_only_when_ok(self):
        md_off = core.build_metric_batch("gg-x", "oracle", {"lag": 0, "abend": 0, "down": 0}, heartbeat_ok=False)
        self.assertNotIn("HeartbeatAgeSeconds", [m["MetricName"] for m in md_off])
        md_on = core.build_metric_batch("gg-x", "oracle", {"lag": 0, "abend": 0, "down": 0}, heartbeat_ok=True)
        hb = next(m for m in md_on if m["MetricName"] == "HeartbeatAgeSeconds")
        self.assertEqual(hb["Value"], 0.0)
        self.assertEqual(hb["Unit"], "Seconds")
        self.assertEqual(hb["Dimensions"], [{"Name": "Deployment", "Value": "gg-x"},
                                            {"Name": "DeploymentType", "Value": "oracle"}])

    def test_critical_service_dimensions_and_values(self):
        md = core.build_metric_batch("gg-x", "oracle", {"lag": 0, "abend": 0, "down": 0},
                                     critical_service_status={"adminsrvr": True, "distsrvr": False})
        entries = {m["Dimensions"][-1]["Value"]: m for m in md if m["MetricName"] == "CriticalServiceDown"}
        self.assertEqual(entries["adminsrvr"]["Value"], 0.0)
        self.assertEqual(entries["distsrvr"]["Value"], 1.0)
        for m in entries.values():
            self.assertEqual(m["Unit"], "Count")
            self.assertEqual([d["Name"] for d in m["Dimensions"]], ["Deployment", "DeploymentType", "Service"])

    def test_process_dimensions_extract_lag(self):
        procs = [{"process": "EXT1", "type": "extract", "lagSeconds": 12.5, "abended": False}]
        md = core.build_metric_batch("gg-x", "oracle", {"lag": 0, "abend": 0, "down": 0}, procs=procs)
        lag_metric = next(m for m in md if m["MetricName"] == "ExtractLagSeconds")
        self.assertEqual(lag_metric["Value"], 12.5)
        self.assertEqual(lag_metric["Unit"], "Seconds")
        self.assertEqual([d["Name"] for d in lag_metric["Dimensions"]], ["Deployment", "DeploymentType", "Process"])
        self.assertEqual(lag_metric["Dimensions"][-1]["Value"], "EXT1")
        abend_metric = next(m for m in md if m["MetricName"] == "AbendState")
        self.assertEqual(abend_metric["Value"], 0.0)
        self.assertEqual(abend_metric["Unit"], "Count")

    def test_process_dimensions_replicat_lag(self):
        procs = [{"process": "REP1", "type": "replicat", "lagSeconds": 3.0, "abended": True}]
        md = core.build_metric_batch("gg-x", "oracle", {"lag": 0, "abend": 0, "down": 0}, procs=procs)
        lag_metric = next(m for m in md if m["MetricName"] == "ReplicatLagSeconds")
        self.assertEqual(lag_metric["Value"], 3.0)
        abend_metric = next(m for m in md if m["MetricName"] == "AbendState")
        self.assertEqual(abend_metric["Value"], 1.0)

    def test_unknown_process_type_no_lag_metric_but_has_abendstate(self):
        procs = [{"process": "DP1", "type": "distpath", "lagSeconds": 0.0, "abended": False}]
        md = core.build_metric_batch("gg-x", "oracle", {"lag": 0, "abend": 0, "down": 0}, procs=procs)
        names = [m["MetricName"] for m in md]
        self.assertNotIn("ExtractLagSeconds", names)
        self.assertNotIn("ReplicatLagSeconds", names)
        self.assertIn("AbendState", names)

    def test_abend_event_entries(self):
        md_none = core.build_metric_batch("gg-x", "oracle", {"lag": 0, "abend": 0, "down": 0}, abend_events=[])
        self.assertNotIn("AbendEvent", [m["MetricName"] for m in md_none])
        md = core.build_metric_batch("gg-x", "oracle", {"lag": 0, "abend": 0, "down": 0}, abend_events=["EXT1"])
        ev = next(m for m in md if m["MetricName"] == "AbendEvent")
        self.assertEqual(ev["Value"], 1.0)
        self.assertEqual(ev["Unit"], "Count")
        self.assertEqual(ev["Dimensions"][-1], {"Name": "Process", "Value": "EXT1"})

    def test_no_boto3_reference_in_build_function(self):
        # co_names holds the actual names the function body loads/calls --
        # unlike source text, it can't false-positive on the docstring.
        names = core.build_metric_batch.__code__.co_names
        self.assertNotIn("boto3", names)
        self.assertNotIn("put_metric_data", names)


class PublishMetricBatchTests(unittest.TestCase):
    def test_batches_of_at_most_20(self):
        metric_data = [{"MetricName": "AbendState",
                        "Dimensions": [{"Name": "Process", "Value": f"P{i}"}],
                        "Value": 0.0, "Unit": "Count"} for i in range(45)]
        cw = MagicMock()
        core.publish_metric_batch(cw, metric_data)
        self.assertEqual(cw.put_metric_data.call_count, 3)
        sizes = [len(c.kwargs["MetricData"]) for c in cw.put_metric_data.call_args_list]
        self.assertEqual(sizes, [20, 20, 5])
        for c in cw.put_metric_data.call_args_list:
            self.assertEqual(c.kwargs["Namespace"], "GoldenGate/Pipelines")

    def test_no_call_with_empty_batch(self):
        cw = MagicMock()
        core.publish_metric_batch(cw, [])
        cw.put_metric_data.assert_not_called()


class MetricPublicationIntegrationTests(unittest.TestCase):
    """Wires build_metric_batch/publish_metric_batch into polling_loop:
    heartbeat semantics must depend on an actual successful, fenced
    STATE#_deployment write for this tick -- never on process status, never
    published from a standby, never published when CloudWatch is disabled."""

    DEPLOYMENT = {
        "name": "gg-oracle-payments-01",
        "type": "oracle",
        "adminHost": "gg-oracle-payments-01.goldengate-dev.svc.cluster.local",
        "adminPort": 8443,
        "tlsServerName": "gg-oracle-payments-01.goldengate-dev.adcbmis.local",
        "pipeline": "payments-ora-to-pg-001",
    }

    def _run_tick(self, leader=True, fence_write=False, cloudwatch_enabled=True, raise_on_fetch=False):
        stop_event = threading.Event()

        def fake_get_item(Key):
            if Key.get("recordType") == "CONFIG":
                stop_event.set()
                return {"Item": {"deploymentType": "oracle", "checkIntervalSeconds": 0,
                                 "alertsEnabled": False, "metricsEnabled": True}}
            return {"Item": {}}

        table = MagicMock()
        table.get_item.side_effect = fake_get_item

        mgr = MagicMock()
        mgr.renew.return_value = not fence_write

        state = core.LeaseState()
        state.set_leader(leader)

        publish_calls = []
        cw_client_calls = []

        def fake_fetch(*a, **k):
            if raise_on_fetch:
                raise RuntimeError("admin rest down")
            return []

        with tempfile.TemporaryDirectory() as tmp:
            user_file = os.path.join(tmp, "user")
            pwd_file = os.path.join(tmp, "pwd")
            with open(user_file, "w") as f:
                f.write("synthetic-user")
            with open(pwd_file, "w") as f:
                f.write("synthetic-pass")

            core.CLOUDWATCH_PUBLISH_ENABLED = cloudwatch_enabled
            try:
                with mock.patch.object(core.cfgmod, "credential_paths", return_value=(user_file, pwd_file)), \
                     mock.patch.object(core, "fetch_gg_processes", side_effect=fake_fetch), \
                     mock.patch.object(core, "_basic_opener", return_value=MagicMock()), \
                     mock.patch.object(core, "_build_ssl_context", return_value=MagicMock()), \
                     mock.patch.object(core, "probe_critical_services",
                                       return_value={"adminsrvr": True, "distsrvr": True}), \
                     mock.patch.object(core, "_cloudwatch_client",
                                       side_effect=lambda: cw_client_calls.append(1) or MagicMock()), \
                     mock.patch.object(core, "publish_metric_batch",
                                       side_effect=lambda cw, md: publish_calls.append(md)):
                    core.polling_loop(self.DEPLOYMENT, table, mgr, state, stop_event)
            finally:
                core.CLOUDWATCH_PUBLISH_ENABLED = False

        return publish_calls, cw_client_calls

    def test_heartbeat_emitted_after_successful_up_write(self):
        publish_calls, cw_calls = self._run_tick(leader=True, fence_write=False, cloudwatch_enabled=True)
        self.assertEqual(len(publish_calls), 1)
        self.assertIn("HeartbeatAgeSeconds", [m["MetricName"] for m in publish_calls[0]])
        self.assertEqual(len(cw_calls), 1)

    def test_heartbeat_emitted_after_successful_deployment_down_write(self):
        publish_calls, cw_calls = self._run_tick(
            leader=True, fence_write=False, cloudwatch_enabled=True, raise_on_fetch=True)
        self.assertEqual(len(publish_calls), 1)
        self.assertIn("HeartbeatAgeSeconds", [m["MetricName"] for m in publish_calls[0]])

    def test_standby_emits_no_heartbeat(self):
        publish_calls, cw_calls = self._run_tick(leader=False, cloudwatch_enabled=True)
        self.assertEqual(publish_calls, [])
        self.assertEqual(cw_calls, [])

    def test_failed_state_write_emits_no_heartbeat(self):
        publish_calls, cw_calls = self._run_tick(leader=True, fence_write=True, cloudwatch_enabled=True)
        self.assertEqual(publish_calls, [])
        self.assertEqual(cw_calls, [])

    def test_no_cloudwatch_client_while_disabled(self):
        publish_calls, cw_calls = self._run_tick(leader=True, fence_write=False, cloudwatch_enabled=False)
        self.assertEqual(publish_calls, [])
        self.assertEqual(cw_calls, [])


class LoggerHierarchyIntegrationTests(unittest.TestCase):
    """Proves the actual running-container logging path: monitor.py
    configures goldengate.monitor's level/handler; collector.py's logger
    must be a child of it (goldengate.monitor.collector), carry no handler
    of its own, and never duplicate a log line. Without this, INFO records
    such as process_discovery_summary are silently dropped in production."""

    @classmethod
    def setUpClass(cls):
        import monitor as monitor_module
        cls.monitor_module = monitor_module

    def test_collector_logger_is_child_of_goldengate_monitor(self):
        self.assertEqual(core.logger.name, "goldengate.monitor.collector")
        self.assertIsNotNone(core.logger.parent)
        self.assertEqual(core.logger.parent.name, "goldengate.monitor")

    def test_collector_logger_has_no_handlers_of_its_own(self):
        # No new StreamHandler, no basicConfig -- only the parent's handler
        # (installed by monitor.py) may ever fire.
        self.assertEqual(core.logger.handlers, [])

    def test_collector_inherits_configured_info_level(self):
        self.assertEqual(core.logger.getEffectiveLevel(), logging.INFO)

    def test_goldengate_monitor_carries_exactly_one_handler(self):
        # Proves collector.py never adds a second handler to the shared
        # parent logger (no duplicate-output path exists).
        self.assertEqual(len(self.monitor_module.logger.handlers), 1)

    def _capture_real_handler_output(self, fn):
        """Swaps the actual configured StreamHandler's target stream (not
        sys.stdout, which the handler captured a fixed reference to at
        import time) so we observe exactly what the real handler chain
        would write to container stdout."""
        import io
        handler = self.monitor_module._handler
        buf = io.StringIO()
        original_stream = handler.stream
        handler.setStream(buf)
        try:
            fn()
        finally:
            handler.setStream(original_stream)
        return buf.getvalue()

    def test_discovery_summary_reaches_real_handler_exactly_once(self):
        procs = [{"process": "E1", "type": "extract"},
                {"process": "R1", "type": "replicat"},
                {"process": "D1", "type": "distpath"}]
        output = self._capture_real_handler_output(
            lambda: core.log_discovery_summary("gg-oracle-payments-01", procs))
        lines = [ln for ln in output.splitlines() if "process_discovery_summary" in ln]
        self.assertEqual(len(lines), 1, f"expected exactly one summary line, got: {lines!r}")

    def test_discovery_summary_is_valid_json_with_only_allowed_keys(self):
        procs = [{"process": "E1", "type": "extract"}]
        output = self._capture_real_handler_output(
            lambda: core.log_discovery_summary("gg-oracle-payments-01", procs))
        line = next(ln for ln in output.splitlines() if "process_discovery_summary" in ln)
        record = json.loads(line)
        self.assertEqual(record["event"], "process_discovery_summary")
        self.assertEqual(
            set(record.keys()),
            {"event", "deployment", "extractCount", "replicatCount", "distpathCount", "totalCount"})
        self.assertEqual(record["deployment"], "gg-oracle-payments-01")
        self.assertEqual(record["extractCount"], 1)
        self.assertEqual(record["replicatCount"], 0)
        self.assertEqual(record["distpathCount"], 0)
        self.assertEqual(record["totalCount"], 1)

    def test_discovery_summary_no_process_names_or_payload_values(self):
        procs = [{"process": "SUPER_SECRET_PROCESS_NAME", "type": "extract",
                 "metrics": {"password": "should-never-appear"}, "error": "leaky detail"}]
        output = self._capture_real_handler_output(
            lambda: core.log_discovery_summary("gg-oracle-payments-01", procs))
        self.assertNotIn("SUPER_SECRET_PROCESS_NAME", output)
        self.assertNotIn("should-never-appear", output)
        self.assertNotIn("leaky detail", output)

    def test_no_duplicate_output_across_repeated_ticks(self):
        procs = [{"process": "E1", "type": "extract"}]
        output = self._capture_real_handler_output(
            lambda: (core.log_discovery_summary("gg-oracle-payments-01", procs),
                    core.log_discovery_summary("gg-oracle-payments-01", procs)))
        lines = [ln for ln in output.splitlines() if "process_discovery_summary" in ln]
        self.assertEqual(len(lines), 2)  # exactly one line per call, no duplication per call


class PmsNormalizationTests(unittest.TestCase):
    """Pure PMS normalization helpers: bounded, safe types only, never an
    exception, never a silently-wrong type."""

    def test_number_malformed_becomes_zero(self):
        self.assertEqual(core._normalize_pms_number("not-a-number"), 0)
        self.assertEqual(core._normalize_pms_number(None), 0)
        self.assertEqual(core._normalize_pms_number([1, 2]), 0)

    def test_number_nan_infinite_negative_become_zero(self):
        self.assertEqual(core._normalize_pms_number(float("nan")), 0)
        self.assertEqual(core._normalize_pms_number(float("inf")), 0)
        self.assertEqual(core._normalize_pms_number(float("-inf")), 0)
        self.assertEqual(core._normalize_pms_number(-5), 0)
        self.assertEqual(core._normalize_pms_number(-0.5), 0)

    def test_number_boolean_rejected_not_treated_as_numeric(self):
        self.assertEqual(core._normalize_pms_number(True), 0)
        self.assertEqual(core._normalize_pms_number(False), 0)

    def test_number_valid_values_preserved(self):
        self.assertEqual(core._normalize_pms_number(42), 42)
        self.assertEqual(core._normalize_pms_number(3.5), 3.5)
        self.assertEqual(core._normalize_pms_number(0), 0)

    def test_inventory_normalization_unknown_fields_ignored(self):
        raw = {"processName": "SYNTHETIC_PROC", "processType": "sm", "processMode": "RUNNING",
              "processState": "UP", "processId": 42, "portNumber": 9011,
              "startTime": "2026-01-01T00:00:00Z", "stateTime": "2026-01-01T00:00:00Z",
              "lastHeartbeat": "2026-01-01T00:00:00Z", "firstMessage": 1, "lastMessage": 2,
              "someRandomUnknownField": "should be ignored", "secretToken": "ignored-too"}
        out = core.normalize_pms_inventory_item(raw)
        self.assertNotIn("someRandomUnknownField", out)
        self.assertNotIn("secretToken", out)
        self.assertEqual(out["processName"], "SYNTHETIC_PROC")
        self.assertEqual(out["processId"], 42)

    def test_inventory_normalization_missing_fields_absent(self):
        out = core.normalize_pms_inventory_item({"processName": "P1"})
        self.assertEqual(out, {"processName": "P1"})

    def test_inventory_normalization_non_dict_returns_empty(self):
        self.assertEqual(core.normalize_pms_inventory_item(None), {})
        self.assertEqual(core.normalize_pms_inventory_item("garbage"), {})
        self.assertEqual(core.normalize_pms_inventory_item([1, 2]), {})

    def test_performance_normalization_only_confirmed_numeric_fields(self):
        raw = {"cpuTimeUs": 100, "kernelTimeUs": 50, "userTimeUs": 50,
              "workingSetSize": 1000, "peakWorkingSetSize": 2000, "privateBytes": 500,
              "threadCount": 10, "handleCount": 20, "pageFaults": 5,
              "ioReadBytes": 1, "ioReadCount": 2, "ioWriteBytes": 3, "ioWriteCount": 4,
              "ioOtherBytes": 5, "ioOtherCount": 6, "processStartTime": 12345, "processId": 7,
              "unknownExtraField": "ignored"}
        out = core.normalize_pms_performance(raw)
        self.assertNotIn("unknownExtraField", out)
        self.assertEqual(out["cpuTimeUs"], 100)
        self.assertEqual(set(out.keys()), set(core._PMS_PERFORMANCE_NUMERIC_FIELDS))

    def test_performance_normalization_cumulative_counters_preserved_as_is(self):
        # cpuTimeUs/kernelTimeUs/userTimeUs must never be converted into a
        # rate/percentage in this phase -- preserved exactly (post-safety-clamp).
        raw = {"cpuTimeUs": 999999, "kernelTimeUs": 111111, "userTimeUs": 222222}
        out = core.normalize_pms_performance(raw)
        self.assertEqual(out["cpuTimeUs"], 999999)
        self.assertEqual(out["kernelTimeUs"], 111111)
        self.assertEqual(out["userTimeUs"], 222222)

    def test_performance_normalization_malformed_values_safe(self):
        raw = {"cpuTimeUs": "garbage", "workingSetSize": -5, "threadCount": True,
              "handleCount": float("nan"), "pageFaults": float("inf")}
        out = core.normalize_pms_performance(raw)
        self.assertEqual(out["cpuTimeUs"], 0)
        self.assertEqual(out["workingSetSize"], 0)
        self.assertEqual(out["threadCount"], 0)
        self.assertEqual(out["handleCount"], 0)
        self.assertEqual(out["pageFaults"], 0)

    def test_performance_normalization_non_dict_returns_empty(self):
        self.assertEqual(core.normalize_pms_performance(None), {})
        self.assertEqual(core.normalize_pms_performance([1, 2, 3]), {})

    def test_service_health_normalization_valid(self):
        out = core.normalize_pms_service_health(
            {"isHealthy": True, "criticalResourcesHealthy": 3, "criticalResourcesUnhealthy": 1})
        self.assertEqual(out, {"isHealthy": True, "criticalResourcesHealthy": 3, "criticalResourcesUnhealthy": 1})

    def test_service_health_normalization_malformed_fields_safe_defaults(self):
        out = core.normalize_pms_service_health(
            {"isHealthy": "yes", "criticalResourcesHealthy": "garbage", "criticalResourcesUnhealthy": -1})
        self.assertEqual(out["isHealthy"], False)  # non-boolean never silently accepted as healthy
        self.assertEqual(out["criticalResourcesHealthy"], 0)
        self.assertEqual(out["criticalResourcesUnhealthy"], 0)

    def test_service_health_normalization_non_dict_returns_safe_defaults(self):
        out = core.normalize_pms_service_health(None)
        self.assertEqual(out, {"isHealthy": False, "criticalResourcesHealthy": 0, "criticalResourcesUnhealthy": 0})


class HeartbeatAgeTests(unittest.TestCase):
    """heartbeat_age_seconds: pure, timezone-aware, injectable clock."""

    NOW = __import__("datetime").datetime(2026, 7, 30, 9, 0, 0, tzinfo=__import__("datetime").timezone.utc)

    def test_valid_z_suffix_timestamp(self):
        self.assertEqual(core.heartbeat_age_seconds("2026-07-30T08:59:00Z", now=self.NOW), 60)

    def test_valid_offset_timestamp(self):
        self.assertEqual(core.heartbeat_age_seconds("2026-07-30T12:59:00+04:00", now=self.NOW), 60)

    def test_future_timestamp_clamped_to_zero(self):
        self.assertEqual(core.heartbeat_age_seconds("2026-07-30T09:05:00Z", now=self.NOW), 0)

    def test_missing_timestamp_returns_none(self):
        self.assertIsNone(core.heartbeat_age_seconds(None, now=self.NOW))
        self.assertIsNone(core.heartbeat_age_seconds("", now=self.NOW))

    def test_malformed_timestamp_returns_none(self):
        self.assertIsNone(core.heartbeat_age_seconds("not-a-timestamp", now=self.NOW))
        self.assertIsNone(core.heartbeat_age_seconds("12345", now=self.NOW))

    def test_naive_timestamp_without_timezone_returns_none(self):
        # never assume local time -- an unqualified timestamp is unusable.
        self.assertIsNone(core.heartbeat_age_seconds("2026-07-30T08:59:00", now=self.NOW))

    def test_non_string_input_returns_none(self):
        self.assertIsNone(core.heartbeat_age_seconds(12345, now=self.NOW))
        self.assertIsNone(core.heartbeat_age_seconds(["2026-07-30T08:59:00Z"], now=self.NOW))

    def test_default_now_is_real_utc_when_not_injected(self):
        # sanity check that the pure helper still works without an injected
        # clock (uses real current time) -- age should be a small
        # non-negative number for a timestamp a few seconds ago.
        import datetime as _dt
        recent = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=5)).isoformat()
        age = core.heartbeat_age_seconds(recent)
        self.assertIsNotNone(age)
        self.assertGreaterEqual(age, 0)


class PmsRequestSequenceTests(unittest.TestCase):
    """collect_pms(): the full bounded, sequential production PMS request
    model, end to end against a mocked opener. Synthetic data only."""

    BASE = "https://gg-test:8443"

    def _fake_opener(self, inventory_payload, detail_payloads=None, detail_exceptions=None,
                     record_calls=None):
        detail_payloads = detail_payloads or {}
        detail_exceptions = detail_exceptions or {}
        record_calls = record_calls if record_calls is not None else []

        def _open(url, timeout=5):
            record_calls.append(url)
            if url in detail_exceptions:
                raise detail_exceptions[url]
            if url == f"{self.BASE}{core.PMS_INVENTORY_PATH}":
                body = json.dumps(inventory_payload).encode()
            elif url in detail_payloads:
                body = json.dumps(detail_payloads[url]).encode()
            else:
                raise AssertionError(f"unexpected PMS URL requested in test: {url}")
            resp = MagicMock()
            resp.read.return_value = body
            resp.__enter__.return_value = resp
            resp.__exit__.return_value = False
            return resp

        opener = MagicMock()
        opener.open.side_effect = _open
        return opener, record_calls

    def _inventory(self, names):
        return {"response": {"processes": [
            {"processName": n, "processId": i, "lastHeartbeat": "2026-07-30T08:59:00Z"}
            for i, n in enumerate(names)]}}

    def test_one_inventory_request_per_call(self):
        opener, calls = self._fake_opener(self._inventory(["P1"]),
                                          {f"{self.BASE}/services/v2/mpoints/P1/processPerformance": {"response": {}},
                                           f"{self.BASE}/services/v2/mpoints/P1/serviceHealth": {"response": {}}})
        core.collect_pms(self.BASE, opener)
        inventory_calls = [c for c in calls if c == f"{self.BASE}{core.PMS_INVENTORY_PATH}"]
        self.assertEqual(len(inventory_calls), 1)

    def test_unique_process_name_deduplication(self):
        inventory = {"response": {"processes": [
            {"processName": "DUP1"}, {"processName": "DUP1"}, {"processName": "UNIQ1"}]}}
        detail = {f"{self.BASE}/services/v2/mpoints/DUP1/processPerformance": {"response": {}},
                 f"{self.BASE}/services/v2/mpoints/DUP1/serviceHealth": {"response": {}},
                 f"{self.BASE}/services/v2/mpoints/UNIQ1/processPerformance": {"response": {}},
                 f"{self.BASE}/services/v2/mpoints/UNIQ1/serviceHealth": {"response": {}}}
        opener, calls = self._fake_opener(inventory, detail)
        result = core.collect_pms(self.BASE, opener)
        self.assertEqual(result["followedCount"], 2)
        detail_calls = [c for c in calls if "DUP1" in c or "UNIQ1" in c]
        self.assertEqual(len(detail_calls), 4)  # 2 processes x 2 detail kinds, never more

    def test_maximum_20_followed_processes(self):
        names = [f"P{i}" for i in range(30)]
        detail = {}
        for n in names:
            detail[f"{self.BASE}/services/v2/mpoints/{n}/processPerformance"] = {"response": {}}
            detail[f"{self.BASE}/services/v2/mpoints/{n}/serviceHealth"] = {"response": {}}
        opener, calls = self._fake_opener(self._inventory(names), detail)
        result = core.collect_pms(self.BASE, opener)
        self.assertEqual(result["inventoryCount"], 30)
        self.assertEqual(result["followedCount"], 20)
        detail_calls = [c for c in calls if c != f"{self.BASE}{core.PMS_INVENTORY_PATH}"]
        self.assertEqual(len(detail_calls), 40)  # 20 processes x 2 kinds

    def test_process_performance_and_service_health_paths_requested(self):
        opener, calls = self._fake_opener(
            self._inventory(["P1"]),
            {f"{self.BASE}/services/v2/mpoints/P1/processPerformance": {"response": {"cpuTimeUs": 1}},
             f"{self.BASE}/services/v2/mpoints/P1/serviceHealth": {"response": {"isHealthy": True}}})
        core.collect_pms(self.BASE, opener)
        self.assertIn(f"{self.BASE}/services/v2/mpoints/P1/processPerformance", calls)
        self.assertIn(f"{self.BASE}/services/v2/mpoints/P1/serviceHealth", calls)

    def test_process_name_url_encoded_as_one_segment(self):
        name = "PROC/WITH/SLASHES"
        opener, calls = self._fake_opener(
            self._inventory([name]),
            {f"{self.BASE}/services/v2/mpoints/PROC%2FWITH%2FSLASHES/processPerformance": {"response": {}},
             f"{self.BASE}/services/v2/mpoints/PROC%2FWITH%2FSLASHES/serviceHealth": {"response": {}}})
        core.collect_pms(self.BASE, opener)
        detail_calls = [c for c in calls if c != f"{self.BASE}{core.PMS_INVENTORY_PATH}"]
        for c in detail_calls:
            segments = c[len(self.BASE):].split("/")
            self.assertEqual(len(segments), 6)  # '', services, v2, mpoints, <encoded>, <kind>

    def test_no_heartbeat_endpoint_requested(self):
        opener, calls = self._fake_opener(
            self._inventory(["P1"]),
            {f"{self.BASE}/services/v2/mpoints/P1/processPerformance": {"response": {}},
             f"{self.BASE}/services/v2/mpoints/P1/serviceHealth": {"response": {}}})
        core.collect_pms(self.BASE, opener)
        self.assertFalse(any("heartbeat" in c.lower() for c in calls))

    def test_no_thread_performance_endpoint_requested(self):
        opener, calls = self._fake_opener(
            self._inventory(["P1"]),
            {f"{self.BASE}/services/v2/mpoints/P1/processPerformance": {"response": {}},
             f"{self.BASE}/services/v2/mpoints/P1/serviceHealth": {"response": {}}})
        core.collect_pms(self.BASE, opener)
        self.assertFalse(any("threadPerformance" in c for c in calls))

    def test_no_redundant_process_detail_endpoint_requested(self):
        opener, calls = self._fake_opener(
            self._inventory(["P1"]),
            {f"{self.BASE}/services/v2/mpoints/P1/processPerformance": {"response": {}},
             f"{self.BASE}/services/v2/mpoints/P1/serviceHealth": {"response": {}}})
        core.collect_pms(self.BASE, opener)
        self.assertFalse(any(c.endswith("/P1/process") for c in calls))

    def test_no_status_changes_or_metrics_endpoint_requested(self):
        opener, calls = self._fake_opener(
            self._inventory(["P1"]),
            {f"{self.BASE}/services/v2/mpoints/P1/processPerformance": {"response": {}},
             f"{self.BASE}/services/v2/mpoints/P1/serviceHealth": {"response": {}}})
        core.collect_pms(self.BASE, opener)
        self.assertFalse(any("statusChanges" in c or "v2/metrics" in c for c in calls))

    def test_no_direct_port_9015_used(self):
        # collect_pms only ever receives/uses the caller's base -- it never
        # constructs an alternate metricsPort/9015 URL of its own.
        with open(core.__file__) as f:
            src = f.read()
        collect_pms_src = src[src.index("def collect_pms"):]
        collect_pms_src = collect_pms_src[:collect_pms_src.index("\n\n\n")]
        self.assertNotIn("9015", collect_pms_src)
        self.assertNotIn("metricsPort", collect_pms_src)

    def test_get_only_no_data_argument(self):
        with open(core.__file__) as f:
            src = f.read()
        self.assertNotIn("data=", src[src.index("_http_json_bounded"):src.index("def normalize_pms_inventory_item")])

    def test_malformed_inventory_records_skipped(self):
        inventory = {"response": {"processes": [
            None, "garbage", 42, [1, 2], {"noProcessName": True}, {"processName": ""},
            {"processName": "OK1"},
        ]}}
        opener, calls = self._fake_opener(
            inventory, {f"{self.BASE}/services/v2/mpoints/OK1/processPerformance": {"response": {}},
                       f"{self.BASE}/services/v2/mpoints/OK1/serviceHealth": {"response": {}}})
        result = core.collect_pms(self.BASE, opener)
        self.assertEqual(result["inventoryCount"], 7)
        self.assertEqual(result["followedCount"], 1)
        self.assertEqual(result["successCount"], 1)

    def test_partial_per_process_failure_continues_remaining(self):
        inventory = self._inventory(["P1", "P2"])
        detail = {f"{self.BASE}/services/v2/mpoints/P1/processPerformance": {"response": {}},
                 f"{self.BASE}/services/v2/mpoints/P1/serviceHealth": {"response": {}},
                 f"{self.BASE}/services/v2/mpoints/P2/serviceHealth": {"response": {}}}
        exceptions = {f"{self.BASE}/services/v2/mpoints/P2/processPerformance": RuntimeError("boom")}
        opener, calls = self._fake_opener(inventory, detail, detail_exceptions=exceptions)
        result = core.collect_pms(self.BASE, opener)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["successCount"], 1)
        self.assertEqual(result["failureCount"], 1)
        # P2's serviceHealth was still attempted despite processPerformance failing
        self.assertIn(f"{self.BASE}/services/v2/mpoints/P2/serviceHealth", calls)

    def test_complete_pms_failure_all_details_fail(self):
        inventory = self._inventory(["P1", "P2"])
        exceptions = {
            f"{self.BASE}/services/v2/mpoints/P1/processPerformance": RuntimeError("boom"),
            f"{self.BASE}/services/v2/mpoints/P1/serviceHealth": RuntimeError("boom"),
            f"{self.BASE}/services/v2/mpoints/P2/processPerformance": RuntimeError("boom"),
            f"{self.BASE}/services/v2/mpoints/P2/serviceHealth": RuntimeError("boom"),
        }
        opener, calls = self._fake_opener(inventory, {}, detail_exceptions=exceptions)
        result = core.collect_pms(self.BASE, opener)
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(result["successCount"], 0)
        self.assertEqual(result["failureCount"], 2)

    def test_inventory_failure_yields_unavailable_status(self):
        opener = MagicMock()
        opener.open.side_effect = RuntimeError("connection refused")
        result = core.collect_pms(self.BASE, opener)
        self.assertIn(result["status"], core.PMS_ERROR_CATEGORIES)
        self.assertEqual(result["followedCount"], 0)
        self.assertEqual(result["inventoryCount"], 0)

    def test_collect_pms_never_raises_on_total_failure(self):
        opener = MagicMock()
        opener.open.side_effect = RuntimeError("boom")
        try:
            result = core.collect_pms(self.BASE, opener)
        except Exception as e:  # pragma: no cover -- must never happen
            self.fail(f"collect_pms raised unexpectedly: {e!r}")
        self.assertIsInstance(result, dict)

    def test_empty_inventory_is_a_valid_ok_result(self):
        opener, calls = self._fake_opener(self._inventory([]))
        result = core.collect_pms(self.BASE, opener)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["followedCount"], 0)

    def test_collection_timestamp_reflects_current_tick(self):
        opener, calls = self._fake_opener(self._inventory([]))
        before = core.cfgmod.now_epoch()
        result = core.collect_pms(self.BASE, opener)
        after = core.cfgmod.now_epoch()
        self.assertGreaterEqual(result["collectedAt"], before)
        self.assertLessEqual(result["collectedAt"], after)

    def test_no_raw_values_or_process_names_in_output_on_failure(self):
        opener = MagicMock()
        opener.open.side_effect = RuntimeError("SECRET_INTERNAL_DETAIL_xyz")
        result = core.collect_pms(self.BASE, opener)
        blob = json.dumps(result)
        self.assertNotIn("SECRET_INTERNAL_DETAIL_xyz", blob)

    def test_no_credential_or_hostname_leakage_in_output(self):
        inventory = self._inventory(["P1"])
        detail = {f"{self.BASE}/services/v2/mpoints/P1/processPerformance":
                 {"response": {"cpuTimeUs": 1}},
                 f"{self.BASE}/services/v2/mpoints/P1/serviceHealth": {"response": {"isHealthy": True}}}
        opener, calls = self._fake_opener(inventory, detail)
        result = core.collect_pms(self.BASE, opener)
        blob = json.dumps(result)
        self.assertNotIn("gg-test", blob)
        self.assertNotIn("8443", blob)


class PmsPollingLoopIntegrationTests(unittest.TestCase):
    """Wires collect_pms into polling_loop's guarded STATE#_deployment
    write: PMS enrichment must respect the exact same lease/fencing rules
    as everything else -- standby never requests PMS, a fenced tick never
    writes PMS state, and a PMS failure must never affect the deployment's
    own UP/DOWN status."""

    DEPLOYMENT = {
        "name": "gg-oracle-payments-01",
        "type": "oracle",
        "adminHost": "gg-oracle-payments-01.goldengate-dev.svc.cluster.local",
        "adminPort": 8443,
        "tlsServerName": "gg-oracle-payments-01.goldengate-dev.adcbmis.local",
        "pipeline": "payments-ora-to-pg-001",
    }

    def _run_tick(self, leader=True, fence_write=False, pms_side_effect=None):
        stop_event = threading.Event()

        def fake_get_item(Key):
            if Key.get("recordType") == "CONFIG":
                stop_event.set()
                return {"Item": {"deploymentType": "oracle", "checkIntervalSeconds": 0,
                                 "alertsEnabled": False, "metricsEnabled": False}}
            return {"Item": {}}

        table = MagicMock()
        table.get_item.side_effect = fake_get_item
        update_calls = []
        table.update_item.side_effect = lambda **kw: update_calls.append(kw)

        mgr = MagicMock()
        mgr.renew.return_value = not fence_write

        state = core.LeaseState()
        state.set_leader(leader)

        pms_calls = []

        def fake_collect_pms(base, opener, **kw):
            pms_calls.append(1)
            if pms_side_effect is not None:
                return pms_side_effect()
            return {"status": "OK", "collectedAt": 123, "inventoryCount": 1, "followedCount": 1,
                   "successCount": 1, "failureCount": 0, "heartbeatAgeSeconds": 5, "processes": {}}

        with tempfile.TemporaryDirectory() as tmp:
            user_file = os.path.join(tmp, "user")
            pwd_file = os.path.join(tmp, "pwd")
            with open(user_file, "w") as f:
                f.write("synthetic-user")
            with open(pwd_file, "w") as f:
                f.write("synthetic-pass")

            with mock.patch.object(core.cfgmod, "credential_paths", return_value=(user_file, pwd_file)), \
                 mock.patch.object(core, "fetch_gg_processes", return_value=[]), \
                 mock.patch.object(core, "_basic_opener", return_value=MagicMock()), \
                 mock.patch.object(core, "_build_ssl_context", return_value=MagicMock()), \
                 mock.patch.object(core, "probe_critical_services", return_value={}), \
                 mock.patch.object(core, "collect_pms", side_effect=fake_collect_pms):
                core.polling_loop(self.DEPLOYMENT, table, mgr, state, stop_event)

        return pms_calls, update_calls

    def test_standby_performs_no_pms_requests(self):
        pms_calls, update_calls = self._run_tick(leader=False)
        self.assertEqual(pms_calls, [])

    def test_fenced_collector_performs_no_pms_write(self):
        pms_calls, update_calls = self._run_tick(leader=True, fence_write=True)
        deployment_writes = [c for c in update_calls if c["Key"].get("recordType") == "STATE#_deployment"]
        self.assertEqual(deployment_writes, [])

    def test_pms_enrichment_in_guarded_deployment_write(self):
        pms_calls, update_calls = self._run_tick(leader=True, fence_write=False)
        self.assertEqual(len(pms_calls), 1)
        deployment_writes = [c for c in update_calls if c["Key"].get("recordType") == "STATE#_deployment"]
        self.assertEqual(len(deployment_writes), 1)
        self.assertIn("pms=:pms", deployment_writes[0]["UpdateExpression"])

    def test_no_new_dynamodb_recordtype_used(self):
        pms_calls, update_calls = self._run_tick(leader=True, fence_write=False)
        record_types = {c["Key"].get("recordType") for c in update_calls}
        for rt in record_types:
            self.assertTrue(rt == "STATE#_deployment" or str(rt).startswith("STATE#"))

    def test_pms_failure_does_not_mark_deployment_down(self):
        pms_calls, update_calls = self._run_tick(
            leader=True, fence_write=False,
            pms_side_effect=lambda: {"status": "UNAVAILABLE", "collectedAt": 1, "inventoryCount": 0,
                                     "followedCount": 0, "successCount": 0, "failureCount": 0,
                                     "heartbeatAgeSeconds": None, "processes": {}})
        deployment_writes = [c for c in update_calls if c["Key"].get("recordType") == "STATE#_deployment"]
        self.assertEqual(len(deployment_writes), 1)
        self.assertEqual(deployment_writes[0]["ExpressionAttributeValues"][":st"], "UP")

    def test_no_pms_service_process_state_rows_created(self):
        pms_calls, update_calls = self._run_tick(leader=True, fence_write=False)
        process_record_types = {c["Key"].get("recordType") for c in update_calls
                                if c["Key"].get("recordType") != "STATE#_deployment"}
        self.assertEqual(process_record_types, set())


if __name__ == "__main__":
    unittest.main()
