"""inventory: canonical runtime discovery for the shared gg-monitor.

Single source of truth for "which GoldenGate runtime deployments exist and how
do I reach them" -- loads pipelines/deployments.yaml (the manager-aligned
inventory) and topologies/dev/*.yaml (endpoint/namespace/secret-reference
detail), and merges them into one canonical runtime list.

No second, hardcoded runtime list exists anywhere else in this application --
every other module receives its runtime facts from load_runtimes() here.

Three separate concepts (manager-alignment correction pass, fix 2 -- an
earlier draft incorrectly collapsed these into one mandatory pipelineId
stored directly on the runtime):

  A. Runtime inventory (load_deployment_inventory): which runtime
     deployments exist, and their type/enabled flag.
  B. Runtime connection detail (load_topologies): namespace, service,
     endpoint, TLS name, and secret reference for each deployment. A
     deployment may appear in more than one topology document as long as
     these immutable facts never conflict between documents.
  C. Process topology (build_process_pipeline_map_json): which
     Extract/Replicat/Distribution Path belongs to which logical
     pipelineId. This is process-level, not deployment-level -- the same
     deployment can have different processes routed under different
     logical pipelines in different topology documents.

Also derives manager-compatible equivalents of the manager's own mounted
ConfigMap inputs (see charts/gg-deployment/files/utility-sidecar.py
build_process_pipeline_map / charts/gg-alerter/files/gg_alerter.py
enabled_deployments in the manager reference repository, inspected read-only):

  - deployments.json: a flat JSON array of canonical keys (gg-<name>) for
    ENABLED deployments only -- exactly the shape gg-alerter's
    enabled_deployments() reads via list(json.load(fh)).
  - process-pipeline-map.json: {PROCESS_NAME_UPPER: {"pipeline_name": ...,
    "deployment": <bare-key-without-gg->}} -- exactly the shape
    build_process_pipeline_map() reads. pipeline_name is the LOGICAL
    topology pipelineId of the document that declared the mapping (concept
    C above), never the canonical per-deployment DynamoDB partition key.
    With the current empty topology process lists (no Extract/Replicat/
    Distribution Path configured yet), this is always {}.
  - runtime-config.json (build_runtime_config_json): an APPROVED
    shared-monitor extension, not a manager artifact -- per-enabled-
    deployment canonical key/type/namespace/admin connect detail/credential
    FILE PATHS, no secret values, generated at packaging time (fix 4) so the
    running container can read it with the standard-library json module
    instead of needing PyYAML + the mounted ConfigMap at runtime.

This module does NOT claim to reproduce the manager's ConfigMap projection
mechanism (pipeline-3 in the manager repo) -- it is this repository's own
equivalent transformation, not a copy of manager infrastructure.

validate_enabled_runtimes() enforces that every ENABLED runtime has
everything gg_monitor_core needs before the process starts serving -- an
enabled runtime missing required topology fails startup clearly instead of
silently polling nothing (base=None) forever. Credential identity is
DEPLOYMENT-level (fix 1: adminSecretObject/credentialUserFile/
credentialPasswordFile, derived from secretReferences.admin), never
ENGINE-TYPE-level -- a future second Oracle deployment with a different
secret works without any Python code change.
"""
from __future__ import annotations

import glob
import os

import yaml

REPO_ROOT_ENV = "REPO_CONFIG_ROOT"
DEFAULT_REPO_ROOT = "/etc/gg-canonical"

DEPLOYMENTS_YAML_RELPATH = "pipelines/deployments.yaml"
TOPOLOGIES_GLOB_RELPATH = "topologies/dev/*.yaml"

# Phase 4 supports exactly these deployment types. An enabled runtime of any
# other type (e.g. a future sqlserver candidate) must fail startup clearly
# rather than silently attempt to poll with no credential-file mapping.
SUPPORTED_TYPES = ("oracle", "postgresql")


class InventoryError(Exception):
    """Raised when the canonical inventory/topology sources cannot be loaded."""


class StartupValidationError(Exception):
    """Raised when one or more ENABLED runtimes are missing required
    configuration. Carries every failure found (not just the first) for a
    single, complete startup error message."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def _repo_root():
    return os.environ.get(REPO_ROOT_ENV, DEFAULT_REPO_ROOT)


def _canonical_key(name):
    """Exact manager derivation: "gg-${d.name}". Applied exactly once."""
    return f"gg-{name}"


def load_deployment_inventory(repo_root=None):
    """Parse pipelines/deployments.yaml. Returns a list of
    {name, type, enabled} dicts, unmodified except for basic shape
    validation -- this function does not derive canonical keys itself
    (see load_runtimes)."""
    repo_root = repo_root or _repo_root()
    path = os.path.join(repo_root, DEPLOYMENTS_YAML_RELPATH)
    try:
        with open(path) as f:
            doc = yaml.safe_load(f)
    except OSError as e:
        raise InventoryError(f"could not read {path}: {e}") from e
    entries = (doc or {}).get("deployments")
    if not isinstance(entries, list):
        raise InventoryError(f"{path}: 'deployments' must be a list")
    seen_keys = {}
    for entry in entries:
        if not isinstance(entry, dict) or "name" not in entry or "type" not in entry:
            raise InventoryError(f"{path}: each entry requires name and type: {entry!r}")
        if entry["name"].startswith("gg-"):
            raise InventoryError(
                f"{path}: inventory name {entry['name']!r} must not already include "
                "the gg- prefix (the canonical key is derived as gg-${d.name})"
            )
        key = _canonical_key(entry["name"])
        if key in seen_keys:
            raise InventoryError(
                f"{path}: duplicate canonical pipeline key {key!r} produced by "
                f"both {seen_keys[key]!r} and {entry['name']!r}"
            )
        seen_keys[key] = entry["name"]
    return entries


def load_topology_documents(repo_root=None):
    """Parse every topologies/dev/*.yaml file into a list of
    (path, raw_parsed_document) pairs, in sorted-path order.

    This is the lower-level building block both load_topologies() (concept
    B: per-deployment connection detail) and build_process_pipeline_map_json()
    (concept C: process topology) are built from. Deliberately left UNMERGED
    here: the same deploymentName can legitimately appear in more than one
    topology document (e.g. one document adds process mappings for a
    "payments" logical pipeline, another adds different process mappings for
    a "loans" logical pipeline, both on the same underlying deployment) --
    only load_topologies() below enforces that the shared, immutable
    CONNECTION facts (namespace/endpoint/secret references) never conflict
    across documents; it says nothing about process-level pipeline routing.
    """
    repo_root = repo_root or _repo_root()
    pattern = os.path.join(repo_root, TOPOLOGIES_GLOB_RELPATH)
    docs = []
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path) as f:
                doc = yaml.safe_load(f) or {}
        except OSError as e:
            raise InventoryError(f"could not read {path}: {e}") from e
        docs.append((path, doc))
    return docs


def _connection_snapshot(detail):
    """The subset of a topology deployment entry that must be IDENTICAL
    every time the same deploymentName appears across more than one
    topology document: deploymentType, namespace, serviceName, the admin
    endpoint (scheme/host/port/tlsServerName), and the admin/TLS secret
    references. These are immutable RUNTIME facts (concept B: "where does
    this deployment live and how do I reach it"); everything else (e.g.
    per-document process lists, or the document's own pipelineId) is
    process-TOPOLOGY data (concept C) and is allowed to differ per
    document -- the same deployment can be routed under different logical
    pipelines for different processes without this being a conflict."""
    admin = (detail.get("endpoints") or {}).get("admin") or {}
    secret_refs = detail.get("secretReferences") or {}
    return {
        "deploymentType": detail.get("deploymentType"),
        "namespace": detail.get("namespace"),
        "serviceName": detail.get("serviceName"),
        "admin.scheme": admin.get("scheme"),
        "admin.host": admin.get("host"),
        "admin.port": admin.get("port"),
        "admin.tlsServerName": admin.get("tlsServerName"),
        "secretReferences.admin": secret_refs.get("admin"),
        "secretReferences.tls": secret_refs.get("tls"),
    }


def load_topologies(repo_root=None):
    """Merge every topology document's deployment entries into ONE
    connection-detail record per deploymentName (concept B: namespace,
    service, endpoint, TLS name, and secret reference for each deployment).

    The SAME deploymentName is allowed to appear in more than one topology
    document -- concept C (which logical pipeline a PROCESS belongs to) is
    tracked separately by build_process_pipeline_map_json, not here; a
    deployment does not "belong to" a single pipeline. This only fails when
    the immutable connection facts genuinely CONFLICT between documents
    (see _connection_snapshot) -- e.g. two different namespaces for the same
    deploymentName. An identical repeated declaration (the normal case: a
    second topology document re-states the same deployment because it adds
    process mappings for a different logical pipeline) is not a conflict.
    """
    by_deployment_name = {}
    seen_in_file = {}
    for path, doc in load_topology_documents(repo_root):
        for _role, detail in (doc.get("deployments") or {}).items():
            if not isinstance(detail, dict) or "deploymentName" not in detail:
                continue
            name = detail["deploymentName"]
            if name in by_deployment_name:
                existing_snapshot = _connection_snapshot(by_deployment_name[name])
                new_snapshot = _connection_snapshot(detail)
                if existing_snapshot != new_snapshot:
                    raise InventoryError(
                        f"deploymentName {name!r} has CONFLICTING connection details "
                        f"between {seen_in_file[name]!r} and {path!r}: "
                        f"{existing_snapshot!r} != {new_snapshot!r}"
                    )
                continue  # identical connection facts -- keep the first-seen detail
            by_deployment_name[name] = detail
            seen_in_file[name] = path
    return by_deployment_name


def _credential_alias_paths(pipeline, mount_root="/mnt/secrets-store"):
    """Deterministic per-deployment CSI object alias file paths, derived
    ONLY from the canonical deployment key -- never from deployment type.
    Both this function and helm/gg-monitor/templates/secretproviderclass.yaml
    independently derive the SAME "<pipeline>-admin-user" /
    "<pipeline>-admin-password" alias names from the same canonical data, so
    the generated SecretProviderClass and this module's expectations always
    agree without cross-referencing each other at render/runtime. A future
    second Oracle deployment (a different pipeline key) gets its own
    distinct pair automatically -- no engine-keyed dict, no code change."""
    return (
        f"{mount_root}/{pipeline}-admin-user",
        f"{mount_root}/{pipeline}-admin-password",
    )


def load_runtimes(repo_root=None):
    """Merge pipelines/deployments.yaml + topologies/dev/*.yaml into the
    canonical runtime list this whole application reads from.

    Returns a list of dicts:
      {
        "pipeline": "gg-oracle-payments-01",   # canonical DDB partition key
        "name": "oracle-payments-01",           # bare inventory name
        "type": "oracle",
        "enabled": True,
        "namespace": "goldengate-dev",
        "serviceName": "gg-oracle-payments-01",
        "topologyDeploymentType": "oracle",     # topology's own deploymentType, or None
        "endpoints": {...},                     # from topology, may be {}
        "secretReferences": {...},               # from topology, may be {}
        "adminSecretObject": "dev/goldengate/source/admin",  # DEPLOYMENT-level
                                                 # credential identity (fix 1) --
                                                 # secretReferences.admin, or ""
                                                 # if no topology entry was found.
        "credentialUserFile": "/mnt/secrets-store/gg-oracle-payments-01-admin-user",
        "credentialPasswordFile": "/mnt/secrets-store/gg-oracle-payments-01-admin-password",
                                                 # both "" when adminSecretObject is "".
      }

    Note: pipelineId and processes are DELIBERATELY NOT part of this shape
    (fix 2, correcting an earlier draft that attached one mandatory
    pipelineId to a runtime). A deployment does not belong to a single
    logical pipeline -- see build_process_pipeline_map_json for the
    process-level model (concept C) that replaces it.

    A deployment declared in pipelines/deployments.yaml with no matching
    topology entry still appears (endpoints/secretReferences default to {},
    credential fields default to "") -- CONFIG seeding and lease/state
    ownership never depend on topology being present, matching the
    Terraform for_each contract this mirrors (Phase 3).
    validate_enabled_runtimes() below is what turns a missing topology into
    a hard startup failure for ENABLED runtimes.
    """
    inventory = load_deployment_inventory(repo_root)
    topologies = load_topologies(repo_root)

    runtimes = []
    for entry in inventory:
        pipeline = _canonical_key(entry["name"])
        detail = topologies.get(pipeline, {})
        admin_secret_object = (detail.get("secretReferences") or {}).get("admin", "")
        if admin_secret_object:
            credential_user_file, credential_password_file = _credential_alias_paths(pipeline)
        else:
            credential_user_file, credential_password_file = "", ""
        runtimes.append({
            "pipeline": pipeline,
            "name": entry["name"],
            "type": entry["type"],
            "enabled": bool(entry.get("enabled", False)),
            "namespace": detail.get("namespace", ""),
            "serviceName": detail.get("serviceName", ""),
            "topologyDeploymentType": detail.get("deploymentType"),
            "endpoints": detail.get("endpoints", {}),
            "secretReferences": detail.get("secretReferences", {}),
            "adminSecretObject": admin_secret_object,
            "credentialUserFile": credential_user_file,
            "credentialPasswordFile": credential_password_file,
        })
    return runtimes


def validate_enabled_runtimes(runtimes):
    """Fail startup clearly for any ENABLED runtime missing required
    configuration, instead of silently continuing with base=None.

    Raises StartupValidationError (all problems collected, not just the
    first) if any enabled runtime is invalid. Disabled runtimes are never
    validated this strictly -- an incomplete disabled candidate is exactly
    the "registered but not live yet" case the inventory is meant to allow.
    """
    problems = []
    for r in runtimes:
        if not r["enabled"]:
            continue
        pipeline = r["pipeline"]

        if r["type"] not in SUPPORTED_TYPES:
            problems.append(
                f"{pipeline}: unsupported deployment type {r['type']!r} "
                f"(supported this phase: {SUPPORTED_TYPES})"
            )
            continue  # further checks assume a supported type

        if not r["namespace"] and not r["serviceName"] and not r["endpoints"]:
            problems.append(f"{pipeline}: no matching topology entry found (enabled runtimes require one)")
            continue  # further endpoint/secret checks would be redundant noise

        if r["topologyDeploymentType"] is not None and r["topologyDeploymentType"] != r["type"]:
            problems.append(
                f"{pipeline}: topology deploymentType {r['topologyDeploymentType']!r} "
                f"does not match inventory type {r['type']!r}"
            )

        if not r["namespace"]:
            problems.append(f"{pipeline}: topology namespace is empty")
        if not r["serviceName"]:
            problems.append(f"{pipeline}: topology serviceName is empty")

        admin_ep = (r["endpoints"] or {}).get("admin") or {}
        if admin_ep.get("scheme") != "https":
            problems.append(f"{pipeline}: admin endpoint scheme must be https, got {admin_ep.get('scheme')!r}")
        if not admin_ep.get("host"):
            problems.append(f"{pipeline}: admin endpoint host is empty")
        port = admin_ep.get("port")
        if not isinstance(port, int) or not (1 <= port <= 65535):
            problems.append(f"{pipeline}: admin endpoint port is invalid: {port!r}")
        if not admin_ep.get("tlsServerName"):
            problems.append(f"{pipeline}: admin endpoint tlsServerName is required for TLS hostname verification")

        # secretReferences.admin is this deployment's OWN admin credential
        # identity (fix 1: deployment-level, not engine-type-level) -- it is
        # what load_runtimes derives adminSecretObject/credentialUserFile/
        # credentialPasswordFile from, so this single check also guarantees
        # those three derived fields are non-empty.
        secret_refs = r["secretReferences"] or {}
        if not secret_refs.get("admin"):
            problems.append(f"{pipeline}: secretReferences.admin is empty (required as this deployment's own admin credential identity)")
        if not secret_refs.get("tls"):
            problems.append(f"{pipeline}: secretReferences.tls is empty")

    if problems:
        raise StartupValidationError(problems)


def validate_secret_arn_coverage(runtimes, allowed_secret_arns, account="668311715351", region="eu-west-1"):
    """Manager-alignment correction (fix 5): proves every ENABLED runtime's
    admin (and TLS) secret reference from canonical runtime data is covered
    by gg-monitor-dev-role's actual IAM policy Resource ARNs, instead of
    assuming it always will be. A future runtime whose secret lives outside
    the currently allowed ARN set fails HERE, at validation time, rather
    than later as an opaque CSI FailedMount event on a running pod.

    This is a build/CI-time check (see hack/test-goldengate-deployment-
    models.sh), not something gg_monitor_core.py calls at container
    startup: the running monitor has no access to its own IAM policy
    document and should not need iam:GetRolePolicy just to self-check --
    allowed_secret_arns is read from the policy JSON file by the caller.

    allowed_secret_arns: the exact Resource ARN patterns granted to the
    role's secretsmanager:GetSecretValue/DescribeSecret statement (e.g.
    "arn:aws:secretsmanager:eu-west-1:668311715351:secret:dev/goldengate/
    source/admin-*") -- Secrets Manager appends a random 6-character suffix
    to every secret's real ARN, so a pattern ending "-*" is the normal,
    correct shape, not a wildcard being used loosely.

    Manager target (documented, not implemented this pass -- the CURRENT
    exact secret ARNs remain unchanged per this task's own instruction not
    to migrate them): a standardized per-deployment credentials path (e.g.
    "dev/goldengate/deployments/<name>/admin", mirroring the manager's own
    "<prefix>/deployments/<name>/credentials" convention) that the monitor/
    writer role would be permitted against via a single prefix-scoped
    Resource pattern, instead of one explicit ARN per current secret.
    """
    def _covers(secret_name, arn_pattern):
        base = f"arn:aws:secretsmanager:{region}:{account}:secret:{secret_name}"
        return arn_pattern == base or arn_pattern.startswith(base + "-")

    problems = []
    for r in runtimes:
        if not r["enabled"]:
            continue
        secret_refs = r["secretReferences"] or {}
        for label, secret_name in (("admin", r.get("adminSecretObject") or secret_refs.get("admin")),
                                   ("tls", secret_refs.get("tls"))):
            if not secret_name:
                continue  # empty secret references are already a validate_enabled_runtimes failure
            if not any(_covers(secret_name, arn) for arn in allowed_secret_arns):
                problems.append(
                    f"{r['pipeline']}: {label} secret {secret_name!r} is not covered by any "
                    "gg-monitor-dev-role IAM policy Resource ARN -- this deployment would fail "
                    "with a CSI FailedMount event at pod start, not a clean pre-deploy failure"
                )
    if problems:
        raise StartupValidationError(problems)


def build_deployments_json(runtimes):
    """Manager-compatible deployments.json: flat list of canonical keys for
    ENABLED deployments only (matches gg_alerter.enabled_deployments())."""
    return [r["pipeline"] for r in runtimes if r["enabled"]]


def build_process_pipeline_map_json(runtimes, repo_root=None):
    """Manager-compatible process-pipeline-map.json:
    {PROCESS_NAME_UPPER: {"pipeline_name": ..., "deployment": bare_key}}.

    Concept C (process topology), built by iterating every topology
    DOCUMENT (load_topology_documents) -- NOT the deduplicated
    connection-detail view (load_topologies) -- because the same deployment
    can appear in multiple documents with DIFFERENT process mappings under
    DIFFERENT logical pipelineIds (e.g. one Extract routed under
    "payments-pipeline", another Extract on the SAME deployment routed under
    "loans-pipeline"). Only ENABLED runtimes (per the passed-in `runtimes`)
    contribute entries.

    pipeline_name is the LOGICAL topology pipelineId of the document that
    declared the mapping -- matching the manager's own concept of a pipeline
    as distinct from a single deployment, never the per-deployment canonical
    DynamoDB partition key. pipelineId is required ONLY on a topology
    document that itself declares at least one process mapping; a document
    used purely for deployment-level connection detail (health polling has
    no process concept) needs no pipelineId at all.

    A process name mapped to CONFLICTING (pipeline_name, deployment) pairs
    across different documents fails loudly. With today's empty topology
    process lists this always returns {} -- no placeholder Extract/Replicat/
    Distribution Path names are invented.
    """
    enabled_pipelines = {r["pipeline"] for r in runtimes if r["enabled"]}
    bare_by_pipeline = {r["pipeline"]: r["name"] for r in runtimes}

    out = {}
    seen = {}  # PROCESS_NAME -> (mapping_dict, path), for conflict detection
    for path, doc in load_topology_documents(repo_root):
        pipeline_id = doc.get("pipelineId") or ""
        doc_has_process = False
        for _role, detail in (doc.get("deployments") or {}).items():
            if not isinstance(detail, dict) or "deploymentName" not in detail:
                continue
            canonical_key = detail["deploymentName"]
            if canonical_key not in enabled_pipelines:
                continue
            bare_key = bare_by_pipeline.get(canonical_key, canonical_key)
            processes = detail.get("processes") or {}
            for _kind, names in processes.items():
                for proc_name in (names or []):
                    doc_has_process = True
                    key = str(proc_name).upper()
                    mapping = {"pipeline_name": pipeline_id, "deployment": bare_key}
                    if key in seen:
                        prev_mapping, prev_path = seen[key]
                        if prev_mapping != mapping:
                            raise InventoryError(
                                f"process {key!r} has conflicting pipeline-map entries "
                                f"between {prev_path!r} ({prev_mapping!r}) and {path!r} ({mapping!r})"
                            )
                        continue
                    seen[key] = (mapping, path)
                    out[key] = mapping
        if doc_has_process and not pipeline_id:
            raise InventoryError(
                f"{path}: declares process mappings but has no top-level pipelineId "
                "(pipelineId is required only for topology documents that declare "
                "process mappings, but this one does and is missing it)"
            )
    return out


def build_runtime_config_json(runtimes):
    """Approved shared-monitor extension (fix 4): a manager-compatible-style
    generated JSON artifact carrying everything gg_monitor_core needs per
    ENABLED deployment, without secret VALUES -- canonical key, type,
    namespace, admin connect host/port, tlsServerName, and credential FILE
    PATHS (never the credential content itself, and never the raw
    Secrets Manager object name/ARN either -- that stays in
    adminSecretObject for IAM-coverage validation only, not runtime
    connection use)."""
    out = []
    for r in runtimes:
        if not r["enabled"]:
            continue
        admin_ep = (r.get("endpoints") or {}).get("admin") or {}
        out.append({
            "pipeline": r["pipeline"],
            "type": r["type"],
            "namespace": r["namespace"],
            "adminHost": admin_ep.get("host", ""),
            "adminPort": admin_ep.get("port"),
            "adminScheme": admin_ep.get("scheme", "https"),
            "tlsServerName": admin_ep.get("tlsServerName", ""),
            "credentialUserFile": r["credentialUserFile"],
            "credentialPasswordFile": r["credentialPasswordFile"],
        })
    return out
