"""Offline tests for hack/goldengate-deployment-model.py; run directly via `python3 hack/test-goldengate-deployment-model.py`."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest

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
{admin_secret_block}
global:
  environment: {environment}

deploymentModel: singleRuntime

runtime:
  deploymentType: {deployment_type}
  containerName: ogg-{deployment_type}
  image:
    repository: {repository}
    tag: "{tag}"
  serviceAccount:
    create: false
    name: {service_account}
  csi:
{csi_admin_block}    certificate:
      objectName: {csi_certificate_object_name}

ingress:
  hostDomain: {ingress_host_domain}
{alb_block}
{extra}
"""


def write_descriptor(root, environment, deployment_id, enabled=True, pipeline="test-pipeline", role="source",
                     deployment_type="oracle", repository=None, tag="1.0.0", service_account="gg-runtime-sa",
                     admin_secret=None, alb_group_order=None, extra="", raw_override=None,
                     csi_admin_object_name=None, csi_certificate_object_name=None, ingress_host_domain=None):
    folder = os.path.join(root, "envs", environment, deployment_id)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "values.yaml")
    if raw_override is not None:
        with open(path, "w") as f:
            f.write(raw_override)
        return path
    repository = repository or f"229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-{deployment_type}"
    admin_secret_block = ""
    if admin_secret is not None:
        name, managed = admin_secret
        admin_secret_block = f"  adminSecret:\n    name: {name}\n    managed: {'true' if managed else 'false'}\n"
    alb_block = f'  alb:\n    groupOrder: "{alb_group_order}"' if alb_group_order is not None else ""
    csi_admin_block = f"    admin:\n      objectName: {csi_admin_object_name}\n" if csi_admin_object_name is not None else ""
    if csi_certificate_object_name is None:
        csi_certificate_object_name = f"{environment}/goldengate/tls-certificate"
    if ingress_host_domain is None:
        ingress_host_domain = f"goldengate-{environment}.adcbmis.local"
    text = BASE_DESCRIPTOR.format(
        enabled=str(enabled).lower(), pipeline=pipeline, role=role, environment=environment,
        deployment_type=deployment_type, repository=repository, tag=tag, service_account=service_account,
        admin_secret_block=admin_secret_block, alb_block=alb_block, extra=extra,
        csi_admin_block=csi_admin_block, csi_certificate_object_name=csi_certificate_object_name,
        ingress_host_domain=ingress_host_domain)
    with open(path, "w") as f:
        f.write(text)
    return path


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
    """Tests 1, 2, 10, 11, 31: exercised against the real, live envs/dev descriptors -- no scratch root."""

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

    def test_existing_oracle_legacy_secret_unchanged(self):
        active, _inactive, _invalid = gdm.scan("dev")
        by_id = {d["deploymentId"]: d for d in active}
        self.assertEqual(by_id["gg-oracle-payments-01"]["adminSecretName"], "dev/goldengate/source/admin")
        self.assertFalse(by_id["gg-oracle-payments-01"]["adminSecretManaged"])

    def test_existing_postgresql_legacy_secret_unchanged(self):
        active, _inactive, _invalid = gdm.scan("dev")
        by_id = {d["deploymentId"]: d for d in active}
        self.assertEqual(by_id["gg-postgresql-payments-01"]["adminSecretName"], "dev/goldengate/target/admin")
        self.assertFalse(by_id["gg-postgresql-payments-01"]["adminSecretManaged"])

    def test_generated_registry_contains_both_existing_live_deployments(self):
        registry = gdm.build_registry("dev")
        names = {d["name"] for d in registry["deployments"]}
        self.assertEqual(names, {"gg-oracle-payments-01", "gg-postgresql-payments-01"})


class GenericDeploymentTypeTests(ScratchEnvironmentTestCase):
    """Tests 3-8: the tool must accept any safe token as deploymentType, without a fixed engine allowlist."""

    def test_synthetic_postgresql_source_descriptor_parses(self):
        write_descriptor(self._tmp.name, "dev", "gg-postgresql-payments-sqlserver-01",
                         pipeline="payments-pg-to-sqlserver-001", role="source", deployment_type="postgresql")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(len(active), 1)

    def test_synthetic_sqlserver_target_descriptor_parses(self):
        write_descriptor(self._tmp.name, "dev", "gg-sqlserver-payments-01",
                         pipeline="payments-pg-to-sqlserver-001", role="target", deployment_type="sqlserver")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(active[0]["deploymentType"], "sqlserver")

    def test_synthetic_mysql_descriptor_parses_without_monitor_source_changes(self):
        write_descriptor(self._tmp.name, "dev", "gg-mysql-fixture-01", deployment_type="mysql")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(active[0]["deploymentType"], "mysql")
        # No mysql-specific branch exists in the tool itself -- generic token validation only.
        with open(TOOL_PATH) as f:
            source = f.read()
        self.assertNotIn('"mysql"', source)

    def test_safe_distributed_type_parses(self):
        write_descriptor(self._tmp.name, "dev", "gg-distributed-fixture-01", deployment_type="distributed")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(active[0]["deploymentType"], "distributed")

    def test_unsafe_deployment_type_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-unsafe-fixture-01", deployment_type="oracle/../etc")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_uppercase_deployment_type_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-uppercase-fixture-01", deployment_type="Oracle")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_trailing_newline_rejected_not_matched_before_dollar_anchor(self):
        # Regression: Python's $ matches just before a trailing \n even without re.MULTILINE; \Z does not.
        self.assertFalse(gdm._safe_token("gg-oracle-payments-01\n", 63))


class AdminSecretDerivationTests(ScratchEnvironmentTestCase):
    """Tests 9, 12, 33: default managed admin-secret naming and inventory exclusion/ordering."""

    def test_default_managed_admin_secret_name_derives_correctly(self):
        write_descriptor(self._tmp.name, "dev", "gg-new-fixture-01")
        active, _inactive, _invalid = gdm.scan("dev")
        self.assertEqual(active[0]["adminSecretName"], "dev/goldengate/runtime/gg-new-fixture-01/admin")
        self.assertTrue(active[0]["adminSecretManaged"])

    def test_managed_false_secrets_excluded_from_managed_inventory(self):
        write_descriptor(self._tmp.name, "dev", "gg-legacy-fixture-01",
                         admin_secret=("dev/goldengate/legacy/admin", False))
        write_descriptor(self._tmp.name, "dev", "gg-managed-fixture-01", pipeline="other-pipeline")
        result = gdm.managed_secrets("dev")
        ids = [entry[0] for entry in result]
        self.assertNotIn("gg-legacy-fixture-01", ids)
        self.assertIn("gg-managed-fixture-01", ids)

    def test_secret_inventory_ordering_is_deterministic(self):
        write_descriptor(self._tmp.name, "dev", "gg-zzz-fixture", pipeline="p1")
        write_descriptor(self._tmp.name, "dev", "gg-aaa-fixture", pipeline="p2")
        result_a = gdm.managed_secrets("dev")
        result_b = gdm.managed_secrets("dev")
        self.assertEqual(result_a, result_b)
        self.assertEqual(result_a, sorted(result_a))


class EnvironmentScopedContractTests(ScratchEnvironmentTestCase):
    """Phase 6D0 correction Task 1: environment-scoped secrets, CSI consistency, EFS, and stricter ECR/name grammar."""

    def test_default_admin_secret_is_scoped_to_the_selected_environment_not_hardcoded_dev(self):
        write_descriptor(self._tmp.name, "staging", "gg-fixture-01")
        active, _inactive, invalid = gdm.scan("staging")
        self.assertEqual(invalid, [])
        self.assertEqual(active[0]["adminSecretName"], "staging/goldengate/runtime/gg-fixture-01/admin")

    def test_managed_true_with_non_deterministic_name_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         admin_secret=("dev/goldengate/runtime/some-other-id/admin", True))
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_managed_false_with_arn_style_name_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         admin_secret=("arn:aws:secretsmanager:eu-west-1:123456789012:secret:x", False))
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_managed_false_with_traversal_name_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         admin_secret=("dev/../etc/admin", False))
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_managed_false_with_leading_slash_name_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         admin_secret=("/dev/goldengate/legacy/admin", False))
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_managed_false_with_out_of_environment_scope_name_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         admin_secret=("prod/goldengate/legacy/admin", False))
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_csi_certificate_object_name_must_match_shared_tls_secret(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         csi_certificate_object_name="dev/goldengate/wrong-cert")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_csi_admin_object_name_mismatch_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         csi_admin_object_name="dev/goldengate/wrong-admin")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_csi_admin_object_name_matching_resolved_name_passes(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         csi_admin_object_name="dev/goldengate/runtime/gg-fixture-01/admin")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])

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

    def test_field_merely_referencing_a_secret_by_name_is_allowed(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         admin_secret=("dev/goldengate/legacy/admin", False))
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])

    def test_efs_enabled_requires_safe_filesystem_id(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         extra="persistence:\n  enabled: true\n  efs:\n    fileSystemId: not-an-fs-id\n")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_efs_enabled_with_safe_filesystem_id_passes(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         extra="persistence:\n  enabled: true\n  efs:\n    fileSystemId: fs-0123456789abcdef0\n")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])

    def test_derived_namespace_fields_present(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01")
        active, _inactive, _invalid = gdm.scan("dev")
        self.assertEqual(active[0]["runtimeNamespace"], "goldengate-dev")
        self.assertEqual(active[0]["monitoringNamespace"], "goldengate-monitoring")
        self.assertEqual(active[0]["ingressDomain"], "goldengate-dev.adcbmis.local")
        self.assertEqual(active[0]["tlsSecretName"], "dev/goldengate/tls-certificate")


class FullValidationGatingTests(unittest.TestCase):
    """Task 1 item 2: no command may emit partial inventory while another runtime folder is invalid."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._original_root = gdm.REPO_ROOT
        gdm.REPO_ROOT = self._tmp.name

    def tearDown(self):
        gdm.REPO_ROOT = self._original_root
        self._tmp.cleanup()

    def test_list_fails_closed_when_an_unrelated_folder_is_invalid(self):
        write_descriptor(self._tmp.name, "dev", "gg-good-fixture-01")
        write_descriptor(self._tmp.name, "dev", "gg-bad-fixture-01", tag="latest")
        active, inactive, invalid, problems = gdm.cmd_list.__wrapped__(self) if False else (None, None, None, None)
        # cmd_list prints; assert via the underlying full-validation gate instead of stdout capture.
        _active, _inactive, invalid, problems = gdm._run_full_validation("dev")
        self.assertTrue(invalid or problems)

    def test_managed_secrets_command_returns_nonzero_when_a_folder_is_invalid(self):
        write_descriptor(self._tmp.name, "dev", "gg-good-fixture-01")
        write_descriptor(self._tmp.name, "dev", "gg-bad-fixture-01", tag="latest")

        class Args:
            environment = "dev"

        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = gdm.cmd_managed_secrets(Args())
        self.assertEqual(exit_code, 1)
        self.assertNotIn("gg-good-fixture-01", buf.getvalue())

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


class LifecycleClassificationTests(ScratchEnvironmentTestCase):
    """Tests 13, 14: disabled/absent runtimes validate but are excluded from active inventory."""

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
    """Tests 15-26: malformed/invalid candidates never silently disappear."""

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

    def test_wrong_service_account_fails(self):
        write_descriptor(self._tmp.name, "dev", "gg-wrong-sa-fixture-01", service_account="gg-something-else")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_embedded_credentials_fail(self):
        write_descriptor(self._tmp.name, "dev", "gg-cred-fixture-01",
                         extra="\nadminPassword: hunter2\n")
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)
        self.assertNotIn("hunter2", invalid[0][1])


class ReplicationSchemaTests(ScratchEnvironmentTestCase):
    """Tests 29, 30: the Phase 6D0 replication bootstrap gate."""

    def test_replication_enabled_false_passes(self):
        write_descriptor(self._tmp.name, "dev", "gg-repl-off-fixture-01",
                         extra="\nreplication:\n  enabled: false\n")
        problems = gdm.validate("dev")
        self.assertEqual(problems, [])

    def test_replication_enabled_true_fails_with_fixed_message(self):
        write_descriptor(self._tmp.name, "dev", "gg-repl-on-fixture-01",
                         extra="\nreplication:\n  enabled: true\n")
        problems = gdm.validate("dev")
        self.assertTrue(any(gdm.REPLICATION_DISABLED_MESSAGE in p for p in problems))


class RegistryDeterminismTests(ScratchEnvironmentTestCase):
    """Tests 27, 28, 32: deterministic, credential-free registry generation."""

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


if __name__ == "__main__":
    unittest.main()
