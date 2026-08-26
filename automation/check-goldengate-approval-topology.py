"""check-goldengate-approval-topology.py: fails closed if any GitHub Actions workflow in this repository drifts away from the Live Deployment Approval Topology Fix invariant -- MAIN owns exactly one GoldenGate application deployment approval (goldengate_deploy_authorization) for the entire end-to-end Deploy DAG, the four specialist reusable workflows it calls (20/30/40/50) never open a second approval when MAIN-orchestrated, each specialist still retains exactly one standalone approval path for a direct workflow_dispatch run, and the corporate Terraform governance boundary (10-sub-iam-secrets.yaml) plus the independent OPS workflows (80/90/91) are left untouched by this invariant."""
from __future__ import annotations

import glob
import os
import sys

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_WORKFLOW_DIR = os.path.join(REPO_ROOT, ".github", "workflows")

MAIN_WORKFLOW_FILENAME = "00-main-goldengate-orchestrator.yaml"
MAIN_AUTHORIZATION_JOB = "goldengate_deploy_authorization"
SPECIALIST_FILENAMES = [
    "20-sub-argocd.yaml",
    "30-sub-platform.yaml",
    "40-sub-observability.yaml",
    "50-sub-monitor.yaml",
]
SPECIALIST_CALLER_JOBS = {
    "20-sub-argocd.yaml": "reconcile_argocd",
    "30-sub-platform.yaml": "platform_sync_once",
    "40-sub-observability.yaml": "observability_sync_once",
    "50-sub-monitor.yaml": "monitor_sync_once",
}
STANDALONE_AUTHORIZATION_JOB = "standalone_deploy_authorization"
ORCHESTRATION_CONTRACT_INPUT = "orchestrated_by_main"
CORPORATE_TERRAFORM_WORKFLOW_FILENAME = "10-sub-iam-secrets.yaml"
CORPORATE_TERRAFORM_WORKFLOW_CALLER_JOB = "terraform_sync_once"
CORPORATE_TERRAFORM_REUSABLE_WORKFLOW_REF = "AbuDhabiCommercialBank/adcb-reusable-workflows/.github/workflows/aws-terraform-apply.yaml@main"
OPS_WORKFLOW_FILENAMES = [
    "80-ops-monitor-metrics-config.yaml",
    "90-ops-observability-artifact-sync.yaml",
    "91-ops-ecr-image-sync.yaml",
]


class _DuplicateKeyLoader(yaml.SafeLoader):
    """A strict variant of the standard safe loader: two colliding keys in the same mapping (for example two "environment:" lines accidentally left in one job) are a parse error, never a silent last-one-wins overwrite."""


def _construct_mapping_no_duplicates(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                None, None, f"found duplicate key {key!r}", key_node.start_mark
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_DuplicateKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping_no_duplicates
)


def load_workflow(path):
    with open(path) as f:
        return yaml.load(f, Loader=_DuplicateKeyLoader)


def _on_block(doc):
    # PyYAML 1.1 resolves the bare scalar key "on" to the boolean True -- both spellings are checked so this never silently reads an empty trigger block.
    return doc.get("on", doc.get(True)) or {}


def _jobs(doc):
    return doc.get("jobs") or {}


def _job_environment(job):
    return job.get("environment")


def _job_if(job):
    return job.get("if") or ""


def _job_needs(job):
    needs = job.get("needs")
    if needs is None:
        return []
    if isinstance(needs, str):
        return [needs]
    return list(needs)


def check_main_single_authorization(doc, findings):
    """Rule 1 / Rule 9 / Rule 10: exactly one job-level environment: in the whole MAIN workflow, and it must be goldengate_deploy_authorization."""
    jobs = _jobs(doc)
    envs = [(name, _job_environment(job)) for name, job in jobs.items() if isinstance(job, dict) and _job_environment(job) is not None]
    if len(envs) != 1:
        findings.append(f"MAIN must have exactly one job-level environment: key, found {len(envs)}: {[n for n, _ in envs]}")
        return
    name, _value = envs[0]
    if name != MAIN_AUTHORIZATION_JOB:
        findings.append(f"MAIN's sole job-level environment: key belongs to {name!r}, expected {MAIN_AUTHORIZATION_JOB!r}")
    job = jobs[MAIN_AUTHORIZATION_JOB]
    if job.get("runs-on") != "ubuntu-latest":
        findings.append(f"MAIN's {MAIN_AUTHORIZATION_JOB} must run on ubuntu-latest (no AWS/Kubernetes mutation), found {job.get('runs-on')!r}")
    if "uses" in job:
        findings.append(f"MAIN's {MAIN_AUTHORIZATION_JOB} must be a normal runs-on job, not a reusable-workflow uses: job")


def check_main_calls_pass_orchestrated_by_main(doc, findings):
    """Rule 5: MAIN passes orchestrated_by_main: true to all four specialist reusable-workflow calls."""
    jobs = _jobs(doc)
    for filename, caller_job_name in SPECIALIST_CALLER_JOBS.items():
        job = jobs.get(caller_job_name)
        if not isinstance(job, dict):
            findings.append(f"MAIN is missing the expected caller job {caller_job_name!r} for {filename!r}")
            continue
        expected_uses_suffix = f".github/workflows/{filename}"
        uses = job.get("uses") or ""
        if not uses.endswith(expected_uses_suffix):
            findings.append(f"MAIN job {caller_job_name!r} does not call {filename!r} via uses: (found {uses!r})")
            continue
        with_block = job.get("with") or {}
        if with_block.get(ORCHESTRATION_CONTRACT_INPUT) is not True:
            findings.append(f"MAIN job {caller_job_name!r} must pass {ORCHESTRATION_CONTRACT_INPUT}: true to {filename!r}, found {with_block.get(ORCHESTRATION_CONTRACT_INPUT)!r}")


def check_main_reconcile_requires_authorization(doc, findings):
    """The reconcile_argocd caller job must itself require goldengate_deploy_authorization's success -- never let a skipped/failed authorization be silently treated as success."""
    jobs = _jobs(doc)
    job = jobs.get("reconcile_argocd")
    if not isinstance(job, dict):
        findings.append("MAIN is missing the expected reconcile_argocd job")
        return
    needs = _job_needs(job)
    if MAIN_AUTHORIZATION_JOB not in needs:
        findings.append(f"MAIN job reconcile_argocd must list {MAIN_AUTHORIZATION_JOB!r} in needs:, found {needs}")
    if f"needs.{MAIN_AUTHORIZATION_JOB}.result == 'success'" not in _job_if(job):
        findings.append(f"MAIN job reconcile_argocd's if: must require needs.{MAIN_AUTHORIZATION_JOB}.result == 'success', found: {_job_if(job)!r}")


DELETION_JOB = "delete_removed_argocd_applications"


def _needs_graph(jobs):
    return {name: _job_needs(job) for name, job in jobs.items() if isinstance(job, dict)}


def _is_transitively_needed(jobs, start, target):
    """BFS over the needs: graph starting at `start` -- True if `target` is reachable by walking `start`'s own (recursive) needs: chain, or if start == target itself. A generic reachability check, never hardcoded to one specific intermediate job name, so it stays correct if the DAG is later restructured."""
    if start == target:
        return True
    graph = _needs_graph(jobs)
    seen = set()
    queue = list(graph.get(start, []))
    while queue:
        current = queue.pop()
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        queue.extend(graph.get(current, []))
    return False


def check_main_deletion_requires_authorization(doc, findings):
    """GoldenGate Runtime Presence Contract -- Final Safety Correction, Gap 1: runtime removal (delete_removed_argocd_applications performs kubectl patch/delete application and kubectl delete namespace) is exactly as consequential a mutation as runtime creation/update and must never be able to bypass the single MAIN application deployment authorization. This proves the GENERIC invariant rather than hardcoding one acceptable intermediate job name (such as validate_argocd_ready): the deletion job must be transitively downstream of goldengate_deploy_authorization via needs:, AND it must directly need some job on that chain whose result it explicitly requires to be exactly 'success' in its own if: -- merely listing a job in needs: is not sufficient, since GitHub Actions treats a skipped dependency as satisfying a bare needs: reference without an explicit result check, silently letting a cancelled/failed/skipped authorization chain still permit deletion. The still-single-approval invariant (no second job-level environment: was introduced) is independently verified by check_main_single_authorization above, not duplicated here."""
    jobs = _jobs(doc)
    job = jobs.get(DELETION_JOB)
    if not isinstance(job, dict):
        findings.append(f"MAIN is missing the expected {DELETION_JOB!r} job")
        return

    if not _is_transitively_needed(jobs, DELETION_JOB, MAIN_AUTHORIZATION_JOB):
        findings.append(f"MAIN job {DELETION_JOB!r} is not transitively downstream of {MAIN_AUTHORIZATION_JOB!r} (via needs:) -- runtime removal must not be able to bypass the single MAIN application deployment authorization")
        return

    needs = _job_needs(job)
    job_if = _job_if(job)
    gating_dependency = None
    for dep in needs:
        if _is_transitively_needed(jobs, dep, MAIN_AUTHORIZATION_JOB) and f"needs.{dep}.result == 'success'" in job_if:
            gating_dependency = dep
            break

    if gating_dependency is None:
        findings.append(
            f"MAIN job {DELETION_JOB!r} does not directly require (via needs.<job>.result == 'success' in its own if:) any job that is transitively downstream of {MAIN_AUTHORIZATION_JOB!r} -- listing such a job in needs: alone is not sufficient; runtime removal could otherwise still proceed after a cancelled/failed/skipped authorization chain"
        )


def check_specialist_orchestration_contract(filename, doc, findings):
    """Rule 4: orchestrated_by_main is declared under workflow_call.inputs only, defaulting to false, and is never exposed as a workflow_dispatch input."""
    on_block = _on_block(doc)
    call_inputs = ((on_block.get("workflow_call") or {}).get("inputs")) or {}
    dispatch_inputs = ((on_block.get("workflow_dispatch") or {}).get("inputs")) or {}

    if ORCHESTRATION_CONTRACT_INPUT not in call_inputs:
        findings.append(f"{filename}: workflow_call.inputs must declare {ORCHESTRATION_CONTRACT_INPUT!r}")
    else:
        spec = call_inputs[ORCHESTRATION_CONTRACT_INPUT]
        if spec.get("type") != "boolean":
            findings.append(f"{filename}: workflow_call.inputs.{ORCHESTRATION_CONTRACT_INPUT} must be type boolean, found {spec.get('type')!r}")
        if spec.get("default") is not False:
            findings.append(f"{filename}: workflow_call.inputs.{ORCHESTRATION_CONTRACT_INPUT} must default to false, found {spec.get('default')!r}")

    if ORCHESTRATION_CONTRACT_INPUT in dispatch_inputs:
        findings.append(f"{filename}: {ORCHESTRATION_CONTRACT_INPUT} must never be a workflow_dispatch input (it is an internal workflow_call-only contract, not an operator setting)")


def check_specialist_single_standalone_gate(filename, doc, findings):
    """Rule 2 / Rule 3 / Rule 8: exactly one job-level environment: in the whole file (the standalone authorization job), and every other job in the file carries no job-level environment: of its own -- including when a specialist has more than one implementation job (50-sub-monitor.yaml)."""
    jobs = _jobs(doc)
    envs = [(name, _job_environment(job)) for name, job in jobs.items() if isinstance(job, dict) and _job_environment(job) is not None]
    if len(envs) != 1:
        findings.append(f"{filename}: must have exactly one job-level environment: key, found {len(envs)}: {[n for n, _ in envs]}")
        return
    name, _value = envs[0]
    if name != STANDALONE_AUTHORIZATION_JOB:
        findings.append(f"{filename}: sole job-level environment: key belongs to {name!r}, expected {STANDALONE_AUTHORIZATION_JOB!r}")
    job = jobs.get(STANDALONE_AUTHORIZATION_JOB)
    if not isinstance(job, dict):
        return
    if job.get("runs-on") != "ubuntu-latest":
        findings.append(f"{filename}: {STANDALONE_AUTHORIZATION_JOB} must run on ubuntu-latest (no AWS/Kubernetes mutation), found {job.get('runs-on')!r}")
    if "uses" in job:
        findings.append(f"{filename}: {STANDALONE_AUTHORIZATION_JOB} must be a normal runs-on job, not a reusable-workflow uses: job")
    expected_if = f"inputs.{ORCHESTRATION_CONTRACT_INPUT} != true"
    if expected_if not in _job_if(job):
        findings.append(f"{filename}: {STANDALONE_AUTHORIZATION_JOB}'s if: must contain {expected_if!r}, found {_job_if(job)!r}")


def check_specialist_implementation_jobs_gated(filename, doc, findings):
    """Rule 6 / Rule 7: every implementation job (any job in the file other than standalone_deploy_authorization) must depend on standalone_deploy_authorization via always()-plus-explicit-result, must never implicitly treat a skipped standalone gate as success when MAIN-orchestrated, and must fail closed on a real standalone run whose gate did not succeed."""
    jobs = _jobs(doc)
    implementation_jobs = {name: job for name, job in jobs.items() if name != STANDALONE_AUTHORIZATION_JOB and isinstance(job, dict)}
    if not implementation_jobs:
        findings.append(f"{filename}: no implementation job found besides {STANDALONE_AUTHORIZATION_JOB}")
        return

    orchestrated_bypass = f"inputs.{ORCHESTRATION_CONTRACT_INPUT} == true"
    standalone_success = f"needs.{STANDALONE_AUTHORIZATION_JOB}.result == 'success'"

    for name, job in implementation_jobs.items():
        needs = _job_needs(job)
        job_if = _job_if(job)
        # A job may depend on the authorization job directly, or transitively through a sibling implementation job that itself already depends on it (50-sub-monitor.yaml's build_publish_and_deploy depends on ensure_monitor_image, which depends on standalone_deploy_authorization).
        depends_directly = STANDALONE_AUTHORIZATION_JOB in needs
        depends_transitively = any(
            STANDALONE_AUTHORIZATION_JOB in _job_needs(implementation_jobs.get(n, {}))
            for n in needs
            if n in implementation_jobs
        )
        if not (depends_directly or depends_transitively):
            findings.append(f"{filename}: implementation job {name!r} does not depend on {STANDALONE_AUTHORIZATION_JOB!r} (directly or transitively)")
            continue
        if depends_directly:
            if "always()" not in job_if:
                findings.append(f"{filename}: implementation job {name!r} depends on {STANDALONE_AUTHORIZATION_JOB!r} but its if: lacks always() -- a legitimately skipped standalone gate during a MAIN-orchestrated run would otherwise be treated as a failed dependency")
            if orchestrated_bypass not in job_if:
                findings.append(f"{filename}: implementation job {name!r}'s if: must contain {orchestrated_bypass!r} so a MAIN-orchestrated run never depends on the skipped standalone gate being success")
            if standalone_success not in job_if:
                findings.append(f"{filename}: implementation job {name!r}'s if: must contain {standalone_success!r} so a real standalone run fails closed on a failed/cancelled/unexpectedly-skipped authorization")


def check_environment_variable_not_exposed_as_secret(filename, doc, findings):
    """Rule: workflow_call outputs must never surface a secret -- a lightweight structural check that no output value expression references secrets.*."""
    on_block = _on_block(doc)
    outputs = ((on_block.get("workflow_call") or {}).get("outputs")) or {}
    for output_name, spec in outputs.items():
        value = str((spec or {}).get("value", ""))
        if "secrets." in value:
            findings.append(f"{filename}: workflow_call output {output_name!r} must never expose a secret, found value {value!r}")


def check_corporate_terraform_boundary(main_doc, terraform_doc, findings):
    """Rule 11: the corporate Terraform reusable workflow call remains present in 10-sub-iam-secrets.yaml with its governance-override inputs intact, and MAIN's own call site into 10-sub-iam-secrets.yaml is unchanged by this fix."""
    jobs = _jobs(terraform_doc)
    corporate_job = None
    for job in jobs.values():
        if isinstance(job, dict) and job.get("uses") == CORPORATE_TERRAFORM_REUSABLE_WORKFLOW_REF:
            corporate_job = job
            break
    if corporate_job is None:
        findings.append(f"{CORPORATE_TERRAFORM_WORKFLOW_FILENAME}: no job calls the corporate reusable workflow {CORPORATE_TERRAFORM_REUSABLE_WORKFLOW_REF!r}")
        return
    with_block = corporate_job.get("with") or {}
    for expected_input in ("override_noncompliance", "override_reason"):
        if expected_input not in with_block:
            findings.append(f"{CORPORATE_TERRAFORM_WORKFLOW_FILENAME}: corporate reusable workflow call is missing governance input {expected_input!r}")

    main_jobs = _jobs(main_doc)
    caller_job = main_jobs.get(CORPORATE_TERRAFORM_WORKFLOW_CALLER_JOB)
    if not isinstance(caller_job, dict):
        findings.append(f"MAIN is missing the expected {CORPORATE_TERRAFORM_WORKFLOW_CALLER_JOB!r} job")
        return
    uses = caller_job.get("uses") or ""
    if not uses.endswith(f".github/workflows/{CORPORATE_TERRAFORM_WORKFLOW_FILENAME}"):
        findings.append(f"MAIN job {CORPORATE_TERRAFORM_WORKFLOW_CALLER_JOB!r} no longer calls {CORPORATE_TERRAFORM_WORKFLOW_FILENAME!r} (found uses: {uses!r})")
    caller_with = caller_job.get("with") or {}
    for expected_input in ("terraform_governance_override", "terraform_governance_override_reason"):
        if expected_input not in caller_with:
            findings.append(f"MAIN job {CORPORATE_TERRAFORM_WORKFLOW_CALLER_JOB!r} is missing expected passthrough input {expected_input!r}")
    if ORCHESTRATION_CONTRACT_INPUT in caller_with:
        findings.append(f"MAIN job {CORPORATE_TERRAFORM_WORKFLOW_CALLER_JOB!r} must never pass {ORCHESTRATION_CONTRACT_INPUT!r} into the corporate governance boundary -- it owns its own, separate approval")


def check_main_never_calls_ops_workflows(main_doc, findings):
    """Rule 12: the OPS workflows remain outside the MAIN application authorization invariant -- confirmed by MAIN never calling them via uses:, so their own independent environment: protection is left untouched."""
    jobs = _jobs(main_doc)
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        uses = job.get("uses") or ""
        for ops_filename in OPS_WORKFLOW_FILENAMES:
            if uses.endswith(f".github/workflows/{ops_filename}"):
                findings.append(f"MAIN must not call {ops_filename!r} -- OPS workflows are outside the single-approval invariant and keep their own independent protection")


def check_ops_workflows_retain_own_protection(workflow_dir, findings):
    """Rule 12 (converse): each OPS workflow still protects its own implementation with a job-level environment: -- this fix must not have accidentally stripped it."""
    for ops_filename in OPS_WORKFLOW_FILENAMES:
        path = os.path.join(workflow_dir, ops_filename)
        if not os.path.exists(path):
            continue
        doc = load_workflow(path)
        jobs = _jobs(doc)
        if not any(isinstance(job, dict) and _job_environment(job) is not None for job in jobs.values()):
            findings.append(f"{ops_filename}: expected at least one job-level environment: key (its own independent operator approval), found none")


def run_checks(workflow_dir):
    findings = []
    workflows_inspected = 0

    main_path = os.path.join(workflow_dir, MAIN_WORKFLOW_FILENAME)
    main_doc = load_workflow(main_path)
    workflows_inspected += 1
    check_main_single_authorization(main_doc, findings)
    check_main_calls_pass_orchestrated_by_main(main_doc, findings)
    check_main_reconcile_requires_authorization(main_doc, findings)
    check_main_deletion_requires_authorization(main_doc, findings)
    check_main_never_calls_ops_workflows(main_doc, findings)

    for filename in SPECIALIST_FILENAMES:
        path = os.path.join(workflow_dir, filename)
        doc = load_workflow(path)
        workflows_inspected += 1
        check_specialist_orchestration_contract(filename, doc, findings)
        check_specialist_single_standalone_gate(filename, doc, findings)
        check_specialist_implementation_jobs_gated(filename, doc, findings)
        check_environment_variable_not_exposed_as_secret(filename, doc, findings)

    terraform_path = os.path.join(workflow_dir, CORPORATE_TERRAFORM_WORKFLOW_FILENAME)
    terraform_doc = load_workflow(terraform_path)
    workflows_inspected += 1
    check_corporate_terraform_boundary(main_doc, terraform_doc, findings)

    check_ops_workflows_retain_own_protection(workflow_dir, findings)
    workflows_inspected += sum(1 for f in OPS_WORKFLOW_FILENAMES if os.path.exists(os.path.join(workflow_dir, f)))

    return workflows_inspected, findings


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    workflow_dir = DEFAULT_WORKFLOW_DIR
    if argv:
        if len(argv) == 2 and argv[0] == "--workflow-dir":
            workflow_dir = argv[1]
        else:
            print("usage: check-goldengate-approval-topology.py [--workflow-dir <dir>]")
            return 2

    workflows_inspected, findings = run_checks(workflow_dir)

    print(f"Workflows inspected: {workflows_inspected}")
    print(f"Unsafe jobs: {len(findings)}")

    if findings:
        for finding in findings:
            print(f"VIOLATION: {finding}")
        print(f"\nFAIL: {len(findings)} approval-topology violation(s) found.")
        return 1

    print("OK: MAIN owns exactly one GoldenGate application deployment authorization, all MAIN-orchestrated specialist calls carry orchestrated_by_main: true, every specialist retains exactly one standalone approval path, and the corporate Terraform governance boundary plus the OPS workflows remain untouched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
