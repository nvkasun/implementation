#!/usr/bin/env python3
"""hack/goldengate-deployment-model.py: single source of truth for folder-driven GoldenGate deployment onboarding; scans envs/<environment>/*/values.yaml, validates each descriptor, and derives the inventory consumed by Terraform/monitor/workflows. Never prints secret values, document contents, or raw exception text."""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sys

import yaml

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

APPROVED_ECR_ACCOUNT = "229410149234"
APPROVED_ECR_REGION = "eu-west-1"
FORBIDDEN_IMAGE_TAG = "latest"

IGNORED_NON_RUNTIME_FOLDER_NAMES = ("argocd", "goldengate-monitor")

_TOKEN_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*\Z")
_MAX_ID_LENGTH = 63
_MAX_TYPE_LENGTH = 32
_MAX_PIPELINE_LENGTH = 63

_VALID_ROLES = ("source", "target")

_ECR_REPO_SUFFIX_RE = re.compile(r"^[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)*\Z")
_EFS_FILESYSTEM_ID_RE = re.compile(r"^fs-[0-9a-f]+\Z")

_VALID_EFS_MODES = ("managed", "existing")
_EFS_CREATION_TOKEN_MAX_LENGTH = 64

_CREDENTIAL_KEY_FRAGMENTS = (
    "password", "passwd", "pwd", "secretvalue", "connectionstring", "conn_str",
    "username", "token", "apikey", "api_key", "dburl", "database_url", "databaseurl",
    "jdbcurl", "jdbc_url",
)

# Phase 6D1: the only currently approved replication-adapter pipeline shape.
REPLICATION_SUPPORTED_SOURCE_TYPE = "postgresql"
REPLICATION_SUPPORTED_TARGET_TYPE = "mssql"
REPLICATION_SCOPE_MESSAGE = (
    "replication.enabled=true is only supported for a postgresql source paired with an mssql target."
)
REPLICATION_SUPPORTED_PLUGIN_TYPES = ("pgoutput", "test_decoding")
REPLICATION_SUPPORTED_PROTOCOL = "wss"
REPLICATION_SUPPORTED_PORT = 443
REPLICATION_SUPPORTED_REPLICAT_MODE_TYPE = "nonintegrated"

_PROCESS_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_$]{0,7}\Z")
_TRAIL_NAME_RE = re.compile(r"^[a-z][a-z0-9]\Z")
_PATH_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,31}\Z")
_CREDENTIAL_DOMAIN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,29}\Z")
_DB_ALIAS_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,29}\Z")
_TABLE_IDENTIFIER_PART_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")
_MAX_TABLE_IDENTIFIER_LENGTH = 128
_FORBIDDEN_IDENTIFIER_FRAGMENTS = (";", "'", '"', "--", "/*", "*/")

_SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9/_.+=@-]+\Z")


def _valid_process_name(value):
    """Extract/Replicat name: uppercase, first char alphabetic, max 8 chars, no whitespace/slash/control chars."""
    return isinstance(value, str) and bool(_PROCESS_NAME_RE.match(value))


def _valid_trail_name(value):
    """Trail name: exactly two lowercase characters, first alphabetic, second alphanumeric; never auto-normalized."""
    return isinstance(value, str) and bool(_TRAIL_NAME_RE.match(value))


def _valid_path_name(value):
    """Distribution path name: 1-32 chars, first alphabetic, remainder letters/digits/dash/underscore/period."""
    return isinstance(value, str) and bool(_PATH_NAME_RE.match(value))


def _valid_credential_domain(value):
    return isinstance(value, str) and bool(_CREDENTIAL_DOMAIN_RE.match(value))


def _valid_derived_alias(value):
    return isinstance(value, str) and bool(_DB_ALIAS_RE.match(value))


def _valid_table_identifier(value, allow_wildcard=True):
    """schema.table or schema.* only; rejects semicolons, quotes, comment markers, control characters."""
    if not isinstance(value, str) or not value:
        return False
    if len(value) > _MAX_TABLE_IDENTIFIER_LENGTH:
        return False
    if any(ord(c) < 0x20 for c in value):
        return False
    if any(fragment in value for fragment in _FORBIDDEN_IDENTIFIER_FRAGMENTS):
        return False
    parts = value.split(".")
    if len(parts) != 2:
        return False
    schema, table = parts
    if not _TABLE_IDENTIFIER_PART_RE.match(schema):
        return False
    if table == "*":
        return allow_wildcard
    return bool(_TABLE_IDENTIFIER_PART_RE.match(table))


def _valid_database_credential_secret(value, environment):
    """dev/goldengate/... shared-secret-style reference only; never an ARN, traversal, or whitespace/control char."""
    if not isinstance(value, str) or not value:
        return False
    if not value.startswith(f"{environment}/goldengate/"):
        return False
    if ".." in value:
        return False
    if any(ch.isspace() for ch in value):
        return False
    if any(ord(c) < 0x20 for c in value):
        return False
    if value.startswith("arn:"):
        return False
    return bool(_SECRET_NAME_RE.match(value))


def derive_database_credential_alias(deployment_id):
    """Deterministic, collision-tested-by-caller Oracle credential alias; never derived from or containing the DB username."""
    normalized = re.sub(r"[^A-Za-z0-9]", "_", deployment_id).upper()
    digest = hashlib.sha256(deployment_id.encode()).hexdigest()[:6].upper()
    suffix = f"_{digest}"
    prefix = normalized[: 30 - len(suffix)]
    if not prefix or not prefix[0].isalpha():
        prefix = "D" + prefix[: 30 - len(suffix) - 1]
    return f"{prefix}{suffix}"


def derive_network_credential_alias(source_deployment_id, target_deployment_id):
    """Deterministic Network-domain credential alias derived from the source/target deployment ID pair."""
    combined = f"{source_deployment_id}:{target_deployment_id}"
    digest = hashlib.sha256(combined.encode()).hexdigest()[:12].upper()
    return f"NET_{digest}"


def resolve_admin_secret(environment, role):
    """The one and only admin-secret derivation rule: role alone selects the shared environment-level secret."""
    if role not in _VALID_ROLES:
        raise ValueError(f"invalid role: {role!r}")
    return f"{environment}/goldengate/{role}/admin"


def resolve_tls_secret(environment):
    return f"{environment}/goldengate/tls-certificate"


def resolve_runtime_service_account(deployment_type):
    """The one and only ServiceAccount derivation rule: every singleRuntime deployment shares the platform-owned gg-runtime-sa identity, regardless of deployment_type -- deploymentType controls image/product/ports/replication semantics, never AWS runtime identity. The parameter is kept (rather than removed) so call sites stay symmetric with the rest of the resolve_* family and so a future per-type override would be a single, obvious change point."""
    return "gg-runtime-sa"


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
        raise DescriptorError("forbidden override: runtime.serviceAccount is a shared platform invariant (gg-runtime-sa for every deploymentType) and must not be set")
    csi = runtime.get("csi") or {}
    if "serviceAccountRoleArn" in csi:
        raise DescriptorError("forbidden override: runtime.csi.serviceAccountRoleArn is a shared platform invariant and must not be set")
    admin = csi.get("admin") or {}
    if "objectName" in admin:
        raise DescriptorError("forbidden override: runtime.csi.admin.objectName is derived from deployment.role and must not be set")
    certificate = csi.get("certificate") or {}
    if "objectName" in certificate:
        raise DescriptorError("forbidden override: runtime.csi.certificate.objectName is a shared platform invariant and must not be set")


def _parse_supplemental_logging(block):
    if block is None:
        return {"enabled": False, "mode": "none", "objects": []}
    _require_dict(block, "invalid replication configuration: replication.supplementalLogging must be a mapping")
    enabled = block.get("enabled", False)
    if not _is_literal_bool(enabled):
        raise DescriptorError("invalid replication configuration: replication.supplementalLogging.enabled must be a literal Boolean")
    mode = block.get("mode", "none")
    if mode not in ("table", "none"):
        raise DescriptorError("invalid replication configuration: replication.supplementalLogging.mode must be \"table\" or \"none\"")
    objects = block.get("objects", [])
    if not isinstance(objects, list):
        raise DescriptorError("invalid replication configuration: replication.supplementalLogging.objects must be a list")
    for obj in objects:
        if not _valid_table_identifier(obj, allow_wildcard=False):
            raise DescriptorError("invalid replication configuration: replication.supplementalLogging.objects entry is not a safe schema.table identifier")
    if enabled and (mode != "table" or not objects):
        raise DescriptorError("invalid replication configuration: replication.supplementalLogging.enabled=true requires mode=table and a non-empty objects list")
    return {"enabled": enabled, "mode": mode, "objects": list(objects)}


def _parse_trail(block, field_name):
    _require_dict(block, f"invalid replication configuration: {field_name} must be a mapping")
    name = block.get("name")
    if not _valid_trail_name(name):
        raise DescriptorError(f"invalid replication configuration: {field_name}.name must be a safe two-character lowercase trail name")
    size_mb = block.get("sizeMB")
    if not isinstance(size_mb, int) or isinstance(size_mb, bool) or size_mb <= 0:
        raise DescriptorError(f"invalid replication configuration: {field_name}.sizeMB must be a positive integer")
    subdirectory = block.get("subdirectory", "")
    if not isinstance(subdirectory, str):
        raise DescriptorError(f"invalid replication configuration: {field_name}.subdirectory must be a string")
    return {"name": name, "sizeMB": size_mb, "subdirectory": subdirectory}


def _parse_extract(block):
    if block is None:
        block = {}
    _require_dict(block, "invalid replication configuration: replication.extract must be a mapping")
    enabled = block.get("enabled", False)
    if not _is_literal_bool(enabled):
        raise DescriptorError("invalid replication configuration: replication.extract.enabled must be a literal Boolean")
    if not enabled:
        return {"enabled": False}

    name = block.get("name")
    if not _valid_process_name(name):
        raise DescriptorError("invalid replication configuration: replication.extract.name must be a valid Extract name")
    description = block.get("description", "")
    if not isinstance(description, str):
        raise DescriptorError("invalid replication configuration: replication.extract.description must be a string")
    plugin_type = block.get("pluginType")
    if plugin_type not in REPLICATION_SUPPORTED_PLUGIN_TYPES:
        raise DescriptorError("invalid replication configuration: replication.extract.pluginType must be explicitly set to \"pgoutput\" or \"test_decoding\"")
    begin = block.get("begin")
    if not isinstance(begin, str) or not begin:
        raise DescriptorError("invalid replication configuration: replication.extract.begin must be a non-empty string")
    trail = _parse_trail(block.get("trail") or {}, "replication.extract.trail")
    tables = block.get("tables")
    if not isinstance(tables, list) or not tables:
        raise DescriptorError("invalid replication configuration: replication.extract.tables must be a non-empty list")
    for table in tables:
        if not _valid_table_identifier(table):
            raise DescriptorError("invalid replication configuration: replication.extract.tables entry is not a safe schema.table identifier")
    start_on_create = block.get("startOnCreate", False)
    if not _is_literal_bool(start_on_create):
        raise DescriptorError("invalid replication configuration: replication.extract.startOnCreate must be a literal Boolean")

    return {
        "enabled": True, "name": name, "description": description, "pluginType": plugin_type,
        "begin": begin, "trail": trail, "tables": list(tables), "startOnCreate": start_on_create,
    }


def _parse_distribution(block):
    if block is None:
        block = {}
    _require_dict(block, "invalid replication configuration: replication.distribution must be a mapping")
    enabled = block.get("enabled", False)
    if not _is_literal_bool(enabled):
        raise DescriptorError("invalid replication configuration: replication.distribution.enabled must be a literal Boolean")
    if not enabled:
        return {"enabled": False}

    path_name = block.get("pathName")
    if not _valid_path_name(path_name):
        raise DescriptorError("invalid replication configuration: replication.distribution.pathName must be a valid path name")
    target_deployment = block.get("targetDeployment")
    if not _safe_token(target_deployment, _MAX_ID_LENGTH):
        raise DescriptorError("invalid replication configuration: replication.distribution.targetDeployment must be a safe deployment ID")
    source_trail_name = block.get("sourceTrailName")
    if not _valid_trail_name(source_trail_name):
        raise DescriptorError("invalid replication configuration: replication.distribution.sourceTrailName must be a valid trail name")
    target_trail_name = block.get("targetTrailName")
    if not _valid_trail_name(target_trail_name):
        raise DescriptorError("invalid replication configuration: replication.distribution.targetTrailName must be a valid trail name")
    if source_trail_name == target_trail_name:
        raise DescriptorError("invalid replication configuration: replication.distribution.sourceTrailName and targetTrailName must not collide")
    protocol = block.get("protocol")
    if protocol != REPLICATION_SUPPORTED_PROTOCOL:
        raise DescriptorError("invalid replication configuration: replication.distribution.protocol must be \"wss\"")
    port = block.get("port")
    if port != REPLICATION_SUPPORTED_PORT:
        raise DescriptorError("invalid replication configuration: replication.distribution.port must be 443")
    start_on_create = block.get("startOnCreate", False)
    if not _is_literal_bool(start_on_create):
        raise DescriptorError("invalid replication configuration: replication.distribution.startOnCreate must be a literal Boolean")

    return {
        "enabled": True, "pathName": path_name, "targetDeployment": target_deployment,
        "sourceTrailName": source_trail_name, "targetTrailName": target_trail_name,
        "protocol": protocol, "port": port, "startOnCreate": start_on_create,
    }


def _parse_checkpoint(block):
    if block is None:
        block = {}
    _require_dict(block, "invalid replication configuration: replication.checkpoint must be a mapping")
    enabled = block.get("enabled", False)
    if not _is_literal_bool(enabled):
        raise DescriptorError("invalid replication configuration: replication.checkpoint.enabled must be a literal Boolean")
    if not enabled:
        return {"enabled": False}

    table = block.get("table")
    if not _valid_table_identifier(table, allow_wildcard=False):
        raise DescriptorError("invalid replication configuration: replication.checkpoint.table must be a safe schema.table identifier")
    create_if_missing = block.get("createIfMissing", False)
    if not _is_literal_bool(create_if_missing):
        raise DescriptorError("invalid replication configuration: replication.checkpoint.createIfMissing must be a literal Boolean")

    return {"enabled": True, "table": table, "createIfMissing": create_if_missing}


def _parse_replicat(block):
    if block is None:
        block = {}
    _require_dict(block, "invalid replication configuration: replication.replicat must be a mapping")
    enabled = block.get("enabled", False)
    if not _is_literal_bool(enabled):
        raise DescriptorError("invalid replication configuration: replication.replicat.enabled must be a literal Boolean")
    if not enabled:
        return {"enabled": False}

    name = block.get("name")
    if not _valid_process_name(name):
        raise DescriptorError("invalid replication configuration: replication.replicat.name must be a valid Replicat name")
    description = block.get("description", "")
    if not isinstance(description, str):
        raise DescriptorError("invalid replication configuration: replication.replicat.description must be a string")
    source_trail_name = block.get("sourceTrailName")
    if not _valid_trail_name(source_trail_name):
        raise DescriptorError("invalid replication configuration: replication.replicat.sourceTrailName must be a valid trail name")
    begin = block.get("begin")
    if not isinstance(begin, str) or not begin:
        raise DescriptorError("invalid replication configuration: replication.replicat.begin must be a non-empty string")

    mode = _require_dict(block.get("mode"), "invalid replication configuration: replication.replicat.mode must be a mapping")
    mode_type = mode.get("type")
    if mode_type != REPLICATION_SUPPORTED_REPLICAT_MODE_TYPE:
        raise DescriptorError("invalid replication configuration: replication.replicat.mode.type must be \"nonintegrated\"")
    parallel = mode.get("parallel", False)
    if parallel is not False:
        raise DescriptorError("invalid replication configuration: replication.replicat.mode.parallel must be the literal false")

    mappings = block.get("mappings")
    if not isinstance(mappings, list) or not mappings:
        raise DescriptorError("invalid replication configuration: replication.replicat.mappings must be a non-empty list")
    normalized_mappings = []
    for mapping in mappings:
        _require_dict(mapping, "invalid replication configuration: replication.replicat.mappings entry must be a mapping")
        source = mapping.get("source")
        target = mapping.get("target")
        if not _valid_table_identifier(source, allow_wildcard=False):
            raise DescriptorError("invalid replication configuration: replication.replicat.mappings source is not a safe schema.table identifier")
        if not _valid_table_identifier(target, allow_wildcard=False):
            raise DescriptorError("invalid replication configuration: replication.replicat.mappings target is not a safe schema.table identifier")
        normalized_mappings.append({"source": source, "target": target})
    start_on_create = block.get("startOnCreate", False)
    if not _is_literal_bool(start_on_create):
        raise DescriptorError("invalid replication configuration: replication.replicat.startOnCreate must be a literal Boolean")

    return {
        "enabled": True, "name": name, "description": description, "sourceTrailName": source_trail_name,
        "begin": begin, "mode": {"type": mode_type, "parallel": False},
        "mappings": normalized_mappings, "startOnCreate": start_on_create,
    }


def _parse_replication(deployment_id, environment, role, deployment_type, doc):
    """Full Phase 6D1 replication schema; a disabled or absent block always normalizes to {"enabled": False}."""
    replication = doc.get("replication")
    if replication is None:
        return {"enabled": False}
    _require_dict(replication, "invalid replication configuration: replication must be a mapping")
    enabled = replication.get("enabled", False)
    if not _is_literal_bool(enabled):
        raise DescriptorError("invalid replication configuration: replication.enabled must be a literal Boolean")
    if not enabled:
        return {"enabled": False}

    if role == "source" and deployment_type != REPLICATION_SUPPORTED_SOURCE_TYPE:
        raise DescriptorError(f"unsupported replication scope: {REPLICATION_SCOPE_MESSAGE}")
    if role == "target" and deployment_type != REPLICATION_SUPPORTED_TARGET_TYPE:
        raise DescriptorError(f"unsupported replication scope: {REPLICATION_SCOPE_MESSAGE}")

    if _contains_credential_like_key(replication):
        raise DescriptorError("embedded credentials found under replication")

    db_secret = replication.get("databaseCredentialSecret")
    if not _valid_database_credential_secret(db_secret, environment):
        raise DescriptorError("invalid replication configuration: replication.databaseCredentialSecret must be a safe, environment-scoped dev/goldengate/... secret name")

    db_credential = _require_dict(replication.get("databaseCredential"), "invalid replication configuration: replication.databaseCredential must be a mapping")
    domain = db_credential.get("domain")
    if not _valid_credential_domain(domain):
        raise DescriptorError("invalid replication configuration: replication.databaseCredential.domain must be a safe Oracle credential-domain token")

    supplemental_logging = _parse_supplemental_logging(replication.get("supplementalLogging"))
    extract = _parse_extract(replication.get("extract"))
    distribution = _parse_distribution(replication.get("distribution"))
    checkpoint = _parse_checkpoint(replication.get("checkpoint"))
    replicat = _parse_replicat(replication.get("replicat"))

    database_alias = derive_database_credential_alias(deployment_id)
    if not _valid_derived_alias(database_alias):
        raise DescriptorError("internal error: derived database credential alias is not valid")

    return {
        "enabled": True,
        "databaseCredentialSecret": db_secret,
        "databaseCredential": {"domain": domain},
        "databaseCredentialAlias": database_alias,
        "supplementalLogging": supplemental_logging,
        "extract": extract,
        "distribution": distribution,
        "checkpoint": checkpoint,
        "replicat": replicat,
    }


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


def derive_efs_creation_token(environment, deployment_id):
    """Deterministic managed-EFS identity; fails closed rather than silently truncating or hashing the deployment ID."""
    token = f"{environment}-{deployment_id}-efs"
    if len(token) > _EFS_CREATION_TOKEN_MAX_LENGTH:
        raise DescriptorError(f"invalid persistence configuration: derived EFS creation token exceeds the {_EFS_CREATION_TOKEN_MAX_LENGTH}-character AWS limit")
    return token


def _parse_efs(deployment_id, environment, doc):
    """Existing mode passes through an operator-supplied fileSystemId; managed mode derives a creation token and forbids a committed ID."""
    persistence = doc.get("persistence")
    if persistence is not None:
        _require_dict(persistence, "invalid persistence configuration: persistence must be a mapping")
    persistence = persistence or {}

    runtime = doc.get("runtime") or {}
    storage = runtime.get("storage") or {}
    u02 = storage.get("u02") or {}
    pvc_claim_name = u02.get("claimName") or u02.get("existingClaim") or ""

    if "enabled" in persistence and not _is_literal_bool(persistence.get("enabled")):
        raise DescriptorError("invalid persistence configuration: persistence.enabled must be a literal Boolean")

    efs_enabled = persistence.get("enabled") is True and persistence.get("provider") == "efs"
    if not efs_enabled:
        return {"mode": None, "fileSystemId": None, "creationToken": None, "pvcClaimName": pvc_claim_name}

    if u02.get("type") != "efs":
        raise DescriptorError("invalid persistence configuration: runtime.storage.u02.type must be \"efs\" when persistence.enabled=true and provider=efs")

    efs = _require_dict(persistence.get("efs"), "invalid persistence configuration: persistence.efs must be a mapping when persistence.enabled=true and provider=efs")
    mode = efs.get("mode")
    if mode not in _VALID_EFS_MODES:
        raise DescriptorError("invalid persistence configuration: persistence.efs.mode must be explicitly \"managed\" or \"existing\"")

    filesystem_id = efs.get("fileSystemId")
    if mode == "existing":
        if not isinstance(filesystem_id, str) or not _EFS_FILESYSTEM_ID_RE.match(filesystem_id):
            raise DescriptorError("invalid persistence configuration: persistence.efs.fileSystemId is not a safe EFS filesystem ID")
        return {"mode": mode, "fileSystemId": filesystem_id, "creationToken": None, "pvcClaimName": pvc_claim_name}

    if filesystem_id not in (None, ""):
        raise DescriptorError("invalid persistence configuration: persistence.efs.fileSystemId must not be set when persistence.efs.mode=managed -- Terraform provisions and resolves it")
    creation_token = derive_efs_creation_token(environment, deployment_id)
    return {"mode": mode, "fileSystemId": None, "creationToken": creation_token, "pvcClaimName": pvc_claim_name}


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
    replication = _parse_replication(deployment_id, environment, role, deployment_type, doc)
    lifecycle_state = _parse_lifecycle(doc)
    efs = _parse_efs(deployment_id, environment, doc)

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
        "efsMode": efs["mode"],
        "efsFileSystemId": efs["fileSystemId"],
        "efsCreationToken": efs["creationToken"],
        "pvcClaimName": efs["pvcClaimName"],
        "albGroupOrder": alb_group_order,
        "replicationEnabled": replication["enabled"],
        "replication": replication,
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


def _validate_replication_pipelines(active):
    """Task 4: full cross-runtime pipeline contract for every pipeline with at least one replication-enabled member."""
    problems = []
    by_pipeline = {}
    for d in active:
        by_pipeline.setdefault(d["pipeline"], []).append(d)

    for pipeline, members in sorted(by_pipeline.items()):
        if not any(d["replicationEnabled"] for d in members):
            continue

        sources = [d for d in members if d["role"] == "source"]
        targets = [d for d in members if d["role"] == "target"]
        if len(sources) != 1:
            problems.append(f"pipeline {pipeline!r}: replication requires exactly one active source deployment")
            continue
        if len(targets) != 1:
            problems.append(f"pipeline {pipeline!r}: replication requires exactly one active target deployment")
            continue

        source, target = sources[0], targets[0]
        if not source["replicationEnabled"] or not target["replicationEnabled"]:
            problems.append(f"pipeline {pipeline!r}: both the source and target deployment must have replication.enabled=true")
            continue

        if source["deploymentType"] != REPLICATION_SUPPORTED_SOURCE_TYPE or target["deploymentType"] != REPLICATION_SUPPORTED_TARGET_TYPE:
            problems.append(f"pipeline {pipeline!r}: {REPLICATION_SCOPE_MESSAGE}")
            continue

        src_repl, tgt_repl = source["replication"], target["replication"]
        role_problems = []
        if not src_repl["extract"]["enabled"]:
            role_problems.append(f"pipeline {pipeline!r}: source deployment must have replication.extract.enabled=true")
        if not src_repl["distribution"]["enabled"]:
            role_problems.append(f"pipeline {pipeline!r}: source deployment must have replication.distribution.enabled=true")
        if src_repl["replicat"]["enabled"]:
            role_problems.append(f"pipeline {pipeline!r}: source deployment must have replication.replicat.enabled=false")
        if src_repl["checkpoint"]["enabled"]:
            role_problems.append(f"pipeline {pipeline!r}: source deployment must have replication.checkpoint.enabled=false")
        if tgt_repl["extract"]["enabled"]:
            role_problems.append(f"pipeline {pipeline!r}: target deployment must have replication.extract.enabled=false")
        if tgt_repl["distribution"]["enabled"]:
            role_problems.append(f"pipeline {pipeline!r}: target deployment must have replication.distribution.enabled=false")
        if not tgt_repl["checkpoint"]["enabled"]:
            role_problems.append(f"pipeline {pipeline!r}: target deployment must have replication.checkpoint.enabled=true")
        if not tgt_repl["replicat"]["enabled"]:
            role_problems.append(f"pipeline {pipeline!r}: target deployment must have replication.replicat.enabled=true")
        problems.extend(role_problems)
        if role_problems:
            continue

        extract, distribution = src_repl["extract"], src_repl["distribution"]
        checkpoint, replicat = tgt_repl["checkpoint"], tgt_repl["replicat"]

        if distribution["targetDeployment"] != target["deploymentId"]:
            problems.append(f"pipeline {pipeline!r}: replication.distribution.targetDeployment must equal the target deployment ID")
        if distribution["sourceTrailName"] != extract["trail"]["name"]:
            problems.append(f"pipeline {pipeline!r}: replication.distribution.sourceTrailName must equal replication.extract.trail.name")
        if distribution["targetTrailName"] != replicat["sourceTrailName"]:
            problems.append(f"pipeline {pipeline!r}: replication.distribution.targetTrailName must equal the target replication.replicat.sourceTrailName")

        extract_tables = set(extract["tables"])
        supplemental_objects = set(src_repl["supplementalLogging"]["objects"])
        if extract_tables - supplemental_objects:
            problems.append(f"pipeline {pipeline!r}: replication.supplementalLogging.objects does not cover every replication.extract.tables entry")

        mapping_sources = {m["source"] for m in replicat["mappings"]}
        if mapping_sources - extract_tables:
            problems.append(f"pipeline {pipeline!r}: every replication.replicat.mappings source must exist in the source replication.extract.tables inventory")

    return problems


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

    problems.extend(_validate_replication_pipelines(active))

    alias_owners = {}
    for d in active:
        if not d["replicationEnabled"]:
            continue
        alias = d["replication"]["databaseCredentialAlias"]
        if alias in alias_owners and alias_owners[alias] != d["deploymentId"]:
            problems.append(f"derived database credential alias collision between {alias_owners[alias]!r} and {d['deploymentId']!r}")
        alias_owners[alias] = d["deploymentId"]

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

    efs_token_owners = {}
    for d in all_valid:
        token = d.get("efsCreationToken")
        if not token:
            continue
        if token in efs_token_owners and efs_token_owners[token] != d["deploymentId"]:
            problems.append(f"managed EFS creation token collision between {efs_token_owners[token]!r} "
                            f"and {d['deploymentId']!r}: {token!r}")
        efs_token_owners[token] = d["deploymentId"]

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


def runtime_identity_inventory(active):
    """Unique enabled deployment types, sorted deterministically, each mapped to its derived ServiceAccount."""
    types = sorted({d["deploymentType"] for d in active})
    return [(t, resolve_runtime_service_account(t)) for t in types]


def cmd_runtime_identities(args):
    active, _inactive, invalid, problems = _run_full_validation(args.environment)
    if invalid or problems:
        _print_reasons(invalid)
        _print_problems(problems)
        print("FAIL: refusing to list runtime identities while validation problems exist")
        return 1
    for deployment_type, service_account_name in runtime_identity_inventory(active):
        print(f"{deployment_type},{service_account_name}")
    return 0


def replication_pipeline_ids(active):
    """Sorted pipeline IDs where an active source and an active target both have replication.enabled=true."""
    by_pipeline = {}
    for d in active:
        by_pipeline.setdefault(d["pipeline"], []).append(d)
    result = []
    for pipeline, members in sorted(by_pipeline.items()):
        if (len(members) == 2 and {m["role"] for m in members} == {"source", "target"}
                and all(m["replicationEnabled"] for m in members)):
            result.append(pipeline)
    return result


def find_replication_pipeline(active, pipeline_id):
    members = [d for d in active if d["pipeline"] == pipeline_id]
    source = next((d for d in members if d["role"] == "source"), None)
    target = next((d for d in members if d["role"] == "target"), None)
    return source, target


def build_replication_plan(source, target):
    """Sanitized desired-state plan for one replication pipeline; contains no username, password, or secret value."""
    dns_domain = source["ingressHost"]
    src_repl, tgt_repl = source["replication"], target["replication"]

    return {
        "pipelineId": source["pipeline"],
        "tlsSecret": source["tlsSecretName"],
        "source": {
            "deploymentId": source["deploymentId"],
            "deploymentType": source["deploymentType"],
            "runtimeHost": f"{source['deploymentId']}.{dns_domain}",
            "serviceAccount": source["runtimeServiceAccountName"],
            "image": f"{source['imageRepository']}:{source['imageTag']}",
            "adminSecret": source["adminSecretName"],
            "databaseSecret": src_repl["databaseCredentialSecret"],
            "databaseCredentialAlias": src_repl["databaseCredentialAlias"],
            "databaseCredentialDomain": src_repl["databaseCredential"]["domain"],
        },
        "target": {
            "deploymentId": target["deploymentId"],
            "deploymentType": target["deploymentType"],
            "runtimeHost": f"{target['deploymentId']}.{dns_domain}",
            "serviceAccount": target["runtimeServiceAccountName"],
            "image": f"{target['imageRepository']}:{target['imageTag']}",
            "adminSecret": target["adminSecretName"],
            "databaseSecret": tgt_repl["databaseCredentialSecret"],
            "databaseCredentialAlias": tgt_repl["databaseCredentialAlias"],
            "databaseCredentialDomain": tgt_repl["databaseCredential"]["domain"],
        },
        "networkCredentialAlias": derive_network_credential_alias(source["deploymentId"], target["deploymentId"]),
        "networkCredentialDomain": "Network",
        "supplementalLogging": src_repl["supplementalLogging"],
        "extract": src_repl["extract"],
        "distribution": src_repl["distribution"],
        "checkpoint": tgt_repl["checkpoint"],
        "replicat": tgt_repl["replicat"],
        "receiver": {
            "targetDeployment": target["deploymentId"],
            "expectedTrail": tgt_repl["replicat"]["sourceTrailName"],
        },
    }


def cmd_replication_pipelines(args):
    active, _inactive, invalid, problems = _run_full_validation(args.environment)
    if invalid or problems:
        _print_reasons(invalid)
        _print_problems(problems)
        print("FAIL: refusing to list replication pipelines while validation problems exist")
        return 1
    for pipeline_id in replication_pipeline_ids(active):
        print(pipeline_id)
    return 0


def cmd_replication_plan(args):
    active, _inactive, invalid, problems = _run_full_validation(args.environment)
    if invalid or problems:
        _print_reasons(invalid)
        _print_problems(problems)
        print("FAIL: refusing to build a replication plan while validation problems exist")
        return 1
    if args.pipeline_id not in replication_pipeline_ids(active):
        print(f"FAIL: {args.pipeline_id!r} is not an enabled replication pipeline")
        return 1
    source, target = find_replication_pipeline(active, args.pipeline_id)
    plan = build_replication_plan(source, target)
    text = json.dumps(plan, indent=2, sort_keys=True)
    if args.output:
        os.makedirs(os.path.dirname(args.output), exist_ok=True)
        with open(args.output, "w") as f:
            f.write(text)
    else:
        print(text)
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


def cmd_managed_efs_inventory(args):
    """Expected managed-EFS inventory (JSON array of {deploymentId, efsCreationToken}) for the AWS-side managed_efs_inventory_guard; includes lifecycle.state=absent descriptors on purpose -- their EFS is retained, not decommissioned, so they remain part of the expected set."""
    active, inactive, invalid, problems = _run_full_validation(args.environment)
    if invalid or problems:
        _print_reasons(invalid)
        _print_problems(problems)
        print("FAIL: refusing to compute the managed-EFS inventory while validation problems exist")
        return 1
    expected = sorted(
        (
            {"deploymentId": d["deploymentId"], "efsCreationToken": d["efsCreationToken"]}
            for d in active + inactive
            if d["efsMode"] == "managed"
        ),
        key=lambda x: x["deploymentId"],
    )
    print(json.dumps(expected, indent=2))
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

    sub.add_parser("runtime-identities").set_defaults(func=cmd_runtime_identities)

    sub.add_parser("replication-pipelines").set_defaults(func=cmd_replication_pipelines)

    replication_plan_parser = sub.add_parser("replication-plan")
    replication_plan_parser.add_argument("pipeline_id")
    replication_plan_parser.add_argument("--output", default=None)
    replication_plan_parser.set_defaults(func=cmd_replication_plan)

    sub.add_parser("managed-efs-inventory").set_defaults(func=cmd_managed_efs_inventory)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
