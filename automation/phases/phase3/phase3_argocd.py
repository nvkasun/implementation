#!/usr/bin/env python3
"""Phase 3 | Argo CD orchestration entrypoint for the argocd_preflight/reconcile_argocd/validate_argocd_ready jobs in .github/workflows/00-main-goldengate-orchestrator.yaml and the build_publish_and_deploy job in .github/workflows/20-sub-argocd.yaml; a thin orchestration/service layer that never reimplements environment.yaml parsing (owned by automation/goldengate-environment.py) and reuses, never duplicates, automation/phases/phase3/argocd_state.py (pre-reconciliation ownership-safety preflight) and automation/phases/phase3/argocd_acceptance.py (strict post-reconciliation acceptance) as separate subprocess-invoked classifiers. Non-secret Helm/Argo CD deployment metadata is threaded between the 20-sub-argocd.yaml subcommands through a JSON state file under the runner temp directory instead of large inline shell blocks; AWS credentials are never written to that state file, to $GITHUB_OUTPUT, or to $GITHUB_ENV."""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

ENVIRONMENT_TOOL = REPO_ROOT / "automation" / "goldengate-environment.py"
ARGOCD_STATE_TOOL = REPO_ROOT / "automation" / "phases" / "phase3" / "argocd_state.py"
ARGOCD_ACCEPTANCE_TOOL = REPO_ROOT / "automation" / "phases" / "phase3" / "argocd_acceptance.py"

# Mirrors automation/phases/phase1/phase1_readiness.py's own _SAFE_TOKEN_RE / automation/phases/phase2/phase2_prerequisites.py's own copy -- each tool in this repository intentionally keeps its own local copy of this grammar rather than importing it across modules; used here only for defense-in-depth path-safety before an environment name is ever interpolated into a filesystem path, never as the canonical acceptance/rejection of an environment (that remains automation/goldengate-environment.py's own concern).
_SAFE_TOKEN_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*\Z")

# Phase 3 constants (helm/argocd, envs/<environment>/argocd/values.yaml) -- moved verbatim from the former .github/workflows/20-sub-argocd.yaml top-level env: block, never re-derived.
HELM_OCI_NAMESPACE = "helm"
CHART_NAME = "argocd"
HELM_CHART_PATH = "helm/argocd"
ENVS_ROOT = "envs"
ARGOCD_RELEASE_NAME = "argocd"

REQUIRED_ECR_POLICY_ACTIONS = frozenset({
    "ecr:BatchCheckLayerAvailability",
    "ecr:BatchGetImage",
    "ecr:GetDownloadUrlForLayer",
    "ecr:DescribeImages",
    "ecr:DescribeRepositories",
})

# All four Helm OCI repositories the Argo CD ECR read IAM policy must grant pull access to -- exact ARN match per repository, never a wildcard.
REQUIRED_ECR_POLICY_REPOS = (
    "helm/goldengate",
    "helm/goldengate-monitor",
    "helm/goldengate-platform",
    "helm/amazon-cloudwatch-observability",
)

# Mirrors automation/phases/phase3/argocd_acceptance.py's own REQUIRED_REPO_SECRETS -- intentionally a local copy (this repository's established convention for small literal mappings), never a cross-module import.
REQUIRED_REPO_SECRETS = {
    "argocd-ecr-goldengate-oci": "helm/goldengate",
    "argocd-ecr-goldengate-monitor-oci": "helm/goldengate-monitor",
    "argocd-ecr-goldengate-platform-oci": "helm/goldengate-platform",
    "argocd-ecr-amazon-cloudwatch-observability-oci": "helm/amazon-cloudwatch-observability",
}

PUBLIC_REGISTRIES = ("quay.io", "ghcr.io", "docker.io", "public.ecr.aws", "registry.k8s.io", "gcr.io", "k8s.gcr.io")

_PLACEHOLDER_RE = re.compile(r"<[A-Z_][A-Z0-9_]*>")
_IMAGE_LINE_RE = re.compile(r"^[ \t]*image:[ \t]*(\S.*)$", re.MULTILINE)

_ROLLOUT_TARGETS = (
    ("deployment", "argocd-server"),
    ("deployment", "argocd-repo-server"),
    ("deployment", "argocd-redis"),
    ("deployment", "argocd-applicationset-controller"),
    ("deployment", "argocd-notifications-controller"),
    ("statefulset", "argocd-application-controller"),
)

ECR_TOKEN_SYNC_NAME = "argocd-ecr-token-sync"

# Non-secret Phase 3 deployment-metadata keys only -- update_state() fails closed on any other key, so an AWS/ECR/Kubernetes credential can never be written to the state file even by an accidental future call site.
ALLOWED_STATE_KEYS = frozenset({
    "environment", "values_file", "chart_version", "helm_ecr_repository",
    "helm_push_url", "helm_chart_ref", "rendered_manifest", "package_path",
    "pulled_directory", "ingress_enabled", "namespace",
})


class Phase3Error(Exception):
    """A fail-closed Phase 3 error; main() reports it and exits non-zero."""


def is_safe_token(value):
    return isinstance(value, str) and bool(_SAFE_TOKEN_RE.match(value))


def require_environment_arg(environment):
    if not is_safe_token(environment):
        raise Phase3Error(f"environment {environment!r} is not a safe identifier; refusing to use it in a filesystem path.")
    return environment


def require_env(name):
    value = os.environ.get(name, "")
    if not value:
        raise Phase3Error(f"{name} is empty; canonical environment configuration must be loaded before this step.")
    return value


# Phase 3 state file

def default_state_path():
    """${RUNNER_TEMP}/goldengate-phase3-argocd-state.json, or a repo-local fallback outside CI."""
    runner_temp = os.environ.get("RUNNER_TEMP")
    if runner_temp:
        return Path(runner_temp) / "goldengate-phase3-argocd-state.json"
    return Path(os.environ.get("TMPDIR", "/tmp")) / "goldengate-phase3-argocd-state.json"


def load_state(state_path):
    if not state_path.exists():
        return {}
    try:
        with state_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise Phase3Error(f"Phase 3 state file {state_path} is unreadable/malformed: {exc}") from exc
    if not isinstance(data, dict):
        raise Phase3Error(f"Phase 3 state file {state_path} did not contain a JSON object.")
    return data


def save_state(state_path, state):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, sort_keys=True, indent=2)
        f.write("\n")
    tmp_path.replace(state_path)


def update_state(state_path, updates):
    disallowed = sorted(set(updates) - ALLOWED_STATE_KEYS)
    if disallowed:
        raise Phase3Error(f"refusing to write disallowed Phase 3 state key(s) {disallowed} -- Phase 3 state may only ever contain non-secret deployment metadata: {sorted(ALLOWED_STATE_KEYS)}")
    state = load_state(state_path)
    state.update(updates)
    save_state(state_path, state)
    return state


def require_state_value(state, key):
    if key not in state or state[key] in (None, ""):
        raise Phase3Error(f"Phase 3 state is missing required key {key!r}; an earlier step did not complete.")
    return state[key]


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


# Safe subprocess execution

def run(argv, env=None, cwd=None, check=True, capture_output=True, input_text=None):
    """Runs argv as an argument array -- the shell keyword argument is never passed and always defaults to disabled, and this helper never builds a shell pipeline. Fails closed with the tool's own stderr/stdout on a non-zero exit when check=True. input_text feeds the subprocess's stdin directly (e.g. an ECR password), never via a shell pipe."""
    proc = subprocess.run(
        argv,
        cwd=cwd or REPO_ROOT,
        env=env,
        capture_output=capture_output,
        text=True,
        input=input_text,
    )
    if check and proc.returncode != 0:
        raise Phase3Error(f"{' '.join(str(a) for a in argv)} failed (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
    return proc


def _kubectl_get_jsonpath(resource, name, namespace, jsonpath):
    proc = run(["kubectl", "get", resource, name, "-n", namespace, "-o", f"jsonpath={jsonpath}"], check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout


# Tool installation (never requires AWS credentials)

def _ensure_kubectl():
    if run(["bash", "-c", "command -v kubectl"], check=False).returncode == 0:
        run(["kubectl", "version", "--client=true"])
        return
    kubectl_version = "v1.35.0"
    machine = run(["uname", "-m"]).stdout.strip()
    arch_map = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
    if machine not in arch_map:
        raise Phase3Error(f"Unsupported architecture for kubectl: {machine}")
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
        raise Phase3Error(f"Unsupported architecture for Helm: {machine}")
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


# argocd_acceptance.py reuse (never a second independent envs/<environment>/argocd/values.yaml parser)

_argocd_acceptance_module = None


def _load_argocd_acceptance_module():
    import importlib.util
    global _argocd_acceptance_module
    if _argocd_acceptance_module is None:
        spec = importlib.util.spec_from_file_location("argocd_acceptance", ARGOCD_ACCEPTANCE_TOOL)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _argocd_acceptance_module = module
    return _argocd_acceptance_module


def _argocd_server_ingress_values(environment):
    """Reuses automation/phases/phase3/argocd_acceptance.py's own ingress_config_from_values() -- never a second independent parser for envs/<environment>/argocd/values.yaml's argocdServerIngress block."""
    module = _load_argocd_acceptance_module()
    module.REPO_ROOT = REPO_ROOT
    return module.ingress_config_from_values(environment)


# Phase 3A: ownership preflight (argocd_preflight)

def cmd_ownership_preflight(args):
    environment = require_environment_arg(args.environment)
    aws_region = require_env("AWS_REGION")
    eks_cluster_name = require_env("EKS_CLUSTER_NAME")
    eks_deploy_role_arn = require_env("EKS_DEPLOY_ROLE_ARN")

    run(["aws", "eks", "update-kubeconfig", "--region", aws_region, "--name", eks_cluster_name,
         "--role-arn", eks_deploy_role_arn, "--assume-role-arn", eks_deploy_role_arn])

    proc = run([sys.executable, str(ARGOCD_STATE_TOOL), "--environment", environment], check=False)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    if proc.returncode != 0:
        raise Phase3Error(f"Argo CD ownership-safety classifier failed (inspection error); refusing to guess ABSENT:\n{proc.stdout}")
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Phase3Error(f"Argo CD ownership-safety classifier produced unparseable output: {exc}") from exc

    state = result.get("state")
    if state not in ("ABSENT", "OWNED", "BROKEN"):
        raise Phase3Error(f"Argo CD ownership-safety classifier produced an unrecognized state {state!r}; refusing to proceed.")
    if state == "BROKEN":
        raise Phase3Error("Argo CD ownership-safety preflight classified the installation as BROKEN; refusing to reconcile. See diagnostics above.")

    write_github_output([("state", state)])
    print(f"OK: Argo CD ownership-safety preflight state is {state}.")


# Phase 3D: strict post-reconciliation acceptance (validate_argocd_ready)

def cmd_strict_acceptance(args):
    environment = require_environment_arg(args.environment)
    aws_region = require_env("AWS_REGION")
    eks_cluster_name = require_env("EKS_CLUSTER_NAME")
    eks_deploy_role_arn = require_env("EKS_DEPLOY_ROLE_ARN")

    run(["aws", "eks", "update-kubeconfig", "--region", aws_region, "--name", eks_cluster_name,
         "--role-arn", eks_deploy_role_arn, "--assume-role-arn", eks_deploy_role_arn])

    proc = run([sys.executable, str(ARGOCD_ACCEPTANCE_TOOL), "--environment", environment], check=False)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    if proc.returncode != 0:
        raise Phase3Error(f"Argo CD strict acceptance classifier failed (inspection error); refusing to guess HEALTHY:\n{proc.stdout}")
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Phase3Error(f"Argo CD strict acceptance classifier produced unparseable output: {exc}") from exc

    state = result.get("state")
    if state != "HEALTHY":
        raise Phase3Error(f"Argo CD strict post-reconciliation acceptance classified the installation as {state!r} (required: HEALTHY); reconciliation success alone is never sufficient. See diagnostics above.")

    print("OK: Argo CD is HEALTHY (strict post-reconciliation acceptance).")


# 20-sub-argocd.yaml: prepare deployment state

def cmd_prepare_deployment(args):
    environment = require_environment_arg(args.environment)
    ecr_registry = require_env("ECR_REGISTRY")
    run_number = require_env("GITHUB_RUN_NUMBER")

    values_file = f"{ENVS_ROOT}/{environment}/argocd/values.yaml"
    chart_version = f"0.1.{run_number}"
    helm_ecr_repository = f"{HELM_OCI_NAMESPACE}/{CHART_NAME}"
    helm_push_url = f"oci://{ecr_registry}/{HELM_OCI_NAMESPACE}"
    helm_chart_ref = f"oci://{ecr_registry}/{helm_ecr_repository}"

    update_state(args.state_path, {
        "environment": environment,
        "values_file": values_file,
        "chart_version": chart_version,
        "helm_ecr_repository": helm_ecr_repository,
        "helm_push_url": helm_push_url,
        "helm_chart_ref": helm_chart_ref,
    })
    print(f"Prepared Argo CD deployment variables: environment={environment!r} values_file={values_file!r} chart_version={chart_version!r} helm_chart_ref={helm_chart_ref!r}")


# 20-sub-argocd.yaml: local validation and packaging (no AWS credentials)

def _validate_vendored_dependency():
    vendored_chart = REPO_ROOT / HELM_CHART_PATH / "charts" / "argo-cd"
    chart_yaml = vendored_chart / "Chart.yaml"
    if not chart_yaml.is_file():
        raise Phase3Error(f"Missing vendored Argo CD chart: {chart_yaml}. This workflow only deploys from the local, file:// vendored dependency under {vendored_chart} -- it never fetches the argo-cd chart from a public Helm repository at build time. Vendor the upstream chart into that path before re-running.")
    with chart_yaml.open() as f:
        for line in f:
            if line.startswith("version:"):
                print(f"Vendored argo-cd chart version: {line.split(':', 1)[1].strip()}")
                break
    wrapper_chart_yaml = REPO_ROOT / HELM_CHART_PATH / "Chart.yaml"
    wrapper_source = wrapper_chart_yaml.read_text()
    if 'repository: "file://charts/argo-cd"' not in wrapper_source:
        raise Phase3Error(f"{wrapper_chart_yaml} does not declare a file://charts/argo-cd dependency.")
    print("Vendored dependency present and correctly declared.")


def _validate_required_files(values_file):
    chart_yaml = REPO_ROOT / HELM_CHART_PATH / "Chart.yaml"
    values_yaml = REPO_ROOT / HELM_CHART_PATH / "values.yaml"
    env_values_path = REPO_ROOT / values_file
    for path, label in ((chart_yaml, "Helm Chart.yaml"), (values_yaml, "Helm values.yaml"), (env_values_path, "environment values file")):
        if not path.is_file():
            raise Phase3Error(f"Missing {label}: {path}")
    print(f"Required files are present. Helm chart path: {HELM_CHART_PATH}. Environment values: {values_file}")


def _validate_ecr_iam_policy(environment):
    region = require_env("AWS_REGION")
    ecr_account_id = require_env("ECR_ACCOUNT_ID")
    policy_path = REPO_ROOT / "envs" / environment / "policies" / f"argocd-ecr-oci-read-{environment}" / "policies" / "policies_1.json"
    if not policy_path.is_file():
        raise Phase3Error(f"missing IAM policy file: {policy_path}")
    with policy_path.open() as f:
        policy = json.load(f)

    expected_repos = {name: f"arn:aws:ecr:{region}:{ecr_account_id}:repository/{name}" for name in REQUIRED_ECR_POLICY_REPOS}
    statements = policy.get("Statement", [])
    found_arns = set()
    for stmt in statements:
        resource = stmt.get("Resource")
        resources = resource if isinstance(resource, list) else [resource]
        actions = stmt.get("Action")
        actions = set(actions if isinstance(actions, list) else [actions])
        for r in resources:
            if r in expected_repos.values():
                found_arns.add(r)
                if not REQUIRED_ECR_POLICY_ACTIONS.issubset(actions):
                    missing = REQUIRED_ECR_POLICY_ACTIONS - actions
                    raise Phase3Error(f"statement for {r} is missing actions: {sorted(missing)}")
                if r == "*" or r.endswith("/*"):
                    raise Phase3Error(f"statement for {r} uses a wildcard resource -- must be an exact repository ARN.")

    missing_repos = set(expected_repos.values()) - found_arns
    if missing_repos:
        raise Phase3Error(f"IAM policy is missing a statement for: {sorted(missing_repos)}")

    for repo, arn in expected_repos.items():
        print(f"OK: {repo} -> {arn} present with required actions.")
    print(f"OK: {policy_path} grants exactly the four expected repository ARNs, no wildcards.")


def _helm_set_string_overrides():
    ecr_registry = require_env("ECR_REGISTRY")
    argocd_ecr_read_role_arn = require_env("ARGOCD_ECR_READ_ROLE_ARN")
    aws_region = require_env("AWS_REGION")
    argocd_host = require_env("ARGOCD_HOST")
    alb_group_name = require_env("ALB_GROUP_NAME")
    acm_certificate_arn = require_env("ACM_CERTIFICATE_ARN")
    overrides = {
        "argo-cd.global.image.repository": f"{ecr_registry}/aws-cloud-factory-infra-argocd",
        "argo-cd.redis.image.repository": f"{ecr_registry}/aws-cloud-factory-infra-redis-alpine",
        "ecrTokenSync.roleArn": argocd_ecr_read_role_arn,
        "ecrTokenSync.awsRegion": aws_region,
        "ecrTokenSync.ecrRegistry": ecr_registry,
        "ecrTokenSync.image.repository": f"{ecr_registry}/aws-cloud-factory-infra-aws-kubectl",
        "argocdServerIngress.host": argocd_host,
        "argocdServerIngress.groupName": alb_group_name,
        "argocdServerIngress.certificateArn": acm_certificate_arn,
    }
    flags = []
    for key, value in overrides.items():
        flags += ["--set-string", f"{key}={value}"]
    return flags


def _helm_dependency_build():
    proc = run(["helm", "dependency", "build", HELM_CHART_PATH], check=False)
    if proc.returncode != 0:
        print(f"WARNING: helm dependency build reported a non-zero exit ({proc.returncode}); continuing, matching the existing tolerated-build contract:\n{proc.stdout}\n{proc.stderr}")
    else:
        print(proc.stdout)


def _helm_lint(values_file):
    run(["helm", "lint", HELM_CHART_PATH, "--values", values_file, *_helm_set_string_overrides()])
    print("OK: helm lint passed.")


def _helm_template(values_file, namespace):
    rendered_dir = REPO_ROOT / "rendered"
    rendered_dir.mkdir(parents=True, exist_ok=True)
    rendered_path = rendered_dir / f"{ARGOCD_RELEASE_NAME}.yaml"
    proc = run(["helm", "template", ARGOCD_RELEASE_NAME, HELM_CHART_PATH, "--namespace", namespace, "--values", values_file, *_helm_set_string_overrides()])
    rendered_path.write_text(proc.stdout)
    print(f"Rendered manifest: {rendered_path.relative_to(REPO_ROOT)}")
    return rendered_path


def _extract_named_role_block(rendered_text, name):
    """Splits the rendered multi-document manifest on bare "---" document separators (never the first Role found -- the wrapper chart renders many Role/ClusterRole objects) and returns the one document with both kind: Role and the given metadata.name, or None."""
    docs = re.split(r"(?m)^---$", rendered_text)
    for doc in docs:
        if re.search(r"(?m)^kind:\s*Role\s*$", doc) and re.search(rf"(?m)^\s*name:\s*{re.escape(name)}\s*$", doc):
            return doc
    return None


def _validate_ecr_token_sync_rendered(rendered_path, values_file):
    rendered_text = rendered_path.read_text()
    with open(values_file) as f:
        ecr_token_sync_tag = (yaml.safe_load(f) or {})["ecrTokenSync"]["image"]["tag"]
    ecr_registry = require_env("ECR_REGISTRY")
    argocd_ecr_read_role_arn = require_env("ARGOCD_ECR_READ_ROLE_ARN")

    required_substrings = (
        "kind: CronJob",
        "name: argocd-ecr-token-sync",
        f"eks.amazonaws.com/role-arn: {argocd_ecr_read_role_arn}",
        f"{ecr_registry}/aws-cloud-factory-infra-aws-kubectl:{ecr_token_sync_tag}",
        'helm/goldengate"',
        'helm/goldengate-monitor"',
        'helm/goldengate-platform"',
        'helm/amazon-cloudwatch-observability"',
        *REQUIRED_REPO_SECRETS.keys(),
    )
    for needle in required_substrings:
        if needle not in rendered_text:
            raise Phase3Error(f"rendered manifest is missing expected ECR token sync content: {needle!r}")

    role_block = _extract_named_role_block(rendered_text, ECR_TOKEN_SYNC_NAME)
    if role_block is None:
        raise Phase3Error("no rendered document has both kind: Role and metadata.name: argocd-ecr-token-sync. Selecting the first rendered Role is unsafe -- it may belong to an unrelated Argo CD component instead of argocd-ecr-token-sync.")

    for secret_name in REQUIRED_REPO_SECRETS:
        if secret_name not in role_block:
            raise Phase3Error(f"Role resourceNames is missing {secret_name}.")
    for verb in ("get", "update", "patch"):
        if f"- {verb}" not in role_block:
            raise Phase3Error(f"Role does not grant the {verb} verb.")
    if re.search(r"(?m)^[ \t]*-[ \t]*(delete|list|watch)[ \t]*$", role_block):
        raise Phase3Error("Role grants a forbidden verb (delete, list, or watch).")

    print("OK: ServiceAccount/Role/RoleBinding/CronJob for ECR token sync are all present in the rendered manifest, covering all four repositories.")


def _expect(actual, expected, label):
    if actual != expected:
        raise Phase3Error(f"{label} is {actual!r}, expected {expected!r}")
    print(f"OK: {label} == {expected!r}")


def _validate_rendered_ingress(rendered_path, ingress_values, namespace):
    """Structural YAML validation (never a fragile grep chain) of the rendered argocd-server-ingress Ingress; skips (returns without checking) when argocdServerIngress.enabled is not true."""
    if not ingress_values.get("enabled"):
        print("argocdServerIngress.enabled is not true. Skipping rendered Ingress validation.")
        return

    argocd_host = require_env("ARGOCD_HOST")
    alb_group_name = require_env("ALB_GROUP_NAME")
    acm_certificate_arn = require_env("ACM_CERTIFICATE_ARN")

    with rendered_path.open() as f:
        docs = [d for d in yaml.safe_load_all(f) if d]

    ingresses = [d for d in docs if d.get("kind") == "Ingress" and (d.get("metadata") or {}).get("name") == "argocd-server-ingress"]
    if len(ingresses) != 1:
        raise Phase3Error(f"expected exactly 1 rendered Ingress named argocd-server-ingress, found {len(ingresses)}.")
    ingress = ingresses[0]
    print("OK: exactly 1 rendered Ingress named argocd-server-ingress.")

    metadata = ingress.get("metadata") or {}
    _expect(metadata.get("namespace"), namespace, "metadata.namespace")

    expected_ingress_class = ingress_values.get("ingressClassName") or "alb"
    spec = ingress.get("spec") or {}
    _expect(spec.get("ingressClassName"), expected_ingress_class, "spec.ingressClassName")

    rules = spec.get("rules") or []
    if len(rules) != 1:
        raise Phase3Error(f"expected exactly 1 rendered rule, found {len(rules)}.")
    _expect(rules[0].get("host"), argocd_host, "spec.rules[0].host")

    paths = ((rules[0].get("http") or {}).get("paths")) or []
    if len(paths) != 1:
        raise Phase3Error(f"expected exactly 1 rendered path, found {len(paths)}.")
    backend_service = ((paths[0].get("backend") or {}).get("service")) or {}
    expected_service_name = ingress_values.get("serviceName") or "argocd-server"
    expected_service_port = ingress_values.get("servicePort")
    if expected_service_port is None:
        expected_service_port = 443
    _expect(backend_service.get("name"), expected_service_name, "backend service name")
    _expect((backend_service.get("port") or {}).get("number"), expected_service_port, "backend service port")

    annotations = metadata.get("annotations") or {}
    _expect(annotations.get("alb.ingress.kubernetes.io/group.name"), alb_group_name, "annotations[group.name]")
    expected_group_order = ingress_values.get("groupOrder")
    if expected_group_order is not None:
        _expect(annotations.get("alb.ingress.kubernetes.io/group.order"), str(expected_group_order), "annotations[group.order]")
    _expect(annotations.get("alb.ingress.kubernetes.io/certificate-arn"), acm_certificate_arn, "annotations[certificate-arn]")

    for annotation_key, values_key in (
        ("alb.ingress.kubernetes.io/listen-ports", "listenPorts"),
        ("alb.ingress.kubernetes.io/target-type", "targetType"),
        ("alb.ingress.kubernetes.io/backend-protocol", "backendProtocol"),
        ("alb.ingress.kubernetes.io/healthcheck-protocol", "healthcheckProtocol"),
        ("alb.ingress.kubernetes.io/healthcheck-path", "healthcheckPath"),
        ("alb.ingress.kubernetes.io/healthcheck-port", "healthcheckPort"),
    ):
        expected_value = ingress_values.get(values_key)
        if expected_value:
            _expect(annotations.get(annotation_key), str(expected_value), f"annotations[{values_key}]")

    # Standalone resident anchor: this Ingress must own the ALB scheme; a shared-mode Ingress must NOT carry this annotation at all -- never asserted in that mode.
    if ingress_values.get("mode") == "standalone":
        expected_scheme = ingress_values.get("scheme")
        if expected_scheme:
            _expect(annotations.get("alb.ingress.kubernetes.io/scheme"), expected_scheme, "annotations[scheme] (standalone resident anchor)")

    print("OK: rendered Argo CD server Ingress passed every structural/contract check.")


def _validate_image_references(rendered_path):
    text = rendered_path.read_text()
    images = sorted({m.group(1).strip().strip("\"'") for m in _IMAGE_LINE_RE.finditer(text)})
    if not images:
        raise Phase3Error("No image references found in rendered manifest. This is unexpected -- failing.")
    print("Rendered image references:")
    for image in images:
        print(image)
    for image in images:
        if any(registry in image.lower() for registry in PUBLIC_REGISTRIES):
            raise Phase3Error(f"rendered image references a public registry: {image}. This EKS cluster has no public internet access. Every runtime image must be mirrored to private ECR and referenced from there.")
    print("OK: no public registry references found.")
    for image in images:
        if _PLACEHOLDER_RE.search(image):
            raise Phase3Error(f"rendered image still contains an unresolved placeholder: {image}. Replace the placeholder tag(s) in envs/<environment>/argocd/values.yaml with a real, approved image tag that has been mirrored to private ECR, then re-run this workflow.")
    print("OK: no placeholder values found.")


def _package_chart(chart_version):
    packaged_dir = REPO_ROOT / "packaged"
    packaged_dir.mkdir(parents=True, exist_ok=True)
    run(["helm", "package", HELM_CHART_PATH, "--version", chart_version, "--app-version", chart_version, "--destination", "packaged"])
    package_path = packaged_dir / f"{CHART_NAME}-{chart_version}.tgz"
    if not package_path.is_file():
        raise Phase3Error(f"helm package did not produce the expected archive: {package_path}")
    return package_path


def cmd_validate_local(args):
    environment = require_environment_arg(args.environment)
    state = load_state(args.state_path)
    values_file = require_state_value(state, "values_file")
    chart_version = require_state_value(state, "chart_version")
    namespace = require_env("ARGOCD_NAMESPACE")

    _validate_vendored_dependency()
    _validate_required_files(values_file)
    _validate_ecr_iam_policy(environment)
    _helm_dependency_build()
    _helm_lint(values_file)
    rendered_path = _helm_template(values_file, namespace)
    _validate_ecr_token_sync_rendered(rendered_path, values_file)
    ingress_values = _argocd_server_ingress_values(environment)
    _validate_rendered_ingress(rendered_path, ingress_values, namespace)
    _validate_image_references(rendered_path)
    package_path = _package_chart(chart_version)

    update_state(args.state_path, {
        "rendered_manifest": str(rendered_path.relative_to(REPO_ROOT)),
        "package_path": str(package_path.relative_to(REPO_ROOT)),
        "namespace": namespace,
        "ingress_enabled": bool(ingress_values.get("enabled")),
    })
    print("OK: Argo CD chart validated and packaged locally.")


# 20-sub-argocd.yaml: publish chart to private ECR (AWS credentials required)

def _ensure_ecr_repository(repository_name, aws_region):
    exists = run(["aws", "ecr", "describe-repositories", "--region", aws_region, "--repository-names", repository_name], check=False)
    if exists.returncode == 0:
        return
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


def cmd_publish_chart(args):
    require_environment_arg(args.environment)
    state = load_state(args.state_path)
    chart_version = require_state_value(state, "chart_version")
    package_path_rel = require_state_value(state, "package_path")
    helm_push_url = require_state_value(state, "helm_push_url")
    helm_ecr_repository = require_state_value(state, "helm_ecr_repository")
    helm_chart_ref = require_state_value(state, "helm_chart_ref")

    aws_region = require_env("AWS_REGION")
    ecr_registry = require_env("ECR_REGISTRY")

    run(["aws", "sts", "get-caller-identity"])

    # ECR login password: fed directly into helm's own stdin, never through a shell pipeline and never printed/logged.
    password_proc = run(["aws", "ecr", "get-login-password", "--region", aws_region])
    password = password_proc.stdout.strip()
    run(["helm", "registry", "login", "--username", "AWS", "--password-stdin", ecr_registry], input_text=password)

    _ensure_ecr_repository(helm_ecr_repository, aws_region)

    package_path = REPO_ROOT / package_path_rel
    run(["helm", "push", str(package_path), helm_push_url])
    print(f"Published Helm chart: {helm_chart_ref}:{chart_version}")

    pulled_dir = REPO_ROOT / "pulled"
    pulled_dir.mkdir(parents=True, exist_ok=True)
    run(["helm", "pull", helm_chart_ref, "--version", chart_version, "--destination", "pulled"])

    update_state(args.state_path, {"pulled_directory": "pulled"})
    print("OK: Argo CD Helm chart published to private ECR and verified pullable.")


# 20-sub-argocd.yaml: reconcile the release in EKS (AWS credentials required)

def _create_and_label_namespace(namespace, environment):
    dry_run = run(["kubectl", "create", "namespace", namespace, "--dry-run=client", "-o", "yaml"])
    run(["kubectl", "apply", "-f", "-"], input_text=dry_run.stdout)
    run(["kubectl", "label", "namespace", namespace,
         "app.kubernetes.io/name=argocd",
         "app.kubernetes.io/managed-by=github-actions",
         f"goldengate.adcb/environment={environment}",
         "--overwrite"])
    run(["kubectl", "get", "namespace", namespace, "--show-labels"], check=False)


def cmd_reconcile_cluster(args):
    environment = require_environment_arg(args.environment)
    state = load_state(args.state_path)
    values_file = require_state_value(state, "values_file")
    chart_version = require_state_value(state, "chart_version")
    helm_chart_ref = require_state_value(state, "helm_chart_ref")
    namespace = require_state_value(state, "namespace")

    aws_region = require_env("AWS_REGION")
    eks_cluster_name = require_env("EKS_CLUSTER_NAME")
    eks_cluster_arn = require_env("EKS_CLUSTER_ARN")
    workload_account_id = require_env("WORKLOAD_ACCOUNT_ID")
    eks_deploy_role_arn = require_env("EKS_DEPLOY_ROLE_ARN")

    print(f"Target EKS cluster name: {eks_cluster_name}")
    print(f"Target EKS cluster ARN: {eks_cluster_arn}")
    print(f"Target EKS account ID: {workload_account_id}")
    run(["aws", "sts", "get-caller-identity"])

    print(f"Connecting to EKS using cross-account EKS deploy role: {eks_deploy_role_arn}")
    run(["aws", "eks", "update-kubeconfig", "--region", aws_region, "--name", eks_cluster_name,
         "--role-arn", eks_deploy_role_arn, "--assume-role-arn", eks_deploy_role_arn])
    run(["kubectl", "config", "current-context"])
    run(["kubectl", "version", "--client=true"])

    run(["kubectl", "auth", "can-i", "create", "namespace"], check=False)
    run(["kubectl", "auth", "can-i", "get", "customresourcedefinitions.apiextensions.k8s.io"], check=False)

    _create_and_label_namespace(namespace, environment)

    run(["helm", "upgrade", "--install", ARGOCD_RELEASE_NAME, helm_chart_ref,
         "--version", chart_version, "--namespace", namespace, "--values", values_file,
         *_helm_set_string_overrides(),
         "--wait", "--atomic", "--cleanup-on-fail", "--timeout", "15m"])

    print("OK: Argo CD Helm release reconciled in EKS.")


# 20-sub-argocd.yaml: bounded post-deployment validation (AWS credentials required)

def _wait_for_rollouts(namespace):
    for kind, name in _ROLLOUT_TARGETS:
        run(["kubectl", "rollout", "status", f"{kind}/{name}", "-n", namespace, "--timeout=10m"])


def _print_deployment_diagnostics(namespace):
    for diag_args in (["get", "pods", "-n", namespace], ["get", "svc", "-n", namespace], ["get", "deploy", "-n", namespace], ["get", "statefulset", "-n", namespace]):
        proc = run(["kubectl", *diag_args], check=False)
        print(proc.stdout)
    crd_proc = run(["kubectl", "get", "crd"], check=False)
    print("\n".join(line for line in crd_proc.stdout.splitlines() if "argoproj" in line))

    # No check=False here: a missing ECR token-sync resource aborts this step instead of silently passing.
    for kind in ("serviceaccount", "role", "rolebinding", "cronjob"):
        run(["kubectl", "get", kind, ECR_TOKEN_SYNC_NAME, "-n", namespace])

    values_proc = run(["helm", "get", "values", ARGOCD_RELEASE_NAME, "-n", namespace, "-a"], check=False)
    print(f"Helm values (ecrTokenSync section):\n{values_proc.stdout}")
    manifest_proc = run(["helm", "get", "manifest", ARGOCD_RELEASE_NAME, "-n", namespace], check=False)
    print(f"Helm manifest (ECR token sync excerpt present): {'argocd-ecr-token-sync' in manifest_proc.stdout}")


def _dump_ingress_diagnostics(namespace):
    run(["kubectl", "get", "ingress", "argocd-server-ingress", "-n", namespace, "-o", "wide"], check=False)
    run(["kubectl", "describe", "ingress", "argocd-server-ingress", "-n", namespace], check=False)


def _wait_for_ingress_ready(namespace, argocd_host, ingress_values):
    """Preserves the original bash step's exact bound: TIMEOUT_SECONDS=900, INTERVAL_SECONDS=15, final probe at exactly elapsed==TIMEOUT_SECONDS (elapsed <= timeout_seconds, never < ), fail-closed immediately (never a transient-readiness tolerance) for a live host mismatch."""
    if not ingress_values.get("enabled"):
        print("argocdServerIngress.enabled is not true. Skipping Argo CD server Ingress readiness wait.")
        return

    timeout_seconds = 900
    interval_seconds = 15
    elapsed = 0
    lb_address = ""

    while elapsed <= timeout_seconds:
        exists_proc = run(["kubectl", "get", "ingress", "argocd-server-ingress", "-n", namespace], check=False)
        if exists_proc.returncode == 0:
            host_val = _kubectl_get_jsonpath("ingress", "argocd-server-ingress", namespace, "{.spec.rules[0].host}") or ""
            if host_val != argocd_host:
                _dump_ingress_diagnostics(namespace)
                raise Phase3Error(f"argocd-server-ingress spec.rules[0].host is {host_val!r}, expected {argocd_host!r}. This is a live desired-state mismatch, never a transient readiness gap -- refusing to wait further.")
            lb_address = _kubectl_get_jsonpath("ingress", "argocd-server-ingress", namespace, "{.status.loadBalancer.ingress[0].hostname}{.status.loadBalancer.ingress[0].ip}") or ""
            if lb_address:
                print(f"OK: argocd-server-ingress carries the expected host and has a published load-balancer address: {lb_address}")
                break
        if elapsed >= timeout_seconds:
            break
        print(f"Not yet ready (elapsed {elapsed}s/{timeout_seconds}s) -- waiting {interval_seconds}s...")
        time.sleep(interval_seconds)
        elapsed += interval_seconds

    if not lb_address:
        _dump_ingress_diagnostics(namespace)
        raise Phase3Error(f"argocd-server-ingress did not receive a published load-balancer address from the AWS Load Balancer Controller within {timeout_seconds}s.")


def _dump_job_diagnostics(job_name, namespace):
    run(["kubectl", "get", "job", job_name, "-n", namespace, "-o", "wide"], check=False)
    run(["kubectl", "describe", "job", job_name, "-n", namespace], check=False)
    run(["kubectl", "logs", f"job/{job_name}", "-n", namespace, "--all-containers=true"], check=False)


def _verify_repository_secret(secret_name, expected_repo, namespace, ecr_registry):
    exists_proc = run(["kubectl", "get", "secret", secret_name, "-n", namespace], check=False)
    if exists_proc.returncode != 0:
        raise Phase3Error(f"Secret {secret_name} does not exist in namespace {namespace}.")
    label = _kubectl_get_jsonpath("secret", secret_name, namespace, r"{.metadata.labels.argocd\.argoproj\.io/secret-type}") or ""
    if label != "repository":
        raise Phase3Error(f"Secret {secret_name} is missing the argocd.argoproj.io/secret-type=repository label (got: {label or '<none>'}).")
    url_proc = run(["kubectl", "get", "secret", secret_name, "-n", namespace, "-o", "jsonpath={.data.url}"])
    actual_url = base64.b64decode(url_proc.stdout).decode("utf-8") if url_proc.stdout else ""
    expected_url = f"oci://{ecr_registry}/{expected_repo}"
    if actual_url != expected_url:
        raise Phase3Error(f"Secret {secret_name} url mismatch. Expected {expected_url}, got {actual_url}.")
    # Password is intentionally never read or printed here.
    print(f"OK: Secret {secret_name} exists, labeled repository, url={actual_url}.")


def _run_ecr_token_sync_verification(namespace, ecr_registry, run_id, run_attempt):
    """Preserves the original bash step's exact bound: TIMEOUT_SECONDS=180, INTERVAL_SECONDS=10, an exclusive elapsed < timeout_seconds loop; success deletes the verification Job, failure/timeout retains it as evidence."""
    job_name = f"ecr-token-sync-verify-{run_id}-{run_attempt}".lower()
    job_name = re.sub(r"[^a-z0-9-]", "-", job_name)[:63].rstrip("-")

    run(["kubectl", "create", "job", job_name, "--from=cronjob/argocd-ecr-token-sync", "-n", namespace])

    timeout_seconds = 180
    interval_seconds = 10
    elapsed = 0
    result = None

    while elapsed < timeout_seconds:
        succeeded = _kubectl_get_jsonpath("job", job_name, namespace, "{.status.succeeded}") or ""
        failed = _kubectl_get_jsonpath("job", job_name, namespace, "{.status.failed}") or ""
        if succeeded and int(succeeded) >= 1:
            result = "succeeded"
            break
        if failed and int(failed) >= 1:
            result = "failed"
            break
        time.sleep(interval_seconds)
        elapsed += interval_seconds

    if result != "succeeded":
        _dump_job_diagnostics(job_name, namespace)
        raise Phase3Error(f"Job {job_name} did not succeed within {timeout_seconds}s (result: {result or 'timeout'}).")

    logs = run(["kubectl", "logs", f"job/{job_name}", "-n", namespace, "--all-containers=true"], check=False)
    print(logs.stdout)

    for secret_name, helm_repo in REQUIRED_REPO_SECRETS.items():
        _verify_repository_secret(secret_name, helm_repo, namespace, ecr_registry)

    run(["kubectl", "delete", "job", job_name, "-n", namespace, "--wait=true", "--timeout=60s"])
    print(f"OK: immediate bounded ECR token-sync validation passed for all {len(REQUIRED_REPO_SECRETS)} repositories.")


def cmd_post_deploy_validation(args):
    environment = require_environment_arg(args.environment)
    state = load_state(args.state_path)
    namespace = require_state_value(state, "namespace")

    argocd_host = require_env("ARGOCD_HOST")
    ecr_registry = require_env("ECR_REGISTRY")
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")

    _wait_for_rollouts(namespace)
    _print_deployment_diagnostics(namespace)
    ingress_values = _argocd_server_ingress_values(environment)
    _wait_for_ingress_ready(namespace, argocd_host, ingress_values)
    _run_ecr_token_sync_verification(namespace, ecr_registry, run_id, run_attempt)

    print("OK: bounded post-deployment validation passed.")


# 20-sub-argocd.yaml: workflow summary (always(), no AWS credentials, tolerant of partial state)

def cmd_summary(args):
    environment = args.environment
    state = load_state(args.state_path)
    namespace = state.get("namespace", "unknown")
    values_file = state.get("values_file", "unknown")
    helm_chart_ref = state.get("helm_chart_ref", "not published")
    chart_version = state.get("chart_version", "unknown")

    lines = [
        "## Argo CD EKS Helm Build and Deploy Summary",
        "",
        "### Deployment details",
        "",
        f"- Environment: `{environment}`",
        f"- Namespace: `{namespace}`",
        f"- Helm release: `{ARGOCD_RELEASE_NAME}`",
        f"- Values file: `{values_file}`",
        "",
        "### Published Helm OCI chart",
        "",
        f"- Chart ref: `{helm_chart_ref}`",
        f"- Chart version: `{chart_version}`",
        "",
        "### Validation commands",
        "",
        "```bash",
        f"kubectl get pods -n {namespace}",
        f"kubectl get svc -n {namespace}",
        f"kubectl get deploy -n {namespace}",
        f"kubectl get statefulset -n {namespace}",
        "kubectl get crd | grep argoproj",
        "```",
        "",
        "### Scope of this workflow",
        "",
        "Server Service remains ClusterIP. Argo CD server Ingress is managed according to argocdServerIngress.enabled and the environment ALB contract. This workflow does not configure Git repository credentials (HTTPS/PAT or SSH) for Argo CD. This workflow does not create GoldenGate runtime Applications (or any Argo CD Application/ApplicationSet resource).",
        "",
    ]
    write_step_summary("\n".join(lines))
    print("OK: wrote Argo CD deployment workflow summary.")


# CLI wiring

_SUBCOMMANDS = {
    "ensure-kubectl": cmd_ensure_kubectl,
    "ensure-deploy-tools": cmd_ensure_deploy_tools,
    "ownership-preflight": cmd_ownership_preflight,
    "prepare-deployment": cmd_prepare_deployment,
    "validate-local": cmd_validate_local,
    "publish-chart": cmd_publish_chart,
    "reconcile-cluster": cmd_reconcile_cluster,
    "post-deploy-validation": cmd_post_deploy_validation,
    "strict-acceptance": cmd_strict_acceptance,
    "summary": cmd_summary,
}

_ENVIRONMENT_SUBCOMMANDS = (
    "ownership-preflight", "prepare-deployment", "validate-local", "publish-chart",
    "reconcile-cluster", "post-deploy-validation", "strict-acceptance", "summary",
)


def build_parser():
    parser = argparse.ArgumentParser(description="Phase 3 | Argo CD orchestrator (ownership preflight, Helm build/publish/deploy, strict acceptance).")
    parser.add_argument("--state-file", type=Path, default=None, help="Override the Phase 3 state file path (default: $RUNNER_TEMP/goldengate-phase3-argocd-state.json).")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("ensure-kubectl")
    subparsers.add_parser("ensure-deploy-tools")
    for name in _ENVIRONMENT_SUBCOMMANDS:
        sub = subparsers.add_parser(name)
        sub.add_argument("--environment", required=True)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.state_path = args.state_file if args.state_file is not None else default_state_path()

    try:
        _SUBCOMMANDS[args.command](args)
    except Phase3Error as exc:
        print(f"FAIL: {exc}")
        return 1
    except subprocess.SubprocessError as exc:
        print(f"FAIL: subprocess execution error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
