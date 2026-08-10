"""Pure comparison/classification logic for the managed_efs_inventory_guard workflow job; read-only, no AWS calls of its own -- the workflow supplies EXPECTED (from `goldengate-deployment-model.py managed-efs-inventory`) and ACTUAL (from a single `aws efs describe-file-systems` call, whose FileSystemDescription objects already carry CreationToken/LifeCycleState/Tags -- never a second `list-tags-for-resource` call) as JSON, and this module decides pass/fail. Kept import-free of boto3/AWS SDKs on purpose so it stays testable with sanitized fixtures, never live AWS."""
from __future__ import annotations

import json
import re
import sys

_SAFE_DEPLOYMENT_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_SAFE_ENVIRONMENT_RE = re.compile(r"^[a-z][a-z0-9-]*$")

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


def check_managed_efs_inventory(expected, actual, environment):
    """expected: [{"deploymentId": ..., "efsCreationToken": ...}, ...] (from the deployment model, includes lifecycle.state=absent). actual: AWS FileSystemDescription-shaped dicts (FileSystemId/CreationToken/Tags) sanitized down to the four GoldenGate tags. Returns the list of orphan deployment IDs (each with the fixed ORPHAN_MESSAGE) -- empty means PASS. Raises InventoryGuardError for a creation-token collision, malformed/missing ownership tags on an otherwise ManagedBy=goldengate-eks-app filesystem, a deployment-tag/creation-token identity mismatch, or a duplicate GoldenGateDeploymentId -- all fail closed before any orphan comparison even runs."""
    expected_by_id = {e["deploymentId"]: e["efsCreationToken"] for e in expected}
    expected_by_token = {e["efsCreationToken"]: e["deploymentId"] for e in expected}

    in_scope = []
    for fs in actual:
        tags = _normalize_tags(fs.get("Tags"))
        filesystem_id = fs.get("FileSystemId")
        creation_token = fs.get("CreationToken")
        managed_by = tags.get("ManagedBy")
        environment_tag = tags.get("GoldenGateEnvironment")
        deployment_id_tag = tags.get("GoldenGateDeploymentId")

        # Creation-token collision check: applies to EVERY actual filesystem regardless of its own tags -- an untagged, mistagged, or wrong-identity filesystem that happens to share one of our deterministic creation tokens is exactly the ambiguous case this guard exists to catch, before Terraform ever sees it.
        if creation_token in expected_by_token:
            expected_deployment_for_token = expected_by_token[creation_token]
            if not (managed_by == MANAGED_BY_VALUE and deployment_id_tag == expected_deployment_for_token and environment_tag == environment):
                raise InventoryGuardError(
                    f"AWS EFS {filesystem_id!r} has CreationToken {creation_token!r}, matching expected managed "
                    f"runtime {expected_deployment_for_token!r}'s efsCreationToken, but its ownership tags "
                    f"(ManagedBy={managed_by!r}, GoldenGateDeploymentId={deployment_id_tag!r}, "
                    f"GoldenGateEnvironment={environment_tag!r}) do not exactly match. Refusing to treat this "
                    f"filesystem ambiguously."
                )

        if managed_by != MANAGED_BY_VALUE:
            continue  # unrelated / non-GoldenGate EFS -- already proven not a token collision above, safely ignored

        # From here, ManagedBy is correct: ownership metadata must be structurally valid, never silently ignored.
        if not isinstance(environment_tag, str) or not _SAFE_ENVIRONMENT_RE.match(environment_tag):
            raise InventoryGuardError(
                f"actual EFS {filesystem_id!r} has ManagedBy={MANAGED_BY_VALUE!r} but its GoldenGateEnvironment "
                f"tag is missing or malformed ({environment_tag!r})."
            )

        if environment_tag != environment:
            continue  # a validly-tagged GoldenGate EFS for a different environment -- already proven not a token collision above

        if not isinstance(deployment_id_tag, str) or not _SAFE_DEPLOYMENT_ID_RE.match(deployment_id_tag):
            raise InventoryGuardError(
                f"actual EFS {filesystem_id!r} has ManagedBy={MANAGED_BY_VALUE!r} but its GoldenGateDeploymentId "
                f"tag is missing or malformed ({deployment_id_tag!r})."
            )

        if not isinstance(creation_token, str) or not creation_token.strip():
            raise InventoryGuardError(
                f"actual EFS {filesystem_id!r} (GoldenGateDeploymentId={deployment_id_tag!r}) has a missing or "
                f"malformed CreationToken in its AWS filesystem description."
            )

        in_scope.append((deployment_id_tag, filesystem_id, creation_token))

    by_deployment_id = {}
    for deployment_id, filesystem_id, creation_token in in_scope:
        by_deployment_id.setdefault(deployment_id, []).append((filesystem_id, creation_token))

    for deployment_id, entries in sorted(by_deployment_id.items()):
        if len(entries) > 1:
            raise InventoryGuardError(
                f"GoldenGateDeploymentId {deployment_id!r} maps to {len(entries)} managed EFS filesystems "
                f"({sorted(fs_id for fs_id, _tok in entries)}) -- exactly one is required per runtime deployment."
            )

    # Defense in depth: the collision pass above already guarantees any in-scope entry whose token matches an expected token has the exact matching deployment_id, so this independently re-derives the same invariant from the deployment_id side (catching a non-colliding, simply-wrong token) with a clearer error message.
    for deployment_id, entries in sorted(by_deployment_id.items()):
        filesystem_id, creation_token = entries[0]
        if deployment_id in expected_by_id and creation_token != expected_by_id[deployment_id]:
            raise InventoryGuardError(
                f"actual EFS {filesystem_id!r} is tagged GoldenGateDeploymentId={deployment_id!r} but its "
                f"CreationToken ({creation_token!r}) does not match that deployment's expected efsCreationToken "
                f"({expected_by_id[deployment_id]!r})."
            )

    actual_ids = set(by_deployment_id.keys())
    expected_ids = set(expected_by_id.keys())
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

    print(f"OK: every actual GoldenGate-managed EFS filesystem in {environment} maps to a current managed deployment descriptor by both identity tag and creation token.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
