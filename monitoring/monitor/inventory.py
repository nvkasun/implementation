"""monitor/inventory.py: portal-local canonical runtime + topology loader.

Deliberately a separate, self-contained module from
monitoring/gg-monitor-core/inventory.py (never imported from there) so the
portal's own Docker build context (monitoring/monitor/, see Dockerfile's
COPY list) never depends on a sibling directory -- copying a file across
build contexts, or reaching outside monitoring/monitor/ at build time, would
be fragile and is unnecessary here. This module reuses the SAME validated
concepts already proven by gg-monitor-core/inventory.py (canonical key
derivation "gg-${name}", topology-driven role resolution) -- see
monitoring/monitor/tests/test_inventory_drift.py, which runs both loaders
against the same repository fixtures and asserts identical canonical
output, so the two copies can never silently drift apart.

Portal-only scope, smaller than the collector's own inventory.py:
  - No credential-file-path derivation (the portal never touches GoldenGate
    admin credentials, TLS material, or Secrets Manager object names).
  - No process-level pipeline map / manager-compatible deployments.json
    (those are collector/writer concerns, not read by the portal).
  - Adds one check the collector's loader does not need: a topology
    document's role must reference a DEPLOYMENT ALREADY DECLARED in
    pipelines/deployments.yaml -- a portal showing a logical pipeline whose
    role points at an undeclared runtime would be showing a relationship to
    nothing, which must fail loudly at load time instead of rendering a
    dangling reference.

No credentials or secret values are read, parsed, or exposed by this
module.
"""
from __future__ import annotations

import glob
import os

import yaml

REPO_ROOT_ENV = "REPO_CONFIG_ROOT"
DEFAULT_REPO_ROOT = "/etc/gg-canonical"

DEPLOYMENTS_YAML_RELPATH = "pipelines/deployments.yaml"
TOPOLOGIES_GLOB_RELPATH = "topologies/dev/*.yaml"


class InventoryError(Exception):
    """Raised when the canonical inventory/topology sources cannot be
    loaded, or are malformed/inconsistent (duplicate canonical keys,
    dangling topology references, conflicting role declarations)."""


def _repo_root():
    return os.environ.get(REPO_ROOT_ENV, DEFAULT_REPO_ROOT)


def _canonical_key(name):
    """Exact manager derivation: "gg-${d.name}". Applied exactly once."""
    return f"gg-{name}"


def load_deployment_inventory(repo_root=None):
    """Parse pipelines/deployments.yaml into the canonical runtime list.

    Returns a list of {"pipeline": "gg-<name>", "name": ..., "type": ...,
    "enabled": bool} dicts. Rejects a name already carrying the gg- prefix
    (the canonical key is derived exactly once) and duplicate canonical
    keys.
    """
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
    runtimes = []
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
        runtimes.append({
            "pipeline": key,
            "name": entry["name"],
            "type": entry["type"],
            "enabled": bool(entry.get("enabled", False)),
        })
    return runtimes


def load_topology_documents(repo_root=None):
    """Parse every topologies/dev/*.yaml file into a list of
    (path, raw_parsed_document) pairs, in sorted-path order."""
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


def load_runtimes(repo_root=None):
    """Canonical runtime list for the portal -- deliberately just the
    inventory (name/type/enabled), never endpoints/secretReferences/
    credential paths, none of which the portal reads."""
    return load_deployment_inventory(repo_root)


def build_logical_pipelines(repo_root=None):
    """Logical topology relationships for the portal: a logical pipeline
    (e.g. "payments-ora-to-pg-001") is a relationship between canonical
    GoldenGate runtime deployments, never a runtime identity in its own
    right.

    Returns a list of dicts, one per distinct enabled topology document
    pipelineId, sorted by pipelineId:
      {
        "pipelineId": "payments-ora-to-pg-001",
        "environment": "dev",
        "roles": {
          "source": {"pipeline": "gg-oracle-payments-01", "deploymentType": "oracle"},
          "target": {"pipeline": "gg-postgresql-payments-01", "deploymentType": "postgresql"},
        },
      }

    A topology document with lifecycle.enabled == False is skipped
    entirely (not a live logical pipeline this phase). A document with no
    top-level pipelineId is connection-detail-only and is also skipped --
    it declares no logical pipeline relationship.

    Fails loudly (InventoryError) when:
      - a role is malformed (no deploymentName),
      - a role's deploymentName is not declared in
        pipelines/deployments.yaml (dangling reference),
      - a role's own deploymentType conflicts with the inventory's type
        for that deployment,
      - the same pipelineId is declared by more than one document with
        conflicting role assignments.
    """
    runtimes_by_name = {r["pipeline"]: r for r in load_deployment_inventory(repo_root)}

    by_id = {}
    for path, doc in load_topology_documents(repo_root):
        lifecycle = doc.get("lifecycle") or {}
        if lifecycle.get("enabled") is False:
            continue

        pipeline_id = doc.get("pipelineId") or ""
        if not pipeline_id:
            continue  # connection-detail-only document -- not a logical pipeline relationship

        environment = doc.get("environment", "")
        roles = {}
        for role, detail in (doc.get("deployments") or {}).items():
            if not isinstance(detail, dict) or "deploymentName" not in detail:
                raise InventoryError(f"{path}: role {role!r} is malformed (missing deploymentName)")

            deployment_name = detail["deploymentName"]
            declared = runtimes_by_name.get(deployment_name)
            if declared is None:
                raise InventoryError(
                    f"{path}: role {role!r} references undeclared deployment "
                    f"{deployment_name!r} (not present in {DEPLOYMENTS_YAML_RELPATH})"
                )

            topology_type = detail.get("deploymentType")
            if topology_type is not None and topology_type != declared["type"]:
                raise InventoryError(
                    f"{path}: role {role!r} deploymentType {topology_type!r} does not "
                    f"match inventory type {declared['type']!r} for {deployment_name!r}"
                )

            roles[role] = {"pipeline": deployment_name, "deploymentType": declared["type"]}

        if pipeline_id in by_id:
            existing = by_id[pipeline_id]
            for role, value in roles.items():
                if role in existing["roles"] and existing["roles"][role] != value:
                    raise InventoryError(
                        f"pipelineId {pipeline_id!r} has CONFLICTING role {role!r} "
                        f"declarations between documents"
                    )
            existing["roles"].update(roles)
        else:
            by_id[pipeline_id] = {
                "pipelineId": pipeline_id,
                "environment": environment,
                "roles": roles,
            }

    return [by_id[pid] for pid in sorted(by_id)]
