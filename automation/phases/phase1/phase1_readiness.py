#!/usr/bin/env python3
"""Phase 1 | Validate Folder-Driven Deployment Model orchestration entrypoint for the single validate_model job in .github/workflows/00-main-goldengate-orchestrator.yaml; a thin orchestration/service layer that never reimplements deployment descriptor parsing, environment parsing, IAM policy rendering, registry construction, or managed-EFS inventory comparison (those stay owned by automation/goldengate-deployment-model.py, automation/goldengate-environment.py, automation/phases/phase1/detect-goldengate-deployments.sh, and automation/phases/phase1/managed_efs_inventory_guard.py); non-secret orchestration state is threaded between subcommands through a JSON state file under the runner temp directory instead of large inline shell blocks."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

DEPLOYMENT_MODEL_TOOL = REPO_ROOT / "automation" / "goldengate-deployment-model.py"
ENVIRONMENT_TOOL = REPO_ROOT / "automation" / "goldengate-environment.py"
DETECT_SCRIPT = REPO_ROOT / "automation" / "phases" / "phase1" / "detect-goldengate-deployments.sh"
EFS_INVENTORY_GUARD_TOOL = REPO_ROOT / "automation" / "phases" / "phase1" / "managed_efs_inventory_guard.py"

# Mirrors automation/goldengate-deployment-model.py's own _TOKEN_RE / automation/phases/phase1/managed_efs_inventory_guard.py's _SAFE_ENVIRONMENT_RE -- each tool in this repository intentionally keeps its own local copy of this grammar rather than importing it across modules; used here only for defense-in-depth path-safety before an environment name is ever interpolated into a filesystem path, never as the canonical acceptance/rejection of an environment (that remains automation/goldengate-environment.py's own concern).
_SAFE_TOKEN_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*\Z")

CANONICAL_OUTPUT_KEYS = (
    "selected_environment",
    "effective_deploy",
    "has_active_deployments",
    "active_runtime_matrix",
    "terraform_governance_override",
    "terraform_governance_override_reason",
    "has_changes",
    "deployment_matrix",
    "has_deletions",
    "deletion_matrix",
    "has_storage_transition_violations",
    "storage_transition_violations",
)

_LITERAL_BOOL_KEYS = ("effective_deploy", "has_active_deployments", "has_changes", "has_deletions", "terraform_governance_override")
_JSON_ARRAY_KEYS = ("active_runtime_matrix", "deployment_matrix", "deletion_matrix", "storage_transition_violations")


class Phase1Error(Exception):
    """A fail-closed Phase 1 error; main() reports it and exits non-zero."""


def is_safe_token(value):
    return isinstance(value, str) and bool(_SAFE_TOKEN_RE.match(value))


def require_literal_bool(name, value):
    """Fail closed unless value is exactly the literal string 'true' or 'false' -- never truthy/falsy-coerced."""
    if value not in ("true", "false"):
        raise Phase1Error(f"{name} is {value!r}, expected literal 'true' or 'false'.")
    return value


def require_json_array(name, value):
    """Fail closed unless value parses as JSON and the result is a list."""
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise Phase1Error(f"{name} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise Phase1Error(f"{name} did not decode to a JSON array (got {type(parsed).__name__}).")
    return parsed


# Phase 1 state file

def default_state_path():
    """${RUNNER_TEMP}/goldengate-phase1-state.json, or a repo-local fallback outside CI."""
    runner_temp = os.environ.get("RUNNER_TEMP")
    if runner_temp:
        return Path(runner_temp) / "goldengate-phase1-state.json"
    return Path(os.environ.get("TMPDIR", "/tmp")) / "goldengate-phase1-state.json"


def load_state(state_path):
    if not state_path.exists():
        return {}
    try:
        with state_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise Phase1Error(f"Phase 1 state file {state_path} is unreadable/malformed: {exc}") from exc
    if not isinstance(data, dict):
        raise Phase1Error(f"Phase 1 state file {state_path} did not contain a JSON object.")
    return data


def save_state(state_path, state):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, sort_keys=True, indent=2)
        f.write("\n")
    tmp_path.replace(state_path)


def update_state(state_path, updates):
    state = load_state(state_path)
    state.update(updates)
    save_state(state_path, state)
    return state


def require_state_value(state, key):
    if key not in state or state[key] in (None, ""):
        raise Phase1Error(f"Phase 1 state is missing required key {key!r}; an earlier step did not complete.")
    return state[key]


# GitHub Actions special-file helpers

def append_github_env(pairs, env_path=None):
    """Appends NAME=value lines to $GITHUB_ENV. No-op (never raises) when GITHUB_ENV is unset, so offline/unit-test invocations never require a real GitHub Actions runner."""
    path = env_path if env_path is not None else os.environ.get("GITHUB_ENV")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for name, value in pairs:
            if "\n" in value:
                delimiter = f"GG_EOF_{name}"
                f.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")
            else:
                f.write(f"{name}={value}\n")


def append_github_env_raw(text, env_path=None):
    """Appends pre-formatted NAME=value lines (as already emitted by an external tool's own github-env output) verbatim to $GITHUB_ENV. No-op when GITHUB_ENV is unset or text is empty."""
    path = env_path if env_path is not None else os.environ.get("GITHUB_ENV")
    if not path or not text:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)
        if not text.endswith("\n"):
            f.write("\n")


def write_github_output(pairs, output_path=None):
    """Appends name=value lines to $GITHUB_OUTPUT (multi-line values use the heredoc form). No-op (never raises) when GITHUB_OUTPUT is unset."""
    path = output_path if output_path is not None else os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for name, value in pairs:
            if "\n" in value:
                delimiter = f"GG_EOF_{name}"
                f.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")
            else:
                f.write(f"{name}={value}\n")


def read_output_file(path):
    """Parses a GITHUB_OUTPUT-shaped file (simple NAME=value lines only -- every producer this module wraps emits single-line JSON, never the multi-line heredoc form) into a dict."""
    pairs = {}
    if not os.path.exists(path):
        return pairs
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or "=" not in line:
                continue
            name, _, value = line.partition("=")
            pairs[name] = value
    return pairs


def write_step_summary(text, summary_path=None):
    path = summary_path if summary_path is not None else os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        print(text)
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)
        if not text.endswith("\n"):
            f.write("\n")


# Safe subprocess execution

def run(argv, env=None, cwd=None, check=True, capture_output=True):
    """Runs argv as an argument array (never shell=True). Fails closed with the tool's own stderr/stdout on a non-zero exit when check=True."""
    proc = subprocess.run(
        argv,
        cwd=cwd or REPO_ROOT,
        env=env,
        capture_output=capture_output,
        text=True,
    )
    if check and proc.returncode != 0:
        raise Phase1Error(
            f"{' '.join(str(a) for a in argv)} failed (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
        )
    return proc


def run_tool(tool_path, args, **kwargs):
    return run([sys.executable, str(tool_path), *args], **kwargs)


# Subcommands

def cmd_prerequisites(args):
    if sys.version_info < (3, 0):
        raise Phase1Error("python3 is required.")
    try:
        import yaml  # noqa: F401
    except ImportError as exc:
        raise Phase1Error("PyYAML is required and is not available on this runner.") from exc
    print("OK: python3 and PyYAML are available.")


def cmd_resolve_environment(args):
    event_name = os.environ.get("EVENT_NAME", "")
    input_environment = os.environ.get("INPUT_ENVIRONMENT", "")

    # workflow_dispatch carries an explicit choice; the push trigger is scoped to envs/dev/** only, so the trigger itself selects dev -- never a second independent environment source.
    if event_name == "workflow_dispatch":
        selected_environment = input_environment
    else:
        selected_environment = "dev"

    if not is_safe_token(selected_environment):
        raise Phase1Error(f"resolved environment {selected_environment!r} is not a safe identifier.")

    state_path = args.state_path
    update_state(state_path, {"selected_environment": selected_environment})
    append_github_env([("GG_SELECTED_ENVIRONMENT", selected_environment)])
    write_github_output([("environment", selected_environment)])
    print(f"OK: selected environment is {selected_environment!r}.")


def cmd_validate_model(args):
    state = load_state(args.state_path)
    environment = require_state_value(state, "selected_environment")
    run_tool(DEPLOYMENT_MODEL_TOOL, ["--environment", environment, "validate"])
    print("OK: folder-driven deployment model is valid.")


def cmd_load_environment(args):
    state = load_state(args.state_path)
    environment = require_state_value(state, "selected_environment")
    run_tool(ENVIRONMENT_TOOL, ["--environment", environment, "validate"])
    proc = run_tool(ENVIRONMENT_TOOL, ["--environment", environment, "github-env"])
    append_github_env_raw(proc.stdout)
    print("OK: selected environment configuration is valid and loaded.")


def cmd_validate_iam_policies(args):
    state = load_state(args.state_path)
    environment = require_state_value(state, "selected_environment")
    run_tool(ENVIRONMENT_TOOL, ["--environment", environment, "render-iam-policies", "--check"])
    print("OK: generated IAM policies are in sync with environment.yaml.")


def cmd_active_runtime_state(args):
    import tempfile

    import yaml

    state = load_state(args.state_path)
    environment = require_state_value(state, "selected_environment")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        run_tool(DEPLOYMENT_MODEL_TOOL, ["--environment", environment, "registry", "--output", tmp_path])
        with open(tmp_path) as f:
            doc = yaml.safe_load(f)
    finally:
        os.unlink(tmp_path)

    if not isinstance(doc, dict):
        raise Phase1Error("registry document is not a mapping.")
    deployments = doc.get("deployments")
    if not isinstance(deployments, list):
        raise Phase1Error("registry deployments is not a list.")

    has_active_deployments = "true" if len(deployments) > 0 else "false"
    active_runtime_matrix = json.dumps(
        sorted(({"environment": environment, "deployment_id": d["name"]} for d in deployments), key=lambda x: x["deployment_id"]),
    )

    print(f"Active GoldenGate runtime count: {len(deployments)}")
    if has_active_deployments == "false":
        print("No active GoldenGate runtimes remain; runtime-dependent monitor stages will be skipped.")
    print(f"Active GoldenGate runtime matrix: {active_runtime_matrix}")

    update_state(args.state_path, {"has_active_deployments": has_active_deployments, "active_runtime_matrix": active_runtime_matrix})
    write_github_output([("has_active_deployments", has_active_deployments), ("active_runtime_matrix", active_runtime_matrix)])


def cmd_effective_deploy(args):
    event_name = os.environ.get("EVENT_NAME", "")
    input_action = os.environ.get("INPUT_ACTION", "")

    # A push event carries no workflow_dispatch inputs and always implies deploy=true, matching the existing detect-goldengate-deployments.sh push-path contract.
    if event_name != "workflow_dispatch":
        effective_deploy = "true"
    elif input_action == "deploy":
        effective_deploy = "true"
    elif input_action == "validate":
        effective_deploy = "false"
    else:
        raise Phase1Error(
            f"unrecognized workflow_dispatch action {input_action!r} -- expected exactly 'validate' or 'deploy'. "
            "Refusing to guess which lifecycle contract applies."
        )

    update_state(args.state_path, {"effective_deploy": effective_deploy})
    write_github_output([("effective_deploy", effective_deploy)])
    print(f"OK: effective_deploy={effective_deploy}.")


def cmd_terraform_governance(args):
    event_name = os.environ.get("EVENT_NAME", "")
    input_override = os.environ.get("INPUT_OVERRIDE", "")
    input_reason = os.environ.get("INPUT_REASON", "")

    # A push event carries no workflow_dispatch inputs and must never be able to activate break-glass automatically.
    if event_name != "workflow_dispatch":
        override, reason = "false", ""
        print(f"OK: {event_name} event -- Terraform governance override is unconditionally false; break-glass requires an explicit manual workflow_dispatch action.")
    else:
        require_literal_bool("terraform_governance_override", input_override)
        trimmed_reason = input_reason.strip()
        if input_override == "true":
            if not trimmed_reason:
                raise Phase1Error(
                    "terraform_governance_override=true requires a non-empty, non-whitespace "
                    "terraform_governance_override_reason. Refusing to activate the corporate Terraform "
                    "governance break-glass without a written justification."
                )
            override, reason = "true", input_reason
            print("Terraform governance override is ENABLED for this manual run -- the corporate reusable "
                  "workflow will receive override_noncompliance=true with the supplied override_reason. "
                  "This is not a bypass: non-compliance remains recorded, and manual approval itself cannot be overridden.")
        else:
            if trimmed_reason:
                print("INFO: terraform_governance_override_reason was provided but terraform_governance_override is false -- the reason is ignored; normal corporate PR-governance behavior applies.")
            override, reason = "false", ""
            print("OK: Terraform governance override is disabled -- normal corporate PR-governance/Kosli attestation applies.")

    update_state(args.state_path, {"terraform_governance_override": override, "terraform_governance_override_reason": reason})
    write_github_output([("terraform_governance_override", override), ("terraform_governance_override_reason", reason)])


def _assume_role_env(role_arn, session_name):
    """Assumes role_arn and returns an env dict carrying ONLY the temporary credentials (plus the caller's own PATH), confined to the caller's own subprocess calls -- never written to GITHUB_ENV, never logged. Masks all three values via ::add-mask::."""
    proc = run(["aws", "sts", "assume-role", "--role-arn", role_arn, "--role-session-name", session_name, "--duration-seconds", "900", "--output", "json"])
    creds = json.loads(proc.stdout)["Credentials"]
    for value in (creds["AccessKeyId"], creds["SecretAccessKey"], creds["SessionToken"]):
        print(f"::add-mask::{value}")
    env = dict(os.environ)
    env["AWS_ACCESS_KEY_ID"] = creds["AccessKeyId"]
    env["AWS_SECRET_ACCESS_KEY"] = creds["SecretAccessKey"]
    env["AWS_SESSION_TOKEN"] = creds["SessionToken"]
    return env


def cmd_eks_preflight(args):
    state = load_state(args.state_path)
    effective_deploy = require_state_value(state, "effective_deploy")
    if effective_deploy != "true":
        raise Phase1Error("eks-preflight was invoked outside Deploy mode; refusing to touch live AWS/EKS.")

    aws_region = os.environ["AWS_REGION"]
    eks_cluster_name = os.environ["EKS_CLUSTER_NAME"]
    eks_cluster_arn = os.environ["EKS_CLUSTER_ARN"]
    eks_oidc_issuer = os.environ["EKS_OIDC_ISSUER"]
    eks_deploy_role_arn = os.environ["EKS_DEPLOY_ROLE_ARN"]
    workload_account_id = os.environ["WORKLOAD_ACCOUNT_ID"]
    selected_environment = require_state_value(state, "selected_environment")

    role_env = _assume_role_env(eks_deploy_role_arn, f"gg-eks-oidc-preflight-{os.environ.get('GITHUB_RUN_ID', 'local')}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}")

    identity = run(["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text", "--region", aws_region], env=role_env)
    actual_account = identity.stdout.strip()
    if not actual_account or actual_account != workload_account_id:
        raise Phase1Error(f"assumed role resolved to caller account '{actual_account}', expected the GoldenGate workload account {workload_account_id}. Refusing to call DescribeCluster.")

    cluster_json = run(["aws", "eks", "describe-cluster", "--name", eks_cluster_name, "--region", aws_region, "--output", "json"], env=role_env)
    cluster = json.loads(cluster_json.stdout)["cluster"]

    problems = []
    if cluster.get("status") != "ACTIVE":
        problems.append(f"cluster status is {cluster.get('status')!r}, expected ACTIVE")
    if cluster.get("arn") != eks_cluster_arn:
        problems.append(f"live cluster ARN {cluster.get('arn')!r} does not match envs/{selected_environment}/environment.yaml-derived ARN {eks_cluster_arn!r}")
    live_oidc_issuer = (cluster.get("identity") or {}).get("oidc", {}).get("issuer")
    if live_oidc_issuer != eks_oidc_issuer:
        problems.append(
            f"Configured EKS OIDC issuer does not match the live cluster. Update envs/{selected_environment}/environment.yaml "
            f"to the current EKS issuer and regenerate IAM policies before deployment. (live={live_oidc_issuer!r}, configured={eks_oidc_issuer!r})"
        )
    if problems:
        raise Phase1Error("; ".join(problems))
    print(f"OK: live EKS cluster {cluster.get('name')!r} is ACTIVE, ARN and OIDC issuer match envs/{selected_environment}/environment.yaml.")

    _ensure_kubectl()

    run(["aws", "eks", "update-kubeconfig", "--region", aws_region, "--name", eks_cluster_name, "--role-arn", eks_deploy_role_arn, "--assume-role-arn", eks_deploy_role_arn])
    context = run(["kubectl", "config", "current-context"])
    print(f"Current kube context: {context.stdout.strip()}")

    # kube-system is a built-in namespace guaranteed to exist on every EKS cluster -- unlike any GoldenGate-owned namespace, which may legitimately not exist yet ahead of a first application deployment. A single read-only `get` proves API connectivity plus sufficient authentication/authorization; no kubectl apply/create/patch/delete is ever issued from this preflight.
    run(["kubectl", "get", "namespace", "kube-system", "-o", "name"])
    print("OK: read-only Kubernetes API access verified (kube-system).")

    update_state(args.state_path, {"eks_preflight_completed": "true"})


def _ensure_kubectl():
    if run(["bash", "-c", "command -v kubectl"], check=False).returncode == 0:
        run(["kubectl", "version", "--client=true"])
        return

    kubectl_version = "v1.35.0"
    machine = run(["uname", "-m"]).stdout.strip()
    arch_map = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
    if machine not in arch_map:
        raise Phase1Error(f"Unsupported architecture for kubectl: {machine}")
    kubectl_arch = arch_map[machine]

    run(["curl", "-fsSL", f"https://dl.k8s.io/release/{kubectl_version}/bin/linux/{kubectl_arch}/kubectl", "-o", "/tmp/kubectl"])
    run(["sudo", "mv", "/tmp/kubectl", "/usr/local/bin/kubectl"])
    run(["sudo", "chmod", "+x", "/usr/local/bin/kubectl"])
    run(["kubectl", "version", "--client=true"])


def cmd_detect_deployments(args):
    import tempfile

    state = load_state(args.state_path)
    effective_deploy = require_state_value(state, "effective_deploy")

    env = dict(os.environ)
    env["INPUT_DEPLOY"] = effective_deploy
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp:
        output_path = tmp.name
    env["GITHUB_OUTPUT"] = output_path
    try:
        run(["bash", str(DETECT_SCRIPT)], env=env)
        outputs = read_output_file(output_path)
    finally:
        os.unlink(output_path)

    required = ("has_changes", "deployment_matrix", "has_deletions", "deletion_matrix", "has_storage_transition_violations", "storage_transition_violations")
    missing = [k for k in required if k not in outputs]
    if missing:
        raise Phase1Error(f"automation/phases/phase1/detect-goldengate-deployments.sh did not emit required output(s): {missing}")

    require_literal_bool("has_changes", outputs["has_changes"])
    require_literal_bool("has_deletions", outputs["has_deletions"])
    require_json_array("deployment_matrix", outputs["deployment_matrix"])
    require_json_array("deletion_matrix", outputs["deletion_matrix"])
    require_json_array("storage_transition_violations", outputs["storage_transition_violations"])

    update_state(args.state_path, {k: outputs[k] for k in required})
    write_github_output([(k, outputs[k]) for k in required])
    print(f"OK: detected deployments -- has_changes={outputs['has_changes']}, has_deletions={outputs['has_deletions']}.")


def cmd_managed_efs_deletion_guard(args):
    state = load_state(args.state_path)
    deletion_matrix = require_json_array("deletion_matrix", require_state_value(state, "deletion_matrix"))

    print(f"Deletion matrix: {json.dumps(deletion_matrix)}")

    managed_deletions = [i.get("deployment_id", "?") for i in deletion_matrix if i.get("efs_mode") == "managed" and i.get("reason") == "physical-removal"]
    deployment_disabled_managed = [i.get("deployment_id", "?") for i in deletion_matrix if i.get("efs_mode") == "managed" and i.get("reason") == "deployment-disabled"]

    if deployment_disabled_managed:
        print("ALLOWED (application decommission only, managed storage retained): the following managed-EFS "
              "deployment(s) transitioned to deployment.enabled=false this push -- their descriptor and "
              "dedicated EFS filesystem are retained; only application-level resources (Argo CD Application, "
              "runtime pod) are affected:")
        for dep_id in deployment_disabled_managed:
            print(dep_id)

    if managed_deletions:
        print("FAIL: the following deployment descriptor(s) were physically deleted while persistence.efs.mode=managed:")
        for dep_id in managed_deletions:
            print(dep_id)
        raise Phase1Error(
            "Managed GoldenGate EFS is durable storage. Physical deletion of the deployment descriptor is "
            "blocked. Set deployment.enabled=false first and use the controlled storage-decommission "
            "procedure before removing the descriptor."
        )

    print("OK: no deleted descriptor declared persistence.efs.mode=managed. Terraform sync may proceed.")


def cmd_storage_transition_guard(args):
    state = load_state(args.state_path)
    violations = require_json_array("storage_transition_violations", require_state_value(state, "storage_transition_violations"))

    print(f"Storage transition violations: {json.dumps(violations)}")

    if violations:
        print("FAIL: one or more changed deployment descriptors made an unsafe persistence.efs storage-identity transition:")
        for item in violations:
            print(f"  {item.get('deployment_id', '?')}: {item.get('violation', '?')}")
        raise Phase1Error(
            "managed<->existing, managed->persistence-disabled, managed->non-EFS-provider, and a mutated "
            "existing fileSystemId are storage migrations, not ordinary deployment updates. They require a "
            "separate, explicit migration/decommission process -- revert the persistence.efs change in this "
            "push before it can proceed."
        )

    print("OK: no unsafe storage-identity transition detected. Terraform sync may proceed.")


_ALLOWED_EFS_TAG_KEYS = ("ManagedBy", "GoldenGateDeploymentId", "GoldenGateEnvironment", "GoldenGateStorage")


def sanitize_efs_filesystems(raw_filesystems):
    """Strips every tag key outside _ALLOWED_EFS_TAG_KEYS from each raw AWS DescribeFileSystems entry and keeps only FileSystemId/CreationToken/LifeCycleState/Tags -- a pure function so it stays independently testable without a live AWS call."""
    sanitized = []
    for fs in raw_filesystems:
        tags = [{"Key": t.get("Key"), "Value": t.get("Value")} for t in fs.get("Tags", []) if t.get("Key") in _ALLOWED_EFS_TAG_KEYS]
        sanitized.append({"FileSystemId": fs.get("FileSystemId"), "CreationToken": fs.get("CreationToken"), "LifeCycleState": fs.get("LifeCycleState"), "Tags": tags})
    return sanitized


def cmd_managed_efs_inventory(args):
    import tempfile

    state = load_state(args.state_path)
    effective_deploy = require_state_value(state, "effective_deploy")
    if effective_deploy != "true":
        raise Phase1Error("managed-efs-inventory was invoked outside Deploy mode; refusing to touch live AWS.")
    selected_environment = require_state_value(state, "selected_environment")

    aws_region = os.environ["AWS_REGION"]
    eks_deploy_role_arn = os.environ["EKS_DEPLOY_ROLE_ARN"]

    expected_match = re.match(r"^arn:aws:iam::([0-9]{12}):role/.*$", eks_deploy_role_arn)
    if not expected_match:
        raise Phase1Error("could not extract a 12-digit account ID from EKS_DEPLOY_ROLE_ARN; refusing to read the managed-EFS inventory without a provable expected workload account.")
    expected_workload_account_id = expected_match.group(1)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        expected_path = tmp.name
    try:
        proc = run_tool(DEPLOYMENT_MODEL_TOOL, ["--environment", selected_environment, "managed-efs-inventory"])
        with open(expected_path, "w") as f:
            f.write(proc.stdout)
        print("Expected managed-EFS inventory (includes deployment.enabled=false descriptors on purpose -- their EFS is retained, not decommissioned):")
        print(proc.stdout)

        role_env = _assume_role_env(eks_deploy_role_arn, f"gg-efs-inventory-{os.environ.get('GITHUB_RUN_ID', 'local')}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}")

        identity = run(["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text", "--region", aws_region], env=role_env)
        actual_account = identity.stdout.strip()
        if actual_account != expected_workload_account_id:
            raise Phase1Error(f"assumed role resolved to account {actual_account}, expected the GoldenGate workload account {expected_workload_account_id}.")

        describe = run(["aws", "efs", "describe-file-systems", "--region", aws_region, "--output", "json"], env=role_env)
        raw = json.loads(describe.stdout)
        sanitized = sanitize_efs_filesystems(raw.get("FileSystems", []))

        in_scope_count = sum(1 for fs in sanitized if any(t.get("Key") == "ManagedBy" and t.get("Value") == "goldengate-eks-app" for t in fs.get("Tags", [])))
        print(f"Actual AWS-side EFS scan (region {aws_region}): {in_scope_count} GoldenGate-managed-tagged filesystem(s) found in scope. Unrelated EFS filesystems and any non-GoldenGate tags are never logged.")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
            actual_path = tmp.name
        with open(actual_path, "w") as f:
            json.dump(sanitized, f)

        try:
            run([sys.executable, str(EFS_INVENTORY_GUARD_TOOL), selected_environment, expected_path, actual_path])
        finally:
            os.unlink(actual_path)
    finally:
        os.unlink(expected_path)

    update_state(args.state_path, {"managed_efs_inventory_completed": "true"})
    print("OK: managed-EFS inventory matches the deployment model; no orphan detected.")


def cmd_publish_outputs(args):
    state = load_state(args.state_path)
    missing = [k for k in CANONICAL_OUTPUT_KEYS if k not in state]
    if missing:
        raise Phase1Error(f"Phase 1 state is missing canonical output key(s): {missing}")
    write_github_output([(k, state[k]) for k in CANONICAL_OUTPUT_KEYS])
    print("OK: published canonical Phase 1 outputs.")


def cmd_acceptance(args):
    state = load_state(args.state_path)

    selected_environment = require_state_value(state, "selected_environment")
    if not is_safe_token(selected_environment):
        raise Phase1Error(f"selected_environment {selected_environment!r} is not a safe identifier.")

    effective_deploy = require_literal_bool("effective_deploy", require_state_value(state, "effective_deploy"))
    has_active_deployments = require_literal_bool("has_active_deployments", require_state_value(state, "has_active_deployments"))
    has_changes = require_literal_bool("has_changes", require_state_value(state, "has_changes"))
    has_deletions = require_literal_bool("has_deletions", require_state_value(state, "has_deletions"))
    terraform_governance_override = require_literal_bool("terraform_governance_override", require_state_value(state, "terraform_governance_override"))

    if terraform_governance_override == "true" and not state.get("terraform_governance_override_reason", "").strip():
        raise Phase1Error("terraform_governance_override=true requires a non-empty terraform_governance_override_reason.")

    active_runtime_matrix = require_json_array("active_runtime_matrix", require_state_value(state, "active_runtime_matrix"))
    deployment_matrix = require_json_array("deployment_matrix", require_state_value(state, "deployment_matrix"))
    deletion_matrix = require_json_array("deletion_matrix", require_state_value(state, "deletion_matrix"))
    require_literal_bool("has_storage_transition_violations", require_state_value(state, "has_storage_transition_violations"))
    storage_transition_violations = require_json_array("storage_transition_violations", require_state_value(state, "storage_transition_violations"))

    if effective_deploy == "true":
        if state.get("eks_preflight_completed") != "true":
            raise Phase1Error("Deploy mode requires the live EKS/OIDC/Kubernetes-API prerequisite to have completed successfully before acceptance.")
        if state.get("managed_efs_inventory_completed") != "true":
            raise Phase1Error("Deploy mode requires the live AWS-side managed-EFS inventory check to have completed successfully before acceptance.")
        eks_status = "validated"
        efs_inventory_status = "passed"
    else:
        if state.get("eks_preflight_completed") == "true" or state.get("managed_efs_inventory_completed") == "true":
            raise Phase1Error("Validate mode must not require (or have performed) live AWS/EKS inventory work.")
        eks_status = "not applicable (Validate mode)"
        efs_inventory_status = "not applicable (Validate mode)"

    summary_lines = [
        "## Phase 1 | Validate Folder-Driven Deployment Model",
        "",
        f"- Environment: {selected_environment}",
        f"- Mode: {'Deploy' if effective_deploy == 'true' else 'Validate'}",
        f"- Active runtimes: {len(active_runtime_matrix)}",
        f"- Selected runtime changes: {'yes' if has_changes == 'true' else 'none'} ({len(deployment_matrix)} deployment(s))",
        f"- Runtime removals: {'yes' if has_deletions == 'true' else 'none'} ({len(deletion_matrix)} deletion(s))",
        f"- EKS prerequisite: {eks_status}",
        "- Managed-EFS deletion safety: passed",
        "- Storage transition safety: passed" + (f" ({len(storage_transition_violations)} violation(s) recorded)" if storage_transition_violations else ""),
        f"- AWS managed-EFS inventory: {efs_inventory_status}",
        "- Result: PASSED",
        "",
    ]
    write_step_summary("\n".join(summary_lines))
    print("OK: Phase 1 | Validate Folder-Driven Deployment Model succeeded.")


# CLI wiring

_SUBCOMMANDS = {
    "prerequisites": cmd_prerequisites,
    "resolve-environment": cmd_resolve_environment,
    "validate-model": cmd_validate_model,
    "load-environment": cmd_load_environment,
    "validate-iam-policies": cmd_validate_iam_policies,
    "active-runtime-state": cmd_active_runtime_state,
    "effective-deploy": cmd_effective_deploy,
    "terraform-governance": cmd_terraform_governance,
    "eks-preflight": cmd_eks_preflight,
    "detect-deployments": cmd_detect_deployments,
    "managed-efs-deletion-guard": cmd_managed_efs_deletion_guard,
    "storage-transition-guard": cmd_storage_transition_guard,
    "managed-efs-inventory": cmd_managed_efs_inventory,
    "publish-outputs": cmd_publish_outputs,
    "acceptance": cmd_acceptance,
}


def build_parser():
    parser = argparse.ArgumentParser(description="Phase 1 | Validate Folder-Driven Deployment Model orchestrator.")
    parser.add_argument("--state-file", type=Path, default=None, help="Override the Phase 1 state file path (default: $RUNNER_TEMP/goldengate-phase1-state.json).")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in _SUBCOMMANDS:
        subparsers.add_parser(name)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.state_path = args.state_file if args.state_file is not None else default_state_path()

    try:
        _SUBCOMMANDS[args.command](args)
    except Phase1Error as exc:
        print(f"FAIL: {exc}")
        return 1
    except subprocess.SubprocessError as exc:
        print(f"FAIL: subprocess execution error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
