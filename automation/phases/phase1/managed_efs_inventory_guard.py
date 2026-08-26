"""Pure comparison/classification logic for the managed_efs_inventory_guard workflow job; read-only, no AWS calls of its own -- the workflow supplies EXPECTED (from `goldengate-deployment-model.py managed-efs-inventory`) and ACTUAL (from a single `aws efs describe-file-systems` call, whose FileSystemDescription objects already carry CreationToken/LifeCycleState/Tags -- never a second `list-tags-for-resource` call) as JSON, and this module decides pass/fail. Kept import-free of boto3/AWS SDKs on purpose so it stays testable with sanitized fixtures, never live AWS."""
from __future__ import annotations

import json
import re
import sys

# Mirrors automation/goldengate-deployment-model.py's exact safe-token grammar (_TOKEN_RE) for deployment IDs and environments -- the looser trailing/double-hyphen grammar is deliberately not used here.
_SAFE_DEPLOYMENT_ID_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*\Z")
_SAFE_ENVIRONMENT_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*\Z")
# Mirrors derive_efs_creation_token()'s deterministic "<environment>-<deployment_id>-efs" shape and automation/goldengate-deployment-model.py's real AWS EFS creation-token length limit (_EFS_CREATION_TOKEN_MAX_LENGTH).
_SAFE_CREATION_TOKEN_RE = re.compile(r"^[a-z][a-z0-9-]*-efs\Z")
_EFS_CREATION_TOKEN_MAX_LENGTH = 64

MANAGED_BY_VALUE = "goldengate-eks-app"
REQUIRED_STORAGE_VALUE = "u02"

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


def _is_safe_creation_token(value):
    return isinstance(value, str) and bool(_SAFE_CREATION_TOKEN_RE.match(value)) and len(value) <= _EFS_CREATION_TOKEN_MAX_LENGTH


def derive_expected_creation_token(environment, deployment_id):
    """Mirrors automation/goldengate-deployment-model.py's derive_efs_creation_token() exactly -- a separate, dependency-free copy rather than an import, since that module unconditionally requires PyYAML at import time and this one is kept free of it. Regression-tested against the real function to catch any future drift. Takes the FILESYSTEM'S OWN GoldenGateEnvironment/GoldenGateDeploymentId tags, not the current run's environment -- this proves an actual filesystem's CreationToken is self-consistent with its own claimed identity, independent of whether that identity happens to belong to the current environment."""
    return f"{environment}-{deployment_id}-efs"


def check_managed_efs_inventory(expected, actual, environment):
    """expected: [{"deploymentId": ..., "efsCreationToken": ...}, ...] (from the deployment model, includes deployment.enabled=false descriptors). actual: AWS FileSystemDescription-shaped dicts (FileSystemId/CreationToken/Tags) sanitized down to the four GoldenGate tags. Returns the list of orphan deployment IDs (each with the fixed ORPHAN_MESSAGE) -- empty means PASS. Raises InventoryGuardError for a creation-token collision, malformed/missing ownership tags on an otherwise ManagedBy=goldengate-eks-app filesystem (checked in full BEFORE any environment-based ignore decision -- a validly-tagged other-environment resource is the only thing ever silently ignored), a deployment-tag/creation-token identity mismatch, or a duplicate GoldenGateDeploymentId -- all fail closed before any orphan comparison even runs."""
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
        storage_tag = tags.get("GoldenGateStorage")

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

        # From here, ManagedBy is correct: EVERY structural ownership field must be validated in full before any environment-based ignore decision is made -- a resource is never silently excused merely because its environment tag happens to look like a different, valid environment while some other ownership field is missing or malformed.
        if not isinstance(environment_tag, str) or not _SAFE_ENVIRONMENT_RE.match(environment_tag):
            raise InventoryGuardError(
                f"actual EFS {filesystem_id!r} has ManagedBy={MANAGED_BY_VALUE!r} but its GoldenGateEnvironment "
                f"tag is missing or malformed ({environment_tag!r})."
            )

        if not isinstance(deployment_id_tag, str) or not _SAFE_DEPLOYMENT_ID_RE.match(deployment_id_tag):
            raise InventoryGuardError(
                f"actual EFS {filesystem_id!r} has ManagedBy={MANAGED_BY_VALUE!r} but its GoldenGateDeploymentId "
                f"tag is missing or malformed ({deployment_id_tag!r})."
            )

        if not _is_safe_creation_token(creation_token):
            raise InventoryGuardError(
                f"actual EFS {filesystem_id!r} (GoldenGateDeploymentId={deployment_id_tag!r}) has a missing, "
                f"malformed, or oversized CreationToken in its AWS filesystem description ({creation_token!r})."
            )

        if storage_tag != REQUIRED_STORAGE_VALUE:
            raise InventoryGuardError(
                f"actual EFS {filesystem_id!r} (GoldenGateDeploymentId={deployment_id_tag!r}) has GoldenGateStorage="
                f"{storage_tag!r}, expected exactly {REQUIRED_STORAGE_VALUE!r}."
            )

        # Self-consistency: the filesystem's own CreationToken must exactly equal the deterministic token derived from its OWN GoldenGateEnvironment/GoldenGateDeploymentId tags -- checked before any environment-based ignore decision, so a resource cannot claim a foreign identity (e.g. GoldenGateEnvironment=sit, GoldenGateDeploymentId=gg-postgresql-orders-01, CreationToken=random-efs) and be waved through merely because it looks like it belongs to another environment.
        self_consistent_token = derive_expected_creation_token(environment_tag, deployment_id_tag)
        if creation_token != self_consistent_token:
            raise InventoryGuardError(
                f"actual EFS {filesystem_id!r} is tagged GoldenGateEnvironment={environment_tag!r} "
                f"GoldenGateDeploymentId={deployment_id_tag!r}, but its CreationToken ({creation_token!r}) does not "
                f"match the deterministic value derived from its own tags ({self_consistent_token!r})."
            )

        # Only now, after every ownership field has been proven structurally valid and self-consistent, may a genuinely different (real) environment be ignored.
        if environment_tag != environment:
            continue

        # Current-environment lifecycle hardening: this loop only ever reaches filesystems that already exist in AWS -- a brand-new expected descriptor with no AWS EFS yet never enters this loop at all, so it is never failed here; Terraform remains free to create it. "deleting"/"deleted"/"error" are unsafe to proceed with and fail closed before Terraform apply. "creating"/"updating" are treated as in-progress, not failed -- no retry policy is invented here since none exists elsewhere in this workflow.
        lifecycle_state = fs.get("LifeCycleState")
        if lifecycle_state in ("deleting", "deleted", "error"):
            raise InventoryGuardError(
                f"actual EFS {filesystem_id!r} (GoldenGateDeploymentId={deployment_id_tag!r}) is in AWS lifecycle "
                f"state {lifecycle_state!r}, which is unsafe to proceed with."
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
        print("usage: managed_efs_inventory_guard.py <environment> <expected-json-file> <actual-json-file>")
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
