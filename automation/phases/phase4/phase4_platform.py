#!/usr/bin/env python3
"""Phase 4A-4C | GoldenGate Platform orchestration entrypoint for the platform_preflight/platform_sync_once/validate_platform_ready jobs in .github/workflows/00-main-goldengate-orchestrator.yaml and the package_publish_and_deploy job in .github/workflows/30-sub-platform.yaml; a thin orchestration/service layer that never reimplements environment.yaml parsing (owned by automation/goldengate-environment.py) and reuses, never duplicates, automation/phases/phase4/platform_state.py (pre-reconciliation ownership-safety preflight) and automation/phases/phase4/platform_acceptance.py (strict post-reconciliation acceptance, including its own FLUENT_BIT_IMAGE format validator, reused here rather than reimplemented) as separate subprocess-invoked classifiers. Non-secret Helm/Platform deployment metadata is threaded between the 30-sub-platform.yaml subcommands through a JSON state file under the runner temp directory instead of large inline shell blocks; AWS credentials are never written to that state file, to $GITHUB_OUTPUT, or to $GITHUB_ENV."""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

ENVIRONMENT_TOOL = REPO_ROOT / "automation" / "goldengate-environment.py"
PLATFORM_STATE_TOOL = REPO_ROOT / "automation" / "phases" / "phase4" / "platform_state.py"
PLATFORM_ACCEPTANCE_TOOL = REPO_ROOT / "automation" / "phases" / "phase4" / "platform_acceptance.py"

# Mirrors automation/phases/phase3/phase3_argocd.py's own _SAFE_TOKEN_RE -- each tool in this repository intentionally keeps its own local copy of this grammar rather than importing it across modules; used here only for defense-in-depth path-safety before an environment name is ever interpolated into a filesystem path, never as the canonical acceptance/rejection of an environment (that remains automation/goldengate-environment.py's own concern).
_SAFE_TOKEN_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*\Z")

# Phase 4 Platform constants (helm/goldengate-platform, .github/workflows/30-sub-platform.yaml) -- moved verbatim from the former workflow's top-level env: block, never re-derived.
HELM_OCI_NAMESPACE = "helm"
CHART_NAME = "goldengate-platform"
HELM_CHART_PATH = "helm/goldengate-platform"
ARGOCD_PLATFORM_SECRET_NAME = "argocd-ecr-goldengate-platform-oci"
RUNTIME_SA_NAME = "gg-runtime-sa"
FLUENT_BIT_SA_NAME = "gg-fluent-bit"
FLUENT_BIT_ECR_REPOSITORY_EXPECTED = "aws-cloud-factory-fluent-bit"
FLUENT_BIT_DAEMONSET_NAME = "gg-fluent-bit"
FLUENT_BIT_CONFIGMAP_NAME = "gg-fluent-bit-config"

ARGOCD_ECR_STATEMENT_SID = "AllowArgocdEksRolePullGoldengatePlatformHelmChart"
REPOSITORY_PULL_ACTIONS = [
    "ecr:BatchCheckLayerAvailability",
    "ecr:BatchGetImage",
    "ecr:GetDownloadUrlForLayer",
    "ecr:DescribeImages",
    "ecr:DescribeRepositories",
]

FORBIDDEN_RENDERED_KINDS = ("StatefulSet", "Deployment", "Service", "Ingress", "PersistentVolumeClaim", "SecretProviderClass")

# Non-secret Phase 4 Platform deployment-metadata keys only -- update_state() fails closed on any other key, so an AWS/ECR/Kubernetes credential can never be written to the state file even by an accidental future call site.
ALLOWED_STATE_KEYS = frozenset({
    "environment", "values_file", "chart_version", "temp_chart_path", "helm_ecr_repository",
    "helm_push_url", "helm_chart_ref", "rendered_manifest", "package_path", "pulled_directory",
    "namespace", "release_name", "argocd_app_name", "fluent_bit_image", "fluent_bit_ecr_repository",
    "fluent_bit_ecr_digest",
})


class Phase4Error(Exception):
    """A fail-closed Phase 4 Platform error; main() reports it and exits non-zero."""


def is_safe_token(value):
    return isinstance(value, str) and bool(_SAFE_TOKEN_RE.match(value))


def require_environment_arg(environment):
    if not is_safe_token(environment):
        raise Phase4Error(f"environment {environment!r} is not a safe identifier; refusing to use it in a filesystem path.")
    return environment


def require_env(name):
    import os
    value = os.environ.get(name, "")
    if not value:
        raise Phase4Error(f"{name} is empty; canonical environment configuration must be loaded before this step.")
    return value


# Phase 4 Platform state file

def default_state_path():
    """${RUNNER_TEMP}/goldengate-phase4-platform-state.json, or a repo-local fallback outside CI."""
    import os
    runner_temp = os.environ.get("RUNNER_TEMP")
    if runner_temp:
        return Path(runner_temp) / "goldengate-phase4-platform-state.json"
    return Path(os.environ.get("TMPDIR", "/tmp")) / "goldengate-phase4-platform-state.json"


def load_state(state_path):
    if not state_path.exists():
        return {}
    try:
        with state_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise Phase4Error(f"Phase 4 Platform state file {state_path} is unreadable/malformed: {exc}") from exc
    if not isinstance(data, dict):
        raise Phase4Error(f"Phase 4 Platform state file {state_path} did not contain a JSON object.")
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
        raise Phase4Error(f"refusing to write disallowed Phase 4 Platform state key(s) {disallowed} -- state may only ever contain non-secret deployment metadata: {sorted(ALLOWED_STATE_KEYS)}")
    state = load_state(state_path)
    state.update(updates)
    save_state(state_path, state)
    return state


def require_state_value(state, key):
    if key not in state or state[key] in (None, ""):
        raise Phase4Error(f"Phase 4 Platform state is missing required key {key!r}; an earlier step did not complete.")
    return state[key]


# GitHub Actions special-file helpers

def write_github_output(pairs, output_path=None):
    """Appends name=value lines to $GITHUB_OUTPUT. Output names/values here are always fixed literals or a program-controlled enum (e.g. ownership state), never caller-controlled free-form text. No-op (never raises) when GITHUB_OUTPUT is unset."""
    import os
    path = output_path if output_path is not None else os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for name, value in pairs:
            f.write(f"{name}={value}\n")


def write_step_summary(text, summary_path=None):
    import os
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
        raise Phase4Error(f"{' '.join(str(a) for a in argv)} failed (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
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
        raise Phase4Error(f"Unsupported architecture for kubectl: {machine}")
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
        raise Phase4Error(f"Unsupported architecture for Helm: {machine}")
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


# automation/phases/phase4/platform_acceptance.py reuse (never a second independent FLUENT_BIT_IMAGE format validator)

_platform_acceptance_module = None


def _load_platform_acceptance_module():
    global _platform_acceptance_module
    if _platform_acceptance_module is None:
        spec = importlib.util.spec_from_file_location("platform_acceptance", PLATFORM_ACCEPTANCE_TOOL)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _platform_acceptance_module = module
    return _platform_acceptance_module


def _validate_fluent_bit_image_format(fluent_bit_image, ecr_registry):
    """Reuses automation/phases/phase4/platform_acceptance.py's own _validate_fluent_bit_image() -- never a second independent format validator for the caller-supplied FLUENT_BIT_IMAGE operational configuration."""
    module = _load_platform_acceptance_module()
    try:
        module._validate_fluent_bit_image(fluent_bit_image, ecr_registry)
    except ValueError as exc:
        raise Phase4Error(str(exc)) from exc


def _derive_fluent_bit_ecr_digest(fluent_bit_image, ecr_registry):
    """The single producer of the fluent_bit_ecr_digest state value cmd_verify_fluent_bit_artifact() later consumes unchanged -- must be called only after _validate_fluent_bit_image_format() has already confirmed fluent_bit_image matches the approved <ECR_REGISTRY>/aws-cloud-factory-fluent-bit@sha256:<64hex> shape. Returns the CANONICAL ECR digest form (sha256:<64hex>) that aws ecr describe-images --image-ids imageDigest=<value> requires -- never the bare hex alone, which fails ECR's own imageDigest regex with InvalidParameterException."""
    expected_prefix = f"{ecr_registry}/{FLUENT_BIT_ECR_REPOSITORY_EXPECTED}@sha256:"
    digest_hex = fluent_bit_image[len(expected_prefix):]
    return f"sha256:{digest_hex}"


# Phase 4A: platform ownership preflight (platform_preflight)

def cmd_ownership_preflight(args):
    environment = require_environment_arg(args.environment)
    aws_region = require_env("AWS_REGION")
    eks_cluster_name = require_env("EKS_CLUSTER_NAME")
    eks_deploy_role_arn = require_env("EKS_DEPLOY_ROLE_ARN")

    run(["aws", "eks", "update-kubeconfig", "--region", aws_region, "--name", eks_cluster_name,
         "--role-arn", eks_deploy_role_arn, "--assume-role-arn", eks_deploy_role_arn])

    proc = run([sys.executable, str(PLATFORM_STATE_TOOL), "--environment", environment], check=False)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    if proc.returncode != 0:
        raise Phase4Error(f"GoldenGate Platform ownership-safety classifier failed (inspection error); refusing to guess ABSENT:\n{proc.stdout}")
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Phase4Error(f"GoldenGate Platform ownership-safety classifier produced unparseable output: {exc}") from exc

    state = result.get("state")
    if state not in ("ABSENT", "OWNED", "BROKEN"):
        raise Phase4Error(f"GoldenGate Platform ownership-safety classifier produced an unrecognized state {state!r}; refusing to proceed.")
    if state == "BROKEN":
        raise Phase4Error("GoldenGate Platform ownership-safety preflight classified the installation as BROKEN; refusing to reconcile. See diagnostics above.")

    write_github_output([("state", state)])
    print(f"OK: GoldenGate Platform ownership-safety preflight state is {state}.")


# Phase 4C: strict post-reconciliation acceptance (validate_platform_ready)

def cmd_strict_acceptance(args):
    environment = require_environment_arg(args.environment)
    aws_region = require_env("AWS_REGION")
    eks_cluster_name = require_env("EKS_CLUSTER_NAME")
    eks_deploy_role_arn = require_env("EKS_DEPLOY_ROLE_ARN")
    fluent_bit_image = require_env("FLUENT_BIT_IMAGE")

    run(["aws", "eks", "update-kubeconfig", "--region", aws_region, "--name", eks_cluster_name,
         "--role-arn", eks_deploy_role_arn, "--assume-role-arn", eks_deploy_role_arn])

    proc = run([sys.executable, str(PLATFORM_ACCEPTANCE_TOOL), "--environment", environment, "--fluent-bit-image", fluent_bit_image], check=False)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    if proc.returncode != 0:
        raise Phase4Error(f"GoldenGate Platform acceptance classifier failed (configuration/inspection error); refusing to guess HEALTHY:\n{proc.stdout}")
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Phase4Error(f"GoldenGate Platform acceptance classifier produced unparseable output: {exc}") from exc

    state = result.get("state")
    if state != "HEALTHY":
        raise Phase4Error(f"GoldenGate Platform acceptance classified the installation as {state!r} (required: HEALTHY); reconciliation success alone is never sufficient. See diagnostics above.")

    print("OK: GoldenGate Platform is HEALTHY (strict post-reconciliation acceptance).")


# 30-sub-platform.yaml: prepare and validate the local Platform deployment (no AWS credentials)

def _validate_required_files(values_file):
    chart_yaml = REPO_ROOT / HELM_CHART_PATH / "Chart.yaml"
    values_yaml = REPO_ROOT / HELM_CHART_PATH / "values.yaml"
    env_values_path = REPO_ROOT / values_file
    for path in (chart_yaml, values_yaml, env_values_path):
        if not path.is_file():
            raise Phase4Error(f"Missing required file: {path}")
    print("Required files are present.")


def _helm_set_string_overrides():
    return [
        "--set-string", f"environment={require_env('GG_ENVIRONMENT')}",
        "--set-string", f"namespaces.runtime.name={require_env('RUNTIME_NAMESPACE')}",
        "--set-string", f"runtimeServiceAccount.roleArn={require_env('RUNTIME_ROLE_ARN')}",
        "--set-string", f"fluentBit.serviceAccount.roleArn={require_env('PLATFORM_LOGGING_ROLE_ARN')}",
        "--set-string", f"fluentBit.aws.region={require_env('AWS_REGION')}",
        "--set-string", f"fluentBit.namespaces.runtime={require_env('RUNTIME_NAMESPACE')}",
        "--set-string", f"fluentBit.namespaces.monitoring={require_env('MONITOR_NAMESPACE')}",
        "--set-string", f"fluentBit.cloudwatch.runtimeLogGroupName={require_env('RUNTIME_LOG_GROUP')}",
        "--set-string", f"fluentBit.cloudwatch.monitorLogGroupName={require_env('MONITOR_LOG_GROUP')}",
        "--set-string", f"fluentBit.image.reference={require_env('FLUENT_BIT_IMAGE')}",
    ]


def _helm_template(values_file, release_name):
    rendered_dir = REPO_ROOT / "rendered"
    rendered_dir.mkdir(parents=True, exist_ok=True)
    rendered_path = rendered_dir / f"{release_name}.yaml"
    proc = run(["helm", "template", release_name, HELM_CHART_PATH, "--values", values_file, *_helm_set_string_overrides()])
    rendered_path.write_text(proc.stdout)
    print(f"Rendered manifest: {rendered_path.relative_to(REPO_ROOT)}")
    return rendered_path


def _parse_documents(rendered_path):
    with rendered_path.open() as f:
        return [d for d in yaml.safe_load_all(f) if d]


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
        raise Phase4Error(f"expected ZERO rendered Namespace documents (namespaces.runtime.create must stay false), found {count} -- namespace metadata ownership belongs exclusively to the Argo CD Application.")
    print("OK: zero Namespace documents rendered -- the Argo CD Application remains the sole namespace-metadata owner.")


def _validate_runtime_service_account(docs, sa_name, expected_role_arn, runtime_namespace, deletion_protected):
    matches = [d for d in docs if d.get("kind") == "ServiceAccount" and (d.get("metadata") or {}).get("name") == sa_name]
    if len(matches) != 1:
        raise Phase4Error(f"expected exactly one rendered ServiceAccount named {sa_name}, found {len(matches)}.")
    sa = matches[0]
    metadata = sa.get("metadata") or {}
    if metadata.get("namespace") != runtime_namespace:
        raise Phase4Error(f"ServiceAccount {sa_name} is not in namespace {runtime_namespace}.")
    annotations = metadata.get("annotations") or {}
    role_arn = annotations.get("eks.amazonaws.com/role-arn")
    if role_arn != expected_role_arn:
        raise Phase4Error(f"ServiceAccount {sa_name} IRSA role ARN mismatch. Expected: {expected_role_arn}. Actual: {role_arn}.")
    print(f"OK: ServiceAccount {sa_name} is in {runtime_namespace} with the expected IRSA role ARN.")
    if deletion_protected:
        sync_options = annotations.get("argocd.argoproj.io/sync-options")
        if sync_options != "Prune=false,Delete=false":
            raise Phase4Error(f"ServiceAccount {sa_name} is missing argocd.argoproj.io/sync-options: Prune=false,Delete=false (got {sync_options!r}).")
        print(f"OK: ServiceAccount {sa_name} carries sync-options: Prune=false,Delete=false (not merely PruneLast=true).")


def _validate_service_account_set(docs):
    names = sorted(d.get("metadata", {}).get("name") for d in docs if d.get("kind") == "ServiceAccount")
    expected = sorted((RUNTIME_SA_NAME, FLUENT_BIT_SA_NAME))
    if names != expected:
        raise Phase4Error(f"rendered ServiceAccount set does not exactly match the expected set. Expected: {expected}. Actual: {names}.")
    print(f"OK: rendered ServiceAccount set is exactly {expected}.")


def _validate_no_unexpected_irsa_role(docs, allowed_role_arns):
    for doc in docs:
        annotations = (doc.get("metadata") or {}).get("annotations") or {}
        role_arn = annotations.get("eks.amazonaws.com/role-arn")
        if role_arn and role_arn not in allowed_role_arns:
            raise Phase4Error(f"unexpected IRSA role introduced by the platform chart: {role_arn}")
    print("OK: every rendered ServiceAccount uses only an approved IRSA role.")


def _validate_no_forbidden_kinds(docs):
    for kind in FORBIDDEN_RENDERED_KINDS:
        if _count_kind(docs, kind) > 0:
            raise Phase4Error(f"a {kind} was rendered by the platform chart -- it must only create shared namespaces, shared runtime ServiceAccounts, and the platform logging DaemonSet.")
    print("OK: no StatefulSet/Deployment/Service/Ingress/PersistentVolumeClaim/SecretProviderClass rendered.")


def _daemonset_container(ds):
    pod_spec = (((ds.get("spec") or {}).get("template") or {}).get("spec")) or {}
    containers = pod_spec.get("containers") or []
    if len(containers) != 1:
        raise Phase4Error(f"daemonset/{FLUENT_BIT_DAEMONSET_NAME} has {len(containers)} container(s), expected exactly 1.")
    return pod_spec, containers[0]


def _validate_fluent_bit_daemonset_shape(docs, fluent_bit_image):
    count = _count_kind(docs, "DaemonSet")
    if count != 1:
        raise Phase4Error(f"expected exactly 1 rendered DaemonSet document, found {count}.")
    ds = _find_document(docs, "DaemonSet", FLUENT_BIT_DAEMONSET_NAME)
    if ds is None:
        raise Phase4Error(f"rendered DaemonSet {FLUENT_BIT_DAEMONSET_NAME} was not found.")
    print(f"OK: exactly 1 DaemonSet document, {FLUENT_BIT_DAEMONSET_NAME}.")

    pod_spec, container = _daemonset_container(ds)
    security_context = container.get("securityContext") or {}
    if security_context.get("privileged") is not False:
        raise Phase4Error(f"{FLUENT_BIT_DAEMONSET_NAME} DaemonSet does not explicitly set privileged: false.")
    print(f"OK: {FLUENT_BIT_DAEMONSET_NAME} DaemonSet is not privileged.")

    if pod_spec.get("hostNetwork") is not False:
        raise Phase4Error(f"{FLUENT_BIT_DAEMONSET_NAME} DaemonSet does not explicitly set hostNetwork: false.")
    print(f"OK: {FLUENT_BIT_DAEMONSET_NAME} DaemonSet does not use host networking.")

    volumes = pod_spec.get("volumes") or []
    varlog_volume = next((v for v in volumes if v.get("name") == "varlog"), None)
    if varlog_volume is None or "hostPath" not in varlog_volume:
        raise Phase4Error(f"{FLUENT_BIT_DAEMONSET_NAME} DaemonSet does not mount a host path for container logs (varlog).")
    mounts = container.get("volumeMounts") or []
    varlog_mount = next((m for m in mounts if m.get("name") == "varlog"), None)
    if varlog_mount is None or varlog_mount.get("readOnly") is not True:
        raise Phase4Error(f"{FLUENT_BIT_DAEMONSET_NAME} DaemonSet's varlog host mount is not read-only.")
    print(f"OK: {FLUENT_BIT_DAEMONSET_NAME} DaemonSet's host log mount (varlog) is read-only.")

    volume_state = next((v for v in volumes if v.get("name") == "fluent-bit-state"), None)
    if volume_state is None or not ((volume_state.get("emptyDir") or {}).get("sizeLimit")):
        raise Phase4Error("the fluent-bit-state emptyDir volume does not set sizeLimit.")
    print("OK: the fluent-bit-state emptyDir volume sets sizeLimit.")

    actual_image = container.get("image")
    if actual_image != fluent_bit_image:
        raise Phase4Error(f"rendered {FLUENT_BIT_DAEMONSET_NAME} image does not match FLUENT_BIT_IMAGE. Expected: {fluent_bit_image}. Actual: {actual_image}.")
    print(f"OK: rendered {FLUENT_BIT_DAEMONSET_NAME} image exactly matches FLUENT_BIT_IMAGE: {actual_image}")


def _validate_fluent_bit_configmap(docs, runtime_namespace, monitor_namespace, runtime_log_group, monitor_log_group):
    """Structurally locates the gg-fluent-bit-config ConfigMap via PyYAML, then applies exact-text checks against its embedded Fluent Bit .conf data values -- that inner content is genuine Fluent Bit configuration-file syntax, not YAML, so text matching remains the correct/practical tool for it."""
    cm = _find_document(docs, "ConfigMap", FLUENT_BIT_CONFIGMAP_NAME)
    if cm is None:
        raise Phase4Error(f"rendered ConfigMap {FLUENT_BIT_CONFIGMAP_NAME} was not found.")
    conf_text = "\n".join(str(v) for v in (cm.get("data") or {}).values())

    tail_count = len(re.findall(r"(?m)^[ \t]*Name[ \t]+tail[ \t]*$", conf_text))
    if tail_count != 2:
        raise Phase4Error(f"expected exactly 2 Tail inputs, found {tail_count}.")
    print("OK: exactly 2 Tail inputs are rendered.")

    if f"Path              /var/log/containers/*_{runtime_namespace}_*.log" not in conf_text:
        raise Phase4Error(f"runtime Tail input Path is not exactly /var/log/containers/*_{runtime_namespace}_*.log")
    if f"Path              /var/log/containers/*_{monitor_namespace}_*.log" not in conf_text:
        raise Phase4Error(f"monitor Tail input Path is not exactly /var/log/containers/*_{monitor_namespace}_*.log")
    if "Path              /var/log/containers/*.log" in conf_text:
        raise Phase4Error("an unrestricted /var/log/containers/*.log Tail Path still exists.")
    if "Tag               runtime.*" not in conf_text:
        raise Phase4Error("runtime Tail input Tag is not exactly runtime.*")
    if "Tag               monitor.*" not in conf_text:
        raise Phase4Error("monitor Tail input Tag is not exactly monitor.*")
    print("OK: runtime and monitor Tail inputs have exact, deterministic Path and Tag values; no unrestricted Path remains.")

    if "Kube_Tag_Prefix   runtime.var.log.containers." not in conf_text:
        raise Phase4Error("runtime Kubernetes filter does not set Kube_Tag_Prefix runtime.var.log.containers.")
    if "Kube_Tag_Prefix   monitor.var.log.containers." not in conf_text:
        raise Phase4Error("monitor Kubernetes filter does not set Kube_Tag_Prefix monitor.var.log.containers.")
    kubernetes_filter_count = len(re.findall(r"(?m)^[ \t]*Name[ \t]+kubernetes[ \t]*$", conf_text))
    if kubernetes_filter_count != 2:
        raise Phase4Error(f"expected exactly 2 kubernetes FILTERs (one per input), found {kubernetes_filter_count}.")
    print("OK: both kubernetes FILTERs set the expected explicit Kube_Tag_Prefix.")

    if re.search(r"Name[ \t]+grep", conf_text):
        raise Phase4Error("a grep FILTER is still rendered -- routing must not depend on Kubernetes-metadata enrichment.")
    if re.search(r"Name[ \t]+rewrite_tag|Emitter_Name|Emitter_Storage\.type|runtime\.\$TAG|monitor\.\$TAG", conf_text):
        raise Phase4Error("a rewrite_tag FILTER or emitter is still rendered -- routing must be direct via Tail Tag, not rewritten downstream.")
    print("OK: no grep FILTER and no rewrite_tag FILTER/emitter are rendered.")

    if "Match                   runtime.*" not in conf_text:
        raise Phase4Error("runtime cloudwatch_logs OUTPUT does not use Match runtime.*")
    if "Match                   monitor.*" not in conf_text:
        raise Phase4Error("monitor cloudwatch_logs OUTPUT does not use Match monitor.*")
    for expected_log_group in (runtime_log_group, monitor_log_group):
        if not re.search(rf"log_group_name[ \t]+{re.escape(expected_log_group)}$", conf_text, re.MULTILINE):
            raise Phase4Error(f"{FLUENT_BIT_CONFIGMAP_NAME} does not target the exact pre-created log group {expected_log_group}.")
    if re.search(r"auto_create_group[ \t]+true", conf_text):
        raise Phase4Error(f"{FLUENT_BIT_CONFIGMAP_NAME} allows Fluent Bit to auto-create CloudWatch log groups.")
    if not re.search(r"auto_create_group[ \t]+false", conf_text):
        raise Phase4Error(f"{FLUENT_BIT_CONFIGMAP_NAME} does not explicitly disable auto_create_group.")
    print("OK: each OUTPUT matches directly on its own input's tag, and both CloudWatch log-group destinations are exact and pre-created (auto_create_group false).")

    total_limit_size_count = len(re.findall(r"storage\.total_limit_size[ \t]+[0-9]", conf_text))
    if total_limit_size_count != 2:
        raise Phase4Error(f"expected storage.total_limit_size on both cloudwatch_logs OUTPUTs, found {total_limit_size_count} occurrence(s).")
    print("OK: both cloudwatch_logs OUTPUTs set storage.total_limit_size.")


def _validate_no_unresolved_placeholders(rendered_path):
    text = rendered_path.read_text()
    if "<no value>" in text:
        raise Phase4Error("rendered manifest contains an unresolved Helm placeholder: <no value>")
    print("OK: no unresolved Helm placeholders in the rendered manifest.")


def _validate_no_public_registry_references(rendered_path):
    for chart_file in (REPO_ROOT / HELM_CHART_PATH / "values.yaml", *sorted((REPO_ROOT / HELM_CHART_PATH / "templates").glob("fluent-bit-*.yaml"))):
        for lineno, line in enumerate(chart_file.read_text().splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or "public.ecr.aws" not in line:
                continue
            if re.search(r'"[^"]*public\.ecr\.aws[^"]*"', line):
                continue
            raise Phase4Error(f"a live public.ecr.aws reference remains in the Phase 6A chart source: {chart_file}:{lineno}")
    if "public.ecr.aws" in rendered_path.read_text():
        raise Phase4Error("a public.ecr.aws reference remains in the rendered manifest.")
    print("OK: no live public.ecr.aws reference anywhere in the Phase 6A chart source or rendered manifest.")


def _validate_no_fluent_bit_sidecar_in_runtime_chart():
    runtime_statefulset = REPO_ROOT / "helm" / "goldengate" / "templates" / "runtime-statefulset.yaml"
    if runtime_statefulset.is_file():
        text = runtime_statefulset.read_text().lower()
        if "fluent-bit" in text or "fluentbit" in text:
            raise Phase4Error(f"{runtime_statefulset} references Fluent Bit -- no GoldenGate runtime sidecar is permitted.")
    print("OK: no Fluent Bit sidecar reference in the GoldenGate runtime StatefulSet template.")


def _validate_rendered_manifest(rendered_path, fluent_bit_image, runtime_namespace, monitor_namespace, runtime_role_arn, platform_logging_role_arn, runtime_log_group, monitor_log_group):
    docs = _parse_documents(rendered_path)
    _validate_zero_namespace_documents(docs)
    _validate_runtime_service_account(docs, RUNTIME_SA_NAME, runtime_role_arn, runtime_namespace, deletion_protected=True)
    _validate_runtime_service_account(docs, FLUENT_BIT_SA_NAME, platform_logging_role_arn, runtime_namespace, deletion_protected=False)
    _validate_service_account_set(docs)
    _validate_no_unexpected_irsa_role(docs, {runtime_role_arn, platform_logging_role_arn})
    _validate_no_forbidden_kinds(docs)
    _validate_fluent_bit_daemonset_shape(docs, fluent_bit_image)
    _validate_fluent_bit_configmap(docs, runtime_namespace, monitor_namespace, runtime_log_group, monitor_log_group)
    _validate_no_unresolved_placeholders(rendered_path)
    _validate_no_public_registry_references(rendered_path)
    _validate_no_fluent_bit_sidecar_in_runtime_chart()
    print("OK: rendered platform manifest passed all checks.")


def _package_chart(chart_version, values_file):
    temp_chart_path = REPO_ROOT / "work" / "charts" / "goldengate-platform"
    if temp_chart_path.exists():
        import shutil
        shutil.rmtree(temp_chart_path)
    temp_chart_path.mkdir(parents=True)
    import shutil
    shutil.copytree(REPO_ROOT / HELM_CHART_PATH, temp_chart_path, dirs_exist_ok=True)
    shutil.copy(REPO_ROOT / values_file, temp_chart_path / "values-deployment.yaml")

    packaged_dir = REPO_ROOT / "packaged"
    packaged_dir.mkdir(parents=True, exist_ok=True)
    run(["helm", "package", str(temp_chart_path), "--version", chart_version, "--app-version", chart_version, "--destination", "packaged"])
    package_path = packaged_dir / f"{CHART_NAME}-{chart_version}.tgz"
    if not package_path.is_file():
        raise Phase4Error(f"helm package did not produce the expected archive: {package_path}")
    return temp_chart_path, package_path


def cmd_prepare_and_validate(args):
    environment = require_environment_arg(args.environment)
    ecr_registry = require_env("ECR_REGISTRY")
    run_number = require_env("GITHUB_RUN_NUMBER")
    fluent_bit_image = require_env("FLUENT_BIT_IMAGE")

    _validate_fluent_bit_image_format(fluent_bit_image, ecr_registry)
    fluent_bit_ecr_digest = _derive_fluent_bit_ecr_digest(fluent_bit_image, ecr_registry)
    print(f"OK: FLUENT_BIT_IMAGE is a validly-formatted private immutable digest reference. Repository: {FLUENT_BIT_ECR_REPOSITORY_EXPECTED}. Digest: {fluent_bit_ecr_digest}")

    chart_version = f"0.1.{run_number}"
    helm_ecr_repository = f"{HELM_OCI_NAMESPACE}/{CHART_NAME}"
    helm_push_url = f"oci://{ecr_registry}/{HELM_OCI_NAMESPACE}"
    helm_chart_ref = f"oci://{ecr_registry}/{helm_ecr_repository}"
    values_file = f"platform/{environment}/goldengate-platform/values.yaml"
    release_name = f"goldengate-{environment}-platform"
    argocd_app_name = release_name

    _validate_required_files(values_file)

    rendered_path = _helm_template(values_file, release_name)
    run(["helm", "lint", HELM_CHART_PATH, "--values", values_file, *_helm_set_string_overrides()])
    print("OK: helm lint passed.")

    _validate_rendered_manifest(
        rendered_path, fluent_bit_image,
        runtime_namespace=require_env("RUNTIME_NAMESPACE"), monitor_namespace=require_env("MONITOR_NAMESPACE"),
        runtime_role_arn=require_env("RUNTIME_ROLE_ARN"), platform_logging_role_arn=require_env("PLATFORM_LOGGING_ROLE_ARN"),
        runtime_log_group=require_env("RUNTIME_LOG_GROUP"), monitor_log_group=require_env("MONITOR_LOG_GROUP"),
    )

    temp_chart_path, package_path = _package_chart(chart_version, values_file)

    update_state(args.state_path, {
        "environment": environment,
        "values_file": values_file,
        "chart_version": chart_version,
        "temp_chart_path": str(temp_chart_path.relative_to(REPO_ROOT)),
        "helm_ecr_repository": helm_ecr_repository,
        "helm_push_url": helm_push_url,
        "helm_chart_ref": helm_chart_ref,
        "rendered_manifest": str(rendered_path.relative_to(REPO_ROOT)),
        "package_path": str(package_path.relative_to(REPO_ROOT)),
        "namespace": require_env("RUNTIME_NAMESPACE"),
        "release_name": release_name,
        "argocd_app_name": argocd_app_name,
        "fluent_bit_image": fluent_bit_image,
        "fluent_bit_ecr_repository": FLUENT_BIT_ECR_REPOSITORY_EXPECTED,
        "fluent_bit_ecr_digest": fluent_bit_ecr_digest,
    })
    print("OK: GoldenGate Platform chart validated and packaged locally.")


# 30-sub-platform.yaml: verify the private Fluent Bit ECR artifact (AWS credentials required)

def cmd_verify_fluent_bit_artifact(args):
    require_environment_arg(args.environment)
    state = load_state(args.state_path)
    fluent_bit_ecr_repository = require_state_value(state, "fluent_bit_ecr_repository")
    fluent_bit_ecr_digest = require_state_value(state, "fluent_bit_ecr_digest")

    aws_region = require_env("AWS_REGION")
    ecr_account_id = require_env("ECR_ACCOUNT_ID")

    caller_account = run(["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"]).stdout.strip()
    if caller_account != ecr_account_id:
        raise Phase4Error(f"AWS caller account is {caller_account}, expected {ecr_account_id}.")
    print(f"OK: AWS caller account is {ecr_account_id}.")

    proc = run(["aws", "ecr", "describe-images", "--region", aws_region, "--repository-name", fluent_bit_ecr_repository,
                "--image-ids", f"imageDigest={fluent_bit_ecr_digest}", "--output", "json"], check=False)
    if proc.returncode != 0:
        error_text = (proc.stderr or "") + (proc.stdout or "")
        if "RepositoryNotFoundException" in error_text:
            raise Phase4Error(f"private ECR repository {fluent_bit_ecr_repository} was not found.")
        if "ImageNotFoundException" in error_text:
            raise Phase4Error(f"digest {fluent_bit_ecr_digest} was not found in repository {fluent_bit_ecr_repository}.")
        raise Phase4Error(f"aws ecr describe-images failed: {error_text.strip() or '(no output)'}")

    result = json.loads(proc.stdout)
    returned_digest = ((result.get("imageDetails") or [{}])[0]).get("imageDigest")
    if returned_digest != fluent_bit_ecr_digest:
        raise Phase4Error(f"ECR returned digest {returned_digest}, expected {fluent_bit_ecr_digest}.")
    print(f"OK: verified digest {fluent_bit_ecr_digest} exists in {fluent_bit_ecr_repository} (account {ecr_account_id}).")


# 30-sub-platform.yaml: ECR repository existence/policy + chart publish (AWS credentials required)

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


def _describe_ecr_repository(repository_name, aws_region):
    return run(["aws", "ecr", "describe-repositories", "--region", aws_region, "--repository-names", repository_name], check=False)


def _ensure_ecr_repository(repository_name, aws_region):
    """Fail-closed repository-existence classification: an inability to inspect state (AccessDenied/ExpiredToken/throttling/network/unknown error) must never be interpreted as "does not exist" -- only an explicit RepositoryNotFoundException from describe-repositories authorizes create-repository. Fixes the same fail-open defect already corrected in Phase 3's ECR hardening."""
    exists = _describe_ecr_repository(repository_name, aws_region)
    if exists.returncode == 0:
        print(f"ECR repository already exists: {repository_name}")
        return

    error_text = (exists.stderr or "") + (exists.stdout or "")
    if "RepositoryNotFoundException" not in error_text:
        raise Phase4Error(
            f"could not determine whether ECR repository {repository_name!r} exists (describe-repositories exited {exists.returncode} without an explicit RepositoryNotFoundException) -- "
            f"refusing to guess and refusing to create it. Failing closed:\n{error_text.strip() or '(no output)'}"
        )

    print(f"Creating ECR repository: {repository_name}")
    try:
        _create_ecr_repository(repository_name, aws_region)
    except Phase4Error as exc:
        # Race-safe handling: another actor may have created the repository between our describe and our create. RepositoryAlreadyExistsException is the ONLY create failure tolerated here, and only after confirming via a fresh describe-repositories that the repository now genuinely exists.
        if "RepositoryAlreadyExistsException" not in str(exc):
            raise
        recheck = _describe_ecr_repository(repository_name, aws_region)
        if recheck.returncode != 0:
            raise Phase4Error(f"ECR repository {repository_name!r} creation raced with another actor (RepositoryAlreadyExistsException), but the required re-describe still did not succeed:\n{((recheck.stderr or '') + (recheck.stdout or '')).strip() or '(no output)'}") from exc
    print(f"Created ECR repository: {repository_name}")


def _ensure_ecr_repository_policy(repository_name, aws_region, argocd_ecr_read_role_arn):
    """Preserves the controlled merge behavior for Sid AllowArgocdEksRolePullGoldengatePlatformHelmChart -- unrelated existing statements are preserved untouched. RepositoryPolicyNotFoundException initializes an empty policy; any OTHER get-repository-policy failure (including AccessDenied) fails closed rather than being silently treated as "no policy"."""
    proc = run(["aws", "ecr", "get-repository-policy", "--region", aws_region, "--repository-name", repository_name, "--query", "policyText", "--output", "text"], check=False)
    if proc.returncode == 0:
        policy = json.loads(proc.stdout)
    else:
        error_text = (proc.stderr or "") + (proc.stdout or "")
        if "RepositoryPolicyNotFoundException" not in error_text:
            raise Phase4Error(f"failed to read the existing ECR repository policy for {repository_name!r}; refusing to assume it is absent:\n{error_text.strip() or '(no output)'}")
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

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        json.dump(policy, tmp)
        tmp_path = tmp.name
    try:
        run(["aws", "ecr", "set-repository-policy", "--region", aws_region, "--repository-name", repository_name, "--policy-text", f"file://{tmp_path}"])
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    print(f"ECR repository policy on {repository_name} now allows pull from {argocd_ecr_read_role_arn}.")


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
    argocd_ecr_read_role_arn = require_env("ARGOCD_ECR_READ_ROLE_ARN")

    # ECR login password: fed directly into helm's own stdin, never through a shell pipeline and never printed/logged.
    password_proc = run(["aws", "ecr", "get-login-password", "--region", aws_region])
    password = password_proc.stdout.strip()
    run(["helm", "registry", "login", "--username", "AWS", "--password-stdin", ecr_registry], input_text=password)

    _ensure_ecr_repository(helm_ecr_repository, aws_region)
    _ensure_ecr_repository_policy(helm_ecr_repository, aws_region, argocd_ecr_read_role_arn)

    package_path = REPO_ROOT / package_path_rel
    run(["helm", "push", str(package_path), helm_push_url])
    print(f"Published Helm chart: {helm_chart_ref}:{chart_version}")

    pulled_dir = REPO_ROOT / "pulled"
    pulled_dir.mkdir(parents=True, exist_ok=True)
    run(["helm", "pull", helm_chart_ref, "--version", chart_version, "--destination", "pulled"])

    update_state(args.state_path, {"pulled_directory": "pulled"})
    print("OK: GoldenGate Platform Helm chart published to private ECR and verified pullable.")


# 30-sub-platform.yaml: reconcile the Platform Argo CD Application (AWS credentials required, inputs.deploy only)

def _build_application_manifest(argocd_app_name, argocd_namespace, helm_chart_ref, chart_version, release_name, runtime_namespace, environment, runtime_role_arn, platform_logging_role_arn, aws_region, monitor_namespace, runtime_log_group, monitor_log_group, fluent_bit_image):
    parameters = [
        {"name": "environment", "value": environment},
        {"name": "namespaces.runtime.name", "value": runtime_namespace},
        {"name": "runtimeServiceAccount.roleArn", "value": runtime_role_arn},
        {"name": "fluentBit.serviceAccount.roleArn", "value": platform_logging_role_arn},
        {"name": "fluentBit.aws.region", "value": aws_region},
        {"name": "fluentBit.namespaces.runtime", "value": runtime_namespace},
        {"name": "fluentBit.namespaces.monitoring", "value": monitor_namespace},
        {"name": "fluentBit.cloudwatch.runtimeLogGroupName", "value": runtime_log_group},
        {"name": "fluentBit.cloudwatch.monitorLogGroupName", "value": monitor_log_group},
        {"name": "fluentBit.image.reference", "value": fluent_bit_image},
    ]
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {
            "name": argocd_app_name,
            "namespace": argocd_namespace,
            "finalizers": ["resources-finalizer.argocd.argoproj.io"],
            "labels": {"app.kubernetes.io/name": "goldengate-platform", "app.kubernetes.io/managed-by": "argocd"},
        },
        "spec": {
            "project": "default",
            "source": {
                "repoURL": helm_chart_ref,
                "targetRevision": chart_version,
                "path": ".",
                "helm": {"releaseName": release_name, "valueFiles": ["values-deployment.yaml"], "parameters": parameters},
            },
            "destination": {"server": "https://kubernetes.default.svc", "namespace": runtime_namespace},
            "syncPolicy": {
                "automated": {"prune": True, "selfHeal": True},
                "syncOptions": ["CreateNamespace=true"],
                "managedNamespaceMetadata": {"labels": {"app.kubernetes.io/name": "goldengate-platform", "app.kubernetes.io/managed-by": "argocd"}},
            },
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
            run(["kubectl", "describe", "application", app_name, "-n", namespace], check=False)
            raise Phase4Error(f"Timed out after {timeout_seconds}s waiting for {app_name} to become Synced and Healthy.")

        time.sleep(interval_seconds)
        elapsed += interval_seconds


def cmd_reconcile_cluster(args):
    environment = require_environment_arg(args.environment)
    state = load_state(args.state_path)
    values_file = require_state_value(state, "values_file")
    chart_version = require_state_value(state, "chart_version")
    helm_chart_ref = require_state_value(state, "helm_chart_ref")
    release_name = require_state_value(state, "release_name")
    argocd_app_name = require_state_value(state, "argocd_app_name")
    runtime_namespace = require_state_value(state, "namespace")
    fluent_bit_image = require_state_value(state, "fluent_bit_image")

    aws_region = require_env("AWS_REGION")
    eks_cluster_name = require_env("EKS_CLUSTER_NAME")
    eks_deploy_role_arn = require_env("EKS_DEPLOY_ROLE_ARN")
    argocd_namespace = require_env("ARGOCD_NAMESPACE")
    ecr_registry = require_env("ECR_REGISTRY")
    monitor_namespace = require_env("MONITOR_NAMESPACE")
    runtime_role_arn = require_env("RUNTIME_ROLE_ARN")
    platform_logging_role_arn = require_env("PLATFORM_LOGGING_ROLE_ARN")
    runtime_log_group = require_env("RUNTIME_LOG_GROUP")
    monitor_log_group = require_env("MONITOR_LOG_GROUP")

    run(["aws", "sts", "get-caller-identity"])
    run(["aws", "eks", "update-kubeconfig", "--region", aws_region, "--name", eks_cluster_name,
         "--role-arn", eks_deploy_role_arn, "--assume-role-arn", eks_deploy_role_arn])
    run(["kubectl", "config", "current-context"])
    run(["kubectl", "version", "--client=true"])

    if run(["kubectl", "get", "crd", "applications.argoproj.io"], check=False).returncode != 0:
        raise Phase4Error(
            "CRD applications.argoproj.io not found. Argo CD prerequisite is not healthy. The MAIN orchestrator normally "
            "classifies and bootstrap-validates Argo CD before this stage runs (00-main-goldengate-orchestrator.yaml); for "
            "standalone repair/reconciliation use 20-sub-argocd.yaml."
        )
    print("Argo CD Application CRD is present.")

    if run(["kubectl", "get", "secret", ARGOCD_PLATFORM_SECRET_NAME, "-n", argocd_namespace], check=False).returncode != 0:
        raise Phase4Error(
            f"PREREQUISITE NOT MET: Secret {ARGOCD_PLATFORM_SECRET_NAME} does not exist in namespace {argocd_namespace}. "
            "Required order: (1) IAM/Secrets Terraform workflow; (2) re-run 20-sub-argocd.yaml (provisions this Secret); "
            "(3) re-run this workflow. Refusing to inject a short-lived credential as a workaround."
        )
    url_proc = run(["kubectl", "get", "secret", ARGOCD_PLATFORM_SECRET_NAME, "-n", argocd_namespace, "-o", "jsonpath={.data.url}"])
    actual_url = base64.b64decode(url_proc.stdout).decode("utf-8") if url_proc.stdout else ""
    expected_url = f"oci://{ecr_registry}/{HELM_OCI_NAMESPACE}/{CHART_NAME}"
    if actual_url != expected_url:
        raise Phase4Error(f"Secret {ARGOCD_PLATFORM_SECRET_NAME} url mismatch. Expected {expected_url}, got {actual_url}.")
    print(f"OK: {ARGOCD_PLATFORM_SECRET_NAME} exists and points to {expected_url}.")

    manifest = _build_application_manifest(
        argocd_app_name, argocd_namespace, helm_chart_ref, chart_version, release_name, runtime_namespace, environment,
        runtime_role_arn, platform_logging_role_arn, aws_region, monitor_namespace, runtime_log_group, monitor_log_group, fluent_bit_image,
    )
    manifest_yaml = yaml.safe_dump(manifest, default_flow_style=False, sort_keys=False)
    run(["kubectl", "apply", "-f", "-"], input_text=manifest_yaml)
    run(["kubectl", "annotate", "application", argocd_app_name, "-n", argocd_namespace, "argocd.argoproj.io/refresh=hard", "--overwrite"])

    _wait_for_argo_application(argocd_app_name, argocd_namespace, timeout_seconds=600, interval_seconds=15)
    print("OK: GoldenGate Platform Argo CD Application reconciled.")


def _list_owned_workloads(kind, namespace, release_name):
    """Positively proves how many <kind> (StatefulSet/Deployment) resources are owned by the given Platform release. Kubernetes supports both resource kinds on the target EKS cluster, so there is no valid "resource kind absent" outcome here -- a successful query returning items=[] is the only way absence is ever represented. run()'s default check=True fails closed (raises Phase4Error) on any kubectl inspection failure -- Forbidden, Unauthorized, a connection/network failure, a timeout, or any other non-zero exit -- so an inspection failure can never be silently treated as zero owned workloads. Also fails closed if the successful response is not well-formed JSON shaped as {"items": [...]}."""
    proc = run(["kubectl", "get", kind, "-n", namespace, "-l", f"app.kubernetes.io/instance={release_name}", "-o", "json"])
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Phase4Error(f"kubectl get {kind} -n {namespace} -l app.kubernetes.io/instance={release_name} returned malformed JSON -- refusing to treat this as an empty workload list: {exc}") from exc
    if not isinstance(parsed, dict):
        raise Phase4Error(f"kubectl get {kind} -n {namespace} -l app.kubernetes.io/instance={release_name} returned a non-object top-level JSON result -- refusing to treat this as an empty workload list.")
    if "items" not in parsed:
        raise Phase4Error(f"kubectl get {kind} -n {namespace} -l app.kubernetes.io/instance={release_name} response has no 'items' key -- refusing to treat this as an empty workload list.")
    items = parsed["items"]
    if not isinstance(items, list):
        raise Phase4Error(f"kubectl get {kind} -n {namespace} -l app.kubernetes.io/instance={release_name} 'items' is a {type(items).__name__}, not a list -- refusing to treat this as an empty workload list.")
    return items


# 30-sub-platform.yaml: post-deployment validation (AWS credentials required, inputs.deploy only)

def cmd_post_deploy_validation(args):
    require_environment_arg(args.environment)
    state = load_state(args.state_path)
    runtime_namespace = require_state_value(state, "namespace")
    release_name = require_state_value(state, "release_name")
    runtime_role_arn = require_env("RUNTIME_ROLE_ARN")

    run(["kubectl", "get", "namespace", runtime_namespace])

    run(["kubectl", "get", "serviceaccount", RUNTIME_SA_NAME, "-n", runtime_namespace])
    actual_role_annotation = _kubectl_get_jsonpath("serviceaccount", RUNTIME_SA_NAME, runtime_namespace, r"{.metadata.annotations.eks\.amazonaws\.com/role-arn}") or ""
    if actual_role_annotation != runtime_role_arn:
        raise Phase4Error(f"unexpected IRSA role annotation on ServiceAccount {RUNTIME_SA_NAME}: {actual_role_annotation}")
    print(f"OK: {RUNTIME_SA_NAME} IRSA role annotation matches {runtime_role_arn}.")

    owned_statefulsets = _list_owned_workloads("statefulset", runtime_namespace, release_name)
    owned_deployments = _list_owned_workloads("deployment", runtime_namespace, release_name)
    if len(owned_statefulsets) != 0 or len(owned_deployments) != 0:
        raise Phase4Error(f"found {len(owned_statefulsets)} StatefulSet(s) and {len(owned_deployments)} Deployment(s) owned by {release_name} -- the platform release must never own a GoldenGate runtime workload.")
    print(f"OK: no StatefulSet/Deployment resources are owned by {release_name}.")

    ds_proc = run(["kubectl", "get", "daemonset", "-n", runtime_namespace, "-l", f"app.kubernetes.io/instance={release_name}", "-o", "json"])
    daemonsets = (json.loads(ds_proc.stdout) or {}).get("items") or []
    if len(daemonsets) != 1:
        raise Phase4Error(f"expected exactly 1 DaemonSet owned by {release_name}, found {len(daemonsets)}.")
    run(["kubectl", "get", "daemonset", FLUENT_BIT_DAEMONSET_NAME, "-n", runtime_namespace])
    print(f"OK: exactly 1 DaemonSet ({FLUENT_BIT_DAEMONSET_NAME}) is owned by {release_name}.")


# 30-sub-platform.yaml: workflow summary (always(), no AWS credentials, tolerant of partial state)

def cmd_summary(args):
    state = load_state(args.state_path)
    chart_version = state.get("chart_version", "unknown")
    helm_chart_ref = state.get("helm_chart_ref", "unknown")
    runtime_namespace = state.get("namespace", "unknown")
    argocd_app_name = state.get("argocd_app_name", "unknown")

    lines = [
        "## GoldenGate platform deploy summary",
        "",
        f"- Chart version: `{chart_version}`",
        f"- Chart ref: `{helm_chart_ref}`",
        f"- Runtime namespace: `{runtime_namespace}`",
        f"- Argo CD Application: `{argocd_app_name}`",
        "",
        "### Ownership",
        "",
        f"This is the single designated owner of Namespace/{runtime_namespace} and ServiceAccount/{RUNTIME_SA_NAME} "
        "(the one canonical runtime identity every GoldenGate deployment type shares). Individual GoldenGate runtime "
        "Applications reference their resolved ServiceAccount by name only and never own or delete it. This Application "
        f"also owns DaemonSet/{FLUENT_BIT_DAEMONSET_NAME} (platform-level centralized container logging), ServiceAccount/{FLUENT_BIT_SA_NAME}, "
        "and its supporting ClusterRole/ClusterRoleBinding and ConfigMap.",
        "",
    ]
    write_step_summary("\n".join(lines))
    print("OK: wrote GoldenGate Platform workflow summary.")


# CLI wiring

_SUBCOMMANDS = {
    "ensure-kubectl": cmd_ensure_kubectl,
    "ensure-deploy-tools": cmd_ensure_deploy_tools,
    "ownership-preflight": cmd_ownership_preflight,
    "prepare-and-validate": cmd_prepare_and_validate,
    "verify-fluent-bit-artifact": cmd_verify_fluent_bit_artifact,
    "publish-chart": cmd_publish_chart,
    "reconcile-cluster": cmd_reconcile_cluster,
    "post-deploy-validation": cmd_post_deploy_validation,
    "strict-acceptance": cmd_strict_acceptance,
    "summary": cmd_summary,
}

_ENVIRONMENT_SUBCOMMANDS = (
    "ownership-preflight", "prepare-and-validate", "verify-fluent-bit-artifact", "publish-chart",
    "reconcile-cluster", "post-deploy-validation", "strict-acceptance", "summary",
)


def build_parser():
    parser = argparse.ArgumentParser(description="Phase 4 | GoldenGate Platform orchestrator (ownership preflight, Helm build/publish/deploy, strict acceptance).")
    parser.add_argument("--state-file", type=Path, default=None, help="Override the Phase 4 Platform state file path (default: $RUNNER_TEMP/goldengate-phase4-platform-state.json).")
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
    except Phase4Error as exc:
        print(f"FAIL: {exc}")
        return 1
    except subprocess.SubprocessError as exc:
        print(f"FAIL: subprocess execution error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
