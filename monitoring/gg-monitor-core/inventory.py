"""inventory: canonical runtime discovery for the shared gg-monitor.

Single source of truth for "which GoldenGate runtime deployments exist and how
do I reach them" -- loads pipelines/deployments.yaml (the manager-aligned
inventory) and topologies/dev/*.yaml (endpoint/namespace/secret-reference
detail), and merges them into one canonical runtime list.

No second, hardcoded runtime list exists anywhere else in this application --
every other module receives its runtime facts from load_runtimes() here.

Also derives manager-compatible equivalents of the manager's own mounted
ConfigMap inputs (see charts/gg-deployment/files/utility-sidecar.py
build_process_pipeline_map / charts/gg-alerter/files/gg_alerter.py
enabled_deployments in the manager reference repository, inspected read-only):

  - deployments.json: a flat JSON array of canonical keys (gg-<name>) for
    ENABLED deployments only -- exactly the shape gg-alerter's
    enabled_deployments() reads via list(json.load(fh)).
  - process-pipeline-map.json: {PROCESS_NAME_UPPER: {"pipeline_name": ...,
    "deployment": <bare-key-without-gg->}} -- exactly the shape
    build_process_pipeline_map() reads. With the current empty topology
    process lists (no Extract/Replicat/Distribution Path configured yet),
    this is always {}.

This module does NOT claim to reproduce the manager's ConfigMap projection
mechanism (pipeline-3 in the manager repo) -- it is this repository's own
equivalent transformation, run at container startup from our own canonical
YAML sources, not a copy of manager infrastructure.
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
    """Raised when the canonical inventory/topology sources cannot be loaded."""


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
    for entry in entries:
        if not isinstance(entry, dict) or "name" not in entry or "type" not in entry:
            raise InventoryError(f"{path}: each entry requires name and type: {entry!r}")
        if entry["name"].startswith("gg-"):
            raise InventoryError(
                f"{path}: inventory name {entry['name']!r} must not already include "
                "the gg- prefix (the canonical key is derived as gg-${d.name})"
            )
    return entries


def load_topologies(repo_root=None):
    """Parse every topologies/dev/*.yaml file. Returns {deploymentName: detail}
    for every deployment entry found in every topology document's
    deployments.* mapping (source/target/... -- topology key names are not
    load-bearing, only each entry's own deploymentName is)."""
    repo_root = repo_root or _repo_root()
    pattern = os.path.join(repo_root, TOPOLOGIES_GLOB_RELPATH)
    by_deployment_name = {}
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path) as f:
                doc = yaml.safe_load(f) or {}
        except OSError as e:
            raise InventoryError(f"could not read {path}: {e}") from e
        for _role, detail in (doc.get("deployments") or {}).items():
            if not isinstance(detail, dict) or "deploymentName" not in detail:
                continue
            by_deployment_name[detail["deploymentName"]] = detail
    return by_deployment_name


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
        "endpoints": {...},                     # from topology, may be {}
        "secretReferences": {...},               # from topology, may be {}
        "processes": {...},                      # from topology, may be {}
      }

    A deployment declared in pipelines/deployments.yaml with no matching
    topology entry still appears (endpoints/secretReferences/processes
    default to {}) -- CONFIG seeding and lease/state ownership never depend
    on topology being present, matching the Terraform for_each contract this
    mirrors (Phase 3).
    """
    inventory = load_deployment_inventory(repo_root)
    topologies = load_topologies(repo_root)

    runtimes = []
    for entry in inventory:
        pipeline = _canonical_key(entry["name"])
        detail = topologies.get(pipeline, {})
        runtimes.append({
            "pipeline": pipeline,
            "name": entry["name"],
            "type": entry["type"],
            "enabled": bool(entry.get("enabled", False)),
            "namespace": detail.get("namespace", ""),
            "serviceName": detail.get("serviceName", ""),
            "endpoints": detail.get("endpoints", {}),
            "secretReferences": detail.get("secretReferences", {}),
            "processes": detail.get("processes", {}),
        })
    return runtimes


def build_deployments_json(runtimes):
    """Manager-compatible deployments.json: flat list of canonical keys for
    ENABLED deployments only (matches gg_alerter.enabled_deployments())."""
    return [r["pipeline"] for r in runtimes if r["enabled"]]


def build_process_pipeline_map_json(runtimes):
    """Manager-compatible process-pipeline-map.json:
    {PROCESS_NAME_UPPER: {"pipeline_name": ..., "deployment": bare_key}}.

    Built from each runtime's topology processes (extracts/distributionPaths/
    replicats). With today's empty process lists this is always {} -- no
    placeholder Extract/Replicat/Distribution Path names are invented.
    """
    out = {}
    for r in runtimes:
        bare_key = r["name"]
        processes = r.get("processes") or {}
        for _kind, names in processes.items():
            for proc_name in (names or []):
                out[str(proc_name).upper()] = {
                    "pipeline_name": r["pipeline"],
                    "deployment": bare_key,
                }
    return out
