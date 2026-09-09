"""Offline tests for automation/phases/phase6/phase6_final.py; run directly via `python3 automation/phases/phase6/tests/test_phase6_final.py`. Pure-function tests only -- validate_gate() takes a plain str->str mapping and returns (ok, log), so this suite never touches a subprocess, the filesystem, or GitHub Actions itself. Covers the complete mode-aware truth table this module moved out of .github/workflows/00-main-goldengate-orchestrator.yaml's inline shell: literal-boolean-only handling, require_success()/allow_non_failure() semantics, and every effective_deploy/has_active_deployments/has_changes/has_deletions branch combination. Retired automated replication results are excluded from the final gate; absence checks preserve that contract."""
from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
TOOL_PATH = REPO_ROOT / "automation" / "phases" / "phase6" / "phase6_final.py"


def _load_tool(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


phase6_final = _load_tool(TOOL_PATH, "phase6_final")


def _all_success_env(**overrides):
    env = {f"RESULT_{name}": "success" for name in phase6_final.RESULT_JOB_NAMES}
    env.update(overrides)
    return env


def _deploy_active_env(**overrides):
    env = _all_success_env(EFFECTIVE_DEPLOY="true", HAS_ACTIVE_DEPLOYMENTS="true", HAS_CHANGES="true", HAS_DELETIONS="true")
    env.update(overrides)
    return env


def _validate_env(has_active, **overrides):
    env = _all_success_env(EFFECTIVE_DEPLOY="false", HAS_ACTIVE_DEPLOYMENTS=has_active, HAS_CHANGES="false", HAS_DELETIONS="false")
    for job_name in ("terraform_sync_once", "validate_argocd_ready", "validate_platform_ready", "validate_observability_ready",
                      "validate_active_runtimes", "monitor_ownership_preflight",
                      "monitor_sync_once", "validate_monitor_ready",
                      "end_to_end_deployment_acceptance", "runtime_ownership_preflight", "build_publish_and_deploy",
                      "delete_removed_argocd_applications"):
        env[f"RESULT_{job_name}"] = "skipped"
    if has_active == "false":
        env["RESULT_monitor_dry_run_validation"] = "skipped"
    env.update(overrides)
    return env


class LiteralBooleanTests(unittest.TestCase):
    def test_all_invalid_literal_booleans_fail(self):
        for bad in ("", "True", "False", "yes", "no", "1", "0", "null", "TRUE", "arbitrary-text"):
            with self.subTest(bad=bad):
                ok, log = phase6_final.validate_gate(_deploy_active_env(EFFECTIVE_DEPLOY=bad))
                self.assertFalse(ok)
                self.assertTrue(any("effective_deploy" in line and "expected literal" in line for line in log))

    def test_has_active_deployments_invalid_fails(self):
        ok, log = phase6_final.validate_gate(_deploy_active_env(HAS_ACTIVE_DEPLOYMENTS="maybe"))
        self.assertFalse(ok)

    def test_has_changes_invalid_fails(self):
        ok, log = phase6_final.validate_gate(_deploy_active_env(HAS_CHANGES="2"))
        self.assertFalse(ok)

    def test_has_deletions_invalid_fails(self):
        ok, log = phase6_final.validate_gate(_deploy_active_env(HAS_DELETIONS="nope"))
        self.assertFalse(ok)

    def test_no_truthiness_coercion_arbitrary_truthy_string_still_fails(self):
        # A naive `bool(value)` would treat any non-empty string (including "false"-looking garbage) as True -- this must be rejected outright instead.
        ok, log = phase6_final.validate_gate(_deploy_active_env(EFFECTIVE_DEPLOY="definitely-not-a-boolean"))
        self.assertFalse(ok)


class ValidateModelGateTests(unittest.TestCase):
    def test_validate_model_skipped_fails(self):
        ok, log = phase6_final.validate_gate(_deploy_active_env(RESULT_validate_model="skipped"))
        self.assertFalse(ok)
        self.assertTrue(any("validate_model" in line and "skipped" in line for line in log))

    def test_validate_model_failed_fails(self):
        ok, log = phase6_final.validate_gate(_deploy_active_env(RESULT_validate_model="failure"))
        self.assertFalse(ok)

    def test_validate_model_cancelled_fails(self):
        ok, log = phase6_final.validate_gate(_deploy_active_env(RESULT_validate_model="cancelled"))
        self.assertFalse(ok)

    def test_shared_secrets_skipped_fails(self):
        ok, log = phase6_final.validate_gate(_deploy_active_env(RESULT_validate_shared_secrets_once="skipped"))
        self.assertFalse(ok)


class DeployActiveTests(unittest.TestCase):
    def test_every_required_gate_succeeding_passes(self):
        ok, log = phase6_final.validate_gate(_deploy_active_env())
        self.assertTrue(ok, log)

    def test_each_individual_required_gate_skipped_fails(self):
        # Automated Replication Implementation Removal: replication_reconcile_once/replication_monitor_acceptance are removed outright -- no automated replication result participates in this required set any longer.
        required_in_deploy_active = (
            "validate_shared_secrets_once", "build_publish_and_deploy", "runtime_ownership_preflight",
            "delete_removed_argocd_applications", "terraform_sync_once", "validate_argocd_ready",
            "validate_platform_ready", "validate_observability_ready", "validate_active_runtimes",
            "monitor_ownership_preflight", "monitor_sync_once",
            "validate_monitor_ready", "end_to_end_deployment_acceptance",
        )
        for job_name in required_in_deploy_active:
            with self.subTest(job=job_name):
                ok, log = phase6_final.validate_gate(_deploy_active_env(**{f"RESULT_{job_name}": "skipped"}))
                self.assertFalse(ok, f"{job_name}=skipped should fail the gate")

    def test_each_individual_required_gate_failure_fails(self):
        ok, log = phase6_final.validate_gate(_deploy_active_env(RESULT_end_to_end_deployment_acceptance="failure"))
        self.assertFalse(ok)

    def test_each_individual_required_gate_cancelled_fails(self):
        ok, log = phase6_final.validate_gate(_deploy_active_env(RESULT_monitor_sync_once="cancelled"))
        self.assertFalse(ok)


class DeployNoActiveTests(unittest.TestCase):
    def test_legitimate_skips_allowed(self):
        env = _deploy_active_env(HAS_ACTIVE_DEPLOYMENTS="false")
        for job_name in ("validate_active_runtimes", "monitor_ownership_preflight",
                          "monitor_sync_once", "validate_monitor_ready",
                          "end_to_end_deployment_acceptance"):
            env[f"RESULT_{job_name}"] = "skipped"
        ok, log = phase6_final.validate_gate(env)
        self.assertTrue(ok, log)

    def test_real_failure_still_blocks_even_when_legitimately_optional(self):
        env = _deploy_active_env(HAS_ACTIVE_DEPLOYMENTS="false")
        env["RESULT_monitor_sync_once"] = "failure"
        ok, log = phase6_final.validate_gate(env)
        self.assertFalse(ok)

    def test_real_cancellation_still_blocks_even_when_legitimately_optional(self):
        env = _deploy_active_env(HAS_ACTIVE_DEPLOYMENTS="false")
        env["RESULT_validate_monitor_ready"] = "cancelled"
        ok, log = phase6_final.validate_gate(env)
        self.assertFalse(ok)


class ValidateActiveTests(unittest.TestCase):
    def test_monitor_dry_run_validation_success_required(self):
        env = _validate_env("true")
        ok, log = phase6_final.validate_gate(env)
        self.assertTrue(ok, log)

    def test_monitor_dry_run_validation_skipped_fails(self):
        env = _validate_env("true", RESULT_monitor_dry_run_validation="skipped")
        ok, log = phase6_final.validate_gate(env)
        self.assertFalse(ok)


class ValidateNoActiveTests(unittest.TestCase):
    def test_monitor_dry_run_validation_skipped_allowed(self):
        env = _validate_env("false")
        ok, log = phase6_final.validate_gate(env)
        self.assertTrue(ok, log)

    def test_validate_mode_no_longer_requires_a_removed_replication_dry_run_result(self):
        # Automated Replication Implementation Removal: VALIDATE mode must no longer require replication_dry_run_validation (it no longer exists) -- proves an otherwise-fully-passing VALIDATE + no-active-runtime env still passes with zero replication-result keys present at all.
        env = _validate_env("false")
        self.assertFalse(any(k.startswith("RESULT_replication") for k in env))
        ok, log = phase6_final.validate_gate(env)
        self.assertTrue(ok, log)


class HasChangesTests(unittest.TestCase):
    def test_has_changes_true_requires_build_publish_and_deploy(self):
        env = _deploy_active_env(HAS_CHANGES="true", RESULT_build_publish_and_deploy="skipped")
        ok, log = phase6_final.validate_gate(env)
        self.assertFalse(ok)

    def test_has_changes_false_allows_build_publish_and_deploy_skip(self):
        env = _deploy_active_env(HAS_CHANGES="false")
        env["RESULT_build_publish_and_deploy"] = "skipped"
        env["RESULT_runtime_ownership_preflight"] = "skipped"
        ok, log = phase6_final.validate_gate(env)
        self.assertTrue(ok, log)

    def test_has_changes_true_effective_deploy_false_allows_runtime_ownership_preflight_skip(self):
        env = _validate_env("true", HAS_CHANGES="true")
        env["RESULT_build_publish_and_deploy"] = "success"
        env["RESULT_runtime_ownership_preflight"] = "skipped"
        ok, log = phase6_final.validate_gate(env)
        self.assertTrue(ok, log)

    def test_has_changes_true_effective_deploy_true_requires_runtime_ownership_preflight(self):
        env = _deploy_active_env(RESULT_runtime_ownership_preflight="skipped")
        ok, log = phase6_final.validate_gate(env)
        self.assertFalse(ok)


class HasDeletionsTests(unittest.TestCase):
    def test_has_deletions_true_requires_deletion_job(self):
        env = _deploy_active_env(RESULT_delete_removed_argocd_applications="skipped")
        ok, log = phase6_final.validate_gate(env)
        self.assertFalse(ok)

    def test_has_deletions_false_allows_deletion_skip(self):
        env = _deploy_active_env(HAS_DELETIONS="false")
        env["RESULT_delete_removed_argocd_applications"] = "skipped"
        ok, log = phase6_final.validate_gate(env)
        self.assertTrue(ok, log)

    def test_has_deletions_false_real_failure_still_blocks(self):
        env = _deploy_active_env(HAS_DELETIONS="false")
        env["RESULT_delete_removed_argocd_applications"] = "failure"
        ok, log = phase6_final.validate_gate(env)
        self.assertFalse(ok)


class AllowNonFailurePathTests(unittest.TestCase):
    def test_any_failure_in_allow_non_failure_path_fails(self):
        env = _validate_env("false")
        env["RESULT_terraform_sync_once"] = "failure"
        ok, log = phase6_final.validate_gate(env)
        self.assertFalse(ok)

    def test_any_cancelled_in_allow_non_failure_path_fails(self):
        env = _validate_env("false")
        env["RESULT_validate_argocd_ready"] = "cancelled"
        ok, log = phase6_final.validate_gate(env)
        self.assertFalse(ok)

    def test_skipped_in_allow_non_failure_path_is_fine(self):
        env = _validate_env("false")
        env["RESULT_terraform_sync_once"] = "skipped"
        ok, log = phase6_final.validate_gate(env)
        self.assertTrue(ok, log)

    def test_success_in_allow_non_failure_path_is_fine(self):
        env = _validate_env("false")
        env["RESULT_terraform_sync_once"] = "success"
        ok, log = phase6_final.validate_gate(env)
        self.assertTrue(ok, log)


class ResultJobNamesTests(unittest.TestCase):
    def test_no_automated_replication_result_job_names_remain(self):
        # Automated Replication Implementation Removal: replication_reconcile_once/replication_dry_run_validation/replication_monitor_acceptance must not appear in RESULT_JOB_NAMES at all.
        for retired in ("replication_reconcile_once", "replication_dry_run_validation", "replication_monitor_acceptance"):
            self.assertNotIn(retired, phase6_final.RESULT_JOB_NAMES)


class CmdValidateTests(unittest.TestCase):
    def test_cmd_validate_returns_zero_on_success(self):
        self.assertEqual(phase6_final.cmd_validate(_deploy_active_env()), 0)

    def test_cmd_validate_returns_one_on_failure(self):
        self.assertEqual(phase6_final.cmd_validate(_deploy_active_env(RESULT_validate_model="skipped")), 1)

    def test_main_dispatches_validate(self):
        env_backup = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(_deploy_active_env())
            self.assertEqual(phase6_final.main(["validate"]), 0)
        finally:
            os.environ.clear()
            os.environ.update(env_backup)

    def test_main_rejects_unknown_command(self):
        self.assertEqual(phase6_final.main(["bogus"]), 1)


if __name__ == "__main__":
    unittest.main()
