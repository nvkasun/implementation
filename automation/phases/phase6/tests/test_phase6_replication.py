"""Offline tests for automation/phases/phase6/phase6_replication.py; run directly via `python3 automation/phases/phase6/tests/test_phase6_replication.py`. No live AWS/Kubernetes/GoldenGate REST mutation -- every subprocess call is intercepted via a scripted fake that asserts on the exact argv and returns a fabricated result (except the discovery/no-op smoke tests, which deliberately exercise the REAL automation/goldengate-deployment-model.py against the CURRENT repository, since both active runtime descriptors genuinely have replication.enabled=false and this is read-only). Covers: local pipeline discovery (zero AWS/kubectl before AWS credentials are ever needed), Deploy-mode zero-pipeline no-op, EKS connection contract, rendered-manifest structural validation (reusing the SAME helper for Deploy and Validate), execution-resource collision preflight, apply order, the 600-second wait contract, failure-evidence retention, success cleanup, sequential/fail-fast pipeline ordering, Validate-mode local-only behaviour, and the workflow's AWS-credential output-scoping contract."""
from __future__ import annotations

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

import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
TOOL_PATH = REPO_ROOT / "automation" / "phases" / "phase6" / "phase6_replication.py"
ENGINE_TOOL_PATH = REPO_ROOT / "automation" / "goldengate-replication.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "00-main-goldengate-orchestrator.yaml"


def _load_tool(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


phase6 = _load_tool(TOOL_PATH, "phase6_replication")
engine = _load_tool(ENGINE_TOOL_PATH, "goldengate_replication")

# The REAL current trusted reconciler source -- fixtures use this (never a hand-rolled placeholder string) as the ConfigMap's embedded goldengate-replication.py, so every happy-path test satisfies the exact byte/text-equivalence contract _validate_rendered_manifests() now enforces against this SAME file.
with open(ENGINE_TOOL_PATH, "r", encoding="utf-8") as _f:
    REAL_ENGINE_SOURCE = _f.read()

ENVIRONMENT = "dev"
NAMESPACE = "goldengate-dev"
AWS_REGION_VALUE = "eu-west-1"
EKS_CLUSTER_NAME_VALUE = "gg-dev-cluster"
EKS_DEPLOY_ROLE_ARN_VALUE = "arn:aws:iam::668311715351:role/GoldenGateEksDeployRole-dev"

# A realistic, self-contained replication plan fixture -- the same shape automation/test-goldengate-replication.py's own PLAN fixture uses -- fed directly into the REAL engine's render_manifests() so every rendered-manifest test below exercises genuine engine output, never a hand-rolled/independently-drifting approximation of it.
PLAN = {
    "pipelineId": "payments-pg-to-mssql-001",
    "tlsSecret": "dev/goldengate/tls-certificate",
    "networkCredentialDomain": "Network",
    "networkCredentialAlias": "NET_TEST",
    "source": {
        "deploymentId": "gg-pg-src-fixture-01", "deploymentType": "postgresql",
        "runtimeHost": "gg-pg-src-fixture-01.goldengate-dev.adcbmis.local",
        "serviceAccount": "gg-runtime-sa",
        "image": "229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-postgresql:23.26.2.0.1",
        "adminSecret": "dev/goldengate/source/admin",
        "databaseSecret": "dev/goldengate/databases/payments-pg-to-mssql-001/source",
        "databaseCredentialAlias": "SRC_ALIAS", "databaseCredentialDomain": "OracleGoldenGate",
    },
    "target": {
        "deploymentId": "gg-mssql-tgt-fixture-01", "deploymentType": "mssql",
        "runtimeHost": "gg-mssql-tgt-fixture-01.goldengate-dev.adcbmis.local",
        "serviceAccount": "gg-runtime-sa",
        "image": "229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-sqlserver:23.26.2.0.1",
        "adminSecret": "dev/goldengate/target/admin",
        "databaseSecret": "dev/goldengate/databases/payments-pg-to-mssql-001/target",
        "databaseCredentialAlias": "TGT_ALIAS", "databaseCredentialDomain": "OracleGoldenGate",
    },
    "checkpoint": {"enabled": True, "table": "dbo.gg_checkpoint", "createIfMissing": True},
    "replicat": {
        "name": "MSTGT01", "sourceTrailName": "ma", "begin": "now", "startOnCreate": True,
        "mappings": [{"source": "public.payments", "target": "dbo.payments"}],
    },
    "supplementalLogging": {"objects": ["public.payments"]},
    "extract": {
        "name": "PGSRC01", "pluginType": "pgoutput", "begin": "now", "startOnCreate": True,
        "trail": {"name": "pa", "sizeMB": 500}, "tables": ["public.payments"],
    },
    "distribution": {
        "pathName": "PG2MS01", "sourceTrailName": "pa", "targetTrailName": "ma",
        "protocol": "wss", "port": 443, "startOnCreate": True,
    },
}


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class ScriptedRun:
    """Replaces phase6_replication.run with a scripted responder: a list of (predicate, FakeProc-or-callable) pairs consulted in order (later registrations take precedence), falling back to a default success. Every call is recorded for assertion. A registered value may be a callable(argv) -> FakeProc instead of a static FakeProc, letting a single rule (e.g. render-job) perform a real filesystem side effect (writing the three rendered manifests) before returning its result."""

    def __init__(self, default=None):
        self.rules = []
        self.calls = []
        self.default = default if default is not None else FakeProc(0, "", "")

    def when(self, predicate, proc_or_fn):
        self.rules.append((predicate, proc_or_fn))
        return self

    def _resolve(self, proc_or_fn, argv):
        return proc_or_fn(argv) if callable(proc_or_fn) and not isinstance(proc_or_fn, FakeProc) else proc_or_fn

    def __call__(self, argv, env=None, cwd=None, check=True, capture_output=True, input_text=None):
        self.calls.append({"argv": list(argv), "env": env})
        for predicate, proc_or_fn in reversed(self.rules):
            if predicate(argv):
                proc = self._resolve(proc_or_fn, argv)
                if check and proc.returncode != 0:
                    raise phase6.Phase6Error(f"{' '.join(str(a) for a in argv)} failed: {proc.stdout}\n{proc.stderr}")
                return proc
        if check and self.default.returncode != 0:
            raise phase6.Phase6Error(f"{' '.join(str(a) for a in argv)} failed: {self.default.stdout}\n{self.default.stderr}")
        return self.default


def _starts_with(*prefix):
    return lambda argv: list(argv[:len(prefix)]) == list(prefix)


def _is_validate_call(argv):
    return len(argv) >= 2 and argv[1] == str(phase6.DEPLOYMENT_MODEL_TOOL) and argv[-1] == "validate"


def _is_pipelines_call(argv):
    return len(argv) >= 2 and argv[1] == str(phase6.DEPLOYMENT_MODEL_TOOL) and argv[-1] == "replication-pipelines"


def _is_render_job_call(argv):
    return len(argv) >= 2 and argv[1] == str(phase6.REPLICATION_ENGINE_TOOL) and "render-job" in argv


def _is_replication_plan_call(argv):
    return len(argv) >= 2 and argv[1] == str(phase6.DEPLOYMENT_MODEL_TOOL) and "replication-plan" in argv


def _extract_arg(argv, flag):
    return argv[argv.index(flag) + 1]


def _is_expected_render_call(argv):
    """_validate_rendered_manifests() now independently re-renders EXPECTED manifests (via _render_expected_manifests_for_validation()) into its own tempfile.TemporaryDirectory() -- distinguishable from the ACTUAL render's --output-dir (always REPO_ROOT/work/replication/<pipeline>/<execution>) by its "phase6-expected-render-" tempdir prefix."""
    return _is_render_job_call(argv) and "phase6-expected-render-" in _extract_arg(argv, "--output-dir")


def _is_actual_render_call(argv):
    """The render-job call for the manifests actually being reconciled/applied -- as opposed to the SECOND, independent 'expected' re-render _validate_rendered_manifests() now performs as its final authoritative proof (see _is_expected_render_call())."""
    return _is_render_job_call(argv) and not _is_expected_render_call(argv)


def _validate_ok(_argv):
    return FakeProc(0, f"OK: {ENVIRONMENT} deployment descriptors are valid")


def _pipelines_response(pipeline_ids):
    text = "".join(f"{p}\n" for p in pipeline_ids)
    return lambda _argv: FakeProc(0, text)


def _plan_for_pipeline_id(base_plan, pipeline_id):
    """Overrides base_plan's own pipelineId field to exactly the requested pipeline_id -- lets fixtures discover/render a pipeline ID that differs from PLAN's own literal pipelineId (e.g. "fabricated-pipeline-001") while keeping the FAKE render-job output and the FAKE canonical replication-plan lookup mutually self-consistent, exactly as the real system always is (both are derived from the same real descriptor for a given pipeline_id)."""
    overridden = dict(base_plan)
    overridden["pipelineId"] = pipeline_id
    return overridden


def _replication_plan_response(plan):
    """Fakes automation/goldengate-deployment-model.py's own `replication-plan <pipeline_id>` CLI -- returns `plan` with pipelineId overridden to the REQUESTED pipeline_id (argv[-1]), kept in lockstep with _render_job_side_effect()'s own override below. _load_canonical_replication_plan() now shells out to this for EVERY manifest validation, so every full-flow scripted test must supply it (see _standard_deploy_scripted())."""
    def _fn(argv):
        return FakeProc(0, json.dumps(_plan_for_pipeline_id(plan, argv[-1])))
    return _fn


def _render_job_side_effect(plan=None):
    """Fakes automation/goldengate-replication.py's own render-job CLI: derives the same three manifests the REAL engine would (via engine.render_manifests(), never a hand-rolled approximation) using the REAL current trusted reconciler source (REAL_ENGINE_SOURCE, never a placeholder string) and a plan whose pipelineId is overridden to the REQUESTED pipeline_id (argv[-1], kept in lockstep with _replication_plan_response() above), then writes them exactly where the real CLI writes them -- so _validate_rendered_manifests() downstream reads genuine engine output that satisfies both the exact-reconciler-source and exact-canonical-plan equivalence contracts."""
    base_plan = plan or PLAN

    def _fn(argv):
        output_dir = _extract_arg(argv, "--output-dir")
        namespace = _extract_arg(argv, "--namespace")
        region = _extract_arg(argv, "--region")
        execution_id = _extract_arg(argv, "--execution-id")
        effective_plan = _plan_for_pipeline_id(base_plan, argv[-1])
        manifests = engine.render_manifests(effective_plan, namespace, region, REAL_ENGINE_SOURCE, execution_id)
        os.makedirs(output_dir, exist_ok=True)
        for kind, doc in manifests.items():
            with open(os.path.join(output_dir, f"{kind.lower()}.yaml"), "w") as f:
                yaml.safe_dump(doc, f, sort_keys=False, default_flow_style=False)
        return FakeProc(0, "")
    return _fn


def _standard_deploy_scripted(pipeline_ids=("payments-pg-to-mssql-001",), plan=None):
    """The happy-path scripted responder every Deploy-mode reconciliation test starts from -- collision preflight absent (kubectl get --ignore-not-found -o name -> rc=0, empty stdout, the TRUE absence shape), canonical replication-plan lookup returns the SAME plan fixture the render used, apply/wait/logs/delete all succeed. Individual tests register additional .when() rules AFTERWARD to inject a specific failure at a specific point (later registrations take precedence)."""
    scripted = ScriptedRun()
    scripted.when(_is_validate_call, _validate_ok)
    scripted.when(_is_pipelines_call, _pipelines_response(list(pipeline_ids)))
    scripted.when(_is_replication_plan_call, _replication_plan_response(plan or PLAN))
    scripted.when(_is_render_job_call, _render_job_side_effect(plan))
    scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
    scripted.when(_starts_with("kubectl", "get"), FakeProc(0, "", ""))
    scripted.when(_starts_with("kubectl", "apply"), FakeProc(0, ""))
    scripted.when(_starts_with("kubectl", "wait"), FakeProc(0, ""))
    scripted.when(_starts_with("kubectl", "logs"), FakeProc(0, "sanitized log line"))
    scripted.when(_starts_with("kubectl", "delete"), FakeProc(0, ""))
    return scripted


def _env_patch(**overrides):
    base = {
        "AWS_REGION": AWS_REGION_VALUE, "EKS_CLUSTER_NAME": EKS_CLUSTER_NAME_VALUE,
        "EKS_DEPLOY_ROLE_ARN": EKS_DEPLOY_ROLE_ARN_VALUE, "RUNTIME_NAMESPACE": NAMESPACE,
    }
    base.update(overrides)
    return mock.patch.dict(os.environ, base, clear=False)


def _run_quiet(func, *args, **kwargs):
    with redirect_stdout(io.StringIO()):
        return func(*args, **kwargs)


class argparse_namespace:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _write_manifests(plan=None, namespace=NAMESPACE, region=AWS_REGION_VALUE, execution_id="test-exec-1", tmp_dir=None):
    """Renders the three REAL engine manifests directly (bypassing the render-job subprocess entirely) into tmp_dir, using the REAL current trusted reconciler source (REAL_ENGINE_SOURCE) -- used by the RenderStructureTests below to feed genuine-shaped input into _validate_rendered_manifests() and then corrupt one specific field per test."""
    plan = plan or PLAN
    manifests = engine.render_manifests(plan, namespace, region, REAL_ENGINE_SOURCE, execution_id)
    for kind, doc in manifests.items():
        with open(os.path.join(tmp_dir, f"{kind.lower()}.yaml"), "w") as f:
            yaml.safe_dump(doc, f, sort_keys=False, default_flow_style=False)
    return manifests


def _load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def _dump_yaml(path, doc):
    with open(path, "w") as f:
        yaml.safe_dump(doc, f, sort_keys=False, default_flow_style=False)


def _expected_manifests_fixture(plan=None, namespace=NAMESPACE, region=AWS_REGION_VALUE, execution_id="test-exec-1"):
    """Renders a FRESH manifest set via the REAL engine -- mirroring exactly what phase6._render_expected_manifests_for_validation() would independently re-render for these inputs -- for use as its mocked return value. Deliberately a SEPARATE engine.render_manifests() call (never the same in-memory object the actual manifests came from), so a test that mutates only the ACTUAL on-disk files still exercises a genuinely independent expected comparison."""
    plan = plan or PLAN
    return engine.render_manifests(plan, namespace, region, REAL_ENGINE_SOURCE, execution_id)


def _validate_with_canonical_plan(output_dir, environment=ENVIRONMENT, pipeline_id=None, execution_id="test-exec-1", namespace=NAMESPACE, region=AWS_REGION_VALUE, plan=None, expected_plan=None):
    """Calls phase6._validate_rendered_manifests() with _load_canonical_replication_plan() mocked to return the given (or PLAN's own) canonical plan directly, and _render_expected_manifests_for_validation() mocked to a FRESH engine render using expected_plan (defaults to the SAME plan) -- avoids re-scripting a full goldengate-deployment-model.py replication-plan / goldengate-replication.py render-job subprocess call for every structural-validation test. The dedicated CanonicalPlanBindingTests class below exercises the plan subprocess contract directly; ExpectedManifestAuthorityTests exercises the expected-render contract directly."""
    plan = plan or PLAN
    pipeline_id = pipeline_id or plan["pipelineId"]
    expected = _expected_manifests_fixture(plan=expected_plan or plan, namespace=namespace, region=region, execution_id=execution_id)
    with mock.patch.object(phase6, "_load_canonical_replication_plan", return_value=plan), \
         mock.patch.object(phase6, "_render_expected_manifests_for_validation", return_value=expected):
        return phase6._validate_rendered_manifests(output_dir, environment, pipeline_id, execution_id, namespace, region)


class DiscoveryTests(unittest.TestCase):
    """1-5: local pipeline discovery -- current disabled model, literal has_pipelines output, a fabricated enabled pipeline, fail-closed model validation, and zero AWS/kubectl calls."""

    def test_1_current_replication_disabled_model_has_no_pipelines(self):
        """Exercises the REAL automation/goldengate-deployment-model.py against the CURRENT repository (read-only) -- both active runtime descriptors genuinely have replication.enabled=false today."""
        pipelines = phase6._discover_pipelines(ENVIRONMENT)
        self.assertEqual(pipelines, [])

    def test_2_discovery_writes_literal_has_pipelines_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "github_output")
            Path(output_path).touch()
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": output_path}):
                _run_quiet(phase6.cmd_discover, argparse_namespace(environment=ENVIRONMENT))
            content = Path(output_path).read_text()
        self.assertEqual(content, "has_pipelines=false\n")

    def test_3_fabricated_valid_enabled_pipeline_has_pipelines_true(self):
        scripted = _standard_deploy_scripted(pipeline_ids=("fabricated-pipeline-001",))
        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "github_output")
            Path(output_path).touch()
            with mock.patch.object(phase6, "run", scripted), mock.patch.dict(os.environ, {"GITHUB_OUTPUT": output_path}):
                _run_quiet(phase6.cmd_discover, argparse_namespace(environment=ENVIRONMENT))
            content = Path(output_path).read_text()
        self.assertEqual(content, "has_pipelines=true\n")

    def test_4_model_validation_failure_fails_closed(self):
        scripted = ScriptedRun()
        scripted.when(_is_validate_call, FakeProc(1, "", "INVALID: envs/dev/bad-descriptor: malformed"))
        with mock.patch.object(phase6, "run", scripted):
            with self.assertRaises(phase6.Phase6Error):
                _run_quiet(phase6.cmd_discover, argparse_namespace(environment=ENVIRONMENT))
        self.assertEqual([c for c in scripted.calls if _is_pipelines_call(c["argv"])], [], "replication-pipelines must never be listed after validation already failed")

    def test_5_discovery_never_calls_aws_or_kubectl(self):
        scripted = _standard_deploy_scripted(pipeline_ids=("fabricated-pipeline-001",))
        with mock.patch.object(phase6, "run", scripted):
            _run_quiet(phase6.cmd_discover, argparse_namespace(environment=ENVIRONMENT))
        self.assertEqual([c for c in scripted.calls if c["argv"][:1] in (["aws"], ["kubectl"])], [])


class DeployNoOpTests(unittest.TestCase):
    """6-9: zero-pipeline Deploy is a true no-op -- zero AWS calls, zero kubectl calls, zero work manifests/resources created."""

    def _run_zero_pipeline_reconcile(self, tmp):
        scripted = ScriptedRun()
        scripted.when(_is_validate_call, _validate_ok)
        scripted.when(_is_pipelines_call, _pipelines_response([]))
        with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
            result = _run_quiet(phase6.cmd_reconcile, argparse_namespace(environment=ENVIRONMENT, execution_id="1-1"))
        return result, scripted

    def test_6_no_pipeline_reconcile_returns_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _scripted = self._run_zero_pipeline_reconcile(tmp)
        self.assertEqual(result, 0)

    def test_7_no_pipeline_path_has_zero_aws(self):
        with tempfile.TemporaryDirectory() as tmp:
            _result, scripted = self._run_zero_pipeline_reconcile(tmp)
        self.assertEqual([c for c in scripted.calls if c["argv"][:1] == ["aws"]], [])

    def test_8_no_pipeline_path_has_zero_kubectl(self):
        with tempfile.TemporaryDirectory() as tmp:
            _result, scripted = self._run_zero_pipeline_reconcile(tmp)
        self.assertEqual([c for c in scripted.calls if c["argv"][:1] == ["kubectl"]], [])

    def test_9_no_pipeline_path_creates_no_work_manifests(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run_zero_pipeline_reconcile(tmp)
            self.assertFalse((Path(tmp) / "work").exists())


class EksConnectionTests(unittest.TestCase):
    """10-12: exact update-kubeconfig role semantics, AWS failure fails closed, and a failed EKS connection is never reinterpreted as an empty-pipeline no-op."""

    def test_10_exact_update_kubeconfig_role_semantics(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
        with mock.patch.object(phase6, "run", scripted), _env_patch():
            phase6._connect_to_eks()
        call = next(c["argv"] for c in scripted.calls if c["argv"][:3] == ["aws", "eks", "update-kubeconfig"])
        self.assertEqual(call, [
            "aws", "eks", "update-kubeconfig", "--region", AWS_REGION_VALUE, "--name", EKS_CLUSTER_NAME_VALUE,
            "--role-arn", EKS_DEPLOY_ROLE_ARN_VALUE, "--assume-role-arn", EKS_DEPLOY_ROLE_ARN_VALUE,
        ])

    def test_11_aws_failure_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(255, "", "AccessDenied"))
        with mock.patch.object(phase6, "run", scripted), _env_patch():
            with self.assertRaises(phase6.Phase6Error):
                phase6._connect_to_eks()

    def test_12_no_failed_eks_inspection_becomes_noop(self):
        scripted = _standard_deploy_scripted(pipeline_ids=("fabricated-pipeline-001",))
        scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(255, "", "Unauthorized"))
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                with self.assertRaises(phase6.Phase6Error):
                    _run_quiet(phase6.cmd_reconcile, argparse_namespace(environment=ENVIRONMENT, execution_id="1-1"))
        # A real, non-empty pipeline list existed -- the EKS failure must propagate as a hard failure, never a silent "no pipelines" no-op.
        self.assertEqual([c for c in scripted.calls if _is_render_job_call(c["argv"])], [])


class RenderStructureTests(unittest.TestCase):
    """13-27: exact structural validation of the three rendered manifests -- kinds, namespace, shared execution name, one-container Job contract, volume read-only mounts, no Secret document, ConfigMap contents, malformed/mismatched inputs fail closed."""

    def _validate(self, tmp):
        return _validate_with_canonical_plan(tmp)

    def test_13_exact_three_manifest_kinds(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp)
            self._validate(tmp)
            self.assertEqual(_load_yaml(os.path.join(tmp, "secretproviderclass.yaml"))["kind"], "SecretProviderClass")
            self.assertEqual(_load_yaml(os.path.join(tmp, "configmap.yaml"))["kind"], "ConfigMap")
            self.assertEqual(_load_yaml(os.path.join(tmp, "job.yaml"))["kind"], "Job")

    def test_14_all_canonical_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp)
            execution_name = self._validate(tmp)
            for manifest in ("secretproviderclass", "configmap", "job"):
                self.assertEqual(_load_yaml(os.path.join(tmp, f"{manifest}.yaml"))["metadata"]["namespace"], NAMESPACE)
            self.assertTrue(execution_name)

    def test_15_same_exact_execution_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp)
            execution_name = self._validate(tmp)
            names = {_load_yaml(os.path.join(tmp, f"{m}.yaml"))["metadata"]["name"] for m in ("secretproviderclass", "configmap", "job")}
            self.assertEqual(names, {execution_name})

    def test_16_one_job_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp)
            job_path = os.path.join(tmp, "job.yaml")
            job = _load_yaml(job_path)
            job["spec"]["template"]["spec"]["containers"].append(dict(job["spec"]["template"]["spec"]["containers"][0]))
            _dump_yaml(job_path, job)
            with self.assertRaises(phase6.Phase6Error):
                self._validate(tmp)

    def test_17_source_image_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp)
            job_path = os.path.join(tmp, "job.yaml")
            job = _load_yaml(job_path)
            job["spec"]["template"]["spec"]["containers"][0]["image"] = "some-other-image:latest"
            _dump_yaml(job_path, job)
            with self.assertRaises(phase6.Phase6Error):
                self._validate(tmp)

    def test_18_source_service_account_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp)
            job_path = os.path.join(tmp, "job.yaml")
            job = _load_yaml(job_path)
            job["spec"]["template"]["spec"]["serviceAccountName"] = "some-other-sa"
            _dump_yaml(job_path, job)
            with self.assertRaises(phase6.Phase6Error):
                self._validate(tmp)

    def test_19_restart_policy_never(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp)
            job_path = os.path.join(tmp, "job.yaml")
            job = _load_yaml(job_path)
            job["spec"]["template"]["spec"]["restartPolicy"] = "Always"
            _dump_yaml(job_path, job)
            with self.assertRaises(phase6.Phase6Error):
                self._validate(tmp)

    def test_20_backofflimit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp)
            job_path = os.path.join(tmp, "job.yaml")
            job = _load_yaml(job_path)
            job["spec"]["backoffLimit"] = 1
            _dump_yaml(job_path, job)
            with self.assertRaises(phase6.Phase6Error):
                self._validate(tmp)

    def test_21_reconciler_volume_readonly(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp)
            job_path = os.path.join(tmp, "job.yaml")
            job = _load_yaml(job_path)
            for vm in job["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]:
                if vm["name"] == "reconciler-script":
                    vm["readOnly"] = False
            _dump_yaml(job_path, job)
            with self.assertRaises(phase6.Phase6Error):
                self._validate(tmp)

    def test_22_replication_secrets_volume_readonly(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp)
            job_path = os.path.join(tmp, "job.yaml")
            job = _load_yaml(job_path)
            for vm in job["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]:
                if vm["name"] == "replication-secrets":
                    vm["readOnly"] = False
            _dump_yaml(job_path, job)
            with self.assertRaises(phase6.Phase6Error):
                self._validate(tmp)

    def test_23_no_kubernetes_secret_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp)
            configmap_path = os.path.join(tmp, "configmap.yaml")
            configmap = _load_yaml(configmap_path)
            configmap["kind"] = "Secret"
            _dump_yaml(configmap_path, configmap)
            with self.assertRaises(phase6.Phase6Error):
                self._validate(tmp)

    def test_24_configmap_contains_engine_and_sanitized_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp)
            configmap_path = os.path.join(tmp, "configmap.yaml")
            configmap = _load_yaml(configmap_path)
            self.assertIn("goldengate-replication.py", configmap["data"])
            self.assertIn("plan.json", configmap["data"])
            del configmap["data"]["plan.json"]
            _dump_yaml(configmap_path, configmap)
            with self.assertRaises(phase6.Phase6Error):
                self._validate(tmp)

    def test_25_malformed_yaml_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp)
            with open(os.path.join(tmp, "job.yaml"), "w") as f:
                f.write("kind: Job\nkind: Job\nmetadata: {name: x}\n")
            with self.assertRaises(phase6.Phase6Error):
                self._validate(tmp)

    def test_26_wrong_namespace_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp)
            configmap_path = os.path.join(tmp, "configmap.yaml")
            configmap = _load_yaml(configmap_path)
            configmap["metadata"]["namespace"] = "some-other-namespace"
            _dump_yaml(configmap_path, configmap)
            with self.assertRaises(phase6.Phase6Error):
                self._validate(tmp)

    def test_27_mismatched_resource_names_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp)
            job_path = os.path.join(tmp, "job.yaml")
            job = _load_yaml(job_path)
            job["metadata"]["name"] = job["metadata"]["name"] + "-mismatched"
            _dump_yaml(job_path, job)
            with self.assertRaises(phase6.Phase6Error):
                self._validate(tmp)


class CollisionPreflightTests(unittest.TestCase):
    """28-32: read-only pre-mutation existence check for the three execution-scoped resources -- all-absent allows apply, any pre-existing resource fails closed before apply with zero mutation."""

    def test_28_all_absent_apply_allowed(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("kubectl", "get"), FakeProc(0, "", ""))
        with mock.patch.object(phase6, "run", scripted):
            phase6._collision_preflight("gg-repl-x-abc12345-1-1", NAMESPACE)

    def test_29_pre_existing_job_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("kubectl", "get"), FakeProc(0, "", ""))
        scripted.when(_starts_with("kubectl", "get", "job"), FakeProc(0, "job/x found"))
        with mock.patch.object(phase6, "run", scripted):
            with self.assertRaises(phase6.Phase6Error):
                phase6._collision_preflight("gg-repl-x-abc12345-1-1", NAMESPACE)

    def test_30_pre_existing_configmap_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("kubectl", "get"), FakeProc(0, "", ""))
        scripted.when(_starts_with("kubectl", "get", "configmap"), FakeProc(0, "configmap/x found"))
        with mock.patch.object(phase6, "run", scripted):
            with self.assertRaises(phase6.Phase6Error):
                phase6._collision_preflight("gg-repl-x-abc12345-1-1", NAMESPACE)

    def test_31_pre_existing_spc_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("kubectl", "get"), FakeProc(0, "", ""))
        scripted.when(_starts_with("kubectl", "get", "secretproviderclass"), FakeProc(0, "secretproviderclass/x found"))
        with mock.patch.object(phase6, "run", scripted):
            with self.assertRaises(phase6.Phase6Error):
                phase6._collision_preflight("gg-repl-x-abc12345-1-1", NAMESPACE)

    def test_32_collision_failure_performs_zero_mutation(self):
        scripted = _standard_deploy_scripted(pipeline_ids=("fabricated-pipeline-001",))
        scripted.when(_starts_with("kubectl", "get", "job"), FakeProc(0, "job/x found"))
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                with self.assertRaises(phase6.Phase6Error):
                    _run_quiet(phase6.cmd_reconcile, argparse_namespace(environment=ENVIRONMENT, execution_id="1-1"))
        self.assertEqual([c for c in scripted.calls if c["argv"][:2] in (["kubectl", "apply"], ["kubectl", "wait"], ["kubectl", "delete"])], [])


class ApplyOrderTests(unittest.TestCase):
    """33-36: apply order SecretProviderClass -> ConfigMap -> Job; a failure at any step prevents every later apply/mutation for that pipeline."""

    def _run_reconcile(self, tmp, scripted):
        with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
            return _run_quiet(phase6.cmd_reconcile, argparse_namespace(environment=ENVIRONMENT, execution_id="1-1"))

    def test_33_apply_order_spc_configmap_job(self):
        scripted = _standard_deploy_scripted(pipeline_ids=("fabricated-pipeline-001",))
        with tempfile.TemporaryDirectory() as tmp:
            self._run_reconcile(tmp, scripted)
        applies = [c["argv"] for c in scripted.calls if c["argv"][:2] == ["kubectl", "apply"]]
        self.assertEqual(len(applies), 3)
        self.assertIn("secretproviderclass.yaml", applies[0][-1])
        self.assertIn("configmap.yaml", applies[1][-1])
        self.assertIn("job.yaml", applies[2][-1])

    def test_34_spc_apply_failure_no_later_apply(self):
        scripted = _standard_deploy_scripted(pipeline_ids=("fabricated-pipeline-001",))
        scripted.when(lambda argv: argv[:2] == ["kubectl", "apply"] and "secretproviderclass.yaml" in argv[-1], FakeProc(1, "", "apply failed"))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._run_reconcile(tmp, scripted)
        applies = [c["argv"] for c in scripted.calls if c["argv"][:2] == ["kubectl", "apply"]]
        self.assertEqual(len(applies), 1)

    def test_35_configmap_apply_failure_job_not_applied(self):
        scripted = _standard_deploy_scripted(pipeline_ids=("fabricated-pipeline-001",))
        scripted.when(lambda argv: argv[:2] == ["kubectl", "apply"] and "configmap.yaml" in argv[-1], FakeProc(1, "", "apply failed"))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._run_reconcile(tmp, scripted)
        applies = [c["argv"] for c in scripted.calls if c["argv"][:2] == ["kubectl", "apply"]]
        self.assertEqual(len(applies), 2)
        self.assertTrue(all("job.yaml" not in a[-1] for a in applies))

    def test_36_job_apply_failure_retains_earlier_evidence(self):
        scripted = _standard_deploy_scripted(pipeline_ids=("fabricated-pipeline-001",))
        scripted.when(lambda argv: argv[:2] == ["kubectl", "apply"] and "job.yaml" in argv[-1], FakeProc(1, "", "apply failed"))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._run_reconcile(tmp, scripted)
        self.assertEqual([c for c in scripted.calls if c["argv"][:2] == ["kubectl", "delete"]], [], "already-applied SecretProviderClass/ConfigMap evidence must never be auto-deleted on a later Job apply failure")


class WaitFailureTests(unittest.TestCase):
    """37-42: exact 600-second wait contract, successful completion proceeds, timeout fails closed with sanitized best-effort logs and retained evidence, and a failure never continues to the next pipeline."""

    def _run_reconcile(self, tmp, scripted, pipeline_ids=("fabricated-pipeline-001",)):
        with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
            return _run_quiet(phase6.cmd_reconcile, argparse_namespace(environment=ENVIRONMENT, execution_id="1-1"))

    def test_37_exact_600_second_wait_contract(self):
        scripted = _standard_deploy_scripted()
        with tempfile.TemporaryDirectory() as tmp:
            self._run_reconcile(tmp, scripted)
        wait_call = next(c["argv"] for c in scripted.calls if c["argv"][:2] == ["kubectl", "wait"])
        self.assertIn("--for=condition=complete", wait_call)
        self.assertIn("--timeout=600s", wait_call)

    def test_38_successful_job_proceeds(self):
        scripted = _standard_deploy_scripted()
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run_reconcile(tmp, scripted)
        self.assertEqual(result, 0)
        self.assertTrue(any(c["argv"][:2] == ["kubectl", "logs"] for c in scripted.calls))
        self.assertEqual(len([c for c in scripted.calls if c["argv"][:2] == ["kubectl", "delete"]]), 3)

    def test_39_timeout_fails(self):
        scripted = _standard_deploy_scripted()
        scripted.when(_starts_with("kubectl", "wait"), FakeProc(1, "", "timed out"))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._run_reconcile(tmp, scripted)

    def test_40_timeout_attempts_sanitized_logs(self):
        scripted = _standard_deploy_scripted()
        scripted.when(_starts_with("kubectl", "wait"), FakeProc(1, "", "timed out"))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._run_reconcile(tmp, scripted)
        self.assertTrue(any(c["argv"][:2] == ["kubectl", "logs"] for c in scripted.calls))

    def test_41_timeout_does_not_clean_evidence(self):
        scripted = _standard_deploy_scripted()
        scripted.when(_starts_with("kubectl", "wait"), FakeProc(1, "", "timed out"))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._run_reconcile(tmp, scripted)
        self.assertEqual([c for c in scripted.calls if c["argv"][:2] == ["kubectl", "delete"]], [])

    def test_42_failure_does_not_continue_to_next_pipeline(self):
        scripted = _standard_deploy_scripted(pipeline_ids=("pipeline-a", "pipeline-b"))
        scripted.when(_starts_with("kubectl", "wait"), FakeProc(1, "", "timed out"))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._run_reconcile(tmp, scripted, pipeline_ids=("pipeline-a", "pipeline-b"))
        render_calls = [c for c in scripted.calls if _is_actual_render_call(c["argv"])]
        self.assertEqual(len(render_calls), 1, "pipeline-b must never be rendered once pipeline-a's Job wait times out")
        self.assertIn("pipeline-a", render_calls[0]["argv"])
        self.assertTrue(all("pipeline-a" in c["argv"] for c in scripted.calls if _is_render_job_call(c["argv"])), "pipeline-b must never be rendered (actual OR expected) once pipeline-a's Job wait times out")


class SuccessCleanupTests(unittest.TestCase):
    """43-46: sanitized logs are retrieved before cleanup, cleanup deletes exactly the execution-scoped Job/ConfigMap/SecretProviderClass, a cleanup failure fails Phase 6A, and no runtime/PVC/EFS delete command exists anywhere in the module."""

    def test_43_success_logs_before_cleanup(self):
        scripted = _standard_deploy_scripted()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                _run_quiet(phase6.cmd_reconcile, argparse_namespace(environment=ENVIRONMENT, execution_id="1-1"))
        logs_index = next(i for i, c in enumerate(scripted.calls) if c["argv"][:2] == ["kubectl", "logs"])
        delete_indexes = [i for i, c in enumerate(scripted.calls) if c["argv"][:2] == ["kubectl", "delete"]]
        self.assertTrue(all(logs_index < i for i in delete_indexes))

    def test_44_cleanup_exact_job_configmap_spc_only(self):
        scripted = _standard_deploy_scripted()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                _run_quiet(phase6.cmd_reconcile, argparse_namespace(environment=ENVIRONMENT, execution_id="1-1"))
        deletes = [c["argv"] for c in scripted.calls if c["argv"][:2] == ["kubectl", "delete"]]
        kinds_deleted = sorted(d[2] for d in deletes)
        self.assertEqual(kinds_deleted, ["configmap", "job", "secretproviderclass"])
        self.assertTrue(all("--ignore-not-found" in d for d in deletes))
        names = {d[3] for d in deletes}
        self.assertEqual(len(names), 1)

    def test_45_cleanup_failure_fails(self):
        scripted = _standard_deploy_scripted()
        scripted.when(_starts_with("kubectl", "delete", "job"), FakeProc(1, "", "delete failed"))
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                with self.assertRaises(phase6.Phase6Error):
                    _run_quiet(phase6.cmd_reconcile, argparse_namespace(environment=ENVIRONMENT, execution_id="1-1"))

    def test_46_no_runtime_pvc_efs_delete_command_exists(self):
        with open(TOOL_PATH) as f:
            source = f.read()
        for forbidden in ("delete pvc", "delete pv ", "\"pv\"", "delete storageclass", "delete application",
                          "delete namespace", "delete secret ", "delete ingress", "delete statefulset", "delete service"):
            self.assertNotIn(forbidden, source)


class SequentialOrderTests(unittest.TestCase):
    """47-48: two enabled pipelines are reconciled strictly sequentially; a failure on the first prevents the second from ever being rendered/applied."""

    def test_47_two_pipelines_execute_sequentially(self):
        scripted = _standard_deploy_scripted(pipeline_ids=("pipeline-a", "pipeline-b"))
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                _run_quiet(phase6.cmd_reconcile, argparse_namespace(environment=ENVIRONMENT, execution_id="1-1"))
        render_calls = [c["argv"] for c in scripted.calls if _is_actual_render_call(c["argv"])]
        self.assertEqual(len(render_calls), 2)
        self.assertIn("pipeline-a", render_calls[0])
        self.assertIn("pipeline-b", render_calls[1])
        self.assertEqual(len([c for c in scripted.calls if _is_render_job_call(c["argv"])]), 4, "each pipeline now renders twice: once actual, once as the final expected-manifest authority")
        # pipeline-a's own full cleanup (its three delete calls) must precede pipeline-b's ACTUAL render call -- never interleaved/parallel.
        b_render_index = next(i for i, c in enumerate(scripted.calls) if _is_actual_render_call(c["argv"]) and "pipeline-b" in c["argv"])
        delete_indexes_before_b = [i for i, c in enumerate(scripted.calls) if c["argv"][:2] == ["kubectl", "delete"] and i < b_render_index]
        self.assertEqual(len(delete_indexes_before_b), 3, "pipeline-a's full cleanup (3 deletes) must complete before pipeline-b is ever rendered")

    def test_48_collision_failure_on_first_prevents_second(self):
        scripted = _standard_deploy_scripted(pipeline_ids=("pipeline-a", "pipeline-b"))
        scripted.when(_starts_with("kubectl", "get", "job"), FakeProc(0, "job/x found"))
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                with self.assertRaises(phase6.Phase6Error):
                    _run_quiet(phase6.cmd_reconcile, argparse_namespace(environment=ENVIRONMENT, execution_id="1-1"))
        render_calls = [c["argv"] for c in scripted.calls if _is_actual_render_call(c["argv"])]
        self.assertEqual(len(render_calls), 1)
        self.assertIn("pipeline-a", render_calls[0])
        self.assertTrue(all("pipeline-a" in c["argv"] for c in scripted.calls if _is_render_job_call(c["argv"])), "pipeline-b must never be rendered (actual OR expected) once pipeline-a's collision preflight fails")


class ValidateModeTests(unittest.TestCase):
    """49-56: Validate mode remains completely local/read-only -- zero pipelines is a clean no-op, an enabled pipeline renders deterministic dry-run resources, zero AWS/kubectl calls, the SAME structural validator Deploy uses, and a genuine secret-value fixture still fails while the legitimate OGG_DB_PASSWORD JMES selector does not."""

    def test_49_no_pipeline_clean_noop(self):
        scripted = ScriptedRun()
        scripted.when(_is_validate_call, _validate_ok)
        scripted.when(_is_pipelines_call, _pipelines_response([]))
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                result = _run_quiet(phase6.cmd_validate_local, argparse_namespace(environment=ENVIRONMENT))
        self.assertEqual(result, 0)
        self.assertEqual(scripted.calls, [c for c in scripted.calls if _is_validate_call(c["argv"]) or _is_pipelines_call(c["argv"])])

    def test_50_enabled_pipeline_renders_dry_run_resources(self):
        scripted = _standard_deploy_scripted(pipeline_ids=("fabricated-pipeline-001",))
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                result = _run_quiet(phase6.cmd_validate_local, argparse_namespace(environment=ENVIRONMENT))
        self.assertEqual(result, 0)
        render_calls = [c["argv"] for c in scripted.calls if _is_actual_render_call(c["argv"])]
        self.assertEqual(len(render_calls), 1)
        self.assertEqual(len([c for c in scripted.calls if _is_render_job_call(c["argv"])]), 2, "the actual dry-run render plus the final expected-manifest authority render")

    def test_51_dry_run_uses_deterministic_execution_id(self):
        scripted = _standard_deploy_scripted(pipeline_ids=("fabricated-pipeline-001",))
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                _run_quiet(phase6.cmd_validate_local, argparse_namespace(environment=ENVIRONMENT))
        render_call = next(c["argv"] for c in scripted.calls if _is_render_job_call(c["argv"]))
        self.assertEqual(_extract_arg(render_call, "--execution-id"), "dry-run")
        self.assertEqual(phase6.DRY_RUN_EXECUTION_ID, "dry-run")

    def test_52_validate_calls_no_aws(self):
        scripted = _standard_deploy_scripted(pipeline_ids=("fabricated-pipeline-001",))
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                _run_quiet(phase6.cmd_validate_local, argparse_namespace(environment=ENVIRONMENT))
        self.assertEqual([c for c in scripted.calls if c["argv"][:1] == ["aws"]], [])

    def test_53_validate_calls_no_kubectl(self):
        scripted = _standard_deploy_scripted(pipeline_ids=("fabricated-pipeline-001",))
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                _run_quiet(phase6.cmd_validate_local, argparse_namespace(environment=ENVIRONMENT))
        self.assertEqual([c for c in scripted.calls if c["argv"][:1] == ["kubectl"]], [])

    def test_54_same_structural_validation_used_by_deploy(self):
        with open(TOOL_PATH) as f:
            source = f.read()
        self.assertEqual(source.count("_validate_rendered_manifests("), 4, "exactly the definition, its own docstring mention, plus one call site each from cmd_validate_local (Validate) and _reconcile_one_pipeline (Deploy) -- never two independently-drifting validators")
        with mock.patch.object(phase6, "_validate_rendered_manifests", wraps=phase6._validate_rendered_manifests) as spy:
            scripted = _standard_deploy_scripted(pipeline_ids=("fabricated-pipeline-001",))
            with tempfile.TemporaryDirectory() as tmp:
                with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                    _run_quiet(phase6.cmd_validate_local, argparse_namespace(environment=ENVIRONMENT))
            self.assertEqual(spy.call_count, 1)

    def test_55_secret_like_literal_value_fixture_fails(self):
        def _poisoned_render(argv):
            output_dir = _extract_arg(argv, "--output-dir")
            namespace = _extract_arg(argv, "--namespace")
            region = _extract_arg(argv, "--region")
            execution_id = _extract_arg(argv, "--execution-id")
            effective_plan = _plan_for_pipeline_id(PLAN, argv[-1])
            manifests = engine.render_manifests(effective_plan, namespace, region, REAL_ENGINE_SOURCE, execution_id)
            manifests["ConfigMap"]["data"]["password"] = "hunter2-actual-literal-secret-value"
            os.makedirs(output_dir, exist_ok=True)
            for kind, doc in manifests.items():
                with open(os.path.join(output_dir, f"{kind.lower()}.yaml"), "w") as f:
                    yaml.safe_dump(doc, f, sort_keys=False, default_flow_style=False)
            return FakeProc(0, "")

        scripted = ScriptedRun()
        scripted.when(_is_validate_call, _validate_ok)
        scripted.when(_is_pipelines_call, _pipelines_response(["fabricated-pipeline-001"]))
        scripted.when(_is_replication_plan_call, _replication_plan_response(PLAN))
        scripted.when(_is_render_job_call, _poisoned_render)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                with self.assertRaises(phase6.Phase6Error):
                    _run_quiet(phase6.cmd_validate_local, argparse_namespace(environment=ENVIRONMENT))

    def test_56_legitimate_ogg_db_password_jmes_selector_does_not_falsely_fail(self):
        manifests = engine.render_manifests(PLAN, NAMESPACE, AWS_REGION_VALUE, REAL_ENGINE_SOURCE, "dry-run")
        objects_text = manifests["SecretProviderClass"]["spec"]["parameters"]["objects"]
        self.assertIn("OGG_DB_PASSWORD", objects_text)
        with tempfile.TemporaryDirectory() as tmp:
            for kind, doc in manifests.items():
                _dump_yaml(os.path.join(tmp, f"{kind.lower()}.yaml"), doc)
            execution_name = _validate_with_canonical_plan(tmp, execution_id="dry-run")
        self.assertTrue(execution_name)


class WorkflowCredentialScopeTests(unittest.TestCase):
    """57-62: the Phase 6A workflow job's AWS-credential output-scoping and has_pipelines gating contract."""

    @classmethod
    def setUpClass(cls):
        with open(WORKFLOW_PATH) as f:
            doc = yaml.safe_load(f)
        cls.job = doc["jobs"]["replication_reconcile_once"]
        cls.steps = {s.get("name"): s for s in cls.job["steps"]}

    def test_57_configure_aws_credentials_output_credentials_true(self):
        step = self.steps["Configure AWS credentials"]
        self.assertTrue(step["with"]["output-credentials"])

    def test_58_output_env_credentials_false(self):
        step = self.steps["Configure AWS credentials"]
        self.assertFalse(step["with"]["output-env-credentials"])

    def test_59_credential_outputs_passed_only_to_reconcile_step(self):
        reconcile_step = self.steps["Reconcile enabled replication pipelines sequentially"]
        env = reconcile_step.get("env", {})
        self.assertIn("aws_build_credentials", env.get("AWS_ACCESS_KEY_ID", ""))
        for name, step in self.steps.items():
            if name == "Reconcile enabled replication pipelines sequentially":
                continue
            self.assertNotIn("aws_build_credentials", json.dumps(step.get("env", {})))

    def test_60_configure_aws_step_gated_by_has_pipelines_true(self):
        step = self.steps["Configure AWS credentials"]
        self.assertEqual(step.get("if"), "steps.replication_discovery.outputs.has_pipelines == 'true'")

    def test_61_reconciliation_step_gated_by_has_pipelines_true(self):
        step = self.steps["Reconcile enabled replication pipelines sequentially"]
        self.assertEqual(step.get("if"), "steps.replication_discovery.outputs.has_pipelines == 'true'")

    def test_62_no_pipeline_step_gated_by_has_pipelines_false(self):
        step = self.steps["No replication pipeline enabled"]
        self.assertEqual(step.get("if"), "steps.replication_discovery.outputs.has_pipelines == 'false'")


class CollisionErrorClassificationTests(unittest.TestCase):
    """Fix 1 regression: kubectl inspection now uses --ignore-not-found -o name with authoritative command success (run()'s own check=True) -- a Forbidden/Unauthorized/timeout/network/cluster-unreachable/TLS/unknown/empty-error kubectl failure is NEVER interpreted as absence; only a successful command with empty stdout proves absence."""

    def _assert_inspection_error_fails_closed(self, returncode, stderr):
        scripted = ScriptedRun()
        scripted.when(_starts_with("kubectl", "get"), FakeProc(returncode, "", stderr))
        with mock.patch.object(phase6, "run", scripted):
            with self.assertRaises(phase6.Phase6Error):
                phase6._collision_preflight("gg-repl-x-abc12345-1-1", NAMESPACE)

    def test_63_confirmed_reproduction_forbidden_error_now_fails_closed(self):
        """Confirmed reproduction: `kubectl get secretproviderclass ...` returncode=1, stderr="Error from server (Forbidden): secretproviderclasses.secrets-store.csi.x-k8s.io is forbidden" previously returned SUCCESS (interpreted as absence) from the retired _collision_preflight() -- now fails closed."""
        self._assert_inspection_error_fails_closed(1, "Error from server (Forbidden): secretproviderclasses.secrets-store.csi.x-k8s.io is forbidden")

    def test_64_unauthorized_fails_closed(self):
        self._assert_inspection_error_fails_closed(1, "error: You must be logged in to the server (Unauthorized)")

    def test_65_context_deadline_exceeded_timeout_fails_closed(self):
        self._assert_inspection_error_fails_closed(1, "Unable to connect to the server: context deadline exceeded")

    def test_66_cluster_unreachable_connection_refused_fails_closed(self):
        self._assert_inspection_error_fails_closed(1, "The connection to the server 127.0.0.1:6443 was refused")

    def test_67_tls_network_failure_fails_closed(self):
        self._assert_inspection_error_fails_closed(1, "Unable to connect to the server: x509: certificate signed by unknown authority")

    def test_68_unknown_error_fails_closed(self):
        self._assert_inspection_error_fails_closed(17, "some completely unrecognized error text")

    def test_69_nonzero_with_empty_stdout_and_stderr_fails_closed(self):
        self._assert_inspection_error_fails_closed(1, "")

    def test_70_ignore_not_found_absent_path_returns_rc0_empty_is_safe(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("kubectl", "get"), FakeProc(0, "", ""))
        with mock.patch.object(phase6, "run", scripted):
            phase6._collision_preflight("gg-repl-x-abc12345-1-1", NAMESPACE)  # must not raise

    def test_71_every_collision_get_command_uses_ignore_not_found_and_o_name(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("kubectl", "get"), FakeProc(0, "", ""))
        with mock.patch.object(phase6, "run", scripted):
            phase6._collision_preflight("gg-repl-x-abc12345-1-1", NAMESPACE)
        get_calls = [c["argv"] for c in scripted.calls if c["argv"][:2] == ["kubectl", "get"]]
        self.assertEqual(len(get_calls), 3)
        for call in get_calls:
            self.assertIn("--ignore-not-found", call)
            self.assertIn("-o", call)
            self.assertIn("name", call)

    def test_72_inspection_error_on_first_resource_zero_subsequent_kubectl_mutation(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("kubectl", "get"), FakeProc(0, "", ""))
        scripted.when(_starts_with("kubectl", "get", "secretproviderclass"), FakeProc(1, "", "Error from server (Forbidden)"))
        with mock.patch.object(phase6, "run", scripted):
            with self.assertRaises(phase6.Phase6Error):
                phase6._collision_preflight("gg-repl-x-abc12345-1-1", NAMESPACE)
        self.assertEqual([c for c in scripted.calls if c["argv"][:2] in (["kubectl", "apply"], ["kubectl", "wait"], ["kubectl", "delete"])], [])
        get_calls = [c["argv"] for c in scripted.calls if c["argv"][:2] == ["kubectl", "get"]]
        self.assertEqual(len(get_calls), 1, "ConfigMap/Job inspection must never even start once the first (SecretProviderClass) inspection has already failed closed")

    def test_73_inspection_failure_inside_full_cmd_reconcile_zero_apply_calls(self):
        scripted = _standard_deploy_scripted(pipeline_ids=("fabricated-pipeline-001",))
        scripted.when(_starts_with("kubectl", "get", "secretproviderclass"), FakeProc(1, "", "Error from server (Forbidden)"))
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                with self.assertRaises(phase6.Phase6Error):
                    _run_quiet(phase6.cmd_reconcile, argparse_namespace(environment=ENVIRONMENT, execution_id="1-1"))
        self.assertEqual([c for c in scripted.calls if c["argv"][:2] == ["kubectl", "apply"]], [])


class CanonicalPlanBindingTests(unittest.TestCase):
    """Fix 2 regression: ConfigMap.data['plan.json'] must exactly equal the CURRENT canonical replication plan obtained via `automation/goldengate-deployment-model.py replication-plan` -- any drift (pipelineId, secret names, image, ServiceAccount, process configuration) fails, never normalized away."""

    def _validate_against_canonical(self, tmp, canonical_plan, rendered_plan=None):
        rendered_plan = rendered_plan if rendered_plan is not None else canonical_plan
        _write_manifests(plan=rendered_plan, tmp_dir=tmp, execution_id="test-exec-1")
        expected = _expected_manifests_fixture(plan=canonical_plan, execution_id="test-exec-1")
        with mock.patch.object(phase6, "_load_canonical_replication_plan", return_value=canonical_plan), \
             mock.patch.object(phase6, "_render_expected_manifests_for_validation", return_value=expected):
            return phase6._validate_rendered_manifests(tmp, ENVIRONMENT, canonical_plan["pipelineId"], "test-exec-1", NAMESPACE, AWS_REGION_VALUE)

    def test_74_valid_configmap_plan_json_equals_canonical_plan_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._validate_against_canonical(tmp, PLAN)

    def test_75_plan_pipeline_id_mismatch_fails(self):
        drifted = json.loads(json.dumps(PLAN))
        drifted["pipelineId"] = "different-pipeline-id"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._validate_against_canonical(tmp, PLAN, rendered_plan=drifted)

    def test_76_source_admin_secret_mismatch_fails(self):
        drifted = json.loads(json.dumps(PLAN))
        drifted["source"]["adminSecret"] = "dev/goldengate/UNRELATED/admin"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._validate_against_canonical(tmp, PLAN, rendered_plan=drifted)

    def test_77_target_admin_secret_mismatch_fails(self):
        drifted = json.loads(json.dumps(PLAN))
        drifted["target"]["adminSecret"] = "dev/goldengate/UNRELATED/admin"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._validate_against_canonical(tmp, PLAN, rendered_plan=drifted)

    def test_78_source_db_secret_mismatch_fails(self):
        drifted = json.loads(json.dumps(PLAN))
        drifted["source"]["databaseSecret"] = "dev/goldengate/UNRELATED/source-db"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._validate_against_canonical(tmp, PLAN, rendered_plan=drifted)

    def test_79_target_db_secret_mismatch_fails(self):
        drifted = json.loads(json.dumps(PLAN))
        drifted["target"]["databaseSecret"] = "dev/goldengate/UNRELATED/target-db"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._validate_against_canonical(tmp, PLAN, rendered_plan=drifted)

    def test_80_tls_secret_mismatch_fails(self):
        drifted = json.loads(json.dumps(PLAN))
        drifted["tlsSecret"] = "dev/goldengate/UNRELATED/tls"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._validate_against_canonical(tmp, PLAN, rendered_plan=drifted)

    def test_81_source_image_mismatch_fails(self):
        drifted = json.loads(json.dumps(PLAN))
        drifted["source"]["image"] = "229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-postgresql:99.99.99"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._validate_against_canonical(tmp, PLAN, rendered_plan=drifted)

    def test_82_source_service_account_mismatch_fails(self):
        drifted = json.loads(json.dumps(PLAN))
        drifted["source"]["serviceAccount"] = "some-other-sa"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._validate_against_canonical(tmp, PLAN, rendered_plan=drifted)

    def test_83_extract_configuration_mismatch_fails(self):
        drifted = json.loads(json.dumps(PLAN))
        drifted["extract"]["name"] = "DIFFERENT_EXTRACT_NAME"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._validate_against_canonical(tmp, PLAN, rendered_plan=drifted)


class ConfigMapContractTests(unittest.TestCase):
    """Fix 2 regression: ConfigMap.data key set must be exactly {goldengate-replication.py, plan.json}; the embedded reconciler program must exactly equal the CURRENT automation/goldengate-replication.py; a replaced/malicious reconciler program must fail before kubectl apply."""

    def _render_and_get_path(self, tmp, plan=None):
        plan = plan or PLAN
        _write_manifests(plan=plan, tmp_dir=tmp, execution_id="test-exec-1")
        return os.path.join(tmp, "configmap.yaml")

    def _validate(self, tmp, plan=None):
        plan = plan or PLAN
        expected = _expected_manifests_fixture(plan=plan, execution_id="test-exec-1")
        with mock.patch.object(phase6, "_load_canonical_replication_plan", return_value=plan), \
             mock.patch.object(phase6, "_render_expected_manifests_for_validation", return_value=expected):
            return phase6._validate_rendered_manifests(tmp, ENVIRONMENT, plan["pipelineId"], "test-exec-1", NAMESPACE, AWS_REGION_VALUE)

    def test_84_exact_two_configmap_keys_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._render_and_get_path(tmp)
            self._validate(tmp)

    def test_85_confirmed_reproduction_extra_configmap_data_key_leaked_fails(self):
        """Confirmed reproduction: ConfigMap.data gaining a "leaked" key with a literal secret value previously returned SUCCESS -- now fails via the exact-key-set contract regardless of the key's name."""
        with tempfile.TemporaryDirectory() as tmp:
            configmap_path = self._render_and_get_path(tmp)
            configmap = _load_yaml(configmap_path)
            configmap["data"]["leaked"] = "hunter2-actual-secret-value"
            _dump_yaml(configmap_path, configmap)
            with self.assertRaises(phase6.Phase6Error):
                self._validate(tmp)

    def test_86_missing_reconciler_source_key_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            configmap_path = self._render_and_get_path(tmp)
            configmap = _load_yaml(configmap_path)
            del configmap["data"]["goldengate-replication.py"]
            _dump_yaml(configmap_path, configmap)
            with self.assertRaises(phase6.Phase6Error):
                self._validate(tmp)

    def test_87_missing_plan_json_key_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            configmap_path = self._render_and_get_path(tmp)
            configmap = _load_yaml(configmap_path)
            del configmap["data"]["plan.json"]
            _dump_yaml(configmap_path, configmap)
            with self.assertRaises(phase6.Phase6Error):
                self._validate(tmp)

    def test_88_confirmed_reproduction_replaced_reconciler_source_fails(self):
        """Confirmed reproduction: ConfigMap.data['goldengate-replication.py'] replaced with print("malicious replacement") previously returned SUCCESS -- now fails via exact byte/text equivalence to the CURRENT trusted engine source."""
        with tempfile.TemporaryDirectory() as tmp:
            configmap_path = self._render_and_get_path(tmp)
            configmap = _load_yaml(configmap_path)
            configmap["data"]["goldengate-replication.py"] = 'print("malicious replacement")\n'
            _dump_yaml(configmap_path, configmap)
            with self.assertRaises(phase6.Phase6Error):
                self._validate(tmp)

    def test_89_reconciler_source_must_exactly_equal_current_engine_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            configmap_path = self._render_and_get_path(tmp)
            configmap = _load_yaml(configmap_path)
            self.assertEqual(configmap["data"]["goldengate-replication.py"], REAL_ENGINE_SOURCE)


class SecretProviderClassContractTests(unittest.TestCase):
    """Fix 2 regression: the replication SecretProviderClass is file-mount-only (spec.secretObjects entirely forbidden) and its object/alias list is bound EXACTLY to the canonical plan -- never read from the SPC being validated."""

    def _render_and_validate(self, tmp, mutate_spc=None, plan=None):
        plan = plan or PLAN
        _write_manifests(plan=plan, tmp_dir=tmp, execution_id="test-exec-1")
        if mutate_spc:
            spc_path = os.path.join(tmp, "secretproviderclass.yaml")
            spc = _load_yaml(spc_path)
            mutate_spc(spc)
            _dump_yaml(spc_path, spc)
        expected = _expected_manifests_fixture(plan=plan, execution_id="test-exec-1")
        with mock.patch.object(phase6, "_load_canonical_replication_plan", return_value=plan), \
             mock.patch.object(phase6, "_render_expected_manifests_for_validation", return_value=expected):
            return phase6._validate_rendered_manifests(tmp, ENVIRONMENT, plan["pipelineId"], "test-exec-1", NAMESPACE, AWS_REGION_VALUE)

    @staticmethod
    def _mutate_objects(spc, fn):
        objects = yaml.safe_load(spc["spec"]["parameters"]["objects"])
        fn(objects)
        spc["spec"]["parameters"]["objects"] = yaml.safe_dump(objects, sort_keys=False, default_flow_style=False)

    def test_90_correct_canonical_objects_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._render_and_validate(tmp)

    def test_91_confirmed_reproduction_secretobjects_added_fails(self):
        """Confirmed reproduction: spec.secretObjects (a Kubernetes-Secret-sync directive) added to an otherwise-valid SPC previously returned SUCCESS -- now forbidden entirely."""
        def _mutate(spc):
            spc["spec"]["secretObjects"] = [{"secretName": "replication-creds", "type": "Opaque",
                                             "data": [{"objectName": "source-db-password", "key": "password"}]}]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._render_and_validate(tmp, mutate_spc=_mutate)

    def test_92_secretobjects_empty_list_still_fails(self):
        def _mutate(spc):
            spc["spec"]["secretObjects"] = []
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._render_and_validate(tmp, mutate_spc=_mutate)

    def test_93_secretobjects_null_still_fails(self):
        def _mutate(spc):
            spc["spec"]["secretObjects"] = None
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._render_and_validate(tmp, mutate_spc=_mutate)

    def test_94_confirmed_reproduction_unrelated_source_admin_objectname_fails(self):
        """Confirmed reproduction: the source-admin SecretProviderClass objectName changed from the canonical replication plan value to dev/goldengate/UNRELATED/admin previously returned SUCCESS -- now bound exactly to the canonical plan."""
        def _mutate(spc):
            self._mutate_objects(spc, lambda objs: objs.__setitem__(0, {**objs[0], "objectName": "dev/goldengate/UNRELATED/admin"}))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._render_and_validate(tmp, mutate_spc=_mutate)

    def test_95_wrong_target_admin_secret_fails(self):
        def _mutate(spc):
            self._mutate_objects(spc, lambda objs: objs.__setitem__(1, {**objs[1], "objectName": "dev/goldengate/UNRELATED/target-admin"}))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._render_and_validate(tmp, mutate_spc=_mutate)

    def test_96_wrong_source_db_secret_fails(self):
        def _mutate(spc):
            self._mutate_objects(spc, lambda objs: objs.__setitem__(2, {**objs[2], "objectName": "dev/goldengate/UNRELATED/source-db"}))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._render_and_validate(tmp, mutate_spc=_mutate)

    def test_97_wrong_target_db_secret_fails(self):
        def _mutate(spc):
            self._mutate_objects(spc, lambda objs: objs.__setitem__(3, {**objs[3], "objectName": "dev/goldengate/UNRELATED/target-db"}))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._render_and_validate(tmp, mutate_spc=_mutate)

    def test_98_wrong_tls_secret_fails(self):
        def _mutate(spc):
            self._mutate_objects(spc, lambda objs: objs.__setitem__(4, {**objs[4], "objectName": "dev/goldengate/UNRELATED/tls"}))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._render_and_validate(tmp, mutate_spc=_mutate)

    def test_99_extra_spc_object_fails(self):
        def _mutate(spc):
            self._mutate_objects(spc, lambda objs: objs.append({"objectName": "dev/goldengate/UNRELATED/extra", "objectType": "secretsmanager",
                                                                 "jmesPath": [{"path": "X", "objectAlias": "extra-alias"}]}))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._render_and_validate(tmp, mutate_spc=_mutate)

    def test_100_missing_spc_object_fails(self):
        def _mutate(spc):
            self._mutate_objects(spc, lambda objs: objs.pop())
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._render_and_validate(tmp, mutate_spc=_mutate)

    def test_101_wrong_jmes_alias_fails(self):
        def _mutate(spc):
            self._mutate_objects(spc, lambda objs: objs[0]["jmesPath"][0].__setitem__("objectAlias", "wrong-alias"))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._render_and_validate(tmp, mutate_spc=_mutate)

    def test_102_wrong_object_type_fails(self):
        def _mutate(spc):
            self._mutate_objects(spc, lambda objs: objs[0].__setitem__("objectType", "someOtherType"))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._render_and_validate(tmp, mutate_spc=_mutate)

    def test_103_wrong_region_fails(self):
        def _mutate(spc):
            spc["spec"]["parameters"]["region"] = "us-east-1"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._render_and_validate(tmp, mutate_spc=_mutate)

    def test_104_duplicate_embedded_yaml_key_fails(self):
        def _mutate(spc):
            spc["spec"]["parameters"]["objects"] = "- objectName: dup\n  objectType: secretsmanager\n  objectType: secretsmanager\n"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._render_and_validate(tmp, mutate_spc=_mutate)


class JobExactContractTests(unittest.TestCase):
    """Fix 2 regression: the Job must run the EXACT fixed worker command, forbid env/envFrom entirely (credentials remain mounted files only), and mount exactly the two expected read-only volumes referencing the execution ConfigMap/SecretProviderClass."""

    def _render_and_validate(self, tmp, mutate_job=None, plan=None):
        plan = plan or PLAN
        _write_manifests(plan=plan, tmp_dir=tmp, execution_id="test-exec-1")
        if mutate_job:
            job_path = os.path.join(tmp, "job.yaml")
            job = _load_yaml(job_path)
            mutate_job(job)
            _dump_yaml(job_path, job)
        expected = _expected_manifests_fixture(plan=plan, execution_id="test-exec-1")
        with mock.patch.object(phase6, "_load_canonical_replication_plan", return_value=plan), \
             mock.patch.object(phase6, "_render_expected_manifests_for_validation", return_value=expected):
            return phase6._validate_rendered_manifests(tmp, ENVIRONMENT, plan["pipelineId"], "test-exec-1", NAMESPACE, AWS_REGION_VALUE)

    def test_105_canonical_command_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._render_and_validate(tmp)

    def test_106_changed_command_fails(self):
        def _mutate(job):
            job["spec"]["template"]["spec"]["containers"][0]["command"] = ["python3", "-c", "print('hi')"]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._render_and_validate(tmp, mutate_job=_mutate)

    def test_107_added_nonempty_args_fails(self):
        def _mutate(job):
            job["spec"]["template"]["spec"]["containers"][0]["args"] = ["--verbose"]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._render_and_validate(tmp, mutate_job=_mutate)

    def test_108_an_added_empty_args_field_now_fails_the_final_expected_manifest_equality(self):
        """The specific 'container.args must be absent or empty' business check tolerates an empty list on its own, but the current trusted engine's render never adds an args key at all -- the FINAL expected-manifest equality check (the authoritative proof introduced by this task) is stricter than any single field check and rejects this deviation too, exactly as intended: the trusted engine, not a per-field allowlist, decides the complete expected manifest."""
        def _mutate(job):
            job["spec"]["template"]["spec"]["containers"][0]["args"] = []
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._render_and_validate(tmp, mutate_job=_mutate)

    def test_109_confirmed_reproduction_literal_env_db_password_fails(self):
        """Confirmed reproduction: a Job container env entry {name: DB_PASSWORD, value: hunter2-actual-secret} previously returned SUCCESS (its mapping keys are name/value, not "password") -- now env is forbidden entirely, regardless of key naming."""
        def _mutate(job):
            job["spec"]["template"]["spec"]["containers"][0]["env"] = [{"name": "DB_PASSWORD", "value": "hunter2-actual-secret"}]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._render_and_validate(tmp, mutate_job=_mutate)

    def test_110_envfrom_fails(self):
        def _mutate(job):
            job["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [{"secretRef": {"name": "some-secret"}}]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._render_and_validate(tmp, mutate_job=_mutate)

    def test_111_wrong_reconciler_mount_path_fails(self):
        def _mutate(job):
            for vm in job["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]:
                if vm["name"] == "reconciler-script":
                    vm["mountPath"] = "/some/other/path"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._render_and_validate(tmp, mutate_job=_mutate)

    def test_112_wrong_secret_mount_path_fails(self):
        def _mutate(job):
            for vm in job["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]:
                if vm["name"] == "replication-secrets":
                    vm["mountPath"] = "/some/other/secret/path"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._render_and_validate(tmp, mutate_job=_mutate)

    def test_113_extra_volumemount_fails(self):
        def _mutate(job):
            job["spec"]["template"]["spec"]["containers"][0]["volumeMounts"].append({"name": "extra-mount", "mountPath": "/mnt/extra", "readOnly": True})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._render_and_validate(tmp, mutate_job=_mutate)

    def test_114_wrong_configmap_volume_reference_fails(self):
        def _mutate(job):
            for v in job["spec"]["template"]["spec"]["volumes"]:
                if v["name"] == "reconciler-script":
                    v["configMap"]["name"] = "some-other-configmap"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._render_and_validate(tmp, mutate_job=_mutate)

    def test_115_wrong_csi_driver_fails(self):
        def _mutate(job):
            for v in job["spec"]["template"]["spec"]["volumes"]:
                if v["name"] == "replication-secrets":
                    v["csi"]["driver"] = "some.other.csi.driver"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._render_and_validate(tmp, mutate_job=_mutate)

    def test_116_extra_volume_fails(self):
        def _mutate(job):
            job["spec"]["template"]["spec"]["volumes"].append({"name": "extra-volume", "emptyDir": {}})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(phase6.Phase6Error):
                self._render_and_validate(tmp, mutate_job=_mutate)

    def test_117_canonical_job_remains_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            execution_name = self._render_and_validate(tmp)
            self.assertTrue(execution_name)


class DeployValidateParityTests(unittest.TestCase):
    """53-56: Deploy and Validate still use the SAME manifest validator; Validate remains zero aws/kubectl; a local manifest validation failure occurs before collision preflight and performs zero kubectl apply."""

    @staticmethod
    def _poisoned_render_with_leaked_key(argv):
        output_dir = _extract_arg(argv, "--output-dir")
        namespace = _extract_arg(argv, "--namespace")
        region = _extract_arg(argv, "--region")
        execution_id = _extract_arg(argv, "--execution-id")
        effective_plan = _plan_for_pipeline_id(PLAN, argv[-1])
        manifests = engine.render_manifests(effective_plan, namespace, region, REAL_ENGINE_SOURCE, execution_id)
        manifests["ConfigMap"]["data"]["leaked"] = "hunter2-actual-secret-value"
        os.makedirs(output_dir, exist_ok=True)
        for kind, doc in manifests.items():
            with open(os.path.join(output_dir, f"{kind.lower()}.yaml"), "w") as f:
                yaml.safe_dump(doc, f, sort_keys=False, default_flow_style=False)
        return FakeProc(0, "")

    def _poisoned_scripted(self):
        scripted = ScriptedRun()
        scripted.when(_is_validate_call, _validate_ok)
        scripted.when(_is_pipelines_call, _pipelines_response([PLAN["pipelineId"]]))
        scripted.when(_is_replication_plan_call, _replication_plan_response(PLAN))
        scripted.when(_is_render_job_call, self._poisoned_render_with_leaked_key)
        scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
        scripted.when(_starts_with("kubectl", "get"), FakeProc(0, "", ""))
        return scripted

    def test_118_deploy_and_validate_use_the_same_validator(self):
        import ast as _ast
        with open(TOOL_PATH) as f:
            source = f.read()
        tree = _ast.parse(source)
        reconcile_one = next(n for n in _ast.walk(tree) if isinstance(n, _ast.FunctionDef) and n.name == "_reconcile_one_pipeline")
        validate_local = next(n for n in _ast.walk(tree) if isinstance(n, _ast.FunctionDef) and n.name == "cmd_validate_local")
        self.assertIn("_validate_rendered_manifests(", _ast.get_source_segment(source, reconcile_one))
        self.assertIn("_validate_rendered_manifests(", _ast.get_source_segment(source, validate_local))

    def test_119_validate_remains_zero_aws_kubectl(self):
        scripted = _standard_deploy_scripted(pipeline_ids=("fabricated-pipeline-001",))
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                _run_quiet(phase6.cmd_validate_local, argparse_namespace(environment=ENVIRONMENT))
        self.assertEqual([c for c in scripted.calls if c["argv"][:1] in (["aws"], ["kubectl"])], [])

    def test_120_local_manifest_validation_failure_occurs_before_collision_preflight(self):
        scripted = self._poisoned_scripted()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                with self.assertRaises(phase6.Phase6Error):
                    _run_quiet(phase6.cmd_reconcile, argparse_namespace(environment=ENVIRONMENT, execution_id="1-1"))
        self.assertEqual([c for c in scripted.calls if c["argv"][:2] == ["kubectl", "get"]], [], "collision preflight must never run once local manifest validation already failed")

    def test_121_local_manifest_validation_failure_performs_zero_kubectl_apply(self):
        scripted = self._poisoned_scripted()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                with self.assertRaises(phase6.Phase6Error):
                    _run_quiet(phase6.cmd_reconcile, argparse_namespace(environment=ENVIRONMENT, execution_id="1-1"))
        self.assertEqual([c for c in scripted.calls if c["argv"][:2] == ["kubectl", "apply"]], [])


class ExpectedManifestAuthorityTests(unittest.TestCase):
    """The FINAL authoritative proof this task adds: actual rendered manifests must be exactly semantically identical (parsed-dict equality, never a selected-field/subset comparison) to a FRESH re-render from the current trusted engine for the SAME environment/pipeline_id/execution_id/namespace/region -- covers execution resource naming, plan-checksum annotation, ttlSecondsAfterFinished, labels, and any future engine-owned field, without Phase 6 ever manually reconstructing job_resource_name()/plan_checksum()."""

    def _missing_expected_file_scripted(self, missing_kind):
        def _fn(argv):
            output_dir = _extract_arg(argv, "--output-dir")
            if "phase6-expected-render-" in output_dir:
                os.makedirs(output_dir, exist_ok=True)
                effective_plan = _plan_for_pipeline_id(PLAN, argv[-1])
                manifests = engine.render_manifests(effective_plan, _extract_arg(argv, "--namespace"), _extract_arg(argv, "--region"), REAL_ENGINE_SOURCE, _extract_arg(argv, "--execution-id"))
                for kind, doc in manifests.items():
                    if kind.lower() == missing_kind:
                        continue
                    with open(os.path.join(output_dir, f"{kind.lower()}.yaml"), "w") as f:
                        yaml.safe_dump(doc, f, sort_keys=False, default_flow_style=False)
                return FakeProc(0, "")
            return _render_job_side_effect(PLAN)(argv)

        scripted = ScriptedRun()
        scripted.when(_is_validate_call, _validate_ok)
        scripted.when(_is_pipelines_call, _pipelines_response(["fabricated-pipeline-001"]))
        scripted.when(_is_replication_plan_call, _replication_plan_response(PLAN))
        scripted.when(_is_render_job_call, _fn)
        return scripted

    def test_122_actual_equals_fresh_expected_render_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp, execution_id="test-exec-1")
            execution_name = _validate_with_canonical_plan(tmp, execution_id="test-exec-1")
        self.assertTrue(execution_name)

    def test_123_confirmed_reproduction_arbitrary_shared_execution_name_fails(self):
        """Confirmed reproduction: all three actual resource names consistently renamed to "totally-unrelated-exec" (with the Job's own ConfigMap/SecretProviderClass volume references updated to match) previously returned SUCCESS from the retired name-only-shared-with-itself check -- now fails because that name no longer matches the engine-authoritative expected render."""
        fake_name = "totally-unrelated-exec"
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp, execution_id="test-exec-1")
            for kind in ("secretproviderclass", "configmap", "job"):
                path = os.path.join(tmp, f"{kind}.yaml")
                doc = _load_yaml(path)
                doc["metadata"]["name"] = fake_name
                _dump_yaml(path, doc)
            job_path = os.path.join(tmp, "job.yaml")
            job = _load_yaml(job_path)
            for v in job["spec"]["template"]["spec"]["volumes"]:
                if v["name"] == "reconciler-script":
                    v["configMap"]["name"] = fake_name
                if v["name"] == "replication-secrets":
                    v["csi"]["volumeAttributes"]["secretProviderClass"] = fake_name
            _dump_yaml(job_path, job)
            with self.assertRaises(phase6.Phase6Error):
                _validate_with_canonical_plan(tmp, execution_id="test-exec-1")

    def test_124_arbitrary_shared_execution_name_zero_kubectl_through_full_reconcile(self):
        fake_name = "totally-unrelated-exec"

        def _renamed_render(argv):
            result = _render_job_side_effect(PLAN)(argv)
            output_dir = _extract_arg(argv, "--output-dir")
            if "phase6-expected-render-" not in output_dir:
                for kind in ("secretproviderclass", "configmap", "job"):
                    path = os.path.join(output_dir, f"{kind}.yaml")
                    doc = _load_yaml(path)
                    doc["metadata"]["name"] = fake_name
                    _dump_yaml(path, doc)
                job_path = os.path.join(output_dir, "job.yaml")
                job = _load_yaml(job_path)
                for v in job["spec"]["template"]["spec"]["volumes"]:
                    if v["name"] == "reconciler-script":
                        v["configMap"]["name"] = fake_name
                    if v["name"] == "replication-secrets":
                        v["csi"]["volumeAttributes"]["secretProviderClass"] = fake_name
                _dump_yaml(job_path, job)
            return result

        scripted = _standard_deploy_scripted(pipeline_ids=("fabricated-pipeline-001",))
        scripted.when(_is_render_job_call, _renamed_render)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                with self.assertRaises(phase6.Phase6Error):
                    _run_quiet(phase6.cmd_reconcile, argparse_namespace(environment=ENVIRONMENT, execution_id="1-1"))
        self.assertEqual([c for c in scripted.calls if c["argv"][:1] == ["kubectl"]], [])

    def test_125_different_execution_id_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp, execution_id="111-1")
            with self.assertRaises(phase6.Phase6Error):
                _validate_with_canonical_plan(tmp, execution_id="222-1")

    def test_126_different_run_attempt_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp, execution_id="12345-1")
            with self.assertRaises(phase6.Phase6Error):
                _validate_with_canonical_plan(tmp, execution_id="12345-2")

    def test_127_dry_run_manifest_with_live_run_name_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp, execution_id=phase6.DRY_RUN_EXECUTION_ID)
            with self.assertRaises(phase6.Phase6Error):
                _validate_with_canonical_plan(tmp, execution_id="12345-1")

    def test_128_live_run_manifest_with_dry_run_name_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp, execution_id="12345-1")
            with self.assertRaises(phase6.Phase6Error):
                _validate_with_canonical_plan(tmp, execution_id=phase6.DRY_RUN_EXECUTION_ID)

    def test_129_confirmed_reproduction_ttl_seconds_after_finished_drift_fails(self):
        """Confirmed reproduction: Job spec.ttlSecondsAfterFinished changed to 0 previously returned SUCCESS -- now fails via the final expected-manifest equality."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp, execution_id="test-exec-1")
            job_path = os.path.join(tmp, "job.yaml")
            job = _load_yaml(job_path)
            job["spec"]["ttlSecondsAfterFinished"] = 0
            _dump_yaml(job_path, job)
            with self.assertRaises(phase6.Phase6Error):
                _validate_with_canonical_plan(tmp, execution_id="test-exec-1")

    def test_130_confirmed_reproduction_plan_checksum_annotation_drift_fails(self):
        """Confirmed reproduction: Job metadata.annotations["goldengate.adcb/plan-checksum"] changed to "deadbeef" previously returned SUCCESS -- now fails via the final expected-manifest equality."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp, execution_id="test-exec-1")
            job_path = os.path.join(tmp, "job.yaml")
            job = _load_yaml(job_path)
            job["metadata"]["annotations"]["goldengate.adcb/plan-checksum"] = "deadbeef"
            _dump_yaml(job_path, job)
            with self.assertRaises(phase6.Phase6Error):
                _validate_with_canonical_plan(tmp, execution_id="test-exec-1")

    def test_131_job_metadata_labels_drift_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp, execution_id="test-exec-1")
            job_path = os.path.join(tmp, "job.yaml")
            job = _load_yaml(job_path)
            job["metadata"]["labels"]["app.kubernetes.io/component"] = "something-else"
            _dump_yaml(job_path, job)
            with self.assertRaises(phase6.Phase6Error):
                _validate_with_canonical_plan(tmp, execution_id="test-exec-1")

    def test_132_job_pod_template_labels_drift_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp, execution_id="test-exec-1")
            job_path = os.path.join(tmp, "job.yaml")
            job = _load_yaml(job_path)
            job["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/component"] = "something-else"
            _dump_yaml(job_path, job)
            with self.assertRaises(phase6.Phase6Error):
                _validate_with_canonical_plan(tmp, execution_id="test-exec-1")

    def test_133_extra_job_metadata_annotation_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp, execution_id="test-exec-1")
            job_path = os.path.join(tmp, "job.yaml")
            job = _load_yaml(job_path)
            job["metadata"]["annotations"]["harmless-looking/note"] = "just a note"
            _dump_yaml(job_path, job)
            with self.assertRaises(phase6.Phase6Error):
                _validate_with_canonical_plan(tmp, execution_id="test-exec-1")

    def test_134_extra_configmap_metadata_label_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp, execution_id="test-exec-1")
            configmap_path = os.path.join(tmp, "configmap.yaml")
            configmap = _load_yaml(configmap_path)
            configmap["metadata"].setdefault("labels", {})["harmless-looking-label"] = "value"
            _dump_yaml(configmap_path, configmap)
            with self.assertRaises(phase6.Phase6Error):
                _validate_with_canonical_plan(tmp, execution_id="test-exec-1")

    def test_135_extra_spc_metadata_label_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_manifests(tmp_dir=tmp, execution_id="test-exec-1")
            spc_path = os.path.join(tmp, "secretproviderclass.yaml")
            spc = _load_yaml(spc_path)
            spc["metadata"].setdefault("labels", {})["harmless-looking-label"] = "value"
            _dump_yaml(spc_path, spc)
            with self.assertRaises(phase6.Phase6Error):
                _validate_with_canonical_plan(tmp, execution_id="test-exec-1")

    def test_136_future_engine_owned_field_automatically_enforced_without_phase6_change(self):
        """Demonstrates that Phase 6 never needs a manually-added field-specific check for a NEW engine-owned field -- a fabricated 'expected' render carrying an extra hypothetical future field the actual manifest lacks is enough, on its own, to fail the final equality check."""
        with tempfile.TemporaryDirectory() as tmp:
            manifests = _write_manifests(tmp_dir=tmp, execution_id="test-exec-1")
            fabricated_expected = {kind: json.loads(json.dumps(doc)) for kind, doc in manifests.items()}
            fabricated_expected["Job"]["metadata"].setdefault("labels", {})["future.engine.owned/field"] = "some-future-value"
            with mock.patch.object(phase6, "_load_canonical_replication_plan", return_value=PLAN), \
                 mock.patch.object(phase6, "_render_expected_manifests_for_validation", return_value=fabricated_expected):
                with self.assertRaises(phase6.Phase6Error):
                    phase6._validate_rendered_manifests(tmp, ENVIRONMENT, PLAN["pipelineId"], "test-exec-1", NAMESPACE, AWS_REGION_VALUE)

    def test_137_expected_render_subprocess_failure_fails_closed_zero_kubectl(self):
        def _fail_expected_render(argv):
            output_dir = _extract_arg(argv, "--output-dir")
            if "phase6-expected-render-" in output_dir:
                return FakeProc(1, "", "engine render-job failed")
            return _render_job_side_effect(PLAN)(argv)

        scripted = _standard_deploy_scripted(pipeline_ids=("fabricated-pipeline-001",))
        scripted.when(_is_render_job_call, _fail_expected_render)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                with self.assertRaises(phase6.Phase6Error):
                    _run_quiet(phase6.cmd_reconcile, argparse_namespace(environment=ENVIRONMENT, execution_id="1-1"))
        self.assertEqual([c for c in scripted.calls if c["argv"][:1] == ["kubectl"]], [])

    def test_138_expected_render_malformed_yaml_fails(self):
        def _malformed_expected_render(argv):
            output_dir = _extract_arg(argv, "--output-dir")
            if "phase6-expected-render-" in output_dir:
                os.makedirs(output_dir, exist_ok=True)
                for kind in ("secretproviderclass", "configmap", "job"):
                    with open(os.path.join(output_dir, f"{kind}.yaml"), "w") as f:
                        f.write("kind: X\nkind: X\n")
                return FakeProc(0, "")
            return _render_job_side_effect(PLAN)(argv)

        scripted = ScriptedRun()
        scripted.when(_is_validate_call, _validate_ok)
        scripted.when(_is_pipelines_call, _pipelines_response(["fabricated-pipeline-001"]))
        scripted.when(_is_replication_plan_call, _replication_plan_response(PLAN))
        scripted.when(_is_render_job_call, _malformed_expected_render)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                with self.assertRaises(phase6.Phase6Error):
                    _run_quiet(phase6.cmd_validate_local, argparse_namespace(environment=ENVIRONMENT))

    def test_139_expected_render_missing_spc_fails(self):
        scripted = self._missing_expected_file_scripted("secretproviderclass")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                with self.assertRaises(phase6.Phase6Error):
                    _run_quiet(phase6.cmd_validate_local, argparse_namespace(environment=ENVIRONMENT))

    def test_140_expected_render_missing_configmap_fails(self):
        scripted = self._missing_expected_file_scripted("configmap")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                with self.assertRaises(phase6.Phase6Error):
                    _run_quiet(phase6.cmd_validate_local, argparse_namespace(environment=ENVIRONMENT))

    def test_141_expected_render_missing_job_fails(self):
        scripted = self._missing_expected_file_scripted("job")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                with self.assertRaises(phase6.Phase6Error):
                    _run_quiet(phase6.cmd_validate_local, argparse_namespace(environment=ENVIRONMENT))

    def test_142_deploy_and_validate_share_the_same_final_validator_with_expected_render(self):
        import ast as _ast
        with open(TOOL_PATH) as f:
            source = f.read()
        tree = _ast.parse(source)
        validate_fn = next(n for n in _ast.walk(tree) if isinstance(n, _ast.FunctionDef) and n.name == "_validate_rendered_manifests")
        validate_src = _ast.get_source_segment(source, validate_fn)
        self.assertIn("_render_expected_manifests_for_validation(", validate_src)
        reconcile_one = next(n for n in _ast.walk(tree) if isinstance(n, _ast.FunctionDef) and n.name == "_reconcile_one_pipeline")
        validate_local = next(n for n in _ast.walk(tree) if isinstance(n, _ast.FunctionDef) and n.name == "cmd_validate_local")
        self.assertIn("_validate_rendered_manifests(", _ast.get_source_segment(source, reconcile_one))
        self.assertIn("_validate_rendered_manifests(", _ast.get_source_segment(source, validate_local))

    def test_143_validate_expected_rerender_performs_zero_aws_kubectl(self):
        scripted = _standard_deploy_scripted(pipeline_ids=("fabricated-pipeline-001",))
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                _run_quiet(phase6.cmd_validate_local, argparse_namespace(environment=ENVIRONMENT))
        self.assertEqual([c for c in scripted.calls if c["argv"][:1] in (["aws"], ["kubectl"])], [])


if __name__ == "__main__":
    unittest.main()
