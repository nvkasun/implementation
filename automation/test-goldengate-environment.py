"""Offline tests for automation/goldengate-environment.py (IAM generated-policy source-of-truth correction); run directly via `python3 automation/test-goldengate-environment.py`."""
from __future__ import annotations

import argparse
import contextlib
import copy
import importlib.util
import inspect
import io
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


class ArgoCdEcrOciReadPolicyLeastPrivilegeTests(unittest.TestCase):
    """helm/gg-monitor was a stale canonical repository entry (no operational helm/gg-monitor chart -- the current monitor chart is helm/goldengate-monitor); this class proves it was actually removed from the canonical generator, never merely from the generated file on disk."""

    def test_canonical_repository_list_is_exactly_the_current_four(self):
        self.assertEqual(
            [name for name, _sid in ge._ARGOCD_ECR_OCI_REPOSITORIES],
            ["helm/goldengate", "helm/goldengate-monitor", "helm/goldengate-platform", "helm/amazon-cloudwatch-observability"],
        )

    def test_stale_gg_monitor_repository_is_absent_from_the_canonical_generator(self):
        repo_names = [name for name, _sid in ge._ARGOCD_ECR_OCI_REPOSITORIES]
        self.assertNotIn("helm/gg-monitor", repo_names)

    def test_generated_argocd_ecr_read_policy_has_exactly_one_authorization_statement_and_four_repository_statements(self):
        doc = ge.load_environment_config("dev")
        v = ge.derive_values(doc)
        policy = ge._argocd_ecr_oci_read_policy(v)
        self.assertEqual(len(policy["Statement"]), 5)
        auth_statements = [s for s in policy["Statement"] if s["Action"] == ["ecr:GetAuthorizationToken"]]
        self.assertEqual(len(auth_statements), 1)
        self.assertEqual(auth_statements[0]["Resource"], "*")

    def test_get_authorization_token_action_is_never_removed_by_this_correction(self):
        doc = ge.load_environment_config("dev")
        v = ge.derive_values(doc)
        policy = ge._argocd_ecr_oci_read_policy(v)
        all_actions = {action for stmt in policy["Statement"] for action in (stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]])}
        self.assertIn("ecr:GetAuthorizationToken", all_actions)


class GithubEnvSerializerSecurityTests(unittest.TestCase):
    """automation/goldengate-environment.py's github-env command is a GitHub Actions special-file producer: format_github_env() is the trust boundary that must fail closed before any value reaches $GITHUB_ENV via a caller's `>> "$GITHUB_ENV"` redirection. Uses fabricated derived-value dicts and a deep-copied synthetic document -- never edits or reads the committed DEV environment.yaml for the attack cases."""

    def test_real_dev_github_env_output_remains_valid_and_deterministic(self):
        doc = ge.load_environment_config("dev")
        values = ge.derive_values(doc)
        lines_a = ge.format_github_env(values)
        lines_b = ge.format_github_env(values)
        self.assertEqual(lines_a, lines_b, "format_github_env must be deterministic for the same input")
        self.assertEqual(lines_a, sorted(lines_a), "lines must already be in sorted-by-key order")

    def test_all_emitted_names_match_the_safe_github_env_name_pattern(self):
        doc = ge.load_environment_config("dev")
        values = ge.derive_values(doc)
        for line in ge.format_github_env(values):
            name = line.split("=", 1)[0]
            self.assertRegex(name, r"^[A-Z_][A-Z0-9_]*\Z")

    def test_lf_injection_value_is_rejected(self):
        with self.assertRaises(ValueError):
            ge.format_github_env({"TAG_BUSINESS_UNIT_OWNER": "owner\nROGUE=injected"})

    def test_bare_cr_injection_value_is_rejected(self):
        with self.assertRaises(ValueError):
            ge.format_github_env({"EFS_SHARED_SECURITY_GROUP_DESCRIPTION": "EFS sg\rROGUE=injected"})

    def test_crlf_injection_value_is_rejected(self):
        with self.assertRaises(ValueError):
            ge.format_github_env({"ALB_GROUP_NAME": "group\r\nROGUE=injected"})

    def test_nul_containing_value_is_rejected(self):
        with self.assertRaises(ValueError):
            ge.format_github_env({"TAG_COST_CENTER": "cc\x00ROGUE=injected"})

    def test_unsafe_variable_name_is_rejected(self):
        with self.assertRaises(ValueError):
            ge.format_github_env({"not a safe name!": "value"})
        with self.assertRaises(ValueError):
            ge.format_github_env({"lower_case_name": "value"})
        with self.assertRaises(ValueError):
            ge.format_github_env({"": "value"})

    def _run_cmd_github_env_with_unsafe_tag(self):
        mutated = copy.deepcopy(_synthetic_doc("scratchenv-cr", "synthetic-cluster-cr"))
        mutated["tags"]["businessUnitOwner"] = "owner\nROGUE=injected"

        orig_load = ge.load_environment_config
        ge.load_environment_config = lambda environment: mutated
        try:
            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()
            with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
                rc = ge.cmd_github_env(argparse.Namespace(environment="scratchenv-cr"))
        finally:
            ge.load_environment_config = orig_load
        return rc, stdout_buf.getvalue(), stderr_buf.getvalue()

    def test_failure_emits_zero_stdout_github_env_records(self):
        rc, stdout_text, _stderr_text = self._run_cmd_github_env_with_unsafe_tag()
        self.assertNotEqual(rc, 0)
        self.assertEqual(stdout_text, "", "a single unsafe derived value must suppress ALL github-env stdout records, not just the offending one")

    def test_failure_diagnostic_does_not_include_the_malicious_value(self):
        _rc, _stdout_text, stderr_text = self._run_cmd_github_env_with_unsafe_tag()
        self.assertNotIn("owner", stderr_text)
        self.assertNotIn("ROGUE", stderr_text)
        self.assertIn("TAG_BUSINESS_UNIT_OWNER", stderr_text, "the diagnostic should still name which key failed")

    def test_serializer_validates_every_pair_before_emitting_anything(self):
        # A key that sorts BEFORE the unsafe key alphabetically must still never reach stdout.
        with self.assertRaises(ValueError):
            ge.format_github_env({"AAAA_SAFE_KEY": "safe-value", "ZZZZ_UNSAFE_KEY": "bad\nvalue"})

    def test_cmd_github_env_routes_every_value_through_the_one_shared_serializer(self):
        """Structural proof that a future/new derived value automatically inherits this protection: cmd_github_env must call format_github_env() and must not also contain its own independent print-per-key loop that could bypass it."""
        source = inspect.getsource(ge.cmd_github_env)
        self.assertIn("format_github_env(", source)
        self.assertNotIn('print(f"{key}', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
