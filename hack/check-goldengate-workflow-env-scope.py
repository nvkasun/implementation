"""check-goldengate-workflow-env-scope.py: fails closed if any active GitHub Actions run: step references $GG_SELECTED_ENVIRONMENT / ${GG_SELECTED_ENVIRONMENT} without satisfying the repository-wide job-scope invariant -- a job-level env.GG_SELECTED_ENVIRONMENT binding from its canonical source (needs.validate_model.outputs.selected_environment for a normal job, matrix.environment for a matrix job), a step-level-only binding is never sufficient. validate_model itself (00-main's sole producer of the value) is the one intentional exception, checked instead for GITHUB_ENV persistence ordering."""
from __future__ import annotations

import glob
import os
import re
import sys

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_WORKFLOW_DIR = os.path.join(REPO_ROOT, ".github", "workflows")
MAIN_WORKFLOW_FILENAME = "00-main-goldengate-orchestrator.yaml"
VALIDATE_MODEL_JOB = "validate_model"

VAR_REF_RE = re.compile(r"\$\{?GG_SELECTED_ENVIRONMENT\}?")
GITHUB_ENV_ASSIGNMENT_RE = re.compile(r"^\s*echo\s+[\"']?GG_SELECTED_ENVIRONMENT=.*>>\s*\"?\$GITHUB_ENV\"?", re.MULTILINE)
NORMAL_EXPECTED_VALUE = "${{ needs.validate_model.outputs.selected_environment }}"
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


def _check_normal_job(filename, job_name, job, is_main):
    steps = job.get("steps") or []
    if not any(_references_var(s.get("run") or "") for s in steps):
        return []

    binding = (job.get("env") or {}).get("GG_SELECTED_ENVIRONMENT")
    if binding is None:
        return [{
            "file": filename, "job": job_name, "step": None,
            "reason": "run: step(s) reference GG_SELECTED_ENVIRONMENT but the job defines no job-level env.GG_SELECTED_ENVIRONMENT binding (a step-level-only binding is not sufficient)",
        }]

    if not is_main:
        return []

    expected = MATRIX_EXPECTED_VALUE if _is_matrix_job(job) else NORMAL_EXPECTED_VALUE
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
    is_main = filename == MAIN_WORKFLOW_FILENAME
    jobs_with_refs = 0
    violations = []
    for job_name, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps") or []
        if any(_references_var(s.get("run") or "") for s in steps):
            jobs_with_refs += 1
        if is_main and job_name == VALIDATE_MODEL_JOB:
            violations.extend(_check_validate_model_job(filename, job_name, job))
        else:
            violations.extend(_check_normal_job(filename, job_name, job, is_main))
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
