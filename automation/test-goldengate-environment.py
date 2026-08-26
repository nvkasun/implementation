"""Offline tests for automation/goldengate-environment.py (IAM generated-policy source-of-truth correction); run directly via `python3 automation/test-goldengate-environment.py`."""
from __future__ import annotations

import importlib.util
import inspect
import json
import os
import tempfile
import unittest

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL_PATH = os.path.join(REPO_ROOT, "automation", "goldengate-environment.py")


def _load_tool():
    spec = importlib.util.spec_from_file_location("goldengate_environment", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ge = _load_tool()


def _synthetic_doc(environment, cluster_name):
    """A fully valid, entirely synthetic environment.yaml document -- fixed fictitious account IDs/region/OIDC ID never resembling the real DEV environment, so these tests can never be confused with production configuration."""
    return {
        "schemaVersion": 1,
        "environment": environment,
        "aws": {"region": "eu-west-1", "workloadAccountId": "111111111111", "buildAccountId": "222222222222"},
        "eks": {
            "clusterName": cluster_name,
            "oidcIssuer": "https://oidc.eks.eu-west-1.amazonaws.com/id/0123456789ABCDEF0123456789ABCDEF",
        },
        "namespaces": {
            "runtime": f"goldengate-{environment}", "monitoring": "goldengate-monitoring",
            "argocd": "argocd", "observability": "amazon-cloudwatch",
        },
        "network": {
            "dnsDomain": f"goldengate-{environment}.example.local",
            "albGroupName": "gg-scratch-alb",
            "certificateArn": "arn:aws:acm:eu-west-1:111111111111:certificate/00000000-0000-0000-0000-000000000000",
        },
        "iam": {
            "roles": {
                "eksDeploy": f"GoldenGateEKSDeployRole-{environment}",
                "runtime": f"GoldenGateSecretsReadRole-{environment}",
                "monitor": f"GoldenGateMonitorReadRole-{environment}",
                "argocdEcrRead": f"GoldenGateArgocdECRRead-{environment}",
                "platformLogging": f"GoldenGatePlatformLoggingRole-{environment}",
                "cloudwatchMetrics": f"GoldenGateCloudWatchMetricsRole-{environment}",
            },
            "runnerRoleName": f"RunnerRole-goldengate-eks-app_{environment}",
            "ecrSyncRoleArn": "arn:aws:iam::222222222222:role/scratch-test-ecr-sync-role",
        },
        "kms": {"monitorDynamoDbKeyArn": "arn:aws:kms:eu-west-1:111111111111:key/00000000-0000-0000-0000-000000000000"},
        "efs": {"sharedSecurityGroupDescription": "Security group for EFS filesystem - scratch test"},
        "tags": {
            "applicationName": "CloudFactory", "businessCriticality": "Low", "businessUnit": "TechnologyPlatform",
            "businessUnitOwner": "scratch-test-owner", "costCenter": "000", "mapMigrated": "scratch-test",
            "requestReference": "SCRATCH-TEST", "dataClassification": "General",
        },
    }


class NoGeneratedFileAsTemplateTests(unittest.TestCase):
    """generate_policy_files() must be a pure function of environment.yaml -- it must never open a generated policies_1.json/sts.json as input."""

    def test_generate_policy_files_never_reads_from_disk(self):
        # No envs/<environment>/policies/** directory exists under this scratch REPO_ROOT -- a template read would raise FileNotFoundError.
        with tempfile.TemporaryDirectory() as tmp:
            orig_repo_root = ge.REPO_ROOT
            ge.REPO_ROOT = tmp
            try:
                doc = _synthetic_doc("scratchenv", "synthetic-cluster-x")
                generated = ge.generate_policy_files(doc)
            finally:
                ge.REPO_ROOT = orig_repo_root
        self.assertEqual(len(generated), 12, "expected exactly 6 assume_role_policy/sts.json + 6 policies/policies_1.json")
        policy_paths = [rel for rel in generated if rel.endswith("policies_1.json")]
        self.assertEqual(len(policy_paths), 6)
        sts_paths = [rel for rel in generated if rel.endswith("sts.json")]
        self.assertEqual(len(sts_paths), 6)

    def test_generator_source_never_opens_a_file(self):
        """Static proof, not just behavioral: generate_policy_files() and every permission-policy builder must contain no file-open call at all -- they are pure functions of derive_values(doc)."""
        for fn in (ge.generate_policy_files, *ge._PERMISSION_POLICY_BUILDERS.values()):
            source = inspect.getsource(fn)
            self.assertNotIn("open(", source, f"{fn.__name__} must never open a file")

    def test_origin_substitution_design_is_fully_removed(self):
        for name in (
            "_ORIGIN_CLUSTER_NAME", "_ORIGIN_REGION", "_ORIGIN_WORKLOAD_ACCOUNT_ID",
            "_ORIGIN_ECR_ACCOUNT_ID", "_ORIGIN_KMS_KEY_ID", "_ORIGIN_SECRET_PATH_PREFIX",
            "_ORIGIN_LOG_GROUP_PREFIX", "_substitute_arns",
        ):
            self.assertFalse(hasattr(ge, name), f"{name} must be fully removed, not just unused")


class ConsecutiveEnvironmentChangeRegressionTests(unittest.TestCase):
    """Proves the exact bug scenario from the task is gone: environment.yaml changing A -> B -> C must never leave a stale prior value in the generated policy, and render_iam_policies(check) must never falsely report sync against stale committed output."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_repo_root = ge.REPO_ROOT
        ge.REPO_ROOT = self._tmp.name
        self.addCleanup(self._restore)

    def _restore(self):
        ge.REPO_ROOT = self._orig_repo_root
        self._tmp.cleanup()

    def _write_environment_yaml(self, environment, cluster_name):
        path = os.path.join(self._tmp.name, "envs", environment, "environment.yaml")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            yaml.safe_dump(_synthetic_doc(environment, cluster_name), f)

    def _ensure_output_dirs_exist(self, environment):
        # render_iam_policies() writes files but does not create directories (pre-existing, unchanged here) -- precreate them for this scratch tree.
        doc = ge.load_environment_config(environment)
        for rel in ge.generate_policy_files(doc):
            abs_path = os.path.join(self._tmp.name, rel)
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)

    def _read_eks_deploy_policy_text(self, environment):
        path = os.path.join(
            self._tmp.name, "envs", environment, "policies",
            "goldengate-eks-deploy-dev", "policies", "policies_1.json")
        with open(path) as f:
            return f.read()

    def test_a_then_b_then_c_never_leak_a_previous_cluster_name(self):
        env = "synthtest-abc"

        self._write_environment_yaml(env, "synthetic-cluster-a")
        self._ensure_output_dirs_exist(env)
        ge.render_iam_policies(env, write=True)
        text_a = self._read_eks_deploy_policy_text(env)
        self.assertIn("cluster/synthetic-cluster-a", text_a)

        self._write_environment_yaml(env, "synthetic-cluster-b")
        mismatches_b = ge.render_iam_policies(env, write=True)
        rel = f"envs/{env}/policies/goldengate-eks-deploy-dev/policies/policies_1.json"
        self.assertIn(rel, mismatches_b, "changing clusterName must be detected as a mismatch requiring regeneration")
        text_b = self._read_eks_deploy_policy_text(env)
        self.assertIn("cluster/synthetic-cluster-b", text_b)
        self.assertNotIn("synthetic-cluster-a", text_b, "the bug: regeneration must not depend on (and thus cannot leak) the A-generated content")

        self._write_environment_yaml(env, "synthetic-cluster-c")
        mismatches_c = ge.render_iam_policies(env, write=True)
        self.assertIn(rel, mismatches_c)
        text_c = self._read_eks_deploy_policy_text(env)
        self.assertIn("cluster/synthetic-cluster-c", text_c)
        self.assertNotIn("synthetic-cluster-b", text_c, "the bug: regeneration must not depend on (and thus cannot leak) the B-generated content")
        self.assertNotIn("synthetic-cluster-a", text_c)

    def test_check_reports_out_of_sync_when_committed_output_is_stale_relative_to_canonical(self):
        """B is committed to disk; environment.yaml is then changed to C without --write. --check (write=False) must report a mismatch, never a false OK, and must never itself mutate the stale file."""
        env = "synthtest-stale-check"

        self._write_environment_yaml(env, "synthetic-cluster-b")
        self._ensure_output_dirs_exist(env)
        ge.render_iam_policies(env, write=True)

        self._write_environment_yaml(env, "synthetic-cluster-c")
        mismatches = ge.render_iam_policies(env, write=False)
        rel = f"envs/{env}/policies/goldengate-eks-deploy-dev/policies/policies_1.json"
        self.assertIn(rel, mismatches, "--check must report the stale B-generated file as out of sync now that environment.yaml says C -- it must never falsely report synchronized")

        text = self._read_eks_deploy_policy_text(env)
        self.assertIn("cluster/synthetic-cluster-b", text, "write=False (--check) must never mutate the file on disk")
        self.assertNotIn("cluster/synthetic-cluster-c", text)


class CurrentDevEnvironmentSemanticEquivalenceTests(unittest.TestCase):
    """Proves the real committed envs/dev/policies/**/policies/policies_1.json content is semantically unchanged by this correction: identical Version/Statement count/Sid/Effect/Action/Resource/Condition for all six policies."""

    def test_all_six_generated_policies_match_committed_content_exactly(self):
        doc = ge.load_environment_config("dev")
        generated = ge.generate_policy_files(doc)
        policy_paths = sorted(rel for rel in generated if rel.endswith("policies_1.json"))
        self.assertEqual(len(policy_paths), 6)
        for rel in policy_paths:
            abs_path = os.path.join(REPO_ROOT, rel)
            with open(abs_path) as f:
                committed = json.load(f)
            self.assertEqual(generated[rel]["Version"], committed["Version"], f"{rel}: Version mismatch")
            self.assertEqual(len(generated[rel]["Statement"]), len(committed["Statement"]), f"{rel}: Statement count mismatch")
            self.assertEqual(generated[rel], committed, f"{rel}: full semantic mismatch (Sid/Effect/Action/Resource/Condition)")

    def test_render_iam_policies_check_passes_for_the_real_committed_dev_output(self):
        mismatches = ge.render_iam_policies("dev", write=False)
        self.assertEqual(mismatches, [], "the real committed envs/dev policy files must already be in sync with envs/dev/environment.yaml")


if __name__ == "__main__":
    unittest.main(verbosity=2)
