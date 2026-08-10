"""Offline tests for hack/goldengate-deployment-model.py; run directly via `python3 hack/test-goldengate-deployment-model.py`."""
from __future__ import annotations

import copy
import importlib.util
import os
import sys
import tempfile
import unittest

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL_PATH = os.path.join(REPO_ROOT, "hack", "goldengate-deployment-model.py")


def _load_tool():
    spec = importlib.util.spec_from_file_location("goldengate_deployment_model", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gdm = _load_tool()

BASE_DESCRIPTOR = """\
deployment:
  enabled: {enabled}
  pipeline: {pipeline}
  role: {role}
{deployment_admin_secret_block}
global:
  environment: {environment}

deploymentModel: singleRuntime

runtime:
  deploymentType: {deployment_type}
  containerName: ogg-{deployment_type}
  image:
    repository: {repository}
    tag: "{tag}"
{service_account_block}  csi:
    enabled: true
{csi_role_arn_block}    admin:
      enabled: true
{csi_admin_object_name_block}    certificate:
      enabled: true
{csi_certificate_object_name_block}
ingress:
  hostDomain: {ingress_host_domain}
{alb_block}
{extra}
"""


def write_descriptor(root, environment, deployment_id, enabled=True, pipeline="test-pipeline", role="source",
                     deployment_type="oracle", repository=None, tag="1.0.0",
                     service_account_name=None, service_account_create=None,
                     deployment_admin_secret=None, alb_group_order=None, extra="", raw_override=None,
                     csi_admin_object_name=None, csi_certificate_object_name=None,
                     csi_service_account_role_arn=None, ingress_host_domain=None):
    folder = os.path.join(root, "envs", environment, deployment_id)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "values.yaml")
    if raw_override is not None:
        with open(path, "w") as f:
            f.write(raw_override)
        return path
    repository = repository or f"229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-{deployment_type}"

    deployment_admin_secret_block = ""
    if deployment_admin_secret is not None:
        deployment_admin_secret_block = f"  adminSecret:\n    name: {deployment_admin_secret}\n"

    service_account_block = ""
    if service_account_name is not None or service_account_create is not None:
        lines = ["  serviceAccount:"]
        if service_account_create is not None:
            lines.append(f"    create: {str(service_account_create).lower()}")
        if service_account_name is not None:
            lines.append(f"    name: {service_account_name}")
        service_account_block = "\n".join(lines) + "\n"

    csi_role_arn_block = f"    serviceAccountRoleArn: {csi_service_account_role_arn}\n" if csi_service_account_role_arn is not None else ""
    csi_admin_object_name_block = f"      objectName: {csi_admin_object_name}\n" if csi_admin_object_name is not None else ""
    csi_certificate_object_name_block = f"      objectName: {csi_certificate_object_name}\n" if csi_certificate_object_name is not None else ""

    alb_block = f'  alb:\n    groupOrder: "{alb_group_order}"' if alb_group_order is not None else ""
    if ingress_host_domain is None:
        ingress_host_domain = f"goldengate-{environment}.adcbmis.local"

    text = BASE_DESCRIPTOR.format(
        enabled=str(enabled).lower(), pipeline=pipeline, role=role, environment=environment,
        deployment_type=deployment_type, repository=repository, tag=tag,
        deployment_admin_secret_block=deployment_admin_secret_block,
        service_account_block=service_account_block,
        csi_role_arn_block=csi_role_arn_block,
        csi_admin_object_name_block=csi_admin_object_name_block,
        csi_certificate_object_name_block=csi_certificate_object_name_block,
        alb_block=alb_block, extra=extra, ingress_host_domain=ingress_host_domain)
    with open(path, "w") as f:
        f.write(text)
    return path


def default_source_doc(environment, pipeline, target_id):
    return {
        "deployment": {"enabled": True, "pipeline": pipeline, "role": "source"},
        "global": {"environment": environment},
        "deploymentModel": "singleRuntime",
        "runtime": {
            "deploymentType": "postgresql",
            "containerName": "ogg-postgresql",
            "image": {"repository": "229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-postgresql", "tag": "23.26.2.0.1"},
            "csi": {"enabled": True, "admin": {"enabled": True}, "certificate": {"enabled": True}},
        },
        "ingress": {"hostDomain": f"goldengate-{environment}.adcbmis.local"},
        "replication": {
            "enabled": True,
            "databaseCredentialSecret": f"{environment}/goldengate/databases/{pipeline}/source",
            "databaseCredential": {"domain": "OracleGoldenGate"},
            "supplementalLogging": {"enabled": True, "mode": "table", "objects": ["public.payments"]},
            "extract": {
                "enabled": True, "name": "PGSRC01", "description": "source extract",
                "pluginType": "pgoutput", "begin": "now",
                "trail": {"name": "pa", "sizeMB": 500, "subdirectory": ""},
                "tables": ["public.payments"], "startOnCreate": True,
            },
            "distribution": {
                "enabled": True, "pathName": "PG2MS01", "targetDeployment": target_id,
                "sourceTrailName": "pa", "targetTrailName": "ma",
                "protocol": "wss", "port": 443, "startOnCreate": True,
            },
            "checkpoint": {"enabled": False},
            "replicat": {"enabled": False},
        },
    }


def default_target_doc(environment, pipeline):
    return {
        "deployment": {"enabled": True, "pipeline": pipeline, "role": "target"},
        "global": {"environment": environment},
        "deploymentModel": "singleRuntime",
        "runtime": {
            "deploymentType": "mssql",
            "containerName": "ogg-sqlserver",
            "image": {"repository": "229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-sqlserver", "tag": "23.26.2.0.1"},
            "csi": {"enabled": True, "admin": {"enabled": True}, "certificate": {"enabled": True}},
        },
        "ingress": {"hostDomain": f"goldengate-{environment}.adcbmis.local"},
        "replication": {
            "enabled": True,
            "databaseCredentialSecret": f"{environment}/goldengate/databases/{pipeline}/target",
            "databaseCredential": {"domain": "OracleGoldenGate"},
            "supplementalLogging": {"enabled": False, "mode": "none", "objects": []},
            "extract": {"enabled": False},
            "distribution": {"enabled": False},
            "checkpoint": {"enabled": True, "table": "dbo.gg_checkpoint", "createIfMissing": True},
            "replicat": {
                "enabled": True, "name": "MSTGT01", "description": "target replicat",
                "sourceTrailName": "ma", "begin": "now",
                "mode": {"type": "nonintegrated", "parallel": False},
                "mappings": [{"source": "public.payments", "target": "dbo.payments"}],
                "startOnCreate": True,
            },
        },
    }


def _efs_test_doc(environment="dev", persistence=None):
    """Minimal valid descriptor with an explicit efs-capable u02 storage block, for persistence.efs.mode tests."""
    doc = {
        "deployment": {"enabled": True, "pipeline": "test-pipeline", "role": "source"},
        "global": {"environment": environment},
        "deploymentModel": "singleRuntime",
        "runtime": {
            "deploymentType": "oracle",
            "containerName": "ogg-oracle",
            "image": {"repository": "229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-oracle", "tag": "1.0.0"},
            "csi": {"enabled": True, "admin": {"enabled": True}, "certificate": {"enabled": True}},
            "storage": {"u02": {"type": "efs"}},
        },
        "ingress": {"hostDomain": f"goldengate-{environment}.adcbmis.local"},
    }
    if persistence is not None:
        doc["persistence"] = persistence
    return doc


def write_doc(root, environment, deployment_id, doc):
    folder = os.path.join(root, "envs", environment, deployment_id)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "values.yaml"), "w") as f:
        yaml.safe_dump(doc, f)


def write_default_pipeline(root, environment="dev", pipeline="payments-pg-to-mssql-001",
                           source_id="gg-pg-src-fixture-01", target_id="gg-mssql-tgt-fixture-01",
                           source_doc=None, target_doc=None, omit_source=False, omit_target=False):
    """Writes a complete valid PostgreSQL->MSSQL pipeline; callers mutate a deep copy for negative-path tests."""
    source_doc = source_doc if source_doc is not None else default_source_doc(environment, pipeline, target_id)
    target_doc = target_doc if target_doc is not None else default_target_doc(environment, pipeline)
    if not omit_source:
        write_doc(root, environment, source_id, source_doc)
    if not omit_target:
        write_doc(root, environment, target_id, target_doc)
    return source_id, target_id


class ScratchEnvironmentTestCase(unittest.TestCase):
    """Base class: points gdm.REPO_ROOT at an isolated temp directory for the duration of each test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._original_root = gdm.REPO_ROOT
        gdm.REPO_ROOT = self._tmp.name

    def tearDown(self):
        gdm.REPO_ROOT = self._original_root
        self._tmp.cleanup()


class RealRepositoryDescriptorTests(unittest.TestCase):
    """Exercised against the real, live envs/dev descriptors -- no scratch root."""

    def test_existing_oracle_descriptor_parses(self):
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        by_id = {d["deploymentId"]: d for d in active}
        self.assertIn("gg-oracle-payments-01", by_id)
        self.assertEqual(by_id["gg-oracle-payments-01"]["deploymentType"], "oracle")

    def test_existing_postgresql_descriptor_parses(self):
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        by_id = {d["deploymentId"]: d for d in active}
        self.assertIn("gg-postgresql-payments-01", by_id)
        self.assertEqual(by_id["gg-postgresql-payments-01"]["deploymentType"], "postgresql")

    def test_existing_oracle_renders_with_source_shared_secret(self):
        active, _inactive, _invalid = gdm.scan("dev")
        by_id = {d["deploymentId"]: d for d in active}
        self.assertEqual(by_id["gg-oracle-payments-01"]["adminSecretName"], "dev/goldengate/source/admin")

    def test_existing_postgresql_renders_with_target_shared_secret(self):
        active, _inactive, _invalid = gdm.scan("dev")
        by_id = {d["deploymentId"]: d for d in active}
        self.assertEqual(by_id["gg-postgresql-payments-01"]["adminSecretName"], "dev/goldengate/target/admin")

    def test_generated_registry_contains_all_three_live_deployments(self):
        # Updated for the first real managed-EFS runtime (gg-postgresql-repltest-01): the live dev registry now has 3 active deployments, not 2 -- this assertion is exact (assertEqual, not a subset check), so it still fails closed if a fourth descriptor is ever added without updating this test.
        registry = gdm.build_registry("dev")
        names = {d["name"] for d in registry["deployments"]}
        self.assertEqual(names, {"gg-oracle-payments-01", "gg-postgresql-payments-01", "gg-postgresql-repltest-01"})

    def test_existing_repltest_descriptor_parses(self):
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        by_id = {d["deploymentId"]: d for d in active}
        self.assertIn("gg-postgresql-repltest-01", by_id)
        self.assertEqual(by_id["gg-postgresql-repltest-01"]["deploymentType"], "postgresql")

    def test_repltest_descriptor_is_the_source_role_on_its_own_new_pipeline(self):
        active, _inactive, _invalid = gdm.scan("dev")
        by_id = {d["deploymentId"]: d for d in active}
        d = by_id["gg-postgresql-repltest-01"]
        self.assertEqual(d["role"], "source")
        self.assertEqual(d["pipeline"], "repltest-pg-to-mssql-001")
        self.assertNotEqual(d["pipeline"], "payments-ora-to-pg-001")

    def test_repltest_descriptor_lifecycle_is_active(self):
        active, _inactive, _invalid = gdm.scan("dev")
        by_id = {d["deploymentId"]: d for d in active}
        self.assertEqual(by_id["gg-postgresql-repltest-01"]["lifecycleState"], "active")

    def test_repltest_descriptor_renders_with_source_shared_secret(self):
        active, _inactive, _invalid = gdm.scan("dev")
        by_id = {d["deploymentId"]: d for d in active}
        self.assertEqual(by_id["gg-postgresql-repltest-01"]["adminSecretName"], "dev/goldengate/source/admin")

    def test_repltest_descriptor_uses_gg_postgresql_sa(self):
        active, _inactive, _invalid = gdm.scan("dev")
        by_id = {d["deploymentId"]: d for d in active}
        self.assertEqual(by_id["gg-postgresql-repltest-01"]["runtimeServiceAccountName"], "gg-postgresql-sa")

    def test_repltest_descriptor_is_the_first_managed_efs_deployment(self):
        active, _inactive, _invalid = gdm.scan("dev")
        by_id = {d["deploymentId"]: d for d in active}
        d = by_id["gg-postgresql-repltest-01"]
        self.assertEqual(d["efsMode"], "managed")
        self.assertIsNone(d["efsFileSystemId"])
        self.assertEqual(d["efsCreationToken"], "dev-gg-postgresql-repltest-01-efs")

    def test_repltest_descriptor_replication_remains_disabled(self):
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        by_id = {d["deploymentId"]: d for d in active}
        self.assertFalse(by_id["gg-postgresql-repltest-01"]["replicationEnabled"])

    def test_managed_efs_inventory_contains_exactly_the_repltest_deployment(self):
        # cmd_managed_efs_inventory prints JSON rather than returning it; re-derive the same expected-inventory shape (same filter: efsMode == "managed", same sort key) directly from the scan, to keep this test independent of stdout capture.
        active, inactive, _invalid = gdm.scan("dev")
        entries = sorted(
            (
                {"deploymentId": d["deploymentId"], "efsCreationToken": d["efsCreationToken"]}
                for d in active + inactive
                if d["efsMode"] == "managed"
            ),
            key=lambda x: x["deploymentId"],
        )
        self.assertEqual(entries, [{"deploymentId": "gg-postgresql-repltest-01", "efsCreationToken": "dev-gg-postgresql-repltest-01-efs"}])

    def test_existing_oracle_still_uses_gg_oracle_sa(self):
        active, _inactive, _invalid = gdm.scan("dev")
        by_id = {d["deploymentId"]: d for d in active}
        self.assertEqual(by_id["gg-oracle-payments-01"]["runtimeServiceAccountName"], "gg-oracle-sa")

    def test_existing_postgresql_still_uses_gg_postgresql_sa(self):
        active, _inactive, _invalid = gdm.scan("dev")
        by_id = {d["deploymentId"]: d for d in active}
        self.assertEqual(by_id["gg-postgresql-payments-01"]["runtimeServiceAccountName"], "gg-postgresql-sa")

    def test_no_existing_deployment_resolves_to_gg_runtime_sa(self):
        active, _inactive, _invalid = gdm.scan("dev")
        for d in active:
            self.assertNotEqual(d["runtimeServiceAccountName"], "gg-runtime-sa")

    def test_replication_1_existing_oracle_disabled_replication_remains_valid(self):
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        by_id = {d["deploymentId"]: d for d in active}
        self.assertIn("gg-oracle-payments-01", by_id)
        self.assertFalse(by_id["gg-oracle-payments-01"]["replicationEnabled"])

    def test_replication_2_existing_postgresql_disabled_replication_remains_valid(self):
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        by_id = {d["deploymentId"]: d for d in active}
        self.assertIn("gg-postgresql-payments-01", by_id)
        self.assertFalse(by_id["gg-postgresql-payments-01"]["replicationEnabled"])


class GenericDeploymentTypeTests(ScratchEnvironmentTestCase):
    """runtime.deploymentType is a safe canonical lowercase token; the derived ServiceAccount is deterministic naming, never a fixed allowlist."""

    def test_synthetic_postgresql_source_descriptor_parses(self):
        write_descriptor(self._tmp.name, "dev", "gg-postgresql-payments-mssql-01",
                         pipeline="payments-pg-to-mssql-001", role="source", deployment_type="postgresql")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["adminSecretName"], "dev/goldengate/source/admin")

    def test_synthetic_mssql_target_descriptor_parses(self):
        write_descriptor(self._tmp.name, "dev", "gg-mssql-payments-01",
                         pipeline="payments-pg-to-mssql-001", role="target", deployment_type="mssql")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(active[0]["deploymentType"], "mssql")
        self.assertEqual(active[0]["adminSecretName"], "dev/goldengate/target/admin")

    def test_any_safe_type_derives_its_service_account_without_a_fixed_allowlist(self):
        write_descriptor(self._tmp.name, "dev", "gg-mysql-fixture-01", deployment_type="mysql")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(active[0]["runtimeServiceAccountName"], "gg-mysql-sa")

    def test_safe_daa_type_parses(self):
        write_descriptor(self._tmp.name, "dev", "gg-daa-fixture-01", deployment_type="daa")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(active[0]["deploymentType"], "daa")

    def test_unsafe_deployment_type_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-unsafe-fixture-01", deployment_type="oracle/../etc")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_uppercase_deployment_type_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-uppercase-fixture-01", deployment_type="Oracle")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_leading_hyphen_deployment_type_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-leading-hyphen-fixture-01", deployment_type="-oracle")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_trailing_hyphen_deployment_type_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-trailing-hyphen-fixture-01", deployment_type="oracle-")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_overlength_deployment_type_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-overlength-fixture-01", deployment_type="a" * 33)
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_trailing_newline_rejected_not_matched_before_dollar_anchor(self):
        # Regression: Python's $ matches just before a trailing \n even without re.MULTILINE; \Z does not.
        self.assertFalse(gdm._safe_token("gg-oracle-payments-01\n", 63))


class RuntimeIdentityNamingTests(unittest.TestCase):
    """Tests 1-7: deterministic gg-<type>-sa naming, never a hardcoded deploymentType-to-ServiceAccount map."""

    def test_oracle_resolves_to_gg_oracle_sa(self):
        self.assertEqual(gdm.resolve_runtime_service_account("oracle"), "gg-oracle-sa")

    def test_postgresql_resolves_to_gg_postgresql_sa(self):
        self.assertEqual(gdm.resolve_runtime_service_account("postgresql"), "gg-postgresql-sa")

    def test_mssql_resolves_to_gg_mssql_sa(self):
        self.assertEqual(gdm.resolve_runtime_service_account("mssql"), "gg-mssql-sa")

    def test_daa_resolves_to_gg_daa_sa(self):
        self.assertEqual(gdm.resolve_runtime_service_account("daa"), "gg-daa-sa")

    def test_mysql_resolves_to_gg_mysql_sa_without_source_changes(self):
        self.assertEqual(gdm.resolve_runtime_service_account("mysql"), "gg-mysql-sa")

    def test_another_synthetic_safe_type_derives_its_matching_service_account(self):
        self.assertEqual(gdm.resolve_runtime_service_account("cassandra"), "gg-cassandra-sa")

    def test_no_hardcoded_deployment_type_to_service_account_map_exists_in_source(self):
        with open(TOOL_PATH) as f:
            source = f.read()
        self.assertNotIn("RUNTIME_IDENTITY_MAP", source)


class SyntheticFlavourRenderTests(ScratchEnvironmentTestCase):
    """Tests 8, 9, 15, 16: shared/distinct ServiceAccounts by type, image stays values-file-derived, existing Oracle/PostgreSQL unaffected."""

    def test_synthetic_mssql_runtime_resolves_gg_mssql_sa(self):
        write_descriptor(self._tmp.name, "dev", "gg-mssql-fixture-01",
                         pipeline="p1", role="target", deployment_type="mssql")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(active[0]["runtimeServiceAccountName"], "gg-mssql-sa")

    def test_synthetic_daa_runtime_resolves_gg_daa_sa(self):
        write_descriptor(self._tmp.name, "dev", "gg-daa-fixture-01",
                         pipeline="p1", role="source", deployment_type="daa")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(active[0]["runtimeServiceAccountName"], "gg-daa-sa")

    def test_two_deployments_of_the_same_type_share_one_service_account(self):
        write_descriptor(self._tmp.name, "dev", "gg-postgresql-fixture-a", pipeline="pa", role="source", deployment_type="postgresql")
        write_descriptor(self._tmp.name, "dev", "gg-postgresql-fixture-b", pipeline="pb", role="source", deployment_type="postgresql")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        sa_names = {d["runtimeServiceAccountName"] for d in active}
        self.assertEqual(sa_names, {"gg-postgresql-sa"})

    def test_different_types_produce_distinct_service_accounts(self):
        write_descriptor(self._tmp.name, "dev", "gg-oracle-fixture-a", pipeline="pa", role="source", deployment_type="oracle")
        write_descriptor(self._tmp.name, "dev", "gg-mssql-fixture-b", pipeline="pb", role="target", deployment_type="mssql")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        sa_names = {d["deploymentId"]: d["runtimeServiceAccountName"] for d in active}
        self.assertEqual(sa_names["gg-oracle-fixture-a"], "gg-oracle-sa")
        self.assertEqual(sa_names["gg-mssql-fixture-b"], "gg-mssql-sa")
        self.assertNotEqual(sa_names["gg-oracle-fixture-a"], sa_names["gg-mssql-fixture-b"])

    def test_mssql_image_comes_from_the_values_file_not_a_mapping(self):
        write_descriptor(self._tmp.name, "dev", "gg-mssql-fixture-01",
                         pipeline="p1", role="target", deployment_type="mssql",
                         repository="229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-sqlserver", tag="9.9.9")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(active[0]["imageRepository"], "229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-sqlserver")
        self.assertEqual(active[0]["imageTag"], "9.9.9")

    def test_no_deployment_type_to_image_mapping_exists_in_source(self):
        with open(TOOL_PATH) as f:
            source = f.read()
        self.assertNotIn("ogg-oracle", source)
        self.assertNotIn("ogg-postgresql", source)
        self.assertNotIn("ogg-mssql", source)
        self.assertNotIn("ogg-daa", source)

    def test_no_engine_specific_branches_in_resolve_function_source(self):
        import inspect
        source = inspect.getsource(gdm.resolve_runtime_service_account)
        for token in ("oracle", "postgresql", "mssql", "daa", "sqlserver", "distributed"):
            self.assertNotIn(token, source)

    def test_no_deployment_name_derived_service_account(self):
        write_descriptor(self._tmp.name, "dev", "gg-oracle-payments-99", pipeline="p1", role="source", deployment_type="oracle")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(active[0]["runtimeServiceAccountName"], "gg-oracle-sa")
        self.assertNotIn("payments-99", active[0]["runtimeServiceAccountName"])


class RuntimeIdentitiesCommandTests(ScratchEnvironmentTestCase):
    """Tests 10, 11: the folder-driven identity inventory command is deterministic and matches the CLI contract."""

    def test_runtime_identity_inventory_sorted_unique(self):
        write_descriptor(self._tmp.name, "dev", "gg-postgresql-fixture-a", pipeline="pa", role="source", deployment_type="postgresql")
        write_descriptor(self._tmp.name, "dev", "gg-postgresql-fixture-b", pipeline="pb", role="target", deployment_type="postgresql")
        write_descriptor(self._tmp.name, "dev", "gg-oracle-fixture-a", pipeline="pc", role="source", deployment_type="oracle")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        inventory = gdm.runtime_identity_inventory(active)
        self.assertEqual(inventory, [("oracle", "gg-oracle-sa"), ("postgresql", "gg-postgresql-sa")])

    def test_disabled_deployment_excluded_from_identity_inventory(self):
        write_descriptor(self._tmp.name, "dev", "gg-oracle-fixture-a", pipeline="pa", role="source", deployment_type="oracle")
        write_descriptor(self._tmp.name, "dev", "gg-mssql-fixture-b", pipeline="pb", role="target", deployment_type="mssql", enabled=False)
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        inventory = gdm.runtime_identity_inventory(active)
        self.assertEqual(inventory, [("oracle", "gg-oracle-sa")])


class SharedSecretDerivationTests(unittest.TestCase):
    """Tests 1-6: role/environment alone select the shared environment-level admin/TLS secrets."""

    def test_source_role_resolves_to_source_admin_secret(self):
        self.assertEqual(gdm.resolve_admin_secret("dev", "source"), "dev/goldengate/source/admin")

    def test_target_role_resolves_to_target_admin_secret(self):
        self.assertEqual(gdm.resolve_admin_secret("dev", "target"), "dev/goldengate/target/admin")

    def test_dev_source_resolves_exactly(self):
        self.assertEqual(gdm.resolve_admin_secret("dev", "source"), "dev/goldengate/source/admin")

    def test_dev_target_resolves_exactly(self):
        self.assertEqual(gdm.resolve_admin_secret("dev", "target"), "dev/goldengate/target/admin")

    def test_sit_source_resolves_exactly(self):
        self.assertEqual(gdm.resolve_admin_secret("sit", "source"), "sit/goldengate/source/admin")

    def test_sit_target_resolves_exactly(self):
        self.assertEqual(gdm.resolve_admin_secret("sit", "target"), "sit/goldengate/target/admin")

    def test_tls_resolves_from_environment(self):
        self.assertEqual(gdm.resolve_tls_secret("dev"), "dev/goldengate/tls-certificate")
        self.assertEqual(gdm.resolve_tls_secret("sit"), "sit/goldengate/tls-certificate")

    def test_invalid_role_raises(self):
        with self.assertRaises(ValueError):
            gdm.resolve_admin_secret("dev", "replica")


class NoPerDeploymentSecretTests(ScratchEnvironmentTestCase):
    """Test 7: no per-deployment runtime admin secret is ever derived; two deployments of the same role share one secret."""

    def test_two_sources_in_different_pipelines_share_the_same_admin_secret(self):
        write_descriptor(self._tmp.name, "dev", "gg-source-a", pipeline="p1", role="source")
        write_descriptor(self._tmp.name, "dev", "gg-source-b", pipeline="p2", role="source")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        names = {d["adminSecretName"] for d in active}
        self.assertEqual(names, {"dev/goldengate/source/admin"})

    def test_no_deployment_specific_runtime_secret_path_exists_in_source(self):
        with open(TOOL_PATH) as f:
            source = f.read()
        self.assertNotIn("goldengate/runtime/", source)
        self.assertNotIn("managed_secrets", source)
        self.assertNotIn("adminSecretManaged", source)


class ForbiddenOverrideTests(ScratchEnvironmentTestCase):
    """Tests 8-12: operator descriptors must not override any shared platform invariant."""

    def test_deployment_admin_secret_override_is_rejected(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         deployment_admin_secret="dev/goldengate/source/admin")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)
        self.assertIn("adminSecret", invalid[0][1])

    def test_csi_admin_object_name_override_is_rejected(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         csi_admin_object_name="dev/goldengate/source/admin")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)
        self.assertIn("csi.admin.objectName", invalid[0][1])

    def test_csi_certificate_object_name_override_is_rejected(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         csi_certificate_object_name="dev/goldengate/tls-certificate")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)
        self.assertIn("csi.certificate.objectName", invalid[0][1])

    def test_csi_service_account_role_arn_override_is_rejected(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         csi_service_account_role_arn="arn:aws:iam::668311715351:role/GoldenGateSecretsReadRole-dev")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)
        self.assertIn("serviceAccountRoleArn", invalid[0][1])

    def test_wrong_service_account_override_is_rejected(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01", service_account_name="gg-something-else")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)
        self.assertIn("runtime.serviceAccount", invalid[0][1])

    def test_service_account_create_override_is_rejected_even_when_literal_false(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         service_account_name="gg-oracle-sa", service_account_create=False)
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)
        self.assertIn("runtime.serviceAccount", invalid[0][1])

    def test_a_correctly_named_service_account_override_is_still_rejected(self):
        # No operator override is ever tolerated, even one that happens to match the derived identity exactly.
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         service_account_name="gg-oracle-sa", service_account_create=False)
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_service_account_omitted_entirely_is_valid(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(active[0]["runtimeServiceAccountName"], "gg-oracle-sa")


class EnvironmentScopedContractTests(ScratchEnvironmentTestCase):
    """Environment-scoped derivation, ECR/name grammar, and EFS identity."""

    def test_admin_secret_is_scoped_to_the_selected_environment_not_hardcoded_dev(self):
        write_descriptor(self._tmp.name, "sit", "gg-fixture-01")
        active, _inactive, invalid = gdm.scan("sit")
        self.assertEqual(invalid, [])
        self.assertEqual(active[0]["adminSecretName"], "sit/goldengate/source/admin")

    def test_ingress_host_domain_inconsistent_with_shared_domain_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         ingress_host_domain="totally-different-domain.example.com")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_ecr_repository_with_digest_syntax_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         repository="229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-oracle@sha256:" + "a" * 64)
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_ecr_repository_with_whitespace_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         repository="229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg oracle")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_ecr_repository_with_empty_suffix_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         repository="229410149234.dkr.ecr.eu-west-1.amazonaws.com/")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_ecr_repository_with_double_slash_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         repository="229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg//oracle")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_multi_segment_ecr_repository_passes(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         repository="229410149234.dkr.ecr.eu-west-1.amazonaws.com/goldengate/ogg-oracle")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])

    def test_username_key_rejected(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01", extra="dbUsername: admin\n")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_token_key_rejected(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01", extra="apiToken: xyz\n")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_database_url_key_rejected(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01", extra="databaseUrl: postgres://x\n")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_jdbc_url_key_rejected(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01", extra="jdbcUrl: jdbc:postgresql://x\n")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_efs_existing_mode_requires_safe_filesystem_id(self):
        write_doc(self._tmp.name, "dev", "gg-fixture-01",
                 _efs_test_doc(persistence={"enabled": True, "provider": "efs",
                                             "efs": {"mode": "existing", "fileSystemId": "not-an-fs-id"}}))
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_efs_existing_mode_missing_filesystem_id_fails(self):
        write_doc(self._tmp.name, "dev", "gg-fixture-01",
                 _efs_test_doc(persistence={"enabled": True, "provider": "efs",
                                             "efs": {"mode": "existing"}}))
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_efs_existing_mode_with_safe_filesystem_id_passes(self):
        write_doc(self._tmp.name, "dev", "gg-fixture-01",
                 _efs_test_doc(persistence={"enabled": True, "provider": "efs",
                                             "efs": {"mode": "existing", "fileSystemId": "fs-0123456789abcdef0"}}))
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(active[0]["efsMode"], "existing")
        self.assertEqual(active[0]["efsFileSystemId"], "fs-0123456789abcdef0")
        self.assertIsNone(active[0]["efsCreationToken"])

    def test_efs_managed_mode_forbids_committed_filesystem_id(self):
        write_doc(self._tmp.name, "dev", "gg-fixture-01",
                 _efs_test_doc(persistence={"enabled": True, "provider": "efs",
                                             "efs": {"mode": "managed", "fileSystemId": "fs-0123456789abcdef0"}}))
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_efs_managed_mode_without_filesystem_id_passes_and_derives_token(self):
        write_doc(self._tmp.name, "dev", "gg-fixture-01",
                 _efs_test_doc(persistence={"enabled": True, "provider": "efs", "efs": {"mode": "managed"}}))
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(active[0]["efsMode"], "managed")
        self.assertIsNone(active[0]["efsFileSystemId"])
        self.assertEqual(active[0]["efsCreationToken"], "dev-gg-fixture-01-efs")

    def test_efs_missing_mode_fails(self):
        write_doc(self._tmp.name, "dev", "gg-fixture-01",
                 _efs_test_doc(persistence={"enabled": True, "provider": "efs",
                                             "efs": {"fileSystemId": "fs-0123456789abcdef0"}}))
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_efs_invalid_mode_value_fails(self):
        write_doc(self._tmp.name, "dev", "gg-fixture-01",
                 _efs_test_doc(persistence={"enabled": True, "provider": "efs", "efs": {"mode": "auto"}}))
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_efs_enabled_requires_u02_storage_type_efs(self):
        doc = _efs_test_doc(persistence={"enabled": True, "provider": "efs", "efs": {"mode": "managed"}})
        doc["runtime"]["storage"]["u02"]["type"] = "emptyDir"
        write_doc(self._tmp.name, "dev", "gg-fixture-01", doc)
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_efs_creation_token_derivation_is_deterministic(self):
        self.assertEqual(gdm.derive_efs_creation_token("dev", "gg-postgresql-repltest-01"),
                         "dev-gg-postgresql-repltest-01-efs")
        self.assertEqual(gdm.derive_efs_creation_token("dev", "gg-postgresql-repltest-01"),
                         gdm.derive_efs_creation_token("dev", "gg-postgresql-repltest-01"))

    def test_efs_creation_token_exceeding_limit_fails_closed(self):
        long_id = "gg-" + ("x" * 60) + "-fixture"
        with self.assertRaises(gdm.DescriptorError):
            gdm.derive_efs_creation_token("dev", long_id)

    def test_efs_creation_token_never_truncated_or_hashed(self):
        deployment_id = "gg-postgresql-repltest-01"
        token = gdm.derive_efs_creation_token("dev", deployment_id)
        self.assertIn(deployment_id, token)

    def test_efs_two_different_deployment_ids_derive_distinct_tokens(self):
        token_a = gdm.derive_efs_creation_token("dev", "gg-postgresql-repltest-01")
        token_b = gdm.derive_efs_creation_token("dev", "gg-mssql-repltest-01")
        self.assertNotEqual(token_a, token_b)

    def test_efs_two_managed_runtimes_validate_together_with_distinct_tokens(self):
        write_doc(self._tmp.name, "dev", "gg-postgresql-repltest-01",
                 _efs_test_doc(persistence={"enabled": True, "provider": "efs", "efs": {"mode": "managed"}}))
        write_doc(self._tmp.name, "dev", "gg-mssql-repltest-01",
                 _efs_test_doc(persistence={"enabled": True, "provider": "efs", "efs": {"mode": "managed"}}))
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        problems = gdm.validate("dev")
        self.assertEqual([p for p in problems if "creation token collision" in p], [])
        tokens = {d["deploymentId"]: d["efsCreationToken"] for d in active}
        self.assertEqual(len(set(tokens.values())), 2)

    def test_efs_disabled_persistence_skips_efs_validation(self):
        write_doc(self._tmp.name, "dev", "gg-fixture-01",
                 _efs_test_doc(persistence={"enabled": False}))
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertIsNone(active[0]["efsMode"])

    def test_persistence_enabled_string_true_fails_closed_not_silently_skipped(self):
        write_doc(self._tmp.name, "dev", "gg-fixture-01",
                 _efs_test_doc(persistence={"enabled": "true", "provider": "efs", "efs": {"mode": "managed"}}))
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)
        self.assertIn("persistence.enabled must be a literal Boolean", invalid[0][1])

    def test_persistence_enabled_string_false_fails_closed(self):
        write_doc(self._tmp.name, "dev", "gg-fixture-01",
                 _efs_test_doc(persistence={"enabled": "false", "provider": "efs"}))
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)
        self.assertIn("persistence.enabled must be a literal Boolean", invalid[0][1])

    def test_persistence_enabled_integer_one_fails_closed(self):
        write_doc(self._tmp.name, "dev", "gg-fixture-01",
                 _efs_test_doc(persistence={"enabled": 1, "provider": "efs"}))
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)
        self.assertIn("persistence.enabled must be a literal Boolean", invalid[0][1])

    def test_persistence_enabled_literal_true_still_supported(self):
        write_doc(self._tmp.name, "dev", "gg-fixture-01",
                 _efs_test_doc(persistence={"enabled": True, "provider": "efs", "efs": {"mode": "managed"}}))
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(active[0]["efsMode"], "managed")

    def test_persistence_enabled_literal_false_still_supported(self):
        write_doc(self._tmp.name, "dev", "gg-fixture-01",
                 _efs_test_doc(persistence={"enabled": False}))
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertIsNone(active[0]["efsMode"])

    def test_derived_namespace_fields_present(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01")
        active, _inactive, _invalid = gdm.scan("dev")
        self.assertEqual(active[0]["runtimeNamespace"], "goldengate-dev")
        self.assertEqual(active[0]["monitoringNamespace"], "goldengate-monitoring")
        self.assertEqual(active[0]["ingressHost"], "goldengate-dev.adcbmis.local")
        self.assertEqual(active[0]["tlsSecretName"], "dev/goldengate/tls-certificate")


class FullValidationGatingTests(unittest.TestCase):
    """No command may emit partial inventory while another runtime folder is invalid."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._original_root = gdm.REPO_ROOT
        gdm.REPO_ROOT = self._tmp.name

    def tearDown(self):
        gdm.REPO_ROOT = self._original_root
        self._tmp.cleanup()

    def test_full_validation_gate_trips_when_an_unrelated_folder_is_invalid(self):
        write_descriptor(self._tmp.name, "dev", "gg-good-fixture-01")
        write_descriptor(self._tmp.name, "dev", "gg-bad-fixture-01", tag="latest")
        _active, _inactive, invalid, problems = gdm._run_full_validation("dev")
        self.assertTrue(invalid or problems)

    def test_shared_secrets_command_returns_nonzero_when_a_folder_is_invalid(self):
        write_descriptor(self._tmp.name, "dev", "gg-good-fixture-01")
        write_descriptor(self._tmp.name, "dev", "gg-bad-fixture-01", tag="latest")

        class Args:
            environment = "dev"

        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = gdm.cmd_shared_secrets(Args())
        self.assertEqual(exit_code, 1)

    def test_describe_command_returns_nonzero_when_an_unrelated_folder_is_invalid(self):
        write_descriptor(self._tmp.name, "dev", "gg-good-fixture-01")
        write_descriptor(self._tmp.name, "dev", "gg-bad-fixture-01", tag="latest")

        class Args:
            environment = "dev"
            deployment_id = "gg-good-fixture-01"

        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = gdm.cmd_describe(Args())
        self.assertEqual(exit_code, 1)

    def test_list_command_returns_nonzero_when_an_unrelated_folder_is_invalid(self):
        write_descriptor(self._tmp.name, "dev", "gg-good-fixture-01")
        write_descriptor(self._tmp.name, "dev", "gg-bad-fixture-01", tag="latest")

        class Args:
            environment = "dev"

        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = gdm.cmd_list(Args())
        self.assertEqual(exit_code, 1)
        self.assertNotIn("ACTIVE  gg-good-fixture-01", buf.getvalue())


class ManagedEfsInventoryCommandTests(unittest.TestCase):
    """The managed-efs-inventory command feeds the workflow's managed_efs_inventory_guard; it must include lifecycle.state=absent managed descriptors (their EFS is retained, not decommissioned) and exclude existing-mode descriptors entirely."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._original_root = gdm.REPO_ROOT
        gdm.REPO_ROOT = self._tmp.name

    def tearDown(self):
        gdm.REPO_ROOT = self._original_root
        self._tmp.cleanup()

    def _run(self):
        class Args:
            environment = "dev"

        import io
        import json
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = gdm.cmd_managed_efs_inventory(Args())
        return exit_code, json.loads(buf.getvalue())

    def test_no_managed_descriptors_yields_empty_inventory(self):
        write_doc(self._tmp.name, "dev", "gg-fixture-01",
                 _efs_test_doc(persistence={"enabled": True, "provider": "efs", "efs": {"mode": "existing", "fileSystemId": "fs-0123456789abcdef0"}}))
        exit_code, inventory = self._run()
        self.assertEqual(exit_code, 0)
        self.assertEqual(inventory, [])

    def test_managed_descriptor_is_included_with_its_creation_token(self):
        write_doc(self._tmp.name, "dev", "gg-fixture-01",
                 _efs_test_doc(persistence={"enabled": True, "provider": "efs", "efs": {"mode": "managed"}}))
        exit_code, inventory = self._run()
        self.assertEqual(exit_code, 0)
        self.assertEqual(inventory, [{"deploymentId": "gg-fixture-01", "efsCreationToken": "dev-gg-fixture-01-efs"}])

    def test_lifecycle_absent_managed_descriptor_is_still_included(self):
        doc = _efs_test_doc(persistence={"enabled": True, "provider": "efs", "efs": {"mode": "managed"}})
        doc["lifecycle"] = {"state": "absent"}
        write_doc(self._tmp.name, "dev", "gg-fixture-01", doc)
        exit_code, inventory = self._run()
        self.assertEqual(exit_code, 0)
        self.assertEqual(inventory, [{"deploymentId": "gg-fixture-01", "efsCreationToken": "dev-gg-fixture-01-efs"}])

    def test_two_managed_descriptors_produce_two_distinct_entries(self):
        source_doc = _efs_test_doc(persistence={"enabled": True, "provider": "efs", "efs": {"mode": "managed"}})
        target_doc = copy.deepcopy(source_doc)
        target_doc["deployment"]["role"] = "target"
        write_doc(self._tmp.name, "dev", "gg-postgresql-repltest-01", source_doc)
        write_doc(self._tmp.name, "dev", "gg-mssql-repltest-01", target_doc)
        exit_code, inventory = self._run()
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(inventory), 2)
        self.assertEqual({i["deploymentId"] for i in inventory}, {"gg-postgresql-repltest-01", "gg-mssql-repltest-01"})
        self.assertEqual(len({i["efsCreationToken"] for i in inventory}), 2)


class SharedSecretsCommandTests(ScratchEnvironmentTestCase):
    """Test 29 support: the shared-secrets command output shape."""

    def test_shared_secrets_command_prints_exactly_three_identifiers(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01")

        class Args:
            environment = "dev"

        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = gdm.cmd_shared_secrets(Args())
        self.assertEqual(exit_code, 0)
        lines = buf.getvalue().splitlines()
        self.assertEqual(lines, [
            "dev/goldengate/source/admin",
            "dev/goldengate/target/admin",
            "dev/goldengate/tls-certificate",
        ])


class LifecycleClassificationTests(ScratchEnvironmentTestCase):
    """Disabled/absent runtimes validate but are excluded from active inventory."""

    def test_disabled_runtime_validates_but_excluded(self):
        write_descriptor(self._tmp.name, "dev", "gg-disabled-fixture-01", enabled=False)
        active, inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(active, [])
        self.assertEqual(len(inactive), 1)

    def test_lifecycle_state_absent_validates_but_excluded(self):
        write_descriptor(self._tmp.name, "dev", "gg-absent-fixture-01",
                         extra="\nlifecycle:\n  state: absent\n")
        active, inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(active, [])
        self.assertEqual(len(inactive), 1)


class FailClosedTests(ScratchEnvironmentTestCase):
    """Malformed/invalid candidates never silently disappear."""

    def test_malformed_yaml_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-malformed-fixture-01", raw_override="deployment: [unterminated")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_duplicate_yaml_keys_fail(self):
        raw = "deployment:\n  enabled: true\n  enabled: false\n"
        write_descriptor(self._tmp.name, "dev", "gg-dup-key-fixture-01", raw_override=raw)
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_invalid_runtime_candidate_is_not_silently_ignored(self):
        write_descriptor(self._tmp.name, "dev", "gg-bad-fixture-01", tag="latest")
        _active, inactive, invalid = gdm.scan("dev")
        self.assertEqual(inactive, [])
        self.assertEqual(len(invalid), 1)
        path, reason = invalid[0]
        self.assertIn("gg-bad-fixture-01", path)
        self.assertTrue(reason)

    def test_non_runtime_folder_ignored_only_when_explicitly_recognized(self):
        os.makedirs(os.path.join(self._tmp.name, "envs", "dev", "argocd"), exist_ok=True)
        with open(os.path.join(self._tmp.name, "envs", "dev", "argocd", "values.yaml"), "w") as f:
            f.write("unrelated: true\n")
        os.makedirs(os.path.join(self._tmp.name, "envs", "dev", "some-other-config"), exist_ok=True)
        with open(os.path.join(self._tmp.name, "envs", "dev", "some-other-config", "values.yaml"), "w") as f:
            f.write("unrelated: true\n")
        _active, _inactive, invalid = gdm.scan("dev")
        invalid_paths = [p for p, _r in invalid]
        self.assertFalse(any("argocd" in p for p in invalid_paths))
        self.assertTrue(any("some-other-config" in p for p in invalid_paths))

    def test_duplicate_source_role_in_one_pipeline_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-source-a", pipeline="p1", role="source")
        write_descriptor(self._tmp.name, "dev", "gg-source-b", pipeline="p1", role="source")
        problems = gdm.validate("dev")
        self.assertTrue(any("more than one source" in p for p in problems))

    def test_duplicate_target_role_in_one_pipeline_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-target-a", pipeline="p1", role="target")
        write_descriptor(self._tmp.name, "dev", "gg-target-b", pipeline="p1", role="target")
        problems = gdm.validate("dev")
        self.assertTrue(any("more than one target" in p for p in problems))

    def test_duplicate_alb_group_order_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-alb-a", pipeline="p1", role="source", alb_group_order="110")
        write_descriptor(self._tmp.name, "dev", "gg-alb-b", pipeline="p2", role="source", alb_group_order="110")
        problems = gdm.validate("dev")
        self.assertTrue(any("duplicate ALB group order" in p for p in problems))

    def test_latest_image_tag_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-latest-fixture-01", tag="latest")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)
        self.assertIn("latest", invalid[0][1])

    def test_public_image_repository_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-public-fixture-01", repository="docker.io/library/postgres")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_wrong_ecr_account_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-wrong-account-fixture-01",
                         repository="123456789012.dkr.ecr.eu-west-1.amazonaws.com/ogg-oracle")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_embedded_credentials_fail(self):
        write_descriptor(self._tmp.name, "dev", "gg-cred-fixture-01",
                         extra="\nadminPassword: hunter2\n")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)
        self.assertNotIn("hunter2", invalid[0][1])

    def test_boolean_like_string_enabled_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-boolstr-fixture-01",
                         raw_override=BASE_DESCRIPTOR.format(
                             enabled='"true"', pipeline="p1", role="source", environment="dev",
                             deployment_admin_secret_block="", deployment_type="oracle",
                             repository="229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-oracle", tag="1.0.0",
                             service_account_block="", csi_role_arn_block="", csi_admin_object_name_block="",
                             csi_certificate_object_name_block="", ingress_host_domain="goldengate-dev.adcbmis.local",
                             alb_block="", extra=""))
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)


class ReplicationSchemaTests(ScratchEnvironmentTestCase):
    """Task 23 items 1-28: the Phase 6D1 replication contract."""

    def test_replication_enabled_false_passes(self):
        write_descriptor(self._tmp.name, "dev", "gg-repl-off-fixture-01",
                         extra="\nreplication:\n  enabled: false\n")
        problems = gdm.validate("dev")
        self.assertEqual(problems, [])

    def test_3_synthetic_postgresql_source_descriptor_is_valid(self):
        write_default_pipeline(self._tmp.name)
        problems = gdm.validate("dev")
        self.assertEqual(problems, [])

    def test_4_synthetic_mssql_target_descriptor_is_valid(self):
        source_id, target_id = write_default_pipeline(self._tmp.name)
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        by_id = {d["deploymentId"]: d for d in active}
        self.assertEqual(by_id[target_id]["deploymentType"], "mssql")

    def test_5_complete_pipeline_is_valid(self):
        write_default_pipeline(self._tmp.name)
        problems = gdm.validate("dev")
        self.assertEqual(problems, [])
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(len(active), 2)

    def test_6_missing_source_fails(self):
        write_default_pipeline(self._tmp.name, omit_source=True)
        problems = gdm.validate("dev")
        self.assertTrue(any("exactly one active source" in p for p in problems))

    def test_7_missing_target_fails(self):
        write_default_pipeline(self._tmp.name, omit_target=True)
        problems = gdm.validate("dev")
        self.assertTrue(any("exactly one active target" in p for p in problems))

    def test_8_both_source_and_target_roles_required(self):
        write_default_pipeline(self._tmp.name, omit_source=True)
        problems = gdm.validate("dev")
        self.assertTrue(problems)

    def test_9_unsupported_source_type_fails(self):
        doc = default_source_doc("dev", "payments-pg-to-mssql-001", "gg-mssql-tgt-fixture-01")
        doc["runtime"]["deploymentType"] = "oracle"
        write_default_pipeline(self._tmp.name, source_doc=doc)
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertTrue(any("unsupported replication scope" in reason for _path, reason in invalid))

    def test_10_unsupported_target_type_fails(self):
        doc = default_target_doc("dev", "payments-pg-to-mssql-001")
        doc["runtime"]["deploymentType"] = "postgresql"
        write_default_pipeline(self._tmp.name, target_doc=doc)
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertTrue(any("unsupported replication scope" in reason for _path, reason in invalid))

    def test_11_source_target_deployment_mismatch_fails(self):
        doc = default_source_doc("dev", "payments-pg-to-mssql-001", "gg-mssql-tgt-fixture-01")
        doc["replication"]["distribution"]["targetDeployment"] = "gg-wrong-target-01"
        write_default_pipeline(self._tmp.name, source_doc=doc)
        problems = gdm.validate("dev")
        self.assertTrue(any("targetDeployment must equal" in p for p in problems))

    def test_12_source_target_trail_mismatch_fails(self):
        doc = default_target_doc("dev", "payments-pg-to-mssql-001")
        doc["replication"]["replicat"]["sourceTrailName"] = "mx"
        write_default_pipeline(self._tmp.name, target_doc=doc)
        problems = gdm.validate("dev")
        self.assertTrue(any("targetTrailName must equal" in p for p in problems))

    def test_13_duplicate_source_fails(self):
        write_default_pipeline(self._tmp.name)
        extra_source = default_source_doc("dev", "payments-pg-to-mssql-001", "gg-mssql-tgt-fixture-01")
        write_doc(self._tmp.name, "dev", "gg-pg-src-fixture-02", extra_source)
        problems = gdm.validate("dev")
        self.assertTrue(any("more than one source" in p or "exactly one active source" in p for p in problems))

    def test_14_duplicate_target_fails(self):
        write_default_pipeline(self._tmp.name)
        extra_target = default_target_doc("dev", "payments-pg-to-mssql-001")
        write_doc(self._tmp.name, "dev", "gg-mssql-tgt-fixture-02", extra_target)
        problems = gdm.validate("dev")
        self.assertTrue(any("more than one target" in p or "exactly one active target" in p for p in problems))

    def test_15_replication_enabled_string_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-repl-bad-bool-01",
                         extra='\nreplication:\n  enabled: "true"\n')
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertTrue(any("literal Boolean" in reason for _path, reason in invalid))

    def test_16_start_on_create_string_fails(self):
        doc = default_source_doc("dev", "payments-pg-to-mssql-001", "gg-mssql-tgt-fixture-01")
        doc["replication"]["extract"]["startOnCreate"] = "true"
        write_default_pipeline(self._tmp.name, source_doc=doc)
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertTrue(any("startOnCreate must be a literal Boolean" in reason for _path, reason in invalid))

    def test_17_invalid_extract_name_fails(self):
        doc = default_source_doc("dev", "payments-pg-to-mssql-001", "gg-mssql-tgt-fixture-01")
        doc["replication"]["extract"]["name"] = "toolongname"
        write_default_pipeline(self._tmp.name, source_doc=doc)
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertTrue(any("Extract name" in reason for _path, reason in invalid))

    def test_18_invalid_replicat_name_fails(self):
        doc = default_target_doc("dev", "payments-pg-to-mssql-001")
        doc["replication"]["replicat"]["name"] = "lowercase"
        write_default_pipeline(self._tmp.name, target_doc=doc)
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertTrue(any("Replicat name" in reason for _path, reason in invalid))

    def test_19_invalid_trail_name_fails(self):
        doc = default_source_doc("dev", "payments-pg-to-mssql-001", "gg-mssql-tgt-fixture-01")
        doc["replication"]["extract"]["trail"]["name"] = "abc"
        write_default_pipeline(self._tmp.name, source_doc=doc)
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertTrue(any("trail name" in reason for _path, reason in invalid))

    def test_20_invalid_path_name_fails(self):
        doc = default_source_doc("dev", "payments-pg-to-mssql-001", "gg-mssql-tgt-fixture-01")
        doc["replication"]["distribution"]["pathName"] = "1BADSTART"
        write_default_pipeline(self._tmp.name, source_doc=doc)
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertTrue(any("pathName" in reason for _path, reason in invalid))

    def test_21_unsafe_table_identifier_fails(self):
        doc = default_source_doc("dev", "payments-pg-to-mssql-001", "gg-mssql-tgt-fixture-01")
        doc["replication"]["extract"]["tables"] = ["public.payments; DROP TABLE x"]
        write_default_pipeline(self._tmp.name, source_doc=doc)
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertTrue(any("schema.table identifier" in reason for _path, reason in invalid))

    def test_22_unsafe_mapping_fails(self):
        doc = default_target_doc("dev", "payments-pg-to-mssql-001")
        doc["replication"]["replicat"]["mappings"] = [{"source": "public.payments", "target": "dbo.pay'ments"}]
        write_default_pipeline(self._tmp.name, target_doc=doc)
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertTrue(any("schema.table identifier" in reason for _path, reason in invalid))

    def test_23_raw_parameter_injection_fails(self):
        doc = default_source_doc("dev", "payments-pg-to-mssql-001", "gg-mssql-tgt-fixture-01")
        doc["replication"]["extract"]["tables"] = ["public.payments\nADD TRANDATA public.other;"]
        write_default_pipeline(self._tmp.name, source_doc=doc)
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertTrue(any("schema.table identifier" in reason for _path, reason in invalid))

    def test_24_database_secret_reference_validation_works(self):
        doc = default_source_doc("dev", "payments-pg-to-mssql-001", "gg-mssql-tgt-fixture-01")
        doc["replication"]["databaseCredentialSecret"] = "arn:aws:secretsmanager:eu-west-1:1:secret:x"
        write_default_pipeline(self._tmp.name, source_doc=doc)
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertTrue(any("databaseCredentialSecret" in reason for _path, reason in invalid))

    def test_24b_database_secret_reference_traversal_fails(self):
        doc = default_source_doc("dev", "payments-pg-to-mssql-001", "gg-mssql-tgt-fixture-01")
        doc["replication"]["databaseCredentialSecret"] = "dev/goldengate/../secret"
        write_default_pipeline(self._tmp.name, source_doc=doc)
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertTrue(any("databaseCredentialSecret" in reason for _path, reason in invalid))

    def test_25_generated_aliases_are_deterministic(self):
        alias1 = gdm.derive_database_credential_alias("gg-pg-src-fixture-01")
        alias2 = gdm.derive_database_credential_alias("gg-pg-src-fixture-01")
        self.assertEqual(alias1, alias2)
        self.assertTrue(alias1[0].isalpha())
        self.assertLessEqual(len(alias1), 30)

    def test_26_generated_aliases_are_collision_checked(self):
        source_doc = default_source_doc("dev", "pipeline-a", "gg-mssql-tgt-fixture-01")
        target_doc = default_target_doc("dev", "pipeline-a")
        write_doc(self._tmp.name, "dev", "gg-pg-src-fixture-01", source_doc)
        write_doc(self._tmp.name, "dev", "gg-mssql-tgt-fixture-01", target_doc)
        import unittest.mock as mock
        with mock.patch.object(gdm, "derive_database_credential_alias", return_value="SAME_ALIAS"):
            problems = gdm.validate("dev")
        self.assertTrue(any("alias collision" in p for p in problems))

    def test_27_replication_plan_is_deterministic(self):
        write_default_pipeline(self._tmp.name)
        active, _inactive, _invalid = gdm.scan("dev")
        source, target = gdm.find_replication_pipeline(active, "payments-pg-to-mssql-001")
        plan1 = gdm.build_replication_plan(source, target)
        plan2 = gdm.build_replication_plan(source, target)
        self.assertEqual(plan1, plan2)

    def test_28_replication_plan_contains_no_secret_values(self):
        write_default_pipeline(self._tmp.name)
        active, _inactive, _invalid = gdm.scan("dev")
        source, target = gdm.find_replication_pipeline(active, "payments-pg-to-mssql-001")
        plan = gdm.build_replication_plan(source, target)
        text = str(plan)
        for forbidden in ("OGG_DB_PASSWORD", "OGG_ADMIN_PWD", "password"):
            self.assertNotIn(forbidden, text)

    def test_supplemental_logging_must_cover_extract_tables(self):
        doc = default_source_doc("dev", "payments-pg-to-mssql-001", "gg-mssql-tgt-fixture-01")
        doc["replication"]["extract"]["tables"] = ["public.payments", "public.other"]
        write_default_pipeline(self._tmp.name, source_doc=doc)
        problems = gdm.validate("dev")
        self.assertTrue(any("supplementalLogging.objects does not cover" in p for p in problems))

    def test_replicat_mapping_source_must_exist_in_extract_tables(self):
        doc = default_target_doc("dev", "payments-pg-to-mssql-001")
        doc["replication"]["replicat"]["mappings"] = [{"source": "public.unknown", "target": "dbo.unknown"}]
        write_default_pipeline(self._tmp.name, target_doc=doc)
        problems = gdm.validate("dev")
        self.assertTrue(any("mappings source must exist" in p for p in problems))

    def test_pluginType_not_silently_defaulted(self):
        doc = default_source_doc("dev", "payments-pg-to-mssql-001", "gg-mssql-tgt-fixture-01")
        del doc["replication"]["extract"]["pluginType"]
        write_default_pipeline(self._tmp.name, source_doc=doc)
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertTrue(any("pluginType" in reason for _path, reason in invalid))

    def test_replicat_parallel_true_rejected(self):
        doc = default_target_doc("dev", "payments-pg-to-mssql-001")
        doc["replication"]["replicat"]["mode"]["parallel"] = True
        write_default_pipeline(self._tmp.name, target_doc=doc)
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertTrue(any("mode.parallel" in reason for _path, reason in invalid))

    def test_source_replicat_must_be_disabled(self):
        doc = default_source_doc("dev", "payments-pg-to-mssql-001", "gg-mssql-tgt-fixture-01")
        doc["replication"]["replicat"] = {
            "enabled": True, "name": "BADREP01", "sourceTrailName": "ma", "begin": "now",
            "mode": {"type": "nonintegrated", "parallel": False},
            "mappings": [{"source": "public.payments", "target": "dbo.payments"}],
        }
        write_default_pipeline(self._tmp.name, source_doc=doc)
        problems = gdm.validate("dev")
        self.assertTrue(any("source deployment must have replication.replicat.enabled=false" in p for p in problems))


class RegistryDeterminismTests(ScratchEnvironmentTestCase):
    """Deterministic, credential-free registry generation using role-derived shared secrets."""

    def _write_platform_and_monitor_fixtures(self):
        platform_dir = os.path.join(self._tmp.name, "platform", "dev", "goldengate-platform")
        os.makedirs(platform_dir, exist_ok=True)
        with open(os.path.join(platform_dir, "values.yaml"), "w") as f:
            f.write("namespaces:\n  runtime:\n    name: goldengate-dev\n"
                   "fluentBit:\n  namespaces:\n    monitoring: goldengate-monitoring\n")
        monitor_dir = os.path.join(self._tmp.name, "envs", "dev", "goldengate-monitor")
        os.makedirs(monitor_dir, exist_ok=True)
        with open(os.path.join(monitor_dir, "values.yaml"), "w") as f:
            f.write("ingress:\n  host: monitor.goldengate-dev.adcbmis.local\n")

    def test_registry_output_is_deterministic(self):
        self._write_platform_and_monitor_fixtures()
        write_descriptor(self._tmp.name, "dev", "gg-b-fixture", pipeline="p1", role="source")
        write_descriptor(self._tmp.name, "dev", "gg-a-fixture", pipeline="p2", role="source")
        first = gdm.build_registry("dev")
        second = gdm.build_registry("dev")
        self.assertEqual(first, second)

    def test_deployment_ordering_is_deterministic(self):
        self._write_platform_and_monitor_fixtures()
        write_descriptor(self._tmp.name, "dev", "gg-zzz-fixture", pipeline="p1", role="source")
        write_descriptor(self._tmp.name, "dev", "gg-aaa-fixture", pipeline="p2", role="source")
        registry = gdm.build_registry("dev")
        names = [d["name"] for d in registry["deployments"]]
        self.assertEqual(names, sorted(names))

    def test_registry_contains_no_credential_values(self):
        self._write_platform_and_monitor_fixtures()
        write_descriptor(self._tmp.name, "dev", "gg-secret-fixture-01", pipeline="p1", role="source")
        import yaml
        text = yaml.safe_dump(gdm.build_registry("dev"))
        for forbidden in ("hunter2", "OGG_ADMIN_PWD", "-----BEGIN"):
            self.assertNotIn(forbidden, text)

    def test_two_sources_sharing_one_secret_get_distinct_registry_entries(self):
        self._write_platform_and_monitor_fixtures()
        write_descriptor(self._tmp.name, "dev", "gg-source-a", pipeline="p1", role="source")
        write_descriptor(self._tmp.name, "dev", "gg-source-b", pipeline="p2", role="source")
        registry = gdm.build_registry("dev")
        entries = {d["name"]: d["adminSecret"] for d in registry["deployments"]}
        self.assertEqual(entries, {
            "gg-source-a": "dev/goldengate/source/admin",
            "gg-source-b": "dev/goldengate/source/admin",
        })

    def test_two_targets_sharing_one_secret_get_distinct_registry_entries(self):
        self._write_platform_and_monitor_fixtures()
        write_descriptor(self._tmp.name, "dev", "gg-target-a", pipeline="p1", role="target")
        write_descriptor(self._tmp.name, "dev", "gg-target-b", pipeline="p2", role="target")
        registry = gdm.build_registry("dev")
        entries = {d["name"]: d["adminSecret"] for d in registry["deployments"]}
        self.assertEqual(entries, {
            "gg-target-a": "dev/goldengate/target/admin",
            "gg-target-b": "dev/goldengate/target/admin",
        })


if __name__ == "__main__":
    unittest.main()
