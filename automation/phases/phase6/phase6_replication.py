#!/usr/bin/env python3
"""Phase 6A-6B | GoldenGate replication orchestration entrypoint for replication_reconcile_once/replication_dry_run_validation in .github/workflows/00-main-goldengate-orchestrator.yaml; a thin orchestration layer that never reimplements descriptor/pipeline resolution (owned by automation/goldengate-deployment-model.py, invoked here as a subprocess CLI, never a second parser of runtime descriptor YAML) or replication plan/manifest rendering (owned by automation/goldengate-replication.py's build_replication_plan()/render_secret_provider_class()/render_config_map()/render_job(), invoked here only via its own `render-job` CLI, never duplicated). Discovers enabled replication pipelines LOCALLY before any AWS/EKS access so a zero-pipeline run is a true no-op; reconciles enabled pipelines sequentially/fail-fast against a live cluster; validates rendered manifests locally with zero cluster mutation. AWS/Kubernetes credentials are never written to $GITHUB_OUTPUT or logged; only a single fixed has_pipelines=true|false boolean is ever emitted as a GitHub Actions output."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
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
    """Loads exactly one YAML mapping document from path via a duplicate-key-rejecting loader -- a rendered manifest containing zero, two, or more documents (an adversarial/malformed render) fails closed here, before any structural field is ever inspected. A missing/unreadable file (e.g. an engine render that silently omitted one of the three expected manifests) fails closed as Phase6Error too, never an uncaught OSError."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        raise Phase6Error(f"{path} could not be read: {exc}") from exc
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


def _load_canonical_replication_plan(environment, pipeline_id):
    """Obtains the CURRENT canonical replication plan through the ONE existing deployment-model CLI (automation/goldengate-deployment-model.py replication-plan <pipeline_id>) -- never an independent descriptor YAML parser. Requires the command to succeed (via run()'s own check=True), stdout to be valid JSON, the result to be a dict, and plan.pipelineId to equal the requested pipeline_id -- defense against a caller passing a pipeline_id that silently resolves to a different plan."""
    proc = run([sys.executable, str(DEPLOYMENT_MODEL_TOOL), "--environment", environment, "replication-plan", pipeline_id])
    try:
        plan = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Phase6Error(f"canonical replication-plan output for pipeline {pipeline_id!r} is not valid JSON: {exc}") from exc
    if not isinstance(plan, dict):
        raise Phase6Error(f"canonical replication-plan output for pipeline {pipeline_id!r} is a {type(plan).__name__}, expected a JSON object.")
    if plan.get("pipelineId") != pipeline_id:
        raise Phase6Error(f"canonical replication-plan pipelineId {plan.get('pipelineId')!r} does not match the requested pipeline_id {pipeline_id!r}.")
    return plan


def _expected_spc_objects(canonical_plan):
    """Builds the EXPECTED SecretProviderClass object list FROM THE CANONICAL PLAN -- never read from the actual rendered SecretProviderClass -- so a tampered SPC (wrong objectName, extra/missing object, changed alias/JMES path/objectType) can never validate against itself. Mirrors the CURRENT engine's own render_secret_provider_class() object/alias contract exactly: source admin, target admin, source DB, target DB, and TLS -- five objects, in this fixed order."""
    src, tgt = canonical_plan["source"], canonical_plan["target"]
    return [
        {
            "objectName": src["adminSecret"], "objectType": "secretsmanager",
            "jmesPath": [
                {"path": "OGG_ADMIN", "objectAlias": "source-admin-username"},
                {"path": "OGG_ADMIN_PWD", "objectAlias": "source-admin-password"},
            ],
        },
        {
            "objectName": tgt["adminSecret"], "objectType": "secretsmanager",
            "jmesPath": [
                {"path": "OGG_ADMIN", "objectAlias": "target-admin-username"},
                {"path": "OGG_ADMIN_PWD", "objectAlias": "target-admin-password"},
            ],
        },
        {
            "objectName": src["databaseSecret"], "objectType": "secretsmanager",
            "jmesPath": [
                {"path": "OGG_DB_USERID", "objectAlias": "source-db-userid"},
                {"path": "OGG_DB_PASSWORD", "objectAlias": "source-db-password"},
            ],
        },
        {
            "objectName": tgt["databaseSecret"], "objectType": "secretsmanager",
            "jmesPath": [
                {"path": "OGG_DB_USERID", "objectAlias": "target-db-userid"},
                {"path": "OGG_DB_PASSWORD", "objectAlias": "target-db-password"},
            ],
        },
        {
            "objectName": canonical_plan["tlsSecret"], "objectType": "secretsmanager",
            "jmesPath": [{"path": '"ca-chain.pem"', "objectAlias": "tls-ca-chain.pem"}],
        },
    ]


_EXPECTED_JOB_COMMAND = ["python3", "/mnt/reconciler/goldengate-replication.py", "worker",
                         "--plan", "/mnt/reconciler/plan.json", "--secrets-root", "/mnt/replication-secrets"]
_EXPECTED_VOLUME_MOUNT_PATHS = {"reconciler-script": "/mnt/reconciler", "replication-secrets": "/mnt/replication-secrets"}
_EXPECTED_CONFIGMAP_DATA_KEYS = frozenset({"goldengate-replication.py", "plan.json"})


def _render_expected_manifests_for_validation(environment, pipeline_id, execution_id, namespace, region):
    """Independently re-renders the EXPECTED replication manifests through the SAME current trusted engine CLI (`goldengate-replication.py render-job`) -- never a second reimplementation of job_resource_name()/plan_checksum()/render_job()/render_config_map()/render_secret_provider_class() inside this module -- into a FRESH tempfile.TemporaryDirectory(), never the actual output_dir under validation, so this expected render can never overwrite the evidence being validated. Strict-parses the three expected manifests via the SAME duplicate-key-rejecting _load_manifest_strict() used for the actual manifests, keyed by kind. This is the authoritative proof that NO engine-owned field (execution resource name, plan-checksum annotation, TTL, labels, or any future engine-owned field) has drifted from what the current trusted engine would generate for this EXACT environment/pipeline_id/namespace/region/execution_id -- entirely local; no AWS/kubectl/GoldenGate REST/DB access occurs."""
    with tempfile.TemporaryDirectory(prefix="phase6-expected-render-") as expected_dir:
        _render_pipeline(environment, pipeline_id, execution_id, region, namespace, expected_dir)
        return {
            "SecretProviderClass": _load_manifest_strict(os.path.join(expected_dir, "secretproviderclass.yaml")),
            "ConfigMap": _load_manifest_strict(os.path.join(expected_dir, "configmap.yaml")),
            "Job": _load_manifest_strict(os.path.join(expected_dir, "job.yaml")),
        }


def _validate_rendered_manifests(output_dir, environment, pipeline_id, execution_id, expected_namespace, expected_region):
    """Structurally validates the three rendered replication reconciliation manifests BEFORE any kubectl apply (Deploy) or as the sole check (Validate). Beyond kind/namespace/shared-name structure, this is STRONGLY bound to the canonical current replication plan (automation/goldengate-deployment-model.py replication-plan) and the canonical current reconciler source (automation/goldengate-replication.py) -- never inferring trust solely from the rendered files themselves: ConfigMap.data key set is exactly {goldengate-replication.py, plan.json} with plan.json exactly equal to the canonical plan and goldengate-replication.py exactly equal to the current trusted engine source; the SecretProviderClass forbids spec.secretObjects entirely (file-mount-only, never synced into a Kubernetes Secret) and its object/alias list must exactly equal the canonical-plan-derived expectation; the Job forbids env/envFrom entirely and requires the exact fixed worker command, one reconciler container, canonical image/ServiceAccount, and the exact two read-only volumeMounts/volumes. FINAL authoritative proof: environment/pipeline_id/execution_id/expected_namespace/expected_region are used to independently re-render EXPECTED manifests through the SAME current trusted engine CLI (_render_expected_manifests_for_validation()), and each ACTUAL parsed manifest must be exactly dict-equal to its EXPECTED counterpart -- this covers every engine-owned field (execution resource name, plan-checksum annotation, ttlSecondsAfterFinished, labels, and any future field) without Phase 6 ever manually reconstructing job_resource_name()/plan_checksum()'s algorithm. The generic _assert_no_secret_values() scan and the explicit checks above remain defense in depth with clear security-specific error messages; the expected-manifest equality is the FINAL guarantee that no engine-owned field has drifted. Returns the shared execution resource name -- by the time this returns, it has already been proven identical to the current trusted engine's own expected name for this exact pipeline/plan/execution_id. Never re-implements the engine's own render functions -- reads only the files render-job already wrote to output_dir (actual) or to its own fresh temporary directory (expected)."""
    canonical_plan = _load_canonical_replication_plan(environment, pipeline_id)

    spc_path = os.path.join(output_dir, "secretproviderclass.yaml")
    configmap_path = os.path.join(output_dir, "configmap.yaml")
    job_path = os.path.join(output_dir, "job.yaml")

    spc = _load_manifest_strict(spc_path)
    configmap = _load_manifest_strict(configmap_path)
    job = _load_manifest_strict(job_path)

    if spc.get("apiVersion") != "secrets-store.csi.x-k8s.io/v1" or spc.get("kind") != "SecretProviderClass":
        raise Phase6Error(f"{spc_path}: expected apiVersion=secrets-store.csi.x-k8s.io/v1 kind=SecretProviderClass, found apiVersion={spc.get('apiVersion')!r} kind={spc.get('kind')!r}")
    if configmap.get("apiVersion") != "v1" or configmap.get("kind") != "ConfigMap":
        raise Phase6Error(f"{configmap_path}: expected apiVersion=v1 kind=ConfigMap, found apiVersion={configmap.get('apiVersion')!r} kind={configmap.get('kind')!r}")
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

    # ConfigMap contract: exact key set (no fourth "leaked"/"password"/"secret" data key can exist regardless of naming), the embedded reconciler program byte-exact to the CURRENT trusted engine source, and plan.json byte-exact (via parsed dict equality) to the canonical current plan -- never a second, independently mutable source of truth.
    configmap_data = configmap.get("data")
    if not isinstance(configmap_data, dict):
        raise Phase6Error(f"{configmap_path}: ConfigMap data must be a mapping.")
    if set(configmap_data.keys()) != _EXPECTED_CONFIGMAP_DATA_KEYS:
        raise Phase6Error(f"{configmap_path}: ConfigMap data key set must be exactly {sorted(_EXPECTED_CONFIGMAP_DATA_KEYS)}, found {sorted(configmap_data.keys())}.")

    with open(REPLICATION_ENGINE_TOOL, "r", encoding="utf-8") as f:
        trusted_engine_source = f.read()
    if configmap_data["goldengate-replication.py"] != trusted_engine_source:
        raise Phase6Error(f"{configmap_path}: ConfigMap's embedded goldengate-replication.py does not exactly match the current trusted {REPLICATION_ENGINE_TOOL} -- refusing to run an unverified reconciler program while GoldenGate/database credentials are mounted.")

    try:
        rendered_plan = json.loads(configmap_data["plan.json"])
    except json.JSONDecodeError as exc:
        raise Phase6Error(f"{configmap_path}: plan.json is not valid JSON: {exc}") from exc
    if rendered_plan != canonical_plan:
        raise Phase6Error(f"{configmap_path}: plan.json does not exactly match the canonical current replication plan for pipeline {pipeline_id!r} -- refusing to trust a rendered plan that has drifted from `automation/goldengate-deployment-model.py replication-plan`.")

    # SecretProviderClass contract: file-mount-only (spec.secretObjects entirely forbidden), and its object/alias list bound EXACTLY to the canonical plan -- never read from the SPC being validated.
    spc_spec = spc.get("spec")
    if not isinstance(spc_spec, dict):
        raise Phase6Error(f"{spc_path}: spec must be a mapping.")
    if spc_spec.get("provider") != "aws":
        raise Phase6Error(f"{spc_path}: spec.provider must be exactly 'aws', found {spc_spec.get('provider')!r}.")
    if "secretObjects" in spc_spec:
        raise Phase6Error(f"{spc_path}: spec.secretObjects is forbidden -- the replication SecretProviderClass is file-mount-only and must never synchronize a credential into a Kubernetes Secret.")
    parameters = spc_spec.get("parameters")
    if not isinstance(parameters, dict):
        raise Phase6Error(f"{spc_path}: spec.parameters must be a mapping.")
    if parameters.get("region") != expected_region:
        raise Phase6Error(f"{spc_path}: spec.parameters.region {parameters.get('region')!r} does not match the canonical region {expected_region!r}.")
    objects_text = parameters.get("objects")
    if not isinstance(objects_text, str):
        raise Phase6Error(f"{spc_path}: spec.parameters.objects must be a YAML-encoded string.")
    try:
        rendered_objects = yaml.load(objects_text, Loader=_StrictSafeLoader)
    except _DuplicateKeyError as exc:
        raise Phase6Error(f"{spc_path}: spec.parameters.objects contains a duplicate YAML mapping key: {exc}") from exc
    except yaml.YAMLError as exc:
        raise Phase6Error(f"{spc_path}: spec.parameters.objects is not valid YAML: {exc}") from exc
    if rendered_objects != _expected_spc_objects(canonical_plan):
        raise Phase6Error(f"{spc_path}: spec.parameters.objects does not exactly match the canonical-plan-bound expected object list -- refusing an extra/missing secret object, unrelated objectName, or changed alias/JMES path/objectType.")

    # Job contract: exact one-container reconciler shape, canonical image/ServiceAccount, the EXACT fixed worker command (never a different program while credentials are mounted), env/envFrom entirely forbidden (credentials remain mounted files only), and exactly the two expected read-only volumeMounts/volumes.
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

    expected_image = (canonical_plan.get("source") or {}).get("image")
    expected_service_account = (canonical_plan.get("source") or {}).get("serviceAccount")
    if container.get("image") != expected_image:
        raise Phase6Error(f"Job container image {container.get('image')!r} does not match the canonical replication plan's source image {expected_image!r}")
    if pod_spec.get("serviceAccountName") != expected_service_account:
        raise Phase6Error(f"Job serviceAccountName {pod_spec.get('serviceAccountName')!r} does not match the canonical replication plan's source ServiceAccount {expected_service_account!r}")

    if container.get("command") != _EXPECTED_JOB_COMMAND:
        raise Phase6Error(f"Job container command must be exactly {_EXPECTED_JOB_COMMAND}, found {container.get('command')!r} -- refusing to run a different program while credentials are mounted.")
    if container.get("args"):
        raise Phase6Error(f"Job container args must be absent or empty, found {container.get('args')!r}.")
    if container.get("env"):
        raise Phase6Error(f"Job container env must be absent or empty -- credentials remain mounted files only, found {container.get('env')!r}.")
    if container.get("envFrom"):
        raise Phase6Error(f"Job container envFrom must be absent or empty -- credentials remain mounted files only, found {container.get('envFrom')!r}.")

    volume_mounts = container.get("volumeMounts") or []
    mounts_by_name = {vm.get("name"): vm for vm in volume_mounts}
    if set(mounts_by_name) != set(_EXPECTED_VOLUME_MOUNT_PATHS) or len(volume_mounts) != len(_EXPECTED_VOLUME_MOUNT_PATHS):
        raise Phase6Error(f"Job container volumeMounts must be exactly {sorted(_EXPECTED_VOLUME_MOUNT_PATHS)}, found {sorted(mounts_by_name)}.")
    for mount_name, expected_path in _EXPECTED_VOLUME_MOUNT_PATHS.items():
        vm = mounts_by_name[mount_name]
        if vm.get("mountPath") != expected_path:
            raise Phase6Error(f"Job volumeMount {mount_name!r} must mount at {expected_path!r}, found {vm.get('mountPath')!r}.")
        if vm.get("readOnly") is not True:
            raise Phase6Error(f"Job volumeMount {mount_name!r} must be readOnly=true.")

    volumes = pod_spec.get("volumes") or []
    volumes_by_name = {v.get("name"): v for v in volumes}
    if set(volumes_by_name) != {"reconciler-script", "replication-secrets"} or len(volumes) != 2:
        raise Phase6Error(f"Job volumes must be exactly ['reconciler-script', 'replication-secrets'], found {sorted(volumes_by_name)}.")

    reconciler_volume = volumes_by_name["reconciler-script"]
    config_map_ref = reconciler_volume.get("configMap")
    if not isinstance(config_map_ref, dict) or config_map_ref.get("name") != execution_name:
        raise Phase6Error(f"Job volume 'reconciler-script' must be ConfigMap-backed by the execution ConfigMap {execution_name!r}, found {reconciler_volume!r}.")

    secrets_volume = volumes_by_name["replication-secrets"]
    csi = secrets_volume.get("csi")
    if not isinstance(csi, dict):
        raise Phase6Error("Job volume 'replication-secrets' must be CSI-backed.")
    if csi.get("driver") != "secrets-store.csi.k8s.io":
        raise Phase6Error(f"Job replication-secrets CSI volume driver must be exactly 'secrets-store.csi.k8s.io', found {csi.get('driver')!r}.")
    if csi.get("readOnly") is not True:
        raise Phase6Error("Job replication-secrets CSI volume must be readOnly=true.")
    if (csi.get("volumeAttributes") or {}).get("secretProviderClass") != execution_name:
        raise Phase6Error("Job replication-secrets CSI volume does not reference the execution SecretProviderClass name.")

    # Generic secret-value scan remains only as defense in depth -- the schema/binding checks above are the authoritative guarantee for secret-value injection specifically.
    for doc, doc_path in ((spc, spc_path), (configmap, configmap_path), (job, job_path)):
        _assert_no_secret_values(doc, doc_path)

    # FINAL authoritative proof: an independently, freshly re-rendered EXPECTED manifest set from the SAME current trusted engine, for this EXACT environment/pipeline_id/execution_id/namespace/region -- covers every engine-owned field (execution resource name, plan-checksum annotation, ttlSecondsAfterFinished, labels, and any future field) that the explicit checks above do not individually enumerate, without ever manually reconstructing the engine's own naming/checksum algorithm. Parsed-dictionary equality, never raw-YAML-byte comparison (harmless serialization formatting must never fail this) and never a subset/selected-field comparison.
    expected = _render_expected_manifests_for_validation(environment, pipeline_id, execution_id, expected_namespace, expected_region)
    if spc != expected["SecretProviderClass"]:
        raise Phase6Error(f"{spc_path}: rendered SecretProviderClass is not semantically identical to a fresh render from the current trusted engine for environment={environment!r} pipeline_id={pipeline_id!r} execution_id={execution_id!r} -- refusing to trust a drifted manifest.")
    if configmap != expected["ConfigMap"]:
        raise Phase6Error(f"{configmap_path}: rendered ConfigMap is not semantically identical to a fresh render from the current trusted engine for environment={environment!r} pipeline_id={pipeline_id!r} execution_id={execution_id!r} -- refusing to trust a drifted manifest.")
    if job != expected["Job"]:
        raise Phase6Error(f"{job_path}: rendered Job is not semantically identical to a fresh render from the current trusted engine for environment={environment!r} pipeline_id={pipeline_id!r} execution_id={execution_id!r} -- refusing to trust a drifted manifest.")

    # execution_name has now been proven identical to the current trusted engine's own expected name (metadata.name is part of the exact Job/ConfigMap/SecretProviderClass equality just enforced above) -- never independently re-derived here.
    return execution_name


# Deploy-mode (live cluster) reconciliation -- collision preflight, apply order, wait contract, success cleanup / failure evidence retention.

def _require_execution_resource_absent(resource, display_kind, execution_name, namespace):
    """Authoritative existence check for ONE execution-scoped resource: `kubectl get <resource> <execution_name> -n <namespace> --ignore-not-found -o name`, run with check=True -- --ignore-not-found guarantees a genuinely-absent resource always yields exit 0 with EMPTY stdout, so the kubectl command itself failing for ANY reason (Forbidden, Unauthorized, timeout, network/cluster-unreachable, TLS failure, or any other error) is never interpreted as absence -- it fails closed via run()'s own check=True, exactly like a real collision. Only a successful command with empty stdout proves absence; a successful command with non-empty stdout is a genuine pre-existing collision. Prints only the resource kind/name -- never the kubectl get output (no -o yaml, no resource dump)."""
    proc = run(["kubectl", "get", resource, execution_name, "-n", namespace, "--ignore-not-found", "-o", "name"])
    if proc.stdout.strip():
        raise Phase6Error(f"refusing to reconcile: {display_kind}/{execution_name} already exists in namespace {namespace} -- expected all three execution-scoped resources to be absent before this reconciliation attempt.")


def _collision_preflight(execution_name, namespace):
    """Read-only pre-mutation existence check for the three execution-scoped resources -- fails closed (Phase6Error) if ANY already exists, OR if the kubectl inspection command itself fails for any reason; a Kubernetes API inspection error is NEVER treated as proof of absence (see _require_execution_resource_absent())."""
    for display_kind, resource in (("SecretProviderClass", "secretproviderclass"), ("ConfigMap", "configmap"), ("Job", "job")):
        _require_execution_resource_absent(resource, display_kind, execution_name, namespace)


def _apply_manifest(kind_label, path):
    run(["kubectl", "apply", "-f", str(path)])
    print(f"Applied {kind_label}: {path}")


def _wait_for_job(execution_name, namespace):
    proc = run(["kubectl", "wait", "--for=condition=complete", f"--timeout={JOB_WAIT_TIMEOUT}", f"job/{execution_name}", "-n", namespace], check=False)
    return proc.returncode == 0


def _reconcile_one_pipeline(environment, pipeline_id, execution_id, region, namespace):
    output_dir = _pipeline_output_dir(pipeline_id, execution_id)
    _render_pipeline(environment, pipeline_id, execution_id, region, namespace, output_dir)
    execution_name = _validate_rendered_manifests(output_dir, environment, pipeline_id, execution_id, namespace, region)

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
        _validate_rendered_manifests(output_dir, args.environment, pipeline_id, DRY_RUN_EXECUTION_ID, namespace, aws_region)
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
