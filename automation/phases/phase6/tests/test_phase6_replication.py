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


def _extract_arg(argv, flag):
    return argv[argv.index(flag) + 1]


def _validate_ok(_argv):
    return FakeProc(0, f"OK: {ENVIRONMENT} deployment descriptors are valid")


def _pipelines_response(pipeline_ids):
    text = "".join(f"{p}\n" for p in pipeline_ids)
    return lambda _argv: FakeProc(0, text)


def _render_job_side_effect(plan=None):
    """Fakes automation/goldengate-replication.py's own render-job CLI: derives the same three manifests the REAL engine would (via engine.render_manifests(), never a hand-rolled approximation), then writes them exactly where the real CLI writes them -- so _validate_rendered_manifests() downstream reads genuine engine output."""
    plan = plan or PLAN

    def _fn(argv):
        output_dir = _extract_arg(argv, "--output-dir")
        namespace = _extract_arg(argv, "--namespace")
        region = _extract_arg(argv, "--region")
        execution_id = _extract_arg(argv, "--execution-id")
        manifests = engine.render_manifests(plan, namespace, region, "# reconciler source", execution_id)
        os.makedirs(output_dir, exist_ok=True)
        for kind, doc in manifests.items():
            with open(os.path.join(output_dir, f"{kind.lower()}.yaml"), "w") as f:
                yaml.safe_dump(doc, f, sort_keys=False, default_flow_style=False)
        return FakeProc(0, "")
    return _fn


def _standard_deploy_scripted(pipeline_ids=("payments-pg-to-mssql-001",), plan=None):
    """The happy-path scripted responder every Deploy-mode reconciliation test starts from -- collision preflight absent (kubectl get -> not found), apply/wait/logs/delete all succeed. Individual tests register additional .when() rules AFTERWARD to inject a specific failure at a specific point (later registrations take precedence)."""
    scripted = ScriptedRun()
    scripted.when(_is_validate_call, _validate_ok)
    scripted.when(_is_pipelines_call, _pipelines_response(list(pipeline_ids)))
    scripted.when(_is_render_job_call, _render_job_side_effect(plan))
    scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
    scripted.when(_starts_with("kubectl", "get"), FakeProc(1, "", "NotFound"))
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
    """Renders the three REAL engine manifests directly (bypassing the render-job subprocess entirely) into tmp_dir -- used by the RenderStructureTests below to feed genuine-shaped input into _validate_rendered_manifests() and then corrupt one specific field per test."""
    plan = plan or PLAN
    manifests = engine.render_manifests(plan, namespace, region, "# reconciler source", execution_id)
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
        return phase6._validate_rendered_manifests(tmp, NAMESPACE)

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
        scripted.when(_starts_with("kubectl", "get"), FakeProc(1, "", "NotFound"))
        with mock.patch.object(phase6, "run", scripted):
            phase6._collision_preflight("gg-repl-x-abc12345-1-1", NAMESPACE)

    def test_29_pre_existing_job_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("kubectl", "get"), FakeProc(1, "", "NotFound"))
        scripted.when(_starts_with("kubectl", "get", "job"), FakeProc(0, "job/x found"))
        with mock.patch.object(phase6, "run", scripted):
            with self.assertRaises(phase6.Phase6Error):
                phase6._collision_preflight("gg-repl-x-abc12345-1-1", NAMESPACE)

    def test_30_pre_existing_configmap_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("kubectl", "get"), FakeProc(1, "", "NotFound"))
        scripted.when(_starts_with("kubectl", "get", "configmap"), FakeProc(0, "configmap/x found"))
        with mock.patch.object(phase6, "run", scripted):
            with self.assertRaises(phase6.Phase6Error):
                phase6._collision_preflight("gg-repl-x-abc12345-1-1", NAMESPACE)

    def test_31_pre_existing_spc_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("kubectl", "get"), FakeProc(1, "", "NotFound"))
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
        render_calls = [c for c in scripted.calls if _is_render_job_call(c["argv"])]
        self.assertEqual(len(render_calls), 1, "pipeline-b must never be rendered once pipeline-a's Job wait times out")
        self.assertIn("pipeline-a", render_calls[0]["argv"])


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
        render_calls = [c["argv"] for c in scripted.calls if _is_render_job_call(c["argv"])]
        self.assertEqual(len(render_calls), 2)
        self.assertIn("pipeline-a", render_calls[0])
        self.assertIn("pipeline-b", render_calls[1])
        # pipeline-a's own full cleanup (its three delete calls) must precede pipeline-b's render call -- never interleaved/parallel.
        b_render_index = next(i for i, c in enumerate(scripted.calls) if _is_render_job_call(c["argv"]) and "pipeline-b" in c["argv"])
        delete_indexes_before_b = [i for i, c in enumerate(scripted.calls) if c["argv"][:2] == ["kubectl", "delete"] and i < b_render_index]
        self.assertEqual(len(delete_indexes_before_b), 3, "pipeline-a's full cleanup (3 deletes) must complete before pipeline-b is ever rendered")

    def test_48_collision_failure_on_first_prevents_second(self):
        scripted = _standard_deploy_scripted(pipeline_ids=("pipeline-a", "pipeline-b"))
        scripted.when(_starts_with("kubectl", "get", "job"), FakeProc(0, "job/x found"))
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                with self.assertRaises(phase6.Phase6Error):
                    _run_quiet(phase6.cmd_reconcile, argparse_namespace(environment=ENVIRONMENT, execution_id="1-1"))
        render_calls = [c["argv"] for c in scripted.calls if _is_render_job_call(c["argv"])]
        self.assertEqual(len(render_calls), 1)
        self.assertIn("pipeline-a", render_calls[0])


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
        render_calls = [c["argv"] for c in scripted.calls if _is_render_job_call(c["argv"])]
        self.assertEqual(len(render_calls), 1)

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
            manifests = engine.render_manifests(PLAN, namespace, region, "# reconciler source", execution_id)
            manifests["ConfigMap"]["data"]["password"] = "hunter2-actual-literal-secret-value"
            os.makedirs(output_dir, exist_ok=True)
            for kind, doc in manifests.items():
                with open(os.path.join(output_dir, f"{kind.lower()}.yaml"), "w") as f:
                    yaml.safe_dump(doc, f, sort_keys=False, default_flow_style=False)
            return FakeProc(0, "")

        scripted = ScriptedRun()
        scripted.when(_is_validate_call, _validate_ok)
        scripted.when(_is_pipelines_call, _pipelines_response(["fabricated-pipeline-001"]))
        scripted.when(_is_render_job_call, _poisoned_render)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phase6, "REPO_ROOT", Path(tmp)), mock.patch.object(phase6, "run", scripted), _env_patch():
                with self.assertRaises(phase6.Phase6Error):
                    _run_quiet(phase6.cmd_validate_local, argparse_namespace(environment=ENVIRONMENT))

    def test_56_legitimate_ogg_db_password_jmes_selector_does_not_falsely_fail(self):
        manifests = engine.render_manifests(PLAN, NAMESPACE, AWS_REGION_VALUE, "# reconciler source", "dry-run")
        objects_text = manifests["SecretProviderClass"]["spec"]["parameters"]["objects"]
        self.assertIn("OGG_DB_PASSWORD", objects_text)
        with tempfile.TemporaryDirectory() as tmp:
            for kind, doc in manifests.items():
                _dump_yaml(os.path.join(tmp, f"{kind.lower()}.yaml"), doc)
            execution_name = phase6._validate_rendered_manifests(tmp, NAMESPACE)
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


if __name__ == "__main__":
    unittest.main()
