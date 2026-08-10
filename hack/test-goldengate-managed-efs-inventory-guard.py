"""Offline tests for hack/goldengate-managed-efs-inventory-guard.py; run directly via `python3 hack/test-goldengate-managed-efs-inventory-guard.py`. No live AWS -- ACTUAL is always a sanitized fixture shaped like a sanitized aws efs describe-file-systems response (FileSystemId/CreationToken/Tags only, never a separate list-tags-for-resource call)."""
from __future__ import annotations

import importlib.util
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL_PATH = os.path.join(REPO_ROOT, "hack", "goldengate-managed-efs-inventory-guard.py")


def _load_tool():
    spec = importlib.util.spec_from_file_location("goldengate_managed_efs_inventory_guard", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_tool()


def _fs(filesystem_id, creation_token=None, tags=None):
    return {"FileSystemId": filesystem_id, "CreationToken": creation_token, "LifeCycleState": "available", "Tags": [{"Key": k, "Value": v} for k, v in (tags or {}).items()]}


def _expected(deployment_id, token=None):
    return {"deploymentId": deployment_id, "efsCreationToken": token or f"dev-{deployment_id}-efs"}


def _valid_tags(deployment_id, environment="dev"):
    """A fully well-formed ownership tag set -- callers mutate/omit individual keys to exercise a specific malformed/missing field."""
    return {"ManagedBy": "goldengate-eks-app", "GoldenGateDeploymentId": deployment_id, "GoldenGateEnvironment": environment, "GoldenGateStorage": "u02"}


class ZeroManagedTests(unittest.TestCase):
    def test_zero_expected_zero_actual_passes(self):
        orphans = guard.check_managed_efs_inventory([], [], "dev")
        self.assertEqual(orphans, [])

    def test_zero_expected_with_orphan_actual_fails(self):
        actual = [_fs("fs-orphan", "some-token-efs", _valid_tags("gg-orphan"))]
        orphans = guard.check_managed_efs_inventory([], actual, "dev")
        self.assertEqual([o["deploymentId"] for o in orphans], ["gg-orphan"])
        self.assertIn("Terraform apply is blocked", orphans[0]["message"])


class MatchingIdentityTests(unittest.TestCase):
    def test_matching_deployment_tag_and_matching_creation_token_passes(self):
        expected = [_expected("gg-a", "dev-gg-a-efs")]
        actual = [_fs("fs-a", "dev-gg-a-efs", _valid_tags("gg-a"))]
        orphans = guard.check_managed_efs_inventory(expected, actual, "dev")
        self.assertEqual(orphans, [])

    def test_two_expected_two_matching_actual_passes_with_distinct_filesystem_ids(self):
        expected = [_expected("gg-a", "dev-gg-a-efs"), _expected("gg-b", "dev-gg-b-efs")]
        actual = [
            _fs("fs-aaaa", "dev-gg-a-efs", _valid_tags("gg-a")),
            _fs("fs-bbbb", "dev-gg-b-efs", _valid_tags("gg-b")),
        ]
        orphans = guard.check_managed_efs_inventory(expected, actual, "dev")
        self.assertEqual(orphans, [])


class NewManagedDescriptorTests(unittest.TestCase):
    def test_new_managed_descriptor_with_no_aws_efs_yet_is_allowed(self):
        expected = [_expected("gg-brand-new", "dev-gg-brand-new-efs")]
        orphans = guard.check_managed_efs_inventory(expected, [], "dev")
        self.assertEqual(orphans, [])


class OrphanTests(unittest.TestCase):
    def test_actual_managed_efs_with_no_expected_descriptor_fails(self):
        expected = [_expected("gg-a", "dev-gg-a-efs")]
        actual = [
            _fs("fs-a", "dev-gg-a-efs", _valid_tags("gg-a")),
            _fs("fs-b", "dev-gg-b-efs", _valid_tags("gg-b")),
        ]
        orphans = guard.check_managed_efs_inventory(expected, actual, "dev")
        self.assertEqual([o["deploymentId"] for o in orphans], ["gg-b"])


class CreationTokenMismatchTests(unittest.TestCase):
    def test_deployment_tag_matches_but_creation_token_mismatches_fails(self):
        # A well-formed but WRONG token, isolating the identity-mismatch check from the separate malformed-token check.
        expected = [_expected("gg-a", "dev-gg-a-efs")]
        actual = [_fs("fs-a", "dev-gg-different-efs", _valid_tags("gg-a"))]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory(expected, actual, "dev")


class CreationTokenCollisionTests(unittest.TestCase):
    def test_expected_creation_token_on_untagged_filesystem_fails(self):
        expected = [_expected("gg-a", "dev-gg-a-efs")]
        actual = [_fs("fs-x", "dev-gg-a-efs", {})]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory(expected, actual, "dev")

    def test_expected_creation_token_with_wrong_deployment_tag_fails(self):
        expected = [_expected("gg-a", "dev-gg-a-efs")]
        actual = [_fs("fs-x", "dev-gg-a-efs", _valid_tags("gg-b"))]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory(expected, actual, "dev")

    def test_expected_creation_token_with_wrong_environment_fails(self):
        expected = [_expected("gg-a", "dev-gg-a-efs")]
        actual = [_fs("fs-x", "dev-gg-a-efs", _valid_tags("gg-a", environment="sit"))]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory(expected, actual, "dev")


class MalformedOwnershipTagTests(unittest.TestCase):
    def test_managed_by_correct_but_environment_missing_fails(self):
        actual = [_fs("fs-x", "unrelated-efs", {"ManagedBy": "goldengate-eks-app"})]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory([], actual, "dev")

    def test_managed_by_correct_but_deployment_id_missing_fails(self):
        actual = [_fs("fs-x", "unrelated-efs", {"ManagedBy": "goldengate-eks-app", "GoldenGateEnvironment": "dev"})]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory([], actual, "dev")

    def test_managed_by_correct_but_deployment_id_malformed_fails(self):
        tags = _valid_tags("gg-a")
        tags["GoldenGateDeploymentId"] = "Not Safe!"
        actual = [_fs("fs-x", "unrelated-efs", tags)]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory([], actual, "dev")

    def test_managed_by_correct_but_creation_token_missing_fails(self):
        actual = [_fs("fs-x", None, _valid_tags("gg-a"))]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory([], actual, "dev")

    def test_duplicate_deployment_id_across_two_filesystems_fails(self):
        expected = [_expected("gg-a", "dev-gg-a-efs")]
        actual = [
            _fs("fs-1", "dev-gg-a-efs", _valid_tags("gg-a")),
            _fs("fs-2", "dev-gg-a-second-efs", _valid_tags("gg-a")),
        ]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory(expected, actual, "dev")


class UnrelatedEfsIgnoredTests(unittest.TestCase):
    def test_efs_with_no_managed_by_tag_is_ignored(self):
        actual = [_fs("fs-unrelated", "unrelated-efs", {"Name": "some-other-team-filesystem"})]
        orphans = guard.check_managed_efs_inventory([], actual, "dev")
        self.assertEqual(orphans, [])

    def test_efs_with_different_managed_by_value_is_ignored(self):
        tags = _valid_tags("gg-a")
        tags["ManagedBy"] = "some-other-application"
        actual = [_fs("fs-other-app", "unrelated-efs", tags)]
        orphans = guard.check_managed_efs_inventory([], actual, "dev")
        self.assertEqual(orphans, [])


class OwnershipValidationOrderingTests(unittest.TestCase):
    """Issue 2: once ManagedBy=goldengate-eks-app, every ownership field must be validated structurally BEFORE a different-but-valid GoldenGateEnvironment is allowed to silently exclude the resource. Only a fully-valid other-environment resource may be ignored."""

    def test_other_environment_with_fully_valid_metadata_is_ignored(self):
        actual = [_fs("fs-sit", "sit-gg-a-efs", _valid_tags("gg-a", environment="sit"))]
        orphans = guard.check_managed_efs_inventory([], actual, "dev")
        self.assertEqual(orphans, [])

    def test_other_environment_missing_deployment_id_fails(self):
        tags = _valid_tags("gg-a", environment="sit")
        del tags["GoldenGateDeploymentId"]
        actual = [_fs("fs-sit", "sit-gg-a-efs", tags)]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory([], actual, "dev")

    def test_other_environment_malformed_deployment_id_fails(self):
        tags = _valid_tags("gg-a", environment="sit")
        tags["GoldenGateDeploymentId"] = "Not Safe!"
        actual = [_fs("fs-sit", "sit-gg-a-efs", tags)]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory([], actual, "dev")

    def test_other_environment_missing_creation_token_fails(self):
        actual = [_fs("fs-sit", None, _valid_tags("gg-a", environment="sit"))]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory([], actual, "dev")

    def test_other_environment_malformed_creation_token_fails(self):
        actual = [_fs("fs-sit", "not a valid token!", _valid_tags("gg-a", environment="sit"))]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory([], actual, "dev")

    def test_other_environment_missing_goldengate_storage_fails(self):
        tags = _valid_tags("gg-a", environment="sit")
        del tags["GoldenGateStorage"]
        actual = [_fs("fs-sit", "sit-gg-a-efs", tags)]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory([], actual, "dev")

    def test_other_environment_wrong_goldengate_storage_fails(self):
        tags = _valid_tags("gg-a", environment="sit")
        tags["GoldenGateStorage"] = "u03"
        actual = [_fs("fs-sit", "sit-gg-a-efs", tags)]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory([], actual, "dev")

    def test_current_environment_valid_resource_behavior_unchanged(self):
        expected = [_expected("gg-a", "dev-gg-a-efs")]
        actual = [_fs("fs-a", "dev-gg-a-efs", _valid_tags("gg-a", environment="dev"))]
        orphans = guard.check_managed_efs_inventory(expected, actual, "dev")
        self.assertEqual(orphans, [])


class GrammarTests(unittest.TestCase):
    """Tightened grammar checks: deployment IDs use the exact hack/goldengate-deployment-model.py _TOKEN_RE contract (no trailing/double hyphen), creation tokens must look like the deterministic <environment>-<deployment_id>-efs shape and respect the real AWS length limit."""

    def test_deployment_id_with_trailing_hyphen_is_rejected(self):
        tags = _valid_tags("gg-a-", environment="dev")
        actual = [_fs("fs-x", "dev-gg-a--efs", tags)]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory([], actual, "dev")

    def test_deployment_id_with_double_hyphen_is_rejected(self):
        tags = _valid_tags("gg--a", environment="dev")
        actual = [_fs("fs-x", "dev-gg--a-efs", tags)]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory([], actual, "dev")

    def test_oversized_creation_token_is_rejected(self):
        long_token = "dev-" + ("x" * 70) + "-efs"
        tags = _valid_tags("gg-a", environment="dev")
        actual = [_fs("fs-x", long_token, tags)]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory([], actual, "dev")


class LifecycleAbsentInventoryTests(unittest.TestCase):
    def test_lifecycle_absent_descriptor_in_expected_prevents_orphan_failure(self):
        # Mirrors what hack/goldengate-deployment-model.py managed-efs-inventory emits for a managed deployment currently at lifecycle.state=absent -- its EFS is retained, so it must remain "expected" and must not be misclassified as an orphan.
        expected = [_expected("gg-decommissioned-app", "dev-gg-decommissioned-app-efs")]
        actual = [_fs("fs-retained", "dev-gg-decommissioned-app-efs", _valid_tags("gg-decommissioned-app"))]
        orphans = guard.check_managed_efs_inventory(expected, actual, "dev")
        self.assertEqual(orphans, [])


class TagNormalizationTests(unittest.TestCase):
    def test_flat_dict_tags_are_also_accepted(self):
        actual = [{"FileSystemId": "fs-x", "CreationToken": "dev-gg-a-efs", "Tags": _valid_tags("gg-a")}]
        expected = [_expected("gg-a", "dev-gg-a-efs")]
        orphans = guard.check_managed_efs_inventory(expected, actual, "dev")
        self.assertEqual(orphans, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
