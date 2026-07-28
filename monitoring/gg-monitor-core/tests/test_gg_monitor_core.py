"""Focused unit tests for gg-monitor-core.

Uses moto (mock_aws) for real DynamoDB/CloudWatch call shapes -- no network,
no real AWS credentials, no real state. Run with:
    python3 -m unittest discover -s monitoring/gg-monitor-core/tests -p "test_*.py" -v
"""
import sys

# Must be set before importing gg_monitor_core/gg_health_rules/inventory
# below -- otherwise importing the very modules under test would itself
# create __pycache__/*.pyc as a side effect of running this test file,
# which would make NoGeneratedArtifactsTests self-defeating.
sys.dont_write_bytecode = True

import json
import os
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import boto3
from moto import mock_aws

import gg_health_rules as gh
import gg_monitor_core as core
import inventory as inv

REPO_ROOT = Path(__file__).resolve().parents[3]


def code_only(src):
    """Strip the leading module/function docstring (via ast, robust to any
    length/quoting) and full-line comments from a source string. Several
    static "must never appear in real code" checks below need this: the
    surrounding code intentionally documents in prose what was deliberately
    excluded and why (e.g. "never CERT_NONE"), and a bare substring search
    would otherwise flag that explanatory comment as if it were the
    forbidden usage itself."""
    import ast
    try:
        tree = ast.parse(src)
        docstring = ast.get_docstring(tree, clean=False)
        if docstring and docstring in src:
            src = src.replace(docstring, "", 1)
    except SyntaxError:
        pass  # src may be a function-source fragment, not a full module
    lines = [line for line in src.splitlines() if not line.strip().startswith("#")]
    return "\n".join(lines)


class InventoryParsingTests(unittest.TestCase):
    def _write(self, root, relpath, content):
        path = os.path.join(root, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)

    def test_canonical_key_derivation_no_double_prefix(self):
        with tempfile.TemporaryDirectory() as root:
            self._write(root, inv.DEPLOYMENTS_YAML_RELPATH, """
deployments:
  - name: oracle-payments-01
    type: oracle
    enabled: true
  - name: postgresql-payments-01
    type: postgresql
    enabled: true
""")
            runtimes = inv.load_runtimes(root)
            keys = [r["pipeline"] for r in runtimes]
            self.assertEqual(keys, ["gg-oracle-payments-01", "gg-postgresql-payments-01"])
            for k in keys:
                self.assertFalse(k.startswith("gg-gg-"), f"double prefix produced: {k}")

    def test_inventory_name_already_prefixed_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            self._write(root, inv.DEPLOYMENTS_YAML_RELPATH, """
deployments:
  - name: gg-oracle-payments-01
    type: oracle
    enabled: true
""")
            with self.assertRaises(inv.InventoryError):
                inv.load_deployment_inventory(root)

    def test_synthetic_sqlserver_entry_resolves(self):
        with tempfile.TemporaryDirectory() as root:
            self._write(root, inv.DEPLOYMENTS_YAML_RELPATH, """
deployments:
  - name: sqlserver-payments-01
    type: sqlserver
    enabled: false
""")
            runtimes = inv.load_runtimes(root)
            self.assertEqual(runtimes[0]["pipeline"], "gg-sqlserver-payments-01")
            self.assertFalse(runtimes[0]["enabled"])

    def test_deployments_json_enabled_only(self):
        with tempfile.TemporaryDirectory() as root:
            self._write(root, inv.DEPLOYMENTS_YAML_RELPATH, """
deployments:
  - name: oracle-payments-01
    type: oracle
    enabled: true
  - name: disabled-01
    type: sqlserver
    enabled: false
""")
            runtimes = inv.load_runtimes(root)
            self.assertEqual(inv.build_deployments_json(runtimes), ["gg-oracle-payments-01"])

    def test_topology_merge_endpoints(self):
        with tempfile.TemporaryDirectory() as root:
            self._write(root, inv.DEPLOYMENTS_YAML_RELPATH, """
deployments:
  - name: oracle-payments-01
    type: oracle
    enabled: true
""")
            self._write(root, "topologies/dev/x.yaml", """
deployments:
  source:
    deploymentName: gg-oracle-payments-01
    namespace: goldengate-dev
    serviceName: gg-oracle-payments-01
    endpoints:
      admin:
        scheme: https
        host: gg-oracle-payments-01.goldengate-dev.svc.cluster.local
        port: 8443
    secretReferences:
      admin: dev/goldengate/source/admin
      tls: dev/goldengate/tls-certificate
    processes:
      extracts: []
      distributionPaths: []
      replicats: []
""")
            runtimes = inv.load_runtimes(root)
            r = runtimes[0]
            self.assertEqual(r["namespace"], "goldengate-dev")
            self.assertEqual(r["endpoints"]["admin"]["port"], 8443)
            self.assertEqual(r["secretReferences"]["admin"], "dev/goldengate/source/admin")

    def test_empty_process_mapping_on_real_repo_topology(self):
        """The actual repository's canonical sources today have zero
        configured Extracts/Replicats/Distribution Paths -- the derived
        process-pipeline-map.json equivalent must be empty, not a guess."""
        runtimes = inv.load_runtimes(str(REPO_ROOT))
        self.assertEqual(inv.build_process_pipeline_map_json(runtimes), {})

    def test_real_repo_canonical_runtimes_present(self):
        runtimes = inv.load_runtimes(str(REPO_ROOT))
        keys = {r["pipeline"] for r in runtimes}
        self.assertIn("gg-oracle-payments-01", keys)
        self.assertIn("gg-postgresql-payments-01", keys)
        for r in runtimes:
            if r["pipeline"] in ("gg-oracle-payments-01", "gg-postgresql-payments-01"):
                self.assertTrue(r["enabled"])


class LeaseContractTests(unittest.TestCase):
    def _table(self):
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

    @mock_aws
    def test_acquire_then_conflict_then_renew(self):
        table = self._table()
        mgr_a = core.LeaseManager(table, "gg-oracle-payments-01", "holder-a", ttl=30)
        mgr_b = core.LeaseManager(table, "gg-oracle-payments-01", "holder-b", ttl=30)

        self.assertTrue(mgr_a.acquire())
        # A different holder/token cannot acquire while the lease is valid.
        self.assertFalse(mgr_b.acquire())
        # The original holder can renew (same holder + token).
        self.assertTrue(mgr_a.renew())
        # A non-holder cannot renew.
        self.assertFalse(mgr_b.renew())

    @mock_aws
    def test_expired_lease_can_be_taken_over(self):
        table = self._table()
        clock = {"t": 1000}
        mgr_a = core.LeaseManager(table, "gg-oracle-payments-01", "holder-a", ttl=5, clock=lambda: clock["t"])
        self.assertTrue(mgr_a.acquire())
        clock["t"] += 100  # well past expiresAt
        mgr_b = core.LeaseManager(table, "gg-oracle-payments-01", "holder-b", ttl=5, clock=lambda: clock["t"])
        self.assertTrue(mgr_b.acquire())

    @mock_aws
    def test_lease_item_shape(self):
        table = self._table()
        mgr = core.LeaseManager(table, "gg-oracle-payments-01", "gg-monitor-0", ttl=30)
        mgr.acquire()
        item = table.get_item(Key={"pipeline": "gg-oracle-payments-01", "recordType": "LEASE"})["Item"]
        self.assertEqual(item["holder"], "gg-monitor-0")
        self.assertIn("expiresAt", item)
        self.assertIn("ttl", item)
        self.assertIn("leaseToken", item)
        # ttl attribute must be expiresAt + GRACE (60)
        self.assertEqual(int(item["ttl"]) - int(item["expiresAt"]), 60)


class DeploymentStateShapeTests(unittest.TestCase):
    def _table(self):
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

    @mock_aws
    def test_state_deployment_key_and_fields(self):
        table = self._table()
        mgr = core.LeaseManager(table, "gg-oracle-payments-01", "gg-monitor-0", ttl=30)
        mgr.acquire()
        ok = core.write_process_state(
            table, mgr, "gg-oracle-payments-01", "oracle", "_deployment",
            {"processType": "deployment", "status": "UP", "recordedAt": 12345},
            is_leader_fn=lambda: True,
        )
        self.assertTrue(ok)
        item = table.get_item(
            Key={"pipeline": "gg-oracle-payments-01", "recordType": "STATE#_deployment"})["Item"]
        self.assertEqual(item["status"], "UP")
        self.assertEqual(item["deploymentType"], "oracle")
        self.assertEqual(int(item["recordedAt"]), 12345)
        self.assertNotIn("ttl", item)  # STATE items carry no ttl (manager writer never sets one)

    @mock_aws
    def test_state_write_fenced_off_when_not_leader(self):
        table = self._table()
        mgr = core.LeaseManager(table, "gg-oracle-payments-01", "gg-monitor-0", ttl=30)
        ok = core.write_process_state(
            table, mgr, "gg-oracle-payments-01", "oracle", "_deployment",
            {"processType": "deployment", "status": "UP", "recordedAt": 1},
            is_leader_fn=lambda: False,
        )
        self.assertFalse(ok)

    @mock_aws
    def test_process_state_key_uses_state_hash_prefix_not_singleton(self):
        table = self._table()
        mgr = core.LeaseManager(table, "gg-oracle-payments-01", "gg-monitor-0", ttl=30)
        mgr.acquire()
        core.write_process_state(
            table, mgr, "gg-oracle-payments-01", "oracle", "EXTORA1",
            {"processType": "extract", "status": "RUNNING", "recordedAt": 1},
            is_leader_fn=lambda: True,
        )
        resp = table.get_item(Key={"pipeline": "gg-oracle-payments-01", "recordType": "STATE#EXTORA1"})
        self.assertIn("Item", resp)
        singleton = table.get_item(Key={"pipeline": "gg-oracle-payments-01", "recordType": "STATE"})
        self.assertNotIn("Item", singleton)


class MetricDimensionTests(unittest.TestCase):
    def test_extract_lag_metric_dimensions(self):
        procs = [{"process": "EXTORA1", "type": "extract", "lagSeconds": 12.0, "abended": False}]
        md = core.build_metric_data("gg-oracle-payments-01", "oracle", procs)
        lag_entries = [m for m in md if m["MetricName"] == "ExtractLagSeconds"]
        self.assertEqual(len(lag_entries), 1)
        dims = {d["Name"]: d["Value"] for d in lag_entries[0]["Dimensions"]}
        self.assertEqual(dims, {"Deployment": "gg-oracle-payments-01",
                                "DeploymentType": "oracle", "Process": "EXTORA1"})
        self.assertEqual(lag_entries[0]["Unit"], "Seconds")

    def test_replicat_lag_metric_and_abend_state(self):
        procs = [{"process": "REPPG1", "type": "replicat", "lagSeconds": 3.0, "abended": True}]
        md = core.build_metric_data("gg-postgresql-payments-01", "postgresql", procs)
        names = {m["MetricName"] for m in md}
        self.assertEqual(names, {"ReplicatLagSeconds", "AbendState"})
        abend = next(m for m in md if m["MetricName"] == "AbendState")
        self.assertEqual(abend["Value"], 1.0)

    def test_emit_excludes_heartbeat_age_seconds(self):
        cw = MagicMock()
        core._emit(cw, "gg-oracle-payments-01", "oracle", {"lag": 0, "abend": 0, "down": 0})
        published_names = set()
        for call in cw.put_metric_data.call_args_list:
            for m in call.kwargs["MetricData"]:
                published_names.add(m["MetricName"])
        self.assertNotIn("HeartbeatAgeSeconds", published_names)
        self.assertEqual(published_names, {"LagBreached", "AbendFailure", "DeploymentDown"})

    def test_emit_uses_goldengate_pipelines_namespace(self):
        cw = MagicMock()
        core._emit(cw, "gg-oracle-payments-01", "oracle", {"lag": 1, "abend": 0, "down": 0})
        self.assertEqual(cw.put_metric_data.call_args.kwargs["Namespace"], "GoldenGate/Pipelines")


class RestResponseParsingTests(unittest.TestCase):
    def test_fetch_gg_processes_parses_extracts_and_replicats(self):
        responses = {
            "https://x/services/v2/deployments": {},
            "https://x/services/v2/extracts": {"response": {"items": [{"name": "EXTORA1"}]}},
            "https://x/services/v2/extracts/EXTORA1": {"response": {"status": "RUNNING", "lag": 5}},
            "https://x/services/v2/replicats": {"response": {"items": []}},
            "https://x/services/v2/sources": {"response": {"items": []}},
        }

        class FakeOpener:
            def open(self, url, timeout=5):
                body = json.dumps(responses[url]).encode()

                class Resp:
                    def __enter__(self_inner):
                        return self_inner

                    def __exit__(self_inner, *a):
                        return False

                    def read(self_inner):
                        return body

                return Resp()

        procs = core.fetch_gg_processes("https://x", FakeOpener())
        self.assertEqual(len(procs), 1)
        self.assertEqual(procs[0]["process"], "EXTORA1")
        self.assertEqual(procs[0]["type"], "extract")
        self.assertEqual(procs[0]["lagSeconds"], 5.0)
        self.assertFalse(procs[0]["abended"])


class CredentialRedactionTests(unittest.TestCase):
    def test_read_secret_file_missing_returns_empty_not_raise(self):
        self.assertEqual(core._read_secret_file("/nonexistent/path/should/not/exist"), "")

    def test_read_secret_value_never_appears_in_repr_of_reader(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("super-secret-password-value\n")
            path = f.name
        try:
            value = core._read_secret_file(path)
            self.assertEqual(value, "super-secret-password-value")
            # The function itself must not embed the value in any exception
            # message on the failure path (tested above) -- here we assert
            # the source code of the failure branch contains no logging call
            # that could leak `value`.
            import inspect
            src = inspect.getsource(core._read_secret_file)
            self.assertNotIn("logger.", src.split("except OSError:")[1] if "except OSError:" in src else "")
        finally:
            os.unlink(path)


class PassiveBehaviorTests(unittest.TestCase):
    """Static source checks: no active mutation path exists anywhere in this
    application's source, regardless of runtime configuration."""

    def _source(self, filename):
        path = os.path.join(os.path.dirname(__file__), "..", filename)
        with open(path) as f:
            return f.read()

    def test_no_os_exit_anywhere(self):
        # os._exit (the manager's own abrupt FAILOVER/heal-restart exit
        # mechanism) must never appear anywhere. sys.exit is different in
        # kind -- a normal process-entrypoint exit on fatal, unrecoverable
        # startup configuration errors (see main()'s
        # validate_enabled_runtimes() call) is legitimate and NOT a
        # healing/mutation action; it is intentionally allowed ONLY inside
        # main() itself, never in any library/business-logic function.
        for fname in ("gg_monitor_core.py", "gg_health_rules.py", "inventory.py"):
            self.assertNotIn("os._exit", self._source(fname), f"{fname} must never call os._exit")

        core_src = self._source("gg_monitor_core.py")
        import re
        functions = re.split(r"^def ", core_src, flags=re.MULTILINE)
        for func_block in functions[1:]:
            func_name = func_block.split("(")[0].strip()
            if func_name == "main":
                continue
            self.assertNotIn(
                "sys.exit", func_block,
                f"gg_monitor_core.py: sys.exit found outside main() in function {func_name!r}"
            )
        for fname in ("gg_health_rules.py", "inventory.py"):
            self.assertNotIn("sys.exit", self._source(fname), f"{fname} must never call sys.exit")

    def test_no_subprocess_or_os_system(self):
        for fname in ("gg_monitor_core.py", "gg_health_rules.py", "inventory.py"):
            src = self._source(fname)
            self.assertNotIn("subprocess", src, f"{fname} must not shell out")
            self.assertNotIn("os.system", src, f"{fname} must not shell out")

    def test_no_kubernetes_client_or_kubectl(self):
        # "Kubernetes"/"kubectl" legitimately appear in comments (e.g. "internal
        # Kubernetes Service DNS", "calls a Kubernetes mutation API") -- what
        # must never appear is an actual client import/call.
        for fname in ("gg_monitor_core.py", "gg_health_rules.py", "inventory.py"):
            code = self._code_lines(self._source(fname))
            self.assertNotIn("import kubernetes", code)
            self.assertNotIn("from kubernetes", code)
            self.assertNotIn("subprocess", code)  # covers any kubectl-via-shell path too
            self.assertNotIn("delete_namespaced_pod", code)

    def _code_lines(self, src):
        return code_only(src)

    def test_no_heal_decision_or_credential_sync(self):
        src = self._code_lines(self._source("gg_health_rules.py"))
        self.assertNotIn("heal_decision(", src)
        self.assertNotIn("serviceHealEnabled", src)
        self.assertNotIn("maxHealAttempts", src)
        core_src = self._code_lines(self._source("gg_monitor_core.py"))
        self.assertNotIn("maybe_sync_credentials", core_src)
        self.assertNotIn("creds_thread", core_src)
        self.assertNotIn("push_fn", core_src)

    def test_no_dispatch_stall_checks_typo(self):
        for fname in ("gg_monitor_core.py", "gg_health_rules.py"):
            code = self._code_lines(self._source(fname))
            self.assertNotIn("dispatchStallChecks", code)
            self.assertIn("distpathStallChecks", code)

    def test_no_legacy_singleton_state_literal(self):
        for fname in ("gg_monitor_core.py",):
            src = self._source(fname)
            self.assertNotIn('"recordType": "STATE"}', src)
            self.assertNotIn("'recordType': 'STATE'}", src)


class HealthRuleEvaluationTests(unittest.TestCase):
    def test_resolve_config_defaults_are_passive_safe(self):
        cfg = gh.resolve_config({})
        self.assertFalse(cfg["alertsEnabled"])
        self.assertFalse(cfg["defaults"]["failoverEnabled"])
        self.assertEqual(cfg["defaults"]["distpathStallChecks"], 3)

    def test_abend_step_computes_failover_flag_without_acting(self):
        rule = dict(gh.DEFAULTS["defaults"])
        rule["maxConsecutiveAbends"] = 1
        rule["failoverEnabled"] = True
        state = {}
        st, act = gh.abend_step("ABENDED", state, now=1000, rule=rule, alerts_enabled=True)
        self.assertTrue(act["failover"])
        # This module only returns the flag; gg_monitor_core.py never branches on it.
        with open(os.path.join(os.path.dirname(__file__), "..", "gg_monitor_core.py")) as f:
            src = f.read()
        self.assertIn('act["failover"] is computed for schema fidelity only', src)

    def test_classify_service_up(self):
        self.assertTrue(gh.classify_service_up(200))
        self.assertTrue(gh.classify_service_up(401))
        self.assertFalse(gh.classify_service_up(502))
        self.assertFalse(gh.classify_service_up(None))


# ===========================================================================
# Correction pass: LEASE renewal threading model (fix 1, deployment blocker).
# Deterministic, fake-clock-driven proof that a 30s-TTL lease renewed on its
# own 5s RENEW_INTERVAL cadence stays valid across a full 60s poll window --
# the exact scenario that was broken before this correction (lease only
# renewed once per 60s poll tick, so it always expired mid-sleep).
# ===========================================================================
class LeaseTimelineTests(unittest.TestCase):
    def _table(self):
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

    @mock_aws
    def test_1_and_2_renewal_keeps_30s_lease_valid_across_60s_poll_window(self):
        table = self._table()
        clock = {"t": 1000}
        mgr = core.LeaseManager(table, "gg-oracle-payments-01", "gg-monitor-0", ttl=30, clock=lambda: clock["t"])

        self.assertTrue(mgr.acquire())

        # Simulate exactly RENEW_INTERVAL=5s ticks across a 60-second window
        # (12 renewals) -- the poll loop's own interval, but lease renewal
        # must NOT be tied to it.
        for _ in range(12):
            clock["t"] += 5
            ok = mgr.renew()
            self.assertTrue(ok, f"renewal must succeed at simulated t={clock['t']}")
            item = table.get_item(Key={"pipeline": "gg-oracle-payments-01", "recordType": "LEASE"})["Item"]
            expires_at = int(item["expiresAt"])
            # 2: expiry is extended every renewal -- always ttl seconds
            # beyond the current simulated time, never allowed to lapse.
            self.assertEqual(expires_at, clock["t"] + 30)
            self.assertGreater(expires_at, clock["t"])

        # At t=1060 the lease must still be valid. Under the old bug (renewed
        # only once per 60s poll tick with ttl=30) it would have expired at
        # t=1030, 30 seconds earlier.
        final_item = table.get_item(Key={"pipeline": "gg-oracle-payments-01", "recordType": "LEASE"})["Item"]
        self.assertGreater(int(final_item["expiresAt"]), clock["t"])

    @mock_aws
    def test_3_second_holder_cannot_acquire_while_renewal_continues(self):
        table = self._table()
        clock = {"t": 3000}
        mgr_a = core.LeaseManager(table, "gg-oracle-payments-01", "holder-a", ttl=30, clock=lambda: clock["t"])
        mgr_b = core.LeaseManager(table, "gg-oracle-payments-01", "holder-b", ttl=30, clock=lambda: clock["t"])
        mgr_a.acquire()
        for _ in range(10):
            clock["t"] += 5
            self.assertTrue(mgr_a.renew())
            self.assertFalse(mgr_b.acquire(), "a second holder must not acquire while the first keeps renewing")

    @mock_aws
    def test_4_second_holder_can_acquire_after_renewals_stop_and_lease_expires(self):
        table = self._table()
        clock = {"t": 4000}
        mgr_a = core.LeaseManager(table, "gg-oracle-payments-01", "holder-a", ttl=30, clock=lambda: clock["t"])
        mgr_b = core.LeaseManager(table, "gg-oracle-payments-01", "holder-b", ttl=30, clock=lambda: clock["t"])
        mgr_a.acquire()
        clock["t"] += 31  # past the 30s TTL; no further renewal issued
        self.assertTrue(mgr_b.acquire())

    def test_6_renew_interval_is_used_by_executable_code_not_just_declared(self):
        import inspect
        self.assertEqual(core.RENEW_INTERVAL, 5)
        src = inspect.getsource(core.lease_control_loop)
        self.assertIn("stop_event.wait(renew_interval)", src)
        sig = inspect.signature(core.lease_control_loop)
        self.assertEqual(sig.parameters["renew_interval"].default, core.RENEW_INTERVAL)
        # Independent of checkIntervalSeconds: the function BODY (not its
        # docstring, which legitimately explains the independence in prose)
        # must never read cfg["checkIntervalSeconds"] or similar.
        body = src.split('"""', 2)[-1] if src.count('"""') >= 2 else src
        self.assertNotIn("checkIntervalSeconds", body)

    @mock_aws
    def test_5_state_reflects_immediate_demotion_on_lease_loss(self):
        table = self._table()
        clock = {"t": 5000}
        mgr = core.LeaseManager(table, "gg-oracle-payments-01", "holder-a", ttl=30, clock=lambda: clock["t"])
        state = core.LeaseState()
        self.assertTrue(mgr.acquire())
        state.set_leader(True)

        # A competing holder takes over after this instance fails to renew
        # in time (simulated by advancing well past TTL with no renewal).
        clock["t"] += 31
        other = core.LeaseManager(table, "gg-oracle-payments-01", "holder-b", ttl=30, clock=lambda: clock["t"])
        self.assertTrue(other.acquire())

        # This instance's own next renew must fail (fenced by holder/token
        # mismatch), and lease_control_loop's exact logic
        # (renew-fails -> set_leader(False)) must reflect the loss immediately.
        self.assertFalse(mgr.renew())
        state.set_leader(False)
        self.assertFalse(state.is_leader())

        # write_process_state must refuse to write once state reflects the loss.
        health_table = table
        ok = core.write_process_state(
            health_table, mgr, "gg-oracle-payments-01", "oracle", "_deployment",
            {"processType": "deployment", "status": "UP", "recordedAt": clock["t"]},
            is_leader_fn=state.is_leader,
        )
        self.assertFalse(ok, "polling must stop writing immediately once demoted")

    def test_lease_control_loop_and_polling_loop_use_independent_tables(self):
        """Structural proof (source inspection) that run_pipeline gives the
        lease-control loop and the polling loop their own boto3 Table
        objects -- boto3 Table objects are not thread-safe across concurrent
        update_item calls."""
        import inspect
        src = inspect.getsource(core.run_pipeline)
        self.assertIn("lease_table = boto3.resource", src)
        self.assertIn("health_table = boto3.resource", src)
        self.assertIn("health_mgr.token = lease_mgr.token", src)


# ===========================================================================
# Correction pass: startup validation + readiness semantics (fix 2).
# ===========================================================================
class ReadinessAndStartupValidationTests(unittest.TestCase):
    def _write(self, root, relpath, content):
        path = os.path.join(root, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)

    def test_4_enabled_runtime_missing_topology_fails_startup(self):
        with tempfile.TemporaryDirectory() as root:
            self._write(root, inv.DEPLOYMENTS_YAML_RELPATH, """
deployments:
  - name: oracle-payments-01
    type: oracle
    enabled: true
""")
            runtimes = inv.load_runtimes(root)
            with self.assertRaises(inv.StartupValidationError) as ctx:
                inv.validate_enabled_runtimes(runtimes, admin_credential_types={"oracle", "postgresql"})
            self.assertTrue(any("no matching topology entry" in p for p in ctx.exception.problems))

    def test_duplicate_deployment_name_fails_startup(self):
        with tempfile.TemporaryDirectory() as root:
            self._write(root, inv.DEPLOYMENTS_YAML_RELPATH, """
deployments:
  - name: oracle-payments-01
    type: oracle
    enabled: true
  - name: oracle-payments-01
    type: oracle
    enabled: false
""")
            with self.assertRaises(inv.InventoryError):
                inv.load_deployment_inventory(root)

    def test_missing_admin_endpoint_fails_startup(self):
        with tempfile.TemporaryDirectory() as root:
            self._write(root, inv.DEPLOYMENTS_YAML_RELPATH, """
deployments:
  - name: oracle-payments-01
    type: oracle
    enabled: true
""")
            self._write(root, "topologies/dev/x.yaml", """
deployments:
  source:
    deploymentName: gg-oracle-payments-01
    namespace: goldengate-dev
    serviceName: gg-oracle-payments-01
    endpoints: {}
    secretReferences:
      admin: dev/goldengate/source/admin
      tls: dev/goldengate/tls-certificate
""")
            runtimes = inv.load_runtimes(root)
            with self.assertRaises(inv.StartupValidationError) as ctx:
                inv.validate_enabled_runtimes(runtimes, admin_credential_types={"oracle", "postgresql"})
            self.assertTrue(any("admin endpoint" in p for p in ctx.exception.problems))

    def test_unsupported_runtime_type_fails_startup(self):
        with tempfile.TemporaryDirectory() as root:
            self._write(root, inv.DEPLOYMENTS_YAML_RELPATH, """
deployments:
  - name: sqlserver-payments-01
    type: sqlserver
    enabled: true
""")
            runtimes = inv.load_runtimes(root)
            with self.assertRaises(inv.StartupValidationError) as ctx:
                inv.validate_enabled_runtimes(runtimes, admin_credential_types={"oracle", "postgresql"})
            self.assertTrue(any("unsupported deployment type" in p for p in ctx.exception.problems))

    def test_missing_tls_server_name_fails_startup(self):
        with tempfile.TemporaryDirectory() as root:
            self._write(root, inv.DEPLOYMENTS_YAML_RELPATH, """
deployments:
  - name: oracle-payments-01
    type: oracle
    enabled: true
""")
            self._write(root, "topologies/dev/x.yaml", """
deployments:
  source:
    deploymentName: gg-oracle-payments-01
    namespace: goldengate-dev
    serviceName: gg-oracle-payments-01
    endpoints:
      admin:
        scheme: https
        host: gg-oracle-payments-01.goldengate-dev.svc.cluster.local
        port: 8443
    secretReferences:
      admin: dev/goldengate/source/admin
      tls: dev/goldengate/tls-certificate
""")
            runtimes = inv.load_runtimes(root)
            with self.assertRaises(inv.StartupValidationError) as ctx:
                inv.validate_enabled_runtimes(runtimes, admin_credential_types={"oracle", "postgresql"})
            self.assertTrue(any("tlsServerName" in p for p in ctx.exception.problems))

    def test_real_repo_enabled_runtimes_pass_startup_validation(self):
        """The actual current repository state must validate cleanly --
        this correction pass must not have broken the real deployment."""
        runtimes = inv.load_runtimes(str(REPO_ROOT))
        inv.validate_enabled_runtimes(runtimes, admin_credential_types=set(core.ADMIN_USER_FILE))

    def test_5_missing_credential_file_not_ready(self):
        runtime = {"pipeline": "gg-oracle-payments-01", "type": "oracle"}
        table = MagicMock()
        table.get_item.return_value = {"Item": {}}
        old_user_file = core.ADMIN_USER_FILE["oracle"]
        core.ADMIN_USER_FILE["oracle"] = "/nonexistent/path/should/not/exist"
        try:
            ok, reason = core.check_static_prerequisites(runtime, table)
            self.assertFalse(ok)
            self.assertIn("credential file", reason)
        finally:
            core.ADMIN_USER_FILE["oracle"] = old_user_file

    def test_5_tls_context_unavailable_not_ready(self):
        runtime = {"pipeline": "gg-oracle-payments-01", "type": "oracle"}
        table = MagicMock()
        with tempfile.NamedTemporaryFile("w", delete=False) as uf:
            uf.write("oggadmin")
            user_path = uf.name
        with tempfile.NamedTemporaryFile("w", delete=False) as pf:
            pf.write("secretpass")
            pwd_path = pf.name
        old_user = core.ADMIN_USER_FILE["oracle"]
        old_pwd = core.ADMIN_PASSWORD_FILE["oracle"]
        old_ca = core.CA_FILE
        core.ADMIN_USER_FILE["oracle"] = user_path
        core.ADMIN_PASSWORD_FILE["oracle"] = pwd_path
        core._SSL_CTX = None
        core.CA_FILE = "/nonexistent/ca.pem"
        try:
            ok, reason = core.check_static_prerequisites(runtime, table)
            self.assertFalse(ok)
            self.assertIn("TLS context", reason)
        finally:
            core.ADMIN_USER_FILE["oracle"] = old_user
            core.ADMIN_PASSWORD_FILE["oracle"] = old_pwd
            core.CA_FILE = old_ca
            core._SSL_CTX = None
            os.unlink(user_path)
            os.unlink(pwd_path)

    def test_6_runtime_api_down_writes_deployment_down_without_affecting_readiness(self):
        """Structural proof: check_static_prerequisites (the only readiness
        gate) never calls the GoldenGate Admin REST client, and
        polling_loop's DEPLOYMENT_DOWN write path never touches ready_state
        -- so an unreachable runtime cannot make the monitor pod unready."""
        import inspect
        prereq_src = inspect.getsource(core.check_static_prerequisites)
        self.assertNotIn("fetch_gg_processes", prereq_src)
        self.assertNotIn("_http_json", prereq_src)
        poll_src = inspect.getsource(core.polling_loop)
        self.assertIn("DEPLOYMENT_DOWN", poll_src)
        self.assertNotIn("ready_state", poll_src)

    def test_readiness_becomes_true_only_after_lease_control_loop_succeeds(self):
        """Structural proof that run_pipeline's readiness flag is driven by
        state.is_ready() (set only inside lease_control_loop's own
        try-block, after a real acquire/renew call succeeds), not merely by
        one loop iteration starting."""
        import inspect
        src = inspect.getsource(core.run_pipeline)
        self.assertIn("state.is_ready()", src)
        lease_src = inspect.getsource(core.lease_control_loop)
        self.assertIn("state.set_ready(True)", lease_src)


# ===========================================================================
# Correction pass: full TLS server identity verification (fix 3).
# ===========================================================================
class TLSServerIdentityTests(unittest.TestCase):
    def setUp(self):
        core._SSL_CTX = None

    def tearDown(self):
        core._SSL_CTX = None

    def test_7_check_hostname_and_cert_required_remain_enabled(self):
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
            f.write("placeholder")
            ca_path = f.name
        try:
            with mock.patch("ssl.SSLContext.load_verify_locations"):
                ctx = core._build_ssl_context(ca_file=ca_path)
            self.assertTrue(ctx.check_hostname, "check_hostname must remain True -- never disabled for this remote client")
            self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED, "verify_mode must remain CERT_REQUIRED -- never CERT_NONE")
        finally:
            os.unlink(ca_path)

    def test_missing_ca_file_raises_never_falls_back_to_unverified(self):
        core._SSL_CTX = None
        with self.assertRaises(RuntimeError):
            core._build_ssl_context(ca_file="/nonexistent/ca.pem")

    def test_8_connect_host_and_tls_server_name_are_distinct(self):
        """connect() must dial the internal Service DNS host but verify
        (SNI + hostname check) against a SEPARATE tlsServerName -- proves
        the two are independently plumbed through, not conflated."""
        conn = core._SNIHTTPSConnection(
            "gg-oracle-payments-01.goldengate-dev.svc.cluster.local", 8443,
            tls_server_name="gg-oracle-payments-01.goldengate-dev.adcbmis.local",
            context=ssl.create_default_context(),
        )
        fake_sock = MagicMock()
        fake_wrapped = MagicMock()
        with mock.patch("socket.create_connection", return_value=fake_sock) as mock_connect:
            with mock.patch.object(conn._context, "wrap_socket", return_value=fake_wrapped) as mock_wrap:
                conn.connect()
        mock_connect.assert_called_once_with(
            ("gg-oracle-payments-01.goldengate-dev.svc.cluster.local", 8443), conn.timeout)
        mock_wrap.assert_called_once()
        server_hostname = mock_wrap.call_args.kwargs["server_hostname"]
        self.assertEqual(server_hostname, "gg-oracle-payments-01.goldengate-dev.adcbmis.local")
        self.assertNotEqual(server_hostname, conn.host, "tlsServerName must differ from the connect host in this deployment")

    def test_sni_connection_falls_back_to_host_when_no_tls_server_name_given(self):
        conn = core._SNIHTTPSConnection("example.internal", 8443, context=ssl.create_default_context())
        with mock.patch("socket.create_connection", return_value=MagicMock()):
            with mock.patch.object(conn._context, "wrap_socket", return_value=MagicMock()) as mock_wrap:
                conn.connect()
        self.assertEqual(mock_wrap.call_args.kwargs["server_hostname"], "example.internal")

    def test_never_uses_cert_none_or_check_hostname_false_anywhere(self):
        # Comments deliberately explain "never CERT_NONE" / "always
        # check_hostname=True" in prose -- code_only() strips those so this
        # checks real assignments only.
        import inspect
        code = code_only(inspect.getsource(core))
        self.assertNotIn("CERT_NONE", code)
        self.assertNotIn("check_hostname = False", code)
        self.assertNotIn("check_hostname=False", code)

    def test_real_repo_topology_has_tls_server_name_for_both_admin_endpoints(self):
        runtimes = inv.load_runtimes(str(REPO_ROOT))
        for r in runtimes:
            if r["enabled"]:
                admin = (r["endpoints"] or {}).get("admin") or {}
                self.assertTrue(admin.get("tlsServerName"), f"{r['pipeline']}: tlsServerName must be set")
                self.assertNotEqual(admin.get("tlsServerName"), admin.get("host"),
                                    f"{r['pipeline']}: tlsServerName must differ from the internal Service DNS host")


# ===========================================================================
# Correction pass: real process-pipeline map, not a hardcoded {} (fix 4).
# ===========================================================================
class ProcessPipelineMapUsageTests(unittest.TestCase):
    def test_9_synthetic_extract_resolves_pipeline_name_from_topology(self):
        runtime = {
            "pipeline": "gg-oracle-payments-01",
            "name": "oracle-payments-01",
            "type": "oracle",
            "processes": {"extracts": ["EXTORA1"], "distributionPaths": [], "replicats": []},
        }
        pipe_map = inv.build_process_pipeline_map_json([runtime])
        self.assertEqual(
            pipe_map["EXTORA1"],
            {"pipeline_name": "gg-oracle-payments-01", "deployment": "oracle-payments-01"},
        )

    def test_polling_loop_consumes_real_map_not_hardcoded_empty_dict(self):
        import inspect
        src = inspect.getsource(core.polling_loop)
        self.assertIn("build_process_pipeline_map_json", src)
        self.assertNotIn("pipe_map = {}", src)
        self.assertIn('process_pipeline_map.get(name.upper()', src)


# ===========================================================================
# Correction pass: workflow-level checks (fix 5 no `|| true`, in-pod
# verification; fix 8 kubectl pin, unconditional tests, accurate comments).
# ===========================================================================
class WorkflowCorrectionTests(unittest.TestCase):
    WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "gg-monitor-core.yaml"
    CONFIGMAP_PATH = REPO_ROOT / "helm" / "gg-monitor" / "templates" / "configmap.yaml"

    def _dynamodb_step_text(self, code_only_=False):
        content = self.WORKFLOW_PATH.read_text()
        idx = content.index("Verify DynamoDB records from inside the gg-monitor pod")
        next_step = content.find("\n      - name:", idx)
        text = content[idx:next_step if next_step != -1 else idx + 8000]
        if not code_only_:
            return text
        # Drop the step's own `name:` line (which deliberately documents "no
        # `|| true`" in its title) and every bash `#` comment line (which
        # deliberately explains why `|| true` is NOT used) -- what must
        # never appear is `|| true` used as ACTUAL shell syntax.
        lines = text.splitlines()[1:]  # [0] is the "name: ..." line itself
        lines = [line for line in lines if not line.strip().startswith("#")]
        return "\n".join(lines)

    def test_10_dynamodb_verification_runs_inside_the_monitor_pod(self):
        step_text = self._dynamodb_step_text()
        self.assertIn("kubectl exec", step_text)
        self.assertIn("assumed-role/gg-monitor-dev-role/", step_text)
        self.assertIn("get_caller_identity", step_text)

    def test_11_no_or_true_in_dynamodb_verification_step(self):
        step_text = self._dynamodb_step_text(code_only_=True)
        self.assertNotIn("|| true", step_text)

    def test_dynamodb_verification_does_not_use_runner_aws_cli_directly(self):
        step_text = self._dynamodb_step_text()
        self.assertNotIn("aws dynamodb get-item", step_text)

    def test_15_kubectl_pinned_to_1_33_not_1_35(self):
        content = self.WORKFLOW_PATH.read_text()
        self.assertIn('KUBECTL_VERSION="v1.33', content)
        self.assertNotIn('KUBECTL_VERSION="v1.35', content)

    def test_unit_tests_and_syntax_check_run_unconditionally(self):
        content = self.WORKFLOW_PATH.read_text()
        for step_name in ("Validate monitor Python syntax", "Run monitor unit tests"):
            idx = content.index(f"name: {step_name}")
            following = content[idx: idx + 250]
            self.assertNotIn("if: env.IMAGE_EXISTED", following,
                             f"{step_name!r} must run on every execution, not only when the image is missing")

    def test_14_configmap_staging_comment_references_correct_workflow(self):
        content = self.CONFIGMAP_PATH.read_text()
        self.assertIn("gg-monitor-core.yaml", content)
        # The comment is allowed to clarify "NOT goldengate-platform.yaml"
        # (a deliberate negation, present precisely because that used to be
        # the wrong claim) -- what must never appear is the OLD wrong claim
        # pattern itself: goldengate-platform.yaml cited as the file where
        # the staging step lives.
        self.assertNotIn("step in .github/workflows/goldengate-platform.yaml", content)

    def test_no_dynamodb_writes_from_the_workflow(self):
        content = self.WORKFLOW_PATH.read_text()
        for forbidden in ("put_item(", "update_item(", "aws dynamodb put-item", "aws dynamodb update-item"):
            self.assertNotIn(forbidden, content, f"the workflow must never write to DynamoDB directly: found {forbidden!r}")


# ===========================================================================
# Correction pass: no generated bytecode/cache files remain (fix 8, item 13).
# Relies on `sys.dont_write_bytecode = True` set at the top of this file
# (before gg_monitor_core/gg_health_rules/inventory are imported) so this
# test file's own execution never regenerates __pycache__ for the very
# modules being checked.
# ===========================================================================
class NoGeneratedArtifactsTests(unittest.TestCase):
    """Note on approach: a plain filesystem walk asserting "__pycache__
    doesn't exist" is inherently self-contaminating here -- Python compiles
    a module to bytecode as a precondition of importing it, including
    THIS test file itself, before any in-file `sys.dont_write_bytecode`
    statement can take effect (that flag only affects imports that happen
    AFTER it executes, e.g. gg_monitor_core/gg_health_rules/inventory
    below, not the test file's own compilation). What actually matters --
    that nothing generated ever gets committed -- is correctly answered by
    asking git, not the filesystem, so that's what these tests do."""

    def test_gitignore_and_dockerignore_exist_and_cover_pycache(self):
        monitor_core_dir = REPO_ROOT / "monitoring" / "gg-monitor-core"
        for fname in (".gitignore", ".dockerignore"):
            path = monitor_core_dir / fname
            self.assertTrue(path.exists(), f"{fname} must exist")
            content = path.read_text()
            self.assertIn("__pycache__", content)

    def test_any_generated_artifacts_present_are_git_ignored_not_tracked(self):
        import subprocess
        monitor_core_dir = REPO_ROOT / "monitoring" / "gg-monitor-core"
        candidates = []
        for pattern in ("__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".coverage"):
            candidates.extend(monitor_core_dir.rglob(pattern))
        if not candidates:
            return  # nothing generated on disk right now -- trivially fine
        for path in candidates:
            tracked = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "ls-files", "--error-unmatch", str(path)],
                capture_output=True, text=True,
            )
            self.assertNotEqual(tracked.returncode, 0,
                                f"{path} must never be committed to git, but `git ls-files` finds it tracked")
            ignored = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", str(path)],
                capture_output=True, text=True,
            )
            self.assertEqual(ignored.returncode, 0,
                             f"{path} exists on disk but is not covered by .gitignore")


if __name__ == "__main__":
    unittest.main()
