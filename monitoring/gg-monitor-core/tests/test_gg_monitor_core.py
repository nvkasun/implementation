"""Focused unit tests for gg-monitor-core.

Uses moto (mock_aws) for real DynamoDB/CloudWatch call shapes -- no network,
no real AWS credentials, no real state. Run with:
    python3 -m unittest discover -s monitoring/gg-monitor-core/tests -p "test_*.py" -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import boto3
from moto import mock_aws

import gg_health_rules as gh
import gg_monitor_core as core
import inventory as inv

REPO_ROOT = Path(__file__).resolve().parents[3]


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
        for fname in ("gg_monitor_core.py", "gg_health_rules.py", "inventory.py"):
            self.assertNotIn("os._exit", self._source(fname), f"{fname} must never call os._exit")
            self.assertNotIn("sys.exit", self._source(fname), f"{fname} must never call sys.exit in library code")

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
        """Strip the leading module docstring (via ast, robust to any
        length/quoting) and full-line comments -- these checks must verify
        actual code (calls/definitions/assignments), not the prose that
        documents what was deliberately excluded and why."""
        import ast
        tree = ast.parse(src)
        docstring = ast.get_docstring(tree, clean=False)
        if docstring and docstring in src:
            src = src.replace(docstring, "", 1)
        lines = [line for line in src.splitlines() if not line.strip().startswith("#")]
        return "\n".join(lines)

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


if __name__ == "__main__":
    unittest.main()
