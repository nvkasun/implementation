"""Pure comparison/classification logic for the managed_efs_inventory_guard workflow job; read-only, no AWS calls of its own -- the workflow supplies EXPECTED (from `goldengate-deployment-model.py managed-efs-inventory`) and ACTUAL (from `aws efs describe-file-systems` + `aws efs list-tags-for-resource`, combined) as JSON, and this module decides pass/fail. Kept import-free of boto3/AWS SDKs on purpose so it stays testable with sanitized fixtures, never live AWS."""
from __future__ import annotations

import json
import re
import sys

_SAFE_DEPLOYMENT_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")

MANAGED_BY_VALUE = "goldengate-eks-app"

ORPHAN_MESSAGE = (
    "An AWS GoldenGate managed EFS exists without a current managed deployment descriptor. "
    "Terraform apply is blocked to prevent destruction of durable /u02 storage."
)


class InventoryGuardError(Exception):
    """A single fixed, safe-to-print failure reason."""


def _normalize_tags(raw_tags):
    """Accepts the raw AWS [{Key, Value}, ...] shape (or an already-flat dict) and returns a flat {key: value} dict."""
    if isinstance(raw_tags, dict):
        return dict(raw_tags)
    tags = {}
    for item in raw_tags or []:
        key = item.get("Key")
        value = item.get("Value")
        if isinstance(key, str):
            tags[key] = value
    return tags


def in_scope_actual_managed_efs(actual, environment):
    """Filters ACTUAL AWS filesystems down to this environment's GoldenGate-managed set; raises InventoryGuardError for a malformed/missing GoldenGateDeploymentId tag on an otherwise-in-scope filesystem. Returns a list of (deployment_id, filesystem_id) pairs -- unrelated/non-GoldenGate EFS and other-environment GoldenGate EFS are silently excluded, never treated as errors."""
    in_scope = []
    for fs in actual:
        tags = _normalize_tags(fs.get("Tags"))
        if tags.get("ManagedBy") != MANAGED_BY_VALUE:
            continue
        if tags.get("GoldenGateEnvironment") != environment:
            continue

        filesystem_id = fs.get("FileSystemId")
        deployment_id = tags.get("GoldenGateDeploymentId")
        if not isinstance(deployment_id, str) or not _SAFE_DEPLOYMENT_ID_RE.match(deployment_id):
            raise InventoryGuardError(
                f"actual EFS {filesystem_id!r} has ManagedBy={MANAGED_BY_VALUE!r} but its "
                f"GoldenGateDeploymentId tag is missing or malformed ({deployment_id!r})."
            )
        in_scope.append((deployment_id, filesystem_id))
    return in_scope


def check_managed_efs_inventory(expected, actual, environment):
    """expected: [{"deploymentId": ..., "efsCreationToken": ...}, ...] (from the deployment model, includes lifecycle.state=absent). actual: raw AWS filesystem descriptions with a "Tags" list. Returns the list of orphan deployment IDs (each with the fixed ORPHAN_MESSAGE) -- empty means PASS. Raises InventoryGuardError for a structurally malformed actual tag (missing/bad GoldenGateDeploymentId) or a duplicate GoldenGateDeploymentId across multiple filesystems -- both fail closed before any orphan comparison even runs."""
    in_scope = in_scope_actual_managed_efs(actual, environment)

    by_deployment_id = {}
    for deployment_id, filesystem_id in in_scope:
        by_deployment_id.setdefault(deployment_id, []).append(filesystem_id)

    for deployment_id, filesystem_ids in sorted(by_deployment_id.items()):
        if len(filesystem_ids) > 1:
            raise InventoryGuardError(
                f"GoldenGateDeploymentId {deployment_id!r} maps to {len(filesystem_ids)} managed EFS filesystems "
                f"({sorted(filesystem_ids)}) -- exactly one is required per runtime deployment."
            )

    expected_ids = {e["deploymentId"] for e in expected}
    actual_ids = set(by_deployment_id.keys())

    orphans = sorted(actual_ids - expected_ids)
    return [{"deploymentId": deployment_id, "message": ORPHAN_MESSAGE} for deployment_id in orphans]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 3:
        print("usage: goldengate-managed-efs-inventory-guard.py <environment> <expected-json-file> <actual-json-file>")
        return 2

    environment, expected_path, actual_path = argv
    with open(expected_path) as f:
        expected = json.load(f)
    with open(actual_path) as f:
        actual = json.load(f)

    try:
        orphans = check_managed_efs_inventory(expected, actual, environment)
    except InventoryGuardError as exc:
        print(f"FAIL: {exc}")
        return 1

    if orphans:
        for orphan in orphans:
            print(f"FAIL: {orphan['deploymentId']}: {orphan['message']}")
        return 1

    print(f"OK: every actual GoldenGate-managed EFS filesystem in {environment} maps to a current managed deployment descriptor.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
