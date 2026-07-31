import json
import logging
import os
import ssl
import sys
import tempfile
import threading
import unittest
import urllib.error
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

    def test_metrics_enabled_must_be_literal_true(self):
        core.CLOUDWATCH_PUBLISH_ENABLED = True
        try:
            for bad_value in ("true", "false", 1, 0, {"reachable": True}, [True], None):
                with self.subTest(bad_value=bad_value):
                    self.assertFalse(core.cloudwatch_enabled_for({"metricsEnabled": bad_value}))
            self.assertFalse(core.cloudwatch_enabled_for({}))  # missing value
            self.assertTrue(core.cloudwatch_enabled_for({"metricsEnabled": True}))
        finally:
            core.CLOUDWATCH_PUBLISH_ENABLED = False

    def test_publish_enabled_env_gate_must_be_literal_true(self):
        # Even if a caller (or a future refactor) assigned a non-Boolean
        # truthy value to the module-level switch, the gate must not accept
        # it via truthiness.
        core.CLOUDWATCH_PUBLISH_ENABLED = "true"
        try:
            self.assertFalse(core.cloudwatch_enabled_for({"metricsEnabled": True}))
        finally:
            core.CLOUDWATCH_PUBLISH_ENABLED = False

    def test_env_parser_accepts_only_trimmed_case_insensitive_true(self):
        for accepted in ("true", "True", "TRUE", "  true  ", "TrUe"):
            with self.subTest(accepted=accepted):
                self.assertTrue(core._parse_strict_bool_env(accepted))

    def test_env_parser_rejects_permissive_aliases_and_arbitrary_strings(self):
        for rejected in ("1", "yes", "on", "false", "0", "no", "off", "enabled", "TRUE!", "", " "):
            with self.subTest(rejected=rejected):
                self.assertFalse(core._parse_strict_bool_env(rejected))

    def test_env_parser_rejects_missing_value(self):
        self.assertFalse(core._parse_strict_bool_env(None))


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


class PublishMetricsIfEnabledTests(unittest.TestCase):
    """Phase 4D1 correction: publish_metrics_if_enabled is the single
    protected publication boundary -- it must construct no CloudWatch
    client while either gate is false, and a client-construction failure
    must never raise or reach a raw-exception log path."""

    METRIC_DATA = [{"MetricName": "AbendState", "Dimensions": [], "Value": 0.0, "Unit": "Count"}]

    def setUp(self):
        core.CLOUDWATCH_PUBLISH_ENABLED = False

    def tearDown(self):
        core.CLOUDWATCH_PUBLISH_ENABLED = False

    def test_no_client_constructed_when_publish_enabled_env_false(self):
        core.CLOUDWATCH_PUBLISH_ENABLED = False
        with mock.patch.object(core, "_cloudwatch_client") as client_fn, \
             mock.patch.object(core, "publish_metric_batch") as publish_fn:
            core.publish_metrics_if_enabled({"metricsEnabled": True}, "gg-x", self.METRIC_DATA)
        client_fn.assert_not_called()
        publish_fn.assert_not_called()

    def test_no_client_constructed_when_metrics_enabled_false(self):
        core.CLOUDWATCH_PUBLISH_ENABLED = True
        with mock.patch.object(core, "_cloudwatch_client") as client_fn, \
             mock.patch.object(core, "publish_metric_batch") as publish_fn:
            core.publish_metrics_if_enabled({"metricsEnabled": False}, "gg-x", self.METRIC_DATA)
        client_fn.assert_not_called()
        publish_fn.assert_not_called()

    def test_client_constructed_and_publish_called_when_both_gates_true(self):
        core.CLOUDWATCH_PUBLISH_ENABLED = True
        fake_cw = MagicMock()
        with mock.patch.object(core, "_cloudwatch_client", return_value=fake_cw) as client_fn, \
             mock.patch.object(core, "publish_metric_batch") as publish_fn:
            core.publish_metrics_if_enabled({"metricsEnabled": True}, "gg-x", self.METRIC_DATA)
        client_fn.assert_called_once()
        publish_fn.assert_called_once_with(fake_cw, self.METRIC_DATA, pipeline="gg-x")

    def test_client_construction_exception_never_escapes(self):
        core.CLOUDWATCH_PUBLISH_ENABLED = True
        with mock.patch.object(core, "_cloudwatch_client", side_effect=RuntimeError("boom")), \
             mock.patch.object(core, "publish_metric_batch") as publish_fn:
            core.publish_metrics_if_enabled({"metricsEnabled": True}, "gg-x", self.METRIC_DATA)  # must not raise
        publish_fn.assert_not_called()

    def test_client_construction_failure_log_has_only_safe_keys(self):
        core.CLOUDWATCH_PUBLISH_ENABLED = True
        with mock.patch.object(core, "_cloudwatch_client", side_effect=RuntimeError("boom")):
            with self.assertLogs(core.logger, level="ERROR") as log_ctx:
                core.publish_metrics_if_enabled({"metricsEnabled": True}, "gg-oracle-payments-01", self.METRIC_DATA)
        record = json.loads(log_ctx.records[0].getMessage())
        self.assertEqual(record["event"], "cloudwatch_client_creation_failed")
        self.assertEqual(record["deployment"], "gg-oracle-payments-01")
        self.assertEqual(record["errorCategory"], "RuntimeError")
        self.assertEqual(set(record.keys()), {"event", "deployment", "errorCategory"})

    def test_client_construction_failure_log_has_no_raw_exception_text(self):
        core.CLOUDWATCH_PUBLISH_ENABLED = True
        secret_message = (
            "arn:aws:sts::668311715351:assumed-role/GoldenGateMonitorReadRole-dev/i-0123456789abcdef "
            "AccessDenied for host gg-oracle-payments-01.goldengate-dev.svc.cluster.local "
            "process EXTORA1 secret=/mnt/secrets-store/dev-goldengate-source-admin")
        with mock.patch.object(core, "_cloudwatch_client", side_effect=RuntimeError(secret_message)):
            with self.assertLogs(core.logger, level="ERROR") as log_ctx:
                core.publish_metrics_if_enabled({"metricsEnabled": True}, "gg-oracle-payments-01", self.METRIC_DATA)
        combined = "\n".join(log_ctx.output)
        for forbidden in ("arn:aws", "GoldenGateMonitorReadRole-dev", "i-0123456789abcdef",
                         "goldengate-dev.svc.cluster.local", "EXTORA1", "/mnt/secrets-store",
                         "AccessDenied", "Traceback", "boom"):
            self.assertNotIn(forbidden, combined)


class PublishMetricBatchFailureLoggingTests(unittest.TestCase):
    """Phase 4D1 correction: a PutMetricData failure must never crash the
    caller and must be logged with only safe, closed fields -- never a raw
    exception message, traceback, ARN, hostname, secret path, or process
    name. No retries -- one publication attempt per batch."""

    def _failing_cw(self, message):
        cw = MagicMock()
        cw.put_metric_data.side_effect = RuntimeError(message)
        return cw

    def test_failure_does_not_raise(self):
        cw = self._failing_cw("boom")
        metric_data = [{"MetricName": "AbendState", "Dimensions": [], "Value": 0.0, "Unit": "Count"}]
        core.publish_metric_batch(cw, metric_data, pipeline="gg-oracle-payments-01")  # must not raise

    def test_failure_log_contains_no_raw_exception_or_internal_detail(self):
        secret_message = (
            "arn:aws:sts::668311715351:assumed-role/GoldenGateMonitorReadRole-dev/i-0123456789abcdef "
            "AccessDenied for host gg-oracle-payments-01.goldengate-dev.svc.cluster.local "
            "process EXTORA1 secret=/mnt/secrets-store/dev-goldengate-source-admin")
        cw = self._failing_cw(secret_message)
        metric_data = [{"MetricName": "AbendState", "Dimensions": [{"Name": "Process", "Value": "EXTORA1"}],
                        "Value": 0.0, "Unit": "Count"}]
        with self.assertLogs(core.logger, level="ERROR") as log_ctx:
            core.publish_metric_batch(cw, metric_data, pipeline="gg-oracle-payments-01")
        combined = "\n".join(log_ctx.output)
        for forbidden in ("arn:aws", "GoldenGateMonitorReadRole-dev", "i-0123456789abcdef",
                         "goldengate-dev.svc.cluster.local", "EXTORA1", "/mnt/secrets-store",
                         "Traceback", "AccessDenied"):
            self.assertNotIn(forbidden, combined)

    def test_failure_log_contains_only_the_documented_safe_fields(self):
        cw = self._failing_cw("boom")
        metric_data = [{"MetricName": "AbendState", "Dimensions": [], "Value": 0.0, "Unit": "Count"}]
        with self.assertLogs(core.logger, level="ERROR") as log_ctx:
            core.publish_metric_batch(cw, metric_data, pipeline="gg-oracle-payments-01")
        record = json.loads(log_ctx.records[0].getMessage())
        self.assertEqual(record["event"], "cloudwatch_put_metric_data_failed")
        self.assertEqual(record["deployment"], "gg-oracle-payments-01")
        self.assertEqual(record["metricCount"], 1)
        self.assertEqual(record["batchIndex"], 0)
        self.assertEqual(record["batchCount"], 1)
        self.assertEqual(record["errorCategory"], "RuntimeError")
        self.assertEqual(set(record.keys()),
                         {"event", "deployment", "metricCount", "batchIndex", "batchCount", "errorCategory"})

    def test_failure_in_one_batch_does_not_stop_remaining_batches(self):
        cw = MagicMock()
        cw.put_metric_data.side_effect = [RuntimeError("boom"), None]
        metric_data = [{"MetricName": "AbendState", "Dimensions": [{"Name": "Process", "Value": f"P{i}"}],
                        "Value": 0.0, "Unit": "Count"} for i in range(25)]
        with self.assertLogs(core.logger, level="ERROR"):
            core.publish_metric_batch(cw, metric_data, pipeline="gg-x")
        self.assertEqual(cw.put_metric_data.call_count, 2)

    def test_no_raw_exception_logging_helper_used_in_source(self):
        import inspect
        src = inspect.getsource(core.publish_metric_batch)
        self.assertNotIn("logger.exception", src)
        self.assertNotIn("exc_info=True", src)


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

    def _run_tick(self, leader=True, fence_write=False, cloudwatch_enabled=True, raise_on_fetch=False,
                 client_construction_fails=False):
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

        def fake_cw_client():
            cw_client_calls.append(1)
            if client_construction_fails:
                raise RuntimeError(
                    "arn:aws:sts::668311715351:assumed-role/GoldenGateMonitorReadRole-dev/i-0123456789abcdef "
                    "AccessDenied for host gg-oracle-payments-01.goldengate-dev.svc.cluster.local "
                    "process EXTORA1 secret=/mnt/secrets-store/dev-goldengate-source-admin")
            return MagicMock()

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
                     mock.patch.object(core, "_cloudwatch_client", side_effect=fake_cw_client), \
                     mock.patch.object(core, "publish_metric_batch",
                                       side_effect=lambda cw, md, pipeline=None: publish_calls.append(md)):
                    with self.assertLogs(core.logger, level="INFO") as log_ctx:
                        core.polling_loop(self.DEPLOYMENT, table, mgr, state, stop_event)
            finally:
                core.CLOUDWATCH_PUBLISH_ENABLED = False

        return publish_calls, cw_client_calls, table, log_ctx

    def test_heartbeat_emitted_after_successful_up_write(self):
        publish_calls, cw_calls, _table, _log = self._run_tick(
            leader=True, fence_write=False, cloudwatch_enabled=True)
        self.assertEqual(len(publish_calls), 1)
        self.assertIn("HeartbeatAgeSeconds", [m["MetricName"] for m in publish_calls[0]])
        self.assertEqual(len(cw_calls), 1)

    def test_heartbeat_emitted_after_successful_deployment_down_write(self):
        publish_calls, cw_calls, _table, _log = self._run_tick(
            leader=True, fence_write=False, cloudwatch_enabled=True, raise_on_fetch=True)
        self.assertEqual(len(publish_calls), 1)
        self.assertIn("HeartbeatAgeSeconds", [m["MetricName"] for m in publish_calls[0]])

    def test_standby_emits_no_heartbeat(self):
        publish_calls, cw_calls, _table, _log = self._run_tick(leader=False, cloudwatch_enabled=True)
        self.assertEqual(publish_calls, [])
        self.assertEqual(cw_calls, [])

    def test_failed_state_write_emits_no_heartbeat(self):
        publish_calls, cw_calls, _table, _log = self._run_tick(
            leader=True, fence_write=True, cloudwatch_enabled=True)
        self.assertEqual(publish_calls, [])
        self.assertEqual(cw_calls, [])

    def test_no_cloudwatch_client_while_disabled(self):
        publish_calls, cw_calls, _table, _log = self._run_tick(
            leader=True, fence_write=False, cloudwatch_enabled=False)
        self.assertEqual(publish_calls, [])
        self.assertEqual(cw_calls, [])

    def test_client_construction_failure_never_raises_and_publishes_nothing(self):
        publish_calls, cw_calls, _table, _log = self._run_tick(
            leader=True, fence_write=False, cloudwatch_enabled=True, client_construction_fails=True)
        self.assertEqual(len(cw_calls), 1)
        self.assertEqual(publish_calls, [])  # publish_metric_batch never reached

    def test_client_construction_failure_leaves_state_deployment_write_successful(self):
        publish_calls, cw_calls, table, _log = self._run_tick(
            leader=True, fence_write=False, cloudwatch_enabled=True, client_construction_fails=True)
        deployment_writes = [c for c in table.update_item.call_args_list
                             if c.kwargs["Key"].get("recordType") == "STATE#_deployment"]
        self.assertEqual(len(deployment_writes), 1)

    def test_client_construction_failure_does_not_produce_outer_tick_failed_log(self):
        _publish_calls, _cw_calls, _table, log_ctx = self._run_tick(
            leader=True, fence_write=False, cloudwatch_enabled=True, client_construction_fails=True)
        combined = "\n".join(log_ctx.output)
        self.assertNotIn("tick failed for", combined)
        self.assertNotIn("Traceback", combined)

    def test_client_construction_failure_log_contains_only_documented_safe_keys(self):
        _publish_calls, _cw_calls, _table, log_ctx = self._run_tick(
            leader=True, fence_write=False, cloudwatch_enabled=True, client_construction_fails=True)
        matches = [r for r in log_ctx.records if "cloudwatch_client_creation_failed" in r.getMessage()]
        self.assertEqual(len(matches), 1)
        record = json.loads(matches[0].getMessage())
        self.assertEqual(record["event"], "cloudwatch_client_creation_failed")
        self.assertEqual(record["deployment"], "gg-oracle-payments-01")
        self.assertEqual(record["errorCategory"], "RuntimeError")
        self.assertEqual(set(record.keys()), {"event", "deployment", "errorCategory"})

    def test_client_construction_failure_log_contains_no_raw_internal_detail(self):
        # Scoped to the cloudwatch_client_creation_failed record itself --
        # unrelated startup INFO logging legitimately includes the
        # deployment's own admin hostname and is not part of this check.
        _publish_calls, _cw_calls, _table, log_ctx = self._run_tick(
            leader=True, fence_write=False, cloudwatch_enabled=True, client_construction_fails=True)
        matches = [r for r in log_ctx.records if "cloudwatch_client_creation_failed" in r.getMessage()]
        self.assertEqual(len(matches), 1)
        failure_message = matches[0].getMessage()
        for forbidden in ("arn:aws", "GoldenGateMonitorReadRole-dev", "i-0123456789abcdef",
                         "goldengate-dev.svc.cluster.local", "EXTORA1", "/mnt/secrets-store",
                         "AccessDenied", "Traceback"):
            self.assertNotIn(forbidden, failure_message)


class CriticalServiceResolutionTests(unittest.TestCase):
    """health_rules.resolve_critical_services: manager-compatible default
    (adminsrvr/distsrvr/recvsrvr for every deployment, regardless of type)
    with a bounded, fail-safe CONFIG.criticalServices override. Purely a
    pure-function unit-test layer -- see CriticalServiceCoverageTests below
    for the end-to-end polling_loop/build_metric_batch/STATE#_deployment
    proof."""

    def test_recognized_set_is_exactly_the_manager_default(self):
        self.assertEqual(gh.RECOGNIZED_CRITICAL_SERVICES, ("adminsrvr", "distsrvr", "recvsrvr"))

    def test_missing_override_defaults_to_all_three(self):
        self.assertEqual(gh.resolve_critical_services(None), ["adminsrvr", "distsrvr", "recvsrvr"])

    def test_valid_subset_override_is_honored(self):
        self.assertEqual(gh.resolve_critical_services(["adminsrvr"]), ["adminsrvr"])

    def test_full_override_in_different_order_is_honored_and_order_preserved(self):
        self.assertEqual(
            gh.resolve_critical_services(["recvsrvr", "adminsrvr"]), ["recvsrvr", "adminsrvr"])

    def test_duplicate_entries_are_deduplicated_preserving_first_occurrence_order(self):
        self.assertEqual(
            gh.resolve_critical_services(["adminsrvr", "distsrvr", "adminsrvr"]),
            ["adminsrvr", "distsrvr"])

    def test_unknown_service_name_is_dropped_not_passed_through(self):
        self.assertEqual(gh.resolve_critical_services(["adminsrvr", "not-a-real-service"]), ["adminsrvr"])

    def test_all_unknown_names_fall_back_to_full_default(self):
        self.assertEqual(
            gh.resolve_critical_services(["not-a-real-service", "also-fake"]),
            ["adminsrvr", "distsrvr", "recvsrvr"])

    def test_empty_list_falls_back_to_full_default(self):
        self.assertEqual(gh.resolve_critical_services([]), ["adminsrvr", "distsrvr", "recvsrvr"])

    def test_non_list_values_fall_back_to_full_default(self):
        for bad_value in ("adminsrvr", {"adminsrvr": True}, 123, True, object()):
            with self.subTest(bad_value=bad_value):
                self.assertEqual(gh.resolve_critical_services(bad_value), ["adminsrvr", "distsrvr", "recvsrvr"])

    def test_resolve_config_populates_critical_services_with_default(self):
        cfg = gh.resolve_config({})
        self.assertEqual(cfg["criticalServices"], ["adminsrvr", "distsrvr", "recvsrvr"])

    def test_resolve_config_honors_a_valid_critical_services_override(self):
        cfg = gh.resolve_config({"criticalServices": ["distsrvr"]})
        self.assertEqual(cfg["criticalServices"], ["distsrvr"])

    def test_resolve_config_falls_back_safely_for_malformed_critical_services(self):
        cfg = gh.resolve_config({"criticalServices": "adminsrvr"})
        self.assertEqual(cfg["criticalServices"], ["adminsrvr", "distsrvr", "recvsrvr"])


class CriticalServiceCoverageTests(unittest.TestCase):
    """Phase 4D2 correction: manager-compatible critical-service coverage.
    Every deployment -- Oracle and PostgreSQL alike -- defaults to probing
    the full adminsrvr/distsrvr/recvsrvr set (the manager's own default
    critical-service list, confirmed read-only against the manager
    reference and reimplemented independently here, never copied), with an
    optional bounded CONFIG.criticalServices override. Proven end-to-end
    through polling_loop: probe_critical_services call, the published
    CriticalServiceDown metrics, and the persisted STATE#_deployment write."""

    _UNSET = object()

    def _run_tick(self, deployment_type, critical_services_config=_UNSET, cloudwatch_enabled=True):
        deployment = {
            "name": f"gg-{deployment_type}-payments-01",
            "type": deployment_type,
            "adminHost": f"gg-{deployment_type}-payments-01.goldengate-dev.svc.cluster.local",
            "adminPort": 8443,
            "tlsServerName": f"gg-{deployment_type}-payments-01.goldengate-dev.adcbmis.local",
            "pipeline": "payments-ora-to-pg-001",
        }
        stop_event = threading.Event()

        config_item = {"deploymentType": deployment_type, "checkIntervalSeconds": 0,
                       "alertsEnabled": False, "metricsEnabled": True}
        if critical_services_config is not self._UNSET:
            config_item["criticalServices"] = critical_services_config

        def fake_get_item(Key):
            if Key.get("recordType") == "CONFIG":
                stop_event.set()
                return {"Item": config_item}
            return {"Item": {}}

        table = MagicMock()
        table.get_item.side_effect = fake_get_item

        mgr = MagicMock()
        mgr.renew.return_value = True

        state = core.LeaseState()
        state.set_leader(True)

        publish_calls = []
        probe_calls = []

        def fake_probe(base, opener, critical):
            probe_calls.append(list(critical))
            return {svc: True for svc in critical}

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
                     mock.patch.object(core, "fetch_gg_processes", return_value=[]), \
                     mock.patch.object(core, "_basic_opener", return_value=MagicMock()), \
                     mock.patch.object(core, "_build_ssl_context", return_value=MagicMock()), \
                     mock.patch.object(core, "probe_critical_services", side_effect=fake_probe), \
                     mock.patch.object(core, "_cloudwatch_client", return_value=MagicMock()), \
                     mock.patch.object(core, "publish_metric_batch",
                                       side_effect=lambda cw, md, pipeline=None: publish_calls.append(md)):
                    core.polling_loop(deployment, table, mgr, state, stop_event)
            finally:
                core.CLOUDWATCH_PUBLISH_ENABLED = False

        return publish_calls, probe_calls, table

    def _deployment_write_critical_services(self, table):
        writes = [c for c in table.update_item.call_args_list
                 if c.kwargs["Key"].get("recordType") == "STATE#_deployment"]
        self.assertEqual(len(writes), 1)
        return writes[0].kwargs["ExpressionAttributeValues"][":cs"]

    def test_oracle_default_service_set_is_exactly_three(self):
        _publish_calls, probe_calls, _table = self._run_tick("oracle")
        self.assertEqual(probe_calls, [["adminsrvr", "distsrvr", "recvsrvr"]])

    def test_postgresql_default_service_set_is_exactly_three(self):
        _publish_calls, probe_calls, _table = self._run_tick("postgresql")
        self.assertEqual(probe_calls, [["adminsrvr", "distsrvr", "recvsrvr"]])

    def test_oracle_three_critical_service_down_metrics_produced(self):
        publish_calls, _probe_calls, _table = self._run_tick("oracle")
        names = [m["Dimensions"][-1]["Value"] for m in publish_calls[0] if m["MetricName"] == "CriticalServiceDown"]
        self.assertEqual(sorted(names), ["adminsrvr", "distsrvr", "recvsrvr"])

    def test_postgresql_three_critical_service_down_metrics_produced(self):
        publish_calls, _probe_calls, _table = self._run_tick("postgresql")
        names = [m["Dimensions"][-1]["Value"] for m in publish_calls[0] if m["MetricName"] == "CriticalServiceDown"]
        self.assertEqual(sorted(names), ["adminsrvr", "distsrvr", "recvsrvr"])

    def test_oracle_state_deployment_contains_all_three_service_entries(self):
        _publish_calls, _probe_calls, table = self._run_tick("oracle")
        cs = self._deployment_write_critical_services(table)
        self.assertEqual(set(cs.keys()), {"adminsrvr", "distsrvr", "recvsrvr"})

    def test_postgresql_state_deployment_contains_all_three_service_entries(self):
        _publish_calls, _probe_calls, table = self._run_tick("postgresql")
        cs = self._deployment_write_critical_services(table)
        self.assertEqual(set(cs.keys()), {"adminsrvr", "distsrvr", "recvsrvr"})

    def test_reachable_booleans_remain_literal_fail_closed(self):
        _publish_calls, _probe_calls, table = self._run_tick("oracle")
        cs = self._deployment_write_critical_services(table)
        for entry in cs.values():
            self.assertIs(entry["reachable"], True)

    def test_valid_config_subset_override_is_honored(self):
        _publish_calls, probe_calls, _table = self._run_tick("oracle", critical_services_config=["adminsrvr"])
        self.assertEqual(probe_calls, [["adminsrvr"]])

    def test_duplicate_override_names_are_deduplicated(self):
        _publish_calls, probe_calls, _table = self._run_tick(
            "oracle", critical_services_config=["adminsrvr", "distsrvr", "adminsrvr"])
        self.assertEqual(probe_calls, [["adminsrvr", "distsrvr"]])

    def test_unknown_service_name_in_override_is_dropped_never_probed(self):
        _publish_calls, probe_calls, _table = self._run_tick(
            "oracle", critical_services_config=["adminsrvr", "not-a-real-service"])
        self.assertEqual(probe_calls, [["adminsrvr"]])

    def test_malformed_override_falls_back_to_full_default(self):
        for bad_override in ("adminsrvr", {"adminsrvr": True}, 123, None, []):
            with self.subTest(bad_override=bad_override):
                _publish_calls, probe_calls, _table = self._run_tick(
                    "oracle", critical_services_config=bad_override)
                self.assertEqual(probe_calls, [["adminsrvr", "distsrvr", "recvsrvr"]])

    def test_unknown_override_never_creates_arbitrary_service_metric_dimension(self):
        publish_calls, probe_calls, _table = self._run_tick(
            "oracle", critical_services_config=["adminsrvr", "not-a-real-service"])
        self.assertEqual(probe_calls, [["adminsrvr"]])  # unknown name never even probed
        names = [m["Dimensions"][-1]["Value"] for m in publish_calls[0] if m["MetricName"] == "CriticalServiceDown"]
        self.assertNotIn("not-a-real-service", names)
        self.assertEqual(names, ["adminsrvr"])

    def test_no_kubernetes_healing_restart_or_fencing_action_introduced(self):
        import inspect
        src = inspect.getsource(core.probe_critical_services)
        for forbidden in ("kubernetes", "restart", "kubectl", "fence", "heal"):
            self.assertNotIn(forbidden, src.lower())


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
            inventory, {f"{self.BASE}/services/v2/mpoints/OK1/processPerformance": {"response": {"cpuTimeUs": 1}},
                       f"{self.BASE}/services/v2/mpoints/OK1/serviceHealth": {"response": {"isHealthy": True}}})
        result = core.collect_pms(self.BASE, opener)
        self.assertEqual(result["inventoryCount"], 7)
        self.assertEqual(result["followedCount"], 1)
        self.assertEqual(result["successCount"], 1)

    def test_partial_per_process_failure_continues_remaining(self):
        inventory = self._inventory(["P1", "P2"])
        detail = {f"{self.BASE}/services/v2/mpoints/P1/processPerformance": {"response": {"cpuTimeUs": 1}},
                 f"{self.BASE}/services/v2/mpoints/P1/serviceHealth": {"response": {"isHealthy": True}},
                 f"{self.BASE}/services/v2/mpoints/P2/serviceHealth": {"response": {"isHealthy": True}}}
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


class PmsResponseShapeValidationTests(unittest.TestCase):
    """Section 3/4 correction: a structurally invalid inventory or detail
    response must never be silently accepted as healthy."""

    BASE = "https://gg-test:8443"

    def _opener(self, responses):
        def _open(url, timeout=5):
            m = MagicMock()
            m.read.return_value = json.dumps(responses[url]).encode()
            m.__enter__.return_value = m
            m.__exit__.return_value = False
            return m
        o = MagicMock()
        o.open.side_effect = _open
        return o

    def test_genuine_empty_inventory_list_is_ok(self):
        o = self._opener({f"{self.BASE}{core.PMS_INVENTORY_PATH}": {"response": {"processes": []}}})
        result = core.collect_pms(self.BASE, o)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["inventoryCount"], 0)
        self.assertEqual(result["followedCount"], 0)

    def test_missing_response_is_invalid_response(self):
        o = self._opener({f"{self.BASE}{core.PMS_INVENTORY_PATH}": {}})
        result = core.collect_pms(self.BASE, o)
        self.assertEqual(result["status"], "INVALID_RESPONSE")

    def test_non_dict_response_is_invalid_response(self):
        for bad in (None, {}, {"response": None}, {"response": "invalid"}, {"response": []}):
            o = self._opener({f"{self.BASE}{core.PMS_INVENTORY_PATH}": bad})
            result = core.collect_pms(self.BASE, o)
            self.assertEqual(result["status"], "INVALID_RESPONSE", f"payload={bad!r}")

    def test_non_list_processes_is_invalid_response(self):
        for bad_processes in ({}, "invalid", 42, None):
            o = self._opener({f"{self.BASE}{core.PMS_INVENTORY_PATH}": {"response": {"processes": bad_processes}}})
            result = core.collect_pms(self.BASE, o)
            self.assertEqual(result["status"], "INVALID_RESPONSE", f"processes={bad_processes!r}")

    def test_top_level_list_is_invalid_response(self):
        o = self._opener({f"{self.BASE}{core.PMS_INVENTORY_PATH}": [1, 2, 3]})
        result = core.collect_pms(self.BASE, o)
        self.assertEqual(result["status"], "INVALID_RESPONSE")

    def test_invalid_inventory_never_raises_and_never_logs_payload(self):
        o = self._opener({f"{self.BASE}{core.PMS_INVENTORY_PATH}": []})
        try:
            result = core.collect_pms(self.BASE, o)
        except Exception as e:  # pragma: no cover
            self.fail(f"collect_pms raised: {e!r}")
        self.assertIsInstance(result, dict)

    def _detail_shape_opener(self, inventory, perf_response, health_response):
        responses = {
            f"{self.BASE}{core.PMS_INVENTORY_PATH}": inventory,
            f"{self.BASE}/services/v2/mpoints/P1/processPerformance": {"response": perf_response},
            f"{self.BASE}/services/v2/mpoints/P1/serviceHealth": {"response": health_response},
        }
        return self._opener(responses)

    def _single_process_inventory(self):
        return {"response": {"processes": [{"processName": "P1"}]}}

    def test_non_dict_process_performance_response_fails(self):
        for bad in (None, "invalid", 42, [1, 2]):
            o = self._detail_shape_opener(self._single_process_inventory(), bad, {"isHealthy": True})
            result = core.collect_pms(self.BASE, o)
            self.assertEqual(result["failureCount"], 1, f"perf={bad!r}")
            self.assertEqual(result["processes"]["P1"]["performance"], {})

    def test_empty_process_performance_response_fails(self):
        o = self._detail_shape_opener(self._single_process_inventory(), {}, {"isHealthy": True})
        result = core.collect_pms(self.BASE, o)
        self.assertEqual(result["failureCount"], 1)
        self.assertEqual(result["processes"]["P1"]["performance"], {})

    def test_non_dict_service_health_response_fails(self):
        for bad in (None, "invalid", 42, [1, 2]):
            o = self._detail_shape_opener(self._single_process_inventory(), {"cpuTimeUs": 1}, bad)
            result = core.collect_pms(self.BASE, o)
            self.assertEqual(result["failureCount"], 1, f"health={bad!r}")
            self.assertEqual(result["processes"]["P1"]["serviceHealth"], {})

    def test_empty_service_health_response_fails(self):
        o = self._detail_shape_opener(self._single_process_inventory(), {"cpuTimeUs": 1}, {})
        result = core.collect_pms(self.BASE, o)
        self.assertEqual(result["failureCount"], 1)
        self.assertEqual(result["processes"]["P1"]["serviceHealth"], {})

    def test_service_health_missing_isHealthy_fails_even_with_other_fields(self):
        # Under the tightened rule, isHealthy specifically must be a literal
        # bool -- merely having criticalResourcesHealthy/Unhealthy present
        # (the old, looser "any of 3 fields" rule) is no longer sufficient.
        o = self._detail_shape_opener(
            self._single_process_inventory(), {"cpuTimeUs": 1},
            {"criticalResourcesHealthy": 3, "criticalResourcesUnhealthy": 0})
        result = core.collect_pms(self.BASE, o)
        self.assertEqual(result["failureCount"], 1)
        self.assertEqual(result["processes"]["P1"]["serviceHealth"], {})

    def test_service_health_non_boolean_isHealthy_fails(self):
        for bad_is_healthy in ("true", 1, 0, None, [], {}):
            o = self._detail_shape_opener(
                self._single_process_inventory(), {"cpuTimeUs": 1}, {"isHealthy": bad_is_healthy})
            result = core.collect_pms(self.BASE, o)
            self.assertEqual(result["failureCount"], 1, f"isHealthy={bad_is_healthy!r}")
            self.assertEqual(result["processes"]["P1"]["serviceHealth"], {})

    def test_service_health_literal_boolean_isHealthy_succeeds(self):
        o = self._detail_shape_opener(self._single_process_inventory(), {"cpuTimeUs": 1}, {"isHealthy": False})
        result = core.collect_pms(self.BASE, o)
        self.assertEqual(result["successCount"], 1)
        self.assertEqual(result["processes"]["P1"]["serviceHealth"]["isHealthy"], False)

    def test_malformed_details_never_increment_full_success(self):
        o = self._detail_shape_opener(self._single_process_inventory(), {}, {})
        result = core.collect_pms(self.BASE, o)
        self.assertEqual(result["successCount"], 0)
        self.assertEqual(result["status"], "UNAVAILABLE")


class PmsPartialUnavailableSemanticsTests(unittest.TestCase):
    """Section 5 correction: status is derived from whether any individual
    detail GET succeeded this tick -- not merely from whether some single
    process got BOTH of its details."""

    BASE = "https://gg-test:8443"

    def _opener(self, inventory, detail_responses, detail_exceptions=None):
        detail_exceptions = detail_exceptions or {}

        def _open(url, timeout=5):
            if url in detail_exceptions:
                raise detail_exceptions[url]
            body = inventory if url == f"{self.BASE}{core.PMS_INVENTORY_PATH}" else detail_responses[url]
            m = MagicMock()
            m.read.return_value = json.dumps(body).encode()
            m.__enter__.return_value = m
            m.__exit__.return_value = False
            return m
        o = MagicMock()
        o.open.side_effect = _open
        return o

    def test_one_success_one_failure_produces_partial(self):
        inventory = {"response": {"processes": [{"processName": "P1"}]}}
        detail = {f"{self.BASE}/services/v2/mpoints/P1/processPerformance": {"response": {"cpuTimeUs": 1}}}
        exceptions = {f"{self.BASE}/services/v2/mpoints/P1/serviceHealth": RuntimeError("boom")}
        o = self._opener(inventory, detail, exceptions)
        result = core.collect_pms(self.BASE, o)
        self.assertEqual(result["status"], "PARTIAL")

    def test_every_process_one_success_one_failure_remains_partial_not_unavailable(self):
        inventory = {"response": {"processes": [{"processName": "P1"}, {"processName": "P2"}]}}
        detail = {
            f"{self.BASE}/services/v2/mpoints/P1/processPerformance": {"response": {"cpuTimeUs": 1}},
            f"{self.BASE}/services/v2/mpoints/P2/serviceHealth": {"response": {"isHealthy": True}},
        }
        exceptions = {
            f"{self.BASE}/services/v2/mpoints/P1/serviceHealth": RuntimeError("boom"),
            f"{self.BASE}/services/v2/mpoints/P2/processPerformance": RuntimeError("boom"),
        }
        o = self._opener(inventory, detail, exceptions)
        result = core.collect_pms(self.BASE, o)
        # process-level successCount is 0 (neither process got BOTH details)
        # -- but real usable data WAS collected, so this must be PARTIAL.
        self.assertEqual(result["successCount"], 0)
        self.assertEqual(result["status"], "PARTIAL")

    def test_zero_successful_detail_requests_produces_unavailable(self):
        inventory = {"response": {"processes": [{"processName": "P1"}, {"processName": "P2"}]}}
        exceptions = {
            f"{self.BASE}/services/v2/mpoints/P1/processPerformance": RuntimeError("boom"),
            f"{self.BASE}/services/v2/mpoints/P1/serviceHealth": RuntimeError("boom"),
            f"{self.BASE}/services/v2/mpoints/P2/processPerformance": RuntimeError("boom"),
            f"{self.BASE}/services/v2/mpoints/P2/serviceHealth": RuntimeError("boom"),
        }
        o = self._opener(inventory, {}, exceptions)
        result = core.collect_pms(self.BASE, o)
        self.assertEqual(result["status"], "UNAVAILABLE")

    def test_all_detail_requests_succeed_produces_ok(self):
        inventory = {"response": {"processes": [{"processName": "P1"}, {"processName": "P2"}]}}
        detail = {
            f"{self.BASE}/services/v2/mpoints/P1/processPerformance": {"response": {"cpuTimeUs": 1}},
            f"{self.BASE}/services/v2/mpoints/P1/serviceHealth": {"response": {"isHealthy": True}},
            f"{self.BASE}/services/v2/mpoints/P2/processPerformance": {"response": {"cpuTimeUs": 2}},
            f"{self.BASE}/services/v2/mpoints/P2/serviceHealth": {"response": {"isHealthy": False}},
        }
        o = self._opener(inventory, detail)
        result = core.collect_pms(self.BASE, o)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["successCount"], 2)
        self.assertEqual(result["failureCount"], 0)


class PmsProcessNameBoundsTests(unittest.TestCase):
    """Section 6 correction: process names are validated (length, control
    characters, '.'/'..' ) before ever being followed, and are preserved
    EXACTLY (never rewritten/truncated) when accepted."""

    def test_name_longer_than_limit_skipped(self):
        overlong = "P" * (core.MAX_PMS_PROCESS_NAME_LENGTH + 1)
        self.assertIsNone(core._valid_pms_process_name(overlong))

    def test_whitespace_only_name_skipped(self):
        self.assertIsNone(core._valid_pms_process_name("   "))
        self.assertIsNone(core._valid_pms_process_name("\t\n"))

    def test_control_character_name_skipped(self):
        self.assertIsNone(core._valid_pms_process_name("EXT\x00RACT"))
        self.assertIsNone(core._valid_pms_process_name("EXT\x7fRACT"))

    def test_dot_and_dotdot_names_skipped(self):
        self.assertIsNone(core._valid_pms_process_name("."))
        self.assertIsNone(core._valid_pms_process_name(".."))

    def test_maximum_length_valid_name_accepted(self):
        exact = "P" * core.MAX_PMS_PROCESS_NAME_LENGTH
        self.assertEqual(core._valid_pms_process_name(exact), exact)

    def test_accepted_name_never_rewritten(self):
        name = "  EXTRACT_01  "  # has non-whitespace content -- must survive exactly
        self.assertEqual(core._valid_pms_process_name(name), name)

    def test_invalid_names_never_appear_as_processes_map_keys(self):
        payload = {"response": {"processes": [
            {"processName": "." }, {"processName": ".."},
            {"processName": "P" * (core.MAX_PMS_PROCESS_NAME_LENGTH + 1)},
            {"processName": "\x00BAD"}, {"processName": "   "},
            {"processName": "GOOD1"},
        ]}}
        names, count = core._pms_valid_process_names(payload)
        self.assertEqual(names, ["GOOD1"])
        self.assertEqual(count, 6)

    def test_duplicate_accepted_names_deduplicated_first_seen_order(self):
        payload = {"response": {"processes": [
            {"processName": "B"}, {"processName": "A"}, {"processName": "B"}, {"processName": "A"},
        ]}}
        names, count = core._pms_valid_process_names(payload)
        self.assertEqual(names, ["B", "A"])
        self.assertEqual(count, 4)

    def test_unpaired_high_surrogate_rejected(self):
        # \ud800 is a valid Python str character (json.loads tolerates a
        # lone \uD800 escape) but cannot be UTF-8 encoded.
        self.assertIsNone(core._valid_pms_process_name("\ud800"))
        self.assertIsNone(core._valid_pms_process_name("EXTRACT_\ud800_01"))

    def test_unpaired_low_surrogate_rejected(self):
        self.assertIsNone(core._valid_pms_process_name("\udc00"))
        self.assertIsNone(core._valid_pms_process_name("EXTRACT_\udfff_01"))

    def test_normal_non_ascii_unicode_name_accepted_and_url_encoded(self):
        name = "EXTRACT_Café_日本語"
        self.assertEqual(core._valid_pms_process_name(name), name)
        path = core._pms_detail_path(name, "processPerformance")
        self.assertIsNotNone(path)
        encoded_segment = path.split("/")[4]
        self.assertEqual(urllib.parse.unquote(encoded_segment), name)


class PmsSnapshotSizeBudgetTests(unittest.TestCase):
    """Section 6 required proof: the maximum permitted bounded PMS snapshot
    (20 processes, maximum-length names, all confirmed fields populated)
    stays comfortably below DynamoDB's 400 KB item-size limit."""

    def test_maximum_snapshot_stays_below_size_budget(self):
        max_name = "P" * core.MAX_PMS_PROCESS_NAME_LENGTH
        performance = {k: 123456789 for k in core._PMS_PERFORMANCE_NUMERIC_FIELDS}
        service_health = {"isHealthy": True, "criticalResourcesHealthy": 5, "criticalResourcesUnhealthy": 0}
        processes = {
            f"{max_name}-{i:02d}": {"performance": dict(performance), "serviceHealth": dict(service_health),
                                    "heartbeatAgeSeconds": 9999}
            for i in range(core.MAX_FOLLOWED_PMS_PROCESSES)
        }
        snapshot = {
            "status": "OK", "collectedAt": 1785000000,
            "inventoryCount": core.MAX_FOLLOWED_PMS_PROCESSES, "followedCount": core.MAX_FOLLOWED_PMS_PROCESSES,
            "successCount": core.MAX_FOLLOWED_PMS_PROCESSES, "failureCount": 0,
            "heartbeatAgeSeconds": 9999, "processes": processes,
        }
        size_bytes = len(json.dumps(snapshot).encode("utf-8"))
        # DynamoDB's per-item limit is 400 KB (409,600 bytes). "Comfortably
        # below" -- assert well under 10% of that budget.
        self.assertLess(size_bytes, 40_000, f"PMS snapshot is {size_bytes} bytes")


class PmsNumericHardeningTests(unittest.TestCase):
    """Section 7 correction: _normalize_pms_number must never raise,
    including OverflowError from float(huge_int), and must reject anything
    outside the documented DynamoDB-safe range."""

    def test_huge_integer_becomes_zero(self):
        self.assertEqual(core._normalize_pms_number(10 ** 400), 0)

    def test_huge_numeric_string_becomes_zero(self):
        self.assertEqual(core._normalize_pms_number("1" + "0" * 400), 0)

    def test_scientific_notation_overflow_string_becomes_zero(self):
        self.assertEqual(core._normalize_pms_number("1e1000"), 0)

    def test_overflow_error_path_never_raises(self):
        try:
            result = core._normalize_pms_number(10 ** 5000)
        except Exception as e:  # pragma: no cover
            self.fail(f"_normalize_pms_number raised: {e!r}")
        self.assertEqual(result, 0)

    def test_exact_maximum_accepted_boundary(self):
        self.assertEqual(core._normalize_pms_number(core.PMS_MAX_SAFE_NUMBER), core.PMS_MAX_SAFE_NUMBER)

    def test_one_value_above_boundary_becomes_zero(self):
        self.assertEqual(core._normalize_pms_number(core.PMS_MAX_SAFE_NUMBER + 1), 0)

    def test_normal_cumulative_counter_preserved(self):
        self.assertEqual(core._normalize_pms_number(123456789), 123456789)

    def test_10_pow_10000_returns_zero_without_raising(self):
        try:
            result = core._normalize_pms_number(10 ** 10000)
        except Exception as e:  # pragma: no cover
            self.fail(f"_normalize_pms_number raised: {e!r}")
        self.assertEqual(result, 0)

    def test_1e300_above_dynamodb_safe_bound_returns_zero(self):
        self.assertEqual(core._normalize_pms_number(1e300), 0)

    def test_bound_is_no_more_than_38_decimal_digits(self):
        self.assertLessEqual(len(str(core.PMS_MAX_SAFE_NUMBER)), 38)


class PmsCollectionBudgetTests(unittest.TestCase):
    """Section 6: a fixed, non-operator-tunable total-time safety net so PMS
    can never make a healthy deployment appear stale before
    STATE#_deployment is written."""

    BASE = "https://gg-test:8443"

    def _opener(self, responses, record=None):
        record = record if record is not None else []

        def _open(url, timeout=5):
            record.append((url, timeout))
            m = MagicMock()
            m.read.return_value = json.dumps(responses[url]).encode()
            m.__enter__.return_value = m
            m.__exit__.return_value = False
            return m
        o = MagicMock()
        o.open.side_effect = _open
        return o, record

    def _all_ok_responses(self, names):
        responses = {f"{self.BASE}{core.PMS_INVENTORY_PATH}":
                    {"response": {"processes": [{"processName": n} for n in names]}}}
        for n in names:
            responses[f"{self.BASE}/services/v2/mpoints/{n}/processPerformance"] = {"response": {"cpuTimeUs": 1}}
            responses[f"{self.BASE}/services/v2/mpoints/{n}/serviceHealth"] = {"response": {"isHealthy": True}}
        return responses

    def _stepping_clock(self, step=3.0, start=0.0):
        state = {"t": start}

        def _clock():
            state["t"] += step
            return state["t"]
        return _clock

    def test_budget_exhaustion_stops_further_requests(self):
        names = [f"P{i}" for i in range(20)]
        opener, calls = self._opener(self._all_ok_responses(names))
        clock = self._stepping_clock(step=3.0)
        result = core.collect_pms(self.BASE, opener, clock=clock, budget_seconds=10)
        # far fewer than the theoretical max of 1 + 20*2 = 41 requests
        self.assertLess(len(calls), 41)

    def test_per_request_timeout_never_exceeds_remaining_budget(self):
        names = ["P1", "P2", "P3"]
        opener, calls = self._opener(self._all_ok_responses(names))
        clock = self._stepping_clock(step=4.0)
        core.collect_pms(self.BASE, opener, clock=clock, budget_seconds=10)
        for _url, timeout in calls:
            self.assertLessEqual(timeout, core.PMS_REQUEST_TIMEOUT_SECONDS)
            self.assertGreater(timeout, 0)

    def test_partial_results_survive_budget_exhaustion(self):
        names = [f"P{i}" for i in range(20)]
        opener, calls = self._opener(self._all_ok_responses(names))
        clock = self._stepping_clock(step=3.0)
        result = core.collect_pms(self.BASE, opener, clock=clock, budget_seconds=10)
        # at least the inventory + one process's data was preserved
        self.assertGreaterEqual(len(result["processes"]), 1)
        self.assertEqual(result["status"], "PARTIAL")

    def test_no_detail_success_before_exhaustion_produces_unavailable(self):
        opener = MagicMock()
        calls = {"n": 0}

        def fake_clock():
            calls["n"] += 1
            return 0.0 if calls["n"] == 1 else 1000.0

        result = core.collect_pms(self.BASE, opener, clock=fake_clock, budget_seconds=1)
        self.assertEqual(result["status"], "UNAVAILABLE")
        opener.open.assert_not_called()

    def test_ample_budget_collects_everything_normally(self):
        names = ["P1", "P2", "P3"]
        opener, calls = self._opener(self._all_ok_responses(names))
        result = core.collect_pms(self.BASE, opener, budget_seconds=30)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["successCount"], 3)
        detail_calls = [c for c in calls if c[0] != f"{self.BASE}{core.PMS_INVENTORY_PATH}"]
        self.assertEqual(len(detail_calls), 6)

    def test_budget_constants_are_fixed_and_conservative(self):
        self.assertEqual(core.PMS_REQUEST_TIMEOUT_SECONDS, 2)
        self.assertEqual(core.PMS_COLLECTION_BUDGET_SECONDS, 30)
        # theoretical worst case must stay comfortably under the deployed
        # 120s stale threshold once the budget (not the per-request
        # timeout) is the binding constraint.
        self.assertLess(core.PMS_COLLECTION_BUDGET_SECONDS, 120)


class PmsWrappedTlsClassificationTests(unittest.TestCase):
    """Section 10 correction: TLS failures classify correctly regardless of
    how deeply they are wrapped, without importing tools/."""

    def test_direct_ssl_error(self):
        self.assertEqual(core._classify_pms_error(ssl.SSLError("bad cert")), "TLS_FAILED")

    def test_direct_ssl_cert_verification_error(self):
        self.assertEqual(core._classify_pms_error(ssl.SSLCertVerificationError("x")), "TLS_FAILED")

    def test_urlerror_wrapping_ssl_cert_verification_error(self):
        wrapped = urllib.error.URLError(ssl.SSLCertVerificationError("x"))
        self.assertEqual(core._classify_pms_error(wrapped), "TLS_FAILED")

    def test_urlerror_wrapping_connection_refused_is_endpoint_unavailable(self):
        wrapped = urllib.error.URLError(ConnectionRefusedError("refused"))
        self.assertEqual(core._classify_pms_error(wrapped), "ENDPOINT_UNAVAILABLE")

    def test_nested_cause_chain(self):
        try:
            try:
                raise ssl.SSLCertVerificationError("cert bad")
            except ssl.SSLCertVerificationError as inner:
                raise RuntimeError("conn failed") from inner
        except RuntimeError as outer:
            self.assertEqual(core._classify_pms_error(outer), "TLS_FAILED")

    def test_nested_context_chain_without_explicit_cause(self):
        try:
            try:
                raise ssl.SSLError("bad handshake")
            except ssl.SSLError:
                raise urllib.error.URLError("generic failure")
        except urllib.error.URLError as outer:
            self.assertEqual(core._classify_pms_error(outer), "TLS_FAILED")

    def test_cycle_protection_does_not_hang(self):
        a = RuntimeError("a")
        b = RuntimeError("b")
        a.__cause__ = b
        b.__cause__ = a
        self.assertFalse(core._contains_pms_tls_error(a))

    def test_classification_never_includes_exception_text(self):
        exc = ssl.SSLCertVerificationError("[SSL: CERTIFICATE_VERIFY_FAILED] SECRET_HOST_DETAIL_xyz")
        category = core._classify_pms_error(exc)
        self.assertEqual(category, "TLS_FAILED")
        self.assertNotIn("SECRET_HOST_DETAIL_xyz", category)


class PmsSurrogateAndDefensiveBoundaryTests(unittest.TestCase):
    """Reproduces and fixes the reported defect: a processName containing
    an unpaired Unicode surrogate must never reach urllib.parse.quote (and
    if it somehow did, must never let UnicodeEncodeError escape
    collect_pms), and no unanticipated internal failure may ever escape
    collect_pms as a whole."""

    BASE = "https://gg-test:8443"

    def _opener(self, inventory, detail_responses=None):
        detail_responses = detail_responses or {}

        def _open(url, timeout=5):
            body = inventory if url == f"{self.BASE}{core.PMS_INVENTORY_PATH}" else detail_responses[url]
            m = MagicMock()
            m.read.return_value = json.dumps(body).encode()
            m.__enter__.return_value = m
            m.__exit__.return_value = False
            return m
        o = MagicMock()
        o.open.side_effect = _open
        return o

    def test_surrogate_name_causes_zero_detail_requests_and_never_stored(self):
        bad_name = "\ud800"
        inventory = {"response": {"processes": [{"processName": bad_name}, {"processName": "GOOD1"}]}}
        detail = {
            f"{self.BASE}/services/v2/mpoints/GOOD1/processPerformance": {"response": {"cpuTimeUs": 1}},
            f"{self.BASE}/services/v2/mpoints/GOOD1/serviceHealth": {"response": {"isHealthy": True}},
        }
        opener = self._opener(inventory, detail)
        result = core.collect_pms(self.BASE, opener)
        self.assertEqual(result["followedCount"], 1)
        self.assertNotIn(bad_name, result["processes"])
        self.assertIn("GOOD1", result["processes"])

    def test_forced_unicode_encode_error_in_path_construction_cannot_escape(self):
        opener = self._opener({"response": {"processes": [{"processName": "OK1"}]}})
        with mock.patch.object(core.urllib.parse, "quote", side_effect=UnicodeEncodeError(
                "utf-8", "x", 0, 1, "surrogates not allowed")):
            try:
                result = core.collect_pms(self.BASE, opener)
            except Exception as e:  # pragma: no cover
                self.fail(f"collect_pms raised: {e!r}")
        self.assertIsInstance(result, dict)
        # OK1 was still followed (a real, valid name) -- its path just
        # couldn't be built this tick, so no data was collected for it, but
        # it is not silently dropped from the map, and no exception escaped.
        self.assertEqual(result["processes"]["OK1"], {
            "performance": {}, "serviceHealth": {}, "heartbeatAgeSeconds": None})
        self.assertEqual(result["failureCount"], 1)

    def test_forced_unexpected_internal_exception_cannot_escape(self):
        opener = self._opener({"response": {"processes": []}})
        with mock.patch.object(core, "_collect_pms_impl", side_effect=RuntimeError("SECRET_INTERNAL_xyz")):
            try:
                result = core.collect_pms(self.BASE, opener)
            except Exception as e:  # pragma: no cover
                self.fail(f"collect_pms raised: {e!r}")
        self.assertEqual(result["status"], "INVALID_RESPONSE")
        self.assertEqual(result["processes"], {})
        self.assertIsNone(result["heartbeatAgeSeconds"])
        self.assertEqual(result["inventoryCount"], 0)
        self.assertEqual(result["followedCount"], 0)

    def test_defensive_result_contains_no_leaks(self):
        opener = self._opener({"response": {"processes": []}})
        with mock.patch.object(
                core, "_collect_pms_impl",
                side_effect=RuntimeError("SECRET_xyz https://internal-host:8443/services/v2/mpoints/PROC1 "
                                         "user=admin ca=/mnt/secrets-store/ca-chain-pem")):
            result = core.collect_pms(self.BASE, opener)
        blob = json.dumps(result)
        self.assertNotIn("SECRET_xyz", blob)
        self.assertNotIn("internal-host", blob)
        self.assertNotIn("PROC1", blob)
        self.assertNotIn("admin", blob)
        self.assertNotIn("secrets-store", blob)
        self.assertNotIn("Traceback", blob)

    def test_defensive_boundary_performs_no_logging(self):
        opener = self._opener({"response": {"processes": []}})
        with mock.patch.object(core, "_collect_pms_impl", side_effect=RuntimeError("boom")):
            with self.assertNoLogs(core.logger):
                core.collect_pms(self.BASE, opener)


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

    def _run_tick(self, leader=True, fence_write=False, pms_side_effect=None, raise_on_fetch=False):
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

            with mock.patch.object(core.cfgmod, "credential_paths", return_value=(user_file, pwd_file)), \
                 mock.patch.object(core, "fetch_gg_processes", side_effect=fake_fetch), \
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

    def test_admin_rest_down_write_overwrites_stale_pms_with_current_unavailable_state(self):
        # collect_pms itself is never called when Admin REST is down (PMS
        # depends on the same connectivity) -- but the write must still
        # carry a CURRENT, sanitized pms snapshot, never leave a prior
        # UP-tick's map silently attached/stale.
        pms_calls, update_calls = self._run_tick(leader=True, fence_write=False, raise_on_fetch=True)
        self.assertEqual(pms_calls, [])
        deployment_writes = [c for c in update_calls if c["Key"].get("recordType") == "STATE#_deployment"]
        self.assertEqual(len(deployment_writes), 1)
        pms_value = deployment_writes[0]["ExpressionAttributeValues"][":pms"]
        self.assertEqual(pms_value["status"], "ENDPOINT_UNAVAILABLE")
        self.assertEqual(pms_value["processes"], {})
        self.assertIsInstance(pms_value["collectedAt"], int)

    def test_unexpected_collect_pms_failure_writes_current_sanitized_state(self):
        def _raise():
            raise RuntimeError("SECRET_INTERNAL_DETAIL_should_not_leak")

        with self.assertLogs(core.logger, level="WARNING") as log_ctx:
            pms_calls, update_calls = self._run_tick(leader=True, fence_write=False, pms_side_effect=_raise)

        deployment_writes = [c for c in update_calls if c["Key"].get("recordType") == "STATE#_deployment"]
        self.assertEqual(len(deployment_writes), 1)
        pms_value = deployment_writes[0]["ExpressionAttributeValues"][":pms"]
        self.assertIn(pms_value["status"], ("UNAVAILABLE", "INVALID_RESPONSE"))
        self.assertEqual(pms_value["processes"], {})

        combined_log = "\n".join(log_ctx.output)
        self.assertIn("gg-oracle-payments-01", combined_log)
        self.assertNotIn("SECRET_INTERNAL_DETAIL_should_not_leak", combined_log)
        self.assertNotIn("Traceback", combined_log)
        self.assertNotIn("RuntimeError", combined_log)

    def test_cloudwatch_client_never_created_during_pms_failure_tick(self):
        def _raise():
            raise RuntimeError("boom")

        with mock.patch("boto3.client") as mock_cw_client:
            self._run_tick(leader=True, fence_write=False, pms_side_effect=_raise)
            mock_cw_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
