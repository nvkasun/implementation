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

    def test_generated_registry_contains_both_existing_live_deployments(self):
        registry = gdm.build_registry("dev")
        names = {d["name"] for d in registry["deployments"]}
        self.assertEqual(names, {"gg-oracle-payments-01", "gg-postgresql-payments-01"})

    def test_both_existing_deployments_use_gg_runtime_sa(self):
        active, _inactive, _invalid = gdm.scan("dev")
        for d in active:
            self.assertEqual(d["runtimeServiceAccountName"], "gg-runtime-sa")


class GenericDeploymentTypeTests(ScratchEnvironmentTestCase):
    """The tool must accept any safe token as deploymentType, without a fixed engine allowlist."""

    def test_synthetic_postgresql_source_descriptor_parses(self):
        write_descriptor(self._tmp.name, "dev", "gg-postgresql-payments-sqlserver-01",
                         pipeline="payments-pg-to-sqlserver-001", role="source", deployment_type="postgresql")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["adminSecretName"], "dev/goldengate/source/admin")

    def test_synthetic_sqlserver_target_descriptor_parses(self):
        write_descriptor(self._tmp.name, "dev", "gg-sqlserver-payments-01",
                         pipeline="payments-pg-to-sqlserver-001", role="target", deployment_type="sqlserver")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(active[0]["deploymentType"], "sqlserver")
        self.assertEqual(active[0]["adminSecretName"], "dev/goldengate/target/admin")

    def test_synthetic_mysql_descriptor_parses_without_engine_allowlist(self):
        write_descriptor(self._tmp.name, "dev", "gg-mysql-fixture-01", deployment_type="mysql")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(active[0]["deploymentType"], "mysql")
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

    def test_service_account_create_true_override_is_rejected(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         service_account_name="gg-runtime-sa", service_account_create=True)
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(len(invalid), 1)

    def test_correct_service_account_override_is_tolerated(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01",
                         service_account_name="gg-runtime-sa", service_account_create=False)
        _active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])

    def test_service_account_omitted_entirely_is_valid(self):
        write_descriptor(self._tmp.name, "dev", "gg-fixture-01")
        active, _inactive, invalid = gdm.scan("dev")
        self.assertEqual(invalid, [])
        self.assertEqual(active[0]["runtimeServiceAccountName"], "gg-runtime-sa")


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
    """The Phase 6D0 replication bootstrap gate."""

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
