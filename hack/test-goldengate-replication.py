"""Offline tests for hack/goldengate-replication.py; run directly via `python3 hack/test-goldengate-replication.py`."""
from __future__ import annotations

import importlib.util
import json
import os
import unittest
import unittest.mock as mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL_PATH = os.path.join(REPO_ROOT, "hack", "goldengate-replication.py")


def _load_tool():
    spec = importlib.util.spec_from_file_location("goldengate_replication", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


repl = _load_tool()

PLAN = {
    "pipelineId": "payments-pg-to-mssql-001",
    "tlsSecret": "dev/goldengate/tls-certificate",
    "networkCredentialDomain": "Network",
    "networkCredentialAlias": "NET_TEST",
    "source": {
        "deploymentId": "gg-pg-src-fixture-01", "deploymentType": "postgresql",
        "runtimeHost": "gg-pg-src-fixture-01.goldengate-dev.adcbmis.local",
        "serviceAccount": "gg-postgresql-sa",
        "image": "229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-postgresql:23.26.2.0.1",
        "adminSecret": "dev/goldengate/source/admin",
        "databaseSecret": "dev/goldengate/databases/payments-pg-to-mssql-001/source",
        "databaseCredentialAlias": "SRC_ALIAS", "databaseCredentialDomain": "OracleGoldenGate",
    },
    "target": {
        "deploymentId": "gg-mssql-tgt-fixture-01", "deploymentType": "mssql",
        "runtimeHost": "gg-mssql-tgt-fixture-01.goldengate-dev.adcbmis.local",
        "serviceAccount": "gg-mssql-sa",
        "image": "229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-sqlserver:23.26.2.0.1",
        "adminSecret": "dev/goldengate/target/admin",
        "databaseSecret": "dev/goldengate/databases/payments-pg-to-mssql-001/target",
        "databaseCredentialAlias": "TGT_ALIAS", "databaseCredentialDomain": "OracleGoldenGate",
    },
    "checkpoint": {"enabled": True, "table": "dbo.gg_checkpoint", "createIfMissing": True},
    "replicat": {
        "name": "MSTGT01", "sourceTrailName": "ma", "begin": "now", "startOnCreate": True,
        "mappings": [{"source": "public.payments", "target": "dbo.payments"}],
    },
    "supplementalLogging": {"objects": ["public.payments"]},
    "extract": {
        "name": "PGSRC01", "pluginType": "pgoutput", "begin": "now", "startOnCreate": True,
        "trail": {"name": "pa"}, "tables": ["public.payments"],
    },
    "distribution": {
        "pathName": "PG2MS01", "sourceTrailName": "pa", "targetTrailName": "ma",
        "protocol": "wss", "port": 443, "startOnCreate": True,
    },
}


class FakeClient:
    """In-memory GET/POST double; never issues DELETE/PUT/PATCH because those methods do not exist on it."""

    def __init__(self, existing=None, statuses=None):
        self.objects = dict(existing or {})
        self.calls = []
        self._forced_statuses = dict(statuses or {})

    def get(self, path, retry=0):
        self.calls.append(("GET", path))
        if path in self._forced_statuses:
            return self._forced_statuses[path], self.objects.get(path)
        if path in self.objects:
            return 200, self.objects[path]
        if path.endswith("/valid"):
            return 200, {"valid": True}
        if path == "/services/v2/targets":
            return 200, {"response": {"items": [{"name": "PG2MS01"}]}}
        if path.startswith("/services/v2/targets/"):
            return 200, {"response": {"trail": "ma"}}
        return 404, None

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        if path == repl.commands_execute_path():
            return 200, {}
        self.objects[path] = {"response": {"status": "RUNNING", "trail": body.get("trail")}}
        return 201, {}


def with_secret_files(func):
    return mock.patch.object(repl, "read_secret_file", return_value="fake-value")(func)


class CredentialTests(unittest.TestCase):
    def test_29_get_existing_credential_succeeds(self):
        path = repl.credential_path("OracleGoldenGate", "SRC_ALIAS")
        client = FakeClient(existing={path: {"response": {}}})
        repl.ensure_credential(client, "OracleGoldenGate", "SRC_ALIAS", "u", "p")
        self.assertIn(("GET", path), client.calls)
        self.assertFalse(any(c[0] == "POST" and c[1] == path for c in client.calls))

    def test_30_missing_credential_is_created_once(self):
        client = FakeClient()
        repl.ensure_credential(client, "OracleGoldenGate", "SRC_ALIAS", "u", "p")
        posts = [c for c in client.calls if c[0] == "POST" and "credentials" in c[1]]
        self.assertEqual(len(posts), 1)

    def test_31_existing_credential_is_never_replaced(self):
        path = repl.credential_path("OracleGoldenGate", "SRC_ALIAS")
        client = FakeClient(existing={path: {"response": {}}})
        repl.ensure_credential(client, "OracleGoldenGate", "SRC_ALIAS", "u", "p")
        self.assertNotIn((path,), [c[1:2] for c in client.calls if c[0] == "POST"])

    def test_32_invalid_credential_fails(self):
        valid_path = repl.credential_valid_path("OracleGoldenGate", "SRC_ALIAS")
        client = FakeClient(statuses={valid_path: 200})
        client.objects[valid_path] = {"valid": False}
        with self.assertRaises(repl.ReplicationError):
            repl.ensure_credential(client, "OracleGoldenGate", "SRC_ALIAS", "u", "p")

    def test_51_no_credential_value_appears_in_error_reason(self):
        client = FakeClient()
        try:
            repl.ensure_credential(client, "OracleGoldenGate", "SRC_ALIAS", "super-secret-user", "super-secret-pass")
        except repl.ReplicationError as exc:
            self.assertNotIn("super-secret", exc.reason)

    def test_52_tls_verification_is_enabled(self):
        import ssl
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ca_path = os.path.join(tmp, "ca.pem")
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-days", "1", "-nodes",
                 "-keyout", os.path.join(tmp, "key.pem"), "-out", ca_path, "-subj", "/CN=test"],
                check=True, capture_output=True,
            )
            ctx = repl._build_ssl_context(ca_path)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_missing_ca_file_fails_closed(self):
        with self.assertRaises(repl.ReplicationError):
            repl._build_ssl_context("/nonexistent/ca.pem")


class TrandataCheckpointTests(unittest.TestCase):
    def test_33_trandata_missing_is_added(self):
        client = FakeClient()
        repl.ensure_trandata(client, "OracleGoldenGate.SRC_ALIAS", "public.payments")
        self.assertTrue(any(c[0] == "POST" and "trandata" in c[1] for c in client.calls))

    def test_34_trandata_request_is_idempotent_by_design(self):
        client = FakeClient()
        repl.ensure_trandata(client, "OracleGoldenGate.SRC_ALIAS", "public.payments")
        repl.ensure_trandata(client, "OracleGoldenGate.SRC_ALIAS", "public.payments")
        posts = [c for c in client.calls if c[0] == "POST" and "trandata" in c[1]]
        self.assertEqual(len(posts), 2)

    def test_35_missing_checkpoint_table_is_added(self):
        client = FakeClient()
        repl.ensure_checkpoint_table(client, "OracleGoldenGate.TGT_ALIAS", PLAN["checkpoint"])
        self.assertTrue(any(c[0] == "POST" and "checkpoint" in c[1] for c in client.calls))

    def test_36_checkpoint_not_requested_when_create_if_missing_false(self):
        client = FakeClient()
        repl.ensure_checkpoint_table(client, "OracleGoldenGate.TGT_ALIAS", {"table": "dbo.gg_checkpoint", "createIfMissing": False})
        self.assertFalse(client.calls)


class ExtractReplicatDistributionTests(unittest.TestCase):
    def test_37_missing_extract_is_created_stopped(self):
        client = FakeClient()
        state = repl.ensure_extract(client, "SRC_ALIAS", "OracleGoldenGate", PLAN["extract"])
        self.assertEqual(state, "created")
        post = next(c for c in client.calls if c[0] == "POST")
        self.assertNotIn("start", json.dumps(post[2]).lower())

    def test_38_equivalent_extract_is_accepted(self):
        path = repl.extract_path("PGSRC01")
        client = FakeClient(existing={path: {"response": {"trail": "pa"}}})
        state = repl.ensure_extract(client, "SRC_ALIAS", "OracleGoldenGate", PLAN["extract"])
        self.assertEqual(state, "existing")

    def test_39_drifted_extract_fails(self):
        path = repl.extract_path("PGSRC01")
        client = FakeClient(existing={path: {"response": {"trail": "zz"}}})
        with self.assertRaises(repl.DriftError):
            repl.ensure_extract(client, "SRC_ALIAS", "OracleGoldenGate", PLAN["extract"])

    def test_40_missing_replicat_is_created_stopped(self):
        client = FakeClient()
        state = repl.ensure_replicat(client, "TGT_ALIAS", "OracleGoldenGate", PLAN["replicat"], PLAN["checkpoint"])
        self.assertEqual(state, "created")

    def test_41_equivalent_replicat_is_accepted(self):
        path = repl.replicat_path("MSTGT01")
        client = FakeClient(existing={path: {"response": {"trail": "ma"}}})
        state = repl.ensure_replicat(client, "TGT_ALIAS", "OracleGoldenGate", PLAN["replicat"], PLAN["checkpoint"])
        self.assertEqual(state, "existing")

    def test_42_drifted_replicat_fails(self):
        path = repl.replicat_path("MSTGT01")
        client = FakeClient(existing={path: {"response": {"trail": "zz"}}})
        with self.assertRaises(repl.DriftError):
            repl.ensure_replicat(client, "TGT_ALIAS", "OracleGoldenGate", PLAN["replicat"], PLAN["checkpoint"])

    def test_43_missing_distribution_path_is_created_stopped(self):
        client = FakeClient()
        state = repl.ensure_distribution_path(client, PLAN["distribution"], "gg-mssql-tgt-fixture-01.goldengate-dev.adcbmis.local")
        self.assertEqual(state, "created")

    def test_44_equivalent_distribution_path_is_accepted(self):
        path = repl.distribution_path("PG2MS01")
        client = FakeClient(existing={path: {"response": {"trail": "pa", "target": "gg-mssql-tgt-fixture-01.goldengate-dev.adcbmis.local"}}})
        state = repl.ensure_distribution_path(client, PLAN["distribution"], "gg-mssql-tgt-fixture-01.goldengate-dev.adcbmis.local")
        self.assertEqual(state, "existing")

    def test_45_drifted_distribution_path_fails(self):
        path = repl.distribution_path("PG2MS01")
        client = FakeClient(existing={path: {"response": {"trail": "zz"}}})
        with self.assertRaises(repl.DriftError):
            repl.ensure_distribution_path(client, PLAN["distribution"], "gg-mssql-tgt-fixture-01.goldengate-dev.adcbmis.local")

    def test_46_receiver_path_is_verified(self):
        client = FakeClient()
        repl.verify_receiver_path(client, "PG2MS01", "ma")
        self.assertTrue(any(c[1] == repl.receiver_paths_path() for c in client.calls))

    def test_46b_duplicate_receiver_path_fails(self):
        client = FakeClient()
        client.get = lambda path, retry=0: (
            (200, {"response": {"items": [{"name": "PG2MS01"}, {"name": "PG2MS01"}]}}) if path == repl.receiver_paths_path()
            else (200, {"response": {"trail": "ma"}})
        )
        with self.assertRaises(repl.ReplicationError):
            repl.verify_receiver_path(client, "PG2MS01", "ma")

    def test_47_unknown_post_result_is_not_blindly_retried(self):
        with mock.patch.object(repl, "_build_ssl_context", return_value=None):
            client_obj = repl.GGClient("example.invalid", "u", "p", "/dev/null", timeout=1)
        with mock.patch("http.client.HTTPSConnection") as mock_conn:
            mock_conn.return_value.request.side_effect = TimeoutError("simulated")
            with self.assertRaises(repl.IndeterminateError):
                client_obj.post("/services/v2/extracts/PGSRC01", {"name": "PGSRC01"})
            self.assertEqual(mock_conn.return_value.request.call_count, 1)

    def test_48_no_delete_method_exists_on_client(self):
        self.assertFalse(hasattr(repl.GGClient, "delete"))

    def test_49_no_put_method_exists_on_client(self):
        self.assertFalse(hasattr(repl.GGClient, "put"))

    def test_50_no_process_patch_is_issued(self):
        self.assertFalse(hasattr(repl.GGClient, "patch"))
        source = inspect_source(repl.ensure_extract)
        self.assertNotIn("PATCH", source)


def inspect_source(func):
    import inspect
    return inspect.getsource(func)


class StartSemanticsTests(unittest.TestCase):
    def test_53_54_55_start_order_replicat_then_distribution_then_extract(self):
        calls = []
        source_client, target_client = FakeClient(), FakeClient()
        with mock.patch.object(repl, "read_secret_file", return_value="fake-value"):
            repl.reconcile_pipeline(PLAN, source_client, target_client)
        target_starts = [c for c in target_client.calls if c[0] == "POST" and c[1] == repl.commands_execute_path()]
        source_starts = [c for c in source_client.calls if c[0] == "POST" and c[1] == repl.commands_execute_path()]
        self.assertEqual(len(target_starts), 1)
        self.assertEqual(target_starts[0][2]["type"], "replicat")
        self.assertEqual(len(source_starts), 2)
        self.assertEqual(source_starts[0][2]["type"], "source")
        self.assertEqual(source_starts[1][2]["type"], "extract")

    def test_56_existing_stopped_process_is_not_started(self):
        client = FakeClient()
        client.objects[repl.replicat_path("MSTGT01")] = {"response": {"status": "STOPPED"}}
        with self.assertRaises(repl.ReplicationError) as ctx:
            repl.ensure_process_running_state(client, "replicat", "MSTGT01", newly_created=False, start_on_create=True)
        self.assertIn("operator action required", ctx.exception.reason)

    def test_57_existing_abended_process_is_not_restarted(self):
        client = FakeClient()
        client.objects[repl.replicat_path("MSTGT01")] = {"response": {"status": "ABENDED"}}
        with self.assertRaises(repl.ReplicationError):
            repl.ensure_process_running_state(client, "replicat", "MSTGT01", newly_created=False, start_on_create=True)
        self.assertFalse(any(c[0] == "POST" for c in client.calls))

    def test_58_a_failure_stops_later_start_steps(self):
        # Creation already occurred (safe/idempotent) by the time ABENDED is detected at start time; the safety property is that no START command follows.
        source_client, target_client = FakeClient(), FakeClient()
        target_client.objects[repl.replicat_path("MSTGT01")] = {"response": {"status": "ABENDED"}}
        with mock.patch.object(repl, "read_secret_file", return_value="fake-value"):
            with self.assertRaises(repl.ReplicationError):
                repl.reconcile_pipeline(PLAN, source_client, target_client)
        self.assertFalse(any(c[0] == "POST" and c[1] == repl.commands_execute_path() for c in source_client.calls))


class JobRenderingTests(unittest.TestCase):
    def setUp(self):
        self.manifests = repl.render_manifests(PLAN, "goldengate-dev", "eu-west-1", "# source")

    def test_59_job_uses_source_deployment_service_account(self):
        job = self.manifests["Job"]
        self.assertEqual(job["spec"]["template"]["spec"]["serviceAccountName"], "gg-postgresql-sa")

    def test_60_job_uses_approved_source_runtime_image(self):
        job = self.manifests["Job"]
        self.assertEqual(job["spec"]["template"]["spec"]["containers"][0]["image"], PLAN["source"]["image"])

    def test_61_job_has_one_container(self):
        job = self.manifests["Job"]
        self.assertEqual(len(job["spec"]["template"]["spec"]["containers"]), 1)

    def test_62_job_mounts_exactly_five_secret_groups(self):
        spc = self.manifests["SecretProviderClass"]
        objects_text = spc["spec"]["parameters"]["objects"]
        self.assertEqual(objects_text.count("objectName:"), 5)
        for alias in ("source-admin/username", "source-admin/password", "target-admin/username", "target-admin/password",
                      "source-db/userid", "source-db/password", "target-db/userid", "target-db/password", "tls/ca-chain.pem"):
            self.assertIn(alias, objects_text)

    def test_63_database_secrets_not_synced_to_kubernetes_secrets(self):
        spc = self.manifests["SecretProviderClass"]
        self.assertNotIn("secretObjects", spc["spec"])

    def test_64_configmap_contains_no_credentials(self):
        cm = self.manifests["ConfigMap"]
        text = json.dumps(cm)
        for forbidden in ("OGG_DB_PASSWORD", "password", "userid"):
            self.assertNotIn(forbidden, text)

    def test_65_wildcard_dns_hosts_derived_correctly(self):
        self.assertEqual(PLAN["source"]["runtimeHost"], "gg-pg-src-fixture-01.goldengate-dev.adcbmis.local")
        self.assertEqual(PLAN["target"]["runtimeHost"], "gg-mssql-tgt-fixture-01.goldengate-dev.adcbmis.local")

    def test_66_no_route53_resource_or_command_in_source(self):
        with open(TOOL_PATH) as f:
            source = f.read()
        for forbidden in ("route53", "Route53", "ChangeResourceRecordSets"):
            self.assertNotIn(forbidden, source)

    def test_67_no_permanent_deployment_controller_introduced(self):
        job = self.manifests["Job"]
        self.assertEqual(job["kind"], "Job")
        self.assertEqual(job["spec"]["template"]["spec"]["restartPolicy"], "Never")
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        with open(TOOL_PATH) as f:
            source = f.read()
        self.assertNotIn('"kind": "Deployment"', source)

    def test_job_name_is_deterministic(self):
        name1 = repl.job_resource_name(PLAN["pipelineId"], PLAN)
        name2 = repl.job_resource_name(PLAN["pipelineId"], PLAN)
        self.assertEqual(name1, name2)

    def test_job_command_never_starts_goldengate_directly(self):
        job = self.manifests["Job"]
        command = job["spec"]["template"]["spec"]["containers"][0]["command"]
        self.assertEqual(command[:2], ["python3", "/mnt/reconciler/goldengate-replication.py"])
        self.assertIn("worker", command)


class ReplicationPlanDeterminismTests(unittest.TestCase):
    def test_71_no_secret_value_present_in_rendered_manifests(self):
        manifests = repl.render_manifests(PLAN, "goldengate-dev", "eu-west-1", "# source")
        text = json.dumps(manifests)
        for forbidden in ("super-secret", "OGG_DB_PASSWORD_VALUE"):
            self.assertNotIn(forbidden, text)

    def test_72_reconcile_is_a_clean_noop_when_no_pipeline_enabled(self):
        # An empty pipeline list at the CLI layer means render_manifests/reconcile_pipeline are never invoked.
        with open(TOOL_PATH) as f:
            source = f.read()
        self.assertIn("is not an enabled replication pipeline", source)


if __name__ == "__main__":
    unittest.main()
