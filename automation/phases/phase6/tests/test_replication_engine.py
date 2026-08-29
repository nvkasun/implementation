"""Offline tests for automation/goldengate-replication.py; run directly via `python3 automation/phases/phase6/tests/test_replication_engine.py`."""
from __future__ import annotations

import importlib.util
import inspect
import json
import os
import unittest
import unittest.mock as mock
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parents[4])
TOOL_PATH = os.path.join(REPO_ROOT, "automation", "goldengate-replication.py")


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
        "serviceAccount": "gg-runtime-sa",
        "image": "229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-postgresql:23.26.2.0.1",
        "adminSecret": "dev/goldengate/source/admin",
        "databaseSecret": "dev/goldengate/databases/payments-pg-to-mssql-001/source",
        "databaseCredentialAlias": "SRC_ALIAS", "databaseCredentialDomain": "OracleGoldenGate",
    },
    "target": {
        "deploymentId": "gg-mssql-tgt-fixture-01", "deploymentType": "mssql",
        "runtimeHost": "gg-mssql-tgt-fixture-01.goldengate-dev.adcbmis.local",
        "serviceAccount": "gg-runtime-sa",
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
        "trail": {"name": "pa", "sizeMB": 500}, "tables": ["public.payments"],
    },
    "distribution": {
        "pathName": "PG2MS01", "sourceTrailName": "pa", "targetTrailName": "ma",
        "protocol": "wss", "port": 443, "startOnCreate": True,
    },
}


def _extract_response(alias="SRC_ALIAS", domain="OracleGoldenGate"):
    return {"response": {
        "source": "tranlogs", "pluginType": "pgoutput",
        "credentials": {"alias": alias, "domain": domain},
        "targets": [{"name": "pa", "type": "trail", "fileSize": 500}],
        "config": repl._generate_extract_config(PLAN["extract"], alias, domain),
    }}


def _replicat_response(alias="TGT_ALIAS", domain="OracleGoldenGate"):
    return {"response": {
        "source": {"name": "ma", "type": "trail"},
        "credentials": {"alias": alias, "domain": domain},
        "checkpoint": {"table": "dbo.gg_checkpoint"},
        "mode": {"type": "nonintegrated", "parallel": False},
        "config": repl._generate_replicat_config(PLAN["replicat"], alias, domain),
    }}


def _distribution_response(target_host="gg-mssql-tgt-fixture-01.goldengate-dev.adcbmis.local"):
    return {"response": {
        "targetInitiated": False,
        "source": {"uri": "localtrail:pa"},
        "target": {
            "uri": f"wss://{target_host}:443/services/v2/targets?trail=ma",
            "authenticationMethod": {"alias": "NET_TEST", "domain": "Network"},
        },
    }}


class FakeClient:
    """In-memory GET/POST/PATCH double using sanitized Oracle-contract response shapes; never issues DELETE or credential/config PUT."""

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
            return 200, {"response": {"items": [{"name": "PG2MS01", "trail": "ma"}]}}
        return 404, None

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        if path == repl.commands_execute_path():
            return 200, {}
        if "trandata" in path:
            if body.get("operation") == "info":
                return 200, {"response": {"loggingEnabled": False}}
            return 200, {}
        if "checkpoint" in path:
            if body.get("operation") == "info":
                return 200, {"response": {"exists": False}}
            return 200, {}
        if "extracts" in path:
            self.objects[path] = _extract_response(body["credentials"]["alias"], body["credentials"]["domain"])
        elif "replicats" in path:
            self.objects[path] = _replicat_response(body["credentials"]["alias"], body["credentials"]["domain"])
        elif "sources" in path:
            self.objects[path] = _distribution_response(body["target"]["uri"].split("//")[1].split(":")[0])
        elif "credentials" in path:
            pass
        return 201, {}

    def patch(self, path, body):
        self.calls.append(("PATCH", path, body))
        return 200, {}


def inspect_source(func):
    return inspect.getsource(func)


class WorkerStdlibOnlyTests(unittest.TestCase):
    def test_8_worker_mode_does_not_require_pyyaml(self):
        import sys

        class _BlockYaml:
            def find_module(self, name, path=None):
                return self if name == "yaml" else None

            def load_module(self, name):
                raise ImportError("yaml is deliberately unavailable in this test")

        blocker = _BlockYaml()
        sys.meta_path.insert(0, blocker)
        saved = sys.modules.pop("yaml", None)
        try:
            spec = importlib.util.spec_from_file_location("goldengate_replication_no_yaml", TOOL_PATH)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with mock.patch.object(module, "read_secret_file", return_value="fake-value"):
                src_client, tgt_client = FakeClient(), FakeClient()
                module.reconcile_pipeline(PLAN, src_client, tgt_client)
        finally:
            sys.meta_path.remove(blocker)
            if saved is not None:
                sys.modules["yaml"] = saved

    def test_worker_never_imports_deployment_model_module(self):
        source = inspect_source(repl.reconcile_pipeline)
        self.assertNotIn("_gdm", source)

    def test_import_statement_is_not_module_level(self):
        with open(TOOL_PATH) as f:
            lines = f.read().splitlines()
        module_level_import_lines = [l for l in lines if l == "import yaml"]
        self.assertEqual(module_level_import_lines, [])
        self.assertIn("    import yaml", lines)


class CredentialContractTests(unittest.TestCase):
    def test_3_credential_post_body_has_userid_password_no_alias(self):
        client = FakeClient()
        repl.ensure_database_credential(client, "OracleGoldenGate", "SRC_ALIAS", "u", "p")
        post = next(c for c in client.calls if c[0] == "POST" and "credentials" in c[1])
        self.assertEqual(set(post[2].keys()), {"userid", "password"})
        self.assertNotIn("alias", post[2])

    def test_29_get_existing_credential_succeeds(self):
        path = repl.credential_path("OracleGoldenGate", "SRC_ALIAS")
        client = FakeClient(existing={path: {"response": {}}})
        repl.ensure_database_credential(client, "OracleGoldenGate", "SRC_ALIAS", "u", "p")
        self.assertIn(("GET", path), client.calls)
        self.assertFalse(any(c[0] == "POST" and c[1] == path for c in client.calls))

    def test_30_missing_credential_is_created_once(self):
        client = FakeClient()
        repl.ensure_database_credential(client, "OracleGoldenGate", "SRC_ALIAS", "u", "p")
        posts = [c for c in client.calls if c[0] == "POST" and "credentials" in c[1]]
        self.assertEqual(len(posts), 1)

    def test_31_existing_credential_is_never_replaced(self):
        path = repl.credential_path("OracleGoldenGate", "SRC_ALIAS")
        client = FakeClient(existing={path: {"response": {}}})
        repl.ensure_database_credential(client, "OracleGoldenGate", "SRC_ALIAS", "u", "p")
        self.assertNotIn((path,), [c[1:2] for c in client.calls if c[0] == "POST"])

    def test_32_invalid_database_credential_fails(self):
        valid_path = repl.credential_valid_path("OracleGoldenGate", "SRC_ALIAS")
        client = FakeClient(statuses={valid_path: 200})
        client.objects[valid_path] = {"valid": False}
        with self.assertRaises(repl.ReplicationError):
            repl.ensure_database_credential(client, "OracleGoldenGate", "SRC_ALIAS", "u", "p")

    def test_network_alias_never_calls_valid_endpoint(self):
        client = FakeClient()
        repl.ensure_network_credential(client, "Network", "NET_TEST", "u", "p")
        self.assertFalse(any("/valid" in c[1] for c in client.calls))

    def test_network_credential_post_body_has_no_alias(self):
        client = FakeClient()
        repl.ensure_network_credential(client, "Network", "NET_TEST", "u", "p")
        post = next(c for c in client.calls if c[0] == "POST")
        self.assertNotIn("alias", post[2])

    def test_network_credential_never_replaced(self):
        path = repl.credential_path("Network", "NET_TEST")
        client = FakeClient(existing={path: {"response": {}}})
        repl.ensure_network_credential(client, "Network", "NET_TEST", "u", "p")
        self.assertFalse(any(c[0] == "POST" for c in client.calls))

    def test_51_no_credential_value_appears_in_error_reason(self):
        client = FakeClient()
        try:
            repl.ensure_database_credential(client, "OracleGoldenGate", "SRC_ALIAS", "super-secret-user", "super-secret-pass")
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


class TrandataCheckpointContractTests(unittest.TestCase):
    def test_trandata_info_runs_before_add(self):
        client = FakeClient()
        repl.ensure_trandata(client, "OracleGoldenGate.SRC_ALIAS", "public.payments")
        posts = [c for c in client.calls if c[0] == "POST" and "trandata" in c[1]]
        self.assertEqual(posts[0][2]["operation"], "info")
        self.assertEqual(posts[1][2]["operation"], "add")

    def test_trandata_body_has_operation_and_tableName(self):
        client = FakeClient()
        repl.ensure_trandata(client, "OracleGoldenGate.SRC_ALIAS", "public.payments")
        for _method, _path, body in [c for c in client.calls if c[0] == "POST"]:
            self.assertEqual(set(body.keys()), {"operation", "tableName"})

    def test_33_trandata_missing_is_added(self):
        client = FakeClient()
        repl.ensure_trandata(client, "OracleGoldenGate.SRC_ALIAS", "public.payments")
        add_calls = [c for c in client.calls if c[0] == "POST" and c[2].get("operation") == "add"]
        self.assertEqual(len(add_calls), 1)

    def test_34_existing_trandata_is_not_modified(self):
        client = FakeClient()
        client.post = lambda path, body: (200, {"response": {"loggingEnabled": True}})
        repl.ensure_trandata(client, "OracleGoldenGate.SRC_ALIAS", "public.payments")

    def test_trandata_unrecognized_info_response_fails_closed(self):
        client = FakeClient()
        client.post = lambda path, body: (200, {"response": {}})
        with self.assertRaises(repl.ReplicationError):
            repl.ensure_trandata(client, "OracleGoldenGate.SRC_ALIAS", "public.payments")

    def test_checkpoint_info_runs_before_add(self):
        client = FakeClient()
        repl.ensure_checkpoint_table(client, "OracleGoldenGate.TGT_ALIAS", PLAN["checkpoint"])
        posts = [c for c in client.calls if c[0] == "POST" and "checkpoint" in c[1]]
        self.assertEqual(posts[0][2]["operation"], "info")
        self.assertEqual(posts[1][2]["operation"], "add")

    def test_checkpoint_body_has_operation_and_name(self):
        client = FakeClient()
        repl.ensure_checkpoint_table(client, "OracleGoldenGate.TGT_ALIAS", PLAN["checkpoint"])
        for _method, _path, body in [c for c in client.calls if c[0] == "POST"]:
            self.assertEqual(set(body.keys()), {"operation", "name"})

    def test_35_missing_checkpoint_table_is_added(self):
        client = FakeClient()
        repl.ensure_checkpoint_table(client, "OracleGoldenGate.TGT_ALIAS", PLAN["checkpoint"])
        add_calls = [c for c in client.calls if c[0] == "POST" and c[2].get("operation") == "add"]
        self.assertEqual(len(add_calls), 1)

    def test_36_existing_checkpoint_table_is_not_modified(self):
        client = FakeClient()
        client.post = lambda path, body: (200, {"response": {"exists": True}})
        repl.ensure_checkpoint_table(client, "OracleGoldenGate.TGT_ALIAS", PLAN["checkpoint"])

    def test_checkpoint_absent_without_create_if_missing_fails(self):
        client = FakeClient()
        client.post = lambda path, body: (200, {"response": {"exists": False}})
        with self.assertRaises(repl.ReplicationError):
            repl.ensure_checkpoint_table(client, "OracleGoldenGate.TGT_ALIAS", {"table": "dbo.gg_checkpoint", "createIfMissing": False})


class ExtractContractTests(unittest.TestCase):
    def test_extract_uses_config_source_pluginType_targets_credentials_status(self):
        client = FakeClient()
        repl.ensure_extract(client, "SRC_ALIAS", "OracleGoldenGate", PLAN["extract"])
        post = next(c for c in client.calls if c[0] == "POST" and "extracts" in c[1])
        body = post[2]
        for key in ("config", "source", "pluginType", "targets", "credentials", "status"):
            self.assertIn(key, body)
        self.assertEqual(body["source"], "tranlogs")
        self.assertEqual(body["status"], "stopped")

    def test_37_missing_extract_is_created_stopped(self):
        client = FakeClient()
        state = repl.ensure_extract(client, "SRC_ALIAS", "OracleGoldenGate", PLAN["extract"])
        self.assertEqual(state, "created")

    def test_38_equivalent_extract_is_accepted(self):
        path = repl.extract_path("PGSRC01")
        client = FakeClient(existing={path: _extract_response()})
        state = repl.ensure_extract(client, "SRC_ALIAS", "OracleGoldenGate", PLAN["extract"])
        self.assertEqual(state, "existing")

    def test_39_drifted_extract_fails(self):
        path = repl.extract_path("PGSRC01")
        drifted = _extract_response()
        drifted["response"]["pluginType"] = "test_decoding"
        client = FakeClient(existing={path: drifted})
        with self.assertRaises(repl.DriftError):
            repl.ensure_extract(client, "SRC_ALIAS", "OracleGoldenGate", PLAN["extract"])

    def test_8_missing_required_response_field_fails_closed_not_equivalent(self):
        path = repl.extract_path("PGSRC01")
        incomplete = {"response": {"source": "tranlogs", "pluginType": "pgoutput"}}
        client = FakeClient(existing={path: incomplete})
        with self.assertRaises(repl.ReplicationError) as ctx:
            repl.ensure_extract(client, "SRC_ALIAS", "OracleGoldenGate", PLAN["extract"])
        self.assertNotIsInstance(ctx.exception, repl.DriftError)

    def test_8_extract_normalizer_compares_more_than_trail(self):
        source = inspect_source(repl._normalize_extract_actual)
        for field in ("source", "pluginType", "credentials", "targets", "config"):
            self.assertIn(field, source)


class ReplicatContractTests(unittest.TestCase):
    def test_replicat_uses_config_source_checkpoint_mode_credentials_status(self):
        client = FakeClient()
        repl.ensure_replicat(client, "TGT_ALIAS", "OracleGoldenGate", PLAN["replicat"], PLAN["checkpoint"])
        post = next(c for c in client.calls if c[0] == "POST" and "replicats" in c[1])
        body = post[2]
        for key in ("config", "source", "checkpoint", "mode", "credentials", "status"):
            self.assertIn(key, body)
        self.assertEqual(body["mode"], {"type": "nonintegrated", "parallel": False})
        self.assertEqual(body["status"], "stopped")

    def test_40_missing_replicat_is_created_stopped(self):
        client = FakeClient()
        state = repl.ensure_replicat(client, "TGT_ALIAS", "OracleGoldenGate", PLAN["replicat"], PLAN["checkpoint"])
        self.assertEqual(state, "created")

    def test_41_equivalent_replicat_is_accepted(self):
        path = repl.replicat_path("MSTGT01")
        client = FakeClient(existing={path: _replicat_response()})
        state = repl.ensure_replicat(client, "TGT_ALIAS", "OracleGoldenGate", PLAN["replicat"], PLAN["checkpoint"])
        self.assertEqual(state, "existing")

    def test_42_drifted_replicat_fails(self):
        path = repl.replicat_path("MSTGT01")
        drifted = _replicat_response()
        drifted["response"]["mode"]["parallel"] = True
        client = FakeClient(existing={path: drifted})
        with self.assertRaises(repl.DriftError):
            repl.ensure_replicat(client, "TGT_ALIAS", "OracleGoldenGate", PLAN["replicat"], PLAN["checkpoint"])

    def test_10_missing_required_response_field_fails_closed(self):
        path = repl.replicat_path("MSTGT01")
        incomplete = {"response": {"source": {"name": "ma"}}}
        client = FakeClient(existing={path: incomplete})
        with self.assertRaises(repl.ReplicationError) as ctx:
            repl.ensure_replicat(client, "TGT_ALIAS", "OracleGoldenGate", PLAN["replicat"], PLAN["checkpoint"])
        self.assertNotIsInstance(ctx.exception, repl.DriftError)

    def test_10_replicat_normalizer_compares_more_than_trail(self):
        source = inspect_source(repl._normalize_replicat_actual)
        for field in ("source", "credentials", "checkpoint", "mode", "config"):
            self.assertIn(field, source)

    def test_replicat_never_enables_parallel_integrated_or_ddl(self):
        client = FakeClient()
        repl.ensure_replicat(client, "TGT_ALIAS", "OracleGoldenGate", PLAN["replicat"], PLAN["checkpoint"])
        post = next(c for c in client.calls if c[0] == "POST" and "replicats" in c[1])
        self.assertEqual(post[2]["mode"]["type"], "nonintegrated")
        self.assertFalse(post[2]["mode"]["parallel"])


class StartCommandContractTests(unittest.TestCase):
    def test_10_start_command_uses_name_processName_processType(self):
        client = FakeClient()
        repl.start_process(client, "extract", "PGSRC01")
        post = next(c for c in client.calls if c[0] == "POST")
        self.assertEqual(post[2], {"name": "start", "processName": "PGSRC01", "processType": "extract"})

    def test_start_replicat_process_type(self):
        client = FakeClient()
        repl.start_process(client, "replicat", "MSTGT01")
        post = next(c for c in client.calls if c[0] == "POST")
        self.assertEqual(post[2]["processType"], "replicat")


class DistributionContractTests(unittest.TestCase):
    def test_distribution_uses_source_target_uri_and_authenticationMethod(self):
        client = FakeClient()
        repl.ensure_distribution_path(client, PLAN["distribution"], "gg-mssql-tgt-fixture-01.goldengate-dev.adcbmis.local", "NET_TEST", "Network")
        post = next(c for c in client.calls if c[0] == "POST" and "sources" in c[1])
        body = post[2]
        self.assertEqual(body["targetInitiated"], False)
        self.assertEqual(body["status"], "stopped")
        self.assertIn("uri", body["source"])
        self.assertIn("uri", body["target"])
        self.assertEqual(body["target"]["authenticationMethod"], {"alias": "NET_TEST", "domain": "Network"})

    def test_12_network_alias_referenced_by_distribution_request(self):
        client = FakeClient()
        repl.ensure_distribution_path(client, PLAN["distribution"], "gg-mssql-tgt-fixture-01.goldengate-dev.adcbmis.local", "NET_TEST", "Network")
        post = next(c for c in client.calls if c[0] == "POST" and "sources" in c[1])
        self.assertEqual(post[2]["target"]["authenticationMethod"]["alias"], "NET_TEST")

    def test_distribution_target_uri_uses_wss_443_and_target_trail(self):
        client = FakeClient()
        repl.ensure_distribution_path(client, PLAN["distribution"], "gg-mssql-tgt-fixture-01.goldengate-dev.adcbmis.local", "NET_TEST", "Network")
        post = next(c for c in client.calls if c[0] == "POST" and "sources" in c[1])
        self.assertEqual(post[2]["target"]["uri"], "wss://gg-mssql-tgt-fixture-01.goldengate-dev.adcbmis.local:443/services/v2/targets?trail=ma")

    def test_43_missing_distribution_path_is_created_stopped(self):
        client = FakeClient()
        state = repl.ensure_distribution_path(client, PLAN["distribution"], "gg-mssql-tgt-fixture-01.goldengate-dev.adcbmis.local", "NET_TEST", "Network")
        self.assertEqual(state, "created")

    def test_44_equivalent_distribution_path_is_accepted(self):
        path = repl.distribution_path("PG2MS01")
        client = FakeClient(existing={path: _distribution_response()})
        state = repl.ensure_distribution_path(client, PLAN["distribution"], "gg-mssql-tgt-fixture-01.goldengate-dev.adcbmis.local", "NET_TEST", "Network")
        self.assertEqual(state, "existing")

    def test_45_drifted_distribution_path_fails(self):
        path = repl.distribution_path("PG2MS01")
        drifted = _distribution_response()
        drifted["response"]["targetInitiated"] = True
        client = FakeClient(existing={path: drifted})
        with self.assertRaises(repl.DriftError):
            repl.ensure_distribution_path(client, PLAN["distribution"], "gg-mssql-tgt-fixture-01.goldengate-dev.adcbmis.local", "NET_TEST", "Network")

    def test_14_missing_required_field_fails_closed(self):
        path = repl.distribution_path("PG2MS01")
        incomplete = {"response": {"targetInitiated": False}}
        client = FakeClient(existing={path: incomplete})
        with self.assertRaises(repl.ReplicationError) as ctx:
            repl.ensure_distribution_path(client, PLAN["distribution"], "gg-mssql-tgt-fixture-01.goldengate-dev.adcbmis.local", "NET_TEST", "Network")
        self.assertNotIsInstance(ctx.exception, repl.DriftError)

    def test_13_distribution_status_change_uses_patch_not_commands_execute(self):
        client = FakeClient()
        repl.start_distribution_path(client, "PG2MS01")
        self.assertTrue(any(c[0] == "PATCH" for c in client.calls))
        self.assertFalse(any(c[0] == "POST" and c[1] == repl.commands_execute_path() for c in client.calls))

    def test_13_patch_never_used_for_credentials_extract_replicat(self):
        for func in (repl.ensure_database_credential, repl.ensure_network_credential, repl.ensure_extract, repl.ensure_replicat):
            self.assertNotIn("patch(", inspect_source(func))

    def test_13_patch_body_is_minimal_status_transition(self):
        client = FakeClient()
        repl.start_distribution_path(client, "PG2MS01")
        patch_call = next(c for c in client.calls if c[0] == "PATCH")
        self.assertEqual(patch_call[2], {"status": "running"})


class ReceiverContractTests(unittest.TestCase):
    def test_15_receiver_matches_by_trail_not_assumed_same_name(self):
        client = FakeClient()
        client.get = lambda path, retry=0: (200, {"response": {"items": [{"name": "SOME-OTHER-NAME", "trail": "ma"}]}}) if path == "/services/v2/targets" else (404, None)
        repl.verify_receiver_path(client, "ma")

    def test_15_no_automatic_detail_get_by_assumed_path_name(self):
        source = inspect_source(repl.verify_receiver_path)
        self.assertNotIn("receiver_path_detail_path", source)

    def test_46_receiver_path_is_verified(self):
        client = FakeClient()
        repl.verify_receiver_path(client, "ma")
        self.assertTrue(any(c[1] == repl.receiver_paths_path() for c in client.calls))

    def test_46b_duplicate_receiver_trail_fails(self):
        client = FakeClient()
        client.get = lambda path, retry=0: (200, {"response": {"items": [{"name": "A", "trail": "ma"}, {"name": "B", "trail": "ma"}]}})
        with self.assertRaises(repl.ReplicationError):
            repl.verify_receiver_path(client, "ma")

    def test_receiver_no_match_fails_closed(self):
        client = FakeClient()
        client.get = lambda path, retry=0: (200, {"response": {"items": [{"name": "A", "trail": "zz"}]}})
        with self.assertRaises(repl.ReplicationError):
            repl.verify_receiver_path(client, "ma")

    def test_receiver_unrecognized_shape_fails_closed(self):
        client = FakeClient()
        client.get = lambda path, retry=0: (200, {"unexpected": "shape"})
        with self.assertRaises(repl.ReplicationError):
            repl.verify_receiver_path(client, "ma")


class TransportSafetyTests(unittest.TestCase):
    def test_47_unknown_post_result_is_not_blindly_retried(self):
        with mock.patch.object(repl, "_build_ssl_context", return_value=None):
            client_obj = repl.GGClient("example.invalid", "u", "p", "/dev/null", timeout=1)
        with mock.patch("http.client.HTTPSConnection") as mock_conn:
            mock_conn.return_value.request.side_effect = TimeoutError("simulated")
            with self.assertRaises(repl.IndeterminateError):
                client_obj.post("/services/v2/extracts/PGSRC01", {"name": "PGSRC01"})
            self.assertEqual(mock_conn.return_value.request.call_count, 1)

    def test_unknown_patch_result_is_not_blindly_retried(self):
        with mock.patch.object(repl, "_build_ssl_context", return_value=None):
            client_obj = repl.GGClient("example.invalid", "u", "p", "/dev/null", timeout=1)
        with mock.patch("http.client.HTTPSConnection") as mock_conn:
            mock_conn.return_value.request.side_effect = TimeoutError("simulated")
            with self.assertRaises(repl.IndeterminateError):
                client_obj.patch("/services/v2/sources/PG2MS01", {"status": "running"})
            self.assertEqual(mock_conn.return_value.request.call_count, 1)

    def test_48_no_delete_method_exists_on_client(self):
        self.assertFalse(hasattr(repl.GGClient, "delete"))

    def test_49_no_put_method_exists_on_client(self):
        self.assertFalse(hasattr(repl.GGClient, "put"))

    def test_50_patch_permitted_only_for_distribution_status(self):
        self.assertTrue(hasattr(repl.GGClient, "patch"))
        self.assertIn("Distribution", inspect_source(repl.GGClient.patch))


class StartSemanticsTests(unittest.TestCase):
    def test_53_54_55_start_order_replicat_then_distribution_then_extract(self):
        source_client, target_client = FakeClient(), FakeClient()
        with mock.patch.object(repl, "read_secret_file", return_value="fake-value"):
            repl.reconcile_pipeline(PLAN, source_client, target_client)
        target_starts = [c for c in target_client.calls if c[0] == "POST" and c[1] == repl.commands_execute_path()]
        self.assertEqual(len(target_starts), 1)
        self.assertEqual(target_starts[0][2]["processType"], "replicat")
        distribution_patches = [c for c in source_client.calls if c[0] == "PATCH"]
        self.assertEqual(len(distribution_patches), 1)
        extract_starts = [c for c in source_client.calls if c[0] == "POST" and c[1] == repl.commands_execute_path()]
        self.assertEqual(len(extract_starts), 1)
        self.assertEqual(extract_starts[0][2]["processType"], "extract")
        patch_index = source_client.calls.index(distribution_patches[0])
        extract_index = source_client.calls.index(extract_starts[0])
        self.assertLess(patch_index, extract_index)

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
        source_client, target_client = FakeClient(), FakeClient()
        target_client.objects[repl.replicat_path("MSTGT01")] = {"response": {"status": "ABENDED"}}
        with mock.patch.object(repl, "read_secret_file", return_value="fake-value"):
            with self.assertRaises(repl.ReplicationError):
                repl.reconcile_pipeline(PLAN, source_client, target_client)
        self.assertFalse(any(c[0] == "POST" and c[1] == repl.commands_execute_path() for c in source_client.calls))
        self.assertFalse(any(c[0] == "PATCH" for c in source_client.calls))


class FlatCsiAliasTests(unittest.TestCase):
    def setUp(self):
        self.manifests = repl.render_manifests(PLAN, "goldengate-dev", "eu-west-1", "# source", "test-exec-1")

    def test_9_flat_aliases_no_slashes(self):
        spc = self.manifests["SecretProviderClass"]
        objects_text = spc["spec"]["parameters"]["objects"]
        for alias in ("source-admin-username", "source-admin-password", "target-admin-username", "target-admin-password",
                      "source-db-userid", "source-db-password", "target-db-userid", "target-db-password", "tls-ca-chain.pem"):
            self.assertIn(alias, objects_text)
            self.assertNotIn(f"{alias}/", objects_text)

    def test_9_no_nested_slash_aliases_remain(self):
        spc = self.manifests["SecretProviderClass"]
        objects_text = spc["spec"]["parameters"]["objects"]
        for legacy in ("source-admin/username", "target-admin/username", "source-db/userid", "tls/ca-chain.pem"):
            self.assertNotIn(legacy, objects_text)

    def test_62_job_mounts_exactly_five_secret_groups(self):
        spc = self.manifests["SecretProviderClass"]
        objects_text = spc["spec"]["parameters"]["objects"]
        self.assertEqual(objects_text.count("objectName:"), 5)


class JobRerunSafeNamingTests(unittest.TestCase):
    def test_17_execution_id_separates_from_plan_checksum(self):
        desired = repl.desired_state_name(PLAN["pipelineId"], PLAN)
        name1 = repl.job_resource_name(PLAN["pipelineId"], PLAN, "111-1")
        name2 = repl.job_resource_name(PLAN["pipelineId"], PLAN, "222-1")
        self.assertTrue(name1.startswith(desired))
        self.assertTrue(name2.startswith(desired))
        self.assertNotEqual(name1, name2)

    def test_17_rerun_with_same_plan_different_execution_id_does_not_collide(self):
        name1 = repl.job_resource_name(PLAN["pipelineId"], PLAN, "111-1")
        name2 = repl.job_resource_name(PLAN["pipelineId"], PLAN, "111-2")
        self.assertNotEqual(name1, name2)

    def test_17_plan_checksum_kept_in_job_annotation(self):
        manifests = repl.render_manifests(PLAN, "goldengate-dev", "eu-west-1", "# source", "111-1")
        checksum = repl.plan_checksum(PLAN)
        self.assertEqual(manifests["Job"]["metadata"]["annotations"]["goldengate.adcb/plan-checksum"], checksum)

    def test_17_dry_run_execution_id_is_deterministic(self):
        name1 = repl.job_resource_name(PLAN["pipelineId"], PLAN, repl.DETERMINISTIC_DRY_RUN_EXECUTION_ID)
        name2 = repl.job_resource_name(PLAN["pipelineId"], PLAN, repl.DETERMINISTIC_DRY_RUN_EXECUTION_ID)
        self.assertEqual(name1, name2)

    def test_execution_id_sanitized_and_bounded(self):
        name = repl.job_resource_name(PLAN["pipelineId"], PLAN, "Run ID! 123/456")
        self.assertNotIn(" ", name)
        self.assertNotIn("!", name)
        self.assertNotIn("/", name)

    def test_empty_execution_id_rejected(self):
        with self.assertRaises(repl.ReplicationError):
            repl.job_resource_name(PLAN["pipelineId"], PLAN, "///")


class JobRenderingTests(unittest.TestCase):
    def setUp(self):
        self.manifests = repl.render_manifests(PLAN, "goldengate-dev", "eu-west-1", "# source", "test-exec-1")

    def test_59_job_uses_source_deployment_service_account(self):
        job = self.manifests["Job"]
        self.assertEqual(job["spec"]["template"]["spec"]["serviceAccountName"], "gg-runtime-sa")

    def test_job_service_account_is_taken_from_the_source_plan_identity(self):
        """Canonical shared runtime identity: render_job() must derive the Job ServiceAccount from plan["source"]["serviceAccount"] (never a hardcoded/per-engine literal) -- every singleRuntime deploymentType, including postgresql/mssql here, resolves the one platform-owned gg-runtime-sa."""
        job = self.manifests["Job"]
        self.assertEqual(job["spec"]["template"]["spec"]["serviceAccountName"], PLAN["source"]["serviceAccount"])
        self.assertEqual(PLAN["source"]["serviceAccount"], "gg-runtime-sa")

    def test_60_job_uses_approved_source_runtime_image(self):
        job = self.manifests["Job"]
        self.assertEqual(job["spec"]["template"]["spec"]["containers"][0]["image"], PLAN["source"]["image"])

    def test_61_job_has_one_container(self):
        job = self.manifests["Job"]
        self.assertEqual(len(job["spec"]["template"]["spec"]["containers"]), 1)

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

    def test_job_command_never_starts_goldengate_directly(self):
        job = self.manifests["Job"]
        command = job["spec"]["template"]["spec"]["containers"][0]["command"]
        self.assertEqual(command[:2], ["python3", "/mnt/reconciler/goldengate-replication.py"])
        self.assertIn("worker", command)


class ReplicationPlanDeterminismTests(unittest.TestCase):
    def test_71_no_secret_value_present_in_rendered_manifests(self):
        manifests = repl.render_manifests(PLAN, "goldengate-dev", "eu-west-1", "# source", "test-exec-1")
        text = json.dumps(manifests)
        for forbidden in ("super-secret", "OGG_DB_PASSWORD_VALUE"):
            self.assertNotIn(forbidden, text)

    def test_72_reconcile_is_a_clean_noop_when_no_pipeline_enabled(self):
        with open(TOOL_PATH) as f:
            source = f.read()
        self.assertIn("is not an enabled replication pipeline", source)


class Python36CompatibilityTests(unittest.TestCase):
    """The reconciliation Job runs automation/goldengate-replication.py inside the live source runtime image, whose Python is 3.6.8 -- these prove the two known 3.7+ incompatibilities are gone and the CLI's missing-command contract still holds."""

    def test_A_no_future_annotations_import(self):
        with open(TOOL_PATH) as f:
            source = f.read()
        self.assertNotIn("from __future__ import annotations", source)

    def test_B_subparsers_do_not_use_required_kwarg(self):
        source = inspect_source(repl.main)
        self.assertNotIn('add_subparsers(dest="command", required=True)', source)
        self.assertIn('add_subparsers(dest="command")', source)

    def test_C_missing_command_still_fails_closed(self):
        import contextlib
        import io

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                repl.main([])
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn("a command is required", stderr.getvalue())

    def test_D_verify_mode_never_imports_deployment_model_module(self):
        source = inspect_source(repl.verify_pipeline)
        self.assertNotIn("_gdm", source)

    def test_E_job_command_is_exact_worker_invocation_with_source_image(self):
        manifests = repl.render_manifests(PLAN, "goldengate-dev", "eu-west-1", "# source", "test-exec-1")
        container = manifests["Job"]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["image"], PLAN["source"]["image"])
        self.assertEqual(container["command"], [
            "python3", "/mnt/reconciler/goldengate-replication.py", "worker",
            "--plan", "/mnt/reconciler/plan.json",
            "--secrets-root", "/mnt/replication-secrets",
        ])


if __name__ == "__main__":
    unittest.main()
