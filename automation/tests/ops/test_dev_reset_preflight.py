"""Offline tests for the DEV reset preflight (just.yaml): runs it under bash against scripted aws/kubectl stubs; run directly via `python3 automation/tests/ops/test_dev_reset_preflight.py`."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PREFLIGHT = REPO_ROOT / "just.yaml"
RUNTIMES = ["gg-postgresql-repltest-001", "gg-mssql-repltest-001", "gg-oracle-repltest-002", "gg-postgresql-repltest-002"]
SECRET_MARKER = "AKIASECRETMARKER0000"

# Each rule: [substring that must appear in the joined argv, exit code, stdout, stderr]; first match wins.
STUB = r'''#!/usr/bin/env python3
import json, os, sys
argv = [os.path.basename(sys.argv[0])] + sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(json.dumps(argv) + "\n")
joined = " ".join(argv)
for needle, rc, out, err in json.load(open(os.environ["STUB_RULES"])):
    if needle in joined:
        sys.stdout.write(out)
        sys.stderr.write(err)
        sys.exit(rc)
sys.exit(0)
'''

DENIED = "An error occurred (AccessDeniedException) when calling the operation: User arn:aws:sts::123:assumed-role/x is not authorized " + SECRET_MARKER
FORBIDDEN = 'Error from server (Forbidden): persistentvolumes is forbidden: User "x" cannot list resource ' + SECRET_MARKER
OLD_FS_ROWS = "".join(f"fs-{n:017x}\tdev-{rt}-old-efs\tavailable\t3\t{rt}\tdev\n" for n, rt in enumerate(RUNTIMES))


@unittest.skipUnless(shutil.which("bash"), "bash is required")
class PreflightClassificationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for tool in ("aws", "kubectl"):
            path = self.bin / tool
            path.write_text(STUB)
            path.chmod(0o755)
        self.log = self.root / "calls.jsonl"

    def _run(self, rules, extra_env=None):
        rules_path = self.root / "rules.json"
        rules_path.write_text(json.dumps(rules + [["sts get-caller-identity", 0, "123456789012\n", ""], ["get --raw /version", 0, "{}", ""]]))
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "STUB_RULES": str(rules_path), "STUB_LOG": str(self.log), **(extra_env or {})}
        proc = subprocess.run(["bash", str(PREFLIGHT)], env=env, capture_output=True, text=True, timeout=120)
        self.assertNotIn(SECRET_MARKER, proc.stdout + proc.stderr, "error bodies must never be printed")
        proc.calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return proc

    def test_confirmed_empty_environment_passes(self):
        proc = self._run([])
        self.assertEqual(proc.returncode, 0, proc.stdout)
        for check in ("CONFIRMED_ZERO efs-filesystems", "CONFIRMED_ZERO pvs", "CONFIRMED_ZERO pods", f"CONFIRMED_ZERO dynamodb:{RUNTIMES[0]}"):
            self.assertIn(check, proc.stdout)
        self.assertIn("unknown checks: 0", proc.stdout)
        self.assertIn("RESULT: NO BLOCKERS", proc.stdout)

    def test_existing_filesystems_are_confirmed_and_not_found_policies_mean_none(self):
        proc = self._run([
            ["describe-file-systems", 0, OLD_FS_ROWS, ""],
            ["describe-access-points", 0, "fsap-1\t/gg/x\tavailable\n", ""],
            ["describe-mount-targets", 0, "fsmt-1\teu-west-1a\tavailable\n", ""],
            ["describe-replication-configurations", 254, "", "An error occurred (ReplicationNotFound) when calling the operation"],
            ["describe-file-system-policy", 254, "", "An error occurred (PolicyNotFound) when calling the operation"],
            ["describe-backup-policy", 254, "", "An error occurred (PolicyNotFound) when calling the operation"],
        ])
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("CONFIRMED_PRESENT efs-filesystems", proc.stdout)
        self.assertIn("pre-reset tokens=4 canonical tokens=0", proc.stdout)
        self.assertIn("CONFIRMED_ZERO efs-file-system-policy:fs-00000000000000000 (PolicyNotFound)", proc.stdout)

    def test_efs_access_denied_is_unknown_never_zero(self):
        proc = self._run([["describe-file-systems", 254, "", DENIED]])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("BLOCKER: UNKNOWN efs-filesystems", proc.stdout)
        self.assertIn("PERMISSION_DENIED (AccessDeniedException)", proc.stdout)
        self.assertNotIn("CONFIRMED_ZERO efs-filesystems", proc.stdout)
        self.assertNotIn("pre-reset tokens=0", proc.stdout)

    def test_efs_policy_and_access_point_failures_are_unknown(self):
        proc = self._run([
            ["describe-file-systems", 0, OLD_FS_ROWS, ""],
            ["describe-file-system-policy", 254, "", DENIED],
            ["describe-access-points", 255, "", "Unable to parse response (not well-formed)"],
        ])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("UNKNOWN efs-file-system-policy:", proc.stdout)
        self.assertIn("UNKNOWN efs-access-points:", proc.stdout)
        self.assertNotIn("CONFIRMED_ZERO efs-file-system-policy:fs-00000000000000000\n", proc.stdout)

    def test_dynamodb_failure_is_unknown_never_existing_items(self):
        proc = self._run([["dynamodb query", 254, "", DENIED]])
        self.assertEqual(proc.returncode, 2)
        self.assertIn(f"UNKNOWN dynamodb:{RUNTIMES[0]}", proc.stdout)
        self.assertNotIn("items remain", proc.stdout)
        self.assertNotIn("process STATE# rows exist", proc.stdout)

    def test_dynamodb_rows_are_reported_when_confirmed(self):
        proc = self._run([["dynamodb query", 0, "CONFIG\tNone\nSTATE#EXT1\t1780000000\n", ""]], {"PRE_RESET_IDS": "gg-postgresql-repltest-01"})
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn(f"WARNING: process STATE# rows exist under {RUNTIMES[0]}", proc.stdout)
        self.assertIn("items remain under pre-reset id gg-postgresql-repltest-01", proc.stdout)

    def test_kubernetes_permission_failures_never_imply_absence(self):
        proc = self._run([["get pods", 1, "", FORBIDDEN], ["get pv ", 1, "", FORBIDDEN]])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("UNKNOWN pods", proc.stdout)
        self.assertIn(f"UNKNOWN pod {RUNTIMES[0]}-0", proc.stdout)
        self.assertNotIn(f"CONFIRMED_ZERO pod {RUNTIMES[0]}-0", proc.stdout)
        self.assertIn("BLOCKER: UNKNOWN pvs", proc.stdout)
        self.assertNotIn("without Retain", proc.stdout)

    def test_non_retain_and_foreign_pv_references_are_flagged_only_from_confirmed_data(self):
        pvs = "pv-a\tgoldengate-dev\tclaim-a\tBound\tDelete\tfs-00000000000000000::fsap-1\npv-b\tother\tclaim-b\tBound\tRetain\tfs-00000000000000000::fsap-9\n"
        proc = self._run([["get pv ", 0, pvs, ""], ["describe-file-systems", 0, OLD_FS_ROWS, ""]])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("WARNING: GoldenGate PV(s) without Retain: pv-a", proc.stdout)
        self.assertIn("BLOCKER: fs-00000000000000000 is referenced by PV(s) outside goldengate-dev: pv-b", proc.stdout)

    def test_no_pre_reset_identities_are_queried_by_default(self):
        proc = self._run([])
        self.assertEqual(proc.returncode, 0, proc.stdout)
        queried = [c for c in proc.calls if c[0] == "aws" and "query" in c]
        self.assertEqual(len(queried), len(RUNTIMES))
        self.assertNotIn("pre-reset id", proc.stdout)

    def test_aws_identity_failure_is_a_blocker(self):
        proc = self._run([["sts get-caller-identity", 255, "", "Unable to locate credentials"]])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("UNKNOWN aws-identity", proc.stdout)
        self.assertIn("AUTHENTICATION", proc.stdout)

    def test_only_read_only_operations_are_invoked(self):
        pods = "".join(f"{rt}-0 Running true 0\n" for rt in RUNTIMES)
        proc = self._run([["get pods", 0, pods, ""], ["describe-file-systems", 0, OLD_FS_ROWS, ""]])
        allowed_aws = {"get-caller-identity", "describe-file-systems", "describe-access-points", "describe-mount-targets",
                       "describe-replication-configurations", "describe-file-system-policy", "describe-backup-policy", "query"}
        for call in proc.calls:
            if call[0] == "aws":
                self.assertIn(call[2], allowed_aws, call)
            else:
                verb = next(a for a in call[1:] if not a.startswith("-") and a not in ("goldengate-dev", "argocd"))
                self.assertIn(verb, ("get", "logs", "exec"), call)
        self.assertTrue(any(c[0] == "kubectl" and "exec" in c for c in proc.calls))


if __name__ == "__main__":
    unittest.main()
