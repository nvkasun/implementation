"""Offline unit tests for automation/phases/phase1/efs_access_point_cleanup.py; run directly via `python3 automation/phases/phase1/tests/test_efs_access_point_cleanup.py`. Fixtures use the four DEV runtimes and their filesystem/access point mappings; the module itself names none of them."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location("efs_access_point_cleanup", REPO_ROOT / "automation" / "phases" / "phase1" / "efs_access_point_cleanup.py")
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)

ACCOUNT = "123456789012"
# runtime -> (filesystem, CSI access point, pre-reset PVC name)
DEV = {
    "gg-postgresql-repltest-001": ("fs-0d1c409669a15bc3e", "fsap-0bd59bb3c3abcd78d", "gg-postgresql-repltest-01-u02"),
    "gg-mssql-repltest-001": ("fs-03eb68ea5f6317e54", "fsap-0a804c4697816195b", "gg-mssql-repltest-01-u02"),
    "gg-oracle-repltest-002": ("fs-0abbe15be0c6fb64d", "fsap-0df58f1ff6912f3f1", "gg-oracle-repltest-01-u02"),
    "gg-postgresql-repltest-002": ("fs-08505eae446b8c918", "fsap-0db2b806b3bdb6563", "gg-postgresql-repltest-02-u02"),
}
FS = [v[0] for v in DEV.values()]
APS = [v[1] for v in DEV.values()]


def access_point(fs, ap, **overrides):
    item = {"AccessPointId": ap, "FileSystemId": fs, "OwnerId": ACCOUNT, "LifeCycleState": "available",
            "RootDirectory": {"Path": f"/old-base/{ap}"}, "Tags": [{"Key": "efs.csi.aws.com/cluster", "Value": "true"}]}
    item.update(overrides)
    return item


def all_access_points():
    return {fs: [access_point(fs, ap)] for fs, ap, _pvc in DEV.values()}


def filesystem(fs, runtime):
    return {"FileSystemId": fs, "CreationToken": f"dev-{runtime}-stale-efs", "LifeCycleState": "available",
            "Tags": [{"Key": "ManagedBy", "Value": "goldengate-eks-app"}, {"Key": "GoldenGateDeploymentId", "Value": runtime},
                     {"Key": "GoldenGateEnvironment", "Value": "dev"}, {"Key": "GoldenGateStorage", "Value": "u02"}]}


def empty_cluster():
    return {k: [] for k in ("pv", "pvc", "pods", "statefulsets", "storageclasses", "applications", "applicationsets")}


class ParseTests(unittest.TestCase):
    def test_valid_and_invalid_ids(self):
        self.assertEqual(cleanup.parse_access_point_ids(""), [])
        self.assertEqual(cleanup.parse_access_point_ids(", ".join(reversed(APS))), sorted(APS))
        for raw in ("fsap-xyz", FS[0], "fsap-0bd59bb3c3abcd78d;id", f"{APS[0]},{APS[0]}",
                    ",".join(f"fsap-{n:017x}" for n in range(cleanup.MAX_AUTHORIZED_ACCESS_POINTS + 1))):
            with self.subTest(raw=raw[:40]), self.assertRaises(cleanup.CleanupError):
                cleanup.parse_access_point_ids(raw)


class RuntimeScopeTests(unittest.TestCase):
    def test_runtime_is_taken_from_the_filesystem_tag(self):
        inventory = [filesystem(fs, rt) for rt, (fs, _ap, _pvc) in DEV.items()]
        self.assertEqual(cleanup.runtime_ids_by_filesystem(FS, inventory), {fs: rt for rt, (fs, _a, _p) in DEV.items()})

    def test_missing_untagged_or_duplicate_runtime_is_rejected(self):
        inventory = [filesystem(FS[0], "gg-postgresql-repltest-001"), filesystem(FS[1], "gg-postgresql-repltest-001"), {"FileSystemId": FS[2], "Tags": []}]
        for ids in ([FS[3]], [FS[2]], FS[:2]):
            with self.subTest(ids=ids), self.assertRaises(cleanup.CleanupError):
                cleanup.runtime_ids_by_filesystem(ids, inventory)


class PlanTests(unittest.TestCase):
    def test_all_four_authorized_csi_access_points_are_deleted(self):
        self.assertEqual(cleanup.plan_access_point_deletions(all_access_points(), APS, ACCOUNT), (sorted(APS), []))

    def test_retry_after_partial_deletion(self):
        aps = all_access_points()
        aps[FS[0]] = []
        aps[FS[1]] = [access_point(FS[1], APS[1], LifeCycleState="deleting")]
        self.assertEqual(cleanup.plan_access_point_deletions(aps, APS, ACCOUNT), (sorted(APS[2:]), [APS[1]]))

    def test_nothing_left_is_a_no_op(self):
        self.assertEqual(cleanup.plan_access_point_deletions({fs: [] for fs in FS}, APS, ACCOUNT), ([], []))

    def test_unsafe_access_points_are_refused(self):
        cases = {
            "unauthorized": access_point(FS[0], "fsap-0000000000000000a"),
            "wrong filesystem": access_point(FS[1], APS[0]),
            "foreign account": access_point(FS[0], APS[0], OwnerId="999999999999"),
            "not CSI": access_point(FS[0], APS[0], Tags=[{"Key": "Name", "Value": "manual"}]),
            "filesystem root": access_point(FS[0], APS[0], RootDirectory={"Path": "/"}),
            "creating": access_point(FS[0], APS[0], LifeCycleState="creating"),
            "error": access_point(FS[0], APS[0], LifeCycleState="error"),
            "malformed id": access_point(FS[0], "ap-1"),
        }
        for label, ap in cases.items():
            with self.subTest(label=label), self.assertRaises(cleanup.CleanupError):
                cleanup.plan_access_point_deletions({FS[0]: [ap]}, APS, ACCOUNT)


class KubernetesReferenceTests(unittest.TestCase):
    RUNTIMES = list(DEV)

    def _check(self, **objects):
        cluster = empty_cluster()
        cluster.update(objects)
        return cleanup.find_kubernetes_references(cluster, FS, APS, self.RUNTIMES, "dev", "goldengate-dev")

    def test_detached_cluster_passes(self):
        unrelated = {"metadata": {"name": "other"}, "spec": {"csi": {"driver": "efs.csi.aws.com", "volumeHandle": "fs-0123456789abcdef0::fsap-0123456789abcdef0"}}}
        self.assertEqual(self._check(pv=[unrelated]), [])

    def test_every_remaining_reference_is_reported(self):
        rt = "gg-oracle-repltest-002"
        fs, ap, old_pvc = DEV[rt]
        pv = {"metadata": {"name": "pvc-5cccdddb"}, "spec": {"csi": {"driver": "efs.csi.aws.com", "volumeHandle": f"{fs}::{ap}"}}}
        cases = {
            "pv": {"pv": [pv]},
            "pv by access point": {"pv": [{"metadata": {"name": "x"}, "spec": {"csi": {"volumeHandle": f"fs-0123456789abcdef0::{ap}"}}}]},
            "pvc bound to that pv": {"pvc": [{"metadata": {"namespace": "goldengate-dev", "name": old_pvc}, "spec": {"volumeName": "pvc-5cccdddb"}}], "pv": [pv]},
            "canonical pvc": {"pvc": [{"metadata": {"namespace": "goldengate-dev", "name": f"{rt}-u02"}, "spec": {}}]},
            "storageclass by name": {"storageclasses": [{"metadata": {"name": f"gg-efs-dev-{rt}"}}]},
            "storageclass by filesystem": {"storageclasses": [{"metadata": {"name": "custom"}, "provisioner": "efs.csi.aws.com", "parameters": {"fileSystemId": fs}}]},
            "runtime pod": {"pods": [{"metadata": {"namespace": "goldengate-dev", "name": f"{rt}-0"}, "spec": {}}]},
            "pod mounting the claim": {"pods": [{"metadata": {"namespace": "tools", "name": "debug"}, "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "c"}}]}}],
                                       "pvc": [{"metadata": {"namespace": "tools", "name": "c"}, "spec": {"volumeName": "pvc-5cccdddb"}}], "pv": [pv]},
            "statefulset": {"statefulsets": [{"metadata": {"namespace": "goldengate-dev", "name": rt}}]},
            "application": {"applications": [{"metadata": {"name": "goldengate-dev-oracle-repltest-002"}}]},
            "application by release": {"applications": [{"metadata": {"name": "renamed"}, "spec": {"source": {"helm": {"releaseName": rt}}}}]},
            "applicationset": {"applicationsets": [{"metadata": {"name": "goldengate-dev-oracle-repltest-002-appset"}}]},
        }
        for label, objects in cases.items():
            with self.subTest(label=label):
                self.assertTrue(self._check(**objects), label)


class NoHardcodingTests(unittest.TestCase):
    def test_module_names_no_runtime_filesystem_or_access_point(self):
        text = (REPO_ROOT / "automation" / "phases" / "phase1" / "efs_access_point_cleanup.py").read_text()
        for rt, (fs, ap, _pvc) in DEV.items():
            for value in (rt, fs, ap):
                self.assertNotIn(value, text)


if __name__ == "__main__":
    unittest.main()
