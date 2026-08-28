#!/usr/bin/env python3
"""Phase 5A-5D | GoldenGate Runtime lifecycle orchestration entrypoint for runtime_ownership_preflight/build_publish_and_deploy/delete_removed_argocd_applications/validate_active_runtimes in .github/workflows/00-main-goldengate-orchestrator.yaml; a thin orchestration/service layer that never reimplements environment.yaml parsing (owned by automation/goldengate-environment.py) or descriptor resolution (owned by automation/goldengate-deployment-model.py), and reuses, never duplicates, automation/phases/phase5/runtime_state.py (pre-reconciliation ownership-safety preflight, and post-removal absence proof) and automation/phases/phase5/runtime_acceptance.py (strict post-reconciliation acceptance) as separate subprocess-invoked classifiers. Non-secret Helm/Argo deployment metadata is threaded between subcommands through JSON state files under the runner temp directory instead of large inline shell blocks; AWS/Kubernetes credentials are never written to those state files, to $GITHUB_OUTPUT, or to $GITHUB_ENV."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

ENVIRONMENT_TOOL = REPO_ROOT / "automation" / "goldengate-environment.py"
DEPLOYMENT_MODEL_TOOL = REPO_ROOT / "automation" / "goldengate-deployment-model.py"
RUNTIME_STATE_TOOL = REPO_ROOT / "automation" / "phases" / "phase5" / "runtime_state.py"
RUNTIME_ACCEPTANCE_TOOL = REPO_ROOT / "automation" / "phases" / "phase5" / "runtime_acceptance.py"

HELM_OCI_NAMESPACE = "helm"
CHART_NAME = "goldengate"
HELM_CHART_PATH = REPO_ROOT / "helm" / "goldengate"
HELM_ECR_REPOSITORY = f"{HELM_OCI_NAMESPACE}/{CHART_NAME}"

ARGOCD_ECR_STATEMENT_SID = "AllowArgocdEksRolePullGoldengateHelmChart"
REPOSITORY_PULL_ACTIONS = [
    "ecr:BatchCheckLayerAvailability",
    "ecr:BatchGetImage",
    "ecr:GetDownloadUrlForLayer",
    "ecr:DescribeImages",
    "ecr:DescribeRepositories",
]

INIT_CONTAINER_NAME = "prepare-u02-permissions"
FORBIDDEN_CONTAINER_SUBSTRINGS = ("goldengate-observer", "utility-sidecar", "fluent-bit", "fluentbit")

# Never a real AWS resource -- syntactically valid only so Helm rendering/validation has something to compare against on a Validate (deploy=false) run; never committed, never sent to Argo CD/AWS.
EFS_DRY_RUN_PLACEHOLDER = "fs-0dead0000000beef0"

_DEPLOYMENT_ID_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?\Z")
_SAFE_ENVIRONMENT_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*\Z")

RECONCILE_ALLOWED_STATE_KEYS = frozenset({
    "environment", "deployment_id", "deployment_model", "deploy", "values_file", "target_namespace",
    "release_name", "argocd_app_name", "temp_chart_path", "chart_version", "helm_ecr_repository",
    "helm_push_url", "helm_chart_ref", "rendered_manifest", "package_path", "pulled_directory",
    "admin_secret_name", "tls_secret_name", "runtime_service_account_name", "image_repository",
    "image_repository_name", "image_tag", "image_digest", "dns_domain", "alb_group_name",
    "certificate_arn", "efs_mode", "efs_file_system_id_declared", "efs_creation_token", "resolved_efs_id",
})

REMOVAL_ALLOWED_STATE_KEYS = frozenset({
    "environment", "deployment_id", "deployment_model", "efs_mode", "reason", "runtime_namespace",
    "argocd_namespace", "argocd_app_name", "ownership_state", "application_found", "footprint_found",
})


class Phase5Error(Exception):
    """A fail-closed Phase 5 Runtime error; main() reports it and exits non-zero."""


# Input validation (before any value is ever used in a path/name)

def require_environment_arg(environment):
    """Defense-in-depth path-safety check only -- the canonical accept/reject authority for an environment name remains automation/goldengate-environment.py itself (invoked below), never re-implemented here."""
    if not isinstance(environment, str) or not _SAFE_ENVIRONMENT_RE.match(environment):
        raise Phase5Error(f"environment {environment!r} is not a safe identifier; refusing to use it in a filesystem path.")
    return environment


def require_deployment_id_arg(deployment_id):
    """The current lowercase DNS-style contract: ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ -- rejects '../', '/', '\\\\', and any embedded CR/LF/NUL by construction (none of those characters are in the accepted character class)."""
    if not isinstance(deployment_id, str) or not _DEPLOYMENT_ID_RE.match(deployment_id):
        raise Phase5Error(f"Invalid deployment_id: {deployment_id!r}. Use lowercase letters, numbers, and hyphens only. Example: gg-oracle-payments-01")
    return deployment_id


def require_env(name):
    value = os.environ.get(name, "")
    if not value:
        raise Phase5Error(f"{name} is empty; canonical environment configuration must be loaded before this step.")
    return value


def _canonical_argocd_app_name(environment, deployment_id):
    """ONE canonical derivation of the runtime Argo CD Application name -- reused by prepare-deployment, prepare-removal, and both the reconcile-state/removal-state identity validators below; never independently duplicated. Strips exactly one leading "gg-" from deployment_id (if present) before composing goldengate-<environment>-<suffix>, e.g. gg-postgresql-repltest-01 -> goldengate-dev-postgresql-repltest-01, gg-gg-test -> goldengate-dev-gg-test."""
    app_suffix = deployment_id[len("gg-"):] if deployment_id.startswith("gg-") else deployment_id
    return f"goldengate-{environment}-{app_suffix}"


def _canonical_chart_version(deployment_id):
    """ONE canonical chart-version derivation -- reused by prepare-deployment, reconcile-state identity validation, package/artifact validation, publish-chart, and reconcile-runtime; never independently duplicated. deployment_id is included because matrix jobs share the same GITHUB_RUN_NUMBER and would otherwise collide. Deliberately excludes github.run_attempt (out of scope for this task) -- preserves the current chart-version contract exactly."""
    run_number = require_env("GITHUB_RUN_NUMBER")
    return f"0.1.{run_number}-{deployment_id}"


def _canonical_temp_chart_path(deployment_id):
    """ONE canonical local temp-chart-directory derivation, relative to REPO_ROOT -- reused by prepare-deployment, reconcile-state identity validation, and packaging."""
    return f"work/charts/{deployment_id}/goldengate"


def _canonical_rendered_manifest_path(deployment_id):
    """ONE canonical local rendered-manifest-file derivation, relative to REPO_ROOT."""
    return f"rendered/{deployment_id}.yaml"


def _canonical_package_path(chart_version):
    """ONE canonical local packaged-chart-archive derivation, relative to REPO_ROOT -- reused by packaging and by publish-chart's own package-path/containment validation. Never an arbitrary state-controlled filesystem path."""
    return f"packaged/{CHART_NAME}-{chart_version}.tgz"


PULLED_DIRECTORY = "pulled"


def _require_literal_bool_state_value(state, key):
    """Never bool(state.get(key)) -- bool("false")/bool(0 or non-empty string) all coerce unpredictably in Python. Requires the persisted JSON value to already be a literal boolean; anything else (string, int, None, missing) fails closed."""
    if key not in state:
        raise Phase5Error(f"Phase 5 state is missing required literal-boolean key {key!r}.")
    value = state[key]
    if not isinstance(value, bool):
        raise Phase5Error(f"Phase 5 state key {key!r} is {value!r} ({type(value).__name__}), expected a literal boolean.")
    return value


def _parse_bool_arg(value, name):
    if value in ("true", "True"):
        return True
    if value in ("false", "False"):
        return False
    raise Phase5Error(f"{name} must be exactly 'true' or 'false', got {value!r}.")


# Phase 5 state files (two independent JSON documents: reconcile vs. removal; never shared/conflated)

def default_state_path(name):
    runner_temp = os.environ.get("RUNNER_TEMP")
    base = Path(runner_temp) if runner_temp else Path(os.environ.get("TMPDIR", "/tmp"))
    return base / f"goldengate-phase5-{name}.json"


def load_state(state_path):
    if not state_path.exists():
        return {}
    try:
        with state_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise Phase5Error(f"Phase 5 state file {state_path} is unreadable/malformed: {exc}") from exc
    if not isinstance(data, dict):
        raise Phase5Error(f"Phase 5 state file {state_path} did not contain a JSON object.")
    return data


def save_state(state_path, state):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, sort_keys=True, indent=2)
        f.write("\n")
    tmp_path.replace(state_path)


def update_state(state_path, updates, allowed_keys):
    disallowed = sorted(set(updates) - allowed_keys)
    if disallowed:
        raise Phase5Error(f"refusing to write disallowed Phase 5 state key(s) {disallowed} -- state may only ever contain non-secret deployment metadata: {sorted(allowed_keys)}")
    state = load_state(state_path)
    state.update(updates)
    save_state(state_path, state)
    return state


def require_state_value(state, key):
    if key not in state or state[key] in (None, ""):
        raise Phase5Error(f"Phase 5 state is missing required key {key!r}; an earlier step did not complete.")
    return state[key]


def _validate_reconcile_state_identity(state, environment, deployment_id):
    """Binds a persisted reconcile-state JSON document back to the CURRENT CLI environment/deployment_id plus canonical Phase 5 naming/config rules -- applied BEFORE any AWS/Kubernetes mutation from a reconcile-state consumer (resolve-live-inputs, publish-chart, reconcile-runtime), so a state file left over from (or substituted from) a different matrix runtime can never control this runtime's mutation. Deliberately never compares mutable runtime RESULTS (image_digest, resolved_efs_id) against independently reconstructed values -- those already have their own authoritative resolution/verification checks. Returns the validated literal `deploy` boolean."""
    if not isinstance(state, dict):
        raise Phase5Error(f"Phase 5 reconcile state is a {type(state).__name__}, expected a JSON object.")

    def _require_exact(key, expected):
        actual = state.get(key)
        if actual != expected:
            raise Phase5Error(f"Phase 5 reconcile state {key}={actual!r} does not match the current matrix runtime (expected {expected!r} for environment={environment!r}, deployment_id={deployment_id!r}) -- refusing to let a mismatched/stale state file control this mutation.")
        return actual

    _require_exact("environment", environment)
    _require_exact("deployment_id", deployment_id)
    _require_exact("deployment_model", "singleRuntime")

    deploy = _require_literal_bool_state_value(state, "deploy")

    _require_exact("release_name", deployment_id)
    _require_exact("argocd_app_name", _canonical_argocd_app_name(environment, deployment_id))
    _require_exact("target_namespace", require_env("RUNTIME_NAMESPACE"))
    _require_exact("values_file", f"envs/{environment}/{deployment_id}/values.yaml")
    _require_exact("helm_ecr_repository", HELM_ECR_REPOSITORY)

    ecr_registry = require_env("ECR_REGISTRY")
    _require_exact("helm_push_url", f"oci://{ecr_registry}/{HELM_OCI_NAMESPACE}")
    _require_exact("helm_chart_ref", f"oci://{ecr_registry}/{HELM_ECR_REPOSITORY}")

    # Both are written by prepare-deployment and are therefore part of the canonical static state contract from that point on -- never left optional. The chart-version/local-path artifact contract these feed is enforced separately by _validate_package_path_and_containment()/_validate_packaged_chart_contents().
    _require_exact("chart_version", _canonical_chart_version(deployment_id))
    _require_exact("temp_chart_path", _canonical_temp_chart_path(deployment_id))

    return deploy


def _validate_removal_state_identity(state, environment, deployment_id):
    """Binds a persisted removal-state JSON document back to the CURRENT CLI environment/deployment_id plus canonical Phase 5 naming/config rules -- applied BEFORE any cluster connection or mutating call from a removal-state consumer (removal-preflight, remove-runtime, post-delete-acceptance), so a stale/cross-runtime removal state file can never control this runtime's deletion. Returns the validated efs_mode (never a corrupted one) for retained-PVC-hint decisions."""
    if not isinstance(state, dict):
        raise Phase5Error(f"Phase 5 removal state is a {type(state).__name__}, expected a JSON object.")

    def _require_exact(key, expected):
        actual = state.get(key)
        if actual != expected:
            raise Phase5Error(f"Phase 5 removal state {key}={actual!r} does not match the current matrix runtime (expected {expected!r} for environment={environment!r}, deployment_id={deployment_id!r}) -- refusing to let a mismatched/stale state file control this mutation.")
        return actual

    _require_exact("environment", environment)
    _require_exact("deployment_id", deployment_id)
    _require_exact("deployment_model", "singleRuntime")

    reason = state.get("reason")
    if reason not in ("deployment-disabled", "physical-removal"):
        raise Phase5Error(f"Phase 5 removal state reason is {reason!r}, expected 'deployment-disabled' or 'physical-removal'.")

    efs_mode = state.get("efs_mode")
    if efs_mode not in ("", "existing", "managed"):
        raise Phase5Error(f"Phase 5 removal state efs_mode is {efs_mode!r}, expected '', 'existing', or 'managed'.")

    if reason == "physical-removal" and efs_mode == "managed":
        raise Phase5Error(f"Phase 5 removal state is reason=physical-removal with efs_mode=managed for {deployment_id} -- refusing before any cluster connection/mutation. Physically removing the descriptor for a MANAGED durable EFS filesystem is unsafe; Terraform remains the sole managed-EFS lifecycle owner.")

    _require_exact("runtime_namespace", require_env("RUNTIME_NAMESPACE"))
    _require_exact("argocd_namespace", require_env("ARGOCD_NAMESPACE"))
    _require_exact("argocd_app_name", _canonical_argocd_app_name(environment, deployment_id))

    return efs_mode


RECONCILE_COMMANDS = frozenset({
    "prepare-deployment", "resolve-live-inputs", "validate-local", "publish-chart",
    "validate-cluster-prerequisites", "reconcile-runtime", "post-deploy-diagnostics",
})
REMOVAL_COMMANDS = frozenset({"prepare-removal", "removal-preflight", "remove-runtime", "post-delete-acceptance"})


def state_path_for(command, mode, override):
    if override is not None:
        return override
    if command in REMOVAL_COMMANDS:
        return default_state_path("removal-state")
    if command == "summary":
        return default_state_path("removal-state" if mode == "remove" else "runtime-state")
    return default_state_path("runtime-state")


# GitHub Actions special-file helpers

def write_github_output(pairs, output_path=None):
    """Appends name=value lines to $GITHUB_OUTPUT. Output names/values here are always fixed literals or a program-controlled enum (e.g. ownership state), never caller-controlled free-form text. No-op (never raises) when GITHUB_OUTPUT is unset."""
    path = output_path if output_path is not None else os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for name, value in pairs:
            f.write(f"{name}={value}\n")


def write_step_summary(text, summary_path=None):
    path = summary_path if summary_path is not None else os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        print(text)
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)
        if not text.endswith("\n"):
            f.write("\n")


# Safe subprocess execution -- argument arrays only, never shell=True, never a shell pipeline.

def run(argv, env=None, cwd=None, check=True, capture_output=True, input_text=None):
    """Runs argv as an argument array. Fails closed with the tool's own stderr/stdout on a non-zero exit when check=True. input_text feeds the subprocess's stdin directly (e.g. an ECR password, a kubectl apply manifest), never via a shell pipe."""
    proc = subprocess.run(
        argv,
        cwd=cwd or REPO_ROOT,
        env=env,
        capture_output=capture_output,
        text=True,
        input=input_text,
    )
    if check and proc.returncode != 0:
        raise Phase5Error(f"{' '.join(str(a) for a in argv)} failed (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
    return proc


def _kubectl_get_jsonpath(resource, name, namespace, jsonpath):
    proc = run(["kubectl", "get", resource, name, "-n", namespace, "-o", f"jsonpath={jsonpath}"], check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout


def _connect_to_eks():
    aws_region = require_env("AWS_REGION")
    eks_cluster_name = require_env("EKS_CLUSTER_NAME")
    eks_deploy_role_arn = require_env("EKS_DEPLOY_ROLE_ARN")
    run(["aws", "eks", "update-kubeconfig", "--region", aws_region, "--name", eks_cluster_name,
         "--role-arn", eks_deploy_role_arn, "--assume-role-arn", eks_deploy_role_arn])
    return aws_region, eks_deploy_role_arn


# Tool installation (never requires AWS credentials)

def _ensure_kubectl():
    if run(["bash", "-c", "command -v kubectl"], check=False).returncode == 0:
        run(["kubectl", "version", "--client=true"])
        return
    kubectl_version = "v1.35.0"
    machine = run(["uname", "-m"]).stdout.strip()
    arch_map = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
    if machine not in arch_map:
        raise Phase5Error(f"Unsupported architecture for kubectl: {machine}")
    kubectl_arch = arch_map[machine]
    run(["curl", "-fsSL", f"https://dl.k8s.io/release/{kubectl_version}/bin/linux/{kubectl_arch}/kubectl", "-o", "/tmp/kubectl"])
    run(["sudo", "mv", "/tmp/kubectl", "/usr/local/bin/kubectl"])
    run(["sudo", "chmod", "+x", "/usr/local/bin/kubectl"])
    run(["kubectl", "version", "--client=true"])


def _ensure_helm():
    if run(["bash", "-c", "command -v helm"], check=False).returncode == 0:
        run(["helm", "version", "--short"])
        return
    helm_version = "v3.15.4"
    machine = run(["uname", "-m"]).stdout.strip()
    arch_map = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
    if machine not in arch_map:
        raise Phase5Error(f"Unsupported architecture for Helm: {machine}")
    helm_arch = arch_map[machine]
    run(["curl", "-fsSL", f"https://get.helm.sh/helm-{helm_version}-linux-{helm_arch}.tar.gz", "-o", "/tmp/helm.tar.gz"])
    run(["tar", "-zxvf", "/tmp/helm.tar.gz", "-C", "/tmp"])
    run(["sudo", "mv", f"/tmp/linux-{helm_arch}/helm", "/usr/local/bin/helm"])
    run(["sudo", "chmod", "+x", "/usr/local/bin/helm"])
    run(["helm", "version", "--short"])


def cmd_ensure_kubectl(args):
    _ensure_kubectl()
    print("OK: kubectl is available.")


def cmd_ensure_deploy_tools(args):
    _ensure_helm()
    _ensure_kubectl()
    print("OK: Helm and kubectl are available.")


# Phase 5A: runtime ownership preflight (runtime_ownership_preflight)

def cmd_ownership_preflight(args):
    environment = require_environment_arg(args.environment)
    deployment_id = require_deployment_id_arg(args.deployment_id)
    _connect_to_eks()

    proc = run([sys.executable, str(RUNTIME_STATE_TOOL), "--environment", environment, "--deployment-id", deployment_id], check=False)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    if proc.returncode != 0:
        raise Phase5Error(f"the GoldenGate runtime ownership classifier could not classify {deployment_id} (configuration or inspection error, not ABSENT) -- see diagnostics above.")
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Phase5Error(f"the GoldenGate runtime ownership classifier produced unparseable output: {exc}") from exc

    state = result.get("state")
    if state not in ("ABSENT", "OWNED", "BROKEN"):
        raise Phase5Error(f"the GoldenGate runtime ownership classifier produced an unrecognized state {state!r}; refusing to proceed.")
    if state == "BROKEN":
        raise Phase5Error(f"GoldenGate runtime ownership-safety state for {deployment_id} is BROKEN -- an existing footprint does not clearly belong to this deployment. This is not auto-repaired here -- investigate the diagnostics above before re-running.")

    write_github_output([("state", state)])
    print(f"OK: GoldenGate runtime ownership-safety state for {deployment_id} is {state}.")


# Deployment-model reuse (never a second independent descriptor parser)

def _describe_deployment_json(environment, deployment_id):
    proc = run([sys.executable, str(DEPLOYMENT_MODEL_TOOL), "--environment", environment, "describe", deployment_id], check=False)
    if proc.returncode != 0:
        raise Phase5Error(f"could not resolve deployment identity for {deployment_id}: {((proc.stdout or '') + (proc.stderr or '')).strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Phase5Error(f"goldengate-deployment-model.py describe returned malformed JSON: {exc}") from exc


# EFS identity resolution -- ONE reusable helper for both Phase 5B (reconcile) and Phase 5D (acceptance); no EFS create/update/delete anywhere in Phase 5 -- Terraform remains the sole managed-EFS lifecycle owner.

def _extract_account_id_from_role_arn(role_arn):
    match = re.match(r"^arn:aws:iam::(\d{12}):role/.*\Z", role_arn or "")
    return match.group(1) if match else None


def _assume_role_credentials(role_arn, aws_region, session_name):
    """Returns a short-lived AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/AWS_SESSION_TOKEN dict. The caller must confine it to a single subprocess call's environment overlay (see _aws_with_credentials) -- it must never be written to state, $GITHUB_ENV, $GITHUB_OUTPUT, or a log line."""
    proc = run(["aws", "sts", "assume-role", "--role-arn", role_arn, "--role-session-name", session_name, "--duration-seconds", "900", "--output", "json"])
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Phase5Error(f"aws sts assume-role returned malformed JSON: {exc}") from exc
    creds = data.get("Credentials") or {}
    access_key_id, secret_access_key, session_token = creds.get("AccessKeyId"), creds.get("SecretAccessKey"), creds.get("SessionToken")
    if not (access_key_id and secret_access_key and session_token):
        raise Phase5Error("aws sts assume-role did not return complete temporary credentials.")
    return {"AWS_ACCESS_KEY_ID": access_key_id, "AWS_SECRET_ACCESS_KEY": secret_access_key, "AWS_SESSION_TOKEN": session_token}


def _aws_with_credentials(argv, credentials_overlay):
    """Runs argv with a temporary credentials overlay confined to THIS subprocess call only -- copies os.environ (never mutates it in place), so the calling process's own build-account credentials are untouched once this call returns."""
    env = dict(os.environ)
    env.update(credentials_overlay)
    return run(argv, env=env)


def _resolve_efs_filesystem_id(efs_mode, efs_file_system_id_declared, efs_creation_token, deploy, environment, deployment_id, eks_deploy_role_arn, aws_region):
    if not efs_mode:
        print("EFS ID source: not applicable (persistence.efs is not in use for this deployment).")
        return ""

    if efs_mode == "existing":
        print("EFS ID source: existing descriptor.")
        print(f"Resolved EFS filesystem ID: {efs_file_system_id_declared}")
        return efs_file_system_id_declared

    if not deploy:
        print("EFS ID source: dry-run placeholder (deploy=false; not a real AWS resource, never committed, never sent to Argo CD/AWS).")
        return EFS_DRY_RUN_PLACEHOLDER

    if not efs_creation_token:
        raise Phase5Error("persistence.efs.mode=managed but no EFS creation token was resolved from the deployment model.")

    expected_workload_account_id = _extract_account_id_from_role_arn(eks_deploy_role_arn)
    if not expected_workload_account_id:
        raise Phase5Error("could not extract a 12-digit account ID from EKS_DEPLOY_ROLE_ARN; refusing to resolve an EFS filesystem ID without a provable expected workload account.")

    session_name = f"gg-efs-resolve-{os.environ.get('GITHUB_RUN_ID', 'local')}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
    credentials = _assume_role_credentials(eks_deploy_role_arn, aws_region, session_name)

    actual_account = _aws_with_credentials(["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text", "--region", aws_region], credentials).stdout.strip()
    if actual_account != expected_workload_account_id:
        raise Phase5Error(f"assumed role resolved to account {actual_account}, expected the GoldenGate workload account {expected_workload_account_id}.")

    describe_proc = _aws_with_credentials(["aws", "efs", "describe-file-systems", "--creation-token", efs_creation_token, "--region", aws_region, "--output", "json"], credentials)
    try:
        described = json.loads(describe_proc.stdout)
    except json.JSONDecodeError as exc:
        raise Phase5Error(f"aws efs describe-file-systems returned malformed JSON: {exc}") from exc

    filesystems = described.get("FileSystems") or []
    if len(filesystems) != 1:
        raise Phase5Error(f"expected exactly one EFS filesystem for creation token {efs_creation_token}, found {len(filesystems)}. Refusing to list-and-guess.")

    fs = filesystems[0]
    if fs.get("LifeCycleState") != "available":
        raise Phase5Error(f"the managed EFS filesystem for creation token {efs_creation_token} is in lifecycle state {fs.get('LifeCycleState')}, not available.")

    tags = {t.get("Key"): t.get("Value") for t in fs.get("Tags", [])}
    if not (tags.get("ManagedBy") == "goldengate-eks-app" and tags.get("GoldenGateDeploymentId") == deployment_id
            and tags.get("GoldenGateEnvironment") == environment and tags.get("GoldenGateStorage") == "u02"):
        raise Phase5Error(f"the EFS filesystem resolved for creation token {efs_creation_token} does not carry the expected ManagedBy/GoldenGateDeploymentId/GoldenGateEnvironment/GoldenGateStorage ownership tags.")

    resolved_efs_id = fs.get("FileSystemId")
    print(f"EFS ID source: managed Terraform/AWS resolution (creation token {efs_creation_token}).")
    print(f"Resolved EFS filesystem ID: {resolved_efs_id}")
    return resolved_efs_id


# Read-only private-ECR runtime image verification -- no engine-to-image mapping, no public registry fallback.

def _verify_image_in_private_ecr(image_repository, image_tag, ecr_registry, aws_region):
    expected_prefix = f"{ecr_registry}/"
    if not (image_repository or "").startswith(expected_prefix):
        raise Phase5Error(f"image repository is not the approved private ECR account/region: {image_repository}")
    ecr_repo_name = image_repository[len(expected_prefix):]

    proc = run(["aws", "ecr", "describe-images", "--region", aws_region, "--repository-name", ecr_repo_name,
                "--image-ids", f"imageTag={image_tag}", "--output", "json"], check=False)
    if proc.returncode != 0:
        raise Phase5Error(f"could not verify image {image_repository}:{image_tag} in private ECR: {((proc.stderr or '') + (proc.stdout or '')).strip()}")
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Phase5Error(f"aws ecr describe-images returned malformed JSON: {exc}") from exc

    image_details = result.get("imageDetails") or []
    image_digest = image_details[0].get("imageDigest") if image_details else None
    if not image_digest:
        raise Phase5Error(f"could not resolve a digest for {image_repository}:{image_tag}.")
    print(f"Verified image exists: {image_repository}:{image_tag} (digest {image_digest})")
    return image_digest


_EXPECTED_IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}\Z")
_EFS_ID_SHAPE_RE = re.compile(r"^fs-[0-9a-zA-Z]+\Z")


def _validate_resolved_runtime_inputs(state, environment, deployment_id, *, verify_managed_efs_live=False):
    """Rebinds every resolve-live-inputs RESULT cached in reconcile state back to its own canonical, independently-verifiable source -- the state file is a cache/transport, never a second mutable source of truth. Never overloads _validate_reconcile_state_identity() (which only binds STATIC target identity); this is a separate second layer for values a stale/substituted state could otherwise use to control the Argo CD Application payload. Descriptor-derived fields (admin/tls secret names, ServiceAccount, image repository/tag, EFS mode/declared-ID/creation-token) are compared against a FRESH automation/goldengate-deployment-model.py describe() call (never a second descriptor parser) -- this is what prevents a source runtime from ever accepting a target admin secret, or vice versa. Environment-derived ingress fields (dns_domain/alb_group_name/certificate_arn) are compared against the already-hardened canonical environment producer (DNS_DOMAIN/ALB_GROUP_NAME/ACM_CERTIFICATE_ARN). image_digest is only shape-validated (sha256:<64 lowercase hex>) -- its repository/tag are already bound to the descriptor above, and a live ECR resolution result is never independently reinvented here. resolved_efs_id is validated per the deterministic 5-case contract: no-EFS requires empty; existing requires exact descriptor equality (no AWS call); managed+deploy=false requires exactly EFS_DRY_RUN_PLACEHOLDER (no AWS call); managed+deploy=true+verify_managed_efs_live=false requires only a valid fs-... shape (suitable for local validation deliberately given no AWS credentials); managed+deploy=true+verify_managed_efs_live=true freshly re-resolves the filesystem ID read-only via the existing canonical _resolve_efs_filesystem_id() (workload-account/lifecycle/ownership-tag verified) and requires it to exactly match the persisted resolved_efs_id before any Kubernetes mutation. Returns a dict of the canonical (descriptor/environment-sourced, never unvalidated state copies) resolved values for the caller to build its mutation payload from."""
    deploy = _require_literal_bool_state_value(state, "deploy")
    descriptor = _describe_deployment_json(environment, deployment_id)

    def _require_equals(label, actual, expected):
        if actual != expected:
            raise Phase5Error(f"Phase 5 reconcile state {label}={actual!r} does not match the current canonical source (expected {expected!r}) for {deployment_id!r} -- refusing to let a stale/mismatched resolved value control this mutation.")
        return expected

    admin_secret_name = _require_equals("admin_secret_name", state.get("admin_secret_name"), descriptor.get("adminSecretName"))
    tls_secret_name = _require_equals("tls_secret_name", state.get("tls_secret_name"), descriptor.get("tlsSecretName"))
    runtime_service_account_name = _require_equals("runtime_service_account_name", state.get("runtime_service_account_name"), descriptor.get("runtimeServiceAccountName"))
    image_repository = _require_equals("image_repository", state.get("image_repository"), descriptor.get("imageRepository"))
    image_repository_name = _require_equals("image_repository_name", state.get("image_repository_name"), descriptor.get("imageRepositoryName"))
    image_tag = _require_equals("image_tag", state.get("image_tag"), descriptor.get("imageTag"))
    efs_mode = _require_equals("efs_mode", state.get("efs_mode"), descriptor.get("efsMode") or "")
    efs_file_system_id_declared = _require_equals("efs_file_system_id_declared", state.get("efs_file_system_id_declared"), descriptor.get("efsFileSystemId") or "")
    efs_creation_token = _require_equals("efs_creation_token", state.get("efs_creation_token"), descriptor.get("efsCreationToken") or "")

    dns_domain = require_env("DNS_DOMAIN")
    alb_group_name = require_env("ALB_GROUP_NAME")
    certificate_arn = require_env("ACM_CERTIFICATE_ARN")
    _require_equals("dns_domain", state.get("dns_domain"), dns_domain)
    _require_equals("alb_group_name", state.get("alb_group_name"), alb_group_name)
    _require_equals("certificate_arn", state.get("certificate_arn"), certificate_arn)

    image_digest = state.get("image_digest")
    if not isinstance(image_digest, str) or not _EXPECTED_IMAGE_DIGEST_RE.match(image_digest):
        raise Phase5Error(f"Phase 5 reconcile state image_digest={image_digest!r} does not match the expected ECR digest shape sha256:<64 lowercase hex> -- refusing to treat it as a resolved live ECR result.")

    resolved_efs_id = state.get("resolved_efs_id") or ""
    if not efs_mode:
        if resolved_efs_id != "":
            raise Phase5Error(f"Phase 5 reconcile state resolved_efs_id={resolved_efs_id!r} is non-empty, but the current canonical descriptor for {deployment_id!r} does not use EFS.")
    elif efs_mode == "existing":
        if resolved_efs_id != efs_file_system_id_declared:
            raise Phase5Error(f"Phase 5 reconcile state resolved_efs_id={resolved_efs_id!r} does not match the current canonical descriptor's declared efsFileSystemId={efs_file_system_id_declared!r} for efsMode=existing.")
    elif efs_mode == "managed":
        if not deploy:
            if resolved_efs_id != EFS_DRY_RUN_PLACEHOLDER:
                raise Phase5Error(f"Phase 5 reconcile state resolved_efs_id={resolved_efs_id!r} is not the dry-run placeholder {EFS_DRY_RUN_PLACEHOLDER!r} expected for a managed-EFS Validate (deploy=false) state.")
        else:
            if not isinstance(resolved_efs_id, str) or not _EFS_ID_SHAPE_RE.match(resolved_efs_id):
                raise Phase5Error(f"Phase 5 reconcile state resolved_efs_id={resolved_efs_id!r} is not a valid EFS filesystem ID shape (fs-...) for a managed-EFS Deploy state.")
            if verify_managed_efs_live:
                aws_region = require_env("AWS_REGION")
                eks_deploy_role_arn = require_env("EKS_DEPLOY_ROLE_ARN")
                fresh_resolved_efs_id = _resolve_efs_filesystem_id(
                    efs_mode=efs_mode, efs_file_system_id_declared=efs_file_system_id_declared, efs_creation_token=efs_creation_token,
                    deploy=True, environment=environment, deployment_id=deployment_id,
                    eks_deploy_role_arn=eks_deploy_role_arn, aws_region=aws_region,
                )
                if fresh_resolved_efs_id != resolved_efs_id:
                    raise Phase5Error(f"freshly re-resolved managed EFS filesystem ID {fresh_resolved_efs_id!r} does not match the persisted resolved_efs_id={resolved_efs_id!r} -- refusing to reconcile with a stale/mismatched EFS filesystem ID.")
                resolved_efs_id = fresh_resolved_efs_id

    return {
        "admin_secret_name": admin_secret_name, "tls_secret_name": tls_secret_name,
        "runtime_service_account_name": runtime_service_account_name, "image_repository": image_repository,
        "image_repository_name": image_repository_name, "image_tag": image_tag, "image_digest": image_digest,
        "efs_mode": efs_mode, "efs_file_system_id_declared": efs_file_system_id_declared, "efs_creation_token": efs_creation_token,
        "dns_domain": dns_domain, "alb_group_name": alb_group_name, "certificate_arn": certificate_arn,
        "resolved_efs_id": resolved_efs_id,
    }


def _prepare_canonical_packaged_output_root():
    """The repository-owned packaged/ OUTPUT directory must never be a symlink -- validated/created BEFORE helm package ever writes into it (a symlinked packaged/ would let helm package's generated archive land entirely outside the repository, invisible to any containment check performed only afterward). Reused identically by _package_runtime_chart() (before helm package runs) and _validate_package_path_and_containment() (before trusting a persisted package_path at publish time) -- never two independently-drifting packaged-directory contracts. Never follows/creates through a symlink. Returns the validated physical packaged/ directory Path."""
    repo_root_resolved = REPO_ROOT.resolve()
    packaged_path = REPO_ROOT / "packaged"

    if packaged_path.is_symlink():
        raise Phase5Error(f"{packaged_path} is a symlink -- the repository-owned packaged/ directory must be a real directory, never a symlink (possible directory-level escape) -- refusing to use it.")
    if packaged_path.exists():
        if not packaged_path.is_dir():
            raise Phase5Error(f"expected packaged/ path {packaged_path} is not a directory.")
    else:
        packaged_path.mkdir(parents=True, exist_ok=False)

    # Re-checked after creation/existence -- defends the same TOCTOU window _validate_canonical_chart_source_root() defends for HELM_CHART_PATH.
    if packaged_path.is_symlink():
        raise Phase5Error(f"{packaged_path} is a symlink -- the repository-owned packaged/ directory must be a real directory, never a symlink (possible directory-level escape) -- refusing to use it.")
    if not packaged_path.is_dir():
        raise Phase5Error(f"expected packaged/ path {packaged_path} is not a directory.")

    packaged_dir_resolved = packaged_path.resolve()
    if packaged_dir_resolved != repo_root_resolved / "packaged":
        raise Phase5Error(f"{packaged_path} resolves to {packaged_dir_resolved}, which is not the expected {repo_root_resolved / 'packaged'} -- refusing to use it.")
    if repo_root_resolved not in packaged_dir_resolved.parents:
        raise Phase5Error(f"{packaged_path} resolves to {packaged_dir_resolved}, which is outside the repository root {repo_root_resolved} -- refusing to use it.")
    return packaged_path


def _validate_package_path_and_containment(package_path_rel, chart_version):
    """Rejects any package_path that is not EXACTLY the canonical packaged/<chart>-<version>.tgz path -- never an arbitrary state-controlled filesystem path. The repository-owned packaged/ directory itself, AND the package file itself, must each be a real (non-symlink) filesystem object genuinely contained under REPO_ROOT -- checked and resolved SEPARATELY and in that order, so a symlinked packaged/ directory can never make an otherwise-correct-looking containment comparison pass merely because both sides resolve to the same (entirely external) location."""
    expected_rel = _canonical_package_path(chart_version)
    if package_path_rel != expected_rel:
        raise Phase5Error(f"Phase 5 reconcile state package_path={package_path_rel!r} is not the canonical path {expected_rel!r} for chart_version={chart_version!r} -- refusing to publish an arbitrary/relocated package.")

    repo_root_resolved = REPO_ROOT.resolve()
    packaged_dir_resolved = _prepare_canonical_packaged_output_root().resolve()

    candidate_package_path = REPO_ROOT / package_path_rel
    if candidate_package_path.is_symlink():
        raise Phase5Error(f"{candidate_package_path} is a symlink -- the packaged chart archive must be a real regular file, never a symlink (possible file-level escape) -- refusing to publish it.")
    if not candidate_package_path.exists():
        raise Phase5Error(f"expected packaged chart archive does not exist: {candidate_package_path}")
    if not candidate_package_path.is_file():
        raise Phase5Error(f"expected packaged chart archive path {candidate_package_path} is not a regular file (found a directory or other filesystem object).")

    resolved_package_path = candidate_package_path.resolve()
    if resolved_package_path.parent != packaged_dir_resolved:
        raise Phase5Error(f"Phase 5 package path {package_path_rel!r} resolves to {resolved_package_path}, which escapes the expected packaged/ directory {packaged_dir_resolved} -- refusing to publish it.")
    if repo_root_resolved not in resolved_package_path.parents:
        raise Phase5Error(f"Phase 5 package path {package_path_rel!r} resolves to {resolved_package_path}, which is outside the repository root {repo_root_resolved} -- refusing to publish it.")
    return resolved_package_path


def _read_tar_member_bytes(tar, member_name):
    member = tar.getmember(member_name)
    if not member.isfile():
        raise Phase5Error(f"packaged chart member {member_name!r} is not a regular file.")
    extracted = tar.extractfile(member)
    if extracted is None:
        raise Phase5Error(f"packaged chart member {member_name!r} could not be read.")
    return extracted.read()


_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _require_safe_archive_member_name(name, resolved_package_path):
    """The canonical Helm package root is EXACTLY 'goldengate/' -- every archive member must live under it, using only safe relative path segments. Inspects the member NAME string only (tarfile inspection, never extraction) -- rejects empty names, absolute paths (POSIX or Windows-style), '..' traversal segments, and any non-canonical top-level root (e.g. evilroot/..., differentroot/...)."""
    if not name or name in (".", "./"):
        raise Phase5Error(f"packaged chart archive {resolved_package_path} contains an unsafe/empty member name {name!r}.")
    if name.startswith("/") or name.startswith("\\"):
        raise Phase5Error(f"packaged chart archive {resolved_package_path} contains an absolute member path {name!r} -- refusing to trust it.")
    if _WINDOWS_ABSOLUTE_PATH_RE.match(name):
        raise Phase5Error(f"packaged chart archive {resolved_package_path} contains a Windows-style absolute member path {name!r} -- refusing to trust it.")
    segments = name.replace("\\", "/").split("/")
    if any(seg in ("..", "") for seg in segments[:-1]) or segments[-1] == "..":
        raise Phase5Error(f"packaged chart archive {resolved_package_path} contains an unsafe path-traversal member name {name!r}.")
    if segments[0] != CHART_NAME:
        raise Phase5Error(f"packaged chart archive {resolved_package_path} contains a member {name!r} outside the canonical archive root {CHART_NAME!r}/ -- refusing to trust a non-canonical package root.")


def _validate_canonical_chart_source_root():
    """The authoritative chart source must be the physical repository-owned directory REPO_ROOT/helm/goldengate -- never an arbitrary filesystem target reachable through a directory-level symlink (the same protection already applied to REPO_ROOT/packaged in _validate_package_path_and_containment()). MUST be called BEFORE any recursive traversal (_expected_chart_package_members()'s rglob) or copy (_package_runtime_chart()'s copytree) ever trusts HELM_CHART_PATH -- an untrusted symlinked chart root must never even be walked. Returns HELM_CHART_PATH once every check has passed, so callers never need to separately re-trust it at another artifact-trust boundary."""
    expected_chart_path = REPO_ROOT / "helm" / CHART_NAME
    if HELM_CHART_PATH != expected_chart_path:
        raise Phase5Error(f"HELM_CHART_PATH={HELM_CHART_PATH} does not refer to the expected canonical chart source path {expected_chart_path} -- refusing to trust an unexpected chart-source constant.")

    if HELM_CHART_PATH.is_symlink():
        raise Phase5Error(f"{HELM_CHART_PATH} is a symlink -- the canonical chart source root must be a real, repository-owned directory, never a symlink (possible directory-level escape) -- refusing to trust anything under it.")
    if not HELM_CHART_PATH.exists():
        raise Phase5Error(f"expected canonical chart source directory does not exist: {HELM_CHART_PATH}")
    if not HELM_CHART_PATH.is_dir():
        raise Phase5Error(f"expected canonical chart source path {HELM_CHART_PATH} is not a directory.")

    repo_root_resolved = REPO_ROOT.resolve()
    chart_root_resolved = HELM_CHART_PATH.resolve()
    if chart_root_resolved != repo_root_resolved / "helm" / CHART_NAME:
        raise Phase5Error(f"{HELM_CHART_PATH} resolves to {chart_root_resolved}, which is not the expected {repo_root_resolved / 'helm' / CHART_NAME} -- refusing to trust an unexpected chart-source location.")
    if repo_root_resolved not in chart_root_resolved.parents:
        raise Phase5Error(f"{HELM_CHART_PATH} resolves to {chart_root_resolved}, which is outside the repository root {repo_root_resolved} -- refusing to trust it.")
    return HELM_CHART_PATH


def _require_symlink_free_tree(root, label):
    """Generic filesystem-tree fail-closed check, reused for BOTH the canonical chart source tree (via _validate_canonical_chart_source_tree()) and the temporary copied chart tree (_package_runtime_chart(), immediately before helm package) -- never a second, independently-drifting tree-walk implementation. Every descendant under root must be a real directory or a real regular file; a symlink (file or directory) or any other special filesystem object (FIFO/device/socket) is rejected."""
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise Phase5Error(f"{label} {path} is a symlink -- refusing to trust it.")
        if not (path.is_dir() or path.is_file()):
            raise Phase5Error(f"{label} {path} is neither a regular file nor a directory -- refusing to trust an ambiguous/special filesystem object.")


def _validate_canonical_chart_source_tree():
    """The COMPLETE canonical helm/goldengate/ source tree -- not merely its root -- must be proven free of symlinks/special objects BEFORE any Helm command, copy, or file read ever consumes it (a descendant symlink must never be dereferenced by Helm, or silently copied into work/charts/, before rejection). Calls _validate_canonical_chart_source_root() first (the already-approved root-identity/symlink/resolved-location contract), then _require_symlink_free_tree() for every descendant. Returns (chart_root, source_files) where source_files is the canonical set of chart-relative (POSIX) regular-file paths, derived dynamically from the real tree -- never a manually-maintained template list. cmd_validate_local()/_package_runtime_chart()/_expected_chart_package_members() all reuse this single contract; none independently re-walks the tree."""
    chart_root = _validate_canonical_chart_source_root()
    _require_symlink_free_tree(chart_root, "canonical chart source")
    source_files = {path.relative_to(chart_root).as_posix() for path in chart_root.rglob("*") if path.is_file()}
    return chart_root, source_files


def _expected_chart_package_members():
    """Derives the expected canonical archive regular-file member set from the CURRENT, already-validated (symlink-free, via _validate_canonical_chart_source_tree()) helm/goldengate/ source tree -- never a second, manually-maintained template list, and never an independent descendant-symlink check duplicating the one canonical tree-validation contract. The one deployment-specific addition (goldengate/values-deployment.yaml, produced by _package_runtime_chart() copying the current deployment values file) is added last."""
    _chart_root, source_files = _validate_canonical_chart_source_tree()
    members = {f"{CHART_NAME}/{relative}" for relative in source_files}
    members.add(f"{CHART_NAME}/values-deployment.yaml")
    return members


def _load_strict_yaml_mapping(raw_bytes, source_label):
    """Parses YAML using the same duplicate-key-rejecting loader used for rendered manifests -- an artifact trust boundary (packaged Chart.yaml) must never silently accept a duplicate key via plain yaml.safe_load's last-value-wins semantics."""
    try:
        loaded = yaml.load(raw_bytes, Loader=_StrictSafeLoader)
    except _DuplicateKeyError as exc:
        raise Phase5Error(f"{source_label} contains a duplicate YAML mapping key: {exc}") from exc
    except yaml.YAMLError as exc:
        raise Phase5Error(f"{source_label} is not valid YAML: {exc}") from exc
    if not isinstance(loaded, dict):
        raise Phase5Error(f"{source_label} is a {type(loaded).__name__}, expected a YAML mapping.")
    return loaded


def _validate_packaged_chart_contents(resolved_package_path, chart_version, environment, deployment_id):
    """Inspects the .tgz using Python's stdlib tarfile module ONLY (never shells out to tar, never extracts) to prove the package genuinely belongs to THIS runtime, from THIS current chart source, before it is ever published -- validate-local proved that helm/goldengate/ + the current deployment values render into the approved manifest; this proves the .tgz about to be pushed IS that same chart source plus that same deployment values, not an independently-mutated package. Every archive member must live under the canonical root goldengate/ with a safe relative path; only regular files and directories are accepted (no symlink/hardlink/device/FIFO); the archive's regular-file member set must EXACTLY equal the set derived from the current helm/goldengate/ source tree plus goldengate/values-deployment.yaml (missing/extra/duplicate members all fail); every member other than Chart.yaml/values-deployment.yaml must be byte-for-byte identical to its current source-tree counterpart; Chart.yaml is compared via the duplicate-key-rejecting loader against the current canonical Chart.yaml with ONLY version/appVersion overridden to the canonical chart version; values-deployment.yaml remains the exact byte-for-byte comparison against the CURRENT envs/<environment>/<deployment_id>/values.yaml."""
    import tarfile

    expected_members = _expected_chart_package_members()
    chart_yaml_member = f"{CHART_NAME}/Chart.yaml"
    values_deployment_member = f"{CHART_NAME}/values-deployment.yaml"

    try:
        tar = tarfile.open(resolved_package_path, mode="r:gz")
    except tarfile.TarError as exc:
        raise Phase5Error(f"packaged chart archive {resolved_package_path} is not a valid gzip tar archive: {exc}") from exc

    with tar:
        regular_file_names = []
        for member in tar.getmembers():
            _require_safe_archive_member_name(member.name, resolved_package_path)
            if member.isdir():
                continue
            if member.issym() or member.islnk():
                raise Phase5Error(f"packaged chart archive {resolved_package_path} contains a link member {member.name!r} (symbolic or hard link) -- refusing to trust any link member in a Helm runtime chart package.")
            if member.ischr() or member.isblk() or member.isfifo():
                raise Phase5Error(f"packaged chart archive {resolved_package_path} contains a device/FIFO member {member.name!r} -- refusing to trust any non-regular-file member in a Helm runtime chart package.")
            if not member.isfile():
                raise Phase5Error(f"packaged chart archive {resolved_package_path} contains a member {member.name!r} of unexpected/unsafe type -- only regular files and directories are accepted.")
            regular_file_names.append(member.name)

        if len(regular_file_names) != len(set(regular_file_names)):
            duplicates = sorted({n for n in regular_file_names if regular_file_names.count(n) > 1})
            raise Phase5Error(f"packaged chart archive {resolved_package_path} contains duplicate/ambiguous member name(s): {duplicates}.")

        actual_members = set(regular_file_names)
        if actual_members != expected_members:
            missing = sorted(expected_members - actual_members)
            unexpected = sorted(actual_members - expected_members)
            raise Phase5Error(f"packaged chart archive {resolved_package_path} regular-file member set does not exactly match the canonical helm/goldengate/ source (missing: {missing}, unexpected: {unexpected}).")

        chart_yaml_bytes = _read_tar_member_bytes(tar, chart_yaml_member)
        values_deployment_bytes = _read_tar_member_bytes(tar, values_deployment_member)

        for member_name in sorted(expected_members - {chart_yaml_member, values_deployment_member}):
            packaged_bytes = _read_tar_member_bytes(tar, member_name)
            relative_path = member_name[len(CHART_NAME) + 1:]
            current_bytes = (HELM_CHART_PATH / relative_path).read_bytes()
            if packaged_bytes != current_bytes:
                raise Phase5Error(f"packaged chart archive {resolved_package_path} member {member_name!r} does not byte-for-byte match the CURRENT helm/goldengate/{relative_path} -- this package was not built from the current chart source.")

    current_chart_yaml = _load_strict_yaml_mapping((HELM_CHART_PATH / "Chart.yaml").read_bytes(), "helm/goldengate/Chart.yaml")
    expected_chart_yaml = dict(current_chart_yaml)
    expected_chart_yaml["version"] = chart_version
    expected_chart_yaml["appVersion"] = chart_version
    packaged_chart_yaml = _load_strict_yaml_mapping(chart_yaml_bytes, f"packaged {chart_yaml_member}")
    if packaged_chart_yaml != expected_chart_yaml:
        raise Phase5Error(f"packaged {chart_yaml_member} {packaged_chart_yaml!r} does not match the expected canonical Chart.yaml (current helm/goldengate/Chart.yaml with only version/appVersion set to the canonical chart version {chart_version!r}): expected {expected_chart_yaml!r}.")

    expected_values_path = REPO_ROOT / "envs" / environment / deployment_id / "values.yaml"
    if not expected_values_path.is_file():
        raise Phase5Error(f"expected current deployment values file does not exist: {expected_values_path}")
    expected_values_bytes = expected_values_path.read_bytes()
    if values_deployment_bytes != expected_values_bytes:
        raise Phase5Error(f"packaged {values_deployment_member} does not byte-for-byte match the CURRENT {expected_values_path.relative_to(REPO_ROOT)} -- this package was not built from the current deployment's values file.")

    print(f"OK: packaged chart archive {resolved_package_path.name} verified against the CURRENT helm/goldengate/ source tree ({len(expected_members)} regular files, exact member-set + byte match) and CURRENT {expected_values_path.relative_to(REPO_ROOT)}.")


# Phase 5B, step 1: prepare-deployment (no AWS credentials)

def cmd_prepare_deployment(args):
    environment = require_environment_arg(args.environment)
    deployment_id = require_deployment_id_arg(args.deployment_id)
    deploy = _parse_bool_arg(args.deploy, "--deploy")

    # deploymentModel comes straight from the validated active matrix (never inferred/defaulted); any other value reaching here is an upstream bug.
    if args.deployment_model != "singleRuntime":
        raise Phase5Error(f"matrix.deployment_model is {args.deployment_model!r}, expected exactly 'singleRuntime'. The active build/deploy path only ever supports deploymentModel=singleRuntime -- legacyPair is retired and is never deployed by this job.")
    deployment_model = args.deployment_model

    runtime_namespace = require_env("RUNTIME_NAMESPACE")
    if len(runtime_namespace) > 63:
        raise Phase5Error(f"Namespace is too long: {runtime_namespace}. Kubernetes namespace names must be 63 characters or less. Please shorten deployment_id.")

    release_name = deployment_id
    if len(release_name) > 53:
        raise Phase5Error(f"Helm release name is too long: {release_name}. Please shorten deployment_id because resource names add suffixes.")

    argocd_app_name = _canonical_argocd_app_name(environment, deployment_id)

    values_file = f"envs/{environment}/{deployment_id}/values.yaml"

    chart_version = _canonical_chart_version(deployment_id)
    ecr_registry = require_env("ECR_REGISTRY")
    helm_push_url = f"oci://{ecr_registry}/{HELM_OCI_NAMESPACE}"
    helm_chart_ref = f"oci://{ecr_registry}/{HELM_ECR_REPOSITORY}"

    update_state(args.state_path, {
        "environment": environment, "deployment_id": deployment_id, "deployment_model": deployment_model,
        "deploy": deploy, "values_file": values_file, "target_namespace": runtime_namespace,
        "release_name": release_name, "argocd_app_name": argocd_app_name,
        "temp_chart_path": _canonical_temp_chart_path(deployment_id), "chart_version": chart_version,
        "helm_ecr_repository": HELM_ECR_REPOSITORY, "helm_push_url": helm_push_url, "helm_chart_ref": helm_chart_ref,
    }, RECONCILE_ALLOWED_STATE_KEYS)
    print(f"OK: prepared deployment state for {deployment_id} (deploymentModel={deployment_model}, deploy={deploy}).")


# Phase 5B, step 2: resolve-live-inputs (AWS credentials required)

def cmd_resolve_live_inputs(args):
    environment = require_environment_arg(args.environment)
    deployment_id = require_deployment_id_arg(args.deployment_id)
    state = load_state(args.state_path)

    # State identity/deploy-boolean binding BEFORE any EFS resolution or ECR image verification -- a state file left over from (or substituted from) a different matrix runtime, or a non-literal deploy value, must never control this mutation-adjacent read.
    deploy = _validate_reconcile_state_identity(state, environment, deployment_id)

    descriptor = _describe_deployment_json(environment, deployment_id)
    admin_secret_name = descriptor.get("adminSecretName")
    tls_secret_name = descriptor.get("tlsSecretName")
    runtime_service_account_name = descriptor.get("runtimeServiceAccountName")
    image_repository = descriptor.get("imageRepository")
    image_repository_name = descriptor.get("imageRepositoryName")
    image_tag = descriptor.get("imageTag")
    efs_mode = descriptor.get("efsMode") or ""
    efs_file_system_id_declared = descriptor.get("efsFileSystemId") or ""
    efs_creation_token = descriptor.get("efsCreationToken") or ""

    if not (admin_secret_name and tls_secret_name and runtime_service_account_name):
        raise Phase5Error(f"could not resolve deployment identity for {deployment_id}.")
    print(f"Resolved admin secret: {admin_secret_name}")
    print(f"Resolved TLS secret: {tls_secret_name}")
    print(f"Resolved ServiceAccount: {runtime_service_account_name}")
    print(f"Resolved image repository: {image_repository}")
    print(f"Resolved persistence.efs.mode: {efs_mode or '<not in use>'}")

    # Fresh-EKS Phase A/Phase 9: shared ingress identity is resolved once from the canonical environment config (already loaded into this process's environment by the job's own "Load canonical environment configuration" step) -- the single source of truth also used by Terraform/IAM, never re-derived or hand-maintained per deployment.
    dns_domain = require_env("DNS_DOMAIN")
    alb_group_name = require_env("ALB_GROUP_NAME")
    certificate_arn = require_env("ACM_CERTIFICATE_ARN")
    print(f"Resolved ingress hostDomain: {dns_domain}")
    print(f"Resolved ALB group: {alb_group_name}")

    aws_region = require_env("AWS_REGION")
    ecr_registry = require_env("ECR_REGISTRY")
    eks_deploy_role_arn = require_env("EKS_DEPLOY_ROLE_ARN")

    resolved_efs_id = _resolve_efs_filesystem_id(
        efs_mode=efs_mode, efs_file_system_id_declared=efs_file_system_id_declared, efs_creation_token=efs_creation_token,
        deploy=deploy, environment=environment, deployment_id=deployment_id,
        eks_deploy_role_arn=eks_deploy_role_arn, aws_region=aws_region,
    )

    image_digest = _verify_image_in_private_ecr(image_repository, image_tag, ecr_registry, aws_region)

    update_state(args.state_path, {
        "admin_secret_name": admin_secret_name, "tls_secret_name": tls_secret_name,
        "runtime_service_account_name": runtime_service_account_name, "image_repository": image_repository,
        "image_repository_name": image_repository_name, "image_tag": image_tag, "image_digest": image_digest,
        "dns_domain": dns_domain, "alb_group_name": alb_group_name, "certificate_arn": certificate_arn,
        "efs_mode": efs_mode, "efs_file_system_id_declared": efs_file_system_id_declared,
        "efs_creation_token": efs_creation_token, "resolved_efs_id": resolved_efs_id,
    }, RECONCILE_ALLOWED_STATE_KEYS)
    print(f"OK: resolved live deployment identity for {deployment_id} (image {image_repository}:{image_tag}, digest {image_digest}).")


# Phase 5B, step 3: validate-local (no AWS credentials -- pure local Helm lint/template/package)

def _validate_required_files(values_file, chart_root):
    """chart_root must already be the validated result of _validate_canonical_chart_source_root() -- this helper never independently trusts HELM_CHART_PATH itself, so it can never establish trust in an untrusted chart root on its own."""
    chart_yaml = chart_root / "Chart.yaml"
    values_yaml = chart_root / "values.yaml"
    env_values_path = REPO_ROOT / values_file
    for path in (chart_yaml, values_yaml, env_values_path):
        if not path.is_file():
            raise Phase5Error(f"Missing required file: {path}")
    print("Required files are present.")


def _helm_set_overrides(environment, image_repository, dns_domain, alb_group_name, certificate_arn,
                         admin_secret_name, tls_secret_name, aws_region, runtime_service_account_name, resolved_efs_id):
    """Exact current override set -- deploymentId/deploymentModel are intentionally never passed via --set (the chart derives runtime name from Release.Name); string-safe (--set-string) for hostname/ARN/image values so Helm's own value-type inference never coerces them."""
    return [
        "--set", f"global.environment={environment}",
        "--set-string", f"runtime.image.repository={image_repository}",
        "--set-string", f"ingress.hostDomain={dns_domain}",
        "--set-string", f"ingress.alb.groupName={alb_group_name}",
        "--set-string", f"ingress.alb.certificateArn={certificate_arn}",
        "--set", f"runtime.csi.admin.objectName={admin_secret_name}",
        "--set", f"runtime.csi.certificate.objectName={tls_secret_name}",
        "--set-string", f"runtime.csi.region={aws_region}",
        "--set", "runtime.serviceAccount.create=false",
        "--set", f"runtime.serviceAccount.name={runtime_service_account_name}",
        "--set", f"persistence.efs.fileSystemId={resolved_efs_id}",
    ]


class _DuplicateKeyError(ValueError):
    pass


class _StrictSafeLoader(yaml.SafeLoader):
    pass


def _no_duplicates_constructor(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise _DuplicateKeyError(f"duplicate mapping key {key!r} at line {key_node.start_mark.line + 1}")
        value = loader.construct_object(value_node, deep=deep)
        mapping[key] = value
    return mapping


_StrictSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates_constructor)


def _parse_rendered_documents(rendered_text):
    """Parses a multi-document Helm-rendered manifest with a duplicate-key-rejecting PyYAML loader -- preserves the old implementation's deliberate refusal to let a duplicate rendered YAML key silently win by last-value semantics."""
    try:
        return [d for d in yaml.load_all(rendered_text, Loader=_StrictSafeLoader) if d]
    except _DuplicateKeyError as exc:
        raise Phase5Error(f"duplicate YAML mapping key in rendered manifest: {exc}") from exc
    except yaml.YAMLError as exc:
        raise Phase5Error(f"rendered manifest is not valid YAML: {exc}") from exc


def _find_document(docs, kind, name):
    for doc in docs:
        if doc.get("kind") == kind and (doc.get("metadata") or {}).get("name") == name:
            return doc
    return None


def _count_kind(docs, kind):
    return sum(1 for d in docs if d.get("kind") == kind)


def _validate_zero_namespace_documents(docs):
    count = _count_kind(docs, "Namespace")
    if count != 0:
        raise Phase5Error(f"a Namespace document was rendered for a singleRuntime release ({count} found). This must never happen -- singleRuntime releases must not create or own the shared namespace.")
    print("OK: no Namespace document rendered, as expected for deploymentModel=singleRuntime.")


def _validate_runtime_service_account_used(docs, sa_name):
    statefulsets = [d for d in docs if d.get("kind") == "StatefulSet"]
    if len(statefulsets) != 1:
        raise Phase5Error(f"expected exactly one StatefulSet, found {len(statefulsets)}.")
    pod_spec = ((statefulsets[0].get("spec") or {}).get("template") or {}).get("spec") or {}
    actual = pod_spec.get("serviceAccountName")
    if actual != sa_name:
        raise Phase5Error(f"rendered StatefulSet does not use serviceAccountName: {sa_name} (found {actual!r}).")
    print(f"OK: rendered StatefulSet uses serviceAccountName: {sa_name}.")


def _opposite_role_admin_secret(admin_secret_name, environment):
    if admin_secret_name == f"{environment}/goldengate/target/admin":
        return f"{environment}/goldengate/source/admin"
    return f"{environment}/goldengate/target/admin"


def _spc_object_names(spc):
    params = (spc.get("spec") or {}).get("parameters") or {}
    objects_yaml = params.get("objects")
    if not objects_yaml:
        return []
    try:
        objects = yaml.safe_load(objects_yaml) or []
    except yaml.YAMLError as exc:
        raise Phase5Error(f"SecretProviderClass {(spc.get('metadata') or {}).get('name')} parameters.objects is not valid YAML: {exc}") from exc
    return [o.get("objectName") for o in objects if isinstance(o, dict)]


def _validate_admin_secret_csi_isolation(docs, admin_secret_name, environment, deployment_id):
    admin_spc = _find_document(docs, "SecretProviderClass", f"{deployment_id}-admin")
    if admin_spc is None:
        raise Phase5Error(f"rendered SecretProviderClass {deployment_id}-admin was not found.")
    names = _spc_object_names(admin_spc)
    if admin_secret_name not in names:
        raise Phase5Error(f"rendered SecretProviderClass does not select this deployment's own admin secret ({admin_secret_name}).")
    opposite = _opposite_role_admin_secret(admin_secret_name, environment)
    if opposite in names:
        raise Phase5Error(f"rendered SecretProviderClass mounts the opposite-role admin secret ({opposite}).")
    print(f"OK: rendered SecretProviderClass mounts only {admin_secret_name}.")


def _init_script_text(container):
    """Tolerates non-string command/args entries (e.g. Helm-rendered numbers) instead of crashing."""
    parts = []
    for item in list(container.get("command") or []) + list(container.get("args") or []):
        if isinstance(item, str):
            parts.append(item)
    return "\n".join(parts)


def _validate_singleruntime_manifest_contract(docs, values, deployment_id, target_namespace, image_repository, image_tag, image_digest):
    """Structural runtime-manifest contract (Python/PyYAML, duplicate-key-safe) -- never indentation-based grep for Kubernetes object structure or field values."""
    expected_image = f"{image_repository}:{image_tag}"
    runtime_values = values.get("runtime") or {}
    expected_container_name = runtime_values.get("containerName")
    if not expected_container_name:
        raise Phase5Error("runtime.containerName not found in the deployment values file.")

    statefulsets = [d for d in docs if d.get("kind") == "StatefulSet"]
    if len(statefulsets) != 1:
        raise Phase5Error(f"expected exactly one StatefulSet, found {len(statefulsets)}.")
    sts = statefulsets[0]
    pod_spec = ((sts.get("spec") or {}).get("template") or {}).get("spec") or {}

    containers = pod_spec.get("containers") or []
    if len(containers) != 1:
        names = [c.get("name") for c in containers]
        raise Phase5Error(f"expected exactly one regular application container in spec.template.spec.containers, found {len(containers)}: {names}")
    main_container = containers[0]
    main_container_name = main_container.get("name")
    if main_container_name != expected_container_name:
        raise Phase5Error(f"expected main container name {expected_container_name!r}, found {main_container_name!r}.")
    print(f"OK: exactly one regular application container (spec.template.spec.containers), name={main_container_name!r}.")

    main_container_image = main_container.get("image")
    if main_container_image != expected_image:
        raise Phase5Error(f"rendered StatefulSet main container {main_container_name!r} does not reference the verified image {expected_image!r} (found {main_container_image!r}).")
    print(f"OK: rendered StatefulSet main container {main_container_name!r} references verified image {expected_image} (verified ECR digest {image_digest}).")

    name_lower = (main_container_name or "").lower()
    image_lower = (main_container.get("image") or "").lower()
    for substring in FORBIDDEN_CONTAINER_SUBSTRINGS:
        if substring in name_lower or substring in image_lower:
            raise Phase5Error(f"the regular container unexpectedly references {substring!r} (name={main_container_name!r}, image={main_container.get('image')!r}).")
    print("OK: no observer/utility-sidecar/Fluent Bit reference in the regular container.")

    # initContainers must be exactly [prepare-u02-permissions] -- an empty list must fail here, not pass vacuously.
    init_containers = pod_spec.get("initContainers") or []
    init_names = [c.get("name") for c in init_containers]
    if init_names != [INIT_CONTAINER_NAME]:
        raise Phase5Error(f"expected exactly one init container named {INIT_CONTAINER_NAME!r}, found {init_names}.")
    init_container = init_containers[0]

    init_container_image = init_container.get("image")
    if init_container_image != expected_image:
        raise Phase5Error(f"{INIT_CONTAINER_NAME} init container does not reference the verified image {expected_image!r} (found {init_container_image!r}).")
    print(f"OK: {INIT_CONTAINER_NAME} init container also references verified image {expected_image}.")

    init_script_text = _init_script_text(init_container)
    if "ServiceManager.pid" not in init_script_text:
        raise Phase5Error(f"the {INIT_CONTAINER_NAME} init container does not reference ServiceManager.pid -- the stale GoldenGate Service Manager PID safeguard is missing.")
    if 'rm -f -- "$SERVICE_MANAGER_PID_FILE"' not in init_script_text:
        raise Phase5Error(f"the {INIT_CONTAINER_NAME} init container does not contain the expected stale-PID removal command (rm -f -- \"$SERVICE_MANAGER_PID_FILE\").")
    print(f"OK: required {INIT_CONTAINER_NAME} init container and stale ServiceManager.pid cleanup are present.")

    # --- Services: exactly one ClusterIP + one headless, partitioned strictly by clusterIP/type ---
    services = [d for d in docs if d.get("kind") == "Service"]
    if len(services) != 2:
        raise Phase5Error(f"expected exactly 2 Service documents (one ClusterIP, one headless), found {len(services)}.")
    headless_services = [s for s in services if (s.get("spec") or {}).get("clusterIP") == "None"]
    normal_services = [s for s in services if (s.get("spec") or {}).get("clusterIP") != "None" and (s.get("spec") or {}).get("type") in (None, "ClusterIP")]
    if len(headless_services) != 1:
        raise Phase5Error(f"expected exactly one headless Service (spec.clusterIP == 'None'), found {len(headless_services)}.")
    if len(normal_services) != 1:
        raise Phase5Error(f"expected exactly one normal ClusterIP Service, found {len(normal_services)}.")
    print("OK: exactly one ClusterIP Service and one headless Service.")

    # --- Ingress: exactly one, with exactly one host, when enabled -----------
    ingress_enabled = bool((values.get("ingress") or {}).get("enabled"))
    ingresses = [d for d in docs if d.get("kind") == "Ingress"]
    if ingress_enabled:
        if len(ingresses) != 1:
            raise Phase5Error(f"expected exactly one rendered Ingress, found {len(ingresses)}.")
        rules = (ingresses[0].get("spec") or {}).get("rules") or []
        if len(rules) != 1:
            raise Phase5Error(f"expected exactly one Ingress host rule, found {len(rules)}.")
        print("OK: exactly one Ingress with one host.")
    else:
        print("ingress.enabled is not true. Skipping Ingress checks.")

    # --- runtime-qualified resource names + shared namespace -----------------
    expected_names = {deployment_id, f"{deployment_id}-headless"}
    found_names = {(doc.get("metadata") or {}).get("name") for doc in docs if (doc.get("metadata") or {}).get("name") in expected_names}
    missing_names = expected_names - found_names
    if missing_names:
        raise Phase5Error(f"expected resource name(s) not found in the rendered manifest: {sorted(missing_names)}")

    spcs = [d for d in docs if d.get("kind") == "SecretProviderClass"]
    for spc in spcs:
        name = (spc.get("metadata") or {}).get("name", "")
        if not name.startswith(f"{deployment_id}-"):
            raise Phase5Error(f"SecretProviderClass name {name!r} is not qualified by the runtime name {deployment_id!r}.")
    if spcs:
        print(f"OK: {len(spcs)} SecretProviderClass name(s) are runtime-qualified.")

    namespace_found = False
    for doc in docs:
        if doc.get("kind") == "Namespace":
            raise Phase5Error("a Namespace document was rendered for a singleRuntime release.")
        metadata = doc.get("metadata") or {}
        ns = metadata.get("namespace")
        if ns is not None:
            namespace_found = True
            if ns != target_namespace:
                raise Phase5Error(f"{doc.get('kind')} {metadata.get('name')} uses unexpected namespace {ns!r}, expected {target_namespace!r}.")
    if not namespace_found:
        raise Phase5Error(f"no namespaced resource references the expected shared namespace {target_namespace!r}.")
    print(f"OK: namespaced resources reference the shared namespace {target_namespace!r}.")
    print("OK: singleRuntime rendered manifest passed all generic runtime contract checks (Python/PyYAML, duplicate-key-safe).")


# EFS render contract (values-shape + rendered-manifest structural checks)

def _default_efs_base_path(values, deployment_id):
    """Mirrors helm/goldengate's goldengate.efsBasePath helper: fullnameOverride, else name, else deployment_id."""
    runtime = values.get("runtime") or {}
    runtime_name = runtime.get("fullnameOverride") or runtime.get("name") or deployment_id
    if not isinstance(runtime_name, str) or not runtime_name.strip():
        raise Phase5Error("could not resolve a runtime name for the default EFS basePath.")
    return f"/{runtime_name}"


def _validate_efs_values_shape(values, deployment_id, deployment_model):
    if deployment_model != "singleRuntime":
        raise Phase5Error(f"unexpected deploymentModel {deployment_model!r} reached EFS validation -- only singleRuntime is supported for active deployments.")

    persistence = values.get("persistence")
    if persistence is not None and not isinstance(persistence, dict):
        raise Phase5Error(f"persistence must be a mapping, got {type(persistence).__name__}.")
    persistence = persistence or {}
    efs_enabled = persistence.get("enabled") is True and persistence.get("provider") == "efs"
    if not efs_enabled:
        return False, None

    efs = persistence.get("efs")
    if not isinstance(efs, dict):
        raise Phase5Error("persistence.efs must be a mapping when persistence.enabled=true and provider=efs.")

    mode = efs.get("mode")
    if mode not in ("existing", "managed"):
        raise Phase5Error(f"persistence.efs.mode must be exactly 'existing' or 'managed', got {mode!r}.")

    declared_file_system_id = efs.get("fileSystemId")
    if mode == "existing" and (not isinstance(declared_file_system_id, str) or not declared_file_system_id.strip()):
        raise Phase5Error("persistence.efs.fileSystemId must be a non-empty string when persistence.efs.mode=existing.")
    if mode == "managed" and declared_file_system_id not in (None, ""):
        raise Phase5Error("persistence.efs.fileSystemId must not be set when persistence.efs.mode=managed -- Terraform provisions and resolves it.")

    storage_class = efs.get("storageClass")
    if storage_class is not None and not isinstance(storage_class, dict):
        raise Phase5Error(f"persistence.efs.storageClass must be a mapping when present, got {type(storage_class).__name__}.")

    explicit_base_path = (storage_class or {}).get("basePath")
    if explicit_base_path is not None and not isinstance(explicit_base_path, str):
        raise Phase5Error(f"persistence.efs.storageClass.basePath must be a string when present, got {type(explicit_base_path).__name__}.")

    base_path = explicit_base_path if isinstance(explicit_base_path, str) and explicit_base_path.strip() else _default_efs_base_path(values, deployment_id)
    return True, {"mode": mode, "declared_file_system_id": declared_file_system_id, "base_path": base_path}


def _validate_rendered_storageclass_and_pvc(docs, environment, deployment_id, resolved_efs_id, base_path):
    expected_sc_name = f"gg-efs-{environment}-{deployment_id}"
    storage_classes = [d for d in docs if d.get("kind") == "StorageClass" and (d.get("metadata") or {}).get("name") == expected_sc_name]
    if len(storage_classes) != 1:
        all_names = [(d.get("metadata") or {}).get("name") for d in docs if d.get("kind") == "StorageClass"]
        raise Phase5Error(f"expected exactly one StorageClass named {expected_sc_name!r}, found {len(storage_classes)} (all rendered StorageClass names: {all_names}).")

    sc = storage_classes[0]
    parameters = sc.get("parameters") or {}
    checks = [
        (sc.get("provisioner") == "efs.csi.aws.com", f"provisioner (expected 'efs.csi.aws.com', got {sc.get('provisioner')!r})"),
        (parameters.get("provisioningMode") == "efs-ap", f"parameters.provisioningMode (expected 'efs-ap', got {parameters.get('provisioningMode')!r})"),
        (parameters.get("fileSystemId") == resolved_efs_id, f"parameters.fileSystemId (expected {resolved_efs_id!r}, got {parameters.get('fileSystemId')!r})"),
        (parameters.get("basePath") == base_path, f"parameters.basePath (expected {base_path!r}, got {parameters.get('basePath')!r})"),
        (parameters.get("subPathPattern") == "${.PVC.name}", f"parameters.subPathPattern (expected '${{.PVC.name}}', got {parameters.get('subPathPattern')!r})"),
        (parameters.get("ensureUniqueDirectory") == "true", f"parameters.ensureUniqueDirectory (expected 'true', got {parameters.get('ensureUniqueDirectory')!r})"),
        (sc.get("reclaimPolicy") == "Retain", f"reclaimPolicy (expected 'Retain', got {sc.get('reclaimPolicy')!r})"),
    ]
    failed = [msg for ok, msg in checks if not ok]
    if failed:
        raise Phase5Error(f"rendered StorageClass {expected_sc_name!r} does not match the expected configuration: {failed}")
    print(f"OK: StorageClass {expected_sc_name!r} matches the expected configuration (provisioner, provisioningMode, fileSystemId, basePath={base_path!r}, subPathPattern, ensureUniqueDirectory, reclaimPolicy).")

    expected_pvc_name = f"{deployment_id}-u02"
    pvcs = [d for d in docs if d.get("kind") == "PersistentVolumeClaim" and (d.get("metadata") or {}).get("name") == expected_pvc_name]
    if len(pvcs) != 1:
        raise Phase5Error(f"expected exactly one PersistentVolumeClaim named {expected_pvc_name!r}, found {len(pvcs)}.")

    statefulsets = [d for d in docs if d.get("kind") == "StatefulSet"]
    if len(statefulsets) != 1:
        raise Phase5Error(f"expected exactly one StatefulSet, found {len(statefulsets)}.")
    volumes = ((statefulsets[0].get("spec") or {}).get("template") or {}).get("spec", {}).get("volumes") or []

    def _volume(name):
        for v in volumes:
            if v.get("name") == name:
                return v
        return None

    u02 = _volume("u02")
    if not u02 or (u02.get("persistentVolumeClaim") or {}).get("claimName") != expected_pvc_name:
        raise Phase5Error(f"StatefulSet u02 volume does not use persistentVolumeClaim.claimName={expected_pvc_name!r} (found: {u02!r}).")
    u03 = _volume("u03")
    if not u03 or "emptyDir" not in u03:
        raise Phase5Error(f"StatefulSet u03 volume is not emptyDir (found: {u03!r}).")
    print("OK: EFS StorageClass, runtime PVC, and StatefulSet u02/u03 volumes all match the expected persistence configuration.")


def _validate_efs_render_contract(values, docs, environment, deployment_id, deployment_model, resolved_efs_id, efs_mode_state, efs_file_system_id_declared_state):
    efs_enabled, facts = _validate_efs_values_shape(values, deployment_id, deployment_model)
    if not efs_enabled:
        print("persistence.enabled/provider=efs not set in the deployment values file. Skipping EFS persistence render checks.")
        return

    if not resolved_efs_id:
        raise Phase5Error("persistence.enabled=true and provider=efs, but resolved_efs_id is empty. EFS resolution must run before this validation.")
    if facts["mode"] == "existing" and resolved_efs_id != efs_file_system_id_declared_state:
        raise Phase5Error(f"resolved EFS ID ({resolved_efs_id}) does not match the declared persistence.efs.fileSystemId ({efs_file_system_id_declared_state}) for mode=existing.")

    print(f"persistence.enabled=true and provider=efs. Expected EFS fileSystemId: {resolved_efs_id}. Expected EFS basePath: {facts['base_path']}")
    _validate_rendered_storageclass_and_pvc(docs, environment, deployment_id, resolved_efs_id, facts["base_path"])


def _package_runtime_chart(deployment_id, values_file, chart_version):
    # 1. Refuses to build/package from an untrusted chart source tree -- FULL tree (root AND every descendant), independently re-validated here (defense in depth, never merely trusting an earlier cmd_validate_local() check from a different call frame) BEFORE copytree ever reads from it.
    chart_root, _source_files = _validate_canonical_chart_source_tree()
    # 2. Refuses to write into a symlinked packaged/ output directory -- validated/created BEFORE copytree or helm package ever runs, so a symlinked packaged/ receives ZERO generated files on failure.
    packaged_dir = _prepare_canonical_packaged_output_root()

    # 3. Only now does the temporary chart get constructed/copied -- symlinks=True so copytree can never silently dereference a chart-source symlink (e.g. one introduced between the tree scan above and this copy) into copied regular content.
    temp_chart_path = REPO_ROOT / _canonical_temp_chart_path(deployment_id)
    if temp_chart_path.exists():
        shutil.rmtree(temp_chart_path)
    temp_chart_path.mkdir(parents=True)
    shutil.copytree(chart_root, temp_chart_path, dirs_exist_ok=True, symlinks=True)
    shutil.copy(REPO_ROOT / values_file, temp_chart_path / "values-deployment.yaml")

    # Defense in depth: the copied TEMPORARY chart must independently be proven symlink-free too, BEFORE helm package ever runs -- catches a symlink that copytree above preserved as a symlink (per symlinks=True) rather than ever letting it reach helm package as an unvalidated object.
    _require_symlink_free_tree(temp_chart_path, "temporary copied chart")

    # 4. Only now does helm package run, writing into the validated ABSOLUTE packaged directory -- never a bare "packaged" relative literal at this filesystem trust boundary.
    run(["helm", "package", str(temp_chart_path), "--version", chart_version, "--app-version", chart_version, "--destination", str(packaged_dir)])

    # Post-package defense in depth: re-derives and re-validates the canonical package path/containment (never merely package_path.is_file()) -- proves packaged/ is still canonical, the archive is a real regular file, not a symlink, and resolves inside the canonical packaged directory.
    package_path_rel = _canonical_package_path(chart_version)
    resolved_package_path = _validate_package_path_and_containment(package_path_rel, chart_version)
    return temp_chart_path, resolved_package_path


def cmd_validate_local(args):
    environment = require_environment_arg(args.environment)
    deployment_id = require_deployment_id_arg(args.deployment_id)
    state = load_state(args.state_path)

    # Static reconcile-state identity binding FIRST -- before any values-file read, Helm command, rendered-output write, or packaging -- so a stale/cross-runtime state can never cause this step to read/render/package another deployment's files. verify_managed_efs_live=False because this step deliberately receives NO AWS credentials.
    _validate_reconcile_state_identity(state, environment, deployment_id)
    resolved = _validate_resolved_runtime_inputs(state, environment, deployment_id, verify_managed_efs_live=False)

    # Canonical FULL chart-source-TREE trust boundary -- not merely the root -- validated BEFORE any required-file read, rendered-output write, or Helm command ever inspects/executes the chart. helm dependency build in particular may perform remote dependency access and/or filesystem writes driven by an untrusted Chart.yaml -- every descendant (a symlinked template, a symlinked nested directory, etc.) must be proven a real regular file/directory first, never trusted-then-checked or discovered only later by package-publication validation. The returned chart_root is used for every chart operation below; HELM_CHART_PATH is never read independently again in this function.
    chart_root, _source_files = _validate_canonical_chart_source_tree()

    values_file = require_state_value(state, "values_file")
    release_name = require_state_value(state, "release_name")
    target_namespace = require_state_value(state, "target_namespace")
    deployment_model = require_state_value(state, "deployment_model")
    admin_secret_name = resolved["admin_secret_name"]
    tls_secret_name = resolved["tls_secret_name"]
    runtime_service_account_name = resolved["runtime_service_account_name"]
    image_repository = resolved["image_repository"]
    image_tag = resolved["image_tag"]
    image_digest = require_state_value(state, "image_digest")
    dns_domain = resolved["dns_domain"]
    alb_group_name = resolved["alb_group_name"]
    certificate_arn = resolved["certificate_arn"]
    resolved_efs_id = resolved["resolved_efs_id"]
    efs_mode = resolved["efs_mode"]
    efs_file_system_id_declared = resolved["efs_file_system_id_declared"]
    chart_version = require_state_value(state, "chart_version")
    aws_region = require_env("AWS_REGION")

    _validate_required_files(values_file, chart_root)

    overrides = _helm_set_overrides(environment, image_repository, dns_domain, alb_group_name, certificate_arn,
                                     admin_secret_name, tls_secret_name, aws_region, runtime_service_account_name, resolved_efs_id)

    # Current source deliberately tolerates a Helm dependency-build failure here (never generalized to any other Helm command) -- subsequent lint/template checks are authoritative.
    dep_proc = run(["helm", "dependency", "build", str(chart_root)], check=False)
    if dep_proc.returncode != 0:
        print(f"WARNING: helm dependency build exited {dep_proc.returncode} (tolerated -- subsequent lint/template checks are authoritative):\n{dep_proc.stdout}\n{dep_proc.stderr}")

    run(["helm", "lint", str(chart_root), "--values", values_file, *overrides])
    print("OK: helm lint passed.")

    rendered_path = REPO_ROOT / _canonical_rendered_manifest_path(deployment_id)
    rendered_path.parent.mkdir(parents=True, exist_ok=True)
    template_proc = run(["helm", "template", release_name, str(chart_root), "--namespace", target_namespace, "--values", values_file, *overrides])
    rendered_path.write_text(template_proc.stdout)
    print(f"Rendered manifest: {rendered_path.relative_to(REPO_ROOT)}")

    docs = _parse_rendered_documents(rendered_path.read_text())
    with (REPO_ROOT / values_file).open() as f:
        values = yaml.safe_load(f) or {}

    _validate_zero_namespace_documents(docs)
    _validate_runtime_service_account_used(docs, runtime_service_account_name)
    _validate_admin_secret_csi_isolation(docs, admin_secret_name, environment, deployment_id)
    _validate_singleruntime_manifest_contract(docs, values, deployment_id, target_namespace, image_repository, image_tag, image_digest)
    _validate_efs_render_contract(values, docs, environment, deployment_id, deployment_model, resolved_efs_id, efs_mode, efs_file_system_id_declared)

    temp_chart_path, package_path = _package_runtime_chart(deployment_id, values_file, chart_version)

    update_state(args.state_path, {
        "rendered_manifest": str(rendered_path.relative_to(REPO_ROOT)),
        "package_path": str(package_path.relative_to(REPO_ROOT)),
        "temp_chart_path": str(temp_chart_path.relative_to(REPO_ROOT)),
    }, RECONCILE_ALLOWED_STATE_KEYS)
    print("OK: GoldenGate runtime chart validated and packaged locally.")


# Phase 5B, step 4: publish-chart (AWS credentials required, Deploy only)

def _describe_ecr_repository(repository_name, aws_region):
    return run(["aws", "ecr", "describe-repositories", "--region", aws_region, "--repository-names", repository_name], check=False)


def _create_ecr_repository(repository_name, aws_region):
    run([
        "aws", "ecr", "create-repository",
        "--region", aws_region,
        "--repository-name", repository_name,
        "--tags",
        "Key=ApplicationName,Value=CloudFactory",
        "Key=DataClassification,Value=General",
        "Key=BusinessCriticality,Value=Low",
        "Key=BusinessUnit,Value=TechnologyPlatform",
        "Key=CostCenter,Value=219",
        "--image-scanning-configuration", "scanOnPush=true",
        "--image-tag-mutability", "MUTABLE",
    ])


def _ensure_ecr_repository(repository_name, aws_region):
    """Fail-closed repository-existence classification: an inability to inspect state (AccessDenied/ExpiredToken/throttling/network/unknown error) must never be interpreted as "does not exist" -- only an explicit RepositoryNotFoundException from describe-repositories authorizes create-repository."""
    exists = _describe_ecr_repository(repository_name, aws_region)
    if exists.returncode == 0:
        print(f"ECR repository already exists: {repository_name}")
        return

    error_text = (exists.stderr or "") + (exists.stdout or "")
    if "RepositoryNotFoundException" not in error_text:
        raise Phase5Error(
            f"could not determine whether ECR repository {repository_name!r} exists (describe-repositories exited {exists.returncode} without an explicit RepositoryNotFoundException) -- "
            f"refusing to guess and refusing to create it. Failing closed:\n{error_text.strip() or '(no output)'}"
        )

    print(f"Creating ECR repository: {repository_name}")
    try:
        _create_ecr_repository(repository_name, aws_region)
    except Phase5Error as exc:
        # Race-safe handling: another actor may have created the repository between our describe and our create. RepositoryAlreadyExistsException is the ONLY create failure tolerated here, and only after confirming via a fresh describe-repositories that the repository now genuinely exists.
        if "RepositoryAlreadyExistsException" not in str(exc):
            raise
        recheck = _describe_ecr_repository(repository_name, aws_region)
        if recheck.returncode != 0:
            raise Phase5Error(f"ECR repository {repository_name!r} creation raced with another actor (RepositoryAlreadyExistsException), but the required re-describe still did not succeed:\n{((recheck.stderr or '') + (recheck.stdout or '')).strip() or '(no output)'}") from exc
    print(f"Created ECR repository: {repository_name}")


def _ensure_ecr_repository_policy(repository_name, aws_region, argocd_ecr_read_role_arn):
    """Preserves the controlled merge behavior for Sid AllowArgocdEksRolePullGoldengateHelmChart -- unrelated existing statements are preserved untouched. RepositoryPolicyNotFoundException initializes an empty policy; any OTHER get-repository-policy failure (including AccessDenied) fails closed rather than being silently treated as "no policy"."""
    proc = run(["aws", "ecr", "get-repository-policy", "--region", aws_region, "--repository-name", repository_name, "--query", "policyText", "--output", "text"], check=False)
    if proc.returncode == 0:
        policy = json.loads(proc.stdout)
    else:
        error_text = (proc.stderr or "") + (proc.stdout or "")
        if "RepositoryPolicyNotFoundException" not in error_text:
            raise Phase5Error(f"failed to read the existing ECR repository policy for {repository_name!r}; refusing to assume it is absent:\n{error_text.strip() or '(no output)'}")
        print("No existing repository policy found. Starting from an empty policy.")
        policy = {"Version": "2012-10-17", "Statement": []}

    statements = [s for s in policy.get("Statement", []) if s.get("Sid") != ARGOCD_ECR_STATEMENT_SID]
    statements.append({
        "Sid": ARGOCD_ECR_STATEMENT_SID,
        "Effect": "Allow",
        "Principal": {"AWS": argocd_ecr_read_role_arn},
        "Action": list(REPOSITORY_PULL_ACTIONS),
    })
    policy["Statement"] = statements

    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        json.dump(policy, tmp)
        tmp_path = tmp.name
    try:
        run(["aws", "ecr", "set-repository-policy", "--region", aws_region, "--repository-name", repository_name, "--policy-text", f"file://{tmp_path}"])
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    print(f"ECR repository policy on {repository_name} now allows pull from {argocd_ecr_read_role_arn}.")


def cmd_publish_chart(args):
    environment = require_environment_arg(args.environment)
    deployment_id = require_deployment_id_arg(args.deployment_id)
    state = load_state(args.state_path)

    # Mutation boundary, independent of the workflow's own matrix.deploy gate: proves this state genuinely belongs to the current matrix runtime AND is a Deploy (not Validate) state BEFORE any AWS/ECR credential use or mutation, even if this subcommand is ever invoked directly.
    deploy = _validate_reconcile_state_identity(state, environment, deployment_id)
    if not deploy:
        raise Phase5Error(f"Phase 5 reconcile state for {deployment_id} has deploy=false (Validate-mode) -- publish-chart is a Deploy-only AWS/ECR mutation boundary and refuses to run against a Validate state, even when invoked directly. This is defense in depth; the workflow itself already gates this step with matrix.deploy.")

    chart_version = require_state_value(state, "chart_version")  # already proven canonical by _validate_reconcile_state_identity above
    package_path_rel = require_state_value(state, "package_path")
    helm_push_url = require_state_value(state, "helm_push_url")
    helm_chart_ref = require_state_value(state, "helm_chart_ref")

    # Package/artifact binding -- ALL of this happens BEFORE any AWS/ECR call: exact canonical package path + containment (defeats ../, absolute-path substitution, and symlink escape), then the packaged Chart.yaml/values-deployment.yaml are inspected via stdlib tarfile (never a shell tar) to prove the archive genuinely belongs to THIS runtime's current chart version and values file -- a state file cannot point this job at a different local chart package/version merely by looking otherwise-canonical.
    resolved_package_path = _validate_package_path_and_containment(package_path_rel, chart_version)
    _validate_packaged_chart_contents(resolved_package_path, chart_version, environment, deployment_id)

    aws_region = require_env("AWS_REGION")
    ecr_registry = require_env("ECR_REGISTRY")
    argocd_ecr_read_role_arn = require_env("ARGOCD_ECR_READ_ROLE_ARN")

    # ECR login password: fed directly into helm's own stdin, never through a shell pipeline and never printed/logged/stored.
    password_proc = run(["aws", "ecr", "get-login-password", "--region", aws_region])
    password = password_proc.stdout.strip()
    run(["helm", "registry", "login", "--username", "AWS", "--password-stdin", ecr_registry], input_text=password)
    del password

    _ensure_ecr_repository(HELM_ECR_REPOSITORY, aws_region)
    _ensure_ecr_repository_policy(HELM_ECR_REPOSITORY, aws_region, argocd_ecr_read_role_arn)

    run(["helm", "push", str(resolved_package_path), helm_push_url])
    print(f"Published Helm chart: {helm_chart_ref}:{chart_version}")

    pulled_dir = REPO_ROOT / PULLED_DIRECTORY
    pulled_dir.mkdir(parents=True, exist_ok=True)
    run(["helm", "pull", helm_chart_ref, "--version", chart_version, "--destination", PULLED_DIRECTORY])

    update_state(args.state_path, {"pulled_directory": PULLED_DIRECTORY}, RECONCILE_ALLOWED_STATE_KEYS)
    print("OK: GoldenGate runtime Helm chart published to private ECR and verified pullable.")


# Phase 5B, step 5: validate-cluster-prerequisites (AWS credentials required, Deploy only)

def _validate_sync_secret_enabled(raw_json):
    try:
        values = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise Phase5Error(f"helm get values --output json produced malformed JSON: {exc}") from exc
    if not isinstance(values, dict):
        raise Phase5Error(f"helm get values --output json produced a top-level {type(values).__name__}, expected a JSON object.")
    sync_secret = values.get("syncSecret")
    if not isinstance(sync_secret, dict):
        raise Phase5Error(f"syncSecret is {sync_secret!r} (missing or not an object) in the computed Helm values for secrets-store-csi-driver.")
    if "enabled" not in sync_secret:
        raise Phase5Error("syncSecret.enabled is not present in the computed Helm values for secrets-store-csi-driver.")
    enabled = sync_secret["enabled"]
    # Strict identity check (never ==): JSON true decodes to the Python True singleton only -- 1, "true", and every other JSON-boolean-like value must still fail closed.
    if enabled is not True:
        raise Phase5Error(f"syncSecret.enabled is {enabled!r} ({type(enabled).__name__}), expected the literal JSON boolean true.")
    print("syncSecret.enabled=true confirmed (structural JSON check).")


def _classify_helm_release_status(release_name, namespace):
    """Fail-closed Helm release-existence classification: "present" when `helm status` succeeds, "not-found" ONLY when the failure is an EXPLICIT Helm release-not-found result (the release may genuinely be managed outside Helm) -- every other failure (cluster unreachable, Forbidden, Unauthorized, timeout/context deadline, network/TLS failure, malformed/unknown/empty error) raises Phase5Error. A generic non-zero exit code is never, by itself, inferred as absence."""
    proc = run(["helm", "status", release_name, "-n", namespace], check=False)
    if proc.returncode == 0:
        return "present"
    error_text = ((proc.stderr or "") + (proc.stdout or "")).strip()
    if "release: not found" in error_text.lower():
        return "not-found"
    raise Phase5Error(
        f"could not determine whether Helm release {release_name!r} exists in namespace {namespace!r} "
        f"(helm status exited {proc.returncode} without an explicit 'release: not found') -- refusing to guess absence. "
        f"Failing closed:\n{error_text or '(no output)'}"
    )


def _validate_csi_prerequisites():
    if run(["kubectl", "get", "csidriver", "secrets-store.csi.k8s.io"], check=False).returncode != 0:
        raise Phase5Error("CSIDriver secrets-store.csi.k8s.io not found. Secrets Store CSI Driver is not installed on this cluster.")
    if run(["kubectl", "get", "crd", "secretproviderclasses.secrets-store.csi.x-k8s.io"], check=False).returncode != 0:
        raise Phase5Error("CRD secretproviderclasses.secrets-store.csi.x-k8s.io not found.")

    token_requests = run(["kubectl", "get", "csidriver", "secrets-store.csi.k8s.io", "-o", "jsonpath={.spec.tokenRequests}"]).stdout
    if "sts.amazonaws.com" not in token_requests:
        raise Phase5Error(f"CSIDriver secrets-store.csi.k8s.io is missing tokenRequests audience sts.amazonaws.com. Current tokenRequests: {token_requests}")
    if "pods.eks.amazonaws.com" not in token_requests:
        raise Phase5Error(f"CSIDriver secrets-store.csi.k8s.io is missing tokenRequests audience pods.eks.amazonaws.com. Current tokenRequests: {token_requests}")
    print(f"tokenRequests OK: {token_requests}")

    release_status = _classify_helm_release_status("secrets-store-csi-driver", "kube-system")
    if release_status == "present":
        values_proc = run(["helm", "get", "values", "secrets-store-csi-driver", "-n", "kube-system", "--all", "--output", "json"])
        _validate_sync_secret_enabled(values_proc.stdout)
    else:
        print("Helm release secrets-store-csi-driver not found in kube-system (explicit 'release: not found'). Skipping syncSecret check (driver may be managed outside Helm).")
    print("Secrets Store CSI Driver prerequisites validated.")


def cmd_validate_cluster_prerequisites(args):
    require_environment_arg(args.environment)
    require_deployment_id_arg(args.deployment_id)
    _connect_to_eks()
    run(["kubectl", "config", "current-context"])

    _validate_csi_prerequisites()

    if run(["kubectl", "get", "crd", "applications.argoproj.io"], check=False).returncode != 0:
        raise Phase5Error(
            "CRD applications.argoproj.io not found. Argo CD prerequisite is not healthy. This orchestrator's own "
            "argocd_preflight/reconcile_argocd/validate_argocd_ready jobs already classify and automatically reconcile "
            "Argo CD before this stage runs -- this check is defense in depth against an unexpected mid-DAG loss of the CRD."
        )
    print("Argo CD Application CRD is present.")
    print("OK: live EKS runtime prerequisites validated.")


# Phase 5B, step 6: reconcile-runtime (AWS credentials required, Deploy only)

def _build_runtime_application_manifest(argocd_app_name, argocd_namespace, environment, deployment_id, helm_chart_ref,
                                         chart_version, release_name, target_namespace, image_repository, dns_domain,
                                         alb_group_name, certificate_arn, admin_secret_name, tls_secret_name, aws_region,
                                         runtime_service_account_name, resolved_efs_id):
    """Exact runtime Application shape -- singleRuntime deploys into the shared goldengate-<environment> namespace (no CreateNamespace/managedNamespaceMetadata); singleRuntime does not own the shared namespace."""
    parameters = [
        {"name": "global.environment", "value": environment},
        {"name": "runtime.image.repository", "value": image_repository},
        {"name": "ingress.hostDomain", "value": dns_domain},
        {"name": "ingress.alb.groupName", "value": alb_group_name},
        {"name": "ingress.alb.certificateArn", "value": certificate_arn},
        {"name": "runtime.csi.admin.objectName", "value": admin_secret_name},
        {"name": "runtime.csi.certificate.objectName", "value": tls_secret_name},
        {"name": "runtime.csi.region", "value": aws_region},
        {"name": "runtime.serviceAccount.create", "value": "false"},
        {"name": "runtime.serviceAccount.name", "value": runtime_service_account_name},
        {"name": "persistence.efs.fileSystemId", "value": resolved_efs_id},
    ]
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {
            "name": argocd_app_name,
            "namespace": argocd_namespace,
            "finalizers": ["resources-finalizer.argocd.argoproj.io"],
            "labels": {
                "app.kubernetes.io/name": "goldengate",
                "app.kubernetes.io/managed-by": "argocd",
                "goldengate.adcb/environment": environment,
                "goldengate.adcb/deployment-id": deployment_id,
            },
        },
        "spec": {
            "project": "default",
            "source": {
                "repoURL": helm_chart_ref,
                "targetRevision": chart_version,
                "path": ".",
                "helm": {"releaseName": release_name, "valueFiles": ["values-deployment.yaml"], "parameters": parameters},
            },
            "destination": {"server": "https://kubernetes.default.svc", "namespace": target_namespace},
            "syncPolicy": {"automated": {"prune": True, "selfHeal": True}},
            "revisionHistoryLimit": 10,
        },
    }


def _wait_for_runtime_argo_application(app_name, namespace, timeout_seconds, interval_seconds):
    elapsed = 0
    while True:
        exists = run(["kubectl", "get", "application", app_name, "-n", namespace], check=False)
        if exists.returncode == 0:
            sync_status = _kubectl_get_jsonpath("application", app_name, namespace, "{.status.sync.status}") or "Unknown"
            health_status = _kubectl_get_jsonpath("application", app_name, namespace, "{.status.health.status}") or "Unknown"
            operation_phase = _kubectl_get_jsonpath("application", app_name, namespace, "{.status.operationState.phase}") or ""
            print(f"sync={sync_status} health={health_status} operation={operation_phase or 'none'} (elapsed {elapsed}s / {timeout_seconds}s)")

            if health_status == "Degraded":
                run(["kubectl", "get", "application", app_name, "-n", namespace, "-o", "wide"], check=False)
                raise Phase5Error(f"Argo CD Application {app_name} is Degraded.")
            if operation_phase in ("Failed", "Error"):
                run(["kubectl", "describe", "application", app_name, "-n", namespace], check=False)
                raise Phase5Error(f"Argo CD sync operation for {app_name} {operation_phase}.")
            if sync_status == "Synced" and health_status == "Healthy":
                print(f"Argo CD Application {app_name} is Synced and Healthy.")
                return
        else:
            print(f"Argo CD Application {app_name} not found yet (elapsed {elapsed}s / {timeout_seconds}s).")

        if elapsed >= timeout_seconds:
            run(["kubectl", "get", "application", app_name, "-n", namespace, "-o", "wide"], check=False)
            run(["kubectl", "describe", "application", app_name, "-n", namespace], check=False)
            raise Phase5Error(f"Timed out after {timeout_seconds}s waiting for {app_name} to become Synced and Healthy.")

        time.sleep(interval_seconds)
        elapsed += interval_seconds


def cmd_reconcile_runtime(args):
    environment = require_environment_arg(args.environment)
    deployment_id = require_deployment_id_arg(args.deployment_id)
    state = load_state(args.state_path)

    # State identity/deploy-boolean binding BEFORE resolved-input validation, _connect_to_eks(), the emergency credential Secret, kubectl apply, or kubectl annotate -- a malformed/cross-runtime state file must result in ZERO Kubernetes calls.
    deploy = _validate_reconcile_state_identity(state, environment, deployment_id)
    if not deploy:
        raise Phase5Error(f"Phase 5 reconcile state for {deployment_id} has deploy=false (Validate-mode) -- reconcile-runtime is a Deploy-only Kubernetes mutation boundary and refuses to run against a Validate state, even when invoked directly.")

    # Resolved-input validation with verify_managed_efs_live=True -- for managed EFS this performs a fresh, read-only STS/EFS re-resolution and requires it to exactly match the persisted resolved_efs_id BEFORE any Kubernetes mutation. Still before _connect_to_eks(): this uses AWS credentials directly, never kubectl.
    resolved = _validate_resolved_runtime_inputs(state, environment, deployment_id, verify_managed_efs_live=True)

    # Only the freshly-recomputed canonical values are ever used for the mutation target/payload -- never merely the (already-proven-matching) state-sourced copies.
    argocd_app_name = _canonical_argocd_app_name(environment, deployment_id)
    release_name = deployment_id
    target_namespace = require_env("RUNTIME_NAMESPACE")
    ecr_registry = require_env("ECR_REGISTRY")
    helm_chart_ref = f"oci://{ecr_registry}/{HELM_ECR_REPOSITORY}"
    chart_version = require_state_value(state, "chart_version")  # already proven canonical by _validate_reconcile_state_identity above
    image_repository = resolved["image_repository"]
    dns_domain = resolved["dns_domain"]
    alb_group_name = resolved["alb_group_name"]
    certificate_arn = resolved["certificate_arn"]
    admin_secret_name = resolved["admin_secret_name"]
    tls_secret_name = resolved["tls_secret_name"]
    runtime_service_account_name = resolved["runtime_service_account_name"]
    resolved_efs_id = resolved["resolved_efs_id"]

    aws_region, _ = _connect_to_eks()
    argocd_namespace = require_env("ARGOCD_NAMESPACE")

    if run(["kubectl", "get", "crd", "applications.argoproj.io"], check=False).returncode != 0:
        raise Phase5Error("CRD applications.argoproj.io not found. Argo CD prerequisite is not healthy.")

    # Emergency fallback only; long-term auth is handled in-cluster by argocd-ecr-token-sync (IRSA role ARGOCD_ECR_READ_ROLE_ARN).
    if os.environ.get("ENABLE_TEMP_ARGOCD_ECR_PASSWORD_INJECTION") == "true":
        print("EMERGENCY FALLBACK: injecting a short-lived ECR password into Argo CD.")
        ecr_registry = require_env("ECR_REGISTRY")
        password = run(["aws", "ecr", "get-login-password", "--region", aws_region]).stdout.strip()
        secret_manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "argocd-ecr-goldengate-oci", "namespace": argocd_namespace, "labels": {"argocd.argoproj.io/secret-type": "repository"}},
            "stringData": {
                "type": "helm", "enableOCI": "true", "url": f"{ecr_registry}/{HELM_ECR_REPOSITORY}",
                "username": "AWS", "password": password,
            },
        }
        run(["kubectl", "apply", "-f", "-"], input_text=yaml.safe_dump(secret_manifest, default_flow_style=False), check=True)
        del password
        print("Argo CD repository credentials Secret applied: argocd-ecr-goldengate-oci")

    manifest = _build_runtime_application_manifest(
        argocd_app_name, argocd_namespace, environment, deployment_id, helm_chart_ref, chart_version, release_name,
        target_namespace, image_repository, dns_domain, alb_group_name, certificate_arn, admin_secret_name,
        tls_secret_name, aws_region, runtime_service_account_name, resolved_efs_id,
    )
    manifest_yaml = yaml.safe_dump(manifest, default_flow_style=False, sort_keys=False)
    run(["kubectl", "apply", "-f", "-"], input_text=manifest_yaml)
    run(["kubectl", "annotate", "application", argocd_app_name, "-n", argocd_namespace, "argocd.argoproj.io/refresh=hard", "--overwrite"])

    _wait_for_runtime_argo_application(argocd_app_name, argocd_namespace, timeout_seconds=1200, interval_seconds=30)
    print("OK: GoldenGate runtime Argo CD Application reconciled.")


# Phase 5B, step 7: post-deploy-diagnostics (AWS credentials required, Deploy only, non-authoritative)

def cmd_post_deploy_diagnostics(args):
    environment = require_environment_arg(args.environment)
    deployment_id = require_deployment_id_arg(args.deployment_id)
    state = load_state(args.state_path)

    # Static reconcile-state identity binding BEFORE _connect_to_eks() -- non-authoritative diagnostics must never query/log a different runtime's Application/namespace due to a stale/cross-runtime state file. Only the freshly-recomputed canonical values are used below, never state-sourced copies.
    _validate_reconcile_state_identity(state, environment, deployment_id)
    argocd_app_name = _canonical_argocd_app_name(environment, deployment_id)
    target_namespace = require_env("RUNTIME_NAMESPACE")

    _connect_to_eks()
    argocd_namespace = require_env("ARGOCD_NAMESPACE")

    run(["kubectl", "get", "application", argocd_app_name, "-n", argocd_namespace, "-o", "wide"], check=False)
    run(["kubectl", "describe", "application", argocd_app_name, "-n", argocd_namespace], check=False)
    run(["kubectl", "get", "all", "-n", target_namespace], check=False)
    run(["kubectl", "get", "ingress", "-n", target_namespace], check=False)
    run(["kubectl", "get", "pvc", "-n", target_namespace], check=False)
    sc_proc = run(["kubectl", "get", "storageclass"], check=False)
    expected_sc_name = f"gg-efs-{environment}-{deployment_id}"
    if sc_proc.returncode == 0 and expected_sc_name in sc_proc.stdout:
        print(f"EFS StorageClass for this deployment found: {expected_sc_name}")
    run(["kubectl", "get", "pv"], check=False)
    run(["kubectl", "describe", "pvc", "-n", target_namespace], check=False)
    print("OK: post-deployment diagnostics collected (non-authoritative -- Phase 5D performs independent strict runtime acceptance afterward).")


# Phase 5C, step 1: prepare-removal (no AWS credentials -- every static safety check happens before any credential is even loaded)

def cmd_prepare_removal(args):
    environment = require_environment_arg(args.environment)
    deployment_id = require_deployment_id_arg(args.deployment_id)
    deployment_model = args.deployment_model
    efs_mode = args.efs_mode or ""
    reason = args.reason

    if deployment_model not in ("singleRuntime", "legacyPair"):
        raise Phase5Error(f"unrecognized deployment_model {deployment_model!r} from the deletion matrix (expected exactly 'singleRuntime' or 'legacyPair'). Refusing to guess or default this value -- it directly controls which namespace/Application this job targets for deletion.")
    if reason not in ("deployment-disabled", "physical-removal"):
        raise Phase5Error(f"unrecognized deletion reason {reason!r} (expected exactly 'deployment-disabled' or 'physical-removal').")
    if efs_mode not in ("", "existing", "managed"):
        raise Phase5Error(f"unrecognized efs_mode {efs_mode!r} (expected '', 'existing', or 'managed').")

    # legacyPair is retired: there is no destructive allow-list state for it here -- it fails BEFORE any mutation, before AWS credentials are even loaded. Manual/out-of-band cleanup remains required for historical legacyPair footprints; no second legacy ownership classifier is built to keep automatic deletion working.
    if deployment_model != "singleRuntime":
        raise Phase5Error(f"deployment_model={deployment_model} is not a currently supported runtime deployment model for automatic removal (only singleRuntime is) -- refusing to patch finalizers or delete anything for {deployment_id}. This requires manual, out-of-band investigation and cleanup, never an automatic bypass.")

    # Managed physical-removal defense: physically removing the Terraform descriptor for a MANAGED durable EFS filesystem would leave Terraform unable to find/manage it. Fails BEFORE any Kubernetes mutation. Terraform remains the sole managed-EFS lifecycle owner.
    if reason == "physical-removal" and efs_mode == "managed":
        raise Phase5Error(f"reason=physical-removal with efs_mode=managed for {deployment_id} -- refusing before any Kubernetes mutation. Physically removing the descriptor for a MANAGED durable EFS filesystem is unsafe; Terraform remains the sole managed-EFS lifecycle owner.")

    runtime_namespace = require_env("RUNTIME_NAMESPACE")
    argocd_namespace = require_env("ARGOCD_NAMESPACE")
    argocd_app_name = _canonical_argocd_app_name(environment, deployment_id)

    update_state(args.state_path, {
        "environment": environment, "deployment_id": deployment_id, "deployment_model": deployment_model,
        "efs_mode": efs_mode, "reason": reason, "runtime_namespace": runtime_namespace,
        "argocd_namespace": argocd_namespace, "argocd_app_name": argocd_app_name,
    }, REMOVAL_ALLOWED_STATE_KEYS)
    print(f"OK: removal prepared for {deployment_id} (reason={reason}, efs_mode={efs_mode or '<none>'}).")


def _retained_pvc_expected_for_removal(efs_mode):
    """A physical-removal+managed entry never reaches this hint in practice (prepare-removal already failed closed before any mutation) -- the hint itself never bypasses that guard, it only affects whether a LEGITIMATE retained-PVC shape is recognized as safe post-removal."""
    return efs_mode in ("existing", "managed")


_runtime_state_module = None


def _load_runtime_state_module():
    """Lazy import of automation/phases/phase5/runtime_state.py -- the single canonical owner of the runtime footprint-key schema (RUNTIME_FOOTPRINT_KEYS). Never a second, independently-maintained footprint-key list here."""
    global _runtime_state_module
    if _runtime_state_module is None:
        spec = importlib.util.spec_from_file_location("runtime_state", RUNTIME_STATE_TOOL)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _runtime_state_module = module
    return _runtime_state_module


def _validate_runtime_state_classifier_output(result):
    """Strict schema validation of a runtime_state.py classifier result -- used BEFORE it is trusted for any removal-preflight or post-delete-acceptance decision. Requires: result is a JSON object; state is exactly one of ABSENT/OWNED/BROKEN; checks is a JSON object; checks.application_found is a literal JSON boolean (never a truthiness-coerced string/int -- bool("false") is True in Python, so this is checked with isinstance, never bool(...)); checks.footprint_found is a JSON object containing EXACTLY the canonical runtime_state.RUNTIME_FOOTPRINT_KEYS key set, every value a literal JSON boolean. Raises Phase5Error on any deviation -- a malformed/incomplete classifier shape must never be silently treated as "everything reads as absent". Returns (state, application_found, footprint_found) only once every check has passed."""
    if not isinstance(result, dict):
        raise Phase5Error(f"runtime ownership classifier output is a {type(result).__name__}, expected a JSON object.")

    state = result.get("state")
    if state not in ("ABSENT", "OWNED", "BROKEN"):
        raise Phase5Error(f"runtime ownership classifier produced an unrecognized or missing state {state!r}; refusing to proceed.")

    checks = result.get("checks")
    if not isinstance(checks, dict):
        raise Phase5Error(f"runtime ownership classifier output is missing a 'checks' object (got {checks!r}).")

    application_found = checks.get("application_found")
    if not isinstance(application_found, bool):
        raise Phase5Error(f"runtime ownership classifier checks.application_found is {application_found!r} ({type(application_found).__name__}), expected a literal JSON boolean.")

    footprint_found = checks.get("footprint_found")
    if not isinstance(footprint_found, dict):
        raise Phase5Error(f"runtime ownership classifier checks.footprint_found is {footprint_found!r}, expected a JSON object.")

    expected_keys = set(_load_runtime_state_module().RUNTIME_FOOTPRINT_KEYS)
    actual_keys = set(footprint_found)
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        unexpected = sorted(actual_keys - expected_keys)
        raise Phase5Error(f"runtime ownership classifier checks.footprint_found does not match the canonical footprint schema (missing: {missing}, unexpected: {unexpected}).")

    for key, value in footprint_found.items():
        if not isinstance(value, bool):
            raise Phase5Error(f"runtime ownership classifier checks.footprint_found[{key!r}] is {value!r} ({type(value).__name__}), expected a literal JSON boolean.")

    return state, application_found, footprint_found


# Phase 5C, step 2: removal-preflight (AWS credentials required)

def cmd_removal_preflight(args):
    environment = require_environment_arg(args.environment)
    deployment_id = require_deployment_id_arg(args.deployment_id)
    state = load_state(args.state_path)

    # Static removal-state identity binding BEFORE _connect_to_eks() -- never trust a corrupted efs_mode to decide retained_pvc_expected; only the validated value is ever used.
    efs_mode = _validate_removal_state_identity(state, environment, deployment_id)

    _connect_to_eks()

    retained_pvc_expected = _retained_pvc_expected_for_removal(efs_mode)
    cli_args = [sys.executable, str(RUNTIME_STATE_TOOL), "--environment", environment, "--deployment-id", deployment_id]
    if retained_pvc_expected:
        cli_args.append("--retained-pvc-expected")
    proc = run(cli_args, check=False)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    if proc.returncode != 0:
        raise Phase5Error(f"the GoldenGate runtime ownership classifier could not classify {deployment_id} (configuration or inspection error, not ABSENT) -- refusing to proceed with removal. See diagnostics above.")

    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Phase5Error(f"the GoldenGate runtime ownership classifier produced unparseable output: {exc}") from exc

    # Strict schema validation BEFORE anything is persisted -- application_found/footprint_found are never truthiness-coerced (bool("false") is True in Python), and an incomplete footprint_found can never be silently treated as "everything absent".
    ownership_state, application_found, footprint_found = _validate_runtime_state_classifier_output(result)
    if ownership_state == "BROKEN":
        raise Phase5Error(f"GoldenGate runtime ownership-safety state for {deployment_id} is BROKEN -- an existing Argo CD Application/footprint does not clearly belong to this deployment (foreign or ambiguous ownership). Refusing to patch finalizers or delete anything.")

    update_state(args.state_path, {
        "ownership_state": ownership_state,
        "application_found": application_found,
        "footprint_found": footprint_found,
    }, REMOVAL_ALLOWED_STATE_KEYS)
    if ownership_state == "ABSENT":
        print(f"Nothing exists for {deployment_id} -- the removal steps below will each independently no-op.")
    else:
        print(f"{deployment_id} is OWNED -- safe to proceed with removal.")


# Phase 5C, step 3: remove-runtime (AWS credentials required)

def _validate_removal_mutation_state(state):
    """Mutation-boundary defense in depth, applied BEFORE any cluster connection or mutating kubectl call: strict schema+semantic validation of the persisted removal state, so state-file corruption, an unexpected future producer regression, or a manually edited/malformed state file can never fall back on truthiness coercion (bool("false") is True in Python) to authorize an Argo CD Application patch/delete. Reuses the same canonical runtime_state.RUNTIME_FOOTPRINT_KEYS schema removal-preflight already enforces -- never a second, independently-drifting key list. Returns (ownership_state, application_found, argocd_app_name, argocd_namespace) only once every check has passed."""
    ownership_state = state.get("ownership_state")
    if ownership_state not in ("ABSENT", "OWNED"):
        raise Phase5Error(f"removal state ownership_state is {ownership_state!r}, expected ABSENT or OWNED -- refusing to mutate anything.")

    application_found = state.get("application_found")
    if not isinstance(application_found, bool):
        raise Phase5Error(f"removal state application_found is {application_found!r} ({type(application_found).__name__}), expected a literal boolean -- refusing to mutate anything.")

    footprint_found = state.get("footprint_found")
    if not isinstance(footprint_found, dict):
        raise Phase5Error(f"removal state footprint_found is {footprint_found!r}, expected a JSON object -- refusing to mutate anything.")

    expected_keys = set(_load_runtime_state_module().RUNTIME_FOOTPRINT_KEYS)
    actual_keys = set(footprint_found)
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        unexpected = sorted(actual_keys - expected_keys)
        raise Phase5Error(f"removal state footprint_found does not match the canonical footprint schema (missing: {missing}, unexpected: {unexpected}) -- refusing to mutate anything.")

    for key, value in footprint_found.items():
        if not isinstance(value, bool):
            raise Phase5Error(f"removal state footprint_found[{key!r}] is {value!r} ({type(value).__name__}), expected a literal boolean -- refusing to mutate anything.")

    if ownership_state == "ABSENT" and application_found:
        raise Phase5Error("removal state is internally inconsistent (ownership_state=ABSENT but application_found=true) -- refusing to mutate anything.")

    argocd_app_name = state.get("argocd_app_name")
    if not isinstance(argocd_app_name, str) or not argocd_app_name:
        raise Phase5Error(f"removal state argocd_app_name is {argocd_app_name!r}, expected a non-empty string -- refusing to mutate anything.")

    argocd_namespace = state.get("argocd_namespace")
    if not isinstance(argocd_namespace, str) or not argocd_namespace:
        raise Phase5Error(f"removal state argocd_namespace is {argocd_namespace!r}, expected a non-empty string -- refusing to mutate anything.")

    return ownership_state, application_found, argocd_app_name, argocd_namespace


def cmd_remove_runtime(args):
    environment = require_environment_arg(args.environment)
    deployment_id = require_deployment_id_arg(args.deployment_id)
    state = load_state(args.state_path)

    # Both validated BEFORE any cluster connection or mutating call is ever issued -- a malformed OR cross-runtime state file results in ZERO Kubernetes calls: static identity binding (environment/deployment_id/canonical Application name/canonical Argo+runtime namespaces/deployment model/reason/efs_mode), extended (never replaced) by the already-approved ownership/application_found/footprint schema check.
    _validate_removal_state_identity(state, environment, deployment_id)
    ownership_state, application_found, _state_argocd_app_name, _state_argocd_namespace = _validate_removal_mutation_state(state)

    # Only the freshly-recomputed canonical values are ever used for the mutation target itself -- never merely the (already-proven-matching) state-sourced copies.
    argocd_app_name = _canonical_argocd_app_name(environment, deployment_id)
    argocd_namespace = require_env("ARGOCD_NAMESPACE")

    # Uses removal-preflight's own already-authoritative checks.application_found -- never a second, redundant "kubectl get application" to decide absence. If preflight already proved the Application absent, this step no-ops without touching the cluster at all.
    if not application_found:
        print(f"Argo CD Application {argocd_app_name} was not found by the removal-preflight classifier -- nothing to delete (no redundant re-inspection performed).")
        return

    _connect_to_eks()

    patch_proc = run(["kubectl", "patch", "application", argocd_app_name, "-n", argocd_namespace, "--type", "merge",
                       "-p", json.dumps({"metadata": {"finalizers": ["resources-finalizer.argocd.argoproj.io"]}})], check=False)
    if patch_proc.returncode != 0:
        raise Phase5Error(f"failed to patch finalizers on Argo CD Application {argocd_app_name}: {((patch_proc.stderr or '') + (patch_proc.stdout or '')).strip()}")

    delete_proc = run(["kubectl", "delete", "application", argocd_app_name, "-n", argocd_namespace, "--wait=true", "--timeout=10m"], check=False)
    if delete_proc.returncode != 0:
        raise Phase5Error(f"failed to delete Argo CD Application {argocd_app_name}: {((delete_proc.stderr or '') + (delete_proc.stdout or '')).strip()}")

    print(f"Argo CD Application {argocd_app_name} deleted. Argo CD will cascade-delete its managed resources.")
    print("The shared runtime namespace is never deleted by this workflow -- singleRuntime does not own it. The retained /u02 PersistentVolumeClaim (Prune=false), any EFS filesystem, and Secrets Manager secrets are never deleted here either.")


# Phase 5C, step 4: post-delete-acceptance (AWS credentials required)

def _post_delete_positively_absent(result, retained_pvc_expected):
    """Positive structural AND semantic proof of runtime-compute absence -- NEVER merely `state != BROKEN` (an OWNED state can legitimately mean the Application still exists and is correctly owned, which must NOT be accepted as deletion-complete), and NEVER inferred from a malformed/incomplete classifier shape (see _validate_runtime_state_classifier_output, the structural layer this function builds on). After structural validation, exactly two semantic shapes are ever accepted as deletion-complete: (1) state=ABSENT with every canonical footprint key false, or (2) state=OWNED with application_found=false, retained_pvc_expected=true, footprint["pvc"]=true, and every other footprint key false (the classifier has already proven the retained PVC belongs to this exact deployment) -- every other shape, including OWNED with zero footprint and ABSENT with any footprint present, fails."""
    try:
        state, application_found, footprint_found = _validate_runtime_state_classifier_output(result)
    except Phase5Error as exc:
        return False, str(exc)

    if application_found:
        return False, "application_found is not confirmed false"

    if state == "ABSENT":
        present = sorted(key for key, value in footprint_found.items() if value)
        if present:
            return False, f"footprint still present: {present}"
        return True, None

    if state == "OWNED":
        non_pvc_present = sorted(key for key in footprint_found if key != "pvc" and footprint_found[key])
        if non_pvc_present:
            return False, f"non-PVC footprint still present: {non_pvc_present}"
        if not retained_pvc_expected:
            return False, "OWNED state is only acceptable as deletion-complete when a retained PVC is expected for this deletion context"
        if not footprint_found["pvc"]:
            return False, "OWNED state without a retained PVC present is not a recognized deletion-complete shape"
        return True, None

    # state == "BROKEN" -- the only remaining value the structural validator allows -- is never a recognized deletion-complete shape, regardless of footprint content.
    present = sorted(key for key, value in footprint_found.items() if value)
    return (False, f"classifier state is BROKEN (footprint still present: {present})") if present else (False, "classifier state is BROKEN")


def cmd_post_delete_acceptance(args):
    environment = require_environment_arg(args.environment)
    deployment_id = require_deployment_id_arg(args.deployment_id)
    state = load_state(args.state_path)
    deployment_model = require_state_value(state, "deployment_model")

    if deployment_model != "singleRuntime":
        print(f"Deployment model is {deployment_model} -- the singleRuntime post-delete compute-absence check does not apply to this retired historical path.")
        return

    # Static removal-state identity binding BEFORE using efs_mode to decide retained_pvc_expected -- corrupted state must never change PVC-retention acceptance semantics.
    efs_mode = _validate_removal_state_identity(state, environment, deployment_id)
    retained_pvc_expected = _retained_pvc_expected_for_removal(efs_mode)

    _connect_to_eks()

    timeout_seconds, interval_seconds, elapsed = 180, 15, 0
    while True:
        cli_args = [sys.executable, str(RUNTIME_STATE_TOOL), "--environment", environment, "--deployment-id", deployment_id]
        if retained_pvc_expected:
            cli_args.append("--retained-pvc-expected")
        proc = run(cli_args, check=False)

        if proc.returncode == 0:
            try:
                result = json.loads(proc.stdout)
            except json.JSONDecodeError:
                result = None
            if result is not None:
                ok, why = _post_delete_positively_absent(result, retained_pvc_expected)
                if ok:
                    print(f"OK: post-delete runtime compute absence positively confirmed for {deployment_id} (Application/StatefulSet/runtime Services/headless Service/runtime Ingress/runtime SecretProviderClasses/StorageClass all absent). A retained durable PVC (if expected for this deletion context), any EFS filesystem, the shared runtime namespace, and AWS Secrets Manager secrets are all allowed to remain.")
                    return
                print(f"[{time.strftime('%H:%M:%S', time.gmtime())}] post-delete check not yet satisfied ({why}) for {deployment_id} (elapsed {elapsed}s / {timeout_seconds}s); diagnostics: {result}")
            else:
                print(f"[{time.strftime('%H:%M:%S', time.gmtime())}] post-delete classifier produced unparseable output for {deployment_id} (elapsed {elapsed}s / {timeout_seconds}s), retrying...")
        else:
            print(f"[{time.strftime('%H:%M:%S', time.gmtime())}] post-delete classifier inspection error for {deployment_id} (elapsed {elapsed}s / {timeout_seconds}s), retrying...")

        if elapsed >= timeout_seconds:
            raise Phase5Error(f"runtime compute for {deployment_id} was not positively confirmed absent within {timeout_seconds}s after removal. Refusing to consider this deletion complete -- investigate the diagnostics above rather than deleting anything else manually.")
        time.sleep(interval_seconds)
        elapsed += interval_seconds


# Phase 5D: strict-acceptance (AWS credentials required)

def cmd_strict_acceptance(args):
    environment = require_environment_arg(args.environment)
    deployment_id = require_deployment_id_arg(args.deployment_id)

    aws_region, eks_deploy_role_arn = _connect_to_eks()

    descriptor = _describe_deployment_json(environment, deployment_id)
    efs_mode = descriptor.get("efsMode") or ""
    expected_efs_id = ""
    if efs_mode == "existing":
        expected_efs_id = descriptor.get("efsFileSystemId") or ""
    elif efs_mode:
        # managed: reuses the SAME reusable EFS-identity helper as Phase 5B, always in its real (non-dry-run) resolution path -- global acceptance never uses the placeholder.
        expected_efs_id = _resolve_efs_filesystem_id(
            efs_mode=efs_mode, efs_file_system_id_declared="", efs_creation_token=descriptor.get("efsCreationToken") or "",
            deploy=True, environment=environment, deployment_id=deployment_id,
            eks_deploy_role_arn=eks_deploy_role_arn, aws_region=aws_region,
        )

    cli_args = [sys.executable, str(RUNTIME_ACCEPTANCE_TOOL), "--environment", environment, "--deployment-id", deployment_id]
    if expected_efs_id:
        cli_args += ["--expected-efs-file-system-id", expected_efs_id]
    proc = run(cli_args, check=False)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    if proc.returncode != 0:
        raise Phase5Error(f"the GoldenGate runtime acceptance classifier could not evaluate {deployment_id} (configuration or inspection error) -- see diagnostics above.")

    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Phase5Error(f"the GoldenGate runtime acceptance classifier produced unparseable output: {exc}") from exc

    state_word = result.get("state")
    if state_word != "HEALTHY":
        raise Phase5Error(f"GoldenGate runtime acceptance state for {deployment_id} is {state_word!r}, expected HEALTHY. Reconciliation success alone is never sufficient. See diagnostics above.")
    print(f"OK: GoldenGate runtime {deployment_id} is HEALTHY.")


# Workflow summary (always(), no AWS credentials, tolerant of partial state)

def cmd_summary(args):
    state = load_state(args.state_path)
    if args.mode == "remove":
        lines = [
            "## GoldenGate Argo CD Application Deletion Summary", "",
            f"- Environment: `{state.get('environment', 'unknown')}`",
            f"- Deployment ID: `{state.get('deployment_id', 'unknown')}`",
            f"- Argo CD Application: `{state.get('argocd_app_name', 'unknown')}` (namespace: `{state.get('argocd_namespace', 'unknown')}`)",
            f"- Reason: `{state.get('reason', 'unknown')}`", "",
            "Argo CD cascade deletion removes the Application-managed resources. The retained /u02",
            "PersistentVolumeClaim (Prune=false), any managed/existing EFS filesystem, the shared",
            "runtime namespace, and AWS Secrets Manager secrets are never deleted by this workflow.",
        ]
    else:
        lines = [
            "## GoldenGate EKS Argo CD-based Deploy Summary", "",
            "### Deployment details", "",
            f"- Environment: `{state.get('environment', 'unknown')}`",
            f"- Deployment ID: `{state.get('deployment_id', 'unknown')}`",
            f"- Target namespace: `{state.get('target_namespace', 'unknown')}`",
            f"- Argo CD Application: `{state.get('argocd_app_name', 'unknown')}`",
            f"- Helm releaseName: `{state.get('release_name', 'unknown')}`",
            f"- Values file: `{state.get('values_file', 'unknown')}`",
            f"- Deploy enabled: `{state.get('deploy', 'unknown')}`", "",
            "### Published Helm OCI chart", "",
            f"- Chart ref: `{state.get('helm_chart_ref', 'unknown')}`",
            f"- Chart version: `{state.get('chart_version', 'unknown')}`", "",
            "### Ownership", "",
            "Argo CD owns create/update/delete of this GoldenGate deployment's Kubernetes resources.",
            "GitHub Actions only packages/publishes the Helm OCI chart and creates/updates the Argo",
            "CD Application resource -- it does not run `helm upgrade --install` or `helm uninstall`",
            "against the cluster.",
        ]
    write_step_summary("\n".join(lines))
    print("OK: wrote GoldenGate Runtime workflow summary.")


# CLI wiring

_SUBCOMMANDS = {
    "ensure-kubectl": cmd_ensure_kubectl,
    "ensure-deploy-tools": cmd_ensure_deploy_tools,
    "ownership-preflight": cmd_ownership_preflight,
    "prepare-deployment": cmd_prepare_deployment,
    "resolve-live-inputs": cmd_resolve_live_inputs,
    "validate-local": cmd_validate_local,
    "publish-chart": cmd_publish_chart,
    "validate-cluster-prerequisites": cmd_validate_cluster_prerequisites,
    "reconcile-runtime": cmd_reconcile_runtime,
    "post-deploy-diagnostics": cmd_post_deploy_diagnostics,
    "prepare-removal": cmd_prepare_removal,
    "removal-preflight": cmd_removal_preflight,
    "remove-runtime": cmd_remove_runtime,
    "post-delete-acceptance": cmd_post_delete_acceptance,
    "strict-acceptance": cmd_strict_acceptance,
    "summary": cmd_summary,
}

_DEPLOYMENT_ID_SUBCOMMANDS = (
    "ownership-preflight", "resolve-live-inputs", "validate-local", "publish-chart",
    "validate-cluster-prerequisites", "reconcile-runtime", "post-deploy-diagnostics",
    "removal-preflight", "remove-runtime", "post-delete-acceptance", "strict-acceptance",
)


def build_parser():
    parser = argparse.ArgumentParser(description="Phase 5 | GoldenGate Runtime lifecycle orchestrator (ownership preflight, Helm build/publish/deploy, removal, strict acceptance).")
    parser.add_argument("--state-file", type=Path, default=None, help="Override the Phase 5 state file path (default depends on the subcommand: reconcile vs. removal state).")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("ensure-kubectl")
    subparsers.add_parser("ensure-deploy-tools")

    for name in _DEPLOYMENT_ID_SUBCOMMANDS:
        sub = subparsers.add_parser(name)
        sub.add_argument("--environment", required=True)
        sub.add_argument("--deployment-id", required=True)

    prepare_deployment = subparsers.add_parser("prepare-deployment")
    prepare_deployment.add_argument("--environment", required=True)
    prepare_deployment.add_argument("--deployment-id", required=True)
    prepare_deployment.add_argument("--deployment-model", required=True)
    prepare_deployment.add_argument("--deploy", required=True)

    prepare_removal = subparsers.add_parser("prepare-removal")
    prepare_removal.add_argument("--environment", required=True)
    prepare_removal.add_argument("--deployment-id", required=True)
    prepare_removal.add_argument("--deployment-model", required=True)
    prepare_removal.add_argument("--efs-mode", default="")
    prepare_removal.add_argument("--reason", required=True)

    summary = subparsers.add_parser("summary")
    summary.add_argument("--mode", required=True, choices=("reconcile", "remove"))
    summary.add_argument("--environment", required=False)
    summary.add_argument("--deployment-id", required=False)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.state_path = state_path_for(args.command, getattr(args, "mode", None), args.state_file)

    try:
        _SUBCOMMANDS[args.command](args)
    except Phase5Error as exc:
        print(f"FAIL: {exc}")
        return 1
    except subprocess.SubprocessError as exc:
        print(f"FAIL: subprocess execution error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
