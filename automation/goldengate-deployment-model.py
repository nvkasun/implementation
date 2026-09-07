#!/usr/bin/env python3
"""automation/goldengate-deployment-model.py: single source of truth for folder-driven GoldenGate deployment onboarding; scans envs/<environment>/*/values.yaml, validates each descriptor, and derives the inventory consumed by Terraform/monitor/workflows. Never prints secret values, document contents, or raw exception text."""
from __future__ import annotations

import argparse
import glob
import hashlib
import importlib.util
import json
import os
import re
import sys

import yaml

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

_ENVIRONMENT_MODULE_PATH = os.path.join(os.path.dirname(__file__), "goldengate-environment.py")
_environment_module = None
_environment_config_cache = {}


def _load_environment_module():
    """Lazy import of automation/goldengate-environment.py -- the single canonical environment-config parser/deriver. Never a second independent schema implementation."""
    global _environment_module
    if _environment_module is None:
        spec = importlib.util.spec_from_file_location("goldengate_environment", _ENVIRONMENT_MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _environment_module = module
    return _environment_module


def _environment_derived_values(environment):
    """Loads+validates+derives envs/<environment>/environment.yaml, cached per (REPO_ROOT, environment) pair. Re-syncs the environment module's own REPO_ROOT to this module's REPO_ROOT on every call: tests monkey-patch REPO_ROOT to an isolated scratch directory per ScratchEnvironmentTestCase, and the environment module -- a separate Python module loaded via importlib -- must follow that same scratch root rather than always resolving the real repository."""
    env_module = _load_environment_module()
    env_module.REPO_ROOT = REPO_ROOT
    cache_key = (REPO_ROOT, environment)
    if cache_key not in _environment_config_cache:
        doc = env_module.load_environment_config(environment)
        _environment_config_cache[cache_key] = env_module.derive_values(doc)
    return _environment_config_cache[cache_key]


FORBIDDEN_IMAGE_TAG = "latest"

IGNORED_NON_RUNTIME_FOLDER_NAMES = ("argocd", "goldengate-monitor")

_TOKEN_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*\Z")
_MAX_ID_LENGTH = 63

# Mirrors automation/goldengate-environment.py's own _DNS_DOMAIN_RE -- used to validate an explicit descriptor-declared ingress.host override (see parse_descriptor below), never imported cross-module.
_INGRESS_HOST_RE = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}\Z")
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


def _parse_image(environment, runtime):
    """The descriptor owns only the environment-neutral repositoryName; the full private-ECR repository is derived ONCE here from environment config, never re-typed by a descriptor or a downstream consumer."""
    image = runtime.get("image")
    _require_dict(image, "invalid image configuration: runtime.image must be a mapping")
    repository_name = image.get("repositoryName")
    tag = image.get("tag")
    if not isinstance(repository_name, str) or not repository_name:
        raise DescriptorError("invalid image configuration: runtime.image.repositoryName is required")
    if not isinstance(tag, str) or not tag:
        raise DescriptorError("invalid image configuration: runtime.image.tag is required and must be explicit")
    if tag == FORBIDDEN_IMAGE_TAG:
        raise DescriptorError("invalid image configuration: runtime.image.tag must not be \"latest\"")
    if not _ECR_REPO_SUFFIX_RE.match(repository_name):
        raise DescriptorError("invalid image configuration: runtime.image.repositoryName is malformed -- must be a safe, environment-neutral ECR repository name with no registry host, tag, digest, whitespace, or traversal")
    ecr_registry = _environment_derived_values(environment)["ECR_REGISTRY"]
    full_repository = f"{ecr_registry}/{repository_name}"
    return {"repository": full_repository, "repositoryName": repository_name, "tag": tag}


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
    image = runtime.get("image") or {}
    if "repository" in image:
        raise DescriptorError("forbidden override: runtime.image.repository is shared environment identity (derived from ECR_REGISTRY + runtime.image.repositoryName) and must not be set")

    global_cfg = doc.get("global") or {}
    if "environment" in global_cfg:
        raise DescriptorError("forbidden override: global.environment is shared environment configuration (envs/<environment>/environment.yaml) and must not be set in a runtime descriptor")

    ingress = doc.get("ingress") or {}
    if "hostDomain" in ingress:
        raise DescriptorError("forbidden override: ingress.hostDomain is shared environment configuration and must not be set in a runtime descriptor")
    alb = ingress.get("alb") or {}
    if "groupName" in alb:
        raise DescriptorError("forbidden override: ingress.alb.groupName is shared environment configuration and must not be set in a runtime descriptor")
    if "certificateArn" in alb:
        raise DescriptorError("forbidden override: ingress.alb.certificateArn is shared environment configuration and must not be set in a runtime descriptor")


def _reject_replication_key_presence(doc):
    """Automated Replication Implementation Removal (Task 4): the complete declarative/automated GoldenGate replication-provisioning schema (replication.enabled, extract, distribution, checkpoint, replicat, databaseCredentialSecret, etc.) has been retired outright -- GoldenGate database connections, credentials, Extract, trails, Distribution Path, Receiver-side configuration, checkpoint configuration, Replicat, and process starts are now configured MANUALLY by an operator/DBA through the GoldenGate UI after deployment. This tombstone guard rejects the mere PRESENCE of a top-level `replication` key in any shape -- null, {}, {enabled: false}, or {enabled: true} -- so a stale descriptor copied forward from before this removal can never be silently accepted (ignored) or silently misread as still-authoritative desired state. Key-presence based (`"replication" in doc`), exactly mirroring the existing _reject_lifecycle_presence_control/_reject_root_level_enabled/_reject_runtime_enabled_presence_control tombstone pattern below -- never `doc.get("replication") is not None`, since that would incorrectly accept a present `replication: null` key as though the key were entirely absent. There is deliberately no replacement replication-shaped field -- do not reintroduce one; replication state now lives only inside the live GoldenGate instances themselves, never in Git."""
    if "replication" in doc:
        raise DescriptorError("unsupported descriptor key: top-level replication automation has been retired; configure database connections and replication processes manually through the GoldenGate UI")


def _reject_lifecycle_presence_control(doc):
    """GoldenGate Runtime Desired-State Simplification: lifecycle.state is retired as a second runtime-presence source of truth -- deployment.enabled is now the ONLY authoritative control over whether a GoldenGate runtime should exist. A descriptor still carrying a lifecycle block (in any shape, valid or not -- including a literal `lifecycle: null`) is rejected outright rather than silently reinterpreted or ignored, so a stale descriptor can never introduce ambiguous or contradictory intent. Key-presence based (`"lifecycle" in doc`), never `doc.get("lifecycle") is not None` -- the contract is that the key must not be present at all, and a `.get()`-based check would incorrectly accept `lifecycle: null` (a present key whose value happens to be null is still a present key). There is deliberately no replacement lifecycle-shaped field -- do not reintroduce one."""
    if "lifecycle" in doc:
        raise DescriptorError("lifecycle.state is no longer supported for runtime presence; use deployment.enabled only")


def _reject_root_level_enabled(doc):
    """GoldenGate Runtime Presence Contract Finalization: a legacy descriptor-root `enabled:` key (outside deployment.enabled) is a second, potentially contradictory runtime-presence signal -- rejected outright, never silently treated as authoritative and never silently ignored. This never fires for nested `enabled` fields belonging to unrelated components (ingress.enabled, persistence.enabled, runtime.csi.enabled, replication.enabled, etc.) since those are different mapping keys entirely, only the descriptor's own top-level `enabled` key."""
    if "enabled" in doc:
        raise DescriptorError("root-level enabled is no longer supported; use deployment.enabled only")


def _reject_runtime_enabled_presence_control(runtime):
    """GoldenGate Runtime Presence Contract Finalization: runtime.enabled was a second, chart-level runtime-presence switch that could silently contradict deployment.enabled (deployment.enabled=true + runtime.enabled=false rendered nothing, while the canonical model still reported the runtime ACTIVE). The Helm release itself (created only when deployment.enabled=true) is now the sole presence boundary -- runtime.enabled is no longer part of the schema at all and is rejected outright if a descriptor still supplies it, never silently ignored."""
    if "enabled" in runtime:
        raise DescriptorError("runtime.enabled is no longer supported as a runtime presence control; use deployment.enabled only")


def _parse_csi_structure(runtime):
    """Validates the CSI block shape and extracts the stable enabled/mountPath fields automation/phases/phase5/runtime_acceptance.py needs to verify actual pod volume/mount wiring against -- never a second descriptor schema, just a few more fields read from the same validated runtime.csi block. objectName/serviceAccountRoleArn presence is rejected earlier by _reject_forbidden_overrides."""
    csi = _require_dict(runtime.get("csi"), "invalid CSI configuration: runtime.csi must be a mapping")
    admin = _require_dict(csi.get("admin"), "invalid CSI configuration: runtime.csi.admin must be a mapping")
    certificate = _require_dict(csi.get("certificate"), "invalid CSI configuration: runtime.csi.certificate must be a mapping")

    csi_enabled = csi.get("enabled", False)
    if not _is_literal_bool(csi_enabled):
        raise DescriptorError("invalid CSI configuration: runtime.csi.enabled must be a literal Boolean")

    admin_enabled = admin.get("enabled", False)
    if not _is_literal_bool(admin_enabled):
        raise DescriptorError("invalid CSI configuration: runtime.csi.admin.enabled must be a literal Boolean")
    admin_mount_path = admin.get("mountPath")
    if admin_enabled and (not isinstance(admin_mount_path, str) or not admin_mount_path):
        raise DescriptorError("invalid CSI configuration: runtime.csi.admin.mountPath is required and must be a non-empty string when runtime.csi.admin.enabled=true")

    certificate_enabled = certificate.get("enabled", False)
    if not _is_literal_bool(certificate_enabled):
        raise DescriptorError("invalid CSI configuration: runtime.csi.certificate.enabled must be a literal Boolean")
    certificate_mount_path = certificate.get("mountPath")
    if certificate_enabled and (not isinstance(certificate_mount_path, str) or not certificate_mount_path):
        raise DescriptorError("invalid CSI configuration: runtime.csi.certificate.mountPath is required and must be a non-empty string when runtime.csi.certificate.enabled=true")

    return {
        "csiEnabled": csi_enabled,
        "csiAdminEnabled": admin_enabled,
        "csiAdminMountPath": admin_mount_path,
        "csiCertificateEnabled": certificate_enabled,
        "csiCertificateMountPath": certificate_mount_path,
    }


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
    _reject_root_level_enabled(doc)
    _reject_replication_key_presence(doc)

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
    _reject_runtime_enabled_presence_control(runtime)
    deployment_type = runtime.get("deploymentType")
    if not _safe_token(deployment_type, _MAX_TYPE_LENGTH):
        raise DescriptorError("invalid deployment metadata: runtime.deploymentType must be a safe lowercase token")

    image = _parse_image(environment, runtime)
    runtime_service_account_name = resolve_runtime_service_account(deployment_type)
    admin_secret_name = resolve_admin_secret(environment, role)
    tls_secret_name = resolve_tls_secret(environment)
    csi_fields = _parse_csi_structure(runtime)
    _reject_lifecycle_presence_control(doc)
    efs = _parse_efs(deployment_id, environment, doc)

    if _contains_credential_like_key(doc):
        raise DescriptorError("embedded credentials found in values.yaml")

    container_name = runtime.get("containerName", deployment_type)
    if not isinstance(container_name, str) or not container_name:
        raise DescriptorError("invalid deployment metadata: runtime.containerName must be a non-empty string")

    # ingress.hostDomain/alb.groupName/alb.certificateArn are shared environment configuration, not descriptor input (see _reject_forbidden_overrides) -- ingressHost below always reflects the canonical shared DNS domain, never a descriptor-declared value.
    ingress = _require_dict(doc.get("ingress"), "invalid deployment metadata: ingress must be a mapping")
    alb = ingress.get("alb") or {}
    alb_group_order = alb.get("groupOrder")

    # Phase 5: focused fields the runtime ownership/acceptance classifiers need (automation/phases/phase5/runtime_state.py, runtime_acceptance.py) -- never a second descriptor schema, just a few more fields extracted from the same validated document. Light shape validation only; Helm's own `required`/type coercion at render time remains the deeper contract for these fields.
    service = runtime.get("service") or {}
    service_type = service.get("type") or "ClusterIP"
    if not isinstance(service_type, str) or not service_type:
        raise DescriptorError("invalid deployment metadata: runtime.service.type must be a non-empty string")
    service_ports_raw = service.get("ports") or {}
    service_ports = {}
    for port_name in ("https", "dist", "receiver", "metrics"):
        port_value = service_ports_raw.get(port_name)
        if port_value is not None and not (isinstance(port_value, int) and not isinstance(port_value, bool) and 1 <= port_value <= 65535):
            raise DescriptorError(f"invalid deployment metadata: runtime.service.ports.{port_name} must be a valid port number or null")
        service_ports[port_name] = port_value

    replicas = runtime.get("replicas", 1)
    if not (isinstance(replicas, int) and not isinstance(replicas, bool) and replicas >= 1):
        raise DescriptorError("invalid deployment metadata: runtime.replicas must be a positive integer")

    init_permissions_enabled = (runtime.get("initPermissions") or {}).get("enabled", False)
    if not _is_literal_bool(init_permissions_enabled):
        raise DescriptorError("invalid deployment metadata: runtime.initPermissions.enabled must be a literal Boolean")

    ingress_enabled = ingress.get("enabled", False)
    if not _is_literal_bool(ingress_enabled):
        raise DescriptorError("invalid deployment metadata: ingress.enabled must be a literal Boolean")
    ingress_class_name = ingress.get("className") or "alb"
    if not isinstance(ingress_class_name, str) or not ingress_class_name:
        raise DescriptorError("invalid deployment metadata: ingress.className must be a non-empty string")

    # Resolves the SAME precedence helm/goldengate.runtimeIngressHost implements: an explicit, non-empty ingress.host wins outright, otherwise "<deploymentId>.<sharedDnsDomain>" -- the single canonical place this runtime's own resolved Ingress hostname is derived, so the monitor topology (build_registry below) never has to reconstruct this rule independently.
    ingress_host_override = ingress.get("host")
    if ingress_host_override not in (None, ""):
        if not isinstance(ingress_host_override, str) or not _INGRESS_HOST_RE.match(ingress_host_override):
            raise DescriptorError("invalid deployment metadata: ingress.host, when set, must be a valid DNS hostname")
        resolved_ingress_host = ingress_host_override
    else:
        resolved_ingress_host = f"{deployment_id}.{shared['dnsDomain']}"

    # Phase B3A closeout: u02Type is the chart's own volume-source discriminator (helm/goldengate/templates/runtime-statefulset.yaml branches on it directly: efs/existingClaim/emptyDir) -- read through, never re-derived. extraVolume(Mount)Names are name-only allow-lists (the chart passes runtime.extraVolumes/extraVolumeMounts through verbatim via `toYaml`) so the acceptance classifier's exact-volume-set check never falsely rejects a descriptor that legitimately uses that chart escape hatch, without inventing a second desired shape for volumes this repository does not itself validate in detail.
    u02_type = ((runtime.get("storage") or {}).get("u02") or {}).get("type")
    extra_volume_names = sorted({v.get("name") for v in (runtime.get("extraVolumes") or []) if isinstance(v, dict) and v.get("name")})
    extra_volume_mount_names = sorted({v.get("name") for v in (runtime.get("extraVolumeMounts") or []) if isinstance(v, dict) and v.get("name")})

    return {
        "deploymentId": deployment_id,
        "environment": environment,
        "pipeline": pipeline,
        "role": role,
        "enabled": enabled,
        "deploymentType": deployment_type,
        "imageRepository": image["repository"],
        "imageRepositoryName": image["repositoryName"],
        "imageTag": image["tag"],
        "containerName": container_name,
        "runtimeServiceAccountName": runtime_service_account_name,
        "adminSecretName": admin_secret_name,
        "tlsSecretName": tls_secret_name,
        "runtimeNamespace": shared["runtimeNamespace"],
        "monitoringNamespace": shared["monitoringNamespace"],
        "ingressHost": shared["dnsDomain"],
        "efsMode": efs["mode"],
        "efsFileSystemId": efs["fileSystemId"],
        "efsCreationToken": efs["creationToken"],
        "pvcClaimName": efs["pvcClaimName"],
        "albGroupOrder": alb_group_order,
        "replicas": replicas,
        "serviceType": service_type,
        "servicePorts": service_ports,
        "initPermissionsEnabled": init_permissions_enabled,
        "ingressEnabled": ingress_enabled,
        "ingressClassName": ingress_class_name,
        # The runtime's own resolved Ingress hostname (ingress.host override, or the "<deploymentId>.<dnsDomain>" default) -- distinct from the "ingressHost" key above, which is this document's own legacy name for the SHARED dnsDomain; never renamed/repurposed here to avoid silently changing that unrelated contract.
        "runtimeIngressHost": resolved_ingress_host,
        "u02Type": u02_type,
        "csiEnabled": csi_fields["csiEnabled"],
        "csiAdminEnabled": csi_fields["csiAdminEnabled"],
        "csiAdminMountPath": csi_fields["csiAdminMountPath"],
        "csiCertificateEnabled": csi_fields["csiCertificateEnabled"],
        "csiCertificateMountPath": csi_fields["csiCertificateMountPath"],
        "extraVolumeNames": extra_volume_names,
        "extraVolumeMountNames": extra_volume_mount_names,
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

    # GoldenGate Runtime Desired-State Simplification: deployment.enabled is the single authoritative runtime-presence control -- active means exactly deployment.enabled=true. lifecycle.state no longer exists as a second source of truth (rejected earlier, in parse_descriptor via _reject_lifecycle_presence_control).
    if descriptor["enabled"] is not True:
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

    # Automated Replication Implementation Removal: the former cross-runtime replication pipeline contract (_validate_replication_pipelines) and derived-database-credential-alias collision check are retired along with the declarative replication schema itself -- only the general logical-topology checks below (deployment.pipeline safety, deployment.role source/target cardinality) remain, since deployment.pipeline/deployment.role continue to serve identity/monitor-grouping/UI-topology purposes independent of any automated replication provisioning.

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
            # Monitoring portal "Open GoldenGate UI" external link: ingressHost is this runtime's own resolved Ingress hostname (parse_descriptor's runtimeIngressHost -- explicit ingress.host override, or the "<deploymentId>.<dnsDomain>" default; the SAME precedence helm/goldengate.runtimeIngressHost implements, never a duplicated rule). Always present regardless of ingressEnabled (a hostname string is not sensitive and is cheap to derive) -- ingressEnabled alone is the single gate the monitor uses to decide whether to render a clickable link at all.
            "ingressEnabled": d["ingressEnabled"],
            "ingressHost": d["runtimeIngressHost"],
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
    env_values = _environment_derived_values(environment)

    runtime_namespace = ((platform_doc or {}).get("namespaces") or {}).get("runtime", {}).get("name") or env_values["RUNTIME_NAMESPACE"]
    fluent_bit_namespaces = ((platform_doc or {}).get("fluentBit") or {}).get("namespaces") or {}
    monitoring_namespace = fluent_bit_namespaces.get("monitoring") or env_values["MONITOR_NAMESPACE"]

    ingress = (monitor_doc or {}).get("ingress") or {}
    monitor_host = ingress.get("host") or ""
    dns_domain = monitor_host.split("monitor.", 1)[-1] if monitor_host.startswith("monitor.") else env_values["DNS_DOMAIN"]

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


def cmd_environment_matrix(args):
    """GoldenGate Runtime Presence Contract Finalization: environment-wide manual MAIN Deploy/Validate matrix -- the canonical registry reshaped into the exact deployment_matrix/deletion_matrix JSON shapes automation/phases/phase1/detect-goldengate-deployments.sh's workflow_dispatch (blank deployment_id) branch emits as GitHub Actions step outputs. The SAME single source of truth as the folder-driven registry itself (scan()); never a second parser reimplementing active/inactive classification in Bash. deployment_matrix contains one entry per ACTIVE (deployment.enabled=true) descriptor, carrying the requested --deploy value. deploymentModel is always exactly "singleRuntime" for every active/inactive descriptor -- parse_descriptor() already rejects any other value as invalid before a descriptor can ever reach the active/inactive lists, so this is never re-derived per entry. deletion_matrix contains one entry per INACTIVE (deployment.enabled=false, still physically present) descriptor, reason=deployment-disabled -- mirroring the push-diff path's own classification -- but ONLY when --deploy is true; the caller is responsible for requesting an empty deletion_matrix in Validate mode, since deletion evaluation is a deploy-only, non-mutating-incompatible concern."""
    active, inactive, invalid, problems = _run_full_validation(args.environment)
    if invalid or problems:
        _print_reasons(invalid)
        _print_problems(problems)
        print("FAIL: refusing to build the environment-wide matrix while validation problems exist")
        return 1

    deploy_bool = args.deploy == "true"

    deployment_matrix = [
        {"environment": args.environment, "deployment_id": d["deploymentId"], "deployment_model": "singleRuntime", "deploy": deploy_bool}
        for d in sorted(active, key=lambda x: x["deploymentId"])
    ]

    deletion_matrix = []
    if deploy_bool:
        deletion_matrix = [
            {"environment": args.environment, "deployment_id": d["deploymentId"], "deployment_model": "singleRuntime", "efs_mode": d["efsMode"] or "", "reason": "deployment-disabled"}
            for d in sorted(inactive, key=lambda x: x["deploymentId"])
        ]

    print(json.dumps({"deployment_matrix": deployment_matrix, "deletion_matrix": deletion_matrix}))
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
    """Expected managed-EFS inventory (JSON array of {deploymentId, efsCreationToken}) for the AWS-side managed_efs_inventory_guard; includes deployment.enabled=false descriptors on purpose -- their EFS is retained, not decommissioned, so they remain part of the expected set. Only a physically removed descriptor drops out of this inventory."""
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

    environment_matrix_parser = sub.add_parser("environment-matrix")
    environment_matrix_parser.add_argument("--deploy", required=True, choices=("true", "false"))
    environment_matrix_parser.set_defaults(func=cmd_environment_matrix)

    describe_parser = sub.add_parser("describe")
    describe_parser.add_argument("deployment_id")
    describe_parser.set_defaults(func=cmd_describe)

    registry_parser = sub.add_parser("registry")
    registry_parser.add_argument("--output", default=None)
    registry_parser.set_defaults(func=cmd_registry)

    sub.add_parser("shared-secrets").set_defaults(func=cmd_shared_secrets)

    sub.add_parser("runtime-identities").set_defaults(func=cmd_runtime_identities)

    sub.add_parser("managed-efs-inventory").set_defaults(func=cmd_managed_efs_inventory)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
