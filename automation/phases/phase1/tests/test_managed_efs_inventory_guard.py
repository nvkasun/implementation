"""Offline tests for automation/phases/phase1/managed_efs_inventory_guard.py; run directly via `python3 automation/phases/phase1/tests/test_managed_efs_inventory_guard.py`. No live AWS -- ACTUAL is always a sanitized fixture shaped like a sanitized aws efs describe-file-systems response (FileSystemId/CreationToken/Tags only, never a separate list-tags-for-resource call)."""
from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parents[4])
TOOL_PATH = os.path.join(REPO_ROOT, "automation", "phases", "phase1", "managed_efs_inventory_guard.py")

try:
    import yaml  # noqa: F401
    _PYYAML_AVAILABLE = True
except ImportError:
    _PYYAML_AVAILABLE = False


def _load_tool():
    spec = importlib.util.spec_from_file_location("goldengate_managed_efs_inventory_guard", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_tool()


def _fs(filesystem_id, creation_token=None, tags=None, lifecycle_state="available"):
    return {"FileSystemId": filesystem_id, "CreationToken": creation_token, "LifeCycleState": lifecycle_state, "Tags": [{"Key": k, "Value": v} for k, v in (tags or {}).items()]}


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
        actual = [_fs("fs-orphan", "dev-gg-orphan-efs", _valid_tags("gg-orphan"))]
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
        # Both filesystems use the same self-consistent token (derived from the same GoldenGateDeploymentId) so this genuinely exercises the duplicate-ID check, not the self-consistency check.
        expected = [_expected("gg-a", "dev-gg-a-efs")]
        actual = [
            _fs("fs-1", "dev-gg-a-efs", _valid_tags("gg-a")),
            _fs("fs-2", "dev-gg-a-efs", _valid_tags("gg-a")),
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


class SelfConsistentOwnershipIdentityTests(unittest.TestCase):
    """Item 3: an actual filesystem's own CreationToken must exactly equal <GoldenGateEnvironment>-<GoldenGateDeploymentId>-efs derived from its OWN tags, checked before any environment-based ignore decision."""

    @unittest.skipUnless(_PYYAML_AVAILABLE, "automation/goldengate-deployment-model.py requires PyYAML at import time")
    def test_derive_expected_creation_token_matches_the_deployment_model_exactly(self):
        # Regression proof against drift: mirrors, rather than imports, automation/goldengate-deployment-model.py's derive_efs_creation_token(); this proves the two stay identical for representative inputs.
        spec = importlib.util.spec_from_file_location("goldengate_deployment_model", os.path.join(REPO_ROOT, "automation", "goldengate-deployment-model.py"))
        dm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dm)
        for environment, deployment_id in [("dev", "gg-a"), ("sit", "gg-postgresql-orders-01"), ("prod", "gg-mssql-repltest-02")]:
            self.assertEqual(
                guard.derive_expected_creation_token(environment, deployment_id),
                dm.derive_efs_creation_token(environment, deployment_id),
            )

    def test_other_environment_fully_self_consistent_is_valid_then_ignored(self):
        # The exact example given: a fully valid other-environment resource whose token derives correctly from its own tags -- valid ownership identity, THEN ignored because current environment=dev.
        tags = {"ManagedBy": "goldengate-eks-app", "GoldenGateEnvironment": "sit", "GoldenGateDeploymentId": "gg-postgresql-orders-01", "GoldenGateStorage": "u02"}
        actual = [_fs("fs-sit", "sit-gg-postgresql-orders-01-efs", tags)]
        orphans = guard.check_managed_efs_inventory([], actual, "dev")
        self.assertEqual(orphans, [])

    def test_other_environment_self_inconsistent_token_fails_even_during_a_dev_run(self):
        # The exact counter-example: GoldenGateEnvironment=sit, GoldenGateDeploymentId=gg-postgresql-orders-01, but CreationToken=random-efs does not match the derived value -- must FAIL CLOSED even though the run's own environment is dev and this resource would otherwise be ignorable as "another environment."
        tags = {"ManagedBy": "goldengate-eks-app", "GoldenGateEnvironment": "sit", "GoldenGateDeploymentId": "gg-postgresql-orders-01", "GoldenGateStorage": "u02"}
        actual = [_fs("fs-sit", "random-efs", tags)]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory([], actual, "dev")

    def test_current_environment_self_inconsistent_token_fails(self):
        tags = _valid_tags("gg-a", environment="dev")
        actual = [_fs("fs-x", "dev-gg-wrong-id-efs", tags)]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory([], actual, "dev")


class PipelineAwareLegacyEfsTokenMigrationTests(unittest.TestCase):
    """Pipeline-Aware Descriptor Hierarchy Migration: proves the exact four (environment, NEW deployment ID) legacy overrides are applied, that they never leak to any other deployment ID, that this module's copy agrees byte-for-byte with automation/goldengate-deployment-model.py's own canonical copy (drift test), and that the real post-migration self-consistency scenario (an actual AWS filesystem tagged with the NEW deployment ID but still carrying its OLD, immutable CreationToken) passes cleanly."""

    LEGACY_PAIRS = [
        ("dev", "gg-postgresql-repltest-001", "dev-gg-postgresql-repltest-01-efs"),
        ("dev", "gg-mssql-repltest-001", "dev-gg-mssql-repltest-01-efs"),
        ("dev", "gg-oracle-repltest-002", "dev-gg-oracle-repltest-01-efs"),
        ("dev", "gg-postgresql-repltest-002", "dev-gg-postgresql-repltest-02-efs"),
    ]

    def test_each_legacy_pair_derives_its_exact_pre_migration_token(self):
        for environment, deployment_id, expected_token in self.LEGACY_PAIRS:
            with self.subTest(deployment_id=deployment_id):
                self.assertEqual(guard.derive_expected_creation_token(environment, deployment_id), expected_token)

    def test_legacy_mapping_never_applies_to_a_different_environment(self):
        # The exact same NEW deployment ID string under a DIFFERENT (hypothetical) environment must never inherit dev's legacy token -- it is keyed by the full (environment, deployment_id) pair, never deployment_id alone.
        self.assertEqual(guard.derive_expected_creation_token("sit", "gg-postgresql-repltest-001"), "sit-gg-postgresql-repltest-001-efs")

    def test_old_pre_migration_deployment_ids_are_not_in_the_legacy_map(self):
        # The OLD deployment IDs (gg-postgresql-repltest-01, etc.) are never map keys -- only the NEW ones are. An actual AWS filesystem still tagged with an OLD ID (before the real, separately-approved live migration has actually run) must keep deriving the plain, unchanged naive formula.
        for environment, deployment_id in [("dev", "gg-postgresql-repltest-01"), ("dev", "gg-mssql-repltest-01"), ("dev", "gg-oracle-repltest-01"), ("dev", "gg-postgresql-repltest-02")]:
            with self.subTest(deployment_id=deployment_id):
                self.assertEqual(guard.derive_expected_creation_token(environment, deployment_id), f"{environment}-{deployment_id}-efs")

    @unittest.skipUnless(_PYYAML_AVAILABLE, "automation/goldengate-deployment-model.py requires PyYAML at import time")
    def test_legacy_map_matches_the_deployment_model_exactly(self):
        # Regression proof against drift: this module's own LEGACY_MANAGED_EFS_CREATION_TOKENS copy must be byte-for-byte identical to automation/goldengate-deployment-model.py's canonical one -- mirrored, never imported, exactly like derive_expected_creation_token()'s own drift test above.
        spec = importlib.util.spec_from_file_location("goldengate_deployment_model", os.path.join(REPO_ROOT, "automation", "goldengate-deployment-model.py"))
        dm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dm)
        self.assertEqual(guard.LEGACY_MANAGED_EFS_CREATION_TOKENS, dm.LEGACY_MANAGED_EFS_CREATION_TOKENS)

    def test_post_migration_actual_filesystem_self_consistency_passes(self):
        # The real scenario this override exists for: after the separately-approved live Terraform apply, an actual AWS filesystem is tagged with the NEW deployment ID (GoldenGateDeploymentId updated in place) but its CreationToken remains the OLD, immutable value (ForceNew -- never changes on an existing resource). Self-consistency (and therefore the whole guard) must pass cleanly, never fail closed, for exactly this shape.
        expected = [_expected("gg-oracle-repltest-002", "dev-gg-oracle-repltest-01-efs")]
        actual = [_fs("fs-oracle", "dev-gg-oracle-repltest-01-efs", _valid_tags("gg-oracle-repltest-002", environment="dev"))]
        orphans = guard.check_managed_efs_inventory(expected, actual, "dev")
        self.assertEqual(orphans, [])

    def test_post_migration_wrong_leftover_old_tag_still_fails_closed(self):
        # Defense in depth: an actual filesystem tagged with the NEW deployment ID but a token that matches NEITHER the legacy value NOR the naive new-ID formula must still fail closed -- the override never becomes a blanket excuse to skip self-consistency.
        actual = [_fs("fs-oracle", "dev-gg-totally-different-efs", _valid_tags("gg-oracle-repltest-002", environment="dev"))]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory([], actual, "dev")


class PendingDeploymentIdMigrationTests(unittest.TestCase):
    """Managed EFS Identity Migration Correction (First-Live-Run Tag Convergence): the Terraform `moved` blocks (envs/dev/efs.tf) migrate the STATE ADDRESS on the very next `terraform apply`, but this read-only guard runs in Phase 1 BEFORE that apply -- so on the very first post-migration push, an actual pre-existing AWS filesystem may still carry one of exactly four bounded OLD GoldenGateDeploymentId tags while the current Git inventory already expects only the mapped NEW deployment ID. Proves the exact nine-condition bounded acceptance contract: any single condition failing (wrong token, wrong mapped target, unknown old id, wrong environment, wrong ManagedBy, wrong storage, duplicate identity) must still fail exactly as fail-closed as before this mechanism existed."""

    MIGRATION_PAIRS = [
        ("dev", "gg-postgresql-repltest-01", "gg-postgresql-repltest-001", "dev-gg-postgresql-repltest-01-efs"),
        ("dev", "gg-mssql-repltest-01", "gg-mssql-repltest-001", "dev-gg-mssql-repltest-01-efs"),
        ("dev", "gg-oracle-repltest-01", "gg-oracle-repltest-002", "dev-gg-oracle-repltest-01-efs"),
        ("dev", "gg-postgresql-repltest-02", "gg-postgresql-repltest-002", "dev-gg-postgresql-repltest-02-efs"),
    ]

    def test_each_bounded_pair_is_present_and_maps_to_its_exact_new_id(self):
        for environment, old_id, new_id, _token in self.MIGRATION_PAIRS:
            with self.subTest(old_id=old_id):
                self.assertEqual(guard.PENDING_DEPLOYMENT_ID_MIGRATIONS.get((environment, old_id)), new_id)

    def test_no_pending_migration_mapping_exists_for_an_unrelated_or_future_runtime(self):
        self.assertEqual(len(guard.PENDING_DEPLOYMENT_ID_MIGRATIONS), 4)
        for key in guard.PENDING_DEPLOYMENT_ID_MIGRATIONS:
            self.assertIn(key, {(env, old_id) for env, old_id, _new, _tok in self.MIGRATION_PAIRS})
        self.assertNotIn(("dev", "gg-postgresql-repltest-001"), guard.PENDING_DEPLOYMENT_ID_MIGRATIONS)
        self.assertNotIn(("dev", "gg-brand-new-runtime-003"), guard.PENDING_DEPLOYMENT_ID_MIGRATIONS)

    def test_4_first_run_old_tag_exact_old_token_mapped_new_expected_passes(self):
        # Requirement 4: existing EFS with OLD deployment tag + exact old token + mapped NEW expected deployment passes the first-run guard.
        for environment, old_id, new_id, token in self.MIGRATION_PAIRS:
            with self.subTest(old_id=old_id):
                expected = [_expected(new_id, token)]
                actual = [_fs(f"fs-{old_id}", token, _valid_tags(old_id, environment=environment))]
                orphans = guard.check_managed_efs_inventory(expected, actual, environment)
                self.assertEqual(orphans, [])

    def test_5_steady_state_new_tag_with_same_old_immutable_token_passes(self):
        # Requirement 5: the SAME physical filesystem, now tagged with the NEW deployment ID (Terraform has applied the moved block), still carries the OLD immutable token -- normal steady state, no migration-map lookup involved at all.
        for environment, _old_id, new_id, token in self.MIGRATION_PAIRS:
            with self.subTest(new_id=new_id):
                expected = [_expected(new_id, token)]
                actual = [_fs(f"fs-{new_id}", token, _valid_tags(new_id, environment=environment))]
                orphans = guard.check_managed_efs_inventory(expected, actual, environment)
                self.assertEqual(orphans, [])

    def test_6_old_tag_with_wrong_creation_token_fails(self):
        expected = [_expected("gg-oracle-repltest-002", "dev-gg-oracle-repltest-01-efs")]
        actual = [_fs("fs-oracle", "dev-gg-oracle-repltest-01-efs-WRONG", _valid_tags("gg-oracle-repltest-01", environment="dev"))]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory(expected, actual, "dev")

    def test_7_old_tag_whose_token_belongs_to_a_different_expected_deployment_fails(self):
        # The OLD id's bounded map target is gg-oracle-repltest-002, but this filesystem's actual CreationToken belongs to an entirely different expected deployment (gg-mssql-repltest-001) -- eligibility must fail, falling through to the pre-existing collision-identity-mismatch rule.
        expected = [
            _expected("gg-oracle-repltest-002", "dev-gg-oracle-repltest-01-efs"),
            _expected("gg-mssql-repltest-001", "dev-gg-mssql-repltest-01-efs"),
        ]
        actual = [_fs("fs-oracle", "dev-gg-mssql-repltest-01-efs", _valid_tags("gg-oracle-repltest-01", environment="dev"))]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory(expected, actual, "dev")

    def test_8_unknown_old_deployment_id_is_not_pending_and_orphans_under_existing_rules(self):
        expected = [_expected("gg-oracle-repltest-002", "dev-gg-oracle-repltest-01-efs")]
        actual = [_fs("fs-unknown", "dev-gg-totally-unknown-01-efs", _valid_tags("gg-totally-unknown-01", environment="dev"))]
        orphans = guard.check_managed_efs_inventory(expected, actual, "dev")
        self.assertEqual([o["deploymentId"] for o in orphans], ["gg-totally-unknown-01"])

    def test_9_wrong_environment_tag_on_an_otherwise_eligible_old_tag_fails(self):
        expected = [_expected("gg-oracle-repltest-002", "dev-gg-oracle-repltest-01-efs")]
        actual = [_fs("fs-oracle", "dev-gg-oracle-repltest-01-efs", _valid_tags("gg-oracle-repltest-01", environment="sit"))]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory(expected, actual, "dev")

    def test_10_wrong_managed_by_on_an_otherwise_eligible_old_tag_fails(self):
        expected = [_expected("gg-oracle-repltest-002", "dev-gg-oracle-repltest-01-efs")]
        tags = _valid_tags("gg-oracle-repltest-01", environment="dev")
        tags["ManagedBy"] = "something-else"
        actual = [_fs("fs-oracle", "dev-gg-oracle-repltest-01-efs", tags)]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory(expected, actual, "dev")

    def test_11_wrong_storage_tag_on_an_otherwise_eligible_old_tag_fails(self):
        expected = [_expected("gg-oracle-repltest-002", "dev-gg-oracle-repltest-01-efs")]
        tags = _valid_tags("gg-oracle-repltest-01", environment="dev")
        tags["GoldenGateStorage"] = "u01"
        actual = [_fs("fs-oracle", "dev-gg-oracle-repltest-01-efs", tags)]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory(expected, actual, "dev")

    def test_12_old_tagged_and_new_tagged_duplicate_of_the_same_effective_identity_fails(self):
        # Requirement 9/12: the OLD-tagged (pending) filesystem and a genuinely separate NEW-tagged filesystem must never both silently satisfy the same expected deployment -- duplicate/ambiguous identity fails exactly like today's pre-existing duplicate-GoldenGateDeploymentId rule.
        expected = [_expected("gg-oracle-repltest-002", "dev-gg-oracle-repltest-01-efs")]
        actual = [
            _fs("fs-oracle-old", "dev-gg-oracle-repltest-01-efs", _valid_tags("gg-oracle-repltest-01", environment="dev")),
            _fs("fs-oracle-new", "dev-gg-oracle-repltest-01-efs", _valid_tags("gg-oracle-repltest-002", environment="dev")),
        ]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory(expected, actual, "dev")

    def test_13_unrelated_filesystem_colliding_with_a_migration_pairs_legacy_token_still_fails(self):
        # Requirement 13: a completely unrelated/mistagged filesystem that happens to share one of the four legacy tokens is still the ambiguous case the pre-existing collision check exists to catch -- the pending-migration mechanism never widens that check.
        expected = [_expected("gg-oracle-repltest-002", "dev-gg-oracle-repltest-01-efs")]
        actual = [_fs("fs-impostor", "dev-gg-oracle-repltest-01-efs", _valid_tags("gg-totally-unrelated", environment="dev"))]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory(expected, actual, "dev")

    @unittest.skipUnless(_PYYAML_AVAILABLE, "automation/phases/phase1/detect-goldengate-deployments.sh's KNOWN_DEPLOYMENT_ID_MIGRATIONS is compared against this module's own bounded map by literal (old, new) pairs, independent of PyYAML -- guarded only to match this file's existing convention for cross-module drift tests")
    def test_pending_map_agrees_with_the_bash_detection_scripts_known_migration_pairs(self):
        # Drift test: automation/phases/phase1/detect-goldengate-deployments.sh's own KNOWN_DEPLOYMENT_ID_MIGRATIONS bash associative array must name the exact same four (old -> new) pairs as this module's PENDING_DEPLOYMENT_ID_MIGRATIONS (dev-scoped) -- both exist to recognize the SAME one-time migration, from two different execution contexts (Phase 1 bash discovery vs. this Python inventory guard), and must never drift apart.
        import re
        detect_script_path = os.path.join(REPO_ROOT, "automation", "phases", "phase1", "detect-goldengate-deployments.sh")
        with open(detect_script_path) as f:
            detect_script_source = f.read()
        match = re.search(r"declare -A KNOWN_DEPLOYMENT_ID_MIGRATIONS=\((.*?)\)\n", detect_script_source, re.S)
        self.assertIsNotNone(match, "could not locate KNOWN_DEPLOYMENT_ID_MIGRATIONS in detect-goldengate-deployments.sh")
        pairs = dict(re.findall(r'\["([^"]+)"\]="([^"]+)"', match.group(1)))
        expected_pairs = {old_id: new_id for _env, old_id, new_id, _tok in self.MIGRATION_PAIRS}
        self.assertEqual(pairs, expected_pairs)
        for old_id, new_id in pairs.items():
            self.assertEqual(guard.PENDING_DEPLOYMENT_ID_MIGRATIONS.get(("dev", old_id)), new_id)


class GrammarTests(unittest.TestCase):
    """Tightened grammar checks: deployment IDs use the exact automation/goldengate-deployment-model.py _TOKEN_RE contract (no trailing/double hyphen), creation tokens must look like the deterministic <environment>-<deployment_id>-efs shape and respect the real AWS length limit."""

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


class CurrentEnvironmentLifecycleStateTests(unittest.TestCase):
    """Item 4: an existing current-environment managed EFS in an unsafe AWS lifecycle state fails closed before Terraform; a brand-new expected descriptor with no AWS EFS yet is never affected."""

    def test_deleting_state_fails(self):
        expected = [_expected("gg-a", "dev-gg-a-efs")]
        actual = [_fs("fs-a", "dev-gg-a-efs", _valid_tags("gg-a"), lifecycle_state="deleting")]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory(expected, actual, "dev")

    def test_deleted_state_fails(self):
        expected = [_expected("gg-a", "dev-gg-a-efs")]
        actual = [_fs("fs-a", "dev-gg-a-efs", _valid_tags("gg-a"), lifecycle_state="deleted")]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory(expected, actual, "dev")

    def test_error_state_fails(self):
        expected = [_expected("gg-a", "dev-gg-a-efs")]
        actual = [_fs("fs-a", "dev-gg-a-efs", _valid_tags("gg-a"), lifecycle_state="error")]
        with self.assertRaises(guard.InventoryGuardError):
            guard.check_managed_efs_inventory(expected, actual, "dev")

    def test_creating_state_is_not_failed(self):
        expected = [_expected("gg-a", "dev-gg-a-efs")]
        actual = [_fs("fs-a", "dev-gg-a-efs", _valid_tags("gg-a"), lifecycle_state="creating")]
        orphans = guard.check_managed_efs_inventory(expected, actual, "dev")
        self.assertEqual(orphans, [])

    def test_updating_state_is_not_failed(self):
        expected = [_expected("gg-a", "dev-gg-a-efs")]
        actual = [_fs("fs-a", "dev-gg-a-efs", _valid_tags("gg-a"), lifecycle_state="updating")]
        orphans = guard.check_managed_efs_inventory(expected, actual, "dev")
        self.assertEqual(orphans, [])

    def test_available_state_is_unaffected(self):
        expected = [_expected("gg-a", "dev-gg-a-efs")]
        actual = [_fs("fs-a", "dev-gg-a-efs", _valid_tags("gg-a"), lifecycle_state="available")]
        orphans = guard.check_managed_efs_inventory(expected, actual, "dev")
        self.assertEqual(orphans, [])

    def test_brand_new_expected_descriptor_with_no_aws_efs_yet_is_never_lifecycle_failed(self):
        # No actual filesystem exists at all for this expected descriptor -- the lifecycle check never runs for it, so Terraform remains free to create it.
        expected = [_expected("gg-brand-new", "dev-gg-brand-new-efs")]
        orphans = guard.check_managed_efs_inventory(expected, [], "dev")
        self.assertEqual(orphans, [])

    def test_other_environment_unsafe_lifecycle_state_is_not_failed(self):
        # Lifecycle hardening is scoped to the CURRENT environment only -- another environment's filesystem lifecycle is not this run's concern.
        actual = [_fs("fs-sit", "sit-gg-a-efs", _valid_tags("gg-a", environment="sit"), lifecycle_state="deleting")]
        orphans = guard.check_managed_efs_inventory([], actual, "dev")
        self.assertEqual(orphans, [])


class RetainedManagedEfsInventoryTests(unittest.TestCase):
    def test_retained_managed_efs_in_expected_prevents_orphan_failure(self):
        # Retained managed EFS stays in the expected inventory even when runtime compute is disabled with deployment.enabled=false; this inventory fixture proves that expected storage is not misclassified as an orphan.
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
