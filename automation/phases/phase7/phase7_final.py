#!/usr/bin/env python3
"""Phase 7G | GoldenGate MAIN final_validation entrypoint -- moves the "Validate the mode-aware final DEPLOY success contract" step's implementation out of .github/workflows/00-main-goldengate-orchestrator.yaml's inline shell into pure, testable Python. This module is deliberately unaware of GitHub Actions' own double-curly template-expression syntax: the calling workflow step maps `needs.<job>.result` and validate_model's boolean outputs into plain environment variables (EFFECTIVE_DEPLOY, HAS_ACTIVE_DEPLOYMENTS, HAS_CHANGES, HAS_DELETIONS, and one RESULT_<job_name> per prerequisite) exactly as it already does today -- this module only ever reads plain strings from os.environ (or, for tests, an explicit mapping), never a template expression. Preserves the EXACT mode-aware require_success()/allow_non_failure() contract, literal-boolean-only handling (no truthiness coercion), and per-mode branching the prior inline shell implemented -- semantic parity only, not a redesign."""
from __future__ import annotations

import os
import sys

# The exact prerequisite job-result environment variable names the calling workflow step maps in via its own `env:` block -- kept as an explicit list (not inferred) so a missing mapping fails loudly via require_success()/allow_non_failure() rather than silently defaulting to an empty string result.
RESULT_JOB_NAMES = (
    "validate_model",
    "terraform_sync_once",
    "validate_shared_secrets_once",
    "validate_argocd_ready",
    "validate_platform_ready",
    "validate_observability_ready",
    "runtime_ownership_preflight",
    "build_publish_and_deploy",
    "delete_removed_argocd_applications",
    "validate_active_runtimes",
    "replication_reconcile_once",
    "replication_dry_run_validation",
    "monitor_ownership_preflight",
    "monitor_sync_once",
    "monitor_dry_run_validation",
    "validate_monitor_ready",
    "replication_monitor_acceptance",
    "end_to_end_deployment_acceptance",
)

_LITERAL_BOOLEAN_INPUTS = ("EFFECTIVE_DEPLOY", "HAS_ACTIVE_DEPLOYMENTS", "HAS_CHANGES", "HAS_DELETIONS")


class Phase7FinalError(Exception):
    """A fail-closed Phase 7 final-validation error; main() reports it and exits non-zero."""


class _Gate:
    """Accumulates OK/FAIL diagnostic lines and a single failed flag exactly like the prior inline shell's FAILED="true"/require_success()/allow_non_failure() functions -- a thin, directly-testable stand-in for that shell state."""

    def __init__(self, env):
        self.env = env
        self.log = []
        self.failed = False

    def emit(self, line):
        self.log.append(line)

    def result_of(self, job_name):
        return self.env.get(f"RESULT_{job_name}", "")

    def require_success(self, job_name):
        result = self.result_of(job_name)
        if result != "success":
            self.emit(f"FAIL: required job '{job_name}' has result '{result}', expected 'success'. A SKIPPED (or failed/cancelled) REQUIRED gate must prevent MAIN from claiming deployment success.")
            self.failed = True
        else:
            self.emit(f"OK: required job '{job_name}' succeeded.")

    def allow_non_failure(self, job_name):
        result = self.result_of(job_name)
        if result in ("failure", "cancelled"):
            self.emit(f"FAIL: job '{job_name}' has result '{result}' -- this job is not required to run in the current resolved mode, but a real failure/cancellation must still block MAIN.")
            self.failed = True
        else:
            self.emit(f"OK: job '{job_name}' result '{result}' is acceptable in the current resolved mode (not a real failure/cancellation).")


def _require_literal_boolean(gate, name, value):
    """Literal 'true'/'false' strings ONLY -- empty/null/True-False-object-reprs/yes-no/1-0/arbitrary text are all rejected here (never coerced via Python truthiness: this is a strict string-identity check, not `bool(value)`)."""
    if value not in ("true", "false"):
        gate.emit(f"FAIL: {name} is {value!r}, expected literal 'true' or 'false'.")
        return None
    return value == "true"


def validate_gate(env):
    """Pure function: env is a plain str->str mapping (os.environ or a test-supplied dict). Returns (ok: bool, log_lines: list[str]) -- never raises; main() is the only place that turns a failed gate into a non-zero process exit. Exactly mirrors the prior inline shell step's branch structure and diagnostic wording."""
    gate = _Gate(env)

    effective_deploy_raw = env.get("EFFECTIVE_DEPLOY", "")
    has_active_raw = env.get("HAS_ACTIVE_DEPLOYMENTS", "")
    has_changes_raw = env.get("HAS_CHANGES", "")
    has_deletions_raw = env.get("HAS_DELETIONS", "")

    gate.emit(f"Resolved mode: effective_deploy={effective_deploy_raw} has_active_deployments={has_active_raw} has_changes={has_changes_raw} has_deletions={has_deletions_raw}")

    # Phase 1 (validate_model) owns the ENTIRE mode-aware EKS/OIDC/Kubernetes-API prerequisite, local deployment/storage-safety, and AWS-side managed-EFS inventory contract internally -- a SKIPPED/failed/cancelled Phase 1 result is reported here directly, before any of its own canonical output values are trusted.
    gate.require_success("validate_model")
    if gate.failed:
        gate.emit("")
        gate.emit("FAIL: Phase 1 | Validate Folder-Driven Deployment Model did not succeed -- see its own job diagnostics above. MAIN cannot claim deployment success.")
        return False, gate.log

    effective_deploy = _require_literal_boolean(gate, "validate_model.outputs.effective_deploy", effective_deploy_raw)
    if effective_deploy is None:
        return False, gate.log
    has_active_deployments = _require_literal_boolean(gate, "validate_model.outputs.has_active_deployments", has_active_raw)
    if has_active_deployments is None:
        return False, gate.log
    has_changes = _require_literal_boolean(gate, "validate_model.outputs.has_changes", has_changes_raw)
    if has_changes is None:
        return False, gate.log
    has_deletions = _require_literal_boolean(gate, "validate_model.outputs.has_deletions", has_deletions_raw)
    if has_deletions is None:
        return False, gate.log

    # Foundational -- required in every mode.
    gate.require_success("validate_shared_secrets_once")

    # Mutation-aware contract: applicability of the selected-deployment build/reconcile, ownership preflight, and deletion stages is already known from validate_model's own outputs -- never inferred only from other jobs' transitive downstream behavior. has_changes/has_deletions are orthogonal to the effective_deploy/has_active_deployments mode contract below.
    if has_changes:
        gate.emit("SELECTED MUTATION: has_changes=true -- the selected-deployment build/reconcile stage is unconditionally required (real deploy: runtime reconciliation; deploy=false: Helm lint/render validation).")
        gate.require_success("build_publish_and_deploy")
        if effective_deploy:
            gate.require_success("runtime_ownership_preflight")
        else:
            # runtime_ownership_preflight's own if: never runs it on deploy=false regardless of has_changes -- a real deploy=false Helm lint/render run legitimately skips it.
            gate.allow_non_failure("runtime_ownership_preflight")
    else:
        gate.emit("NO SELECTED MUTATION: has_changes=false -- the selected-deployment build/reconcile and ownership preflight stages are legitimately not applicable and may be cleanly skipped, but a real failure/cancellation still blocks MAIN.")
        gate.allow_non_failure("runtime_ownership_preflight")
        gate.allow_non_failure("build_publish_and_deploy")

    if has_deletions:
        gate.emit("SELECTED DELETION: has_deletions=true -- the removed-Application deletion stage is unconditionally required.")
        gate.require_success("delete_removed_argocd_applications")
    else:
        gate.allow_non_failure("delete_removed_argocd_applications")

    if effective_deploy:
        gate.emit("REAL DEPLOY: Terraform, Argo CD / Platform / Observability readiness are unconditionally required.")
        gate.require_success("terraform_sync_once")
        gate.require_success("validate_argocd_ready")
        gate.require_success("validate_platform_ready")
        gate.require_success("validate_observability_ready")

        if has_active_deployments:
            gate.emit("REAL DEPLOY + ACTIVE RUNTIMES: the full runtime/replication/monitor/E2E acceptance chain is unconditionally required -- a SKIPPED value for any of these must fail this gate, not merely pass through as 'not a failure'.")
            gate.require_success("validate_active_runtimes")
            gate.require_success("replication_reconcile_once")
            gate.require_success("monitor_ownership_preflight")
            gate.require_success("monitor_sync_once")
            gate.require_success("validate_monitor_ready")
            gate.require_success("replication_monitor_acceptance")
            gate.require_success("end_to_end_deployment_acceptance")
        else:
            gate.emit("REAL DEPLOY + NO ACTIVE RUNTIMES: the runtime/replication/monitor/E2E acceptance chain is legitimately not applicable and may be cleanly skipped -- only a real failure/cancellation blocks MAIN.")
            gate.allow_non_failure("validate_active_runtimes")
            gate.allow_non_failure("replication_reconcile_once")
            gate.allow_non_failure("monitor_ownership_preflight")
            gate.allow_non_failure("monitor_sync_once")
            gate.allow_non_failure("validate_monitor_ready")
            gate.allow_non_failure("replication_monitor_acceptance")
            gate.allow_non_failure("end_to_end_deployment_acceptance")
    else:
        gate.emit("DRY RUN: live EKS/OIDC/Kubernetes-API prerequisite, Terraform, live EFS inventory, and Argo CD / Platform / Observability / runtime / monitor deployment gates are legitimately skipped -- the existing dry-run validation path governs success instead.")
        gate.allow_non_failure("terraform_sync_once")
        gate.allow_non_failure("validate_argocd_ready")
        gate.allow_non_failure("validate_platform_ready")
        gate.allow_non_failure("validate_observability_ready")
        gate.allow_non_failure("validate_active_runtimes")
        gate.allow_non_failure("replication_reconcile_once")
        gate.allow_non_failure("monitor_ownership_preflight")
        gate.allow_non_failure("monitor_sync_once")
        gate.allow_non_failure("validate_monitor_ready")
        gate.allow_non_failure("replication_monitor_acceptance")
        gate.allow_non_failure("end_to_end_deployment_acceptance")

        # Replication dry-run validates the global replication schema/pipeline model and must succeed even with zero active runtimes -- unconditionally required for every dry run.
        gate.require_success("replication_dry_run_validation")

        if has_active_deployments:
            gate.emit("DRY RUN + ACTIVE RUNTIMES: monitor_dry_run_validation's own if: condition makes it applicable -- a SKIPPED value here must fail this gate, not merely pass through as 'not a failure'.")
            gate.require_success("monitor_dry_run_validation")
        else:
            # monitor_dry_run_validation's own if: condition never runs it against a deliberately empty active-runtime registry -- a real failure/cancellation (should it somehow still run) must still block MAIN.
            gate.emit("DRY RUN + NO ACTIVE RUNTIMES: monitor_dry_run_validation is legitimately not applicable and may be cleanly skipped.")
            gate.allow_non_failure("monitor_dry_run_validation")

    if gate.failed:
        gate.emit("")
        gate.emit("FAIL: the mode-aware final DEPLOY success contract was not satisfied -- see diagnostics above. MAIN cannot claim deployment success.")
        return False, gate.log

    gate.emit("")
    gate.emit(f"OK: every REQUIRED gate for the resolved mode (effective_deploy={effective_deploy_raw}, has_active_deployments={has_active_raw}) succeeded.")
    return True, gate.log


def cmd_validate(env=None):
    ok, log = validate_gate(env if env is not None else os.environ)
    for line in log:
        print(line)
    return 0 if ok else 1


def main(argv=None):
    parser_args = argv if argv is not None else sys.argv[1:]
    if parser_args and parser_args[0] != "validate":
        print(f"FAIL: unknown command {parser_args[0]!r}; the only supported command is 'validate'.")
        return 1
    return cmd_validate()


if __name__ == "__main__":
    sys.exit(main())
