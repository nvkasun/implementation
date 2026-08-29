#!/usr/bin/env python3
"""automation/orchestration/end_to_end_acceptance.py: offline/pure GoldenGate monitor-to-runtime end-to-end acceptance classifier (Phase B3B) -- answers exactly one question, "does the shared monitor currently see every GLOBAL active GoldenGate runtime as fresh, UP, and safe?", as one of HEALTHY/BROKEN. This tool NEVER accesses Kubernetes or AWS itself: it consumes (1) the environment name, (2) the canonical folder-driven ACTIVE deployment model (automation/goldengate-deployment-model.py's own scan/validation -- read from the local repository, never re-parsed independently), and (3) an already-captured JSON response from GET http://127.0.0.1:8080/api/processes (saved to a local file by the calling workflow via a bounded, read-only kubectl exec against the SAME verified Ready monitor pod automation/orchestration/monitor_acceptance.py selected -- this tool never fetches it itself). The existing automation/orchestration/replication_state.py-adjacent exact-process-name replication acceptance (invoked by the 00-main workflow's own replication_monitor_acceptance job) remains the sole authority for exact expected Extract/Distribution Path/Replicat process names and startOnCreate semantics -- this tool deliberately does not duplicate that; it only proves the monitor-to-runtime health envelope and generic per-process safety (never stale, never ABENDED)."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

_DEPLOYMENT_MODEL_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "goldengate-deployment-model.py")
_deployment_model_module = None


def _load_deployment_model_module():
    """Lazy import of automation/goldengate-deployment-model.py -- the single canonical folder-driven descriptor resolver. Never a second independent descriptor schema. Reading local envs/<environment>/*/values.yaml files is offline/pure -- no Kubernetes or AWS call is involved."""
    global _deployment_model_module
    if _deployment_model_module is None:
        spec = importlib.util.spec_from_file_location("goldengate_deployment_model", _DEPLOYMENT_MODEL_MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _deployment_model_module = module
    return _deployment_model_module


def load_active_deployments(environment):
    """Returns [{"deploymentId", "deploymentType", "replicationEnabled"}, ...] for the environment's GLOBAL active runtime inventory. Raises ValueError (a configuration error, never a BROKEN acceptance result) if the folder-driven model itself has a problem."""
    gdm = _load_deployment_model_module()
    gdm.REPO_ROOT = REPO_ROOT
    active, _inactive, invalid, problems = gdm._run_full_validation(environment)
    if invalid or problems:
        raise ValueError(f"the folder-driven deployment model for {environment!r} has validation problems -- refusing to accept end-to-end runtime health against an inconsistent model")
    return [{"deploymentId": d["deploymentId"], "deploymentType": d["deploymentType"], "replicationEnabled": d["replicationEnabled"]} for d in active]


STATE_HEALTHY = "HEALTHY"
STATE_BROKEN = "BROKEN"

# monitor.py's own build_processes_payload()/read_deployment_processes_view() output contract -- verified against the real monitoring/monitor/monitor.py source, never guessed.
_NON_REPLICATION_ALLOWED_DISCOVERY_STATUSES = (None, "EMPTY", "OK")


def _describe_malformed_value(value, max_repr_len=48):
    """Bounded, non-dumping diagnostic description of a possibly-malformed API value: 'TypeName=repr' for a short scalar (str/int/float/bool/None), or just 'TypeName' for any container or oversized scalar -- a malformed field can never inject a large/raw payload into acceptance diagnostics."""
    type_name = type(value).__name__
    if value is None or isinstance(value, (str, int, float, bool)):
        text = repr(value)
        if len(text) <= max_repr_len:
            return f"{type_name}={text}"
    return type_name


def classify(environment, active_deployments, api_processes_doc):
    """Returns the stable {"state", "environment", "reasons", "checks"} shape (state is HEALTHY or BROKEN only). Pure function: no I/O, no Kubernetes/AWS access -- everything it needs is already in its arguments."""
    reasons = []
    checks = {}

    expected_by_id = {d["deploymentId"]: d for d in active_deployments}
    expected_names = set(expected_by_id)

    api_deployments = api_processes_doc.get("deployments") if isinstance(api_processes_doc, dict) else None
    if not isinstance(api_deployments, list):
        reasons.append("/api/processes response is missing a 'deployments' list")
        checks["expected_deployment_count"] = len(expected_names)
        checks["actual_deployment_count"] = 0
        return {"state": STATE_BROKEN, "environment": environment, "reasons": reasons, "checks": checks}

    # Every list member must be validated, never silently discarded: a malformed row (not an object, or missing/null/empty/non-string deploymentName) is itself a BROKEN condition -- it must not be excluded from consideration as though it simply didn't exist, or the exact GLOBAL inventory contract could be satisfied by coincidence while a malformed row goes unnoticed.
    actual_by_name = {}
    for index, entry in enumerate(api_deployments):
        if not isinstance(entry, dict):
            reasons.append(f"/api/processes deployment row #{index} is not an object: {entry!r}")
            continue
        deployment_name = entry.get("deploymentName")
        if deployment_name is None:
            reasons.append(f"/api/processes deployment row #{index} is missing deploymentName")
            continue
        if not isinstance(deployment_name, str):
            reasons.append(f"/api/processes deployment row #{index} has a non-string deploymentName: {deployment_name!r}")
            continue
        if deployment_name == "":
            reasons.append(f"/api/processes deployment row #{index} has an empty deploymentName")
            continue
        actual_by_name.setdefault(deployment_name, []).append(entry)

    for name, entries in actual_by_name.items():
        if len(entries) > 1:
            reasons.append(f"/api/processes has duplicate deploymentName {name!r} ({len(entries)} entries)")

    actual_names = set(actual_by_name)
    checks["expected_deployment_count"] = len(expected_names)
    checks["actual_deployment_count"] = len(actual_names)

    missing = expected_names - actual_names
    if missing:
        reasons.append(f"/api/processes is missing expected ACTIVE deployment(s) {sorted(missing)!r}")

    extra = actual_names - expected_names
    if extra:
        reasons.append(f"/api/processes reports unexpected/stale deployment(s) {sorted(extra)!r} not in the current GLOBAL active inventory")

    for name in sorted(expected_names & actual_names):
        expected = expected_by_id[name]
        entry = actual_by_name[name][0]

        if entry.get("deploymentName") != name:
            reasons.append(f"deployment {name!r}: deploymentName={entry.get('deploymentName')!r}, expected {name!r}")
        if entry.get("deploymentType") != expected["deploymentType"]:
            reasons.append(f"deployment {name!r}: deploymentType={entry.get('deploymentType')!r}, expected {expected['deploymentType']!r}")
        if entry.get("enabled") is not True:
            reasons.append(f"deployment {name!r}: enabled={entry.get('enabled')!r}, expected true")

        effective_status = entry.get("effectiveStatus")
        if effective_status != "UP":
            reasons.append(f"deployment {name!r}: effectiveStatus={effective_status!r}, expected 'UP'")
        if entry.get("fresh") is not True:
            reasons.append(f"deployment {name!r}: fresh={entry.get('fresh')!r}, expected true")
        age_seconds = entry.get("ageSeconds")
        if not isinstance(age_seconds, int) or isinstance(age_seconds, bool) or age_seconds < 0:
            reasons.append(f"deployment {name!r}: ageSeconds={age_seconds!r} is not a sane non-negative integer")

        lease = entry.get("lease")
        if not isinstance(lease, dict):
            reasons.append(f"deployment {name!r}: no current lease ownership recorded")
        else:
            if lease.get("fresh") is not True:
                reasons.append(f"deployment {name!r}: lease.fresh={lease.get('fresh')!r}, expected true")
            if not lease.get("holder"):
                reasons.append(f"deployment {name!r}: lease.holder is empty -- no current writer owns this deployment's monitoring data")

        critical_services = entry.get("criticalServices")
        if not isinstance(critical_services, dict) or not critical_services:
            reasons.append(f"deployment {name!r}: criticalServices is empty, expected at least one reported critical service")
        else:
            unreachable = sorted(svc for svc, reachable in critical_services.items() if reachable is not True)
            if unreachable:
                reasons.append(f"deployment {name!r}: critical service(s) not reachable: {unreachable!r}")

        replication_enabled = bool(expected.get("replicationEnabled"))
        process_discovery = entry.get("processDiscovery")
        # The monitor API contract (normalize_process_discovery()) only ever emits null or an object -- a non-dict, non-null value (a string/list/number/bool) is itself a malformed-schema condition and must never be silently coerced into "absent" (None), or a malformed value would be indistinguishable from a legitimately absent discovery result for a replication-disabled deployment.
        discovery_shape_valid = process_discovery is None or isinstance(process_discovery, dict)
        if not discovery_shape_valid:
            reasons.append(f"deployment {name!r}: processDiscovery must be null or an object per the monitor API contract, got {_describe_malformed_value(process_discovery)}")
        else:
            discovery_status = process_discovery.get("status") if isinstance(process_discovery, dict) else None
            if replication_enabled:
                # The existing replication_monitor_acceptance job remains authoritative for exact expected process names/startOnCreate -- this only proves discovery itself succeeded.
                if discovery_status != "OK":
                    reasons.append(f"deployment {name!r}: participates in enabled replication but processDiscovery.status={discovery_status!r}, expected 'OK'")
            else:
                if discovery_status not in _NON_REPLICATION_ALLOWED_DISCOVERY_STATUSES:
                    reasons.append(f"deployment {name!r}: replication is not enabled but processDiscovery.status={discovery_status!r}, expected EMPTY, OK, or absent (an empty process list is valid when no replication process is desired)")

        # monitor.py's read_deployment_processes_view() always emits an actual JSON array for "processes" (possibly empty) -- `entry.get("processes") or []` previously coerced ANY falsey malformed value ({}, "", 0, false) into a legitimate empty list, and a missing key was treated identically. The current API always includes this key, so a missing key is malformed too, never a silent default.
        processes_present = "processes" in entry
        processes_raw = entry.get("processes") if processes_present else None
        if not isinstance(processes_raw, list):
            if not processes_present:
                reasons.append(f"deployment {name!r}: /api/processes response is missing the required 'processes' field (expected a JSON array per the monitor API contract)")
            else:
                reasons.append(f"deployment {name!r}: processes must be a JSON array per the monitor API contract, got {_describe_malformed_value(processes_raw)}")
            process_rows = []
        else:
            process_rows = processes_raw

        for row_index, row in enumerate(process_rows):
            if not isinstance(row, dict):
                reasons.append(f"deployment {name!r}: process row #{row_index} is not an object (got {_describe_malformed_value(row)})")
                continue

            raw_process_name = row.get("process")
            if isinstance(raw_process_name, str) and raw_process_name != "":
                process_label = raw_process_name
            else:
                process_label = f"#{row_index}"
                reasons.append(f"deployment {name!r}: process row #{row_index} has an invalid 'process' identity (expected a non-empty string, got {_describe_malformed_value(raw_process_name)})")

            # The normalized monitor contract supplies a literal Boolean -- truthiness is never used here, so a missing/null/string/integer stale field is its own malformed-schema condition rather than being silently evaluated as falsey (not stale).
            stale = row.get("stale")
            if isinstance(stale, bool):
                if stale is True:
                    reasons.append(f"deployment {name!r} process {process_label!r}: stale=true")
            else:
                reasons.append(f"deployment {name!r} process {process_label!r}: stale is not a boolean per the monitor API contract (got {_describe_malformed_value(stale)})")

            status = row.get("status")
            if not isinstance(status, str) or status == "":
                reasons.append(f"deployment {name!r} process {process_label!r}: status is not a non-empty string per the monitor API contract (got {_describe_malformed_value(status)})")
            elif status == "ABENDED":
                reasons.append(f"deployment {name!r} process {process_label!r}: status=ABENDED")

    state = STATE_BROKEN if reasons else STATE_HEALTHY
    return {"state": state, "environment": environment, "reasons": reasons, "checks": checks}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--api-processes-file", required=True, help="Path to a locally-saved JSON response captured from GET http://127.0.0.1:8080/api/processes against the verified Ready monitor pod -- never fetched by this tool itself.")
    args = parser.parse_args(argv)

    try:
        active_deployments = load_active_deployments(args.environment)
        with open(args.api_processes_file) as f:
            api_processes_doc = json.load(f)
    except ValueError as exc:
        print(f"CONFIGURATION ERROR: {exc}", file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError) as exc:
        print(f"INPUT ERROR: {exc}", file=sys.stderr)
        return 1

    result = classify(args.environment, active_deployments, api_processes_doc)

    print(json.dumps(result))
    if result["reasons"]:
        print("GoldenGate end-to-end monitor-to-runtime acceptance diagnostics:", file=sys.stderr)
        for reason in result["reasons"]:
            print(f"  - {reason}", file=sys.stderr)
    return 0 if result["state"] == STATE_HEALTHY else 1


if __name__ == "__main__":
    sys.exit(main())
