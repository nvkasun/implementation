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


if __name__ == "__main__":
    unittest.main()
