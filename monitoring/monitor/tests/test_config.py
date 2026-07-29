import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config as cfgmod

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def write_deployments_yaml(root, content):
    path = os.path.join(root, "goldengate-deployments.yaml")
    with open(path, "w") as f:
        f.write(content)
    return path


VALID_DOC = """
environment: dev
runtimeNamespace: goldengate-dev
monitoringNamespace: goldengate-monitoring
dnsDomain: goldengate-dev.adcbmis.local
deployments:
  - name: gg-oracle-payments-01
    type: oracle
    pipeline: payments-ora-to-pg-001
    role: source
    enabled: true
    adminSecret: dev/goldengate/source/admin
  - name: gg-postgresql-payments-01
    type: postgresql
    pipeline: payments-ora-to-pg-001
    role: target
    enabled: true
    adminSecret: dev/goldengate/target/admin
"""


class LoadDeploymentsTests(unittest.TestCase):
    def test_real_repo_config_loads_and_derives_correctly(self):
        doc = cfgmod.load_deployments(os.path.join(REPO_ROOT, "envs", "dev"))
        self.assertEqual(doc["environment"], "dev")
        self.assertEqual(doc["runtimeNamespace"], "goldengate-dev")
        self.assertEqual(len(doc["deployments"]), 2)
        names = {d["name"] for d in doc["deployments"]}
        self.assertEqual(names, {"gg-oracle-payments-01", "gg-postgresql-payments-01"})
        for d in doc["deployments"]:
            self.assertTrue(d["enabled"])
            self.assertEqual(d["adminHost"], f"{d['name']}.goldengate-dev.svc.cluster.local")
            self.assertEqual(d["tlsServerName"], f"{d['name']}.goldengate-dev.adcbmis.local")
            self.assertEqual(d["adminPort"], 8443)
            self.assertEqual(d["metricsPort"], 9015)

    def test_derivation_uses_no_prefix_manipulation(self):
        """deployment.name IS the DynamoDB partition key -- no gg- prefix
        is prepended or stripped anywhere in the loader."""
        with tempfile.TemporaryDirectory() as tmp:
            write_deployments_yaml(tmp, VALID_DOC)
            doc = cfgmod.load_deployments(tmp)
        for d in doc["deployments"]:
            self.assertTrue(d["name"].startswith("gg-"))

    def test_missing_required_top_level_key_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_deployments_yaml(tmp, "environment: dev\ndeployments: []\n")
            with self.assertRaises(cfgmod.ConfigError):
                cfgmod.load_deployments(tmp)

    def test_empty_deployments_list_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_deployments_yaml(tmp, """
environment: dev
runtimeNamespace: goldengate-dev
monitoringNamespace: goldengate-monitoring
dnsDomain: goldengate-dev.adcbmis.local
deployments: []
""")
            with self.assertRaises(cfgmod.ConfigError):
                cfgmod.load_deployments(tmp)

    def test_duplicate_name_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_deployments_yaml(tmp, """
environment: dev
runtimeNamespace: goldengate-dev
monitoringNamespace: goldengate-monitoring
dnsDomain: goldengate-dev.adcbmis.local
deployments:
  - name: gg-dup
    type: oracle
    pipeline: p1
    role: source
  - name: gg-dup
    type: oracle
    pipeline: p1
    role: target
""")
            with self.assertRaises(cfgmod.ConfigError):
                cfgmod.load_deployments(tmp)

    def test_invalid_role_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_deployments_yaml(tmp, """
environment: dev
runtimeNamespace: goldengate-dev
monitoringNamespace: goldengate-monitoring
dnsDomain: goldengate-dev.adcbmis.local
deployments:
  - name: gg-x
    type: oracle
    pipeline: p1
    role: middleman
""")
            with self.assertRaises(cfgmod.ConfigError):
                cfgmod.load_deployments(tmp)

    def test_port_override_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_deployments_yaml(tmp, """
environment: dev
runtimeNamespace: goldengate-dev
monitoringNamespace: goldengate-monitoring
dnsDomain: goldengate-dev.adcbmis.local
deployments:
  - name: gg-x
    type: oracle
    pipeline: p1
    role: source
    adminPort: 9443
    metricsPort: 9999
""")
            doc = cfgmod.load_deployments(tmp)
        self.assertEqual(doc["deployments"][0]["adminPort"], 9443)
        self.assertEqual(doc["deployments"][0]["metricsPort"], 9999)


class BuildLogicalPipelinesTests(unittest.TestCase):
    def test_groups_by_pipeline_and_role(self):
        deployments = [
            {"name": "gg-oracle-payments-01", "pipeline": "payments-ora-to-pg-001", "role": "source"},
            {"name": "gg-postgresql-payments-01", "pipeline": "payments-ora-to-pg-001", "role": "target"},
        ]
        lps = cfgmod.build_logical_pipelines(deployments)
        self.assertEqual(len(lps), 1)
        self.assertEqual(lps[0]["pipelineId"], "payments-ora-to-pg-001")
        self.assertEqual(lps[0]["roles"]["source"], "gg-oracle-payments-01")
        self.assertEqual(lps[0]["roles"]["target"], "gg-postgresql-payments-01")

    def test_conflicting_role_assignment_rejected(self):
        deployments = [
            {"name": "gg-a", "pipeline": "p1", "role": "source"},
            {"name": "gg-b", "pipeline": "p1", "role": "source"},
        ]
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.build_logical_pipelines(deployments)


class ValidateEnabledDeploymentsTests(unittest.TestCase):
    def test_enabled_deployment_missing_admin_secret_fails(self):
        deployments = [{"name": "gg-x", "type": "oracle", "enabled": True, "adminSecret": ""}]
        with self.assertRaises(cfgmod.StartupValidationError):
            cfgmod.validate_enabled_deployments(deployments)

    def test_disabled_deployment_not_validated(self):
        deployments = [{"name": "gg-x", "type": "unsupported", "enabled": False, "adminSecret": ""}]
        cfgmod.validate_enabled_deployments(deployments)  # must not raise

    def test_unsupported_type_rejected(self):
        deployments = [{"name": "gg-x", "type": "sqlserver", "enabled": True, "adminSecret": "s"}]
        with self.assertRaises(cfgmod.StartupValidationError):
            cfgmod.validate_enabled_deployments(deployments)

    def test_real_repo_config_validates_cleanly(self):
        doc = cfgmod.load_deployments(os.path.join(REPO_ROOT, "envs", "dev"))
        cfgmod.validate_enabled_deployments(doc["deployments"])  # must not raise


class MonitorConfigLoadTests(unittest.TestCase):
    def test_valid_configuration(self):
        config = cfgmod.load_config({"AWS_REGION": "eu-west-1", "DYNAMODB_TABLE": "gg-eks-pipeline"})
        self.assertEqual(config.aws_region, "eu-west-1")
        self.assertEqual(config.port, 8080)
        self.assertTrue(config.legacy_fallback_enabled)

    def test_missing_required_env_fails(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.load_config({"AWS_REGION": "eu-west-1"})

    def test_legacy_fallback_disable(self):
        config = cfgmod.load_config({"AWS_REGION": "eu-west-1", "DYNAMODB_TABLE": "t",
                                     "LEGACY_FALLBACK_ENABLED": "false"})
        self.assertFalse(config.legacy_fallback_enabled)


class CredentialPathsTests(unittest.TestCase):
    def test_paths_derived_from_name_only(self):
        user_file, pwd_file = cfgmod.credential_paths("gg-oracle-payments-01")
        self.assertEqual(user_file, "/mnt/secrets-store/gg-oracle-payments-01-admin-user")
        self.assertEqual(pwd_file, "/mnt/secrets-store/gg-oracle-payments-01-admin-password")


if __name__ == "__main__":
    unittest.main()
