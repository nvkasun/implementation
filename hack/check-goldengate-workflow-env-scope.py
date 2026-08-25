"""check-goldengate-workflow-env-scope.py: fails closed if any active GitHub Actions run: step references $GG_SELECTED_ENVIRONMENT / ${GG_SELECTED_ENVIRONMENT} without satisfying the repository-wide job-scope invariant -- a job-level env.GG_SELECTED_ENVIRONMENT binding from its canonical source, a step-level-only binding is never sufficient. PHASE-ORIENTED ORCHESTRATION: 01-phase-readiness-safety.yaml's validate_model job is the ONE canonical producer (checked instead for GITHUB_ENV persistence ordering); every other non-matrix job's canonical source is needs.validate_model.outputs.selected_environment when it lives alongside validate_model in 01-phase-readiness-safety.yaml itself, inputs.selected_environment when it lives in any other 0N-phase-*.yaml (the value relayed from the preceding phase via workflow_call inputs/outputs), or needs.phase_1_readiness_safety.outputs.selected_environment for a MAIN-level (00-main-goldengate-orchestrator.yaml) job such as final_validation. A matrix job always draws its per-entry selected environment from matrix.environment, regardless of file."""
from __future__ import annotations

import glob
import os
import re
import sys

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_WORKFLOW_DIR = os.path.join(REPO_ROOT, ".github", "workflows")
MAIN_WORKFLOW_FILENAME = "00-main-goldengate-orchestrator.yaml"
READINESS_SAFETY_PHASE_FILENAME = "01-phase-readiness-safety.yaml"
VALIDATE_MODEL_JOB = "validate_model"

# Every 0N-phase-*.yaml file participates in the same scope contract; only 01-phase-readiness-safety.yaml (the validate_model producer) and 00-main-goldengate-orchestrator.yaml (the phase-calling orchestrator) are structurally distinct.
PHASE_WORKFLOW_FILENAMES = {
    "01-phase-readiness-safety.yaml",
    "02-phase-aws-prerequisites.yaml",
    "03-phase-argocd.yaml",
    "04-phase-platform-observability.yaml",
    "05-phase-runtimes.yaml",
    "06-phase-replication.yaml",
    "07-phase-monitor-acceptance.yaml",
}

VAR_REF_RE = re.compile(r"\$\{?GG_SELECTED_ENVIRONMENT\}?")
GITHUB_ENV_ASSIGNMENT_RE = re.compile(r"^\s*echo\s+[\"']?GG_SELECTED_ENVIRONMENT=.*>>\s*\"?\$GITHUB_ENV\"?", re.MULTILINE)
READINESS_SAFETY_EXPECTED_VALUE = "${{ needs.validate_model.outputs.selected_environment }}"
RELAYED_PHASE_EXPECTED_VALUE = "${{ inputs.selected_environment }}"
MAIN_EXPECTED_VALUE = "${{ needs.phase_1_readiness_safety.outputs.selected_environment }}"
MATRIX_EXPECTED_VALUE = "${{ matrix.environment }}"


def _references_var(run_text):
    return bool(VAR_REF_RE.search(run_text or ""))


def _is_matrix_job(job):
    # Structural, never a hardcoded job-name list -- a job whose strategy declares a matrix draws its per-entry selected environment from matrix.environment, exactly like deployment_matrix/deletion_matrix/active_runtime_matrix already do.
    return bool((job.get("strategy") or {}).get("matrix"))


def _check_validate_model_job(filename, job_name, job):
    """The sole intentional exception: no job-level binding required, but the GITHUB_ENV-persisting step must run strictly before any step that references the variable."""
    violations = []
    steps = job.get("steps") or []
    persisted = False
    for step in steps:
        run_text = step.get("run") or ""
        if _references_var(run_text) and not persisted:
            violations.append({
                "file": filename, "job": job_name, "step": step.get("name"),
                "reason": "references GG_SELECTED_ENVIRONMENT before any step in this job has persisted it to $GITHUB_ENV",
            })
        if GITHUB_ENV_ASSIGNMENT_RE.search(run_text):
            persisted = True
    return violations


def _expected_value_for(filename, job):
    """The canonical source expression a non-matrix job in this file must bind GG_SELECTED_ENVIRONMENT to -- None means this file's jobs are not scope-checked for an exact expression (a specialist 10/20/30/40/50-sub-*.yaml or any other non-phase workflow), only that a job-level binding exists at all."""
    if _is_matrix_job(job):
        return MATRIX_EXPECTED_VALUE
    if filename == READINESS_SAFETY_PHASE_FILENAME:
        return READINESS_SAFETY_EXPECTED_VALUE
    if filename in PHASE_WORKFLOW_FILENAMES:
        return RELAYED_PHASE_EXPECTED_VALUE
    if filename == MAIN_WORKFLOW_FILENAME:
        return MAIN_EXPECTED_VALUE
    return None


def _check_normal_job(filename, job_name, job):
    steps = job.get("steps") or []
    if not any(_references_var(s.get("run") or "") for s in steps):
        return []

    binding = (job.get("env") or {}).get("GG_SELECTED_ENVIRONMENT")
    if binding is None:
        return [{
            "file": filename, "job": job_name, "step": None,
            "reason": "run: step(s) reference GG_SELECTED_ENVIRONMENT but the job defines no job-level env.GG_SELECTED_ENVIRONMENT binding (a step-level-only binding is not sufficient)",
        }]

    expected = _expected_value_for(filename, job)
    if expected is None:
        return []

    if str(binding) != expected:
        return [{
            "file": filename, "job": job_name, "step": None,
            "reason": f"job-level GG_SELECTED_ENVIRONMENT is {binding!r}, expected exactly {expected!r}",
        }]
    return []


def check_workflow_file(path):
    """Returns (jobs_with_refs, violations) for a single workflow file."""
    with open(path) as f:
        doc = yaml.safe_load(f)
    if not isinstance(doc, dict):
        return 0, []

    filename = os.path.basename(path)
    is_readiness_safety_phase = filename == READINESS_SAFETY_PHASE_FILENAME
    jobs_with_refs = 0
    violations = []
    for job_name, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps") or []
        if any(_references_var(s.get("run") or "") for s in steps):
            jobs_with_refs += 1
        if is_readiness_safety_phase and job_name == VALIDATE_MODEL_JOB:
            violations.extend(_check_validate_model_job(filename, job_name, job))
        else:
            violations.extend(_check_normal_job(filename, job_name, job))
    return jobs_with_refs, violations


def scan_workflow_dir(workflow_dir):
    paths = sorted(glob.glob(os.path.join(workflow_dir, "*.yaml")) + glob.glob(os.path.join(workflow_dir, "*.yml")))
    total_jobs_with_refs = 0
    all_violations = []
    for path in paths:
        jobs_with_refs, violations = check_workflow_file(path)
        total_jobs_with_refs += jobs_with_refs
        all_violations.extend(violations)
    return paths, total_jobs_with_refs, all_violations


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    workflow_dir = DEFAULT_WORKFLOW_DIR
    if argv:
        if len(argv) == 2 and argv[0] == "--workflow-dir":
            workflow_dir = argv[1]
        else:
            print("usage: check-goldengate-workflow-env-scope.py [--workflow-dir <dir>]")
            return 2

    paths, jobs_with_refs, violations = scan_workflow_dir(workflow_dir)
    unsafe_jobs = sorted({(v["file"], v["job"]) for v in violations})

    print(f"Workflows inspected: {len(paths)}")
    print(f"Jobs with GG_SELECTED_ENVIRONMENT run: references: {jobs_with_refs}")
    print(f"Unsafe jobs: {len(unsafe_jobs)}")

    if violations:
        for v in violations:
            step = f" step={v['step']!r}" if v["step"] else ""
            print(f"VIOLATION: {v['file']} job={v['job']}{step}: {v['reason']}")
        print(f"\nFAIL: {len(violations)} GG_SELECTED_ENVIRONMENT scope violation(s) found across {len(unsafe_jobs)} job(s).")
        return 1

    print("OK: zero unsafe GG_SELECTED_ENVIRONMENT references in active workflow run: blocks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
