#!/usr/bin/env python3
"""Phase 7A-7F | GoldenGate monitor orchestration entrypoint for monitor_ownership_preflight/monitor_dry_run_validation/validate_monitor_ready/replication_monitor_acceptance/end_to_end_deployment_acceptance in .github/workflows/00-main-goldengate-orchestrator.yaml; a thin orchestration layer that never reimplements the existing, already-approved classification logic in automation/orchestration/monitor_state.py (ownership-safety ABSENT/OWNED/BROKEN), automation/orchestration/monitor_acceptance.py (structural+health HEALTHY/BROKEN, ownership-chain-verified Ready pod selection), or automation/orchestration/end_to_end_acceptance.py (offline monitor-to-runtime GLOBAL active-runtime HEALTHY/BROKEN classification) -- each is invoked here as its own subprocess CLI, exactly as MAIN's prior bash implementation invoked it, never a second/parallel reimplementation of their business logic. Canonical environment values come from automation/goldengate-environment.py and canonical folder-driven deployment/replication state from automation/goldengate-deployment-model.py -- both invoked as subprocess CLIs, never duplicated. monitor_sync_once itself remains a `uses: ./.github/workflows/50-sub-monitor.yaml` reusable-workflow call and is intentionally NOT represented in this module -- its own fail-closed ECR/Ready-monitor-detection/runtime-log-acceptance safety corrections remain untouched inside that specialist workflow. AWS/Kubernetes credentials are never written to $GITHUB_OUTPUT, to any state file, or logged; kubectl/API failure diagnostics are always bounded/sanitized before being printed."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

ENVIRONMENT_TOOL = REPO_ROOT / "automation" / "goldengate-environment.py"
DEPLOYMENT_MODEL_TOOL = REPO_ROOT / "automation" / "goldengate-deployment-model.py"
MONITOR_STATE_TOOL = REPO_ROOT / "automation" / "orchestration" / "monitor_state.py"
MONITOR_ACCEPTANCE_TOOL = REPO_ROOT / "automation" / "orchestration" / "monitor_acceptance.py"
END_TO_END_ACCEPTANCE_TOOL = REPO_ROOT / "automation" / "orchestration" / "end_to_end_acceptance.py"
MONITOR_CHART_PATH = REPO_ROOT / "helm" / "goldengate-monitor"

DEFAULT_END_TO_END_TIMEOUT_SECONDS = 600
DEFAULT_END_TO_END_INTERVAL_SECONDS = 15

# Bounds how much of a raw kubectl/API stderr diagnostic may ever be surfaced -- never the full unbounded text (matches the existing sanitized-diagnostic idiom already used elsewhere in this workflow, e.g. end_to_end_deployment_acceptance's own `tail -c 500`).
_SANITIZED_ERROR_TAIL_CHARS = 500

# Outer kubectl-exec wall-clock bound = the in-pod HTTP request's own timeout_seconds + this buffer -- covers Kubernetes API/auth/SPDY-WebSocket negotiation and kubectl startup overhead that happens BEFORE the in-pod urllib call is even reached, while remaining tightly bounded (never unbounded, never open-ended).
_KUBECTL_EXEC_OUTER_TIMEOUT_BUFFER_SECONDS = 15


class Phase7MonitorError(Exception):
    """A fail-closed Phase 7 monitor orchestration error; main() reports it and exits non-zero."""


def require_env(name):
    value = os.environ.get(name, "")
    if not value:
        raise Phase7MonitorError(f"{name} is empty; canonical environment configuration must be loaded before this step.")
    return value


def require_environment_arg(environment):
    """Defense-in-depth non-empty check only -- the canonical accept/reject authority for an environment name remains automation/goldengate-environment.py itself (invoked below), never re-implemented here."""
    if not isinstance(environment, str) or not environment:
        raise Phase7MonitorError(f"environment {environment!r} is not a usable identifier; refusing to use it.")
    return environment


def require_pod_name_arg(pod_name):
    if not pod_name:
        raise Phase7MonitorError("no verified Ready gg-monitor pod name was provided -- refusing to proceed without a pod already selected/health-checked by strict-acceptance.")
    return pod_name


def write_github_output(pairs, output_path=None):
    """Appends name=value lines to $GITHUB_OUTPUT. Every value written by this module is a fixed literal state enum or a Kubernetes pod NAME (never a secret, never raw kubectl/API output). No-op (never raises) when GITHUB_OUTPUT is unset."""
    path = output_path if output_path is not None else os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for name, value in pairs:
            f.write(f"{name}={value}\n")


def _sanitize_tail(text):
    """Bounds a raw kubectl/subprocess stderr string to its last _SANITIZED_ERROR_TAIL_CHARS characters -- never the full unbounded text -- so a failure diagnostic can be informative without blindly re-emitting unexpected cluster/server detail."""
    if not text:
        return "<no diagnostic output>"
    return text[-_SANITIZED_ERROR_TAIL_CHARS:]


# Safe subprocess execution -- argument arrays only, never shell=True, never a shell pipeline, never a caller-constructed shell command string.

def run(argv, env=None, cwd=None, check=True, capture_output=True, input_text=None, timeout_seconds=None):
    """Runs argv as an argument array. Fails closed with the tool's own stdout/stderr on a non-zero exit when check=True. If timeout_seconds is given and the command exceeds it, subprocess.TimeoutExpired is caught here and converted into a normal-shaped CompletedProcess with a fixed non-zero returncode (124, matching the conventional shell `timeout` exit code) and a short, bounded diagnostic -- never an uncontrolled traceback, never any (possibly unbounded) partial command output. Every existing caller that already checks proc.returncode/raises on non-zero therefore fails closed on a timeout automatically, with no further per-call-site special-casing required."""
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd or REPO_ROOT,
            env=env,
            capture_output=capture_output,
            text=True,
            input=input_text,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        proc = subprocess.CompletedProcess(
            args=argv,
            returncode=124,
            stdout="",
            stderr=f"TIMEOUT: command exceeded {timeout_seconds}s and was terminated (argv: {' '.join(str(a) for a in argv)}).",
        )
    if check and proc.returncode != 0:
        raise Phase7MonitorError(f"{' '.join(str(a) for a in argv)} failed (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
    return proc


def _print_proc_stdout(proc):
    if proc.stdout:
        print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")


# Tool installation (never requires AWS credentials) and EKS connection (canonical environment values only).

def _ensure_kubectl():
    if run(["bash", "-c", "command -v kubectl"], check=False).returncode == 0:
        run(["kubectl", "version", "--client=true"])
        return
    kubectl_version = "v1.35.0"
    machine = run(["uname", "-m"]).stdout.strip()
    arch_map = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
    if machine not in arch_map:
        raise Phase7MonitorError(f"Unsupported architecture for kubectl: {machine}")
    kubectl_arch = arch_map[machine]
    run(["curl", "-fsSL", f"https://dl.k8s.io/release/{kubectl_version}/bin/linux/{kubectl_arch}/kubectl", "-o", "/tmp/kubectl"])
    run(["sudo", "mv", "/tmp/kubectl", "/usr/local/bin/kubectl"])
    run(["sudo", "chmod", "+x", "/usr/local/bin/kubectl"])
    run(["kubectl", "version", "--client=true"])


def cmd_ensure_kubectl(args):
    _ensure_kubectl()
    print("OK: kubectl is available.")


def _connect_to_eks():
    """Exact preserved role model: aws eks update-kubeconfig --region AWS_REGION --name EKS_CLUSTER_NAME --role-arn EKS_DEPLOY_ROLE_ARN --assume-role-arn EKS_DEPLOY_ROLE_ARN -- fails closed (via run()'s own check=True) on ANY AWS/EKS error (AccessDenied, Unauthorized, network error, missing cluster)."""
    aws_region = require_env("AWS_REGION")
    eks_cluster_name = require_env("EKS_CLUSTER_NAME")
    eks_deploy_role_arn = require_env("EKS_DEPLOY_ROLE_ARN")
    run(["aws", "eks", "update-kubeconfig", "--region", aws_region, "--name", eks_cluster_name,
         "--role-arn", eks_deploy_role_arn, "--assume-role-arn", eks_deploy_role_arn])


# Phase 7A: monitor ownership preflight (monitor_ownership_preflight) -- reuses automation/orchestration/monitor_state.py, never a second ownership classifier.

def cmd_ownership_preflight(args):
    environment = require_environment_arg(args.environment)
    _connect_to_eks()

    proc = run([sys.executable, str(MONITOR_STATE_TOOL), "--environment", environment], check=False)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    if proc.returncode != 0:
        raise Phase7MonitorError("the GoldenGate monitor ownership classifier could not classify the shared monitor (configuration or inspection error, not ABSENT) -- see diagnostics above.")

    _print_proc_stdout(proc)
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Phase7MonitorError(f"the GoldenGate monitor ownership classifier produced unparseable output: {exc}") from exc

    state = result.get("state")
    if state not in ("ABSENT", "OWNED", "BROKEN"):
        raise Phase7MonitorError(f"the GoldenGate monitor ownership classifier produced an unrecognized or missing state {state!r}; refusing to proceed.")

    print(f"GoldenGate monitor ownership-safety state: {state}")
    write_github_output([("state", state)])

    if state == "BROKEN":
        raise Phase7MonitorError("GoldenGate monitor ownership-safety state is BROKEN -- an existing footprint does not clearly belong to the shared monitor. This is not auto-repaired here -- investigate the diagnostics above before re-running.")


# Phase 7C: local monitor validation (monitor_dry_run_validation) -- deploy=false only, zero AWS/kubectl/mutation, private tempdir for all staged/rendered output (test evidence only, never desired state).

def cmd_validate_local(args):
    environment = require_environment_arg(args.environment)

    print("Running monitor unit tests...")
    run([sys.executable, "-m", "unittest", "discover", "-s", "monitoring/monitor/tests", "-p", "test_*.py", "-v"])

    if run(["bash", "-c", "command -v helm"], check=False).returncode != 0:
        raise Phase7MonitorError("helm is required for local monitor chart validation and is not available on this runner.")

    with tempfile.TemporaryDirectory(prefix="phase7-monitor-dry-run-") as tmp:
        registry_path = os.path.join(tmp, "goldengate-deployments.yaml")
        run([sys.executable, str(DEPLOYMENT_MODEL_TOOL), "--environment", environment, "registry", "--output", registry_path])
        print("Generated registry:")
        with open(registry_path) as f:
            print(f.read())

        staged_chart = os.path.join(tmp, "goldengate-monitor")
        shutil.copytree(MONITOR_CHART_PATH, staged_chart)
        files_dir = os.path.join(staged_chart, "files")
        os.makedirs(files_dir, exist_ok=True)
        shutil.copy(registry_path, os.path.join(files_dir, "goldengate-deployments.yaml"))

        monitor_role_arn = require_env("MONITOR_ROLE_ARN")
        monitor_namespace = require_env("MONITOR_NAMESPACE")
        common_set_args = [
            "--set", "image.repository=example.invalid/goldengate-monitor",
            "--set", "image.tag=dry-run",
            "--set", f"serviceAccount.roleArn={monitor_role_arn}",
        ]

        run(["helm", "lint", staged_chart, *common_set_args])

        values_file = REPO_ROOT / "envs" / environment / "goldengate-monitor" / "values.yaml"
        template_proc = run(["helm", "template", "gg-monitor", staged_chart,
                              "--namespace", monitor_namespace,
                              "-f", str(values_file),
                              *common_set_args])
        with open(os.path.join(tmp, "gg-monitor-dry-run.yaml"), "w") as f:
            f.write(template_proc.stdout)

    print("OK: monitor chart lints and renders cleanly (no image build, no ECR push, no Argo CD/Kubernetes mutation).")


# Bounded, read-only kubectl exec HTTP helpers -- never shell=True, never blindly print raw stderr/response bodies.

def _kubectl_exec_http_status(pod_name, namespace, path, timeout_seconds):
    """Bounded in-pod HTTP GET via `kubectl exec ... -- python3 -c <program>` (argument array only). A kubectl-level failure (Forbidden/Unauthorized/pod gone/timeout) is NEVER treated as HTTP 0 success -- it fails this command closed immediately with a sanitized (bounded) diagnostic. A successful kubectl exec must yield EXACTLY one integer output line (the embedded program either prints the real HTTP status, an HTTPError's status code, or 0 for any other in-pod request failure) -- malformed/non-integer/multi-line output fails closed too, never silently coerced to a default status. The OUTER kubectl exec call itself is wall-clock bounded (timeout_seconds + _KUBECTL_EXEC_OUTER_TIMEOUT_BUFFER_SECONDS) so Kubernetes API/auth/SPDY-WebSocket negotiation stalling BEFORE the in-pod urllib timeout is ever reached cannot hang this call indefinitely -- an outer timeout is treated exactly like any other kubectl exec failure (non-zero returncode) and raises Phase7MonitorError here, never a bare subprocess.TimeoutExpired traceback."""
    program = (
        "import urllib.request, urllib.error\n"
        "try:\n"
        f"    print(urllib.request.urlopen('http://127.0.0.1:8080{path}', timeout={timeout_seconds}).status)\n"
        "except urllib.error.HTTPError as exc:\n"
        "    print(exc.code)\n"
        "except Exception:\n"
        "    print(0)\n"
    )
    proc = run(["kubectl", "exec", pod_name, "-n", namespace, "--", "python3", "-c", program], check=False,
               timeout_seconds=timeout_seconds + _KUBECTL_EXEC_OUTER_TIMEOUT_BUFFER_SECONDS)
    if proc.returncode != 0:
        raise Phase7MonitorError(f"kubectl exec into pod {pod_name} in namespace {namespace} for GET {path} failed (exit {proc.returncode}); sanitized diagnostic: {_sanitize_tail(proc.stderr)}")
    lines = [line for line in proc.stdout.splitlines() if line.strip() != ""]
    if len(lines) != 1:
        raise Phase7MonitorError(f"kubectl exec into pod {pod_name} for GET {path} returned {len(lines)} output line(s), expected exactly 1 (malformed health-check output).")
    try:
        return int(lines[0].strip())
    except ValueError:
        raise Phase7MonitorError(f"kubectl exec into pod {pod_name} for GET {path} returned non-integer output {lines[0]!r} (malformed health-check output).")


def _try_fetch_api_processes(pod_name, namespace, timeout_seconds=5):
    """Bounded, read-only fetch of GET /api/processes through pod_name (kubectl exec). Returns (True, parsed_json_doc) on success, or (False, sanitized_error_text) on ANY failure (kubectl exec failure, non-JSON output) -- never raises, so a bounded-retry caller can distinguish 'transient fetch failure, keep polling' from a hard failure elsewhere. The sanitized_error_text is bounded/truncated -- never a raw unbounded kubectl/API server diagnostic. The OUTER kubectl exec call itself is wall-clock bounded (timeout_seconds + _KUBECTL_EXEC_OUTER_TIMEOUT_BUFFER_SECONDS) -- a stall before the in-pod urllib timeout is ever reached (API/auth/SPDY negotiation, kubectl itself hanging) surfaces as an ordinary non-zero kubectl exec failure here, never an unbounded hang or a bare traceback; callers that hard-fail on this (replication-monitor acceptance) or bounded-retry it (end-to-end acceptance) both get correct behavior automatically."""
    program = f"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/api/processes', timeout={timeout_seconds}).read().decode('utf-8'))"
    proc = run(["kubectl", "exec", pod_name, "-n", namespace, "--", "python3", "-c", program], check=False,
               timeout_seconds=timeout_seconds + _KUBECTL_EXEC_OUTER_TIMEOUT_BUFFER_SECONDS)
    if proc.returncode != 0:
        return False, f"kubectl exec exit {proc.returncode}; sanitized diagnostic: {_sanitize_tail(proc.stderr)}"
    try:
        return True, json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return False, f"GET /api/processes did not return valid JSON: {exc}"


def _fetch_api_processes_or_fail(pod_name, namespace):
    ok, result = _try_fetch_api_processes(pod_name, namespace)
    if not ok:
        raise Phase7MonitorError(f"could not fetch /api/processes from pod {pod_name} in namespace {namespace}: {result}")
    return result


# Phase 7D: strict monitor acceptance (validate_monitor_ready) -- reuses automation/orchestration/monitor_acceptance.py for BOTH the structural-only pass (selects the ownership-chain-verified Ready pod) and the final pass (folds in /healthz+/readyz observed by THIS module against that exact pod). Never independently selects another pod, never a raw label-selector shortcut.

def cmd_strict_acceptance(args):
    environment = require_environment_arg(args.environment)
    _connect_to_eks()

    with tempfile.TemporaryDirectory(prefix="phase7-monitor-acceptance-") as tmp:
        registry_path = os.path.join(tmp, "goldengate-deployments.yaml")
        run([sys.executable, str(DEPLOYMENT_MODEL_TOOL), "--environment", environment, "registry", "--output", registry_path])

        common_args = [
            sys.executable, str(MONITOR_ACCEPTANCE_TOOL),
            "--environment", environment,
            "--expected-image-repository", args.expected_image_repository,
            "--expected-image-tag", args.expected_image_tag,
            "--expected-chart-version", args.expected_chart_version,
            "--expected-cloudwatch-publish-enabled", "true" if args.expected_cloudwatch_publish_enabled else "false",
            "--registry-file", registry_path,
        ]

        structural_proc = run(common_args, check=False)
        if structural_proc.stderr:
            print(structural_proc.stderr, file=sys.stderr)
        if structural_proc.returncode != 0:
            raise Phase7MonitorError("the GoldenGate monitor acceptance classifier could not evaluate the shared monitor (configuration or inspection error) -- see diagnostics above.")
        _print_proc_stdout(structural_proc)
        try:
            structural_result = json.loads(structural_proc.stdout)
        except json.JSONDecodeError as exc:
            raise Phase7MonitorError(f"the GoldenGate monitor acceptance classifier produced unparseable output: {exc}") from exc

        structural_state = structural_result.get("state")
        print(f"GoldenGate monitor structural acceptance state: {structural_state}")
        if structural_state != "HEALTHY":
            raise Phase7MonitorError(f"GoldenGate monitor structural acceptance state is {structural_state}, expected HEALTHY. Never proceeding to /healthz or /readyz checks against an unverified/absent pod.")

        pod_name = (structural_result.get("checks") or {}).get("ready_pod_name")
        if not pod_name:
            raise Phase7MonitorError("no verified Ready pod name was returned by structural acceptance.")
        print(f"Pod: {pod_name}")

        monitor_namespace = require_env("MONITOR_NAMESPACE")
        healthz_status = _kubectl_exec_http_status(pod_name, monitor_namespace, "/healthz", timeout_seconds=3)
        readyz_status = _kubectl_exec_http_status(pod_name, monitor_namespace, "/readyz", timeout_seconds=5)
        print(f"pod/{pod_name} /healthz -> {healthz_status}, /readyz -> {readyz_status}")

        final_args = common_args + ["--healthz-status", str(healthz_status), "--readyz-status", str(readyz_status)]
        final_proc = run(final_args, check=False)
        if final_proc.stderr:
            print(final_proc.stderr, file=sys.stderr)
        if final_proc.returncode != 0:
            raise Phase7MonitorError("the GoldenGate monitor acceptance classifier could not evaluate the shared monitor on the final pass (configuration or inspection error) -- see diagnostics above.")
        _print_proc_stdout(final_proc)
        try:
            final_result = json.loads(final_proc.stdout)
        except json.JSONDecodeError as exc:
            raise Phase7MonitorError(f"the GoldenGate monitor acceptance classifier (final pass) produced unparseable output: {exc}") from exc

        final_state = final_result.get("state")
        print(f"GoldenGate monitor final acceptance state: {final_state}")
        if final_state != "HEALTHY":
            raise Phase7MonitorError(f"GoldenGate monitor final acceptance state is {final_state}, expected HEALTHY. MAIN cannot claim the monitor is ready while it is not fully healthy (structural shape, canonical registry equality, /healthz, and /readyz all gate this).")

        write_github_output([("state", "HEALTHY"), ("ready_pod_name", pod_name)])
        print("OK: the shared GoldenGate monitor is HEALTHY (structural acceptance + /healthz + /readyz).")


# Replication pipeline discovery -- the SAME automation/goldengate-deployment-model.py CLI used everywhere else, never a second independent parser of runtime descriptor YAML. Deterministically recomputed by every caller (never accepted as a caller-supplied argument), matching automation/phases/phase6/phase6_replication.py's own established discovery philosophy.

def _discover_replication_pipelines(environment):
    proc = run([sys.executable, str(DEPLOYMENT_MODEL_TOOL), "--environment", environment, "replication-pipelines"])
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def cmd_list_replication_pipelines(args):
    """No AWS/EKS/kubectl access -- purely local canonical deployment-model discovery, invoked BEFORE any AWS credential is configured so a zero-pipeline run never needs one. Writes exactly one fixed GitHub output: has_pipelines=true|false."""
    environment = require_environment_arg(args.environment)
    pipelines = _discover_replication_pipelines(environment)
    if pipelines:
        print(f"Enabled replication pipelines ({len(pipelines)}): {', '.join(pipelines)}")
    else:
        print("No enabled replication pipelines.")
    write_github_output([("has_pipelines", "true" if pipelines else "false")])


def _load_canonical_replication_plan(environment, pipeline_id):
    """Obtains the CURRENT canonical replication plan through the ONE existing deployment-model CLI (automation/goldengate-deployment-model.py replication-plan <pipeline_id>) -- never an independent descriptor YAML parser."""
    proc = run([sys.executable, str(DEPLOYMENT_MODEL_TOOL), "--environment", environment, "replication-plan", pipeline_id])
    try:
        plan = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Phase7MonitorError(f"canonical replication-plan output for pipeline {pipeline_id!r} is not valid JSON: {exc}") from exc
    if not isinstance(plan, dict):
        raise Phase7MonitorError(f"canonical replication-plan output for pipeline {pipeline_id!r} is a {type(plan).__name__}, expected a JSON object.")
    if plan.get("pipelineId") != pipeline_id:
        raise Phase7MonitorError(f"canonical replication-plan pipelineId {plan.get('pipelineId')!r} does not match the requested pipeline_id {pipeline_id!r}.")
    return plan


def _api_processes_deployments_by_name(api_doc):
    """Strictly validates GET /api/processes' top-level shape and every deployment row -- a malformed row (not an object, or missing/empty/non-string/duplicate deploymentName) FAILS CLOSED here; it is never silently dropped as though it simply did not exist."""
    raw_deployments = api_doc.get("deployments") if isinstance(api_doc, dict) else None
    if not isinstance(raw_deployments, list):
        raise Phase7MonitorError("GET /api/processes response is missing a 'deployments' list.")
    deployments_by_name = {}
    for index, entry in enumerate(raw_deployments):
        if not isinstance(entry, dict):
            raise Phase7MonitorError(f"/api/processes deployment row #{index} is not an object: {entry!r}")
        deployment_name = entry.get("deploymentName")
        if not isinstance(deployment_name, str) or not deployment_name:
            raise Phase7MonitorError(f"/api/processes deployment row #{index} has a missing/empty/non-string deploymentName: {deployment_name!r}")
        if deployment_name in deployments_by_name:
            raise Phase7MonitorError(f"/api/processes has duplicate deploymentName {deployment_name!r}")
        deployments_by_name[deployment_name] = entry
    return deployments_by_name


def _validate_process_inventory(dep, deployment_name, pipeline_id):
    """Strictly validates ONE deployment's process inventory BEFORE any expected-process matching is attempted -- a JSON response that is coherent at the top level but carries a malformed processDiscovery/processes/row anywhere is a HARD failure here, never a silently-filtered/emptied inventory. Required contract: (1) processDiscovery, if present, must be a JSON object -- a truthy string/list/number is never allowed to reach a bare .get() call and raise an uncontrolled AttributeError; (2) processes, if present, must be a JSON array -- never coerced to an empty inventory via `x or []`, which would silently accept a malformed non-list value; (3) every process row must itself be a JSON object -- a stray string/number/null/list anywhere in the array fails the WHOLE deployment's inventory, it is never dropped as though it simply were not there; (4) every row's `process` identity must be a non-empty string -- missing/empty/non-string identity fails closed; (5) process identities must be unique within the deployment -- a duplicate identity makes the inventory ambiguous and is rejected outright, never resolved by arbitrarily keeping the first occurrence. Returns (discovery_or_None, {process_name: row}) for the caller's exact-match lookup."""
    discovery = dep.get("processDiscovery")
    if discovery is not None and not isinstance(discovery, dict):
        raise Phase7MonitorError(f"{pipeline_id}: {deployment_name} processDiscovery is a {type(discovery).__name__}, expected a JSON object: {discovery!r}")

    raw_processes = dep.get("processes")
    if raw_processes is None:
        raw_processes = []
    elif not isinstance(raw_processes, list):
        raise Phase7MonitorError(f"{pipeline_id}: {deployment_name} processes is a {type(raw_processes).__name__}, expected a JSON array: {raw_processes!r}")

    rows_by_name = {}
    for index, row in enumerate(raw_processes):
        if not isinstance(row, dict):
            raise Phase7MonitorError(f"{pipeline_id}: {deployment_name} process row #{index} is not an object: {row!r}")
        process_name = row.get("process")
        if not isinstance(process_name, str) or not process_name:
            raise Phase7MonitorError(f"{pipeline_id}: {deployment_name} process row #{index} has a missing/empty/non-string process identity: {process_name!r}")
        if process_name in rows_by_name:
            raise Phase7MonitorError(f"{pipeline_id}: {deployment_name} has duplicate process identity {process_name!r} -- an ambiguous inventory is never resolved by arbitrarily keeping the first occurrence")
        rows_by_name[process_name] = row

    return discovery, rows_by_name


def _require_process(deployments_by_name, deployment_name, process_name, expect_running, pipeline_id):
    """Mirrors the original per-pipeline check's exact fail-fast semantics: the first violated rule raises immediately (a pipeline's Extract/Distribution/Replicat checks stop at the first failure, and no later pipeline is evaluated once one has failed) -- never an aggregate-and-report-all pass. The entire process inventory is strictly validated (see _validate_process_inventory()) before any expected-row matching is attempted."""
    dep = deployments_by_name.get(deployment_name)
    if dep is None:
        raise Phase7MonitorError(f"{pipeline_id}: monitor has no entry for deployment {deployment_name}")
    discovery, rows_by_name = _validate_process_inventory(dep, deployment_name, pipeline_id)
    effective_discovery = discovery if discovery is not None else {}
    if effective_discovery.get("status") != "OK":
        raise Phase7MonitorError(f"{pipeline_id}: {deployment_name} processDiscovery.status is {effective_discovery.get('status')!r}, expected OK")
    row = rows_by_name.get(process_name)
    if row is None:
        raise Phase7MonitorError(f"{pipeline_id}: {deployment_name} has no real process row named {process_name!r}")
    if row.get("stale"):
        raise Phase7MonitorError(f"{pipeline_id}: {deployment_name} process {process_name!r} is stale")
    if row.get("status") == "ABENDED":
        raise Phase7MonitorError(f"{pipeline_id}: {deployment_name} process {process_name!r} is ABENDED")
    if expect_running and row.get("status") != "RUNNING":
        raise Phase7MonitorError(f"{pipeline_id}: {deployment_name} process {process_name!r} expected RUNNING, found {row.get('status')!r}")
    print(f"OK: {pipeline_id}: {deployment_name} process {process_name!r} status={row.get('status')} stale={row.get('stale')}")


def _verify_replication_process_acceptance(environment, pod_name, pipelines):
    monitor_namespace = require_env("MONITOR_NAMESPACE")
    api_doc = _fetch_api_processes_or_fail(pod_name, monitor_namespace)
    deployments_by_name = _api_processes_deployments_by_name(api_doc)

    for pipeline_id in pipelines:
        print(f"::group::Verifying {pipeline_id}")
        plan = _load_canonical_replication_plan(environment, pipeline_id)
        _require_process(deployments_by_name, plan["source"]["deploymentId"], plan["extract"]["name"], plan["extract"]["startOnCreate"], pipeline_id)
        _require_process(deployments_by_name, plan["source"]["deploymentId"], plan["distribution"]["pathName"], plan["distribution"]["startOnCreate"], pipeline_id)
        _require_process(deployments_by_name, plan["target"]["deploymentId"], plan["replicat"]["name"], plan["replicat"]["startOnCreate"], pipeline_id)
        print(f"OK: {pipeline_id}: all expected replication process rows are present, current, and not ABENDED")
        print("::endgroup::")


# Phase 7E: replication-specific monitor acceptance (replication_monitor_acceptance) -- reuses the SAME ownership-chain-verified Ready pod validate_monitor_ready already selected/health-checked; never a second, independent pod selection.

def cmd_replication_monitor_acceptance(args):
    environment = require_environment_arg(args.environment)
    pipelines = _discover_replication_pipelines(environment)
    if not pipelines:
        print("No enabled replication pipeline -- clean no-op: replication-specific monitor acceptance skipped.")
        return
    pod_name = require_pod_name_arg(args.pod_name)
    _connect_to_eks()
    _verify_replication_process_acceptance(environment, pod_name, pipelines)


# Phase 7F: monitor-to-runtime end-to-end acceptance (end_to_end_deployment_acceptance) -- bounded poll, reuses automation/orchestration/end_to_end_acceptance.py as the sole authoritative classifier, reuses the SAME verified Ready pod.

def _require_positive_int(label, value):
    """Semantic range validation, deliberately never relying on argparse's own `type=int` alone (a test or a future caller may construct/override these values directly, bypassing argparse entirely). Rejects non-int types (including bool, which is a subclass of int in Python and must never be silently accepted as a duration), and any value <= 0 -- zero or negative would make the elapsed/timeout bookkeeping in the polling loop below meaningless (a zero interval never advances elapsed, producing an unbounded loop; a zero/negative timeout is immediately-or-already exceeded in a way that defeats the bound's purpose)."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise Phase7MonitorError(f"{label} must be a positive integer, got {value!r}.")
    return value


def cmd_end_to_end_acceptance(args):
    environment = require_environment_arg(args.environment)
    pod_name = require_pod_name_arg(args.pod_name)
    monitor_namespace = require_env("MONITOR_NAMESPACE")
    # Bound validation happens BEFORE any kubectl/sleep/polling/classifier call below -- an invalid bound must never reach the loop, let alone create one that cannot naturally terminate (e.g. interval_seconds=0 would make `elapsed += interval_seconds` never advance).
    timeout_seconds = _require_positive_int("--timeout-seconds", args.timeout_seconds)
    interval_seconds = _require_positive_int("--interval-seconds", args.interval_seconds)
    elapsed = 0

    while True:
        ok, result = _try_fetch_api_processes(pod_name, monitor_namespace)
        if ok:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                json.dump(result, f)
                api_processes_path = f.name
            try:
                proc = run([sys.executable, str(END_TO_END_ACCEPTANCE_TOOL), "--environment", environment, "--api-processes-file", api_processes_path], check=False)
            finally:
                os.unlink(api_processes_path)
            _print_proc_stdout(proc)
            if proc.returncode == 0:
                print("OK: GoldenGate monitor-to-runtime end-to-end acceptance is HEALTHY.")
                return
            print(f"Not yet HEALTHY (elapsed {elapsed}s / {timeout_seconds}s)")
        else:
            print(f"Could not fetch /api/processes from pod {pod_name} this iteration (elapsed {elapsed}s / {timeout_seconds}s); {result}")

        if elapsed >= timeout_seconds:
            raise Phase7MonitorError(f"timed out after {timeout_seconds}s waiting for GoldenGate monitor-to-runtime end-to-end acceptance to become HEALTHY. This is never suppressed -- a persistent problem must surface as a failed workflow.")

        time.sleep(interval_seconds)
        elapsed += interval_seconds


# CLI

def build_parser():
    parser = argparse.ArgumentParser(description="Phase 7 | GoldenGate monitor orchestrator (ownership preflight, local dry-run validation, strict acceptance, replication-specific acceptance, bounded end-to-end acceptance).")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("ensure-kubectl").set_defaults(func=cmd_ensure_kubectl)

    ownership_preflight = subparsers.add_parser("ownership-preflight")
    ownership_preflight.add_argument("--environment", required=True)
    ownership_preflight.set_defaults(func=cmd_ownership_preflight)

    validate_local = subparsers.add_parser("validate-local")
    validate_local.add_argument("--environment", required=True)
    validate_local.set_defaults(func=cmd_validate_local)

    strict_acceptance = subparsers.add_parser("strict-acceptance")
    strict_acceptance.add_argument("--environment", required=True)
    strict_acceptance.add_argument("--expected-image-repository", required=True)
    strict_acceptance.add_argument("--expected-image-tag", required=True)
    strict_acceptance.add_argument("--expected-chart-version", required=True)
    strict_acceptance.add_argument("--expected-cloudwatch-publish-enabled", required=True, choices=["true", "false"])
    strict_acceptance.set_defaults(func=lambda args: cmd_strict_acceptance(_with_bool(args, "expected_cloudwatch_publish_enabled")))

    list_pipelines = subparsers.add_parser("list-replication-pipelines")
    list_pipelines.add_argument("--environment", required=True)
    list_pipelines.set_defaults(func=cmd_list_replication_pipelines)

    replication_monitor_acceptance = subparsers.add_parser("replication-monitor-acceptance")
    replication_monitor_acceptance.add_argument("--environment", required=True)
    replication_monitor_acceptance.add_argument("--pod-name", required=True)
    replication_monitor_acceptance.set_defaults(func=cmd_replication_monitor_acceptance)

    end_to_end_acceptance = subparsers.add_parser("end-to-end-acceptance")
    end_to_end_acceptance.add_argument("--environment", required=True)
    end_to_end_acceptance.add_argument("--pod-name", required=True)
    end_to_end_acceptance.add_argument("--timeout-seconds", type=int, default=DEFAULT_END_TO_END_TIMEOUT_SECONDS)
    end_to_end_acceptance.add_argument("--interval-seconds", type=int, default=DEFAULT_END_TO_END_INTERVAL_SECONDS)
    end_to_end_acceptance.set_defaults(func=cmd_end_to_end_acceptance)

    return parser


def _with_bool(args, attr):
    setattr(args, attr, getattr(args, attr) == "true")
    return args


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except Phase7MonitorError as exc:
        print(f"FAIL: {exc}")
        return 1
    except subprocess.SubprocessError as exc:
        print(f"FAIL: subprocess execution error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
