#!/usr/bin/env python3
"""Phase 4D-4F | Observability (amazon-cloudwatch-observability) orchestration entrypoint for the observability_preflight/observability_sync_once/validate_observability_ready jobs in .github/workflows/00-main-goldengate-orchestrator.yaml and the validate_and_deploy job in .github/workflows/40-sub-observability.yaml; a thin orchestration/service layer that never reimplements environment.yaml parsing (owned by automation/goldengate-environment.py) and reuses, never duplicates, automation/phases/phase4/observability_state.py (pre-reconciliation ownership-safety preflight) and automation/phases/phase4/observability_acceptance.py (strict post-reconciliation acceptance) as separate subprocess-invoked classifiers. Non-secret Observability deployment metadata (image digests, chart path, namespace) is threaded between the 40-sub-observability.yaml subcommands through a JSON state file under the runner temp directory instead of large inline shell blocks; AWS credentials are never written to that state file, to $GITHUB_OUTPUT, or to $GITHUB_ENV."""
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
OBSERVABILITY_STATE_TOOL = REPO_ROOT / "automation" / "phases" / "phase4" / "observability_state.py"
OBSERVABILITY_ACCEPTANCE_TOOL = REPO_ROOT / "automation" / "phases" / "phase4" / "observability_acceptance.py"

_SAFE_TOKEN_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*\Z")

# Phase 4 Observability constants (.github/workflows/40-sub-observability.yaml) -- moved verbatim from the former workflow's top-level env: block, never re-derived.
HELM_OCI_NAMESPACE = "helm"
CHART_NAME = "amazon-cloudwatch-observability"
CHART_VERSION = "6.2.0"
CHART_ECR_REPOSITORY = "helm/amazon-cloudwatch-observability"
RELEASE_NAME = "amazon-cloudwatch-observability"
ARGOCD_APP_NAME = "goldengate-observability"
ARGOCD_OBSERVABILITY_SECRET_NAME = "argocd-ecr-amazon-cloudwatch-observability-oci"
CLOUDWATCH_AGENT_SERVICE_ACCOUNT = "cloudwatch-agent"

# (repository, tag) single source of truth driving digest resolution and generated-values injection -- the exact current four private image mirrors.
IMAGE_TABLE = (
    ("aws-cloud-factory-cloudwatch-agent-operator", "3.4.2"),
    ("aws-cloud-factory-cloudwatch-agent", "1.300069.0b1529"),
    ("aws-cloud-factory-kube-state-metrics", "v2.18.0"),
    ("aws-cloud-factory-node-exporter", "v1.11.1"),
)
IMAGE_VALUES_PATH = {
    "aws-cloud-factory-cloudwatch-agent-operator": ("manager", "image"),
    "aws-cloud-factory-cloudwatch-agent": ("agent", "image"),
    "aws-cloud-factory-kube-state-metrics": ("kubeStateMetrics", "image"),
    "aws-cloud-factory-node-exporter": ("nodeExporter", "image"),
}
ALLOWED_IMAGE_REPOS = tuple(repo for repo, _tag in IMAGE_TABLE)

UNAPPROVED_REGISTRIES = ("public.ecr.aws", "registry.k8s.io", "quay.io", "docker.io", "ghcr.io", "gcr.io", "nvcr.io")
FORBIDDEN_CR_KINDS = ("DcgmExporter", "NeuronMonitor", "Instrumentation")
FORBIDDEN_NAME_SUBSTRINGS = ("fluent-bit", "fluentbit", "dcgm", "neuron", "adot", "target-allocator", "auto-instrumentation")

ALLOWED_STATE_KEYS = frozenset({
    "environment", "values_file", "namespace", "chart_dir", "image_digests", "rendered_manifest",
    "generated_values_path", "cluster_scraper_correction",
})


class Phase4Error(Exception):
    """A fail-closed Phase 4 Observability error; main() reports it and exits non-zero."""


def is_safe_token(value):
    return isinstance(value, str) and bool(_SAFE_TOKEN_RE.match(value))


def require_environment_arg(environment):
    if not is_safe_token(environment):
        raise Phase4Error(f"environment {environment!r} is not a safe identifier; refusing to use it in a filesystem path.")
    return environment


def require_env(name):
    value = os.environ.get(name, "")
    if not value:
        raise Phase4Error(f"{name} is empty; canonical environment configuration must be loaded before this step.")
    return value


def default_state_path():
    """${RUNNER_TEMP}/goldengate-phase4-observability-state.json, or a repo-local fallback outside CI."""
    runner_temp = os.environ.get("RUNNER_TEMP")
    if runner_temp:
        return Path(runner_temp) / "goldengate-phase4-observability-state.json"
    return Path(os.environ.get("TMPDIR", "/tmp")) / "goldengate-phase4-observability-state.json"


def load_state(state_path):
    if not state_path.exists():
        return {}
    try:
        with state_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise Phase4Error(f"Phase 4 Observability state file {state_path} is unreadable/malformed: {exc}") from exc
    if not isinstance(data, dict):
        raise Phase4Error(f"Phase 4 Observability state file {state_path} did not contain a JSON object.")
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
        raise Phase4Error(f"refusing to write disallowed Phase 4 Observability state key(s) {disallowed} -- state may only ever contain non-secret deployment metadata: {sorted(ALLOWED_STATE_KEYS)}")
    state = load_state(state_path)
    state.update(updates)
    save_state(state_path, state)
    return state


def require_state_value(state, key):
    if key not in state or state[key] in (None, ""):
        raise Phase4Error(f"Phase 4 Observability state is missing required key {key!r}; an earlier step did not complete.")
    return state[key]


def write_github_output(pairs, output_path=None):
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


def run(argv, env=None, cwd=None, check=True, capture_output=True, input_text=None):
    """Runs argv as an argument array -- the shell keyword argument is never passed and always defaults to disabled, and this helper never builds a shell pipeline. Fails closed with the tool's own stderr/stdout on a non-zero exit when check=True."""
    proc = subprocess.run(
        argv,
        cwd=cwd or REPO_ROOT,
        env=env,
        capture_output=capture_output,
        text=True,
        input=input_text,
    )
    if check and proc.returncode != 0:
        raise Phase4Error(f"{' '.join(str(a) for a in argv)} failed (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
    return proc


def _kubectl_get_json(resource, name, namespace, check=True):
    proc = run(["kubectl", "get", resource, name, "-n", namespace, "-o", "json"], check=False)
    if proc.returncode != 0:
        if check:
            raise Phase4Error(f"kubectl get {resource} {name} -n {namespace} failed: {proc.stderr.strip()}")
        return None
    return json.loads(proc.stdout)


def _kubectl_get_jsonpath(resource, name, namespace, jsonpath):
    proc = run(["kubectl", "get", resource, name, "-n", namespace, "-o", f"jsonpath={jsonpath}"], check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout


def _label_selector(matched_object):
    match_labels = ((matched_object.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
    return ",".join(f"{k}={v}" for k, v in match_labels.items())


def _pods_for_selector(namespace, selector):
    proc = run(["kubectl", "get", "pods", "-n", namespace, "-l", selector, "-o", "json"])
    return (json.loads(proc.stdout) or {}).get("items") or []


def _replicaset_owner_deployment_uid(namespace, replicaset_name):
    """Fail-closed ReplicaSet ownership inspection for the canonical current-revision pod resolver below. Returns the ReplicaSet's own controller Deployment UID (or None if the ReplicaSet itself has no single Deployment controller -- it cannot then certify any pod as current for any Deployment). A genuine Kubernetes NotFound (the ReplicaSet has already disappeared, e.g. a fully-scaled-down stale revision) also returns None -- that pod is correctly excluded from the current-revision set, never treated as current. Every OTHER inspection failure (Forbidden, network/API failure, malformed JSON, unknown error) raises Phase4Error: an RBAC/API failure must never silently shrink the authoritative pod set into a false pass."""
    proc = run(["kubectl", "get", "replicaset", replicaset_name, "-n", namespace, "-o", "json"], check=False)
    if proc.returncode != 0:
        error_text = (proc.stderr or "") + (proc.stdout or "")
        if re.search(r"NotFound", error_text, re.IGNORECASE):
            return None
        raise Phase4Error(f"could not inspect ReplicaSet {replicaset_name} in {namespace} (fail-closed -- an inspection failure is never treated as a stale/absent ReplicaSet): {error_text.strip() or '(no output)'}")
    try:
        replicaset = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Phase4Error(f"ReplicaSet {replicaset_name} in {namespace} returned malformed JSON: {exc}") from exc
    owner_refs = [o for o in ((replicaset.get("metadata") or {}).get("ownerReferences") or []) if o.get("controller") is True]
    if len(owner_refs) != 1 or owner_refs[0].get("kind") != "Deployment":
        return None
    return owner_refs[0].get("uid")


def _current_deployment_pods(namespace, deployment_name, *, running_only=False, ready_only=False):
    """The one canonical current-revision pod resolver for every Observability acceptance/diagnostic path that must never certify a stale-ReplicaSet pod (cluster-scraper's Deployment is recreated in place by _ensure_cluster_scraper_host_network_isolated(), so a stale ReplicaSet's pod can still be Running/Ready for a time after a new revision is live). Resolves the live Deployment's own metadata.uid, derives its pod selector via _label_selector(), then for every non-terminating candidate pod walks Pod -> controller ReplicaSet -> controller Deployment UID and requires an exact match before classifying the pod as CURRENT REVISION. Fails closed (Phase4Error) on a missing Deployment UID, an empty selector, or any ReplicaSet inspection failure other than a genuine NotFound -- callers never see a silently-narrowed pod set."""
    deploy = _kubectl_get_json("deployment", deployment_name, namespace)
    deploy_uid = (deploy.get("metadata") or {}).get("uid")
    if not deploy_uid:
        raise Phase4Error(f"Deployment/{deployment_name} in {namespace} has no metadata.uid -- refusing to resolve current-revision pods.")

    selector = _label_selector(deploy)
    if not selector:
        raise Phase4Error(f"Deployment/{deployment_name} in {namespace} has an empty pod selector -- refusing to resolve current-revision pods.")

    replicaset_uid_cache = {}
    current_pods = []
    for pod in _pods_for_selector(namespace, selector):
        metadata = pod.get("metadata") or {}
        if metadata.get("deletionTimestamp"):
            continue
        owner_refs = [o for o in (metadata.get("ownerReferences") or []) if o.get("controller") is True]
        if len(owner_refs) != 1 or owner_refs[0].get("kind") != "ReplicaSet":
            continue
        replicaset_name = owner_refs[0].get("name")
        if replicaset_name not in replicaset_uid_cache:
            replicaset_uid_cache[replicaset_name] = _replicaset_owner_deployment_uid(namespace, replicaset_name)
        if replicaset_uid_cache[replicaset_name] != deploy_uid:
            continue
        if running_only and (pod.get("status") or {}).get("phase") != "Running":
            continue
        if ready_only:
            ready = next((c.get("status") for c in ((pod.get("status") or {}).get("conditions") or []) if c.get("type") == "Ready"), "Unknown")
            if ready != "True":
                continue
        current_pods.append(pod)
    return current_pods


# Tool installation (never requires AWS credentials)

def _ensure_kubectl():
    if run(["bash", "-c", "command -v kubectl"], check=False).returncode == 0:
        run(["kubectl", "version", "--client=true"])
        return
    kubectl_version = "v1.35.0"
    machine = run(["uname", "-m"]).stdout.strip()
    arch_map = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
    if machine not in arch_map:
        raise Phase4Error(f"Unsupported architecture for kubectl: {machine}")
    kubectl_arch = arch_map[machine]
    run(["curl", "-fsSL", f"https://dl.k8s.io/release/{kubectl_version}/bin/linux/{kubectl_arch}/kubectl", "-o", "/tmp/kubectl"])
    run(["sudo", "mv", "/tmp/kubectl", "/usr/local/bin/kubectl"])
    run(["sudo", "chmod", "+x", "/usr/local/bin/kubectl"])
    run(["kubectl", "version", "--client=true"])


def _ensure_helm():
    if run(["bash", "-c", "command -v helm"], check=False).returncode != 0:
        helm_version = "v3.15.4"
        machine = run(["uname", "-m"]).stdout.strip()
        arch_map = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
        if machine not in arch_map:
            raise Phase4Error(f"Unsupported architecture for Helm: {machine}")
        helm_arch = arch_map[machine]
        run(["curl", "-fsSL", f"https://get.helm.sh/helm-{helm_version}-linux-{helm_arch}.tar.gz", "-o", "/tmp/helm.tar.gz"])
        run(["tar", "-zxvf", "/tmp/helm.tar.gz", "-C", "/tmp"])
        run(["sudo", "mv", f"/tmp/linux-{helm_arch}/helm", "/usr/local/bin/helm"])
        run(["sudo", "chmod", "+x", "/usr/local/bin/helm"])

    version_text = run(["helm", "version", "--short"]).stdout.strip()
    match = re.search(r"v?(\d+)\.(\d+)", version_text)
    if not match or (int(match.group(1)), int(match.group(2))) < (3, 9):
        raise Phase4Error(f"Helm {version_text!r} is older than the required 3.9.")
    print(f"Helm version: {version_text} (required: 3.9 or later)")


def _ensure_jq():
    if run(["bash", "-c", "command -v jq"], check=False).returncode != 0:
        raise Phase4Error("jq is not installed and this tool does not install system packages -- provision jq on the runner image.")
    run(["jq", "--version"])


def cmd_ensure_tools(args):
    _ensure_helm()
    _ensure_kubectl()
    _ensure_jq()
    print("OK: Helm (>=3.9), kubectl, and jq are available.")


# automation/phases/phase4/observability_acceptance.py reuse (never a second independent envs/<environment>/argocd/values.yaml parser is needed here; this module has no equivalent shared logic to reuse beyond the classifiers themselves, invoked as subprocesses below)

# Phase 4D: observability ownership preflight (observability_preflight)

def cmd_ownership_preflight(args):
    environment = require_environment_arg(args.environment)
    aws_region = require_env("AWS_REGION")
    eks_cluster_name = require_env("EKS_CLUSTER_NAME")
    eks_deploy_role_arn = require_env("EKS_DEPLOY_ROLE_ARN")

    run(["aws", "eks", "update-kubeconfig", "--region", aws_region, "--name", eks_cluster_name,
         "--role-arn", eks_deploy_role_arn, "--assume-role-arn", eks_deploy_role_arn])

    proc = run([sys.executable, str(OBSERVABILITY_STATE_TOOL), "--environment", environment], check=False)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    if proc.returncode != 0:
        raise Phase4Error(f"Observability ownership-safety classifier failed (inspection error); refusing to guess ABSENT:\n{proc.stdout}")
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Phase4Error(f"Observability ownership-safety classifier produced unparseable output: {exc}") from exc

    state = result.get("state")
    if state not in ("ABSENT", "OWNED", "BROKEN"):
        raise Phase4Error(f"Observability ownership-safety classifier produced an unrecognized state {state!r}; refusing to proceed.")
    if state == "BROKEN":
        raise Phase4Error("Observability ownership-safety preflight classified the installation as BROKEN; refusing to reconcile. See diagnostics above.")

    write_github_output([("state", state)])
    print(f"OK: Observability ownership-safety preflight state is {state}.")


# Phase 4F: strict post-reconciliation acceptance (validate_observability_ready)

def cmd_strict_acceptance(args):
    environment = require_environment_arg(args.environment)
    aws_region = require_env("AWS_REGION")
    eks_cluster_name = require_env("EKS_CLUSTER_NAME")
    eks_deploy_role_arn = require_env("EKS_DEPLOY_ROLE_ARN")

    run(["aws", "eks", "update-kubeconfig", "--region", aws_region, "--name", eks_cluster_name,
         "--role-arn", eks_deploy_role_arn, "--assume-role-arn", eks_deploy_role_arn])

    proc = run([sys.executable, str(OBSERVABILITY_ACCEPTANCE_TOOL), "--environment", environment], check=False)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    if proc.returncode != 0:
        raise Phase4Error(f"Observability acceptance classifier failed (inspection error); refusing to guess HEALTHY:\n{proc.stdout}")
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Phase4Error(f"Observability acceptance classifier produced unparseable output: {exc}") from exc

    state = result.get("state")
    if state != "HEALTHY":
        raise Phase4Error(f"Observability acceptance classified the installation as {state!r} (required: HEALTHY); reconciliation success alone is never sufficient. See diagnostics above.")

    print("OK: Observability is HEALTHY (strict post-reconciliation acceptance).")


# 40-sub-observability.yaml: prepare local state (no AWS credentials)

def cmd_prepare(args):
    environment = require_environment_arg(args.environment)
    values_file = f"platform/{environment}/goldengate-observability/values.yaml"
    target_namespace = require_env("OBSERVABILITY_NAMESPACE")
    update_state(args.state_path, {"environment": environment, "values_file": values_file, "namespace": target_namespace})
    print(f"OK: prepared Observability local state for environment {environment!r} (namespace={target_namespace!r}).")


# 40-sub-observability.yaml: resolve and verify private ECR artifacts (AWS credentials required)

def _check_repo_exists(repo, aws_region):
    proc = run(["aws", "ecr", "describe-repositories", "--region", aws_region, "--repository-names", repo], check=False)
    if proc.returncode != 0:
        raise Phase4Error(f"ECR repository {repo} does not exist or could not be described: {proc.stderr.strip()}")
    print(f"OK: repository {repo} exists.")
    return json.loads(proc.stdout)


def _check_repo_immutable(repo, aws_region):
    proc = run(["aws", "ecr", "describe-repositories", "--region", aws_region, "--repository-names", repo, "--query", "repositories[0].imageTagMutability", "--output", "text"])
    mutability = proc.stdout.strip()
    if mutability != "IMMUTABLE":
        raise Phase4Error(f"repository {repo} has imageTagMutability={mutability}, expected IMMUTABLE.")
    print(f"OK: repository {repo} is IMMUTABLE.")


def _resolve_image_digest(repo, tag, aws_region):
    proc = run(["aws", "ecr", "describe-images", "--region", aws_region, "--repository-name", repo, "--image-ids", f"imageTag={tag}", "--output", "json"], check=False)
    if proc.returncode != 0:
        raise Phase4Error(f"tag {tag} not found in repository {repo}: {proc.stderr.strip()}")
    result = json.loads(proc.stdout)
    digest = ((result.get("imageDetails") or [{}])[0]).get("imageDigest")
    if not digest:
        raise Phase4Error(f"empty digest resolved for {repo}:{tag}.")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise Phase4Error(f"malformed digest resolved for {repo}:{tag}: {digest}")
    print(f"OK: {repo}:{tag} -> {digest}")
    return digest


def cmd_resolve_private_artifacts(args):
    require_environment_arg(args.environment)
    aws_region = require_env("AWS_REGION")
    ecr_account_id = require_env("ECR_ACCOUNT_ID")
    ecr_registry = require_env("ECR_REGISTRY")

    caller_account = run(["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"]).stdout.strip()
    if caller_account != ecr_account_id:
        raise Phase4Error(f"AWS caller account is {caller_account}, expected {ecr_account_id}.")
    print(f"OK: AWS caller account is {ecr_account_id}.")

    _check_repo_exists(CHART_ECR_REPOSITORY, aws_region)
    _check_repo_immutable(CHART_ECR_REPOSITORY, aws_region)
    for repo, _tag in IMAGE_TABLE:
        _check_repo_exists(repo, aws_region)
        _check_repo_immutable(repo, aws_region)

    seen_repos = set()
    digests = {}
    for repo, tag in IMAGE_TABLE:
        if repo in seen_repos:
            raise Phase4Error(f"repository {repo} appears more than once in the image inventory.")
        seen_repos.add(repo)
        digests[repo] = {"tag": tag, "digest": _resolve_image_digest(repo, tag, aws_region)}

    password_proc = run(["aws", "ecr", "get-login-password", "--region", aws_region])
    password = password_proc.stdout.strip()
    run(["helm", "registry", "login", "--username", "AWS", "--password-stdin", ecr_registry], input_text=password)

    chart_untar_dir = REPO_ROOT / "work" / "chart"
    chart_untar_dir.mkdir(parents=True, exist_ok=True)
    run(["helm", "pull", f"oci://{ecr_registry}/{HELM_OCI_NAMESPACE}/{CHART_NAME}", "--version", CHART_VERSION, "--untar", "--untardir", str(chart_untar_dir)])
    chart_dir = chart_untar_dir / CHART_NAME
    chart_yaml = chart_dir / "Chart.yaml"
    if not chart_yaml.is_file():
        raise Phase4Error(f"expected chart directory {chart_dir} was not produced by helm pull.")

    with chart_yaml.open() as f:
        chart_meta = yaml.safe_load(f) or {}
    if chart_meta.get("name") != CHART_NAME:
        raise Phase4Error(f"chart name is {chart_meta.get('name')!r}, expected {CHART_NAME!r}.")
    if chart_meta.get("version") != CHART_VERSION:
        raise Phase4Error(f"chart version is {chart_meta.get('version')!r}, expected {CHART_VERSION!r}.")
    print(f"OK: private chart name={chart_meta.get('name')} version={chart_meta.get('version')}")

    update_state(args.state_path, {"chart_dir": str(chart_dir.relative_to(REPO_ROOT)), "image_digests": digests})

    # work/image-digests.psv preserved for artifact-upload/diagnostic parity with the prior workflow -- repo|tag|digest, one line per image; non-secret.
    work_dir = REPO_ROOT / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    with (work_dir / "image-digests.psv").open("w") as f:
        for repo, tag in IMAGE_TABLE:
            f.write(f"{repo}|{tag}|{digests[repo]['digest']}\n")

    print("OK: private ECR artifacts resolved and verified; private chart pulled.")


# 40-sub-observability.yaml: local render/semantic validation (no AWS credentials, chart already pulled locally)

def _load_committed_values(values_path):
    class DupKeyLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in mapping:
                raise Phase4Error(f"duplicate key {key!r} in {values_path}")
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    DupKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)
    with open(values_path) as f:
        return yaml.load(f, Loader=DupKeyLoader)


def _generate_deployment_values(committed_values_path, digests, cluster_name, region, ecr_registry, output_path):
    """Injects tag@digest image references, plus clusterName/region/repositoryDomainMap.public into the generated values file; the committed file itself is never mutated."""
    values = _load_committed_values(committed_values_path)
    values["clusterName"] = cluster_name
    values["region"] = region

    for repo, (top_key, image_key) in IMAGE_VALUES_PATH.items():
        if repo not in digests:
            raise Phase4Error(f"no resolved digest for repository {repo}")
        tag, digest = digests[repo]["tag"], digests[repo]["digest"]
        image_block = values[top_key][image_key]
        if image_block["repository"] != repo:
            raise Phase4Error(f"{top_key}.{image_key}.repository is {image_block['repository']!r}, expected {repo!r}")
        if image_block["tag"] != tag:
            raise Phase4Error(f"{top_key}.{image_key}.tag is {image_block['tag']!r}, expected base tag {tag!r} before digest injection")
        image_block["tag"] = f"{tag}@{digest}"
        image_block["repositoryDomainMap"] = {"public": ecr_registry}

    with open(output_path, "w") as f:
        yaml.safe_dump(values, f, default_flow_style=False, sort_keys=False)
    print(f"OK: generated {output_path} with clusterName/region/repositoryDomainMap.public and tag@digest for all four images.")


def _expect(actual, expected, label):
    if actual != expected:
        raise Phase4Error(f"{label} is {actual!r}, expected {expected!r}")
    print(f"OK: {label} == {expected!r}")


def _validate_generated_values_semantics(values_path, cluster_name, region, ecr_registry):
    with open(values_path) as f:
        v = yaml.safe_load(f)

    _expect(v.get("clusterName"), cluster_name, "clusterName")
    _expect(v.get("region"), region, "region")
    _expect(v.get("k8sMode"), "EKS", "k8sMode")
    _expect((v.get("containerLogs") or {}).get("enabled"), False, "containerLogs.enabled")
    _expect((v.get("containerInsights") or {}).get("enabled"), False, "containerInsights.enabled")
    _expect((v.get("applicationSignals") or {}).get("enabled"), False, "applicationSignals.enabled")
    _expect(((v.get("manager") or {}).get("applicationSignals") or {}).get("autoMonitor", {}).get("monitorAllServices"), False, "manager.applicationSignals.autoMonitor.monitorAllServices")
    _expect((v.get("otelContainerInsights") or {}).get("enabled"), True, "otelContainerInsights.enabled")
    _expect((v.get("otelContainerInsights") or {}).get("logs", {}).get("enabled"), False, "otelContainerInsights.logs.enabled")
    _expect((v.get("dcgmExporter") or {}).get("enabled"), False, "dcgmExporter.enabled")
    _expect((v.get("neuronMonitor") or {}).get("enabled"), False, "neuronMonitor.enabled")
    _expect((v.get("kubeStateMetrics") or {}).get("enabled"), True, "kubeStateMetrics.enabled")
    _expect((v.get("nodeExporter") or {}).get("enabled"), True, "nodeExporter.enabled")
    _expect(((v.get("agent") or {}).get("prometheus") or {}).get("targetAllocator", {}).get("enabled"), False, "agent.prometheus.targetAllocator.enabled")
    _expect(((v.get("agent") or {}).get("serviceAccount") or {}).get("name"), "cloudwatch-agent", "agent.serviceAccount.name")

    agents = v.get("agents")
    if not isinstance(agents, list) or len(agents) != 2:
        raise Phase4Error(f"agents must be a list of exactly 2 entries, found {agents!r}")
    agent_names = [a.get("name") for a in agents]
    if len(agent_names) != len(set(agent_names)):
        raise Phase4Error(f"agents contains duplicate names: {agent_names}")
    expected_names = {"cloudwatch-agent", "cloudwatch-agent-cluster-scraper"}
    if set(agent_names) != expected_names:
        raise Phase4Error(f"agents names are {sorted(agent_names)}, expected exactly {sorted(expected_names)}.")
    print(f"OK: agents is a list of exactly 2 entries named {sorted(expected_names)}, no duplicates.")

    agents_by_name = {a.get("name"): a for a in agents}
    cw_agent = agents_by_name["cloudwatch-agent"]
    _expect(cw_agent.get("mode"), "daemonset", "agents[cloudwatch-agent].mode")
    if cw_agent.get("hostNetwork") is not True:
        raise Phase4Error(f"agents[cloudwatch-agent].hostNetwork is {cw_agent.get('hostNetwork')!r}, expected literal true")
    print("OK: agents[cloudwatch-agent].hostNetwork is literal true.")

    scraper_agent = agents_by_name["cloudwatch-agent-cluster-scraper"]
    _expect(scraper_agent.get("mode"), "deployment", "agents[cloudwatch-agent-cluster-scraper].mode")
    _expect(scraper_agent.get("config"), "default", "agents[cloudwatch-agent-cluster-scraper].config")
    if scraper_agent.get("hostNetwork") is not False:
        raise Phase4Error(f"agents[cloudwatch-agent-cluster-scraper].hostNetwork is {scraper_agent.get('hostNetwork')!r}, expected literal false")
    print("OK: agents[cloudwatch-agent-cluster-scraper].hostNetwork is literal false.")

    for top_key, image_key in IMAGE_VALUES_PATH.values():
        block = v[top_key][image_key]
        domain = (block.get("repositoryDomainMap") or {}).get("public")
        _expect(domain, ecr_registry, f"{top_key}.{image_key}.repositoryDomainMap.public")
        tag = block.get("tag", "")
        if "@sha256:" not in tag:
            raise Phase4Error(f"{top_key}.{image_key}.tag ({tag!r}) does not contain @sha256:")
        print(f"OK: {top_key}.{image_key}.tag contains @sha256: ({tag})")
    print("OK: generated values file passes every semantic check.")


def _render_chart(chart_dir, release_name, target_namespace, generated_values_path):
    rendered_dir = REPO_ROOT / "rendered"
    rendered_dir.mkdir(parents=True, exist_ok=True)
    rendered_path = rendered_dir / f"{release_name}.yaml"
    proc = run(["helm", "template", release_name, str(chart_dir), "--namespace", target_namespace, "--include-crds", "--values", str(generated_values_path)])
    rendered_path.write_text(proc.stdout)
    print(f"Rendered manifest: {rendered_path.relative_to(REPO_ROOT)} ({len(proc.stdout.splitlines())} lines)")
    return rendered_path


def _walk_images(node, images):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "image" and isinstance(v, str):
                images.add(v)
            else:
                _walk_images(v, images)
    elif isinstance(node, list):
        for item in node:
            _walk_images(item, images)


def _validate_no_unresolved_placeholders(rendered_path):
    text = rendered_path.read_text()
    if "<no value>" in text:
        raise Phase4Error("rendered manifest contains an unresolved Helm placeholder: <no value>")
    print("OK: no unresolved Helm placeholders in the rendered manifest.")


def _validate_rendered_images(docs, ecr_registry):
    images = set()
    for doc in docs:
        _walk_images(doc, images)
    images = sorted(images)
    print(f"Rendered image set: {images}")

    for image in images:
        lowered = image.lower()
        if any(registry in lowered for registry in UNAPPROVED_REGISTRIES):
            raise Phase4Error(f"rendered image set references an unapproved public registry: {image}")
    print("OK: no unapproved public registry reference.")

    expected_prefix = f"{ecr_registry}/"
    for image in images:
        if not image.startswith(expected_prefix):
            raise Phase4Error(f"image {image} does not use the private registry {expected_prefix}")
        if "@sha256:" not in image:
            raise Phase4Error(f"image {image} has no @sha256: digest.")
        repo = image[len(expected_prefix):].split(":")[0]
        if repo not in ALLOWED_IMAGE_REPOS:
            raise Phase4Error(f"image {image} belongs to repository {repo}, which is outside the four allow-listed repositories.")
    if len(images) != 4:
        raise Phase4Error(f"expected exactly 4 unique rendered images, found {len(images)}.")
    print("OK: exactly 4 unique rendered images, all private, all repository-allow-listed, all digest-pinned.")


def _validate_no_forbidden_components(docs):
    present_forbidden_kinds = {d.get("kind") for d in docs} & set(FORBIDDEN_CR_KINDS)
    if present_forbidden_kinds:
        raise Phase4Error(f"forbidden CR instance kind(s) rendered: {sorted(present_forbidden_kinds)}")

    for doc in docs:
        if doc.get("kind") == "CustomResourceDefinition":
            continue
        name = ((doc.get("metadata") or {}).get("name") or "").lower()
        for substring in FORBIDDEN_NAME_SUBSTRINGS:
            if substring in name:
                raise Phase4Error(f"forbidden resource name {name!r} (kind={doc.get('kind')}) matches forbidden substring {substring!r}.")
    print("OK: no DcgmExporter/NeuronMonitor/Instrumentation CR instance, and no fluent-bit/dcgm/neuron/adot/target-allocator/auto-instrumentation named resource, was rendered.")

    for doc in docs:
        if doc.get("kind") == "AmazonCloudWatchAgent":
            otel_config = str((doc.get("spec") or {}).get("otelConfig", ""))
            if "filelog" in otel_config:
                raise Phase4Error(f"AmazonCloudWatchAgent/{(doc.get('metadata') or {}).get('name')} otelConfig contains a filelog receiver -- OTel logs must stay disabled.")
    print("OK: no filelog receiver in any rendered AmazonCloudWatchAgent otelConfig.")

    for doc in docs:
        if doc.get("kind") == "DaemonSet" and "fluent" in ((doc.get("metadata") or {}).get("name") or "").lower():
            raise Phase4Error("a Fluent Bit DaemonSet was rendered.")
    print("OK: no Fluent Bit DaemonSet or container rendered.")


def _validate_cloudwatch_agent_service_account(docs, target_namespace):
    service_accounts = [d for d in docs if d.get("kind") == "ServiceAccount"]
    cw_agent_sas = [sa for sa in service_accounts if (sa.get("metadata") or {}).get("name") == "cloudwatch-agent" and (sa.get("metadata") or {}).get("namespace") == target_namespace]
    if len(cw_agent_sas) != 1:
        raise Phase4Error(f"expected exactly 1 ServiceAccount {target_namespace}/cloudwatch-agent, found {len(cw_agent_sas)}.")
    print(f"OK: ServiceAccount {target_namespace}/cloudwatch-agent is rendered by the chart.")

    for sa in service_accounts:
        annotations = (sa.get("metadata") or {}).get("annotations") or {}
        role_arn = annotations.get("eks.amazonaws.com/role-arn")
        name = (sa.get("metadata") or {}).get("name")
        if role_arn and name != "cloudwatch-agent":
            raise Phase4Error(f"unexpected eks.amazonaws.com/role-arn annotation on unrelated ServiceAccount {name}: {role_arn}")
        if role_arn and name == "cloudwatch-agent":
            raise Phase4Error("chart unexpectedly renders a role-arn annotation on cloudwatch-agent directly -- update the digest-injection step to stop double-annotating.")
    print("OK: no ServiceAccount carries an eks.amazonaws.com/role-arn annotation from the chart render itself (applied post-sync instead).")


def _find_one(docs, kind, name):
    matches = [d for d in docs if d.get("kind") == kind and (d.get("metadata") or {}).get("name") == name]
    if len(matches) != 1:
        raise Phase4Error(f"expected exactly 1 {kind}/{name}, found {len(matches)}.")
    return matches[0]


def _validate_exact_resource_names(docs):
    _find_one(docs, "AmazonCloudWatchAgent", "cloudwatch-agent")
    _find_one(docs, "AmazonCloudWatchAgent", "cloudwatch-agent-cluster-scraper")
    _find_one(docs, "Deployment", "amazon-cloudwatch-observability-controller-manager")
    _find_one(docs, "Deployment", "kube-state-metrics")
    _find_one(docs, "DaemonSet", "node-exporter")
    _find_one(docs, "ServiceAccount", "kube-state-metrics-service-acct")
    _find_one(docs, "ServiceAccount", "node-exporter-service-acct")
    _find_one(docs, "ServiceAccount", "amazon-cloudwatch-observability-controller-manager")
    print("OK: every expected chart-rendered resource name is present exactly once.")


def _validate_rendered_host_network_isolation(docs):
    cw_agent_cr = _find_one(docs, "AmazonCloudWatchAgent", "cloudwatch-agent")
    if (cw_agent_cr.get("spec") or {}).get("mode") != "daemonset":
        raise Phase4Error(f"AmazonCloudWatchAgent/cloudwatch-agent spec.mode is {(cw_agent_cr.get('spec') or {}).get('mode')!r}, expected 'daemonset'.")
    if (cw_agent_cr.get("spec") or {}).get("hostNetwork") is not True:
        raise Phase4Error(f"AmazonCloudWatchAgent/cloudwatch-agent spec.hostNetwork is {(cw_agent_cr.get('spec') or {}).get('hostNetwork')!r}, expected literal true.")
    print("OK: AmazonCloudWatchAgent/cloudwatch-agent mode=daemonset hostNetwork=True")

    scraper_cr = _find_one(docs, "AmazonCloudWatchAgent", "cloudwatch-agent-cluster-scraper")
    if (scraper_cr.get("spec") or {}).get("mode") != "deployment":
        raise Phase4Error(f"AmazonCloudWatchAgent/cloudwatch-agent-cluster-scraper spec.mode is {(scraper_cr.get('spec') or {}).get('mode')!r}, expected 'deployment'.")
    if (scraper_cr.get("spec") or {}).get("hostNetwork") is not False:
        raise Phase4Error(f"AmazonCloudWatchAgent/cloudwatch-agent-cluster-scraper spec.hostNetwork is {(scraper_cr.get('spec') or {}).get('hostNetwork')!r}, expected literal false.")
    print("OK: AmazonCloudWatchAgent/cloudwatch-agent-cluster-scraper mode=deployment hostNetwork=False")
    print("OK: rendered CloudWatch Agent custom resources have the expected host-network isolation.")


def cmd_validate_local(args):
    environment = require_environment_arg(args.environment)
    state = load_state(args.state_path)
    values_file = require_state_value(state, "values_file")
    target_namespace = require_state_value(state, "namespace")
    chart_dir_rel = require_state_value(state, "chart_dir")
    digests = require_state_value(state, "image_digests")

    cluster_name = require_env("EKS_CLUSTER_NAME")
    aws_region = require_env("AWS_REGION")
    ecr_registry = require_env("ECR_REGISTRY")

    work_dir = REPO_ROOT / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    generated_values_path = work_dir / "generated-values.yaml"
    _generate_deployment_values(REPO_ROOT / values_file, digests, cluster_name, aws_region, ecr_registry, generated_values_path)
    _validate_generated_values_semantics(generated_values_path, cluster_name, aws_region, ecr_registry)

    chart_dir = REPO_ROOT / chart_dir_rel
    rendered_path = _render_chart(chart_dir, RELEASE_NAME, target_namespace, generated_values_path)
    with rendered_path.open() as f:
        docs = [d for d in yaml.safe_load_all(f) if d]

    _validate_no_unresolved_placeholders(rendered_path)
    _validate_rendered_images(docs, ecr_registry)
    _validate_no_forbidden_components(docs)
    _validate_cloudwatch_agent_service_account(docs, target_namespace)
    _validate_exact_resource_names(docs)
    _validate_rendered_host_network_isolation(docs)

    update_state(args.state_path, {
        "rendered_manifest": str(rendered_path.relative_to(REPO_ROOT)),
        "generated_values_path": str(generated_values_path.relative_to(REPO_ROOT)),
    })
    print("OK: Observability chart rendered and validated locally.")


# 40-sub-observability.yaml: reconcile the Observability Argo CD Application (AWS credentials required, inputs.deploy only)

def _classify_readyz(proc):
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        print("Private EKS API is reachable from the CodeBuild runner.")
        return
    network_pattern = re.compile(r"i/o timeout|connection timed out|no route to host|context deadline exceeded|connection refused", re.IGNORECASE)
    auth_pattern = re.compile(r"Unauthorized|Forbidden|the server has asked for the client to provide credentials|exec credential|AssumeRole")
    if network_pattern.search(output):
        raise Phase4Error("the CodeBuild runner cannot reach the private EKS API endpoint (network-reachability failure, never proof that Argo CD/its CRD is missing).")
    if auth_pattern.search(output):
        raise Phase4Error("the Kubernetes API responded, but EKS authentication or RBAC authorization failed (EKS_DEPLOY_ROLE_ARN aws-auth/IAM trust issue, not a missing CRD).")
    raise Phase4Error(f"the private EKS API connectivity check failed for an unexpected reason (status {proc.returncode}): {output[:800]}")


def _classify_argocd_crd(proc):
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        print("OK: Argo CD Application CRD (applications.argoproj.io) is present.")
        return
    if re.search(r"NotFound|not found", output, re.IGNORECASE):
        raise Phase4Error("the Kubernetes API is reachable, but applications.argoproj.io is genuinely absent. Argo CD prerequisite is not healthy; use 20-sub-argocd.yaml for standalone repair.")
    if re.search(r"Forbidden", output, re.IGNORECASE):
        raise Phase4Error("the deploy identity (EKS_DEPLOY_ROLE_ARN) lacks permission to read CustomResourceDefinitions (RBAC authorization gap, not a missing CRD).")
    raise Phase4Error(f"the Argo CD Application CRD check failed for an unexpected reason (status {proc.returncode}), not a confirmed 'CRD missing' result: {output[:800]}")


def _build_application_manifest(values_text, helm_chart_ref, chart_version, release_name, argocd_app_name, argocd_namespace, observability_namespace):
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {
            "name": argocd_app_name,
            "namespace": argocd_namespace,
            "finalizers": ["resources-finalizer.argocd.argoproj.io"],
            "labels": {"app.kubernetes.io/name": argocd_app_name, "app.kubernetes.io/managed-by": "argocd"},
        },
        "spec": {
            "project": "default",
            "source": {
                "repoURL": helm_chart_ref,
                "targetRevision": chart_version,
                "path": ".",
                "helm": {"releaseName": release_name, "values": values_text},
            },
            "destination": {"server": "https://kubernetes.default.svc", "namespace": observability_namespace},
            "syncPolicy": {
                "automated": {"prune": True, "selfHeal": True},
                "syncOptions": ["CreateNamespace=true", "ServerSideApply=true", "RespectIgnoreDifferences=true"],
                "managedNamespaceMetadata": {"labels": {"app.kubernetes.io/name": argocd_app_name, "app.kubernetes.io/managed-by": "argocd"}},
            },
            "ignoreDifferences": [{
                "group": "", "kind": "ServiceAccount", "name": "cloudwatch-agent", "namespace": observability_namespace,
                "jsonPointers": ["/metadata/annotations/eks.amazonaws.com~1role-arn"],
            }],
            "revisionHistoryLimit": 10,
        },
    }


def _wait_for_argo_application(app_name, namespace, timeout_seconds, interval_seconds):
    elapsed = 0
    while True:
        exists = run(["kubectl", "get", "application", app_name, "-n", namespace], check=False)
        if exists.returncode == 0:
            sync_status = _kubectl_get_jsonpath("application", app_name, namespace, "{.status.sync.status}") or "Unknown"
            health_status = _kubectl_get_jsonpath("application", app_name, namespace, "{.status.health.status}") or "Unknown"
            print(f"sync={sync_status} health={health_status} (elapsed {elapsed}s / {timeout_seconds}s)")
            if health_status == "Degraded":
                run(["kubectl", "get", "application", app_name, "-n", namespace, "-o", "wide"], check=False)
                raise Phase4Error(f"Argo CD Application {app_name} is Degraded.")
            if sync_status == "Synced" and health_status == "Healthy":
                print(f"Argo CD Application {app_name} is Synced and Healthy.")
                return
        else:
            print(f"Argo CD Application {app_name} not found yet (elapsed {elapsed}s / {timeout_seconds}s).")
        if elapsed >= timeout_seconds:
            run(["kubectl", "get", "application", app_name, "-n", namespace, "-o", "wide"], check=False)
            raise Phase4Error(f"timed out after {timeout_seconds}s waiting for {app_name} to become Synced and Healthy.")
        time.sleep(interval_seconds)
        elapsed += interval_seconds


def cmd_reconcile_cluster(args):
    require_environment_arg(args.environment)
    state = load_state(args.state_path)
    target_namespace = require_state_value(state, "namespace")
    generated_values_path = require_state_value(state, "generated_values_path")

    aws_region = require_env("AWS_REGION")
    eks_cluster_name = require_env("EKS_CLUSTER_NAME")
    eks_deploy_role_arn = require_env("EKS_DEPLOY_ROLE_ARN")
    argocd_namespace = require_env("ARGOCD_NAMESPACE")
    ecr_registry = require_env("ECR_REGISTRY")

    run(["aws", "sts", "get-caller-identity"])
    run(["aws", "eks", "update-kubeconfig", "--region", aws_region, "--name", eks_cluster_name,
         "--role-arn", eks_deploy_role_arn, "--assume-role-arn", eks_deploy_role_arn])
    run(["kubectl", "config", "current-context"])

    _classify_readyz(run(["kubectl", "get", "--raw=/readyz", "--request-timeout=20s"], check=False))
    _classify_argocd_crd(run(["kubectl", "get", "crd", "applications.argoproj.io", "--request-timeout=20s"], check=False))

    if run(["kubectl", "get", "secret", ARGOCD_OBSERVABILITY_SECRET_NAME, "-n", argocd_namespace], check=False).returncode != 0:
        raise Phase4Error(f"PREREQUISITE NOT MET: Secret {ARGOCD_OBSERVABILITY_SECRET_NAME} does not exist in namespace {argocd_namespace}. Re-run 20-sub-argocd.yaml first.")
    url_proc = run(["kubectl", "get", "secret", ARGOCD_OBSERVABILITY_SECRET_NAME, "-n", argocd_namespace, "-o", "jsonpath={.data.url}"])
    actual_url = base64.b64decode(url_proc.stdout).decode("utf-8") if url_proc.stdout else ""
    expected_url = f"oci://{ecr_registry}/{HELM_OCI_NAMESPACE}/{CHART_NAME}"
    if actual_url != expected_url:
        raise Phase4Error(f"Secret {ARGOCD_OBSERVABILITY_SECRET_NAME} url mismatch. Expected {expected_url}, got {actual_url}.")
    print(f"OK: {ARGOCD_OBSERVABILITY_SECRET_NAME} exists and points to {expected_url}.")

    values_text = (REPO_ROOT / generated_values_path).read_text()
    helm_chart_ref = f"oci://{ecr_registry}/{HELM_OCI_NAMESPACE}/{CHART_NAME}"
    manifest = _build_application_manifest(values_text, helm_chart_ref, CHART_VERSION, RELEASE_NAME, ARGOCD_APP_NAME, argocd_namespace, target_namespace)
    manifest_yaml = yaml.dump(manifest, default_flow_style=False, sort_keys=False)
    run(["kubectl", "apply", "-f", "-"], input_text=manifest_yaml)
    run(["kubectl", "annotate", "application", ARGOCD_APP_NAME, "-n", argocd_namespace, "argocd.argoproj.io/refresh=hard", "--overwrite"])

    _wait_for_argo_application(ARGOCD_APP_NAME, argocd_namespace, timeout_seconds=900, interval_seconds=15)
    print("OK: Observability Argo CD Application reconciled.")


# 40-sub-observability.yaml: bounded post-deployment validation (AWS credentials required, inputs.deploy only)

def _ensure_cluster_scraper_host_network_isolated(namespace):
    """Preserves the operator-bug workaround exactly: only ever deletes a Deployment it has fully ownership-verified as the current CR's own child, and only when the CR itself already carries hostNetwork=false; recreation is confirmed via UID replacement (never a NotFound interval), with a bounded 180s recreate wait (with a one-time reconciliation nudge at 60s) and a further bounded 180s full-readiness wait. Returns "not_required" or "recreated_once" for the workflow summary."""
    cr_name = "cloudwatch-agent-cluster-scraper"
    deployment_name = "cloudwatch-agent-cluster-scraper"

    cr = _kubectl_get_json("amazoncloudwatchagents.cloudwatch.aws.amazon.com", cr_name, namespace)
    cr_spec = cr.get("spec") or {}
    if cr_spec.get("mode") != "deployment":
        raise Phase4Error(f"CR {cr_name} spec.mode is {cr_spec.get('mode')}, expected deployment.")
    if cr_spec.get("hostNetwork") is not False:
        raise Phase4Error(f"CR {cr_name} spec.hostNetwork is {cr_spec.get('hostNetwork')}, expected false -- refusing to touch the Deployment based on a CR that does not yet carry the corrected value.")
    cr_uid = (cr.get("metadata") or {}).get("uid")
    if not cr_uid:
        raise Phase4Error(f"could not read metadata.uid for CR {cr_name}.")

    deploy = _kubectl_get_json("deployment", deployment_name, namespace, check=False)
    if deploy is None:
        print(f"Deployment/{deployment_name} does not exist yet -- nothing to correct.")
        return "not_required"

    deploy_host_network = ((deploy.get("spec") or {}).get("template") or {}).get("spec", {}).get("hostNetwork", False)
    if deploy_host_network is False:
        print(f"OK: Deployment/{deployment_name} spec.template.spec.hostNetwork is already false -- correction not required.")
        return "not_required"

    print(f"Deployment/{deployment_name} spec.template.spec.hostNetwork is true -- verifying ownership before any deletion.")
    owner_refs = [o for o in ((deploy.get("metadata") or {}).get("ownerReferences") or []) if o.get("controller") is True]
    if len(owner_refs) != 1:
        raise Phase4Error(f"Deployment/{deployment_name} has {len(owner_refs)} controller ownerReferences, expected exactly 1. Refusing to delete.")
    owner = owner_refs[0]
    metadata = deploy.get("metadata") or {}
    problems = []
    if owner.get("kind") != "AmazonCloudWatchAgent":
        problems.append(f"ownerReference.kind is {owner.get('kind')}, expected AmazonCloudWatchAgent.")
    if owner.get("name") != cr_name:
        problems.append(f"ownerReference.name is {owner.get('name')}, expected {cr_name}.")
    if not str(owner.get("apiVersion", "")).startswith("cloudwatch.aws.amazon.com/"):
        problems.append(f"ownerReference.apiVersion is {owner.get('apiVersion')}, expected the cloudwatch.aws.amazon.com API group.")
    if metadata.get("namespace") != namespace:
        problems.append(f"Deployment namespace is {metadata.get('namespace')}, expected {namespace}.")
    if metadata.get("name") != deployment_name:
        problems.append(f"Deployment name is {metadata.get('name')}, expected {deployment_name}.")
    if owner.get("uid") != cr_uid:
        problems.append("ownerReference.uid does not match the live CR metadata.uid.")
    if problems:
        raise Phase4Error("ownership validation failed -- never deleting an unowned, differently owned, or ambiguously owned Deployment: " + "; ".join(problems))

    old_uid = metadata.get("uid")
    print(f"Deleting deployment/{deployment_name} in {namespace} (bounded, idempotent corrective recreation).")
    run(["kubectl", "delete", "deployment", deployment_name, "-n", namespace, "--wait=true", "--timeout=60s"])

    recreate_timeout, nudge_after, recreate_elapsed, nudged = 180, 60, 0, False
    while True:
        new_deploy = _kubectl_get_json("deployment", deployment_name, namespace, check=False)
        if new_deploy is not None:
            new_uid = (new_deploy.get("metadata") or {}).get("uid")
            if new_uid and new_uid != old_uid:
                print(f"OK: Deployment/{deployment_name} was recreated by the operator (new UID differs from the deleted one).")
                break
        if recreate_elapsed >= recreate_timeout:
            raise Phase4Error(f"operator did not recreate deployment/{deployment_name} with a new UID within {recreate_timeout}s.")
        if not nudged and recreate_elapsed >= nudge_after:
            print(f"Operator has not recreated the Deployment after {nudge_after}s -- triggering a harmless reconciliation nudge.")
            run(["kubectl", "annotate", "amazoncloudwatchagents.cloudwatch.aws.amazon.com", cr_name, "-n", namespace,
                 f"cloudfactory.adcb/reconcile-requested-at={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}", "--overwrite"], check=False)
            nudged = True
        time.sleep(10)
        recreate_elapsed += 10

    validate_timeout, validate_elapsed = 180, 0
    while True:
        deploy_json = _kubectl_get_json("deployment", deployment_name, namespace)
        d_metadata = deploy_json.get("metadata") or {}
        d_status = deploy_json.get("status") or {}
        d_spec = deploy_json.get("spec") or {}
        d_hostnetwork = ((d_spec.get("template") or {}).get("spec") or {}).get("hostNetwork", False)
        if d_metadata.get("uid") == old_uid:
            raise Phase4Error("Deployment UID did not change -- this is not actually a recreated object.")
        if d_metadata.get("deletionTimestamp"):
            raise Phase4Error("recreated Deployment already has a deletionTimestamp.")
        if d_hostnetwork is not False:
            raise Phase4Error(f"recreated Deployment spec.template.spec.hostNetwork is {d_hostnetwork}, expected false -- not deleting a second time.")

        desired = d_spec.get("replicas") or 0
        updated = d_status.get("updatedReplicas") or 0
        available = d_status.get("availableReplicas") or 0
        unavailable = d_status.get("unavailableReplicas") or 0
        if d_metadata.get("generation") == d_status.get("observedGeneration") and desired > 0 and updated == desired and available == desired and unavailable == 0:
            print(f"OK: recreated Deployment/{deployment_name} is fully ready and available (hostNetwork=false, {available}/{desired}).")
            break
        if validate_elapsed >= validate_timeout:
            raise Phase4Error(f"recreated Deployment/{deployment_name} did not become fully ready within {validate_timeout}s.")
        time.sleep(10)
        validate_elapsed += 10

    return "recreated_once"


def _validate_active_cluster_scraper_pods_pre_irsa(namespace):
    """Pre-IRSA active-pod safety proof, restored from the old workflow: runs immediately after the host-network correction and strictly BEFORE the ServiceAccount IRSA annotation/rollout-restart, so it intentionally never inspects AWS_ROLE_ARN/AWS_WEB_IDENTITY_TOKEN_FILE -- those only become meaningful once the annotation exists and pods have rolled. Uses the canonical current-revision pod resolver, so a stale ReplicaSet's pod can never satisfy this gate. Requires at least one current-revision, non-terminating, Running, Ready=True cluster-scraper pod, and validates hostNetwork/podIP/hostIP/serviceAccountName on every one of them."""
    current_pods = _current_deployment_pods(namespace, "cloudwatch-agent-cluster-scraper", running_only=True, ready_only=True)
    if not current_pods:
        raise Phase4Error("no current-revision, Running, Ready cloudwatch-agent-cluster-scraper pod found before IRSA annotation -- refusing to proceed.")

    for pod in current_pods:
        pod_name = (pod.get("metadata") or {}).get("name")
        spec = pod.get("spec") or {}
        status = pod.get("status") or {}
        if spec.get("hostNetwork") is not False:
            raise Phase4Error(f"cluster-scraper pod {pod_name} spec.hostNetwork is {spec.get('hostNetwork')!r}, expected literal false.")
        pod_ip = status.get("podIP")
        host_ip = status.get("hostIP")
        if not pod_ip:
            raise Phase4Error(f"cluster-scraper pod {pod_name} has an empty podIP.")
        if not host_ip:
            raise Phase4Error(f"cluster-scraper pod {pod_name} has an empty hostIP.")
        if pod_ip == host_ip:
            raise Phase4Error(f"cluster-scraper pod {pod_name} podIP equals hostIP ({pod_ip}) -- host networking leaked through.")
        if spec.get("serviceAccountName") != CLOUDWATCH_AGENT_SERVICE_ACCOUNT:
            raise Phase4Error(f"cluster-scraper pod {pod_name} uses serviceAccountName={spec.get('serviceAccountName')!r}, expected {CLOUDWATCH_AGENT_SERVICE_ACCOUNT!r}.")
        print(f"OK: cluster-scraper pod {pod_name} -- current revision, Running, Ready, hostNetwork=false, podIP != hostIP, serviceAccountName correct.")
    print(f"OK: {len(current_pods)} active current-revision cluster-scraper pod(s) pass pre-IRSA safety validation.")


def _annotate_cloudwatch_agent_service_account_and_restart(namespace, cloudwatch_metrics_role_arn):
    run(["kubectl", "get", "serviceaccount", CLOUDWATCH_AGENT_SERVICE_ACCOUNT, "-n", namespace])
    run(["kubectl", "annotate", "serviceaccount", CLOUDWATCH_AGENT_SERVICE_ACCOUNT, "-n", namespace,
         f"eks.amazonaws.com/role-arn={cloudwatch_metrics_role_arn}", "--overwrite"])
    actual = _kubectl_get_jsonpath("serviceaccount", CLOUDWATCH_AGENT_SERVICE_ACCOUNT, namespace, r"{.metadata.annotations.eks\.amazonaws\.com/role-arn}") or ""
    if actual != cloudwatch_metrics_role_arn:
        raise Phase4Error(f"ServiceAccount {CLOUDWATCH_AGENT_SERVICE_ACCOUNT} role-arn annotation is {actual}, expected {cloudwatch_metrics_role_arn}.")
    print(f"OK: ServiceAccount {namespace}/{CLOUDWATCH_AGENT_SERVICE_ACCOUNT} annotated with {cloudwatch_metrics_role_arn}.")

    if run(["kubectl", "get", "daemonset", "cloudwatch-agent", "-n", namespace], check=False).returncode == 0:
        run(["kubectl", "rollout", "restart", "daemonset/cloudwatch-agent", "-n", namespace])
    if run(["kubectl", "get", "deployment", "cloudwatch-agent-cluster-scraper", "-n", namespace], check=False).returncode == 0:
        run(["kubectl", "rollout", "restart", "deployment/cloudwatch-agent-cluster-scraper", "-n", namespace])


def _wait_for_daemonset_fully_ready(namespace, name, timeout_seconds):
    elapsed = 0
    while True:
        ds = _kubectl_get_json("daemonset", name, namespace)
        status = ds.get("status") or {}
        metadata = ds.get("metadata") or {}
        desired = status.get("desiredNumberScheduled") or 0
        fields = ("currentNumberScheduled", "updatedNumberScheduled", "numberReady", "numberAvailable")
        if metadata.get("generation") == status.get("observedGeneration") and desired > 0 and all(status.get(f) == desired for f in fields) and (status.get("numberUnavailable") or 0) == 0:
            print(f"OK: {name} is fully ready ({status.get('numberReady')}/{desired}).")
            return
        if elapsed >= timeout_seconds:
            raise Phase4Error(f"{name} did not reach full readiness before the bounded {timeout_seconds}s timeout.")
        time.sleep(10)
        elapsed += 10


def _wait_for_cloudwatch_agent_workloads(namespace):
    if run(["kubectl", "get", "daemonset", "cloudwatch-agent", "-n", namespace], check=False).returncode == 0:
        run(["kubectl", "rollout", "status", "daemonset/cloudwatch-agent", "-n", namespace, "--timeout=10m"])
        _wait_for_daemonset_fully_ready(namespace, "cloudwatch-agent", 600)
    if run(["kubectl", "get", "deployment", "cloudwatch-agent-cluster-scraper", "-n", namespace], check=False).returncode == 0:
        run(["kubectl", "rollout", "status", "deployment/cloudwatch-agent-cluster-scraper", "-n", namespace, "--timeout=10m"])
    run(["kubectl", "rollout", "status", "deployment/amazon-cloudwatch-observability-controller-manager", "-n", namespace, "--timeout=10m"])
    run(["kubectl", "rollout", "status", "deployment/kube-state-metrics", "-n", namespace, "--timeout=10m"])
    run(["kubectl", "rollout", "status", "daemonset/node-exporter", "-n", namespace, "--timeout=10m"])
    _wait_for_daemonset_fully_ready(namespace, "node-exporter", 600)


def _verify_pod_irsa(workload_label, pod_json):
    pod_name = (pod_json.get("metadata") or {}).get("name")
    status = pod_json.get("status") or {}
    if status.get("phase") != "Running":
        raise Phase4Error(f"{workload_label} pod {pod_name} is in phase {status.get('phase')}, expected Running.")
    ready = next((c.get("status") for c in (status.get("conditions") or []) if c.get("type") == "Ready"), "Unknown")
    if ready != "True":
        raise Phase4Error(f"{workload_label} pod {pod_name} Ready condition is {ready}, expected True.")
    pod_sa = (pod_json.get("spec") or {}).get("serviceAccountName")
    if pod_sa != CLOUDWATCH_AGENT_SERVICE_ACCOUNT:
        raise Phase4Error(f"{workload_label} pod {pod_name} uses serviceAccountName={pod_sa}, expected {CLOUDWATCH_AGENT_SERVICE_ACCOUNT}.")
    env_names = {e.get("name") for c in ((pod_json.get("spec") or {}).get("containers") or []) for e in (c.get("env") or [])}
    for required_var in ("AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE"):
        if required_var not in env_names:
            raise Phase4Error(f"{workload_label} pod {pod_name} is missing the {required_var} environment variable.")
    print(f"OK: {workload_label} pod {pod_name} -- phase=Running, Ready=True, serviceAccountName correct, IRSA env var names present (values not printed).")


def _verify_irsa_injection(namespace):
    ds = _kubectl_get_json("daemonset", "cloudwatch-agent", namespace)
    desired = (ds.get("status") or {}).get("desiredNumberScheduled") or 0
    selector = _label_selector(ds)
    if not selector or desired == 0:
        raise Phase4Error("could not derive a pod selector or desiredNumberScheduled for DaemonSet cloudwatch-agent.")
    pods = _pods_for_selector(namespace, selector)
    if len(pods) != desired:
        raise Phase4Error(f"found {len(pods)} pods for DaemonSet cloudwatch-agent, expected exactly {desired}.")
    for pod in pods:
        _verify_pod_irsa("cloudwatch-agent DaemonSet", pod)

    # Current-revision only: cloudwatch-agent-cluster-scraper's Deployment is recreated in place by _ensure_cluster_scraper_host_network_isolated(), so a stale ReplicaSet's pod can still be Running/Ready for a time after a new revision is live -- a stale pod's IRSA env vars must never certify the current Deployment.
    scraper_pods = _current_deployment_pods(namespace, "cloudwatch-agent-cluster-scraper", running_only=True, ready_only=True)
    if not scraper_pods:
        raise Phase4Error("no active current-revision cloudwatch-agent-cluster-scraper pod found for IRSA verification.")
    for pod in scraper_pods:
        _verify_pod_irsa("cloudwatch-agent-cluster-scraper Deployment", pod)
    print("OK: IRSA injection verified on every cloudwatch-agent DaemonSet pod and every current-revision cluster-scraper pod.")


_AUTH_ERROR_PATTERN = re.compile(r"PermissionDenied|HTTP Status Code 403|not authorized to perform: cloudwatch:PutMetricData|no identity-based policy allows|Exporting failed\. Dropping data\.|error exporting items|resource: arn:aws:cloudwatch:|dataset/default", re.IGNORECASE)
_STARTUP_ERROR_PATTERN = re.compile(r"binding address localhost:8888|listen tcp 127\.0\.0\.1:8888|bind: address already in use|failed to create SDK", re.IGNORECASE)


def _check_pod_logs_for_errors(pod_name, namespace, containers, since_time):
    any_error = False
    for container_name in containers:
        proc = run(["kubectl", "logs", pod_name, "-n", namespace, "-c", container_name, f"--since-time={since_time}", "--tail=80"], check=False)
        if proc.returncode != 0:
            raise Phase4Error(f"could not retrieve logs for pod {pod_name} container {container_name} (kubectl logs exited {proc.returncode}).")
        if _AUTH_ERROR_PATTERN.search(proc.stdout):
            print(f"FAIL-CANDIDATE: pod {pod_name} container {container_name} emitted a new CloudWatch metrics authorization/export error since {since_time}.")
            any_error = True
        if _STARTUP_ERROR_PATTERN.search(proc.stdout):
            print(f"FAIL-CANDIDATE: pod {pod_name} container {container_name} emitted a new port-collision/startup error since {since_time}.")
            any_error = True
    return any_error


def _validate_no_recent_cloudwatch_export_errors(namespace):
    """Preserves the exact 90-second bounded observation window: sleeps first, then inspects only ACTIVE current-revision pod logs since the observation start timestamp -- never historical/stale pod logs."""
    observation_period_seconds = 90
    validation_start_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"Observing active CloudWatch Agent pod logs for {observation_period_seconds}s starting at {validation_start_ts}.")
    time.sleep(observation_period_seconds)

    any_error = False
    ds = _kubectl_get_json("daemonset", "cloudwatch-agent", namespace)
    ds_uid = (ds.get("metadata") or {}).get("uid")
    desired = (ds.get("status") or {}).get("desiredNumberScheduled") or 0
    checked = 0
    for pod in _pods_for_selector(namespace, _label_selector(ds)):
        if (pod.get("metadata") or {}).get("deletionTimestamp"):
            continue
        owner_uid = next((o.get("uid") for o in ((pod.get("metadata") or {}).get("ownerReferences") or []) if o.get("controller") and o.get("kind") == "DaemonSet"), None)
        if owner_uid != ds_uid:
            continue
        containers = [c.get("name") for c in ((pod.get("spec") or {}).get("containers") or [])]
        if _check_pod_logs_for_errors((pod.get("metadata") or {}).get("name"), namespace, containers, validation_start_ts):
            any_error = True
        checked += 1
    if checked != desired:
        raise Phase4Error(f"checked {checked} active node-agent pods, expected exactly desiredNumberScheduled={desired}.")

    # Current-revision only (mirrors the DaemonSet owner-UID filtering above): a stale ReplicaSet's scraper pod must never be inspected here -- its logs belong to a superseded revision, not the current deployment under observation.
    checked_scraper = 0
    for pod in _current_deployment_pods(namespace, "cloudwatch-agent-cluster-scraper"):
        containers = [c.get("name") for c in ((pod.get("spec") or {}).get("containers") or [])]
        if _check_pod_logs_for_errors((pod.get("metadata") or {}).get("name"), namespace, containers, validation_start_ts):
            any_error = True
        checked_scraper += 1
    if checked_scraper < 1:
        raise Phase4Error("checked 0 active cluster-scraper pods, expected at least 1.")

    if any_error:
        raise Phase4Error(f"at least one active, current-revision CloudWatch Agent pod emitted a new authorization or port-collision error since {validation_start_ts}.")
    print(f"OK: no active, current-revision CloudWatch Agent pod emitted a new authorization or port-collision error in the {observation_period_seconds}s observation window.")


def _live_kubernetes_validation(namespace, cloudwatch_metrics_role_arn, ecr_registry):
    run(["kubectl", "get", "namespace", namespace])

    actual = _kubectl_get_jsonpath("serviceaccount", CLOUDWATCH_AGENT_SERVICE_ACCOUNT, namespace, r"{.metadata.annotations.eks\.amazonaws\.com/role-arn}") or ""
    if actual != cloudwatch_metrics_role_arn:
        raise Phase4Error(f"unexpected role-arn annotation: {actual}")

    run(["kubectl", "wait", "--for=condition=Available", "deployment/amazon-cloudwatch-observability-controller-manager", "-n", namespace, "--timeout=60s"])

    cw_ds = _kubectl_get_json("daemonset", "cloudwatch-agent", namespace)
    cw_status = cw_ds.get("status") or {}
    desired, ready, available = cw_status.get("desiredNumberScheduled") or 0, cw_status.get("numberReady") or 0, cw_status.get("numberAvailable") or 0
    if desired == 0 or ready != desired or available != desired:
        raise Phase4Error(f"cloudwatch-agent DaemonSet is not fully ready and available (desired={desired} ready={ready} available={available}).")

    run(["kubectl", "wait", "--for=condition=Available", "deployment/cloudwatch-agent-cluster-scraper", "-n", namespace, "--timeout=60s"])
    run(["kubectl", "wait", "--for=condition=Available", "deployment/kube-state-metrics", "-n", namespace, "--timeout=60s"])

    ne_ds = _kubectl_get_json("daemonset", "node-exporter", namespace)
    ne_status = ne_ds.get("status") or {}
    ne_desired, ne_ready, ne_available = ne_status.get("desiredNumberScheduled") or 0, ne_status.get("numberReady") or 0, ne_status.get("numberAvailable") or 0
    if ne_desired == 0 or ne_ready != ne_desired or ne_available != ne_desired:
        raise Phase4Error(f"node-exporter DaemonSet is not fully ready and available (desired={ne_desired} ready={ne_ready} available={ne_available}).")

    proc = run(["kubectl", "get", "pods", "-n", namespace, "-o",
                "jsonpath={range .items[*]}{range .spec.containers[*]}{.image}{\"\\n\"}{end}{range .spec.initContainers[*]}{.image}{\"\\n\"}{end}{end}"])
    running_images = sorted(set(filter(None, proc.stdout.splitlines())))
    for image in running_images:
        if not image.startswith(f"{ecr_registry}/") or "@sha256:" not in image:
            raise Phase4Error(f"running image {image} is not private/digest-pinned.")
        repo = image[len(ecr_registry) + 1:].split(":")[0]
        if repo not in ALLOWED_IMAGE_REPOS:
            raise Phase4Error(f"running image {image} belongs to an unapproved repository {repo}.")
    print(f"OK: every running image in {namespace} is private, allow-listed, and digest-pinned.")

    ds_names_proc = run(["kubectl", "get", "daemonset", "-n", namespace, "-o", "name"], check=False)
    if any("fluent" in line.lower() for line in ds_names_proc.stdout.splitlines()):
        raise Phase4Error(f"a Fluent Bit-like DaemonSet exists in {namespace}.")

    for resource in ("instrumentations.cloudwatch.aws.amazon.com", "dcgmexporters.cloudwatch.aws.amazon.com", "neuronmonitors.cloudwatch.aws.amazon.com"):
        proc = run(["kubectl", "get", resource, "-n", namespace, "--no-headers"], check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            raise Phase4Error(f"a forbidden {resource} resource exists in {namespace}.")

    pods_proc = run(["kubectl", "get", "pods", "-n", namespace, "-o", "name"], check=False)
    if any("target-allocator" in line.lower() for line in pods_proc.stdout.splitlines()):
        raise Phase4Error("a target-allocator pod exists.")

    cr_names_proc = run(["kubectl", "get", "amazoncloudwatchagents.cloudwatch.aws.amazon.com", "-n", namespace, "-o", "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}"])
    cr_names = [n for n in cr_names_proc.stdout.splitlines() if n]
    if not cr_names:
        raise Phase4Error(f"no AmazonCloudWatchAgent resource found in {namespace}.")
    for cr_name in cr_names:
        otel_config = _kubectl_get_jsonpath("amazoncloudwatchagents.cloudwatch.aws.amazon.com", cr_name, namespace, "{.spec.otelConfig}") or ""
        if "filelog" in otel_config.lower():
            raise Phase4Error(f"AmazonCloudWatchAgent/{cr_name} otelConfig contains a filelog receiver.")

    cw_cr = _kubectl_get_json("amazoncloudwatchagents.cloudwatch.aws.amazon.com", "cloudwatch-agent", namespace)
    if (cw_cr.get("spec") or {}).get("hostNetwork") is not True:
        raise Phase4Error("AmazonCloudWatchAgent/cloudwatch-agent spec.hostNetwork is not true.")
    scraper_cr = _kubectl_get_json("amazoncloudwatchagents.cloudwatch.aws.amazon.com", "cloudwatch-agent-cluster-scraper", namespace)
    if (scraper_cr.get("spec") or {}).get("hostNetwork") is not False:
        raise Phase4Error("AmazonCloudWatchAgent/cloudwatch-agent-cluster-scraper spec.hostNetwork is not false.")

    cw_ds_hostnetwork = ((cw_ds.get("spec") or {}).get("template") or {}).get("spec", {}).get("hostNetwork", False)
    if cw_ds_hostnetwork is not True:
        raise Phase4Error("DaemonSet/cloudwatch-agent spec.template.spec.hostNetwork is not true.")
    scraper_deploy = _kubectl_get_json("deployment", "cloudwatch-agent-cluster-scraper", namespace)
    scraper_hostnetwork = ((scraper_deploy.get("spec") or {}).get("template") or {}).get("spec", {}).get("hostNetwork", False)
    if scraper_hostnetwork not in (False, None):
        raise Phase4Error("Deployment/cloudwatch-agent-cluster-scraper spec.template.spec.hostNetwork is not false.")

    for pod in _pods_for_selector(namespace, _label_selector(cw_ds)):
        if (pod.get("spec") or {}).get("hostNetwork") is not True:
            raise Phase4Error(f"node-agent pod {(pod.get('metadata') or {}).get('name')} spec.hostNetwork is not true.")
    running_scraper_pods = [p for p in _pods_for_selector(namespace, _label_selector(scraper_deploy)) if (p.get("status") or {}).get("phase") == "Running"]
    if not running_scraper_pods:
        raise Phase4Error("no active (Running) cluster-scraper pods found.")
    for pod in running_scraper_pods:
        if (pod.get("spec") or {}).get("hostNetwork") is not False:
            raise Phase4Error(f"cluster-scraper pod {(pod.get('metadata') or {}).get('name')} spec.hostNetwork is not false.")

    # Current-revision only for the scraper side of this final bounded log scan: a stale ReplicaSet's pod (still Running for a time after a new revision is live) must never be able to fail the current deployment on its own historical crash symptom.
    crash_pattern = re.compile(r"bind: address already in use|binding address localhost:8888|listen tcp 127\.0\.0\.1:8888|failed to create SDK", re.IGNORECASE)
    current_scraper_pods_for_logs = _current_deployment_pods(namespace, "cloudwatch-agent-cluster-scraper", running_only=True)
    for pod in _pods_for_selector(namespace, _label_selector(cw_ds)) + current_scraper_pods_for_logs:
        if (pod.get("metadata") or {}).get("deletionTimestamp"):
            continue
        proc = run(["kubectl", "logs", (pod.get("metadata") or {}).get("name"), "-n", namespace, "--tail=80"], check=False)
        if crash_pattern.search(proc.stdout or ""):
            raise Phase4Error(f"pod {(pod.get('metadata') or {}).get('name')} log shows the previously observed 127.0.0.1:8888 bind collision.")

    print("OK: live Kubernetes validation passed (namespace, SA annotation, workload readiness, image provenance, forbidden-component absence, host-network isolation, no port-collision symptom).")


def cmd_post_deploy_validation(args):
    require_environment_arg(args.environment)
    state = load_state(args.state_path)
    namespace = require_state_value(state, "namespace")
    cloudwatch_metrics_role_arn = require_env("CLOUDWATCH_METRICS_ROLE_ARN")
    ecr_registry = require_env("ECR_REGISTRY")

    correction_result = _ensure_cluster_scraper_host_network_isolated(namespace)
    update_state(args.state_path, {"cluster_scraper_correction": correction_result})

    _validate_active_cluster_scraper_pods_pre_irsa(namespace)

    _annotate_cloudwatch_agent_service_account_and_restart(namespace, cloudwatch_metrics_role_arn)
    _wait_for_cloudwatch_agent_workloads(namespace)
    _verify_irsa_injection(namespace)
    _validate_no_recent_cloudwatch_export_errors(namespace)
    _live_kubernetes_validation(namespace, cloudwatch_metrics_role_arn, ecr_registry)
    print("OK: bounded post-deployment validation passed.")


# 40-sub-observability.yaml: non-authoritative bounded diagnostics (always() && inputs.deploy)

def cmd_diagnostics(args):
    """Evidence-only: every command is individually guarded so this step always finishes successfully and never masks an earlier deployment failure. Bounded (--tail/--since) and pattern-matched only -- never a full/unbounded log dump, never credentials/tokens/env values."""
    state = load_state(args.state_path)
    namespace = state.get("namespace")
    if not namespace:
        print("No Phase 4 Observability namespace recorded in state -- skipping bounded diagnostics.")
        return
    error_patterns = re.compile(r"AccessDenied|UnauthorizedOperation|NoCredentialProviders|WebIdentityErr|AssumeRoleWithWebIdentity|PutMetricData|PutLogEvents|ImagePullBackOff|ErrImagePull|CrashLoopBackOff|[Rr]eadiness probe failed|connection refused|[Tt]imeout|OOMKilled|x509|webhook.*certificate", re.IGNORECASE)

    def _tail_and_scan(label, pod_name):
        if not pod_name:
            print(f"{label}: pod not found.")
            return
        proc = run(["kubectl", "logs", pod_name, "-n", namespace, "--tail=80", "--since=15m"], check=False)
        matches = [line for line in (proc.stdout or "").splitlines() if error_patterns.search(line)]
        print(f"{label} ({pod_name}): " + ("\n".join(matches) if matches else "No matching error patterns found."))

    try:
        proc = run(["kubectl", "get", "pods", "-n", namespace, "-l", "control-plane=controller-manager", "-o", "jsonpath={.items[0].metadata.name}"], check=False)
        _tail_and_scan("Operator logs", proc.stdout.strip() if proc.returncode == 0 else None)
    except Exception as exc:
        print(f"Operator log diagnostic skipped: {exc}")

    try:
        ds = _kubectl_get_json("daemonset", "cloudwatch-agent", namespace, check=False)
        if ds:
            for pod in _pods_for_selector(namespace, _label_selector(ds)):
                _tail_and_scan("CloudWatch Agent node-agent pod logs", (pod.get("metadata") or {}).get("name"))
    except Exception as exc:
        print(f"CloudWatch Agent log diagnostic skipped: {exc}")

    try:
        scraper_deploy = _kubectl_get_json("deployment", "cloudwatch-agent-cluster-scraper", namespace, check=False)
        if scraper_deploy:
            pods = _pods_for_selector(namespace, _label_selector(scraper_deploy))
            _tail_and_scan("cloudwatch-agent-cluster-scraper logs", (pods[0].get("metadata") or {}).get("name") if pods else None)
    except Exception as exc:
        print(f"Cluster-scraper log diagnostic skipped: {exc}")

    print("NOTE: healthy pods and absent error patterns above do NOT confirm CloudWatch metrics are actually visible in the console/API.")


# 40-sub-observability.yaml: workflow summary (always(), no AWS credentials, tolerant of partial state)

def cmd_summary(args):
    environment = args.environment
    state = load_state(args.state_path)
    namespace = state.get("namespace", "unknown")
    digests = state.get("image_digests") or {}
    deploy_requested = os.environ.get("OBSERVABILITY_DEPLOY_REQUESTED", "")

    lines = [
        "## GoldenGate observability (CloudWatch metrics) deploy summary",
        "",
        f"- Environment: `{environment}`",
        f"- Deploy requested: `{deploy_requested or 'unknown'}`",
        f"- Chart: `{HELM_OCI_NAMESPACE}/{CHART_NAME}:{CHART_VERSION}`",
        f"- Argo CD Application: `{ARGOCD_APP_NAME}`",
        f"- Release: `{RELEASE_NAME}` -> namespace `{namespace}`",
        "",
        "### Private images (verified in this run)",
        "",
        "| Repository | Tag |",
        "|---|---|",
    ]
    for repo, tag in IMAGE_TABLE:
        entry = digests.get(repo) or {}
        lines.append(f"| `{repo}` | `{entry.get('tag', tag)}` |")
    lines.append("")

    if deploy_requested == "true":
        correction = state.get("cluster_scraper_correction", "unknown")
        lines += [
            "### Cluster-scraper Deployment host-network correction",
            "",
            f"Correction: `{correction}`.",
            "",
        ]

    lines += [
        "### Explicitly out of scope for this phase",
        "",
        "- CloudWatch alarms, SNS, dashboards",
        "- GoldenGate replication",
        "- Fluent Bit changes (gg-fluent-bit remains the sole log collector)",
        "- CloudWatch data-plane confirmation",
        "",
    ]

    if deploy_requested and deploy_requested != "true":
        lines += [
            "### Validation-only run",
            "",
            "deploy=false: no Argo CD Application was created or updated, and no live Kubernetes validation was performed.",
            "",
        ]

    write_step_summary("\n".join(lines))
    print("OK: wrote Observability workflow summary.")


# CLI wiring

_SUBCOMMANDS = {
    "ensure-tools": cmd_ensure_tools,
    "ownership-preflight": cmd_ownership_preflight,
    "prepare": cmd_prepare,
    "resolve-private-artifacts": cmd_resolve_private_artifacts,
    "validate-local": cmd_validate_local,
    "reconcile-cluster": cmd_reconcile_cluster,
    "post-deploy-validation": cmd_post_deploy_validation,
    "diagnostics": cmd_diagnostics,
    "strict-acceptance": cmd_strict_acceptance,
    "summary": cmd_summary,
}

_ENVIRONMENT_SUBCOMMANDS = (
    "ownership-preflight", "prepare", "resolve-private-artifacts", "validate-local",
    "reconcile-cluster", "post-deploy-validation", "diagnostics", "strict-acceptance", "summary",
)


def build_parser():
    parser = argparse.ArgumentParser(description="Phase 4 | Observability orchestrator (ownership preflight, private-artifact resolution, local validation, Argo reconciliation, strict acceptance).")
    parser.add_argument("--state-file", type=Path, default=None, help="Override the Phase 4 Observability state file path (default: $RUNNER_TEMP/goldengate-phase4-observability-state.json).")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("ensure-tools")
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
    except Phase4Error as exc:
        print(f"FAIL: {exc}")
        return 1
    except subprocess.SubprocessError as exc:
        print(f"FAIL: subprocess execution error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
