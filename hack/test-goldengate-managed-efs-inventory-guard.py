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


class ZeroManagedTests(unittest.TestCase):
    def test_zero_expected_zero_actual_passes(self):
        orphans = guard.check_managed_efs_inventory([], [], "dev")
        self.assertEqual(orphans, [])

    def test_zero_expected_with_orphan_actual_fails(self):
        actual = [_fs("fs-orphan", "some-token", {"ManagedBy": "goldengate-eks-app", "GoldenGateDeploymentId": "gg-orphan", "GoldenGateEnvironment": "dev"})]
        orphans = guard.check_managed_efs_inventory([], actual, "dev")
        self.assertEqual([o["deploymentId"] for o in orphans], ["gg-orphan"])
        self.assertIn("Terraform apply is blocked", orphans[0]["message"])


class MatchingIdentityTests(unittest.TestCase):
    def test_matching_deployment_tag_and_matching_creation_token_passes(self):
        expected = [_expected("gg-a", "dev-gg-a-efs")]
        actual = [_fs("fs-a", "dev-gg-a-efs", {"ManagedBy": "goldengate-eks-app", "GoldenGateDeploymentId": "gg-a", "GoldenGateEnvironment": "dev"})]
        orphans = guard.check_managed_efs_inventory(expected, actual, "dev")
        self.assertEqual(orphans, [])

    def test_two_expected_two_matching_actual_passes_with_distinct_filesystem_ids(self):
        expected = [_expected("gg-a", "dev-gg-a-efs"), _expected("gg-b", "dev-gg-b-efs")]
        actual = [
            _fs("fs-aaaa", "dev-gg-a-efs", {"ManagedBy": "goldengate-eks-app", "GoldenGateDeploymentId": "gg-a", "GoldenGateEnvironment": "dev"}),
            _fs("fs-bbbb", "dev-gg-b-efs", {"ManagedBy": "goldengate-eks-app", "GoldenGateDeploymentId": "gg-b", "GoldenGateEnvironment": "dev"}),
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
            _fs("fs-a", "dev-gg-a-efs", {"ManagedBy": "goldengate-eks-app", "GoldenGateDeploymentId": "gg-a", "GoldenGateEnvironment": "dev"}),
            _fs("fs-b", "dev-gg-b-efs", {"ManagedBy": "goldengate-eks-app", "GoldenGateDeploymentId": "gg-b", "GoldenGateEnvironment": "dev"}),
        ]
        orphans = guard.check_managed_efs_inventory(expected, actual, "dev")
        self.assertEqual([o["deploymentId"] for o in orphans], ["gg-b"])


class CreationTokenMismatchTests(unittest.TestCase):
    def test_deployment_tag_matches_but_creation_token_mismatches_fails(self):
        expected = [_expected("gg-a", "dev-gg-a-efs")]
        actual = [_fs("fs-a", "totally-different-token", {"ManagedBy": "goldengate-eks-app", "GoldenGateDeploymentId": "gg-a", "GoldenGateEnvironment": "dev"})]
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
        actual = [_fs("fs-x", "dev-gg-a-efs", {"ManagedBy": "goldengate-eks-app", "GoldenGateDeploymentId": "gg-b", "GoldenGateEnvironment": "dev"})]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory(expected, actual, "dev")

    def test_expected_creation_token_with_wrong_environment_fails(self):
        expected = [_expected("gg-a", "dev-gg-a-efs")]
        actual = [_fs("fs-x", "dev-gg-a-efs", {"ManagedBy": "goldengate-eks-app", "GoldenGateDeploymentId": "gg-a", "GoldenGateEnvironment": "sit"})]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory(expected, actual, "dev")


class MalformedOwnershipTagTests(unittest.TestCase):
    def test_managed_by_correct_but_environment_missing_fails(self):
        actual = [_fs("fs-x", "unrelated-token", {"ManagedBy": "goldengate-eks-app"})]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory([], actual, "dev")

    def test_managed_by_correct_but_deployment_id_missing_fails(self):
        actual = [_fs("fs-x", "unrelated-token", {"ManagedBy": "goldengate-eks-app", "GoldenGateEnvironment": "dev"})]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory([], actual, "dev")

    def test_managed_by_correct_but_deployment_id_malformed_fails(self):
        actual = [_fs("fs-x", "unrelated-token", {"ManagedBy": "goldengate-eks-app", "GoldenGateDeploymentId": "Not Safe!", "GoldenGateEnvironment": "dev"})]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory([], actual, "dev")

    def test_managed_by_correct_but_creation_token_missing_fails(self):
        actual = [_fs("fs-x", None, {"ManagedBy": "goldengate-eks-app", "GoldenGateDeploymentId": "gg-a", "GoldenGateEnvironment": "dev"})]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory([], actual, "dev")

    def test_duplicate_deployment_id_across_two_filesystems_fails(self):
        expected = [_expected("gg-a", "dev-gg-a-efs")]
        actual = [
            _fs("fs-1", "dev-gg-a-efs", {"ManagedBy": "goldengate-eks-app", "GoldenGateDeploymentId": "gg-a", "GoldenGateEnvironment": "dev"}),
            _fs("fs-2", "some-other-token", {"ManagedBy": "goldengate-eks-app", "GoldenGateDeploymentId": "gg-a", "GoldenGateEnvironment": "dev"}),
        ]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory(expected, actual, "dev")


class UnrelatedEfsIgnoredTests(unittest.TestCase):
    def test_efs_with_no_managed_by_tag_is_ignored(self):
        actual = [_fs("fs-unrelated", "unrelated-token", {"Name": "some-other-team-filesystem"})]
        orphans = guard.check_managed_efs_inventory([], actual, "dev")
        self.assertEqual(orphans, [])

    def test_efs_with_different_managed_by_value_is_ignored(self):
        actual = [_fs("fs-other-app", "unrelated-token", {"ManagedBy": "some-other-application", "GoldenGateDeploymentId": "gg-a", "GoldenGateEnvironment": "dev"})]
        orphans = guard.check_managed_efs_inventory([], actual, "dev")
        self.assertEqual(orphans, [])

    def test_goldengate_efs_from_a_different_environment_is_ignored(self):
        actual = [_fs("fs-sit", "sit-gg-a-efs", {"ManagedBy": "goldengate-eks-app", "GoldenGateDeploymentId": "gg-a", "GoldenGateEnvironment": "sit"})]
        orphans = guard.check_managed_efs_inventory([], actual, "dev")
        self.assertEqual(orphans, [])


class LifecycleAbsentInventoryTests(unittest.TestCase):
    def test_lifecycle_absent_descriptor_in_expected_prevents_orphan_failure(self):
        # Mirrors what hack/goldengate-deployment-model.py managed-efs-inventory emits for a managed deployment currently at lifecycle.state=absent -- its EFS is retained, so it must remain "expected" and must not be misclassified as an orphan.
        expected = [_expected("gg-decommissioned-app", "dev-gg-decommissioned-app-efs")]
        actual = [_fs("fs-retained", "dev-gg-decommissioned-app-efs", {"ManagedBy": "goldengate-eks-app", "GoldenGateDeploymentId": "gg-decommissioned-app", "GoldenGateEnvironment": "dev"})]
        orphans = guard.check_managed_efs_inventory(expected, actual, "dev")
        self.assertEqual(orphans, [])


class TagNormalizationTests(unittest.TestCase):
    def test_flat_dict_tags_are_also_accepted(self):
        actual = [{"FileSystemId": "fs-x", "CreationToken": "dev-gg-a-efs", "Tags": {"ManagedBy": "goldengate-eks-app", "GoldenGateDeploymentId": "gg-a", "GoldenGateEnvironment": "dev"}}]
        expected = [_expected("gg-a", "dev-gg-a-efs")]
        orphans = guard.check_managed_efs_inventory(expected, actual, "dev")
        self.assertEqual(orphans, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
