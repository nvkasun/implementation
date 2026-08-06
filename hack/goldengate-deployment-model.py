#!/usr/bin/env python3
"""hack/goldengate-deployment-model.py: single source of truth for folder-driven GoldenGate deployment onboarding; scans envs/<environment>/*/values.yaml, validates each descriptor, and derives the inventory consumed by Terraform/monitor/workflows. Never prints secret values, document contents, or raw exception text."""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

import yaml

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

APPROVED_ECR_ACCOUNT = "229410149234"
APPROVED_ECR_REGION = "eu-west-1"
FORBIDDEN_IMAGE_TAG = "latest"

RUNTIME_IDENTITY_MAP = {
    "oracle": "gg-oracle-sa",
    "postgresql": "gg-postgresql-sa",
    "sqlserver": "gg-mssql-sa",
    "distributed": "gg-daa-sa",
}

IGNORED_NON_RUNTIME_FOLDER_NAMES = ("argocd", "goldengate-monitor")

REPLICATION_DISABLED_MESSAGE = (
    "Replication bootstrap activation is not available in Phase 6D0. "
    "Complete the approved database and GoldenGate Admin REST validation phase first."
)

_TOKEN_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*\Z")
_MAX_ID_LENGTH = 63
_MAX_TYPE_LENGTH = 32
_MAX_PIPELINE_LENGTH = 63

_VALID_ROLES = ("source", "target")

_ECR_REPO_SUFFIX_RE = re.compile(r"^[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)*\Z")
_EFS_FILESYSTEM_ID_RE = re.compile(r"^fs-[0-9a-f]+\Z")

_CREDENTIAL_KEY_FRAGMENTS = (
    "password", "passwd", "pwd", "secretvalue", "connectionstring", "conn_str",
    "username", "token", "apikey", "api_key", "dburl", "database_url", "databaseurl",
    "jdbcurl", "jdbc_url",
)


def resolve_admin_secret(environment, role):
    """The one and only admin-secret derivation rule: role alone selects the shared environment-level secret."""
    if role not in _VALID_ROLES:
        raise ValueError(f"invalid role: {role!r}")
    return f"{environment}/goldengate/{role}/admin"


def resolve_tls_secret(environment):
    return f"{environment}/goldengate/tls-certificate"


def resolve_runtime_service_account(deployment_type):
    """The one and only ServiceAccount derivation rule: deployment_type alone selects the approved platform identity."""
    try:
        return RUNTIME_IDENTITY_MAP[deployment_type]
    except KeyError:
        raise DescriptorError(f"Deployment type {deployment_type!r} does not have an approved runtime identity.") from None


def _safe_token(value, max_length):
    if not isinstance(value, str) or not value:
        return False
    if len(value) > max_length:
        return False
    return bool(_TOKEN_RE.match(value))


def _is_literal_bool(value):
    """True only for the literal Python bool type; YAML 1.1 "yes"/"no"/"on"/"off" strings must never pass."""
    return isinstance(value, bool)


class DescriptorError(Exception):
    """A structurally or semantically invalid runtime candidate; .reason is always a fixed, safe-to-print string."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class _StrictLoader(yaml.SafeLoader):
    pass


def _no_duplicate_keys(loader, node):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                None, None, "duplicate key in mapping", key_node.start_mark)
        mapping[key] = loader.construct_object(value_node)
    return mapping


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys)


def load_yaml_strict(path):
    with open(path) as f:
        return yaml.load(f, Loader=_StrictLoader)


def find_values_files(environment):
    pattern = os.path.join(REPO_ROOT, "envs", environment, "*", "values.yaml")
    return sorted(glob.glob(pattern))


def _folder_name(path):
    return os.path.basename(os.path.dirname(path))


def _require_dict(value, reason):
    if not isinstance(value, dict):
        raise DescriptorError(reason)
    return value


def _contains_credential_like_key(node, path=""):
    """Fails closed on usernames/passwords/tokens/connection-strings/database-URLs/API keys; a mere secret-name reference field (e.g. objectName) is never flagged since "secret" alone is not a forbidden fragment."""
    if isinstance(node, dict):
        for key, value in node.items():
            key_lower = str(key).lower()
            if any(fragment in key_lower for fragment in _CREDENTIAL_KEY_FRAGMENTS):
                return True
            if _contains_credential_like_key(value, f"{path}.{key}"):
                return True
    elif isinstance(node, list):
        for item in node:
            if _contains_credential_like_key(item, path):
                return True
    return False


def _parse_image(runtime):
    image = runtime.get("image")
    _require_dict(image, "invalid image configuration: runtime.image must be a mapping")
    repository = image.get("repository")
    tag = image.get("tag")
    if not isinstance(repository, str) or not repository:
        raise DescriptorError("invalid image configuration: runtime.image.repository is required")
    if not isinstance(tag, str) or not tag:
        raise DescriptorError("invalid image configuration: runtime.image.tag is required and must be explicit")
    if tag == FORBIDDEN_IMAGE_TAG:
        raise DescriptorError("invalid image configuration: runtime.image.tag must not be \"latest\"")
    expected_prefix = f"{APPROVED_ECR_ACCOUNT}.dkr.ecr.{APPROVED_ECR_REGION}.amazonaws.com/"
    if not repository.startswith(expected_prefix):
        raise DescriptorError("invalid image configuration: repository is not the approved private ECR account/region")
    suffix = repository[len(expected_prefix):]
    if not suffix:
        raise DescriptorError("invalid image configuration: repository suffix is empty")
    if not _ECR_REPO_SUFFIX_RE.match(suffix):
        raise DescriptorError("invalid image configuration: repository suffix is malformed, contains a digest/tag, whitespace, or traversal")
    return {"repository": repository, "tag": tag}


def _reject_forbidden_overrides(doc):
    """These identities are shared platform invariants, derived once and injected by the deploy workflow; an operator descriptor must never define them."""
    deployment = doc.get("deployment") or {}
    if "adminSecret" in deployment:
        raise DescriptorError("forbidden override: deployment.adminSecret is derived from deployment.role and must not be set")

    runtime = doc.get("runtime") or {}
    if "serviceAccount" in runtime:
        raise DescriptorError("forbidden override: runtime.serviceAccount is derived from runtime.deploymentType and must not be set")
    csi = runtime.get("csi") or {}
    if "serviceAccountRoleArn" in csi:
        raise DescriptorError("forbidden override: runtime.csi.serviceAccountRoleArn is a shared platform invariant and must not be set")
    admin = csi.get("admin") or {}
    if "objectName" in admin:
        raise DescriptorError("forbidden override: runtime.csi.admin.objectName is derived from deployment.role and must not be set")
    certificate = csi.get("certificate") or {}
    if "objectName" in certificate:
        raise DescriptorError("forbidden override: runtime.csi.certificate.objectName is a shared platform invariant and must not be set")


def _parse_replication(deployment_id, doc):
    replication = doc.get("replication")
    if replication is None:
        return {"enabled": False}
    _require_dict(replication, "invalid replication configuration: replication must be a mapping")
    enabled = replication.get("enabled", False)
    if not _is_literal_bool(enabled):
        raise DescriptorError("invalid replication configuration: replication.enabled must be a literal Boolean")
    if _contains_credential_like_key(replication):
        raise DescriptorError("embedded credentials found under replication")
    db_secret = replication.get("databaseCredentialSecret", "")
    if db_secret and not isinstance(db_secret, str):
        raise DescriptorError("invalid replication configuration: databaseCredentialSecret must be a string")
    return {"enabled": enabled}


def _parse_lifecycle(doc):
    lifecycle = doc.get("lifecycle")
    if lifecycle is None:
        return None
    _require_dict(lifecycle, "invalid lifecycle configuration: lifecycle must be a mapping")
    state = lifecycle.get("state")
    if state is not None and state not in ("active", "absent"):
        raise DescriptorError("invalid lifecycle value: lifecycle.state must be \"active\" or \"absent\"")
    return state


def _parse_csi_structure(runtime):
    """Validates the CSI block shape only; objectName/serviceAccountRoleArn presence is rejected earlier by _reject_forbidden_overrides."""
    csi = _require_dict(runtime.get("csi"), "invalid CSI configuration: runtime.csi must be a mapping")
    _require_dict(csi.get("admin"), "invalid CSI configuration: runtime.csi.admin must be a mapping")
    _require_dict(csi.get("certificate"), "invalid CSI configuration: runtime.csi.certificate must be a mapping")


def _parse_efs(doc):
    """Derives EFS/PVC identity for migration safety checks; validated only when persistence is actually enabled."""
    persistence = doc.get("persistence") or {}
    runtime = doc.get("runtime") or {}
    storage = runtime.get("storage") or {}
    u02 = storage.get("u02") or {}
    filesystem_id = (persistence.get("efs") or {}).get("fileSystemId")
    if persistence.get("enabled") is True:
        if not isinstance(filesystem_id, str) or not _EFS_FILESYSTEM_ID_RE.match(filesystem_id):
            raise DescriptorError("invalid persistence configuration: persistence.efs.fileSystemId is not a safe EFS filesystem ID")
    pvc_claim_name = u02.get("claimName") or u02.get("existingClaim") or ""
    return {"fileSystemId": filesystem_id, "pvcClaimName": pvc_claim_name}


def parse_descriptor(deployment_id, environment, doc, shared=None):
    """Fully validates one values.yaml document; raises DescriptorError with a fixed, safe reason on any problem."""
    if shared is None:
        shared = _load_shared_environment_metadata(environment, None, None)

    if not _safe_token(deployment_id, _MAX_ID_LENGTH):
        raise DescriptorError("invalid folder name: deployment ID must be a safe lowercase token")

    if doc.get("deploymentModel") != "singleRuntime":
        raise DescriptorError("missing or invalid deploymentModel: must be exactly \"singleRuntime\"")

    _reject_forbidden_overrides(doc)

    deployment = _require_dict(doc.get("deployment"), "invalid deployment metadata: deployment must be a mapping")
    enabled = deployment.get("enabled")
    if not _is_literal_bool(enabled):
        raise DescriptorError("invalid deployment metadata: deployment.enabled must be a literal Boolean")
    pipeline = deployment.get("pipeline")
    if not _safe_token(pipeline, _MAX_PIPELINE_LENGTH):
        raise DescriptorError("invalid deployment metadata: deployment.pipeline must be a safe non-empty identifier")
    role = deployment.get("role")
    if role not in _VALID_ROLES:
        raise DescriptorError("invalid deployment metadata: deployment.role must be exactly \"source\" or \"target\"")

    runtime = _require_dict(doc.get("runtime"), "invalid deployment metadata: runtime must be a mapping")
    deployment_type = runtime.get("deploymentType")
    if not _safe_token(deployment_type, _MAX_TYPE_LENGTH):
        raise DescriptorError("invalid deployment metadata: runtime.deploymentType must be a safe lowercase token")

    image = _parse_image(runtime)
    runtime_service_account_name = resolve_runtime_service_account(deployment_type)
    admin_secret_name = resolve_admin_secret(environment, role)
    tls_secret_name = resolve_tls_secret(environment)
    _parse_csi_structure(runtime)
    replication = _parse_replication(deployment_id, doc)
    lifecycle_state = _parse_lifecycle(doc)
    efs = _parse_efs(doc)

    global_cfg = _require_dict(doc.get("global"), "invalid deployment metadata: global must be a mapping")
    if global_cfg.get("environment") != environment:
        raise DescriptorError("inconsistent shared environment metadata: global.environment does not match the scanned environment")

    if _contains_credential_like_key(doc):
        raise DescriptorError("embedded credentials found in values.yaml")

    container_name = runtime.get("containerName", deployment_type)
    if not isinstance(container_name, str) or not container_name:
        raise DescriptorError("invalid deployment metadata: runtime.containerName must be a non-empty string")

    ingress = _require_dict(doc.get("ingress"), "invalid deployment metadata: ingress must be a mapping")
    ingress_host = ingress.get("hostDomain")
    if ingress_host != shared["dnsDomain"]:
        raise DescriptorError("inconsistent ingress domain: ingress.hostDomain must match the shared DNS domain")
    alb = ingress.get("alb") or {}
    alb_group_order = alb.get("groupOrder")

    return {
        "deploymentId": deployment_id,
        "environment": environment,
        "pipeline": pipeline,
        "role": role,
        "enabled": enabled,
        "lifecycleState": lifecycle_state,
        "deploymentType": deployment_type,
        "imageRepository": image["repository"],
        "imageTag": image["tag"],
        "containerName": container_name,
        "runtimeServiceAccountName": runtime_service_account_name,
        "adminSecretName": admin_secret_name,
        "tlsSecretName": tls_secret_name,
        "runtimeNamespace": shared["runtimeNamespace"],
        "monitoringNamespace": shared["monitoringNamespace"],
        "ingressHost": ingress_host,
        "efsFileSystemId": efs["fileSystemId"],
        "pvcClaimName": efs["pvcClaimName"],
        "albGroupOrder": alb_group_order,
        "replicationEnabled": replication["enabled"],
    }


def classify_folder(path, environment, shared):
    """Returns (category, descriptor_or_none, reason_or_none). category is one of ignored/inactive/active/invalid."""
    name = _folder_name(path)
    if name in IGNORED_NON_RUNTIME_FOLDER_NAMES:
        return "ignored", None, None

    try:
        doc = load_yaml_strict(path)
    except yaml.YAMLError:
        return "invalid", None, "malformed or duplicate-key YAML"
    except OSError:
        return "invalid", None, "could not read values file"

    if not isinstance(doc, dict):
        return "invalid", None, "document is not a mapping"

    try:
        descriptor = parse_descriptor(name, environment, doc, shared=shared)
    except DescriptorError as exc:
        return "invalid", None, exc.reason

    if descriptor["enabled"] is not True or descriptor["lifecycleState"] == "absent":
        return "inactive", descriptor, None
    return "active", descriptor, None


def scan(environment):
    """Returns (active, inactive, invalid) as (list[descriptor], list[descriptor], list[(path, reason)])."""
    try:
        shared = _load_shared_environment_metadata(environment, None, None)
    except (yaml.YAMLError, OSError) as exc:
        return [], [], [("shared environment metadata", f"could not load shared platform/monitor values: {type(exc).__name__}")]

    active, inactive, invalid = [], [], []
    for path in find_values_files(environment):
        category, descriptor, reason = classify_folder(path, environment, shared)
        if category == "ignored":
            continue
        if category == "invalid":
            invalid.append((path, reason))
        elif category == "inactive":
            inactive.append(descriptor)
        elif category == "active":
            active.append(descriptor)
    return active, inactive, invalid


def validate(environment):
    """Cross-descriptor structural validation; returns a list of fixed-reason problem strings (empty if none)."""
    active, inactive, invalid = scan(environment)
    problems = [f"{path}: {reason}" for path, reason in invalid]

    all_valid = active + inactive
    seen_ids = set()
    for d in all_valid:
        if d["deploymentId"] in seen_ids:
            problems.append(f"duplicate deployment ID: {d['deploymentId']}")
        seen_ids.add(d["deploymentId"])

    for d in all_valid:
        if d["replicationEnabled"]:
            problems.append(f"{d['deploymentId']}: {REPLICATION_DISABLED_MESSAGE}")

    roles_by_pipeline = {}
    alb_orders_seen = {}
    for d in active:
        roles = roles_by_pipeline.setdefault(d["pipeline"], set())
        if d["role"] in roles:
            problems.append(f"pipeline {d['pipeline']!r} has more than one {d['role']} deployment")
        roles.add(d["role"])

        if d["albGroupOrder"] is not None:
            if d["albGroupOrder"] in alb_orders_seen:
                problems.append(f"duplicate ALB group order {d['albGroupOrder']!r} "
                                f"({alb_orders_seen[d['albGroupOrder']]} and {d['deploymentId']})")
            alb_orders_seen[d["albGroupOrder"]] = d["deploymentId"]

    return problems


def build_registry(environment, platform_values_path=None, monitor_values_path=None):
    """Deterministic monitor-compatible registry document; raises if any deployment fails validation."""
    problems = validate(environment)
    if problems:
        raise DescriptorError("; ".join(sorted(problems)))

    active, _inactive, _invalid = scan(environment)
    active_sorted = sorted(active, key=lambda d: d["deploymentId"])

    shared = _load_shared_environment_metadata(environment, platform_values_path, monitor_values_path)

    deployments = [
        {
            "name": d["deploymentId"],
            "type": d["deploymentType"],
            "pipeline": d["pipeline"],
            "role": d["role"],
            "enabled": True,
            "adminSecret": d["adminSecretName"],
        }
        for d in active_sorted
    ]

    return {
        "environment": shared["environment"],
        "runtimeNamespace": shared["runtimeNamespace"],
        "monitoringNamespace": shared["monitoringNamespace"],
        "dnsDomain": shared["dnsDomain"],
        "tlsSecret": shared["tlsSecret"],
        "deployments": deployments,
    }


def _load_shared_environment_metadata(environment, platform_values_path, monitor_values_path):
    platform_values_path = platform_values_path or os.path.join(
        REPO_ROOT, "platform", environment, "goldengate-platform", "values.yaml")
    monitor_values_path = monitor_values_path or os.path.join(
        REPO_ROOT, "envs", environment, "goldengate-monitor", "values.yaml")

    platform_doc = load_yaml_strict(platform_values_path) if os.path.exists(platform_values_path) else {}
    monitor_doc = load_yaml_strict(monitor_values_path) if os.path.exists(monitor_values_path) else {}

    runtime_namespace = ((platform_doc or {}).get("namespaces") or {}).get("runtime", {}).get("name") or f"goldengate-{environment}"
    fluent_bit_namespaces = ((platform_doc or {}).get("fluentBit") or {}).get("namespaces") or {}
    monitoring_namespace = fluent_bit_namespaces.get("monitoring") or "goldengate-monitoring"

    ingress = (monitor_doc or {}).get("ingress") or {}
    monitor_host = ingress.get("host") or ""
    dns_domain = monitor_host.split("monitor.", 1)[-1] if monitor_host.startswith("monitor.") else f"goldengate-{environment}.adcbmis.local"

    return {
        "environment": environment,
        "runtimeNamespace": runtime_namespace,
        "monitoringNamespace": monitoring_namespace,
        "dnsDomain": dns_domain,
        "tlsSecret": resolve_tls_secret(environment),
    }


def _print_reasons(invalid):
    for path, reason in invalid:
        print(f"INVALID: {path}: {reason}")


def _print_problems(problems):
    for problem in sorted(problems):
        print(f"PROBLEM: {problem}")


def _run_full_validation(environment):
    """The single fail-closed gate every output-producing command runs first: no command may emit any part of the inventory while another runtime folder is invalid or a cross-descriptor problem exists."""
    active, inactive, invalid = scan(environment)
    problems = validate(environment)
    return active, inactive, invalid, problems


def cmd_validate(args):
    _active, _inactive, invalid, problems = _run_full_validation(args.environment)
    _print_reasons(invalid)
    _print_problems(problems)
    if invalid or problems:
        return 1
    print(f"OK: {args.environment} deployment descriptors are valid")
    return 0


def cmd_list(args):
    active, inactive, invalid, problems = _run_full_validation(args.environment)
    if invalid or problems:
        _print_reasons(invalid)
        _print_problems(problems)
        print("FAIL: refusing to list a partial inventory while validation problems exist")
        return 1
    for d in sorted(active, key=lambda x: x["deploymentId"]):
        print(f"ACTIVE  {d['deploymentId']} type={d['deploymentType']} role={d['role']} pipeline={d['pipeline']}")
    for d in sorted(inactive, key=lambda x: x["deploymentId"]):
        print(f"INACTIVE {d['deploymentId']} type={d['deploymentType']} role={d['role']} pipeline={d['pipeline']}")
    return 0


def cmd_describe(args):
    active, inactive, invalid, problems = _run_full_validation(args.environment)
    if invalid or problems:
        _print_reasons(invalid)
        _print_problems(problems)
        print("FAIL: refusing to describe a deployment while validation problems exist")
        return 1
    by_id = {d["deploymentId"]: d for d in active + inactive}
    d = by_id.get(args.deployment_id)
    if d is None:
        print(f"FAIL: unknown deployment ID: {args.deployment_id}")
        return 1
    print(json.dumps(d, indent=2, sort_keys=True))
    return 0


def cmd_registry(args):
    try:
        registry = build_registry(args.environment)
    except DescriptorError as exc:
        print(f"FAIL: {exc.reason}")
        return 1
    text = yaml.safe_dump(registry, sort_keys=False, default_flow_style=False)
    if args.output:
        os.makedirs(os.path.dirname(args.output), exist_ok=True)
        with open(args.output, "w") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    return 0


def cmd_shared_secrets(args):
    """The three fixed environment-level secret identifiers only, never values; independent of which deployments exist."""
    _active, _inactive, invalid, problems = _run_full_validation(args.environment)
    if invalid or problems:
        _print_reasons(invalid)
        _print_problems(problems)
        print("FAIL: refusing to list shared secrets while validation problems exist")
        return 1
    print(resolve_admin_secret(args.environment, "source"))
    print(resolve_admin_secret(args.environment, "target"))
    print(resolve_tls_secret(args.environment))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", default="dev")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate").set_defaults(func=cmd_validate)
    sub.add_parser("list").set_defaults(func=cmd_list)

    describe_parser = sub.add_parser("describe")
    describe_parser.add_argument("deployment_id")
    describe_parser.set_defaults(func=cmd_describe)

    registry_parser = sub.add_parser("registry")
    registry_parser.add_argument("--output", default=None)
    registry_parser.set_defaults(func=cmd_registry)

    sub.add_parser("shared-secrets").set_defaults(func=cmd_shared_secrets)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
