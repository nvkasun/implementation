"""Offline tests for hack/goldengate-deployment-model.py; run directly via `python3 hack/test-goldengate-deployment-model.py`."""
from __future__ import annotations

import argparse
import contextlib
import copy
import importlib.util
import io
import json
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
deploymentModel: singleRuntime

runtime:
  deploymentType: {deployment_type}
  containerName: ogg-{deployment_type}
  image:
    repositoryName: {repository_name}
    tag: "{tag}"
{service_account_block}  csi:
    enabled: true
{csi_role_arn_block}    admin:
      enabled: true
{csi_admin_object_name_block}    certificate:
      enabled: true
{csi_certificate_object_name_block}
ingress:
  enabled: true
{alb_block}
{extra}
"""


_SYNTHETIC_ENVIRONMENT_YAML = """\
schemaVersion: 1
environment: {environment}
aws:
  region: eu-west-1
  workloadAccountId: "668311715351"
  buildAccountId: "229410149234"
eks:
  clusterName: gg-scratch-test
  oidcIssuer: "https://oidc.eks.eu-west-1.amazonaws.com/id/0123456789ABCDEF0123456789ABCDEF"
namespaces:
  runtime: goldengate-{environment}
  monitoring: goldengate-monitoring
  argocd: argocd
  observability: amazon-cloudwatch
network:
  dnsDomain: goldengate-{environment}.adcbmis.local
  albGroupName: gg-scratch-test-alb
  certificateArn: arn:aws:acm:eu-west-1:668311715351:certificate/00000000-0000-0000-0000-000000000000
iam:
  roles:
    eksDeploy: GoldenGateEKSDeployRole-{environment}
    runtime: GoldenGateSecretsReadRole-{environment}
    monitor: GoldenGateMonitorReadRole-{environment}
    argocdEcrRead: GoldenGateArgocdECRRead-{environment}
    platformLogging: GoldenGatePlatformLoggingRole-{environment}
    cloudwatchMetrics: GoldenGateCloudWatchMetricsRole-{environment}
  runnerRoleName: RunnerRole-goldengate-eks-app_{environment}
  ecrSyncRoleArn: arn:aws:iam::229410149234:role/scratch-test-ecr-sync-role
kms:
  monitorDynamoDbKeyArn: arn:aws:kms:eu-west-1:668311715351:key/00000000-0000-0000-0000-000000000000
efs:
  sharedSecurityGroupDescription: "Security group for EFS filesystem - scratch test"
tags:
  applicationName: CloudFactory
  businessCriticality: Low
  businessUnit: TechnologyPlatform
  businessUnitOwner: scratch-test-owner
  costCenter: "000"
  mapMigrated: scratch-test
  requestReference: SCRATCH-TEST
  dataClassification: General
"""


def ensure_scratch_environment_yaml(root, environment):
    """Writes a deterministic, schema-valid synthetic envs/<environment>/environment.yaml into the isolated scratch root if one doesn't already exist -- account IDs/region/cluster name here are fixed synthetic fixture values (matching this file's own long-established repository/domain fixture defaults below), never production hardcoding, since they only ever exist under a tempfile.TemporaryDirectory() scratch root."""
    path = os.path.join(root, "envs", environment, "environment.yaml")
    if os.path.exists(path):
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(_SYNTHETIC_ENVIRONMENT_YAML.format(environment=environment))
    return path


def write_descriptor(root, environment, deployment_id, enabled=True, pipeline="test-pipeline", role="source",
                     deployment_type="oracle", repository_name=None, tag="1.0.0",
                     service_account_name=None, service_account_create=None,
                     deployment_admin_secret=None, alb_group_order=None, extra="", raw_override=None,
                     csi_admin_object_name=None, csi_certificate_object_name=None,
                     csi_service_account_role_arn=None, ingress_host_domain=None,
                     image_repository_override=None, global_environment_override=None,
                     ingress_alb_group_name=None, ingress_alb_certificate_arn=None):
    ensure_scratch_environment_yaml(root, environment)
    folder = os.path.join(root, "envs", environment, deployment_id)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "values.yaml")
    if raw_override is not None:
        with open(path, "w") as f:
            f.write(raw_override)
        return path
    repository_name = repository_name or f"ogg-{deployment_type}"

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

    alb_lines = []
    if alb_group_order is not None:
        alb_lines.append(f'    groupOrder: "{alb_group_order}"')
    # ingress_alb_group_name/ingress_alb_certificate_arn are opt-in, to test the forbidden-override rejection of descriptor-declared shared ALB identity.
    if ingress_alb_group_name is not None:
        alb_lines.append(f"    groupName: {ingress_alb_group_name}")
    if ingress_alb_certificate_arn is not None:
        alb_lines.append(f"    certificateArn: {ingress_alb_certificate_arn}")
    alb_block = ("  alb:\n" + "\n".join(alb_lines)) if alb_lines else ""
    # ingress_host_domain is opt-in; absent by default so no descriptor written here declares hostDomain, matching the real Phase 9 schema.
    ingress_host_domain_block = f"  hostDomain: {ingress_host_domain}\n" if ingress_host_domain is not None else ""

    # image_repository_override/global_environment_override are opt-in, to test the forbidden-override rejection of descriptor-declared shared identity.
    if global_environment_override is not None:
        extra = extra + f"global:\n  environment: {global_environment_override}\n"

    text = BASE_DESCRIPTOR.format(
        enabled=str(enabled).lower(), pipeline=pipeline, role=role,
        deployment_type=deployment_type, repository_name=repository_name, tag=tag,
        deployment_admin_secret_block=deployment_admin_secret_block,
        service_account_block=service_account_block,
        csi_role_arn_block=csi_role_arn_block,
        csi_admin_object_name_block=csi_admin_object_name_block,
        csi_certificate_object_name_block=csi_certificate_object_name_block,
        alb_block=alb_block, extra=extra)
    if image_repository_override is not None:
        text = text.replace(
            f"    repositoryName: {repository_name}\n",
            f"    repositoryName: {repository_name}\n    repository: {image_repository_override}\n")
    if ingress_host_domain_block:
        text = text.replace("ingress:\n  enabled: true\n", f"ingress:\n  enabled: true\n{ingress_host_domain_block}")
    with open(path, "w") as f:
        f.write(text)
    return path


def default_source_doc(environment, pipeline, target_id):
    return {
        "deployment": {"enabled": True, "pipeline": pipeline, "role": "source"},
        "deploymentModel": "singleRuntime",
        "runtime": {
            "deploymentType": "postgresql",
            "containerName": "ogg-postgresql",
            "image": {"repositoryName": "ogg-postgresql", "tag": "23.26.2.0.1"},
            "csi": {"enabled": True, "admin": {"enabled": True}, "certificate": {"enabled": True}},
        },
        "ingress": {"enabled": True},
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
        "deploymentModel": "singleRuntime",
        "runtime": {
            "deploymentType": "mssql",
            "containerName": "ogg-sqlserver",
            "image": {"repositoryName": "ogg-sqlserver", "tag": "23.26.2.0.1"},
            "csi": {"enabled": True, "admin": {"enabled": True}, "certificate": {"enabled": True}},
        },
        "ingress": {"enabled": True},
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
        "deploymentModel": "singleRuntime",
        "runtime": {
            "deploymentType": "oracle",
            "containerName": "ogg-oracle",
            "image": {"repositoryName": "ogg-oracle", "tag": "1.0.0"},
            "csi": {"enabled": True, "admin": {"enabled": True}, "certificate": {"enabled": True}},
            "storage": {"u02": {"type": "efs"}},
        },
        "ingress": {"enabled": True},
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
    """Base class: points gdm.REPO_ROOT at an isolated temp directory for the duration of each test. Also seeds envs/dev/environment.yaml (the overwhelmingly common fixture environment) into that scratch root up front -- individual write_descriptor() calls seed any other environment name (e.g. "sit") on demand."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._original_root = gdm.REPO_ROOT
        gdm.REPO_ROOT = self._tmp.name
        ensure_scratch_environment_yaml(self._tmp.name, "dev")

    def tearDown(self):
        gdm.REPO_ROOT = self._original_root
        self._tmp.cleanup()


class RealRepositoryDescriptorTests(unittest.TestCase):
    """Exercised against the real, live envs/dev descriptors -- no scratch root. Derives source/target descriptors by role, never by a specific deployment ID, so retiring or onboarding a descriptor never requires editing this class."""

    def _active_by_role(self, role):
        # lifecycle.state=absent can legitimately leave zero active descriptors during a controlled environment decommission; this only validates whichever descriptors ARE active, never their count.
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        return [d for d in active if d["role"] == role]

    def test_current_source_descriptors_parse_with_a_real_deployment_type(self):
        for d in self._active_by_role("source"):
            self.assertTrue(d["deploymentType"])

    def test_current_target_descriptors_parse_with_a_real_deployment_type(self):
        for d in self._active_by_role("target"):
            self.assertTrue(d["deploymentType"])

    def test_current_source_descriptors_render_with_source_shared_secret(self):
        for d in self._active_by_role("source"):
            self.assertEqual(d["adminSecretName"], "dev/goldengate/source/admin")

    def test_current_target_descriptors_render_with_target_shared_secret(self):
        for d in self._active_by_role("target"):
            self.assertEqual(d["adminSecretName"], "dev/goldengate/target/admin")

    def test_registry_contains_exactly_the_scanned_active_ids(self):
        # Self-service: dynamic invariant, never a hardcoded name/count -- onboarding a new envs/dev/<id>/values.yaml folder must never require editing this test. Proves the registry contains EXACTLY what the canonical folder scanner contains.
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        expected_active_ids = sorted(d["deploymentId"] for d in active)

        registry = gdm.build_registry("dev")
        actual_registry_ids = sorted(d["name"] for d in registry["deployments"])
        self.assertEqual(actual_registry_ids, expected_active_ids)

    def test_managed_efs_inventory_matches_dynamically_derived_managed_set(self):
        # Self-service: never asserts today's managed count is any particular fixed number -- compares the real cmd_managed_efs_inventory JSON output against a set derived independently from the same scan (efsMode == "managed"), including lifecycle.state=absent descriptors (inactive), exactly like the real command.
        active, inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        expected_managed = sorted(
            (
                {"deploymentId": d["deploymentId"], "efsCreationToken": d["efsCreationToken"]}
                for d in active + inactive
                if d["efsMode"] == "managed"
            ),
            key=lambda x: x["deploymentId"],
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exit_code = gdm.cmd_managed_efs_inventory(argparse.Namespace(environment="dev"))
        self.assertEqual(exit_code, 0)
        actual_managed = json.loads(buf.getvalue())
        self.assertEqual(actual_managed, expected_managed)

    def test_at_least_one_managed_efs_descriptor_exists(self):
        # MILESTONE (temporary, not a permanent inventory coupling): proves the first production managed-EFS runtime was successfully onboarded, without naming it or coupling to an exact count. Safe to delete once managed EFS is routine.
        active, inactive, _invalid = gdm.scan("dev")
        managed = [d for d in active + inactive if d["efsMode"] == "managed"]
        self.assertGreaterEqual(len(managed), 1)

    def test_every_active_descriptor_satisfies_generic_contracts(self):
        # Self-service: one generic loop, driven entirely by each descriptor's OWN properties (role/persistence mode/deploymentType) -- never by deployment ID -- so it automatically covers any future onboarded folder without new test code.
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(gdm.validate("dev"), [])

        alb_orders = []
        for d in active:
            with self.subTest(deploymentId=d["deploymentId"]):
                self.assertEqual(d["runtimeServiceAccountName"], gdm.resolve_runtime_service_account(d["deploymentType"]))
                self.assertEqual(d["tlsSecretName"], gdm.resolve_tls_secret(d["environment"]))
                self.assertEqual(d["adminSecretName"], gdm.resolve_admin_secret(d["environment"], d["role"]))

                with open(os.path.join(REPO_ROOT, "envs", "dev", d["deploymentId"], "values.yaml")) as f:
                    raw = yaml.safe_load(f)
                ports = ((raw.get("runtime") or {}).get("service") or {}).get("ports") or {}
                if d["role"] == "source":
                    self.assertEqual(ports.get("dist"), 9013)
                    self.assertIsNone(ports.get("receiver"))
                elif d["role"] == "target":
                    self.assertIsNone(ports.get("dist"))
                    self.assertEqual(ports.get("receiver"), 9014)

                if d["albGroupOrder"] is not None:
                    self.assertTrue(str(d["albGroupOrder"]).lstrip("-").isdigit())
                    if (raw.get("ingress") or {}).get("mode") == "shared":
                        alb_orders.append(d["albGroupOrder"])

                persistence = raw.get("persistence") or {}
                if persistence.get("enabled") is True and persistence.get("provider") == "efs":
                    efs = persistence.get("efs") or {}
                    if d["efsMode"] == "managed":
                        self.assertIsNone(d["efsFileSystemId"])
                        self.assertTrue(d["efsCreationToken"])
                        self.assertEqual(d["efsCreationToken"], gdm.derive_efs_creation_token(d["environment"], d["deploymentId"]))
                        self.assertEqual((efs.get("storageClass") or {}).get("reclaimPolicy"), "Retain")
                    elif d["efsMode"] == "existing":
                        self.assertIsNotNone(d["efsFileSystemId"])
                        self.assertRegex(d["efsFileSystemId"], gdm._EFS_FILESYSTEM_ID_RE.pattern)

        self.assertEqual(len(alb_orders), len(set(alb_orders)), "ALB groupOrder must be unique across every active shared-ALB descriptor")

    def test_every_active_deployment_resolves_to_gg_runtime_sa(self):
        # Restored shared runtime identity: deploymentType never selects the ServiceAccount -- every active singleRuntime deployment resolves the one platform-owned gg-runtime-sa.
        active, _inactive, _invalid = gdm.scan("dev")
        for d in active:
            self.assertEqual(d["runtimeServiceAccountName"], "gg-runtime-sa")

    def test_current_active_deployments_have_replication_disabled(self):
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        for d in active:
            self.assertFalse(d["replicationEnabled"])


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

    def test_any_safe_type_derives_the_shared_service_account_without_a_fixed_allowlist(self):
        write_descriptor(self._tmp.name, "dev", "gg-mysql-fixture-01", deployment_type="mysql")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(active[0]["runtimeServiceAccountName"], "gg-runtime-sa")

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
    """Restored shared runtime identity: EVERY deploymentType resolves the same gg-runtime-sa, never a per-type gg-<type>-sa map. deploymentType controls image/product/ports/replication semantics, never AWS runtime identity."""

    def test_oracle_resolves_to_gg_runtime_sa(self):
        self.assertEqual(gdm.resolve_runtime_service_account("oracle"), "gg-runtime-sa")

    def test_postgresql_resolves_to_gg_runtime_sa(self):
        self.assertEqual(gdm.resolve_runtime_service_account("postgresql"), "gg-runtime-sa")

    def test_mssql_resolves_to_gg_runtime_sa(self):
        self.assertEqual(gdm.resolve_runtime_service_account("mssql"), "gg-runtime-sa")

    def test_daa_resolves_to_gg_runtime_sa(self):
        self.assertEqual(gdm.resolve_runtime_service_account("daa"), "gg-runtime-sa")

    def test_mysql_resolves_to_gg_runtime_sa(self):
        self.assertEqual(gdm.resolve_runtime_service_account("mysql"), "gg-runtime-sa")

    def test_another_synthetic_safe_type_also_resolves_to_gg_runtime_sa(self):
        self.assertEqual(gdm.resolve_runtime_service_account("cassandra"), "gg-runtime-sa")

    def test_no_hardcoded_deployment_type_to_service_account_map_exists_in_source(self):
        with open(TOOL_PATH) as f:
            source = f.read()
        self.assertNotIn("RUNTIME_IDENTITY_MAP", source)
        self.assertNotIn('f"gg-{deployment_type}-sa"', source)


class SyntheticFlavourRenderTests(ScratchEnvironmentTestCase):
    """Tests 8, 9, 15, 16: every type shares the one restored gg-runtime-sa identity, image stays values-file-derived, existing Oracle/PostgreSQL unaffected."""

    def test_synthetic_mssql_runtime_resolves_gg_runtime_sa(self):
        write_descriptor(self._tmp.name, "dev", "gg-mssql-fixture-01",
                         pipeline="p1", role="target", deployment_type="mssql")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(active[0]["runtimeServiceAccountName"], "gg-runtime-sa")

    def test_synthetic_daa_runtime_resolves_gg_runtime_sa(self):
        write_descriptor(self._tmp.name, "dev", "gg-daa-fixture-01",
                         pipeline="p1", role="source", deployment_type="daa")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(active[0]["runtimeServiceAccountName"], "gg-runtime-sa")

    def test_two_deployments_of_the_same_type_share_one_service_account(self):
        write_descriptor(self._tmp.name, "dev", "gg-postgresql-fixture-a", pipeline="pa", role="source", deployment_type="postgresql")
        write_descriptor(self._tmp.name, "dev", "gg-postgresql-fixture-b", pipeline="pb", role="source", deployment_type="postgresql")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        sa_names = {d["runtimeServiceAccountName"] for d in active}
        self.assertEqual(sa_names, {"gg-runtime-sa"})

    def test_different_types_still_share_the_same_service_account(self):
        # Restored shared identity: deploymentType controls image/product/ports/replication semantics, never AWS runtime identity -- different types must NOT produce distinct ServiceAccounts anymore.
        write_descriptor(self._tmp.name, "dev", "gg-oracle-fixture-a", pipeline="pa", role="source", deployment_type="oracle")
        write_descriptor(self._tmp.name, "dev", "gg-mssql-fixture-b", pipeline="pb", role="target", deployment_type="mssql")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        sa_names = {d["deploymentId"]: d["runtimeServiceAccountName"] for d in active}
        self.assertEqual(sa_names["gg-oracle-fixture-a"], "gg-runtime-sa")
        self.assertEqual(sa_names["gg-mssql-fixture-b"], "gg-runtime-sa")
        self.assertEqual(sa_names["gg-oracle-fixture-a"], sa_names["gg-mssql-fixture-b"])

    def test_mssql_image_comes_from_the_values_file_not_a_mapping(self):
        write_descriptor(self._tmp.name, "dev", "gg-mssql-fixture-01",
                         pipeline="p1", role="target", deployment_type="mssql",
                         repository_name="ogg-sqlserver", tag="9.9.9")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(active[0]["imageRepository"], "229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-sqlserver")
        self.assertEqual(active[0]["imageRepositoryName"], "ogg-sqlserver")
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
        self.assertEqual(active[0]["runtimeServiceAccountName"], "gg-runtime-sa")
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
        self.assertEqual(inventory, [("oracle", "gg-runtime-sa"), ("postgresql", "gg-runtime-sa")])

    def test_disabled_deployment_excluded_from_identity_inventory(self):
        write_descriptor(self._tmp.name, "dev", "gg-oracle-fixture-a", pipeline="pa", role="source", deployment_type="oracle")
        write_descriptor(self._tmp.name, "dev", "gg-mssql-fixture-b", pipeline="pb", role="target", deployment_type="mssql", enabled=False)
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        inventory = gdm.runtime_identity_inventory(active)
        self.assertEqual(inventory, [("oracle", "gg-runtime-sa")])


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
        self.assertEqual(active[0]["runtimeServiceAccountName"], "gg-runtime-sa")

    def test_image_repository_override_is_rejected(self):
        # runtime.image.repository is shared identity derived by the deployment model; a descriptor must never declare it directly.
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         image_repository_override="229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-oracle")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)
        self.assertIn("runtime.image.repository", invalid[0][1])

    def test_global_environment_override_is_rejected(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01", global_environment_override="dev")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)
        self.assertIn("global.environment", invalid[0][1])

    def test_ingress_alb_group_name_override_is_rejected(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01", ingress_alb_group_name="gg-poc-dev-alb")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)
        self.assertIn("ingress.alb.groupName", invalid[0][1])

    def test_ingress_alb_certificate_arn_override_is_rejected(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         ingress_alb_certificate_arn="arn:aws:acm:eu-west-1:668311715351:certificate/00000000-0000-0000-0000-000000000000")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)
        self.assertIn("ingress.alb.certificateArn", invalid[0][1])


class EnvironmentScopedContractTests(ScratchEnvironmentTestCase):
    """Environment-scoped derivation, ECR/name grammar, and EFS identity."""

    def test_admin_secret_is_scoped_to_the_selected_environment_not_hardcoded_dev(self):
        write_descriptor(self._tmp.name, "sit", "gg-fixture-01")
        active, _inactive, invalid = gdm.scan("sit")
        self.assertEqual(invalid, [])
        self.assertEqual(active[0]["adminSecretName"], "sit/goldengate/source/admin")

    def test_ingress_host_domain_declared_in_descriptor_fails(self):
        # ingress.hostDomain is shared environment configuration -- declaring it at all is a forbidden override, regardless of value.
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         ingress_host_domain="goldengate-dev.adcbmis.local")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_ecr_repository_name_with_digest_syntax_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         repository_name="ogg-oracle@sha256:" + "a" * 64)
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_ecr_repository_name_with_whitespace_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         repository_name="ogg oracle")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_ecr_repository_name_with_empty_suffix_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         repository_name="ogg-oracle/")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_ecr_repository_name_with_double_slash_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         repository_name="ogg//oracle")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_multi_segment_ecr_repository_name_passes(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         repository_name="goldengate/ogg-oracle")
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


class ManagedEfsInventoryCommandTests(ScratchEnvironmentTestCase):
    """The managed-efs-inventory command feeds the workflow's managed_efs_inventory_guard; it must include lifecycle.state=absent managed descriptors (their EFS is retained, not decommissioned) and exclude existing-mode descriptors entirely."""

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

    # test_public_image_repository_fails/test_wrong_ecr_account_fails no longer apply: a descriptor can't declare a full repository string at all; see test_image_repository_override_is_rejected above.

    def test_embedded_credentials_fail(self):
        write_descriptor(self._tmp.name, "dev", "gg-cred-fixture-01",
                         extra="\nadminPassword: hunter2\n")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)
        self.assertNotIn("hunter2", invalid[0][1])

    def test_boolean_like_string_enabled_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-boolstr-fixture-01",
                         raw_override=BASE_DESCRIPTOR.format(
                             enabled='"true"', pipeline="p1", role="source",
                             deployment_admin_secret_block="", deployment_type="oracle",
                             repository_name="ogg-oracle", tag="1.0.0",
                             service_account_block="", csi_role_arn_block="", csi_admin_object_name_block="",
                             csi_certificate_object_name_block="",
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
