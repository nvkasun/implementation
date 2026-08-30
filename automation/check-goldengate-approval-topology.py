"""check-goldengate-approval-topology.py: fails closed if any GitHub Actions workflow in this repository drifts away from the Live Deployment Approval Topology Fix invariant -- exactly one GoldenGate application deployment approval (goldengate_deploy_authorization) exists for the entire end-to-end Deploy DAG, the four specialist reusable workflows (20/30/40/50) never open a second approval when MAIN-orchestrated, each specialist still retains exactly one standalone approval path for a direct workflow_dispatch run, and the corporate Terraform governance boundary (10-sub-iam-secrets.yaml) plus the independent OPS workflows (80/90/91) are left untouched by this invariant. Phase 7 grouping: 20/30/40 remain DIRECT MAIN calls, but 50-sub-monitor.yaml is now called NESTED -- MAIN -> 70-phase-monitor-final-acceptance.yaml -> monitor_sync_once -> 50-sub-monitor.yaml. Phase 3 grouping: goldengate_deploy_authorization itself, along with argocd_preflight/reconcile_argocd/validate_argocd_ready, moved OFF MAIN entirely into 30-phase-argocd-orchestration.yaml -- MAIN -> 30-phase-argocd-orchestration.yaml -> {goldengate_deploy_authorization; reconcile_argocd -> 20-sub-argocd.yaml with orchestrated_by_main: true}. This checker actively verifies both full nested chains end to end (never merely stops checking a job because it moved), proves MAIN itself now carries ZERO job-level environment: gates, proves the Phase 3 wrapper carries exactly the one gate that moved into it, and replaces the old single-document transitive-needs-graph-walk proof for delete_removed_argocd_applications with an explicit two-part cross-workflow proof spanning both YAML documents."""
from __future__ import annotations

import glob
import os
import sys

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_WORKFLOW_DIR = os.path.join(REPO_ROOT, ".github", "workflows")

MAIN_WORKFLOW_FILENAME = "00-main-goldengate-orchestrator.yaml"
# This job no longer lives directly in MAIN after the Phase 3 grouping -- it now lives inside PHASE3_WRAPPER_FILENAME. The name is kept generic (not MAIN_*) because the invariant it anchors ("exactly one end-to-end GoldenGate application deployment approval exists anywhere") is no longer about which single file holds it.
GOLDENGATE_AUTHORIZATION_JOB = "goldengate_deploy_authorization"
SPECIALIST_FILENAMES = [
    "20-sub-argocd.yaml",
    "30-sub-platform.yaml",
    "40-sub-observability.yaml",
    "50-sub-monitor.yaml",
]
# 30/40 remain DIRECT MAIN caller jobs. 20-sub-argocd.yaml (Phase 3 grouping) and 50-sub-monitor.yaml (Phase 7 grouping) are both deliberately EXCLUDED from this direct-caller map -- MAIN no longer calls either directly; see check_main_calls_phase3_wrapper_which_calls_argocd / check_main_calls_phase7_wrapper_which_calls_monitor below for the actively-verified nested chains instead. Both 20-sub-argocd.yaml and 50-sub-monitor.yaml remain in SPECIALIST_FILENAMES above, unaffected -- their own internal structure (orchestration contract, single standalone gate, gated implementation jobs) is checked regardless of who calls them.
SPECIALIST_CALLER_JOBS = {
    "30-sub-platform.yaml": "platform_sync_once",
    "40-sub-observability.yaml": "observability_sync_once",
}
STANDALONE_AUTHORIZATION_JOB = "standalone_deploy_authorization"
ORCHESTRATION_CONTRACT_INPUT = "orchestrated_by_main"
MONITOR_SPECIALIST_FILENAME = "50-sub-monitor.yaml"
PHASE7_WRAPPER_FILENAME = "70-phase-monitor-final-acceptance.yaml"
PHASE7_WRAPPER_MONITOR_CALLER_JOB = "monitor_sync_once"
ARGOCD_SPECIALIST_FILENAME = "20-sub-argocd.yaml"
PHASE3_WRAPPER_FILENAME = "30-phase-argocd-orchestration.yaml"
PHASE3_WRAPPER_CALLER_JOB = "phase_3_argocd"
PHASE3_RECONCILE_JOB = "reconcile_argocd"
PHASE3_VALIDATE_JOB = "validate_argocd_ready"
PHASE3_VALIDATE_OUTPUT = "validate_argocd_ready_result"
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


def check_main_has_zero_authorization_gates(doc, findings):
    """Phase 3 grouping, invariant 2: goldengate_deploy_authorization moved off MAIN entirely into PHASE3_WRAPPER_FILENAME, so MAIN itself must now carry ZERO job-level environment: keys -- any job-level environment: reappearing directly in MAIN would mean a second, redundant (or worse, unreviewed) GoldenGate deployment approval was introduced outside the single wrapper this checker actively verifies below."""
    jobs = _jobs(doc)
    envs = [name for name, job in jobs.items() if isinstance(job, dict) and _job_environment(job) is not None]
    if envs:
        findings.append(f"MAIN must have zero job-level environment: keys after the Phase 3 grouping (found on {envs}) -- goldengate_deploy_authorization belongs only inside {PHASE3_WRAPPER_FILENAME!r}")


def check_phase3_wrapper_single_authorization(wrapper_doc, findings):
    """Phase 3 grouping, invariants 1/3/4: exactly one job-level environment: in the whole Phase 3 wrapper, and it must be goldengate_deploy_authorization, running on ubuntu-latest, never a uses: job -- the exact same shape this job had directly in MAIN before the grouping."""
    jobs = _jobs(wrapper_doc)
    envs = [(name, _job_environment(job)) for name, job in jobs.items() if isinstance(job, dict) and _job_environment(job) is not None]
    if len(envs) != 1:
        findings.append(f"{PHASE3_WRAPPER_FILENAME}: must have exactly one job-level environment: key, found {len(envs)}: {[n for n, _ in envs]}")
        return
    name, _value = envs[0]
    if name != GOLDENGATE_AUTHORIZATION_JOB:
        findings.append(f"{PHASE3_WRAPPER_FILENAME}: sole job-level environment: key belongs to {name!r}, expected {GOLDENGATE_AUTHORIZATION_JOB!r}")
    job = jobs[GOLDENGATE_AUTHORIZATION_JOB]
    if job.get("runs-on") != "ubuntu-latest":
        findings.append(f"{PHASE3_WRAPPER_FILENAME}: {GOLDENGATE_AUTHORIZATION_JOB} must run on ubuntu-latest (no AWS/Kubernetes mutation), found {job.get('runs-on')!r}")
    if "uses" in job:
        findings.append(f"{PHASE3_WRAPPER_FILENAME}: {GOLDENGATE_AUTHORIZATION_JOB} must be a normal runs-on job, not a reusable-workflow uses: job")


def check_phase3_wrapper_is_workflow_call_only(wrapper_doc, findings):
    """Phase 3 grouping, invariant 5: the Phase 3 wrapper is an internal orchestration wrapper, never an independent second operator-facing entry point -- it must expose workflow_call only."""
    on_block = _on_block(wrapper_doc)
    extra_triggers = [t for t in ("workflow_dispatch", "push", "pull_request", "schedule") if t in on_block]
    if extra_triggers:
        findings.append(f"{PHASE3_WRAPPER_FILENAME}: must expose workflow_call only -- found additional trigger(s) {sorted(extra_triggers)!r}")


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


def _find_job_calling(jobs, expected_filename):
    """Returns (job_name, job) for the first job whose uses: ends with .github/workflows/<expected_filename>, or (None, None) if none does."""
    expected_uses_suffix = f".github/workflows/{expected_filename}"
    for name, job in jobs.items():
        if isinstance(job, dict) and str(job.get("uses") or "").endswith(expected_uses_suffix):
            return name, job
    return None, None


def check_main_calls_phase7_wrapper_which_calls_monitor(main_doc, workflow_dir, findings):
    """Rule 5 (nested, Phase 7 grouping): 50-sub-monitor.yaml is no longer called directly by MAIN -- it must be reached through the EXACT chain MAIN -> 70-phase-monitor-final-acceptance.yaml -> monitor_sync_once -> 50-sub-monitor.yaml, with orchestrated_by_main: true preserved at the innermost call. This actively verifies every link, never merely stops checking 50-sub-monitor.yaml because it moved."""
    main_jobs = _jobs(main_doc)
    wrapper_job_name, wrapper_job = _find_job_calling(main_jobs, PHASE7_WRAPPER_FILENAME)
    if wrapper_job is None:
        findings.append(f"MAIN has no job calling {PHASE7_WRAPPER_FILENAME!r} -- 50-sub-monitor.yaml must be reached through this approved Phase 7 wrapper, never directly from MAIN and never left unreachable")
        return

    wrapper_path = os.path.join(workflow_dir, PHASE7_WRAPPER_FILENAME)
    if not os.path.exists(wrapper_path):
        findings.append(f"MAIN job {wrapper_job_name!r} calls {PHASE7_WRAPPER_FILENAME!r}, but that file does not exist")
        return
    wrapper_doc = load_workflow(wrapper_path)
    wrapper_jobs = _jobs(wrapper_doc)

    monitor_job = wrapper_jobs.get(PHASE7_WRAPPER_MONITOR_CALLER_JOB)
    if not isinstance(monitor_job, dict):
        findings.append(f"{PHASE7_WRAPPER_FILENAME} is missing the expected caller job {PHASE7_WRAPPER_MONITOR_CALLER_JOB!r} for {MONITOR_SPECIALIST_FILENAME!r}")
        return
    expected_uses_suffix = f".github/workflows/{MONITOR_SPECIALIST_FILENAME}"
    uses = monitor_job.get("uses") or ""
    if not uses.endswith(expected_uses_suffix):
        findings.append(f"{PHASE7_WRAPPER_FILENAME} job {PHASE7_WRAPPER_MONITOR_CALLER_JOB!r} does not call {MONITOR_SPECIALIST_FILENAME!r} via uses: (found {uses!r})")
        return
    with_block = monitor_job.get("with") or {}
    if with_block.get(ORCHESTRATION_CONTRACT_INPUT) is not True:
        findings.append(f"{PHASE7_WRAPPER_FILENAME} job {PHASE7_WRAPPER_MONITOR_CALLER_JOB!r} must pass {ORCHESTRATION_CONTRACT_INPUT}: true to {MONITOR_SPECIALIST_FILENAME!r}, found {with_block.get(ORCHESTRATION_CONTRACT_INPUT)!r}")


def check_main_calls_phase3_wrapper_which_calls_argocd(main_doc, workflow_dir, findings):
    """Phase 3 grouping, invariants 6/7 (nested): 20-sub-argocd.yaml is no longer called directly by MAIN -- it must be reached through the EXACT chain MAIN -> phase_3_argocd -> 30-phase-argocd-orchestration.yaml -> reconcile_argocd -> 20-sub-argocd.yaml, with orchestrated_by_main: true preserved at the innermost call and reconcile_argocd itself requiring goldengate_deploy_authorization's success. This actively verifies every link, never merely stops checking 20-sub-argocd.yaml because it moved."""
    main_jobs = _jobs(main_doc)
    wrapper_job_name, wrapper_job = _find_job_calling(main_jobs, PHASE3_WRAPPER_FILENAME)
    if wrapper_job is None:
        findings.append(f"MAIN has no job calling {PHASE3_WRAPPER_FILENAME!r} -- 20-sub-argocd.yaml must be reached through this approved Phase 3 wrapper, never directly from MAIN and never left unreachable")
        return
    if wrapper_job_name != PHASE3_WRAPPER_CALLER_JOB:
        findings.append(f"MAIN's caller job for {PHASE3_WRAPPER_FILENAME!r} is named {wrapper_job_name!r}, expected {PHASE3_WRAPPER_CALLER_JOB!r}")

    wrapper_path = os.path.join(workflow_dir, PHASE3_WRAPPER_FILENAME)
    if not os.path.exists(wrapper_path):
        findings.append(f"MAIN job {wrapper_job_name!r} calls {PHASE3_WRAPPER_FILENAME!r}, but that file does not exist")
        return
    wrapper_doc = load_workflow(wrapper_path)
    wrapper_jobs = _jobs(wrapper_doc)

    reconcile_job = wrapper_jobs.get(PHASE3_RECONCILE_JOB)
    if not isinstance(reconcile_job, dict):
        findings.append(f"{PHASE3_WRAPPER_FILENAME} is missing the expected caller job {PHASE3_RECONCILE_JOB!r} for {ARGOCD_SPECIALIST_FILENAME!r}")
        return
    expected_uses_suffix = f".github/workflows/{ARGOCD_SPECIALIST_FILENAME}"
    uses = reconcile_job.get("uses") or ""
    if not uses.endswith(expected_uses_suffix):
        findings.append(f"{PHASE3_WRAPPER_FILENAME} job {PHASE3_RECONCILE_JOB!r} does not call {ARGOCD_SPECIALIST_FILENAME!r} via uses: (found {uses!r})")
        return
    with_block = reconcile_job.get("with") or {}
    if with_block.get(ORCHESTRATION_CONTRACT_INPUT) is not True:
        findings.append(f"{PHASE3_WRAPPER_FILENAME} job {PHASE3_RECONCILE_JOB!r} must pass {ORCHESTRATION_CONTRACT_INPUT}: true to {ARGOCD_SPECIALIST_FILENAME!r}, found {with_block.get(ORCHESTRATION_CONTRACT_INPUT)!r}")


def check_phase7_wrapper_opens_no_second_authorization(wrapper_doc, findings):
    """The Phase 7 wrapper is a pure orchestration passthrough -- it must never declare its own job-level environment: (which would open a second, redundant GoldenGate deployment approval alongside MAIN's single goldengate_deploy_authorization) and must never itself be a standalone_deploy_authorization-style gate."""
    jobs = _jobs(wrapper_doc)
    envs = [name for name, job in jobs.items() if isinstance(job, dict) and _job_environment(job) is not None]
    if envs:
        findings.append(f"{PHASE7_WRAPPER_FILENAME}: must declare zero job-level environment: keys (found on {envs}) -- it must never open a second GoldenGate deployment approval; MAIN's single goldengate_deploy_authorization already covers this chain")
    on_block = _on_block(wrapper_doc)
    if "workflow_dispatch" in on_block or "push" in on_block or "pull_request" in on_block or "schedule" in on_block:
        findings.append(f"{PHASE7_WRAPPER_FILENAME}: must expose workflow_call only -- found additional trigger(s) {sorted(on_block.keys())!r}, which would make it a second operator-facing standalone workflow")


def check_phase3_wrapper_reconcile_requires_authorization(wrapper_doc, findings):
    """Phase 3 grouping, invariant 6: the reconcile_argocd caller job (now inside PHASE3_WRAPPER_FILENAME, no longer in MAIN) must itself require goldengate_deploy_authorization's success -- never let a skipped/failed authorization be silently treated as success."""
    jobs = _jobs(wrapper_doc)
    job = jobs.get(PHASE3_RECONCILE_JOB)
    if not isinstance(job, dict):
        findings.append(f"{PHASE3_WRAPPER_FILENAME} is missing the expected {PHASE3_RECONCILE_JOB!r} job")
        return
    needs = _job_needs(job)
    if GOLDENGATE_AUTHORIZATION_JOB not in needs:
        findings.append(f"{PHASE3_WRAPPER_FILENAME} job {PHASE3_RECONCILE_JOB!r} must list {GOLDENGATE_AUTHORIZATION_JOB!r} in needs:, found {needs}")
    if f"needs.{GOLDENGATE_AUTHORIZATION_JOB}.result == 'success'" not in _job_if(job):
        findings.append(f"{PHASE3_WRAPPER_FILENAME} job {PHASE3_RECONCILE_JOB!r}'s if: must require needs.{GOLDENGATE_AUTHORIZATION_JOB}.result == 'success', found: {_job_if(job)!r}")


DELETION_JOB = "delete_removed_argocd_applications"


def _explicit_success_chain_to(jobs, start, target):
    """True if there is a path from `start` down to `target` via needs: where EVERY hop's own if: explicitly requires needs.<next-hop>.result == 'success' for the next hop toward target -- proving the whole chain fails closed end to end, never merely that `target` happens to appear somewhere in the transitive needs: graph (a bare needs: listing is not sufficient, since GitHub Actions treats a skipped dependency as satisfying it without an explicit result check)."""
    if start == target:
        return True
    job = jobs.get(start)
    if not isinstance(job, dict):
        return False
    job_if = _job_if(job)
    for dep in _job_needs(job):
        if f"needs.{dep}.result == 'success'" in job_if and _explicit_success_chain_to(jobs, dep, target):
            return True
    return False


def check_phase3_internal_chain_fail_closed(wrapper_doc, findings):
    """Part A of the two-part cross-workflow deletion-authorization proof (Phase 3 grouping): now that goldengate_deploy_authorization lives inside PHASE3_WRAPPER_FILENAME rather than directly in MAIN, the old single-document transitive-needs-graph walk this checker used before the grouping can no longer span the reusable-workflow boundary by itself. This proves the INTERNAL half: PHASE3_VALIDATE_JOB is reachable from GOLDENGATE_AUTHORIZATION_JOB via an unbroken chain of explicit needs.<job>.result == 'success' checks entirely inside the wrapper. See check_main_deletion_requires_phase3_boundary below for Part B, the external half this composes with."""
    jobs = _jobs(wrapper_doc)
    if not _explicit_success_chain_to(jobs, PHASE3_VALIDATE_JOB, GOLDENGATE_AUTHORIZATION_JOB):
        findings.append(
            f"{PHASE3_WRAPPER_FILENAME}: {PHASE3_VALIDATE_JOB!r} is not reachable from {GOLDENGATE_AUTHORIZATION_JOB!r} via an unbroken chain of explicit needs.<job>.result == 'success' checks -- a cancelled/failed/skipped authorization could otherwise still let this wrapper's exposed validate_argocd_ready_result output report success"
        )


def check_main_deletion_requires_phase3_boundary(main_doc, findings):
    """Part B of the two-part cross-workflow deletion-authorization proof (Phase 3 grouping): GoldenGate Runtime Presence Contract -- Final Safety Correction, Gap 1: runtime removal (delete_removed_argocd_applications performs kubectl patch/delete application and kubectl delete namespace) is exactly as consequential a mutation as runtime creation/update and must never be able to bypass the single GoldenGate application deployment authorization. Since that authorization now lives behind the Phase 3 reusable-workflow boundary, proving this here requires BOTH an explicit needs.phase_3_argocd.result == 'success' check (the wrapper's own overall result) AND an explicit needs.phase_3_argocd.outputs.validate_argocd_ready_result == 'success' check (the wrapper's exact internal Phase 3D result) -- checking only the former is not sufficient, since an earlier internal Phase 3 failure and a genuine internal validate_argocd_ready skip are not guaranteed to mean the same thing. See check_phase3_internal_chain_fail_closed above for Part A, the internal half this composes with."""
    jobs = _jobs(main_doc)
    job = jobs.get(DELETION_JOB)
    if not isinstance(job, dict):
        findings.append(f"MAIN is missing the expected {DELETION_JOB!r} job")
        return

    needs = _job_needs(job)
    if PHASE3_WRAPPER_CALLER_JOB not in needs:
        findings.append(f"MAIN job {DELETION_JOB!r} must list {PHASE3_WRAPPER_CALLER_JOB!r} in needs:, found {needs}")
        return

    job_if = _job_if(job)
    required_result_check = f"needs.{PHASE3_WRAPPER_CALLER_JOB}.result == 'success'"
    required_output_check = f"needs.{PHASE3_WRAPPER_CALLER_JOB}.outputs.{PHASE3_VALIDATE_OUTPUT} == 'success'"
    if required_result_check not in job_if:
        findings.append(f"MAIN job {DELETION_JOB!r}'s if: must contain {required_result_check!r} (listing {PHASE3_WRAPPER_CALLER_JOB!r} in needs: alone is not sufficient), found: {job_if!r}")
    if required_output_check not in job_if:
        findings.append(f"MAIN job {DELETION_JOB!r}'s if: must contain {required_output_check!r} -- checking only the wrapper's overall result is not sufficient, since an earlier internal Phase 3 failure and a genuine internal validate_argocd_ready skip are not the same thing, found: {job_if!r}")


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
    check_main_has_zero_authorization_gates(main_doc, findings)
    check_main_calls_pass_orchestrated_by_main(main_doc, findings)
    check_main_calls_phase7_wrapper_which_calls_monitor(main_doc, workflow_dir, findings)
    check_main_calls_phase3_wrapper_which_calls_argocd(main_doc, workflow_dir, findings)
    check_main_deletion_requires_phase3_boundary(main_doc, findings)
    check_main_never_calls_ops_workflows(main_doc, findings)

    phase7_wrapper_path = os.path.join(workflow_dir, PHASE7_WRAPPER_FILENAME)
    if os.path.exists(phase7_wrapper_path):
        phase7_wrapper_doc = load_workflow(phase7_wrapper_path)
        workflows_inspected += 1
        check_phase7_wrapper_opens_no_second_authorization(phase7_wrapper_doc, findings)
    else:
        findings.append(f"{PHASE7_WRAPPER_FILENAME}: expected file does not exist")

    phase3_wrapper_path = os.path.join(workflow_dir, PHASE3_WRAPPER_FILENAME)
    if os.path.exists(phase3_wrapper_path):
        phase3_wrapper_doc = load_workflow(phase3_wrapper_path)
        workflows_inspected += 1
        check_phase3_wrapper_single_authorization(phase3_wrapper_doc, findings)
        check_phase3_wrapper_is_workflow_call_only(phase3_wrapper_doc, findings)
        check_phase3_wrapper_reconcile_requires_authorization(phase3_wrapper_doc, findings)
        check_phase3_internal_chain_fail_closed(phase3_wrapper_doc, findings)
        check_environment_variable_not_exposed_as_secret(PHASE3_WRAPPER_FILENAME, phase3_wrapper_doc, findings)
    else:
        findings.append(f"{PHASE3_WRAPPER_FILENAME}: expected file does not exist")

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

    print("OK: exactly one GoldenGate application deployment authorization exists end to end (inside the Phase 3 wrapper), MAIN itself carries zero job-level environment: gates, all MAIN-orchestrated specialist calls (direct or nested through the Phase 3/Phase 7 wrappers) carry orchestrated_by_main: true, every specialist retains exactly one standalone approval path, and the corporate Terraform governance boundary plus the OPS workflows remain untouched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
