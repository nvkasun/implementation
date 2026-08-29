"""Offline tests for automation/phases/phase7/phase7_monitor.py; run directly via `python3 automation/phases/phase7/tests/test_phase7_monitor.py`. No live AWS/Kubernetes/GoldenGate REST access -- every subprocess call (aws, kubectl, python3 <classifier>.py, helm) is intercepted via a scripted fake that asserts on the exact argv and returns a fabricated result. This suite deliberately does NOT re-test automation/orchestration/monitor_state.py's, monitor_acceptance.py's, or end_to_end_acceptance.py's own classification logic (already covered by automation/test-goldengate-monitor-state.py / test-goldengate-monitor-acceptance.py / test-goldengate-end-to-end-acceptance.py) -- it tests ONLY phase7_monitor.py's own orchestration: subprocess wiring, fail-closed handling of a classifier's non-zero exit or malformed output, credential-free pipeline discovery, the ownership-chain-verified pod name flowing unchanged into every consumer, bounded end-to-end retry, and health-check-output safety."""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[4]
TOOL_PATH = REPO_ROOT / "automation" / "phases" / "phase7" / "phase7_monitor.py"


def _load_tool(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


phase7_monitor = _load_tool(TOOL_PATH, "phase7_monitor")

ENVIRONMENT = "dev"
AWS_REGION_VALUE = "eu-west-1"
EKS_CLUSTER_NAME_VALUE = "gg-dev-cluster"
EKS_DEPLOY_ROLE_ARN_VALUE = "arn:aws:iam::668311715351:role/GoldenGateEksDeployRole-dev"
MONITOR_NAMESPACE_VALUE = "goldengate-monitoring"
MONITOR_ROLE_ARN_VALUE = "arn:aws:iam::668311715351:role/GoldenGateMonitorRole-dev"

BASE_ENV = {
    "AWS_REGION": AWS_REGION_VALUE,
    "EKS_CLUSTER_NAME": EKS_CLUSTER_NAME_VALUE,
    "EKS_DEPLOY_ROLE_ARN": EKS_DEPLOY_ROLE_ARN_VALUE,
    "MONITOR_NAMESPACE": MONITOR_NAMESPACE_VALUE,
    "MONITOR_ROLE_ARN": MONITOR_ROLE_ARN_VALUE,
}


def _env_patch(extra=None):
    merged = dict(BASE_ENV)
    if extra:
        merged.update(extra)
    return mock.patch.dict(os.environ, merged, clear=False)


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class ScriptedRun:
    """Replaces phase7_monitor.run with a scripted responder: a list of (predicate, FakeProc-or-callable) pairs consulted in order (later registrations take precedence), falling back to a default success. Every call is recorded for assertion."""

    def __init__(self, default=None):
        self.rules = []
        self.calls = []
        self.default = default if default is not None else FakeProc(0, "", "")

    def when(self, predicate, proc_or_fn):
        self.rules.append((predicate, proc_or_fn))
        return self

    def _resolve(self, proc_or_fn, argv):
        return proc_or_fn(argv) if callable(proc_or_fn) and not isinstance(proc_or_fn, FakeProc) else proc_or_fn

    def __call__(self, argv, env=None, cwd=None, check=True, capture_output=True, input_text=None, timeout_seconds=None):
        self.calls.append({"argv": list(argv), "input_text": input_text, "timeout_seconds": timeout_seconds})
        for predicate, proc_or_fn in reversed(self.rules):
            if predicate(argv):
                proc = self._resolve(proc_or_fn, argv)
                if check and proc.returncode != 0:
                    raise phase7_monitor.Phase7MonitorError(f"{' '.join(str(a) for a in argv)} failed: {proc.stdout}\n{proc.stderr}")
                return proc
        if check and self.default.returncode != 0:
            raise phase7_monitor.Phase7MonitorError(f"{' '.join(str(a) for a in argv)} failed: {self.default.stdout}\n{self.default.stderr}")
        return self.default


def _starts_with(*prefix):
    return lambda argv: list(argv[:len(prefix)]) == list(prefix)


def _is_tool_call(tool_path):
    return lambda argv: len(argv) >= 2 and argv[1] == str(tool_path)


def _is_monitor_state_call(argv):
    return _is_tool_call(phase7_monitor.MONITOR_STATE_TOOL)(argv)


def _is_monitor_acceptance_call(argv):
    return _is_tool_call(phase7_monitor.MONITOR_ACCEPTANCE_TOOL)(argv)


def _is_end_to_end_call(argv):
    return _is_tool_call(phase7_monitor.END_TO_END_ACCEPTANCE_TOOL)(argv)


def _is_registry_call(argv):
    return _is_tool_call(phase7_monitor.DEPLOYMENT_MODEL_TOOL)(argv) and "registry" in argv


def _is_pipelines_call(argv):
    return _is_tool_call(phase7_monitor.DEPLOYMENT_MODEL_TOOL)(argv) and argv[-1] == "replication-pipelines"


def _is_replication_plan_call(argv):
    return _is_tool_call(phase7_monitor.DEPLOYMENT_MODEL_TOOL)(argv) and "replication-plan" in argv


def _is_kubectl_exec_call(argv):
    return list(argv[:2]) == ["kubectl", "exec"]


def _exec_program(argv):
    """Extracts the inline python3 -c program string from a `kubectl exec ... -- python3 -c <program>` argv."""
    return argv[-1]


def _base_scripted():
    scripted = ScriptedRun()
    scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
    return scripted


def _capture_stdout(fn, *args, **kwargs):
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


def _github_output_pairs(path):
    with open(path) as f:
        lines = [line.rstrip("\n") for line in f if line.strip()]
    return dict(line.split("=", 1) for line in lines)


class EnsureKubectlTests(unittest.TestCase):
    def test_kubectl_already_present_skips_download(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("bash", "-c", "command -v kubectl"), FakeProc(0, "/usr/local/bin/kubectl"))
        scripted.when(_starts_with("kubectl", "version"), FakeProc(0, ""))
        with mock.patch.object(phase7_monitor, "run", scripted):
            phase7_monitor.cmd_ensure_kubectl(argparse_namespace())
        self.assertFalse(any(list(c["argv"][:1]) == ["curl"] for c in scripted.calls))


def argparse_namespace(**kwargs):
    ns = argparse.Namespace()
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


class OwnershipPreflightTests(unittest.TestCase):
    """1-4: ABSENT/OWNED pass and emit the exact state; BROKEN fails; a classifier inspection/configuration error (non-zero exit, never a parsed ABSENT) fails closed too."""

    def _run(self, scripted, github_output_path):
        args = argparse_namespace(environment=ENVIRONMENT)
        with _env_patch({"GITHUB_OUTPUT": github_output_path}), mock.patch.object(phase7_monitor, "run", scripted):
            return phase7_monitor.cmd_ownership_preflight(args)

    def test_absent_passes_and_emits_absent(self):
        scripted = _base_scripted()
        scripted.when(_is_monitor_state_call, FakeProc(0, json.dumps({"state": "ABSENT", "reasons": [], "checks": {}})))
        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "gh_output")
            open(output_path, "w").close()
            self._run(scripted, output_path)
            self.assertEqual(_github_output_pairs(output_path), {"state": "ABSENT"})

    def test_owned_passes_and_emits_owned(self):
        scripted = _base_scripted()
        scripted.when(_is_monitor_state_call, FakeProc(0, json.dumps({"state": "OWNED", "reasons": [], "checks": {}})))
        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "gh_output")
            open(output_path, "w").close()
            self._run(scripted, output_path)
            self.assertEqual(_github_output_pairs(output_path), {"state": "OWNED"})

    def test_broken_fails(self):
        scripted = _base_scripted()
        scripted.when(_is_monitor_state_call, FakeProc(0, json.dumps({"state": "BROKEN", "reasons": ["conflict"], "checks": {}})))
        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "gh_output")
            open(output_path, "w").close()
            with self.assertRaises(phase7_monitor.Phase7MonitorError):
                self._run(scripted, output_path)
            # BROKEN is still published (diagnostic visibility) before the hard failure -- matching the prior bash's exact ordering.
            self.assertEqual(_github_output_pairs(output_path), {"state": "BROKEN"})

    def test_kubernetes_inspection_error_fails_and_never_becomes_absent(self):
        scripted = _base_scripted()
        scripted.when(_is_monitor_state_call, FakeProc(1, "", "INSPECTION ERROR: kubectl get application failed: Forbidden"))
        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "gh_output")
            open(output_path, "w").close()
            with self.assertRaises(phase7_monitor.Phase7MonitorError):
                self._run(scripted, output_path)
            # No state was ever written -- an inspection error must never be represented as any state, least of all ABSENT.
            self.assertEqual(_github_output_pairs(output_path), {})

    def test_connects_to_eks_with_canonical_values_before_classifying(self):
        scripted = _base_scripted()
        scripted.when(_is_monitor_state_call, FakeProc(0, json.dumps({"state": "ABSENT", "reasons": [], "checks": {}})))
        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "gh_output")
            open(output_path, "w").close()
            self._run(scripted, output_path)
        eks_calls = [c for c in scripted.calls if list(c["argv"][:3]) == ["aws", "eks", "update-kubeconfig"]]
        self.assertEqual(len(eks_calls), 1)
        argv = eks_calls[0]["argv"]
        self.assertIn(AWS_REGION_VALUE, argv)
        self.assertIn(EKS_CLUSTER_NAME_VALUE, argv)
        self.assertIn(EKS_DEPLOY_ROLE_ARN_VALUE, argv)


class ValidateLocalTests(unittest.TestCase):
    """5: unit tests invoked, canonical registry generated, helm lint invoked, helm template invoked, no AWS/kubectl/ECR/mutation."""

    def test_full_local_dry_run_sequence_and_no_cloud_calls(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("bash", "-c", "command -v helm"), FakeProc(0, "/usr/local/bin/helm"))
        scripted.when(_starts_with(sys.executable, "-m", "unittest"), FakeProc(0, "OK"))
        scripted.when(_is_registry_call, lambda argv: _write_registry_fixture(argv))
        scripted.when(_starts_with("helm", "lint"), FakeProc(0, ""))
        scripted.when(_starts_with("helm", "template"), FakeProc(0, "kind: Deployment\n"))

        args = argparse_namespace(environment=ENVIRONMENT)
        with _env_patch(), mock.patch.object(phase7_monitor, "run", scripted):
            phase7_monitor.cmd_validate_local(args)

        self.assertTrue(any(_starts_with(sys.executable, "-m", "unittest")(c["argv"]) for c in scripted.calls))
        self.assertTrue(any(_is_registry_call(c["argv"]) for c in scripted.calls))
        self.assertTrue(any(_starts_with("helm", "lint")(c["argv"]) for c in scripted.calls))
        self.assertTrue(any(_starts_with("helm", "template")(c["argv"]) for c in scripted.calls))
        self.assertFalse(any(_starts_with("aws")(c["argv"]) for c in scripted.calls))
        self.assertFalse(any(_starts_with("kubectl")(c["argv"]) for c in scripted.calls))

    def test_missing_helm_fails_closed(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("bash", "-c", "command -v helm"), FakeProc(1, "", ""))
        args = argparse_namespace(environment=ENVIRONMENT)
        with _env_patch(), mock.patch.object(phase7_monitor, "run", scripted):
            with self.assertRaises(phase7_monitor.Phase7MonitorError):
                phase7_monitor.cmd_validate_local(args)


def _write_registry_fixture(argv):
    output_path = argv[argv.index("--output") + 1]
    with open(output_path, "w") as f:
        f.write("deployments: []\n")
    return FakeProc(0, "")


class StrictAcceptanceTests(unittest.TestCase):
    """6-13: structural BROKEN -> no health exec; structural HEALTHY but no Ready pod -> fail; the SAME verified pod flows into /healthz and /readyz; kubectl exec health failure fails closed; malformed health status fails; /healthz!=200 or /readyz!=200 -> final BROKEN; final HEALTHY emits state+ready_pod_name."""

    def _common_kwargs(self):
        return dict(environment=ENVIRONMENT, expected_image_repository="repo", expected_image_tag="tag",
                    expected_chart_version="1.2.3", expected_cloudwatch_publish_enabled=True)

    def _run(self, scripted, github_output_path):
        args = argparse_namespace(**self._common_kwargs())
        with _env_patch({"GITHUB_OUTPUT": github_output_path}), mock.patch.object(phase7_monitor, "run", scripted):
            return phase7_monitor.cmd_strict_acceptance(args)

    def _healthy_http_shim(self, healthz=200, readyz=200):
        def _fn(argv):
            program = _exec_program(argv)
            if "/healthz" in program:
                return FakeProc(0, f"{healthz}\n")
            if "/readyz" in program:
                return FakeProc(0, f"{readyz}\n")
            raise AssertionError(f"unexpected kubectl exec program: {program}")
        return _fn

    def test_structural_broken_never_execs_health(self):
        scripted = _base_scripted()
        scripted.when(_is_registry_call, _write_registry_fixture)
        scripted.when(_is_monitor_acceptance_call, FakeProc(0, json.dumps({"state": "BROKEN", "reasons": ["x"], "checks": {"ready_pod_name": None}})))
        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "gh_output")
            open(output_path, "w").close()
            with self.assertRaises(phase7_monitor.Phase7MonitorError):
                self._run(scripted, output_path)
        self.assertFalse(any(_is_kubectl_exec_call(c["argv"]) for c in scripted.calls))

    def test_structural_healthy_but_no_ready_pod_fails(self):
        scripted = _base_scripted()
        scripted.when(_is_registry_call, _write_registry_fixture)
        scripted.when(_is_monitor_acceptance_call, FakeProc(0, json.dumps({"state": "HEALTHY", "reasons": [], "checks": {"ready_pod_name": None}})))
        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "gh_output")
            open(output_path, "w").close()
            with self.assertRaises(phase7_monitor.Phase7MonitorError):
                self._run(scripted, output_path)
        self.assertFalse(any(_is_kubectl_exec_call(c["argv"]) for c in scripted.calls))

    def test_same_verified_pod_used_for_healthz_and_readyz_and_final_pass(self):
        pod_name = "gg-monitor-abc123"
        scripted = _base_scripted()
        scripted.when(_is_registry_call, _write_registry_fixture)
        seen_final_pod_args = []

        def _monitor_acceptance_responder(argv):
            if "--healthz-status" in argv:
                seen_final_pod_args.append(argv)
                return FakeProc(0, json.dumps({"state": "HEALTHY", "reasons": [], "checks": {"ready_pod_name": pod_name}}))
            return FakeProc(0, json.dumps({"state": "HEALTHY", "reasons": [], "checks": {"ready_pod_name": pod_name}}))

        scripted.when(_is_monitor_acceptance_call, _monitor_acceptance_responder)
        scripted.when(_is_kubectl_exec_call, self._healthy_http_shim())

        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "gh_output")
            open(output_path, "w").close()
            self._run(scripted, output_path)
            self.assertEqual(_github_output_pairs(output_path), {"state": "HEALTHY", "ready_pod_name": pod_name})

        exec_calls = [c for c in scripted.calls if _is_kubectl_exec_call(c["argv"])]
        self.assertEqual(len(exec_calls), 2)
        for c in exec_calls:
            self.assertIn(pod_name, c["argv"])
        self.assertEqual(len(seen_final_pod_args), 1)

    def test_kubectl_exec_health_failure_fails_closed(self):
        scripted = _base_scripted()
        scripted.when(_is_registry_call, _write_registry_fixture)
        scripted.when(_is_monitor_acceptance_call, FakeProc(0, json.dumps({"state": "HEALTHY", "reasons": [], "checks": {"ready_pod_name": "gg-monitor-x"}})))
        scripted.when(_is_kubectl_exec_call, FakeProc(1, "", "Error from server (Forbidden): pods \"gg-monitor-x\" is forbidden"))
        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "gh_output")
            open(output_path, "w").close()
            with self.assertRaises(phase7_monitor.Phase7MonitorError):
                self._run(scripted, output_path)
            self.assertEqual(_github_output_pairs(output_path), {})

    # J: a kubectl exec WALL-CLOCK TIMEOUT on /healthz or /readyz (simulating Kubernetes API/auth/SPDY negotiation stalling before the in-pod urllib timeout is ever reached) must fail closed with a controlled Phase7MonitorError -- never an uncontrolled subprocess.TimeoutExpired traceback, never treated as HTTP 0 success.
    def test_kubectl_health_exec_timeout_fails_closed_without_traceback(self):
        scripted = _base_scripted()
        scripted.when(_is_registry_call, _write_registry_fixture)
        scripted.when(_is_monitor_acceptance_call, FakeProc(0, json.dumps({"state": "HEALTHY", "reasons": [], "checks": {"ready_pod_name": "gg-monitor-x"}})))
        scripted.when(_is_kubectl_exec_call, FakeProc(124, "", "TIMEOUT: command exceeded 18s and was terminated"))
        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "gh_output")
            open(output_path, "w").close()
            try:
                self._run(scripted, output_path)
                self.fail("expected Phase7MonitorError")
            except phase7_monitor.Phase7MonitorError as exc:
                self.assertIn("failed", str(exc))
            self.assertEqual(_github_output_pairs(output_path), {})

    def test_malformed_health_status_fails(self):
        scripted = _base_scripted()
        scripted.when(_is_registry_call, _write_registry_fixture)
        scripted.when(_is_monitor_acceptance_call, FakeProc(0, json.dumps({"state": "HEALTHY", "reasons": [], "checks": {"ready_pod_name": "gg-monitor-x"}})))
        scripted.when(_is_kubectl_exec_call, FakeProc(0, "not-a-number\n"))
        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "gh_output")
            open(output_path, "w").close()
            with self.assertRaises(phase7_monitor.Phase7MonitorError):
                self._run(scripted, output_path)

    def test_multiple_output_lines_fails(self):
        scripted = _base_scripted()
        scripted.when(_is_registry_call, _write_registry_fixture)
        scripted.when(_is_monitor_acceptance_call, FakeProc(0, json.dumps({"state": "HEALTHY", "reasons": [], "checks": {"ready_pod_name": "gg-monitor-x"}})))
        scripted.when(_is_kubectl_exec_call, FakeProc(0, "200\n200\n"))
        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "gh_output")
            open(output_path, "w").close()
            with self.assertRaises(phase7_monitor.Phase7MonitorError):
                self._run(scripted, output_path)

    def test_healthz_not_200_yields_final_broken(self):
        scripted = _base_scripted()
        scripted.when(_is_registry_call, _write_registry_fixture)
        scripted.when(_is_kubectl_exec_call, self._healthy_http_shim(healthz=503, readyz=200))

        def _monitor_acceptance_responder(argv):
            if "--healthz-status" in argv:
                self.assertIn("503", argv)
                return FakeProc(0, json.dumps({"state": "BROKEN", "reasons": ["healthz"], "checks": {"ready_pod_name": "gg-monitor-x"}}))
            return FakeProc(0, json.dumps({"state": "HEALTHY", "reasons": [], "checks": {"ready_pod_name": "gg-monitor-x"}}))

        scripted.when(_is_monitor_acceptance_call, _monitor_acceptance_responder)
        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "gh_output")
            open(output_path, "w").close()
            with self.assertRaises(phase7_monitor.Phase7MonitorError):
                self._run(scripted, output_path)
            self.assertEqual(_github_output_pairs(output_path), {})

    def test_readyz_not_200_yields_final_broken(self):
        scripted = _base_scripted()
        scripted.when(_is_registry_call, _write_registry_fixture)
        scripted.when(_is_kubectl_exec_call, self._healthy_http_shim(healthz=200, readyz=500))

        def _monitor_acceptance_responder(argv):
            if "--healthz-status" in argv:
                self.assertIn("500", argv)
                return FakeProc(0, json.dumps({"state": "BROKEN", "reasons": ["readyz"], "checks": {"ready_pod_name": "gg-monitor-x"}}))
            return FakeProc(0, json.dumps({"state": "HEALTHY", "reasons": [], "checks": {"ready_pod_name": "gg-monitor-x"}}))

        scripted.when(_is_monitor_acceptance_call, _monitor_acceptance_responder)
        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "gh_output")
            open(output_path, "w").close()
            with self.assertRaises(phase7_monitor.Phase7MonitorError):
                self._run(scripted, output_path)

    def test_final_healthy_emits_state_and_pod_name(self):
        pod_name = "gg-monitor-final-pod"
        scripted = _base_scripted()
        scripted.when(_is_registry_call, _write_registry_fixture)
        scripted.when(_is_monitor_acceptance_call, FakeProc(0, json.dumps({"state": "HEALTHY", "reasons": [], "checks": {"ready_pod_name": pod_name}})))
        scripted.when(_is_kubectl_exec_call, self._healthy_http_shim())
        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "gh_output")
            open(output_path, "w").close()
            self._run(scripted, output_path)
            self.assertEqual(_github_output_pairs(output_path), {"state": "HEALTHY", "ready_pod_name": pod_name})


class ReplicationMonitorAcceptanceTests(unittest.TestCase):
    """14-19: zero pipelines -> success, no AWS/EKS/kubectl; enabled pipeline exact Extract/Distribution/Replicat rows required; stale/ABENDED/startOnCreate-not-RUNNING all fail; malformed /api/processes fails."""

    PLAN = {
        "pipelineId": "payments-pg-to-mssql-001",
        "source": {"deploymentId": "gg-pg-src-01"},
        "target": {"deploymentId": "gg-mssql-tgt-01"},
        "extract": {"name": "PGSRC01", "startOnCreate": True},
        "distribution": {"pathName": "PG2MS01", "startOnCreate": True},
        "replicat": {"name": "MSTGT01", "startOnCreate": True},
    }

    def _healthy_api_processes(self):
        return {
            "deployments": [
                {
                    "deploymentName": "gg-pg-src-01",
                    "processDiscovery": {"status": "OK"},
                    "processes": [
                        {"process": "PGSRC01", "status": "RUNNING", "stale": False},
                        {"process": "PG2MS01", "status": "RUNNING", "stale": False},
                    ],
                },
                {
                    "deploymentName": "gg-mssql-tgt-01",
                    "processDiscovery": {"status": "OK"},
                    "processes": [
                        {"process": "MSTGT01", "status": "RUNNING", "stale": False},
                    ],
                },
            ]
        }

    def _scripted_with_pipelines(self, pipeline_ids, api_processes_doc):
        scripted = _base_scripted()
        scripted.when(_is_pipelines_call, FakeProc(0, "".join(f"{p}\n" for p in pipeline_ids)))
        scripted.when(_is_replication_plan_call, lambda argv: FakeProc(0, json.dumps(self.PLAN)))
        scripted.when(_is_kubectl_exec_call, FakeProc(0, json.dumps(api_processes_doc)))
        return scripted

    def test_zero_pipelines_is_a_clean_noop_with_no_aws_eks_kubectl(self):
        scripted = ScriptedRun()
        scripted.when(_is_pipelines_call, FakeProc(0, ""))
        args = argparse_namespace(environment=ENVIRONMENT, pod_name="")
        with _env_patch(), mock.patch.object(phase7_monitor, "run", scripted):
            phase7_monitor.cmd_replication_monitor_acceptance(args)
        self.assertFalse(any(_starts_with("aws")(c["argv"]) for c in scripted.calls))
        self.assertFalse(any(_starts_with("kubectl")(c["argv"]) for c in scripted.calls))

    def test_enabled_pipeline_exact_process_rows_required_and_pass(self):
        scripted = self._scripted_with_pipelines(["payments-pg-to-mssql-001"], self._healthy_api_processes())
        args = argparse_namespace(environment=ENVIRONMENT, pod_name="gg-monitor-verified")
        with _env_patch(), mock.patch.object(phase7_monitor, "run", scripted):
            phase7_monitor.cmd_replication_monitor_acceptance(args)
        exec_calls = [c for c in scripted.calls if _is_kubectl_exec_call(c["argv"])]
        self.assertEqual(len(exec_calls), 1)
        self.assertIn("gg-monitor-verified", exec_calls[0]["argv"])

    def test_stale_process_fails(self):
        doc = self._healthy_api_processes()
        doc["deployments"][0]["processes"][0]["stale"] = True
        scripted = self._scripted_with_pipelines(["payments-pg-to-mssql-001"], doc)
        args = argparse_namespace(environment=ENVIRONMENT, pod_name="gg-monitor-verified")
        with _env_patch(), mock.patch.object(phase7_monitor, "run", scripted):
            with self.assertRaises(phase7_monitor.Phase7MonitorError):
                phase7_monitor.cmd_replication_monitor_acceptance(args)

    def test_abended_fails(self):
        doc = self._healthy_api_processes()
        doc["deployments"][0]["processes"][0]["status"] = "ABENDED"
        scripted = self._scripted_with_pipelines(["payments-pg-to-mssql-001"], doc)
        args = argparse_namespace(environment=ENVIRONMENT, pod_name="gg-monitor-verified")
        with _env_patch(), mock.patch.object(phase7_monitor, "run", scripted):
            with self.assertRaises(phase7_monitor.Phase7MonitorError):
                phase7_monitor.cmd_replication_monitor_acceptance(args)

    def test_start_on_create_process_not_running_fails(self):
        doc = self._healthy_api_processes()
        doc["deployments"][0]["processes"][0]["status"] = "STOPPED"
        scripted = self._scripted_with_pipelines(["payments-pg-to-mssql-001"], doc)
        args = argparse_namespace(environment=ENVIRONMENT, pod_name="gg-monitor-verified")
        with _env_patch(), mock.patch.object(phase7_monitor, "run", scripted):
            with self.assertRaises(phase7_monitor.Phase7MonitorError):
                phase7_monitor.cmd_replication_monitor_acceptance(args)

    def test_malformed_api_processes_fails(self):
        scripted = _base_scripted()
        scripted.when(_is_pipelines_call, FakeProc(0, "payments-pg-to-mssql-001\n"))
        scripted.when(_is_replication_plan_call, lambda argv: FakeProc(0, json.dumps(self.PLAN)))
        scripted.when(_is_kubectl_exec_call, FakeProc(0, json.dumps({"deployments": [{"processDiscovery": {"status": "OK"}}]})))
        args = argparse_namespace(environment=ENVIRONMENT, pod_name="gg-monitor-verified")
        with _env_patch(), mock.patch.object(phase7_monitor, "run", scripted):
            with self.assertRaises(phase7_monitor.Phase7MonitorError):
                phase7_monitor.cmd_replication_monitor_acceptance(args)

    def test_empty_pod_name_with_enabled_pipelines_fails_closed(self):
        scripted = self._scripted_with_pipelines(["payments-pg-to-mssql-001"], self._healthy_api_processes())
        args = argparse_namespace(environment=ENVIRONMENT, pod_name="")
        with _env_patch(), mock.patch.object(phase7_monitor, "run", scripted):
            with self.assertRaises(phase7_monitor.Phase7MonitorError):
                phase7_monitor.cmd_replication_monitor_acceptance(args)

    def _assert_inventory_fails(self, doc):
        scripted = self._scripted_with_pipelines(["payments-pg-to-mssql-001"], doc)
        args = argparse_namespace(environment=ENVIRONMENT, pod_name="gg-monitor-verified")
        with _env_patch(), mock.patch.object(phase7_monitor, "run", scripted):
            with self.assertRaises(phase7_monitor.Phase7MonitorError):
                phase7_monitor.cmd_replication_monitor_acceptance(args)

    # A: a malformed process row must fail closed even when every expected valid Extract/Distribution/Replicat row is ALSO present -- this is the exact defect: the old `isinstance(p, dict)` filter silently dropped "MALFORMED_ROW" while still finding and accepting the three valid rows.
    def test_malformed_process_row_fails_even_with_all_valid_expected_rows_present(self):
        doc = self._healthy_api_processes()
        doc["deployments"][0]["processes"].insert(1, "MALFORMED_ROW")
        self._assert_inventory_fails(doc)

    # B: representative non-object process row variants (null/list/number/bool) must all fail closed -- never silently filtered out of the inventory.
    def test_process_row_non_object_variants_fail(self):
        for bad_row in (None, [], ["nested", "list"], 123, 1.5, True, False):
            with self.subTest(bad_row=bad_row):
                doc = self._healthy_api_processes()
                doc["deployments"][0]["processes"].append(bad_row)
                self._assert_inventory_fails(doc)

    # C: missing/empty/non-string process identity must fail closed.
    def test_missing_or_non_string_process_name_fails(self):
        for bad_row in ({}, {"process": ""}, {"process": 123}, {"process": None}, {"status": "RUNNING"}):
            with self.subTest(bad_row=bad_row):
                doc = self._healthy_api_processes()
                doc["deployments"][0]["processes"].append(bad_row)
                self._assert_inventory_fails(doc)

    # D: a non-list `processes` value must fail closed -- never silently coerced to an empty inventory via `x or []`.
    def test_non_list_processes_fails(self):
        for bad_processes in ({}, "bad", 123, True):
            with self.subTest(bad_processes=bad_processes):
                doc = self._healthy_api_processes()
                doc["deployments"][0]["processes"] = bad_processes
                self._assert_inventory_fails(doc)

    # E: duplicate process identities within one deployment make the inventory ambiguous -- must fail closed, never silently resolved by keeping rows[0].
    def test_duplicate_process_name_fails(self):
        doc = self._healthy_api_processes()
        doc["deployments"][0]["processes"].append({"process": "PGSRC01", "status": "STOPPED", "stale": False})
        self._assert_inventory_fails(doc)

    # F: a non-object processDiscovery value must fail with a controlled Phase7MonitorError, never an uncontrolled AttributeError/traceback from a bare `.get()` call.
    def test_malformed_process_discovery_fails_with_controlled_error(self):
        for bad_discovery in ("OK", ["OK"], 1, True, "OK"):
            with self.subTest(bad_discovery=bad_discovery):
                doc = self._healthy_api_processes()
                doc["deployments"][0]["processDiscovery"] = bad_discovery
                self._assert_inventory_fails(doc)

    # K: a kubectl exec wall-clock timeout while fetching /api/processes for replication-monitor acceptance is a HARD failure -- never tolerated, never retried.
    def test_api_processes_kubectl_exec_timeout_fails_hard(self):
        scripted = _base_scripted()
        scripted.when(_is_pipelines_call, FakeProc(0, "payments-pg-to-mssql-001\n"))
        scripted.when(_is_kubectl_exec_call, FakeProc(124, "", "TIMEOUT: command exceeded 20s and was terminated"))
        args = argparse_namespace(environment=ENVIRONMENT, pod_name="gg-monitor-verified")
        with _env_patch(), mock.patch.object(phase7_monitor, "run", scripted):
            with self.assertRaises(phase7_monitor.Phase7MonitorError):
                phase7_monitor.cmd_replication_monitor_acceptance(args)


class ListReplicationPipelinesTests(unittest.TestCase):
    def test_writes_has_pipelines_false_with_no_aws(self):
        scripted = ScriptedRun()
        scripted.when(_is_pipelines_call, FakeProc(0, ""))
        args = argparse_namespace(environment=ENVIRONMENT)
        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "gh_output")
            open(output_path, "w").close()
            with _env_patch({"GITHUB_OUTPUT": output_path}), mock.patch.object(phase7_monitor, "run", scripted):
                phase7_monitor.cmd_list_replication_pipelines(args)
            self.assertEqual(_github_output_pairs(output_path), {"has_pipelines": "false"})
        self.assertFalse(any(_starts_with("aws")(c["argv"]) for c in scripted.calls))

    def test_writes_has_pipelines_true(self):
        scripted = ScriptedRun()
        scripted.when(_is_pipelines_call, FakeProc(0, "payments-pg-to-mssql-001\n"))
        args = argparse_namespace(environment=ENVIRONMENT)
        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "gh_output")
            open(output_path, "w").close()
            with _env_patch({"GITHUB_OUTPUT": output_path}), mock.patch.object(phase7_monitor, "run", scripted):
                phase7_monitor.cmd_list_replication_pipelines(args)
            self.assertEqual(_github_output_pairs(output_path), {"has_pipelines": "true"})


class EndToEndAcceptanceTests(unittest.TestCase):
    """20-25: HEALTHY immediately; bounded retry on BROKEN; bounded retry on transient fetch failure; timeout fails; no unbounded retry; same verified pod reused every iteration."""

    def _kubectl_exec_json_shim(self, doc):
        return FakeProc(0, json.dumps(doc))

    def test_accepts_healthy_immediately(self):
        scripted = ScriptedRun()
        scripted.when(_is_kubectl_exec_call, self._kubectl_exec_json_shim({"deployments": []}))
        scripted.when(_is_end_to_end_call, FakeProc(0, json.dumps({"state": "HEALTHY"})))
        args = argparse_namespace(environment=ENVIRONMENT, pod_name="gg-monitor-x", timeout_seconds=600, interval_seconds=15)
        with _env_patch(), mock.patch.object(phase7_monitor, "run", scripted), mock.patch.object(phase7_monitor.time, "sleep") as sleep_mock:
            phase7_monitor.cmd_end_to_end_acceptance(args)
        sleep_mock.assert_not_called()

    def test_retries_broken_within_bounds_then_succeeds(self):
        scripted = ScriptedRun()
        scripted.when(_is_kubectl_exec_call, self._kubectl_exec_json_shim({"deployments": []}))
        attempts = {"n": 0}

        def _e2e_responder(argv):
            attempts["n"] += 1
            if attempts["n"] < 3:
                return FakeProc(1, json.dumps({"state": "BROKEN"}))
            return FakeProc(0, json.dumps({"state": "HEALTHY"}))

        scripted.when(_is_end_to_end_call, _e2e_responder)
        args = argparse_namespace(environment=ENVIRONMENT, pod_name="gg-monitor-x", timeout_seconds=600, interval_seconds=15)
        with _env_patch(), mock.patch.object(phase7_monitor, "run", scripted), mock.patch.object(phase7_monitor.time, "sleep") as sleep_mock:
            phase7_monitor.cmd_end_to_end_acceptance(args)
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(sleep_mock.call_count, 2)

    def test_retries_transient_fetch_failure_within_bounds(self):
        scripted = ScriptedRun()
        attempts = {"n": 0}

        def _kubectl_responder(argv):
            attempts["n"] += 1
            if attempts["n"] < 2:
                return FakeProc(1, "", "Error from server (Forbidden)")
            return FakeProc(0, json.dumps({"deployments": []}))

        scripted.when(_is_kubectl_exec_call, _kubectl_responder)
        scripted.when(_is_end_to_end_call, FakeProc(0, json.dumps({"state": "HEALTHY"})))
        args = argparse_namespace(environment=ENVIRONMENT, pod_name="gg-monitor-x", timeout_seconds=600, interval_seconds=15)
        with _env_patch(), mock.patch.object(phase7_monitor, "run", scripted), mock.patch.object(phase7_monitor.time, "sleep") as sleep_mock:
            phase7_monitor.cmd_end_to_end_acceptance(args)
        self.assertEqual(attempts["n"], 2)
        self.assertEqual(sleep_mock.call_count, 1)
        # A transient fetch failure must never be reported as HEALTHY -- end_to_end tool is never even invoked on that iteration.
        self.assertEqual(len([c for c in scripted.calls if _is_end_to_end_call(c["argv"])]), 1)

    def test_timeout_fails(self):
        scripted = ScriptedRun()
        scripted.when(_is_kubectl_exec_call, self._kubectl_exec_json_shim({"deployments": []}))
        scripted.when(_is_end_to_end_call, FakeProc(1, json.dumps({"state": "BROKEN"})))
        args = argparse_namespace(environment=ENVIRONMENT, pod_name="gg-monitor-x", timeout_seconds=30, interval_seconds=15)
        with _env_patch(), mock.patch.object(phase7_monitor, "run", scripted), mock.patch.object(phase7_monitor.time, "sleep") as sleep_mock:
            with self.assertRaises(phase7_monitor.Phase7MonitorError):
                phase7_monitor.cmd_end_to_end_acceptance(args)
        # timeout=30, interval=15 -> attempts at elapsed=0,15,30 (3 attempts), sleeping exactly twice -- never unbounded.
        e2e_calls = [c for c in scripted.calls if _is_end_to_end_call(c["argv"])]
        self.assertEqual(len(e2e_calls), 3)
        self.assertEqual(sleep_mock.call_count, 2)

    def test_no_unbounded_retry_call_count_matches_bound_exactly(self):
        scripted = ScriptedRun()
        scripted.when(_is_kubectl_exec_call, self._kubectl_exec_json_shim({"deployments": []}))
        scripted.when(_is_end_to_end_call, FakeProc(1, json.dumps({"state": "BROKEN"})))
        args = argparse_namespace(environment=ENVIRONMENT, pod_name="gg-monitor-x", timeout_seconds=45, interval_seconds=15)
        with _env_patch(), mock.patch.object(phase7_monitor, "run", scripted), mock.patch.object(phase7_monitor.time, "sleep"):
            with self.assertRaises(phase7_monitor.Phase7MonitorError):
                phase7_monitor.cmd_end_to_end_acceptance(args)
        # elapsed sequence: 0, 15, 30, 45 -> exactly 4 attempts for a 45s/15s bound, never more.
        e2e_calls = [c for c in scripted.calls if _is_end_to_end_call(c["argv"])]
        self.assertEqual(len(e2e_calls), 4)

    def test_same_verified_pod_reused_every_iteration(self):
        pod_name = "gg-monitor-fixed-pod"
        scripted = ScriptedRun()
        scripted.when(_is_kubectl_exec_call, self._kubectl_exec_json_shim({"deployments": []}))
        attempts = {"n": 0}

        def _e2e_responder(argv):
            attempts["n"] += 1
            return FakeProc(0 if attempts["n"] >= 2 else 1, json.dumps({"state": "BROKEN"}))

        scripted.when(_is_end_to_end_call, _e2e_responder)
        args = argparse_namespace(environment=ENVIRONMENT, pod_name=pod_name, timeout_seconds=600, interval_seconds=15)
        with _env_patch(), mock.patch.object(phase7_monitor, "run", scripted), mock.patch.object(phase7_monitor.time, "sleep"):
            phase7_monitor.cmd_end_to_end_acceptance(args)
        exec_calls = [c for c in scripted.calls if _is_kubectl_exec_call(c["argv"])]
        self.assertGreaterEqual(len(exec_calls), 2)
        for c in exec_calls:
            self.assertIn(pod_name, c["argv"])

    # A: the canonical EKS connection must happen before the FIRST /api/processes fetch, and exactly once, for a valid end-to-end-acceptance invocation.
    def test_connects_to_eks_before_first_fetch_exactly_once(self):
        call_order = []

        def fake_connect():
            call_order.append("connect")

        def fake_fetch(pod_name, namespace, timeout_seconds=5):
            call_order.append("fetch")
            return True, {"deployments": []}

        scripted = ScriptedRun()
        scripted.when(_is_end_to_end_call, FakeProc(0, json.dumps({"state": "HEALTHY"})))
        args = argparse_namespace(environment=ENVIRONMENT, pod_name="gg-monitor-x", timeout_seconds=600, interval_seconds=15)
        with _env_patch(), mock.patch.object(phase7_monitor, "_connect_to_eks", fake_connect), \
                mock.patch.object(phase7_monitor, "_try_fetch_api_processes", fake_fetch), \
                mock.patch.object(phase7_monitor, "run", scripted), \
                mock.patch.object(phase7_monitor.time, "sleep"):
            phase7_monitor.cmd_end_to_end_acceptance(args)
        self.assertEqual(call_order, ["connect", "fetch"])

    # B: an EKS connection failure is a SETUP failure, not a monitor-health state -- it must stop everything before any polling: no fetch, no sleep, no classifier execution.
    def test_eks_connection_failure_stops_before_any_polling(self):
        def fake_connect():
            raise phase7_monitor.Phase7MonitorError("simulated EKS connection failure (AccessDenied/expired credentials/cluster not found/network failure)")

        fetch_mock = mock.Mock()
        run_mock = mock.Mock()
        args = argparse_namespace(environment=ENVIRONMENT, pod_name="gg-monitor-x", timeout_seconds=600, interval_seconds=15)
        with _env_patch(), mock.patch.object(phase7_monitor, "_connect_to_eks", fake_connect), \
                mock.patch.object(phase7_monitor, "_try_fetch_api_processes", fetch_mock), \
                mock.patch.object(phase7_monitor, "run", run_mock), \
                mock.patch.object(phase7_monitor.time, "sleep") as sleep_mock:
            with self.assertRaises(phase7_monitor.Phase7MonitorError):
                phase7_monitor.cmd_end_to_end_acceptance(args)
        fetch_mock.assert_not_called()
        run_mock.assert_not_called()
        sleep_mock.assert_not_called()

    # C: a transient /api/processes fetch failure that is later retried successfully must NEVER trigger a second EKS connection -- the kubeconfig binding is one-time command setup, not convergence-loop business logic.
    def test_transient_fetch_retry_does_not_reconnect_to_eks(self):
        connect_mock = mock.Mock()
        scripted = ScriptedRun()
        attempts = {"n": 0}

        def _kubectl_responder(argv):
            attempts["n"] += 1
            if attempts["n"] < 2:
                return FakeProc(1, "", "Error from server (Forbidden)")
            return FakeProc(0, json.dumps({"deployments": []}))

        scripted.when(_is_kubectl_exec_call, _kubectl_responder)
        scripted.when(_is_end_to_end_call, FakeProc(0, json.dumps({"state": "HEALTHY"})))
        args = argparse_namespace(environment=ENVIRONMENT, pod_name="gg-monitor-x", timeout_seconds=600, interval_seconds=15)
        with _env_patch(), mock.patch.object(phase7_monitor, "_connect_to_eks", connect_mock), \
                mock.patch.object(phase7_monitor, "run", scripted), \
                mock.patch.object(phase7_monitor.time, "sleep"):
            phase7_monitor.cmd_end_to_end_acceptance(args)
        self.assertEqual(connect_mock.call_count, 1)

    # D: a persistent bounded BROKEN/timeout sequence must also never reconnect -- the existing 30s/15s bound (3 attempts) is preserved exactly, and EKS is connected exactly once for the whole command.
    def test_persistent_bounded_failure_does_not_reconnect_to_eks(self):
        connect_mock = mock.Mock()
        scripted = ScriptedRun()
        scripted.when(_is_kubectl_exec_call, self._kubectl_exec_json_shim({"deployments": []}))
        scripted.when(_is_end_to_end_call, FakeProc(1, json.dumps({"state": "BROKEN"})))
        args = argparse_namespace(environment=ENVIRONMENT, pod_name="gg-monitor-x", timeout_seconds=30, interval_seconds=15)
        with _env_patch(), mock.patch.object(phase7_monitor, "_connect_to_eks", connect_mock), \
                mock.patch.object(phase7_monitor, "run", scripted), \
                mock.patch.object(phase7_monitor.time, "sleep") as sleep_mock:
            with self.assertRaises(phase7_monitor.Phase7MonitorError):
                phase7_monitor.cmd_end_to_end_acceptance(args)
        e2e_calls = [c for c in scripted.calls if _is_end_to_end_call(c["argv"])]
        self.assertEqual(len(e2e_calls), 3)
        self.assertEqual(connect_mock.call_count, 1)
        self.assertEqual(sleep_mock.call_count, 2)

    # E: invalid polling bounds must still fail BEFORE any EKS connection -- the just-approved bound-validation-first contract is not weakened by adding the EKS connection.
    def test_invalid_bounds_never_call_connect_to_eks(self):
        for kwargs in (
            dict(timeout_seconds=30, interval_seconds=0),
            dict(timeout_seconds=30, interval_seconds=-1),
            dict(timeout_seconds=0, interval_seconds=15),
            dict(timeout_seconds=-1, interval_seconds=15),
        ):
            with self.subTest(**kwargs):
                connect_mock = mock.Mock()
                scripted = ScriptedRun()
                args = argparse_namespace(environment=ENVIRONMENT, pod_name="gg-monitor-x", **kwargs)
                with _env_patch(), mock.patch.object(phase7_monitor, "_connect_to_eks", connect_mock), \
                        mock.patch.object(phase7_monitor, "run", scripted), \
                        mock.patch.object(phase7_monitor.time, "sleep") as sleep_mock:
                    with self.assertRaises(phase7_monitor.Phase7MonitorError):
                        phase7_monitor.cmd_end_to_end_acceptance(args)
                self.assertEqual(connect_mock.call_count, 0)
                self.assertEqual(scripted.calls, [])
                sleep_mock.assert_not_called()

    # F: the canonical _connect_to_eks() command shape/values are reused exactly -- no duplicated/hardcoded EKS logic is introduced inside cmd_end_to_end_acceptance itself.
    def test_e2e_connect_uses_canonical_eks_command_and_values(self):
        scripted = ScriptedRun()
        scripted.when(_is_kubectl_exec_call, self._kubectl_exec_json_shim({"deployments": []}))
        scripted.when(_is_end_to_end_call, FakeProc(0, json.dumps({"state": "HEALTHY"})))
        args = argparse_namespace(environment=ENVIRONMENT, pod_name="gg-monitor-x", timeout_seconds=600, interval_seconds=15)
        with _env_patch(), mock.patch.object(phase7_monitor, "run", scripted), mock.patch.object(phase7_monitor.time, "sleep"):
            phase7_monitor.cmd_end_to_end_acceptance(args)
        eks_calls = [c for c in scripted.calls if _starts_with("aws", "eks", "update-kubeconfig")(c["argv"])]
        self.assertEqual(len(eks_calls), 1)
        self.assertEqual(eks_calls[0]["argv"], [
            "aws", "eks", "update-kubeconfig",
            "--region", AWS_REGION_VALUE,
            "--name", EKS_CLUSTER_NAME_VALUE,
            "--role-arn", EKS_DEPLOY_ROLE_ARN_VALUE,
            "--assume-role-arn", EKS_DEPLOY_ROLE_ARN_VALUE,
        ])
        import inspect
        source = inspect.getsource(phase7_monitor.cmd_end_to_end_acceptance)
        self.assertIn("_connect_to_eks()", source)
        # No duplicated argv construction -- cmd_end_to_end_acceptance must delegate to the shared helper, never build its own "aws", "eks", "update-kubeconfig" argument array (an explanatory comment may legitimately mention the words; only the literal argv-construction pattern is forbidden here).
        self.assertNotIn('"aws", "eks", "update-kubeconfig"', source)

    def test_empty_pod_name_fails_closed_before_any_fetch(self):
        scripted = ScriptedRun()
        args = argparse_namespace(environment=ENVIRONMENT, pod_name="", timeout_seconds=600, interval_seconds=15)
        with _env_patch(), mock.patch.object(phase7_monitor, "run", scripted):
            with self.assertRaises(phase7_monitor.Phase7MonitorError):
                phase7_monitor.cmd_end_to_end_acceptance(args)
        self.assertEqual(scripted.calls, [])

    # G: interval_seconds=0 must fail BEFORE any polling -- this is the exact defect: `elapsed += interval_seconds` never advances elapsed when interval_seconds is 0, producing a loop that cannot naturally terminate.
    def test_zero_interval_fails_before_polling(self):
        scripted = ScriptedRun()
        args = argparse_namespace(environment=ENVIRONMENT, pod_name="gg-monitor-x", timeout_seconds=30, interval_seconds=0)
        with _env_patch(), mock.patch.object(phase7_monitor, "run", scripted), mock.patch.object(phase7_monitor.time, "sleep") as sleep_mock:
            with self.assertRaises(phase7_monitor.Phase7MonitorError):
                phase7_monitor.cmd_end_to_end_acceptance(args)
        self.assertEqual(scripted.calls, [])
        sleep_mock.assert_not_called()

    # H: a negative interval must fail the same way as zero.
    def test_negative_interval_fails_before_polling(self):
        scripted = ScriptedRun()
        args = argparse_namespace(environment=ENVIRONMENT, pod_name="gg-monitor-x", timeout_seconds=30, interval_seconds=-1)
        with _env_patch(), mock.patch.object(phase7_monitor, "run", scripted), mock.patch.object(phase7_monitor.time, "sleep") as sleep_mock:
            with self.assertRaises(phase7_monitor.Phase7MonitorError):
                phase7_monitor.cmd_end_to_end_acceptance(args)
        self.assertEqual(scripted.calls, [])
        sleep_mock.assert_not_called()

    # I: a zero or negative timeout must also fail before any polling.
    def test_zero_or_negative_timeout_fails_before_polling(self):
        for bad_timeout in (0, -1):
            with self.subTest(bad_timeout=bad_timeout):
                scripted = ScriptedRun()
                args = argparse_namespace(environment=ENVIRONMENT, pod_name="gg-monitor-x", timeout_seconds=bad_timeout, interval_seconds=15)
                with _env_patch(), mock.patch.object(phase7_monitor, "run", scripted), mock.patch.object(phase7_monitor.time, "sleep") as sleep_mock:
                    with self.assertRaises(phase7_monitor.Phase7MonitorError):
                        phase7_monitor.cmd_end_to_end_acceptance(args)
                self.assertEqual(scripted.calls, [])
                sleep_mock.assert_not_called()

    # I (continued): non-integer bounds (defense in depth -- never rely on argparse's own type=int alone) must also fail closed, including bool (a subclass of int in Python that must never be silently accepted as a duration).
    def test_non_integer_bounds_fail_before_polling(self):
        for bad_value in (True, False, "30", 30.5, None):
            with self.subTest(bad_value=bad_value):
                scripted = ScriptedRun()
                args = argparse_namespace(environment=ENVIRONMENT, pod_name="gg-monitor-x", timeout_seconds=bad_value, interval_seconds=15)
                with _env_patch(), mock.patch.object(phase7_monitor, "run", scripted), mock.patch.object(phase7_monitor.time, "sleep") as sleep_mock:
                    with self.assertRaises(phase7_monitor.Phase7MonitorError):
                        phase7_monitor.cmd_end_to_end_acceptance(args)
                self.assertEqual(scripted.calls, [])
                sleep_mock.assert_not_called()

    # L: a kubectl exec wall-clock timeout while fetching /api/processes during E2E convergence is a TRANSIENT, RETRYABLE fetch failure -- never an unbounded loop, never immediately fatal. It must be retried within the existing bounded window, succeeding once a later fetch succeeds, or failing at the bound if it never does.
    def test_api_processes_kubectl_exec_timeout_is_retried_and_can_still_succeed(self):
        scripted = ScriptedRun()
        attempts = {"n": 0}

        def _kubectl_responder(argv):
            attempts["n"] += 1
            if attempts["n"] < 2:
                return FakeProc(124, "", "TIMEOUT: command exceeded 20s and was terminated")
            return FakeProc(0, json.dumps({"deployments": []}))

        scripted.when(_is_kubectl_exec_call, _kubectl_responder)
        scripted.when(_is_end_to_end_call, FakeProc(0, json.dumps({"state": "HEALTHY"})))
        args = argparse_namespace(environment=ENVIRONMENT, pod_name="gg-monitor-x", timeout_seconds=600, interval_seconds=15)
        with _env_patch(), mock.patch.object(phase7_monitor, "run", scripted), mock.patch.object(phase7_monitor.time, "sleep") as sleep_mock:
            phase7_monitor.cmd_end_to_end_acceptance(args)
        self.assertEqual(attempts["n"], 2)
        self.assertEqual(sleep_mock.call_count, 1)
        # A timed-out fetch must never even reach the classifier -- it is a fetch failure, not a HEALTHY/BROKEN classification.
        self.assertEqual(len([c for c in scripted.calls if _is_end_to_end_call(c["argv"])]), 1)

    def test_api_processes_kubectl_exec_timeout_persistent_fails_at_bound_never_unbounded(self):
        scripted = ScriptedRun()
        scripted.when(_is_kubectl_exec_call, FakeProc(124, "", "TIMEOUT: command exceeded 20s and was terminated"))
        args = argparse_namespace(environment=ENVIRONMENT, pod_name="gg-monitor-x", timeout_seconds=30, interval_seconds=15)
        with _env_patch(), mock.patch.object(phase7_monitor, "run", scripted), mock.patch.object(phase7_monitor.time, "sleep") as sleep_mock:
            with self.assertRaises(phase7_monitor.Phase7MonitorError):
                phase7_monitor.cmd_end_to_end_acceptance(args)
        # elapsed sequence 0, 15, 30 -> exactly 3 fetch attempts for a 30s/15s bound, never more -- a persistently timing-out fetch must still terminate at the bound.
        exec_calls = [c for c in scripted.calls if _is_kubectl_exec_call(c["argv"])]
        self.assertEqual(len(exec_calls), 3)
        self.assertEqual(sleep_mock.call_count, 2)


class EndToEndAcceptanceRealClassifierIntegrationTests(unittest.TestCase):
    """Pre-VDR correction integration proof: unlike every other test in this file, `run` here is the REAL phase7_monitor.run (not a ScriptedRun fake), so cmd_end_to_end_acceptance()'s subprocess call to automation/orchestration/end_to_end_acceptance.py actually executes the REAL, unmodified classifier against the REAL current folder-driven active-deployment model -- only _connect_to_eks (no real AWS) and _try_fetch_api_processes (no real kubectl) are mocked, so this remains fully offline. This deliberately does NOT re-implement any of the classifier's own schema-validation rules -- it only proves the wiring: a genuine classifier non-zero/BROKEN result can never be reported/returned as HEALTHY by cmd_end_to_end_acceptance()."""

    # Same malformed shape as automation/phases/phase7/tests/test_end_to_end_acceptance.py's exact-reproduction test, fed straight to the REAL classifier subprocess via a mocked _try_fetch_api_processes (never a fake classifier response).
    _MALFORMED_API_PROCESSES_DOC = {
        "generatedAt": 1_700_000_100,
        "deployments": [
            {
                "deploymentName": "gg-postgresql-repltest-01",
                "deploymentType": "postgresql",
                "enabled": True,
                "effectiveStatus": "UP",
                "ageSeconds": 5,
                "fresh": True,
                "lease": {"holder": "gg-monitor-x", "fresh": True},
                "criticalServices": {"admin": True},
                "processDiscovery": "MALFORMED-DISCOVERY",
                "processes": {},
            },
            {
                "deploymentName": "gg-mssql-repltest-01",
                "deploymentType": "mssql",
                "enabled": True,
                "effectiveStatus": "UP",
                "ageSeconds": 5,
                "fresh": True,
                "lease": {"holder": "gg-monitor-x", "fresh": True},
                "criticalServices": {"admin": True},
                "processDiscovery": None,
                "processes": [{}],
            },
        ],
    }

    def test_real_classifier_broken_result_can_never_be_reported_healthy(self):
        fake_fetch = lambda pod_name, namespace, timeout_seconds=5: (True, self._MALFORMED_API_PROCESSES_DOC)
        args = argparse_namespace(environment=ENVIRONMENT, pod_name="gg-monitor-x", timeout_seconds=1, interval_seconds=1)
        captured = io.StringIO()
        with _env_patch(), mock.patch.object(phase7_monitor, "_connect_to_eks", mock.Mock()), \
                mock.patch.object(phase7_monitor, "_try_fetch_api_processes", fake_fetch), \
                mock.patch.object(phase7_monitor.time, "sleep"), \
                redirect_stdout(captured):
            with self.assertRaises(phase7_monitor.Phase7MonitorError):
                phase7_monitor.cmd_end_to_end_acceptance(args)
        output = captured.getvalue()
        self.assertNotIn("OK: GoldenGate monitor-to-runtime end-to-end acceptance is HEALTHY.", output)
        # Proves the REAL classifier genuinely ran (never mocked) and genuinely returned BROKEN.
        self.assertIn('"state": "BROKEN"', output)


class SanitizedDiagnosticTests(unittest.TestCase):
    """Failure diagnostics never blindly re-emit unbounded raw kubectl stderr -- bounded to the last N characters via _sanitize_tail()."""

    def test_sanitize_tail_bounds_length(self):
        huge = "x" * 5000
        sanitized = phase7_monitor._sanitize_tail(huge)
        self.assertLessEqual(len(sanitized), phase7_monitor._SANITIZED_ERROR_TAIL_CHARS)

    def test_sanitize_tail_handles_empty(self):
        self.assertEqual(phase7_monitor._sanitize_tail(""), "<no diagnostic output>")


if __name__ == "__main__":
    unittest.main()
