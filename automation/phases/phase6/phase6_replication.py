#!/usr/bin/env python3
"""Phase 6A-6B | GoldenGate replication orchestration entrypoint for replication_reconcile_once/replication_dry_run_validation in .github/workflows/00-main-goldengate-orchestrator.yaml; a thin orchestration layer that never reimplements descriptor/pipeline resolution (owned by automation/goldengate-deployment-model.py, invoked here as a subprocess CLI, never a second parser of runtime descriptor YAML) or replication plan/manifest rendering (owned by automation/goldengate-replication.py's build_replication_plan()/render_secret_provider_class()/render_config_map()/render_job(), invoked here only via its own `render-job` CLI, never duplicated). Discovers enabled replication pipelines LOCALLY before any AWS/EKS access so a zero-pipeline run is a true no-op; reconciles enabled pipelines sequentially/fail-fast against a live cluster; validates rendered manifests locally with zero cluster mutation. AWS/Kubernetes credentials are never written to $GITHUB_OUTPUT or logged; only a single fixed has_pipelines=true|false boolean is ever emitted as a GitHub Actions output."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

DEPLOYMENT_MODEL_TOOL = REPO_ROOT / "automation" / "goldengate-deployment-model.py"
REPLICATION_ENGINE_TOOL = REPO_ROOT / "automation" / "goldengate-replication.py"

# Must match automation/goldengate-replication.py's own DETERMINISTIC_DRY_RUN_EXECUTION_ID -- a fixed, non-live-run-scoped execution token so local Validate-mode dry-run rendering is reproducible and never collides with a real reconciliation's <run-id>-<run-attempt> execution suffix.
DRY_RUN_EXECUTION_ID = "dry-run"

JOB_WAIT_TIMEOUT = "600s"

# Defense-in-depth secret-VALUE field-name denylist -- these are the mapping KEYS a real credential value would be stored under if one were ever accidentally rendered; legitimate JMES/objectAlias selector STRINGS such as "OGG_DB_PASSWORD" or "source-admin-password" are VALUES, never themselves used as a mapping key, so they never trip this check.
FORBIDDEN_SECRET_VALUE_KEYS = frozenset({
    "password", "aws_secret_access_key", "aws_session_token", "secret_access_key",
    "session_token", "client_secret", "private_key", "databasecredentialpassword",
})


class Phase6Error(Exception):
    """A fail-closed Phase 6 replication orchestration error; main() reports it and exits non-zero."""


class _DuplicateKeyError(Exception):
    """Raised by _no_duplicates_constructor when a rendered manifest contains a duplicate YAML mapping key."""


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


def require_env(name):
    value = os.environ.get(name, "")
    if not value:
        raise Phase6Error(f"{name} is empty; canonical environment configuration must be loaded before this step.")
    return value


def write_github_output(pairs, output_path=None):
    """Appends name=value lines to $GITHUB_OUTPUT. The only output ever written by this module is the single fixed has_pipelines=true|false boolean -- never a caller-controlled name, never a multiline pipeline list. No-op (never raises) when GITHUB_OUTPUT is unset."""
    path = output_path if output_path is not None else os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for name, value in pairs:
            f.write(f"{name}={value}\n")


# Safe subprocess execution -- argument arrays only, never shell=True, never a shell pipeline.

def run(argv, env=None, cwd=None, check=True, capture_output=True, input_text=None):
    """Runs argv as an argument array. Fails closed with the tool's own stderr/stdout on a non-zero exit when check=True."""
    proc = subprocess.run(
        argv,
        cwd=cwd or REPO_ROOT,
        env=env,
        capture_output=capture_output,
        text=True,
        input=input_text,
    )
    if check and proc.returncode != 0:
        raise Phase6Error(f"{' '.join(str(a) for a in argv)} failed (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
    return proc


def _connect_to_eks():
    """Exact preserved role model: aws eks update-kubeconfig --region AWS_REGION --name EKS_CLUSTER_NAME --role-arn EKS_DEPLOY_ROLE_ARN --assume-role-arn EKS_DEPLOY_ROLE_ARN -- fails closed (via run()'s check=True) on ANY AWS/EKS error (AccessDenied, Unauthorized, network error, missing cluster); never reinterprets such a failure as an empty-pipeline no-op, since pipeline discovery has already established a non-empty pipeline list before this is ever called."""
    aws_region = require_env("AWS_REGION")
    eks_cluster_name = require_env("EKS_CLUSTER_NAME")
    eks_deploy_role_arn = require_env("EKS_DEPLOY_ROLE_ARN")
    run(["aws", "eks", "update-kubeconfig", "--region", aws_region, "--name", eks_cluster_name,
         "--role-arn", eks_deploy_role_arn, "--assume-role-arn", eks_deploy_role_arn])
    return aws_region


# Canonical pipeline discovery -- the SAME automation/goldengate-deployment-model.py CLI used everywhere else, never a second independent parser of runtime descriptor YAML.

def _discover_pipelines(environment):
    """Runs the canonical `validate` then `replication-pipelines` subcommands of automation/goldengate-deployment-model.py -- fails closed (raises Phase6Error) on any model validation problem BEFORE ever touching AWS/kubectl; otherwise returns the sorted list of enabled replication pipeline IDs (may be empty). Every caller (discover/reconcile/validate-local) recomputes this independently from the same checkout/model -- pipeline IDs are never threaded through $GITHUB_OUTPUT as a multiline list."""
    validate_proc = run([sys.executable, str(DEPLOYMENT_MODEL_TOOL), "--environment", environment, "validate"], check=False)
    if validate_proc.returncode != 0:
        raise Phase6Error(f"canonical deployment-model validation failed for environment {environment!r}:\n{validate_proc.stdout}\n{validate_proc.stderr}")
    pipelines_proc = run([sys.executable, str(DEPLOYMENT_MODEL_TOOL), "--environment", environment, "replication-pipelines"], check=False)
    if pipelines_proc.returncode != 0:
        raise Phase6Error(f"failed to list canonical replication pipelines for environment {environment!r}:\n{pipelines_proc.stdout}\n{pipelines_proc.stderr}")
    return [line.strip() for line in pipelines_proc.stdout.splitlines() if line.strip()]


def _pipeline_output_dir(pipeline_id, execution_id):
    """ONE canonical local render-output-directory derivation, relative to REPO_ROOT, keyed by both pipeline_id and execution_id -- a live reconcile (<run-id>-<run-attempt>) and a local dry-run validation ("dry-run") never share or collide over the same on-disk directory, even within the same working tree."""
    return REPO_ROOT / "work" / "replication" / pipeline_id / execution_id


def _render_pipeline(environment, pipeline_id, execution_id, region, namespace, output_dir):
    """Invokes the CURRENT, unmodified replication business engine's own `render-job` CLI -- never a second reimplementation of build_replication_plan()/render_secret_provider_class()/render_config_map()/render_job() inside this module. The engine itself creates output_dir and writes secretproviderclass.yaml/configmap.yaml/job.yaml into it."""
    run([sys.executable, str(REPLICATION_ENGINE_TOOL), "render-job",
         "--environment", environment, "--region", region, "--namespace", namespace,
         "--execution-id", execution_id, "--output-dir", str(output_dir), pipeline_id])


# Rendered-manifest structural validation -- the SAME helper used by both Deploy (before kubectl apply) and Validate (read-only, no cluster access).

def _load_manifest_strict(path):
    """Loads exactly one YAML mapping document from path via a duplicate-key-rejecting loader -- a rendered manifest containing zero, two, or more documents (an adversarial/malformed render) fails closed here, before any structural field is ever inspected."""
    with open(path, "rb") as f:
        raw = f.read()
    try:
        docs = [d for d in yaml.load_all(raw, Loader=_StrictSafeLoader) if d is not None]
    except _DuplicateKeyError as exc:
        raise Phase6Error(f"{path} contains a duplicate YAML mapping key: {exc}") from exc
    except yaml.YAMLError as exc:
        raise Phase6Error(f"{path} is not valid YAML: {exc}") from exc
    if len(docs) != 1:
        raise Phase6Error(f"{path} must contain exactly one YAML document, found {len(docs)}")
    if not isinstance(docs[0], dict):
        raise Phase6Error(f"{path} is a {type(docs[0]).__name__}, expected a YAML mapping.")
    return docs[0]


def _try_parse_embedded_yaml(text):
    """The SecretProviderClass's spec.parameters.objects field is itself a YAML-encoded STRING (the CSI driver's own contract, produced by render_secret_provider_class()'s yaml.safe_dump()) -- this recovers its structure so _assert_no_secret_values() can recurse into it too, instead of treating it as an opaque leaf string."""
    stripped = text.strip()
    if not stripped or (":" not in stripped and not stripped.lstrip().startswith("-")):
        return None
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    return loaded if isinstance(loaded, (dict, list)) else None


def _assert_no_secret_values(obj, path_label):
    """Recursively walks a parsed manifest (including any embedded YAML-string field), failing closed if any mapping KEY is a known secret-VALUE field name (password, aws_secret_access_key, etc.) -- never bans a secret-related field NAME/JMES selector STRING used only as a value, such as OGG_DB_PASSWORD or source-admin-password."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and key.strip().lower() in FORBIDDEN_SECRET_VALUE_KEYS:
                raise Phase6Error(f"{path_label}: forbidden secret-value key {key!r} found in a rendered replication manifest.")
            _assert_no_secret_values(value, path_label)
    elif isinstance(obj, list):
        for item in obj:
            _assert_no_secret_values(item, path_label)
    elif isinstance(obj, str):
        nested = _try_parse_embedded_yaml(obj)
        if nested is not None:
            _assert_no_secret_values(nested, path_label)


def _validate_rendered_manifests(output_dir, expected_namespace):
    """Structurally validates the three rendered replication reconciliation manifests BEFORE any kubectl apply (Deploy) or as the sole check (Validate) -- exactly one SecretProviderClass/ConfigMap/Job, all three sharing the SAME canonical RUNTIME_NAMESPACE and the SAME engine-derived execution resource name, the Job matching the fixed one-container reconciler contract (image/ServiceAccount cross-checked against the SAME plan.json embedded in the ConfigMap, never a second independently-maintained expectation), and no secret VALUE anywhere across all three documents. Returns the shared execution resource name. Never re-implements the engine's own render functions -- reads only the files render-job already wrote to output_dir."""
    spc_path = os.path.join(output_dir, "secretproviderclass.yaml")
    configmap_path = os.path.join(output_dir, "configmap.yaml")
    job_path = os.path.join(output_dir, "job.yaml")

    spc = _load_manifest_strict(spc_path)
    configmap = _load_manifest_strict(configmap_path)
    job = _load_manifest_strict(job_path)

    if spc.get("kind") != "SecretProviderClass":
        raise Phase6Error(f"{spc_path}: expected kind=SecretProviderClass, found {spc.get('kind')!r}")
    if configmap.get("kind") != "ConfigMap":
        raise Phase6Error(f"{configmap_path}: expected kind=ConfigMap, found {configmap.get('kind')!r}")
    if job.get("kind") != "Job" or job.get("apiVersion") != "batch/v1":
        raise Phase6Error(f"{job_path}: expected apiVersion=batch/v1 kind=Job, found apiVersion={job.get('apiVersion')!r} kind={job.get('kind')!r}")

    names = set()
    for label, doc in (("SecretProviderClass", spc), ("ConfigMap", configmap), ("Job", job)):
        metadata = doc.get("metadata") or {}
        namespace = metadata.get("namespace")
        name = metadata.get("name")
        if namespace != expected_namespace:
            raise Phase6Error(f"{label} namespace {namespace!r} does not match canonical RUNTIME_NAMESPACE {expected_namespace!r}")
        if not name:
            raise Phase6Error(f"{label} is missing metadata.name")
        names.add(name)
    if len(names) != 1:
        raise Phase6Error(f"SecretProviderClass/ConfigMap/Job do not share the same execution resource name: {sorted(names)}")
    execution_name = next(iter(names))

    job_spec = job.get("spec") or {}
    if job_spec.get("backoffLimit") != 0:
        raise Phase6Error(f"Job backoffLimit must be exactly 0, found {job_spec.get('backoffLimit')!r}")
    pod_spec = (job_spec.get("template") or {}).get("spec") or {}
    if pod_spec.get("restartPolicy") != "Never":
        raise Phase6Error(f"Job restartPolicy must be exactly 'Never', found {pod_spec.get('restartPolicy')!r}")
    containers = pod_spec.get("containers") or []
    if len(containers) != 1:
        raise Phase6Error(f"Job must have exactly one container, found {len(containers)}")
    container = containers[0]
    if container.get("name") != "reconciler":
        raise Phase6Error(f"Job container name must be exactly 'reconciler', found {container.get('name')!r}")

    configmap_data = configmap.get("data") or {}
    if "goldengate-replication.py" not in configmap_data:
        raise Phase6Error(f"{configmap_path}: ConfigMap data is missing goldengate-replication.py")
    plan_json_text = configmap_data.get("plan.json")
    if not plan_json_text:
        raise Phase6Error(f"{configmap_path}: ConfigMap data is missing plan.json")
    try:
        plan = json.loads(plan_json_text)
    except json.JSONDecodeError as exc:
        raise Phase6Error(f"{configmap_path}: plan.json is not valid JSON: {exc}") from exc

    expected_image = (plan.get("source") or {}).get("image")
    expected_service_account = (plan.get("source") or {}).get("serviceAccount")
    if container.get("image") != expected_image:
        raise Phase6Error(f"Job container image {container.get('image')!r} does not match the canonical replication plan's source image {expected_image!r}")
    if pod_spec.get("serviceAccountName") != expected_service_account:
        raise Phase6Error(f"Job serviceAccountName {pod_spec.get('serviceAccountName')!r} does not match the canonical replication plan's source ServiceAccount {expected_service_account!r}")

    volume_mounts = {vm.get("name"): vm for vm in (container.get("volumeMounts") or [])}
    for mount_name in ("reconciler-script", "replication-secrets"):
        vm = volume_mounts.get(mount_name)
        if vm is None:
            raise Phase6Error(f"Job container is missing the required volumeMount {mount_name!r}")
        if vm.get("readOnly") is not True:
            raise Phase6Error(f"Job volumeMount {mount_name!r} must be readOnly=true")

    volumes = {v.get("name"): v for v in (pod_spec.get("volumes") or [])}
    csi_volume = volumes.get("replication-secrets") or {}
    csi = csi_volume.get("csi")
    if not csi:
        raise Phase6Error("Job is missing the replication-secrets CSI volume")
    if csi.get("readOnly") is not True:
        raise Phase6Error("Job replication-secrets CSI volume must be readOnly=true")
    if (csi.get("volumeAttributes") or {}).get("secretProviderClass") != execution_name:
        raise Phase6Error("Job replication-secrets CSI volume does not reference the execution SecretProviderClass name")

    for doc, doc_path in ((spc, spc_path), (configmap, configmap_path), (job, job_path)):
        _assert_no_secret_values(doc, doc_path)

    return execution_name


# Deploy-mode (live cluster) reconciliation -- collision preflight, apply order, wait contract, success cleanup / failure evidence retention.

def _collision_preflight(execution_name, namespace):
    """Read-only pre-mutation existence check for the three execution-scoped resources -- fails closed if ANY already exists, since the execution identity (pipeline + plan checksum + run/run-attempt) is expected to be unique for this workflow execution; a pre-existing same-name object is treated as a foreign/accidental collision, never silently overwritten. Prints only the resource kind/name -- never the kubectl get output (no -o yaml, no resource dump)."""
    for kind, resource in (("SecretProviderClass", "secretproviderclass"), ("ConfigMap", "configmap"), ("Job", "job")):
        proc = run(["kubectl", "get", resource, execution_name, "-n", namespace], check=False)
        if proc.returncode == 0:
            raise Phase6Error(f"refusing to reconcile: {kind}/{execution_name} already exists in namespace {namespace} -- expected all three execution-scoped resources to be absent before this reconciliation attempt.")


def _apply_manifest(kind_label, path):
    run(["kubectl", "apply", "-f", str(path)])
    print(f"Applied {kind_label}: {path}")


def _wait_for_job(execution_name, namespace):
    proc = run(["kubectl", "wait", "--for=condition=complete", f"--timeout={JOB_WAIT_TIMEOUT}", f"job/{execution_name}", "-n", namespace], check=False)
    return proc.returncode == 0


def _reconcile_one_pipeline(environment, pipeline_id, execution_id, region, namespace):
    output_dir = _pipeline_output_dir(pipeline_id, execution_id)
    _render_pipeline(environment, pipeline_id, execution_id, region, namespace, output_dir)
    execution_name = _validate_rendered_manifests(output_dir, namespace)

    _collision_preflight(execution_name, namespace)

    _apply_manifest("SecretProviderClass", os.path.join(output_dir, "secretproviderclass.yaml"))
    _apply_manifest("ConfigMap", os.path.join(output_dir, "configmap.yaml"))
    _apply_manifest("Job", os.path.join(output_dir, "job.yaml"))

    if not _wait_for_job(execution_name, namespace):
        print(f"FAIL: reconciliation Job {execution_name} for {pipeline_id} did not complete successfully.")
        print("Sanitized Job logs (the worker never prints a mounted secret file):")
        run(["kubectl", "logs", f"job/{execution_name}", "-n", namespace, "--all-containers"], check=False)
        print(f"Job evidence retained for diagnosis: job/{execution_name}, configmap/{execution_name}, secretproviderclass/{execution_name} in {namespace}.")
        raise Phase6Error(f"reconciliation Job {execution_name} for pipeline {pipeline_id} did not complete successfully.")

    # Success cleanup happens ONLY after the Job completed successfully AND sanitized logs were retrieved successfully (check=True below -- a log-retrieval failure here still fails this pipeline BEFORE any cleanup delete runs).
    log_proc = run(["kubectl", "logs", f"job/{execution_name}", "-n", namespace, "--all-containers"])
    print("Sanitized Job logs (the worker never prints a mounted secret file):")
    print(log_proc.stdout)

    run(["kubectl", "delete", "job", execution_name, "-n", namespace, "--ignore-not-found"])
    run(["kubectl", "delete", "configmap", execution_name, "-n", namespace, "--ignore-not-found"])
    run(["kubectl", "delete", "secretproviderclass", execution_name, "-n", namespace, "--ignore-not-found"])
    print(f"OK: {pipeline_id} reconciled successfully; execution-scoped resources {execution_name} cleaned up.")


# CLI

def cmd_discover(args):
    """1) validates the complete folder-driven model; 2) uses the canonical deployment model to determine enabled replication pipeline IDs; 3) prints a concise pipeline inventory; 4) returns clean success (0) when zero pipelines are enabled; 5) writes exactly one fixed GitHub output: has_pipelines=true|false. Never touches AWS/kubectl."""
    pipelines = _discover_pipelines(args.environment)
    if pipelines:
        print(f"Enabled replication pipelines ({len(pipelines)}): {', '.join(pipelines)}")
    else:
        print("No enabled replication pipelines.")
    write_github_output([("has_pipelines", "true" if pipelines else "false")])
    return 0


def cmd_reconcile(args):
    """Deterministically recomputes the enabled pipeline list from the same checkout/model (never accepts it as a caller-supplied argument); a true zero-pipeline no-op returns before ANY AWS credential use / EKS connection / kubectl call. Otherwise connects to EKS once, then reconciles each enabled pipeline deterministically/sequentially/fail-fast -- a failure on one pipeline stops all later pipelines, never runs pipelines in parallel."""
    pipelines = _discover_pipelines(args.environment)
    if not pipelines:
        print("No enabled replication pipeline -- clean no-op: no Job created, no existing runtime changed.")
        return 0

    aws_region = _connect_to_eks()
    namespace = require_env("RUNTIME_NAMESPACE")
    for pipeline_id in pipelines:
        print(f"::group::Reconciling {pipeline_id}")
        _reconcile_one_pipeline(args.environment, pipeline_id, args.execution_id, aws_region, namespace)
        print("::endgroup::")
    return 0


def cmd_validate_local(args):
    """Validate-mode: local/read-only, zero cluster mutation. 1) runs full canonical deployment-model validation; 2) lists canonical enabled replication pipelines; 3) clean no-op when there are none; 4) for each enabled pipeline sequentially, renders via the SAME engine render-job with execution-id=dry-run; 5) structurally validates the exact three rendered manifests using the SAME _validate_rendered_manifests() helper Deploy uses; 6) proves no secret VALUE is embedded; 7) performs ZERO mutation -- no aws command, no kubectl command, no cluster dependency."""
    pipelines = _discover_pipelines(args.environment)
    if not pipelines:
        print("No enabled replication pipelines -- clean no-op.")
        return 0

    namespace = require_env("RUNTIME_NAMESPACE")
    aws_region = require_env("AWS_REGION")
    for pipeline_id in pipelines:
        print(f"::group::Validating {pipeline_id}")
        output_dir = _pipeline_output_dir(pipeline_id, DRY_RUN_EXECUTION_ID)
        _render_pipeline(args.environment, pipeline_id, DRY_RUN_EXECUTION_ID, aws_region, namespace, output_dir)
        _validate_rendered_manifests(output_dir, namespace)
        print(f"OK: {pipeline_id} replication manifests render and validate cleanly (no secret value, no cluster mutation).")
        print("::endgroup::")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="Phase 6 | GoldenGate replication orchestrator (local pipeline discovery, sequential fail-fast reconciliation, local manifest validation).")
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover = subparsers.add_parser("discover")
    discover.add_argument("--environment", required=True)
    discover.set_defaults(func=cmd_discover)

    reconcile = subparsers.add_parser("reconcile")
    reconcile.add_argument("--environment", required=True)
    reconcile.add_argument("--execution-id", required=True)
    reconcile.set_defaults(func=cmd_reconcile)

    validate_local = subparsers.add_parser("validate-local")
    validate_local.add_argument("--environment", required=True)
    validate_local.set_defaults(func=cmd_validate_local)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Phase6Error as exc:
        print(f"FAIL: {exc}")
        return 1
    except subprocess.SubprocessError as exc:
        print(f"FAIL: subprocess execution error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
