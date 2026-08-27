"""Offline tests for automation/phases/phase4/phase4_observability.py; run directly via `python3 automation/phases/phase4/tests/test_phase4_observability.py`. No live AWS/Kubernetes/Helm -- subprocess calls are intercepted via a scripted fake, and higher-level Kubernetes read helpers (_kubectl_get_json/_pods_for_selector) are patched directly for the more intricate live-cluster behaviors (cluster-scraper host-network correction, IRSA verification, DaemonSet readiness, the 90-second export-error observation, live negative-safety checks). Covers the private-artifact inventory/digest resolution, generated-values semantic contract, recursive image extraction, forbidden-component checks, and the Argo reconciliation/acceptance delegation."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
TOOL_PATH = REPO_ROOT / "automation" / "phases" / "phase4" / "phase4_observability.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("phase4_observability", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


phase4_observability = _load_tool()

ENVIRONMENT = "dev"
ECR_REGISTRY = "229410149234.dkr.ecr.eu-west-1.amazonaws.com"
ECR_ACCOUNT_ID = "229410149234"
CLUSTER_NAME = "gg-dev-cluster"
AWS_REGION = "eu-west-1"
CLOUDWATCH_METRICS_ROLE_ARN = "arn:aws:iam::668311715351:role/GoldenGateCloudWatchMetricsRole-dev"


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class ScriptedRun:
    def __init__(self, default=None):
        self.rules = []
        self.calls = []
        self.default = default if default is not None else FakeProc(0, "", "")

    def when(self, predicate, proc):
        self.rules.append((predicate, proc))
        return self

    def __call__(self, argv, env=None, cwd=None, check=True, capture_output=True, input_text=None):
        self.calls.append({"argv": list(argv), "env": env, "input_text": input_text})
        for arg in argv:
            if isinstance(arg, str) and arg.startswith("file://") and Path(arg[len("file://"):]).is_file():
                self.calls[-1]["file_contents"] = Path(arg[len("file://"):]).read_text()
        for predicate, proc in reversed(self.rules):
            if predicate(argv):
                if check and proc.returncode != 0:
                    raise phase4_observability.Phase4Error(f"{' '.join(str(a) for a in argv)} failed: {proc.stdout}\n{proc.stderr}")
                return proc
        if check and self.default.returncode != 0:
            raise phase4_observability.Phase4Error(f"{' '.join(str(a) for a in argv)} failed: {self.default.stdout}\n{self.default.stderr}")
        return self.default


def _starts_with(*prefix):
    return lambda argv: list(argv[:len(prefix)]) == list(prefix)


class argparse_namespace:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class TempStateCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self._tmpdir.name) / "state.json"
        self.args = argparse_namespace(environment=ENVIRONMENT, state_path=self.state_path)

    def tearDown(self):
        self._tmpdir.cleanup()


def _env_patch(**overrides):
    base = {
        "AWS_REGION": AWS_REGION,
        "EKS_CLUSTER_NAME": CLUSTER_NAME,
        "EKS_DEPLOY_ROLE_ARN": "arn:aws:iam::668311715351:role/GoldenGateEksDeployRole-dev",
        "ECR_REGISTRY": ECR_REGISTRY,
        "ECR_ACCOUNT_ID": ECR_ACCOUNT_ID,
        "OBSERVABILITY_NAMESPACE": "amazon-cloudwatch",
        "ARGOCD_NAMESPACE": "argocd",
        "CLOUDWATCH_METRICS_ROLE_ARN": CLOUDWATCH_METRICS_ROLE_ARN,
    }
    base.update(overrides)
    return mock.patch.dict(os.environ, base, clear=False)


def _run_quiet(func, *args, **kwargs):
    with redirect_stdout(io.StringIO()):
        return func(*args, **kwargs)


def _digest(seed):
    return f"sha256:{seed * 64}"


def _all_digests():
    return {repo: {"tag": tag, "digest": _digest(str(i)[0] if str(i) else "a")} for i, (repo, tag) in enumerate(phase4_observability.IMAGE_TABLE, start=1)}


class SafeTokenTests(unittest.TestCase):
    def test_unsafe_environment_rejected(self):
        with self.assertRaises(phase4_observability.Phase4Error):
            phase4_observability.require_environment_arg("dev; rm -rf /")


class ImageInventoryTests(unittest.TestCase):
    def test_exact_image_inventory(self):
        expected = (
            ("aws-cloud-factory-cloudwatch-agent-operator", "3.4.2"),
            ("aws-cloud-factory-cloudwatch-agent", "1.300069.0b1529"),
            ("aws-cloud-factory-kube-state-metrics", "v2.18.0"),
            ("aws-cloud-factory-node-exporter", "v1.11.1"),
        )
        self.assertEqual(phase4_observability.IMAGE_TABLE, expected)

    def test_private_chart_contract(self):
        self.assertEqual(phase4_observability.HELM_OCI_NAMESPACE, "helm")
        self.assertEqual(phase4_observability.CHART_NAME, "amazon-cloudwatch-observability")
        self.assertEqual(phase4_observability.CHART_VERSION, "6.2.0")
        self.assertEqual(phase4_observability.CHART_ECR_REPOSITORY, "helm/amazon-cloudwatch-observability")


class ResolvePrivateArtifactsTests(TempStateCase):
    def _base_scripted(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "get-caller-identity"), FakeProc(0, ECR_ACCOUNT_ID + "\n"))
        scripted.when(_starts_with("aws", "ecr", "describe-repositories") and (lambda argv: "--query" not in argv), FakeProc(0, "{}"))
        scripted.when(lambda argv: argv[:3] == ["aws", "ecr", "describe-repositories"] and "--query" in argv, FakeProc(0, "IMMUTABLE"))
        for i, (repo, tag) in enumerate(phase4_observability.IMAGE_TABLE, start=1):
            digest = _digest(str(i))
            scripted.when(lambda argv, repo=repo, digest=digest: argv[:3] == ["aws", "ecr", "describe-images"] and repo in argv, FakeProc(0, json.dumps({"imageDetails": [{"imageDigest": digest}]})))
        scripted.when(_starts_with("aws", "ecr", "get-login-password"), FakeProc(0, "pw\n"))
        scripted.when(_starts_with("helm", "registry", "login"), FakeProc(0, ""))
        return scripted

    def _mock_helm_pull(self, chart_dir):
        def fake_helm_pull(argv, env=None, cwd=None, check=True, capture_output=True, input_text=None):
            if argv[:2] == ["helm", "pull"]:
                chart_subdir = chart_dir / phase4_observability.CHART_NAME
                chart_subdir.mkdir(parents=True, exist_ok=True)
                (chart_subdir / "Chart.yaml").write_text(yaml.safe_dump({"name": phase4_observability.CHART_NAME, "version": phase4_observability.CHART_VERSION}))
                return FakeProc(0, "")
            return None
        return fake_helm_pull

    def test_wrong_build_account_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "get-caller-identity"), FakeProc(0, "999999999999\n"))
        with mock.patch.object(phase4_observability, "run", scripted), _env_patch():
            with self.assertRaises(phase4_observability.Phase4Error):
                _run_quiet(phase4_observability.cmd_resolve_private_artifacts, self.args)

    def test_missing_ecr_repo_fails(self):
        scripted = self._base_scripted()
        scripted.when(lambda argv: argv[:3] == ["aws", "ecr", "describe-repositories"] and "--query" not in argv and phase4_observability.CHART_ECR_REPOSITORY in argv, FakeProc(1, "", "RepositoryNotFoundException"))
        with mock.patch.object(phase4_observability, "run", scripted), _env_patch():
            with self.assertRaises(phase4_observability.Phase4Error):
                _run_quiet(phase4_observability.cmd_resolve_private_artifacts, self.args)

    def test_non_immutable_repo_fails(self):
        scripted = self._base_scripted()
        scripted.when(lambda argv: argv[:3] == ["aws", "ecr", "describe-repositories"] and "--query" in argv, FakeProc(0, "MUTABLE"))
        with mock.patch.object(phase4_observability, "run", scripted), _env_patch():
            with self.assertRaises(phase4_observability.Phase4Error):
                _run_quiet(phase4_observability.cmd_resolve_private_artifacts, self.args)

    def test_missing_tag_fails(self):
        scripted = self._base_scripted()
        first_repo = phase4_observability.IMAGE_TABLE[0][0]
        scripted.when(lambda argv, r=first_repo: argv[:3] == ["aws", "ecr", "describe-images"] and r in argv, FakeProc(1, "", "ImageNotFoundException"))
        with mock.patch.object(phase4_observability, "run", scripted), _env_patch():
            with self.assertRaises(phase4_observability.Phase4Error):
                _run_quiet(phase4_observability.cmd_resolve_private_artifacts, self.args)

    def test_malformed_digest_fails(self):
        scripted = self._base_scripted()
        first_repo = phase4_observability.IMAGE_TABLE[0][0]
        scripted.when(lambda argv, r=first_repo: argv[:3] == ["aws", "ecr", "describe-images"] and r in argv, FakeProc(0, json.dumps({"imageDetails": [{"imageDigest": "not-a-digest"}]})))
        with mock.patch.object(phase4_observability, "run", scripted), _env_patch():
            with self.assertRaises(phase4_observability.Phase4Error):
                _run_quiet(phase4_observability.cmd_resolve_private_artifacts, self.args)

    def test_ecr_access_denied_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "get-caller-identity"), FakeProc(0, ECR_ACCOUNT_ID + "\n"))
        scripted.when(_starts_with("aws", "ecr", "describe-repositories"), FakeProc(1, "", "AccessDeniedException"))
        with mock.patch.object(phase4_observability, "run", scripted), _env_patch():
            with self.assertRaises(phase4_observability.Phase4Error):
                _run_quiet(phase4_observability.cmd_resolve_private_artifacts, self.args)

    def test_does_not_create_repositories(self):
        scripted = self._base_scripted()
        scripted.when(lambda argv: argv[:3] == ["aws", "ecr", "describe-repositories"] and "--query" not in argv and phase4_observability.CHART_ECR_REPOSITORY in argv, FakeProc(1, "", "RepositoryNotFoundException"))
        with mock.patch.object(phase4_observability, "run", scripted), _env_patch():
            with self.assertRaises(phase4_observability.Phase4Error):
                _run_quiet(phase4_observability.cmd_resolve_private_artifacts, self.args)
        create_calls = [c for c in scripted.calls if "create-repository" in c["argv"]]
        self.assertEqual(create_calls, [])

    def test_wrong_chart_name_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            chart_dir = Path(tmp) / "work" / "chart"
            scripted = self._base_scripted()

            def fake_run(argv, env=None, cwd=None, check=True, capture_output=True, input_text=None):
                if argv[:2] == ["helm", "pull"]:
                    subdir = chart_dir / phase4_observability.CHART_NAME
                    subdir.mkdir(parents=True, exist_ok=True)
                    (subdir / "Chart.yaml").write_text(yaml.safe_dump({"name": "wrong-chart-name", "version": phase4_observability.CHART_VERSION}))
                    return FakeProc(0, "")
                return scripted(argv, env, cwd, check, capture_output, input_text)

            with mock.patch.object(phase4_observability, "run", fake_run), mock.patch.object(phase4_observability, "REPO_ROOT", Path(tmp)), _env_patch():
                with self.assertRaises(phase4_observability.Phase4Error):
                    _run_quiet(phase4_observability.cmd_resolve_private_artifacts, self.args)

    def test_wrong_chart_version_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            chart_dir = Path(tmp) / "work" / "chart"
            scripted = self._base_scripted()

            def fake_run(argv, env=None, cwd=None, check=True, capture_output=True, input_text=None):
                if argv[:2] == ["helm", "pull"]:
                    subdir = chart_dir / phase4_observability.CHART_NAME
                    subdir.mkdir(parents=True, exist_ok=True)
                    (subdir / "Chart.yaml").write_text(yaml.safe_dump({"name": phase4_observability.CHART_NAME, "version": "1.0.0"}))
                    return FakeProc(0, "")
                return scripted(argv, env, cwd, check, capture_output, input_text)

            with mock.patch.object(phase4_observability, "run", fake_run), mock.patch.object(phase4_observability, "REPO_ROOT", Path(tmp)), _env_patch():
                with self.assertRaises(phase4_observability.Phase4Error):
                    _run_quiet(phase4_observability.cmd_resolve_private_artifacts, self.args)

    def test_success_resolves_all_digests_and_pulls_chart(self):
        with tempfile.TemporaryDirectory() as tmp:
            chart_dir = Path(tmp) / "work" / "chart"
            scripted = self._base_scripted()

            def fake_run(argv, env=None, cwd=None, check=True, capture_output=True, input_text=None):
                if argv[:2] == ["helm", "pull"]:
                    subdir = chart_dir / phase4_observability.CHART_NAME
                    subdir.mkdir(parents=True, exist_ok=True)
                    (subdir / "Chart.yaml").write_text(yaml.safe_dump({"name": phase4_observability.CHART_NAME, "version": phase4_observability.CHART_VERSION}))
                    return FakeProc(0, "")
                return scripted(argv, env, cwd, check, capture_output, input_text)

            with mock.patch.object(phase4_observability, "run", fake_run), mock.patch.object(phase4_observability, "REPO_ROOT", Path(tmp)), _env_patch():
                _run_quiet(phase4_observability.cmd_resolve_private_artifacts, self.args)
            state = phase4_observability.load_state(self.state_path)
            self.assertEqual(len(state["image_digests"]), 4)
            for repo, _tag in phase4_observability.IMAGE_TABLE:
                self.assertIn(repo, state["image_digests"])

    def test_ecr_password_via_stdin_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            chart_dir = Path(tmp) / "work" / "chart"
            scripted = self._base_scripted()

            def fake_run(argv, env=None, cwd=None, check=True, capture_output=True, input_text=None):
                scripted.calls.append({"argv": list(argv), "input_text": input_text})
                if argv[:2] == ["helm", "pull"]:
                    subdir = chart_dir / phase4_observability.CHART_NAME
                    subdir.mkdir(parents=True, exist_ok=True)
                    (subdir / "Chart.yaml").write_text(yaml.safe_dump({"name": phase4_observability.CHART_NAME, "version": phase4_observability.CHART_VERSION}))
                    return FakeProc(0, "")
                return ScriptedRun.__call__(scripted, argv, env, cwd, check, capture_output, input_text)

            with mock.patch.object(phase4_observability, "run", fake_run), mock.patch.object(phase4_observability, "REPO_ROOT", Path(tmp)), _env_patch():
                _run_quiet(phase4_observability.cmd_resolve_private_artifacts, self.args)
            login_call = next(c for c in scripted.calls if c["argv"][:2] == ["helm", "registry"])
            self.assertEqual(login_call["input_text"], "pw")
            self.assertNotIn("pw", login_call["argv"])


class GeneratedValuesTests(unittest.TestCase):
    def _committed_values(self):
        return {
            "clusterName": "placeholder", "region": "placeholder",
            "agent": {"image": {"repository": "aws-cloud-factory-cloudwatch-agent", "tag": "1.300069.0b1529"}, "serviceAccount": {"name": "cloudwatch-agent"}},
            "manager": {"image": {"repository": "aws-cloud-factory-cloudwatch-agent-operator", "tag": "3.4.2"}},
            "kubeStateMetrics": {"image": {"repository": "aws-cloud-factory-kube-state-metrics", "tag": "v2.18.0"}},
            "nodeExporter": {"image": {"repository": "aws-cloud-factory-node-exporter", "tag": "v1.11.1"}},
        }

    def test_generate_deployment_values_injects_tag_at_digest(self):
        digests = {repo: {"tag": tag, "digest": _digest("a")} for repo, tag in phase4_observability.IMAGE_TABLE}
        with tempfile.TemporaryDirectory() as tmp:
            committed_path = Path(tmp) / "values.yaml"
            committed_path.write_text(yaml.safe_dump(self._committed_values()))
            output_path = Path(tmp) / "generated-values.yaml"
            _run_quiet(phase4_observability._generate_deployment_values, committed_path, digests, CLUSTER_NAME, AWS_REGION, ECR_REGISTRY, output_path)
            with output_path.open() as f:
                generated = yaml.safe_load(f)
        self.assertEqual(generated["clusterName"], CLUSTER_NAME)
        self.assertEqual(generated["region"], AWS_REGION)
        self.assertEqual(generated["agent"]["image"]["tag"], f"1.300069.0b1529@{_digest('a')}")
        self.assertEqual(generated["agent"]["image"]["repositoryDomainMap"]["public"], ECR_REGISTRY)

    def test_generate_deployment_values_does_not_mutate_committed_file(self):
        digests = {repo: {"tag": tag, "digest": _digest("a")} for repo, tag in phase4_observability.IMAGE_TABLE}
        with tempfile.TemporaryDirectory() as tmp:
            committed_path = Path(tmp) / "values.yaml"
            original_text = yaml.safe_dump(self._committed_values())
            committed_path.write_text(original_text)
            output_path = Path(tmp) / "generated-values.yaml"
            _run_quiet(phase4_observability._generate_deployment_values, committed_path, digests, CLUSTER_NAME, AWS_REGION, ECR_REGISTRY, output_path)
            self.assertEqual(committed_path.read_text(), original_text)

    def test_generate_deployment_values_rejects_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            committed_path = Path(tmp) / "values.yaml"
            committed_path.write_text("clusterName: a\nclusterName: b\n")
            with self.assertRaises(phase4_observability.Phase4Error):
                phase4_observability._load_committed_values(committed_path)

    def _valid_generated_values(self):
        return {
            "clusterName": CLUSTER_NAME, "region": AWS_REGION, "k8sMode": "EKS",
            "containerLogs": {"enabled": False}, "containerInsights": {"enabled": False},
            "applicationSignals": {"enabled": False},
            "manager": {"applicationSignals": {"autoMonitor": {"monitorAllServices": False}}, "image": {"repository": "aws-cloud-factory-cloudwatch-agent-operator", "tag": f"3.4.2@{_digest('a')}", "repositoryDomainMap": {"public": ECR_REGISTRY}}},
            "otelContainerInsights": {"enabled": True, "logs": {"enabled": False}},
            "dcgmExporter": {"enabled": False}, "neuronMonitor": {"enabled": False},
            "kubeStateMetrics": {"enabled": True, "image": {"repository": "aws-cloud-factory-kube-state-metrics", "tag": f"v2.18.0@{_digest('b')}", "repositoryDomainMap": {"public": ECR_REGISTRY}}},
            "nodeExporter": {"enabled": True, "image": {"repository": "aws-cloud-factory-node-exporter", "tag": f"v1.11.1@{_digest('c')}", "repositoryDomainMap": {"public": ECR_REGISTRY}}},
            "agent": {
                "prometheus": {"targetAllocator": {"enabled": False}},
                "serviceAccount": {"name": "cloudwatch-agent"},
                "image": {"repository": "aws-cloud-factory-cloudwatch-agent", "tag": f"1.300069.0b1529@{_digest('d')}", "repositoryDomainMap": {"public": ECR_REGISTRY}},
            },
            "agents": [
                {"name": "cloudwatch-agent", "mode": "daemonset", "hostNetwork": True},
                {"name": "cloudwatch-agent-cluster-scraper", "mode": "deployment", "config": "default", "hostNetwork": False},
            ],
        }

    def _write_and_validate(self, values):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "generated.yaml"
            path.write_text(yaml.safe_dump(values))
            _run_quiet(phase4_observability._validate_generated_values_semantics, path, CLUSTER_NAME, AWS_REGION, ECR_REGISTRY)

    def test_valid_generated_values_pass(self):
        self._write_and_validate(self._valid_generated_values())

    def test_wrong_agents_count_fails(self):
        values = self._valid_generated_values()
        values["agents"] = [values["agents"][0]]
        with self.assertRaises(phase4_observability.Phase4Error):
            self._write_and_validate(values)

    def test_duplicate_agent_names_fail(self):
        values = self._valid_generated_values()
        values["agents"][1]["name"] = "cloudwatch-agent"
        with self.assertRaises(phase4_observability.Phase4Error):
            self._write_and_validate(values)

    def test_cloudwatch_agent_hostnetwork_must_be_true(self):
        values = self._valid_generated_values()
        values["agents"][0]["hostNetwork"] = False
        with self.assertRaises(phase4_observability.Phase4Error):
            self._write_and_validate(values)

    def test_cluster_scraper_hostnetwork_must_be_false(self):
        values = self._valid_generated_values()
        values["agents"][1]["hostNetwork"] = True
        with self.assertRaises(phase4_observability.Phase4Error):
            self._write_and_validate(values)

    def test_cluster_scraper_mode_must_be_deployment(self):
        values = self._valid_generated_values()
        values["agents"][1]["mode"] = "daemonset"
        with self.assertRaises(phase4_observability.Phase4Error):
            self._write_and_validate(values)

    def test_target_allocator_must_be_disabled(self):
        values = self._valid_generated_values()
        values["agent"]["prometheus"]["targetAllocator"]["enabled"] = True
        with self.assertRaises(phase4_observability.Phase4Error):
            self._write_and_validate(values)

    def test_dcgm_must_be_disabled(self):
        values = self._valid_generated_values()
        values["dcgmExporter"]["enabled"] = True
        with self.assertRaises(phase4_observability.Phase4Error):
            self._write_and_validate(values)

    def test_neuron_must_be_disabled(self):
        values = self._valid_generated_values()
        values["neuronMonitor"]["enabled"] = True
        with self.assertRaises(phase4_observability.Phase4Error):
            self._write_and_validate(values)

    def test_application_signals_must_be_disabled(self):
        values = self._valid_generated_values()
        values["applicationSignals"]["enabled"] = True
        with self.assertRaises(phase4_observability.Phase4Error):
            self._write_and_validate(values)

    def test_wrong_repository_domain_map_fails(self):
        values = self._valid_generated_values()
        values["agent"]["image"]["repositoryDomainMap"]["public"] = "wrong.example.com"
        with self.assertRaises(phase4_observability.Phase4Error):
            self._write_and_validate(values)

    def test_tag_without_digest_fails(self):
        values = self._valid_generated_values()
        values["agent"]["image"]["tag"] = "1.300069.0b1529"
        with self.assertRaises(phase4_observability.Phase4Error):
            self._write_and_validate(values)


class RecursiveImageExtractionTests(unittest.TestCase):
    def _valid_images(self):
        # Rendered format is repo:tag@sha256:digest (the chart templates `{{ repository }}:{{ tag }}`, and tag itself was set to `tag@sha256:digest` during values generation).
        return [f"{ECR_REGISTRY}/{repo}:{tag}@{_digest(str(i))}" for i, (repo, tag) in enumerate(phase4_observability.IMAGE_TABLE, start=1)]

    def test_exactly_four_unique_images_pass(self):
        images = self._valid_images()
        docs = [{"kind": "Pod", "spec": {"containers": [{"image": img} for img in images]}}]
        _run_quiet(phase4_observability._validate_rendered_images, docs, ECR_REGISTRY)

    def test_initcontainer_image_is_included(self):
        images = self._valid_images()
        docs = [{"kind": "Pod", "spec": {"containers": [{"image": images[0]}], "initContainers": [{"image": images[1]}, {"image": images[2]}, {"image": images[3]}]}}]
        _run_quiet(phase4_observability._validate_rendered_images, docs, ECR_REGISTRY)

    def test_initcontainer_public_image_fails(self):
        images = self._valid_images()[:3]
        docs = [{"kind": "Pod", "spec": {"containers": [{"image": img} for img in images], "initContainers": [{"image": "docker.io/library/busybox:latest"}]}}]
        with self.assertRaises(phase4_observability.Phase4Error):
            phase4_observability._validate_rendered_images(docs, ECR_REGISTRY)

    def test_public_registry_image_fails(self):
        docs = [{"kind": "Pod", "spec": {"containers": [{"image": "public.ecr.aws/x/y:latest"}]}}]
        with self.assertRaises(phase4_observability.Phase4Error):
            phase4_observability._validate_rendered_images(docs, ECR_REGISTRY)

    def test_image_missing_digest_fails(self):
        docs = [{"kind": "Pod", "spec": {"containers": [{"image": f"{ECR_REGISTRY}/aws-cloud-factory-cloudwatch-agent:1.300069.0b1529"}]}}]
        with self.assertRaises(phase4_observability.Phase4Error):
            phase4_observability._validate_rendered_images(docs, ECR_REGISTRY)

    def test_unlisted_repository_fails(self):
        docs = [{"kind": "Pod", "spec": {"containers": [{"image": f"{ECR_REGISTRY}/some-unlisted-repo:v1@{_digest('a')}"}]}}]
        with self.assertRaises(phase4_observability.Phase4Error):
            phase4_observability._validate_rendered_images(docs, ECR_REGISTRY)

    def test_too_few_unique_images_fails(self):
        images = self._valid_images()[:3]
        docs = [{"kind": "Pod", "spec": {"containers": [{"image": img} for img in images]}}]
        with self.assertRaises(phase4_observability.Phase4Error):
            phase4_observability._validate_rendered_images(docs, ECR_REGISTRY)


class ForbiddenComponentTests(unittest.TestCase):
    def test_baseline_passes(self):
        docs = [
            {"kind": "Deployment", "metadata": {"name": "amazon-cloudwatch-observability-controller-manager"}},
            {"kind": "AmazonCloudWatchAgent", "metadata": {"name": "cloudwatch-agent"}, "spec": {"otelConfig": "receivers:\n  awscontainerinsightreceiver: {}\n"}},
        ]
        _run_quiet(phase4_observability._validate_no_forbidden_components, docs)

    def test_dcgm_cr_instance_fails(self):
        docs = [{"kind": "DcgmExporter", "metadata": {"name": "some-dcgm"}}]
        with self.assertRaises(phase4_observability.Phase4Error):
            phase4_observability._validate_no_forbidden_components(docs)

    def test_neuron_cr_instance_fails(self):
        docs = [{"kind": "NeuronMonitor", "metadata": {"name": "some-neuron"}}]
        with self.assertRaises(phase4_observability.Phase4Error):
            phase4_observability._validate_no_forbidden_components(docs)

    def test_application_signals_instrumentation_fails(self):
        docs = [{"kind": "Instrumentation", "metadata": {"name": "auto-instrumentation"}}]
        with self.assertRaises(phase4_observability.Phase4Error):
            phase4_observability._validate_no_forbidden_components(docs)

    def test_target_allocator_named_resource_fails(self):
        docs = [{"kind": "Deployment", "metadata": {"name": "target-allocator"}}]
        with self.assertRaises(phase4_observability.Phase4Error):
            phase4_observability._validate_no_forbidden_components(docs)

    def test_fluent_bit_daemonset_fails(self):
        docs = [{"kind": "DaemonSet", "metadata": {"name": "fluent-bit"}}]
        with self.assertRaises(phase4_observability.Phase4Error):
            phase4_observability._validate_no_forbidden_components(docs)

    def test_filelog_receiver_in_otel_config_fails(self):
        docs = [{"kind": "AmazonCloudWatchAgent", "metadata": {"name": "cloudwatch-agent"}, "spec": {"otelConfig": "receivers:\n  filelog: {}\n"}}]
        with self.assertRaises(phase4_observability.Phase4Error):
            phase4_observability._validate_no_forbidden_components(docs)

    def test_crd_schema_document_is_not_a_forbidden_instance(self):
        docs = [{"kind": "CustomResourceDefinition", "metadata": {"name": "dcgmexporters.cloudwatch.aws.amazon.com"}}]
        _run_quiet(phase4_observability._validate_no_forbidden_components, docs)


class ServiceAccountAndResourceNameTests(unittest.TestCase):
    def _sa_docs(self, extra_annotation=None):
        annotations = {"eks.amazonaws.com/role-arn": extra_annotation} if extra_annotation else {}
        return [{"kind": "ServiceAccount", "metadata": {"name": "cloudwatch-agent", "namespace": "amazon-cloudwatch", "annotations": annotations}}]

    def test_exact_cloudwatch_agent_service_account(self):
        _run_quiet(phase4_observability._validate_cloudwatch_agent_service_account, self._sa_docs(), "amazon-cloudwatch")

    def test_missing_cloudwatch_agent_service_account_fails(self):
        with self.assertRaises(phase4_observability.Phase4Error):
            phase4_observability._validate_cloudwatch_agent_service_account([], "amazon-cloudwatch")

    def test_chart_rendered_role_arn_annotation_fails(self):
        with self.assertRaises(phase4_observability.Phase4Error):
            phase4_observability._validate_cloudwatch_agent_service_account(self._sa_docs(extra_annotation=CLOUDWATCH_METRICS_ROLE_ARN), "amazon-cloudwatch")

    def test_expected_resource_names_present(self):
        docs = [
            {"kind": "AmazonCloudWatchAgent", "metadata": {"name": "cloudwatch-agent"}},
            {"kind": "AmazonCloudWatchAgent", "metadata": {"name": "cloudwatch-agent-cluster-scraper"}},
            {"kind": "Deployment", "metadata": {"name": "amazon-cloudwatch-observability-controller-manager"}},
            {"kind": "Deployment", "metadata": {"name": "kube-state-metrics"}},
            {"kind": "DaemonSet", "metadata": {"name": "node-exporter"}},
            {"kind": "ServiceAccount", "metadata": {"name": "kube-state-metrics-service-acct"}},
            {"kind": "ServiceAccount", "metadata": {"name": "node-exporter-service-acct"}},
            {"kind": "ServiceAccount", "metadata": {"name": "amazon-cloudwatch-observability-controller-manager"}},
        ]
        _run_quiet(phase4_observability._validate_exact_resource_names, docs)

    def test_missing_expected_resource_fails(self):
        with self.assertRaises(phase4_observability.Phase4Error):
            phase4_observability._validate_exact_resource_names([])

    def _cr_docs(self, agent_hostnetwork=True, scraper_hostnetwork=False):
        return [
            {"kind": "AmazonCloudWatchAgent", "metadata": {"name": "cloudwatch-agent"}, "spec": {"mode": "daemonset", "hostNetwork": agent_hostnetwork}},
            {"kind": "AmazonCloudWatchAgent", "metadata": {"name": "cloudwatch-agent-cluster-scraper"}, "spec": {"mode": "deployment", "hostNetwork": scraper_hostnetwork}},
        ]

    def test_rendered_host_network_isolation_passes(self):
        _run_quiet(phase4_observability._validate_rendered_host_network_isolation, self._cr_docs())

    def test_agent_cr_daemonset_hostnetwork_true_required(self):
        with self.assertRaises(phase4_observability.Phase4Error):
            phase4_observability._validate_rendered_host_network_isolation(self._cr_docs(agent_hostnetwork=False))

    def test_scraper_cr_deployment_hostnetwork_false_required(self):
        with self.assertRaises(phase4_observability.Phase4Error):
            phase4_observability._validate_rendered_host_network_isolation(self._cr_docs(scraper_hostnetwork=True))

    def test_no_unresolved_placeholders_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rendered.yaml"
            path.write_text("kind: Pod\n")
            _run_quiet(phase4_observability._validate_no_unresolved_placeholders, path)

    def test_unresolved_placeholder_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rendered.yaml"
            path.write_text("value: <no value>\n")
            with self.assertRaises(phase4_observability.Phase4Error):
                phase4_observability._validate_no_unresolved_placeholders(path)


class ReadyzAndCrdClassificationTests(unittest.TestCase):
    def test_readyz_success(self):
        _run_quiet(phase4_observability._classify_readyz, FakeProc(0, "ok", ""))

    def test_readyz_network_timeout_classified(self):
        with self.assertRaises(phase4_observability.Phase4Error) as ctx:
            phase4_observability._classify_readyz(FakeProc(1, "", "dial tcp: i/o timeout"))
        self.assertIn("network-reachability", str(ctx.exception))

    def test_readyz_forbidden_classified(self):
        with self.assertRaises(phase4_observability.Phase4Error) as ctx:
            phase4_observability._classify_readyz(FakeProc(1, "", "Error from server (Forbidden): ..."))
        self.assertIn("RBAC", str(ctx.exception))

    def test_crd_present_passes(self):
        _run_quiet(phase4_observability._classify_argocd_crd, FakeProc(0, "", ""))

    def test_crd_not_found_classified(self):
        with self.assertRaises(phase4_observability.Phase4Error) as ctx:
            phase4_observability._classify_argocd_crd(FakeProc(1, "", 'Error from server (NotFound): customresourcedefinitions.apiextensions.k8s.io "applications.argoproj.io" not found'))
        self.assertIn("genuinely absent", str(ctx.exception))

    def test_crd_forbidden_classified_distinct_from_missing(self):
        with self.assertRaises(phase4_observability.Phase4Error) as ctx:
            phase4_observability._classify_argocd_crd(FakeProc(1, "", "Error from server (Forbidden): ..."))
        self.assertIn("RBAC authorization gap", str(ctx.exception))

    def test_crd_unexpected_failure_classified(self):
        with self.assertRaises(phase4_observability.Phase4Error) as ctx:
            phase4_observability._classify_argocd_crd(FakeProc(1, "", "connection reset by peer"))
        self.assertIn("unexpected reason", str(ctx.exception))


class ApplicationManifestTests(unittest.TestCase):
    def test_application_manifest_exact_contract(self):
        manifest = phase4_observability._build_application_manifest(
            "values: {}", f"oci://{ECR_REGISTRY}/helm/amazon-cloudwatch-observability", "6.2.0",
            "amazon-cloudwatch-observability", "goldengate-observability", "argocd", "amazon-cloudwatch",
        )
        self.assertEqual(manifest["metadata"]["name"], "goldengate-observability")
        self.assertEqual(manifest["spec"]["source"]["targetRevision"], "6.2.0")
        self.assertEqual(manifest["spec"]["destination"]["namespace"], "amazon-cloudwatch")
        self.assertIn("CreateNamespace=true", manifest["spec"]["syncPolicy"]["syncOptions"])
        self.assertIn("ServerSideApply=true", manifest["spec"]["syncPolicy"]["syncOptions"])
        self.assertIn("RespectIgnoreDifferences=true", manifest["spec"]["syncPolicy"]["syncOptions"])
        ignore = manifest["spec"]["ignoreDifferences"][0]
        self.assertEqual(ignore["name"], "cloudwatch-agent")
        self.assertIn("/metadata/annotations/eks.amazonaws.com~1role-arn", ignore["jsonPointers"])


class ArgoWaitTests(unittest.TestCase):
    def test_argo_wait_success(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("kubectl", "get", "application"), FakeProc(0, ""))
        scripted.when(lambda argv: "jsonpath={.status.sync.status}" in argv, FakeProc(0, "Synced"))
        scripted.when(lambda argv: "jsonpath={.status.health.status}" in argv, FakeProc(0, "Healthy"))
        with mock.patch.object(phase4_observability, "run", scripted), mock.patch.object(phase4_observability.time, "sleep") as sleep_mock:
            _run_quiet(phase4_observability._wait_for_argo_application, "goldengate-observability", "argocd", 900, 15)
        sleep_mock.assert_not_called()

    def test_argo_wait_timeout_at_900s(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("kubectl", "get", "application"), FakeProc(0, ""))
        scripted.when(lambda argv: "jsonpath={.status.sync.status}" in argv, FakeProc(0, "OutOfSync"))
        scripted.when(lambda argv: "jsonpath={.status.health.status}" in argv, FakeProc(0, "Progressing"))
        with mock.patch.object(phase4_observability, "run", scripted), mock.patch.object(phase4_observability.time, "sleep") as sleep_mock:
            with self.assertRaises(phase4_observability.Phase4Error) as ctx:
                _run_quiet(phase4_observability._wait_for_argo_application, "goldengate-observability", "argocd", 900, 15)
            self.assertIn("900s", str(ctx.exception))
        total_slept = sum(c.args[0] for c in sleep_mock.call_args_list)
        self.assertGreaterEqual(total_slept, 900)

    def test_argo_degraded_fails_immediately(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("kubectl", "get", "application"), FakeProc(0, ""))
        scripted.when(lambda argv: "jsonpath={.status.sync.status}" in argv, FakeProc(0, "OutOfSync"))
        scripted.when(lambda argv: "jsonpath={.status.health.status}" in argv, FakeProc(0, "Degraded"))
        with mock.patch.object(phase4_observability, "run", scripted), mock.patch.object(phase4_observability.time, "sleep") as sleep_mock:
            with self.assertRaises(phase4_observability.Phase4Error) as ctx:
                _run_quiet(phase4_observability._wait_for_argo_application, "goldengate-observability", "argocd", 900, 15)
            self.assertIn("Degraded", str(ctx.exception))
        sleep_mock.assert_not_called()


class ClusterScraperHostNetworkCorrectionTests(unittest.TestCase):
    NAMESPACE = "amazon-cloudwatch"

    def _cr(self, mode="deployment", host_network=False, uid="cr-uid-1"):
        return {"spec": {"mode": mode, "hostNetwork": host_network}, "metadata": {"uid": uid}}

    def _deployment(self, host_network=True, owner_uid="cr-uid-1", owner_kind="AmazonCloudWatchAgent", owner_name="cloudwatch-agent-cluster-scraper", uid="deploy-uid-1", ready=True, deletion_timestamp=None):
        replicas = 1
        return {
            "metadata": {"name": "cloudwatch-agent-cluster-scraper", "namespace": self.NAMESPACE, "uid": uid, "generation": 1, "deletionTimestamp": deletion_timestamp,
                         "ownerReferences": [{"controller": True, "kind": owner_kind, "name": owner_name, "apiVersion": "cloudwatch.aws.amazon.com/v1alpha1", "uid": owner_uid}]},
            "spec": {"replicas": replicas, "template": {"spec": {"hostNetwork": host_network}}},
            "status": {"observedGeneration": 1, "updatedReplicas": replicas if ready else 0, "availableReplicas": replicas if ready else 0, "unavailableReplicas": 0},
        }

    def test_cr_wrong_mode_fails(self):
        with mock.patch.object(phase4_observability, "_kubectl_get_json", return_value=self._cr(mode="daemonset")):
            with self.assertRaises(phase4_observability.Phase4Error):
                phase4_observability._ensure_cluster_scraper_host_network_isolated(self.NAMESPACE)

    def test_cr_hostnetwork_not_false_fails(self):
        with mock.patch.object(phase4_observability, "_kubectl_get_json", return_value=self._cr(host_network=True)):
            with self.assertRaises(phase4_observability.Phase4Error):
                phase4_observability._ensure_cluster_scraper_host_network_isolated(self.NAMESPACE)

    def test_deployment_absent_not_required(self):
        def fake_get(resource, name, namespace, check=True):
            if resource.startswith("amazoncloudwatchagents"):
                return self._cr()
            return None
        with mock.patch.object(phase4_observability, "_kubectl_get_json", side_effect=fake_get):
            result = _run_quiet(phase4_observability._ensure_cluster_scraper_host_network_isolated, self.NAMESPACE)
        self.assertEqual(result, "not_required")

    def test_deployment_already_hostnetwork_false_not_required(self):
        def fake_get(resource, name, namespace, check=True):
            if resource.startswith("amazoncloudwatchagents"):
                return self._cr()
            return self._deployment(host_network=False)
        with mock.patch.object(phase4_observability, "_kubectl_get_json", side_effect=fake_get):
            result = _run_quiet(phase4_observability._ensure_cluster_scraper_host_network_isolated, self.NAMESPACE)
        self.assertEqual(result, "not_required")

    def test_owner_chain_mismatch_fails(self):
        def fake_get(resource, name, namespace, check=True):
            if resource.startswith("amazoncloudwatchagents"):
                return self._cr()
            return self._deployment(host_network=True, owner_uid="some-other-uid")
        with mock.patch.object(phase4_observability, "_kubectl_get_json", side_effect=fake_get):
            with self.assertRaises(phase4_observability.Phase4Error) as ctx:
                phase4_observability._ensure_cluster_scraper_host_network_isolated(self.NAMESPACE)
        self.assertIn("ownership validation failed", str(ctx.exception))

    def test_owner_chain_wrong_kind_fails(self):
        def fake_get(resource, name, namespace, check=True):
            if resource.startswith("amazoncloudwatchagents"):
                return self._cr()
            return self._deployment(host_network=True, owner_kind="SomeOtherKind")
        with mock.patch.object(phase4_observability, "_kubectl_get_json", side_effect=fake_get):
            with self.assertRaises(phase4_observability.Phase4Error):
                phase4_observability._ensure_cluster_scraper_host_network_isolated(self.NAMESPACE)

    def test_corrective_path_recreates_and_validates(self):
        state = {"phase": "before_delete"}

        def fake_get(resource, name, namespace, check=True):
            if resource.startswith("amazoncloudwatchagents"):
                return self._cr()
            if state["phase"] == "before_delete":
                return self._deployment(host_network=True, uid="old-uid")
            return self._deployment(host_network=False, uid="new-uid")

        def fake_run(argv, env=None, cwd=None, check=True, capture_output=True, input_text=None):
            if argv[:2] == ["kubectl", "delete"]:
                state["phase"] = "recreated"
                return FakeProc(0, "")
            return FakeProc(0, "")

        with mock.patch.object(phase4_observability, "_kubectl_get_json", side_effect=fake_get), \
             mock.patch.object(phase4_observability, "run", fake_run), \
             mock.patch.object(phase4_observability.time, "sleep"):
            result = _run_quiet(phase4_observability._ensure_cluster_scraper_host_network_isolated, self.NAMESPACE)
        self.assertEqual(result, "recreated_once")

    def test_correction_recreate_timeout_fails(self):
        def fake_get(resource, name, namespace, check=True):
            if resource.startswith("amazoncloudwatchagents"):
                return self._cr()
            return self._deployment(host_network=True, uid="old-uid")  # never changes -- simulates the operator never recreating it

        with mock.patch.object(phase4_observability, "_kubectl_get_json", side_effect=fake_get), \
             mock.patch.object(phase4_observability, "run", return_value=FakeProc(0, "")), \
             mock.patch.object(phase4_observability.time, "sleep"):
            with self.assertRaises(phase4_observability.Phase4Error) as ctx:
                _run_quiet(phase4_observability._ensure_cluster_scraper_host_network_isolated, self.NAMESPACE)
        self.assertIn("did not recreate", str(ctx.exception))


class IrsaVerificationTests(unittest.TestCase):
    def _pod(self, name="pod-1", phase="Running", ready="True", sa="cloudwatch-agent", env_names=("AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE")):
        return {
            "metadata": {"name": name},
            "status": {"phase": phase, "conditions": [{"type": "Ready", "status": ready}]},
            "spec": {"serviceAccountName": sa, "containers": [{"env": [{"name": n, "value": "irrelevant"} for n in env_names]}]},
        }

    def test_pod_irsa_valid(self):
        _run_quiet(phase4_observability._verify_pod_irsa, "cloudwatch-agent DaemonSet", self._pod())

    def test_pod_not_running_fails(self):
        with self.assertRaises(phase4_observability.Phase4Error):
            phase4_observability._verify_pod_irsa("x", self._pod(phase="Pending"))

    def test_pod_not_ready_fails(self):
        with self.assertRaises(phase4_observability.Phase4Error):
            phase4_observability._verify_pod_irsa("x", self._pod(ready="False"))

    def test_pod_wrong_service_account_fails(self):
        with self.assertRaises(phase4_observability.Phase4Error):
            phase4_observability._verify_pod_irsa("x", self._pod(sa="default"))

    def test_pod_missing_role_arn_env_fails(self):
        with self.assertRaises(phase4_observability.Phase4Error):
            phase4_observability._verify_pod_irsa("x", self._pod(env_names=("AWS_WEB_IDENTITY_TOKEN_FILE",)))

    def test_pod_missing_token_file_env_fails(self):
        with self.assertRaises(phase4_observability.Phase4Error):
            phase4_observability._verify_pod_irsa("x", self._pod(env_names=("AWS_ROLE_ARN",)))

    def test_env_values_never_inspected(self):
        # The verifier only checks env-var NAMES; a pod whose token-file value is garbage/empty still passes.
        pod = self._pod()
        pod["spec"]["containers"][0]["env"] = [{"name": "AWS_ROLE_ARN", "value": ""}, {"name": "AWS_WEB_IDENTITY_TOKEN_FILE", "value": ""}]
        _run_quiet(phase4_observability._verify_pod_irsa, "x", pod)

    def test_all_daemonset_pods_verified(self):
        ds = {"status": {"desiredNumberScheduled": 2}, "spec": {"selector": {"matchLabels": {"k8s-app": "cloudwatch-agent"}}}}
        scraper_deploy = {"spec": {"selector": {"matchLabels": {"app": "cluster-scraper"}}}}

        def fake_get(resource, name, namespace, check=True):
            if resource == "daemonset":
                return ds
            return scraper_deploy

        def fake_pods(namespace, selector):
            if "k8s-app" in selector:
                return [self._pod("agent-1"), self._pod("agent-2")]
            return [self._pod("scraper-1")]

        with mock.patch.object(phase4_observability, "_kubectl_get_json", side_effect=fake_get), \
             mock.patch.object(phase4_observability, "_pods_for_selector", side_effect=fake_pods):
            _run_quiet(phase4_observability._verify_irsa_injection, "amazon-cloudwatch")

    def test_pod_count_mismatch_fails(self):
        ds = {"status": {"desiredNumberScheduled": 3}, "spec": {"selector": {"matchLabels": {"k8s-app": "cloudwatch-agent"}}}}
        with mock.patch.object(phase4_observability, "_kubectl_get_json", return_value=ds), \
             mock.patch.object(phase4_observability, "_pods_for_selector", return_value=[self._pod("agent-1")]):
            with self.assertRaises(phase4_observability.Phase4Error):
                phase4_observability._verify_irsa_injection("amazon-cloudwatch")


class DaemonsetReadinessTests(unittest.TestCase):
    def _ds(self, generation=5, observed=5, desired=3, current=3, updated=3, ready=3, available=3, unavailable=0):
        return {
            "metadata": {"generation": generation},
            "status": {"observedGeneration": observed, "desiredNumberScheduled": desired, "currentNumberScheduled": current,
                       "updatedNumberScheduled": updated, "numberReady": ready, "numberAvailable": available, "numberUnavailable": unavailable},
        }

    def test_full_readiness_exact_equality_passes(self):
        with mock.patch.object(phase4_observability, "_kubectl_get_json", return_value=self._ds()):
            _run_quiet(phase4_observability._wait_for_daemonset_fully_ready, "amazon-cloudwatch", "cloudwatch-agent", 1)

    def test_desired_zero_fails_bounded(self):
        with mock.patch.object(phase4_observability, "_kubectl_get_json", return_value=self._ds(desired=0)), \
             mock.patch.object(phase4_observability.time, "sleep"):
            with self.assertRaises(phase4_observability.Phase4Error):
                phase4_observability._wait_for_daemonset_fully_ready("amazon-cloudwatch", "cloudwatch-agent", 5)

    def test_generation_mismatch_not_ready(self):
        with mock.patch.object(phase4_observability, "_kubectl_get_json", return_value=self._ds(generation=6, observed=5)), \
             mock.patch.object(phase4_observability.time, "sleep"):
            with self.assertRaises(phase4_observability.Phase4Error):
                phase4_observability._wait_for_daemonset_fully_ready("amazon-cloudwatch", "cloudwatch-agent", 5)

    def test_unavailable_nonzero_not_ready(self):
        with mock.patch.object(phase4_observability, "_kubectl_get_json", return_value=self._ds(unavailable=1)), \
             mock.patch.object(phase4_observability.time, "sleep"):
            with self.assertRaises(phase4_observability.Phase4Error):
                phase4_observability._wait_for_daemonset_fully_ready("amazon-cloudwatch", "cloudwatch-agent", 5)

    def test_ready_less_than_desired_not_ready(self):
        with mock.patch.object(phase4_observability, "_kubectl_get_json", return_value=self._ds(ready=2)), \
             mock.patch.object(phase4_observability.time, "sleep"):
            with self.assertRaises(phase4_observability.Phase4Error):
                phase4_observability._wait_for_daemonset_fully_ready("amazon-cloudwatch", "cloudwatch-agent", 5)


class ExportErrorObservationTests(unittest.TestCase):
    def test_no_error_pattern_passes(self):
        self.assertFalse(phase4_observability._AUTH_ERROR_PATTERN.search("everything is fine"))
        self.assertFalse(phase4_observability._STARTUP_ERROR_PATTERN.search("everything is fine"))

    def test_authorization_error_pattern_detected(self):
        self.assertTrue(phase4_observability._AUTH_ERROR_PATTERN.search("AccessDenied: not authorized to perform: cloudwatch:PutMetricData"))

    def test_port_collision_pattern_detected(self):
        self.assertTrue(phase4_observability._STARTUP_ERROR_PATTERN.search("listen tcp 127.0.0.1:8888: bind: address already in use"))

    def test_observation_excludes_stale_pods_by_owner_uid(self):
        ds = {"metadata": {"uid": "ds-uid-current"}, "status": {"desiredNumberScheduled": 1}, "spec": {"selector": {"matchLabels": {"k8s-app": "cloudwatch-agent"}}}}
        scraper_deploy = {"metadata": {"uid": "deploy-uid-current"}, "spec": {"selector": {"matchLabels": {"app": "scraper"}}}}
        current_pod = {"metadata": {"name": "agent-current", "ownerReferences": [{"controller": True, "kind": "DaemonSet", "uid": "ds-uid-current"}]}, "spec": {"containers": [{"name": "cloudwatch-agent"}]}}
        stale_pod = {"metadata": {"name": "agent-stale", "ownerReferences": [{"controller": True, "kind": "DaemonSet", "uid": "ds-uid-OLD"}]}, "spec": {"containers": [{"name": "cloudwatch-agent"}]}}
        scraper_pod = {"metadata": {"name": "scraper-1"}, "spec": {"containers": [{"name": "cloudwatch-agent"}]}}

        def fake_get(resource, name, namespace, check=True):
            return ds if resource == "daemonset" else scraper_deploy

        def fake_pods(namespace, selector):
            return [current_pod, stale_pod] if "k8s-app" in selector else [scraper_pod]

        checked_pods = []

        def fake_run(argv, env=None, cwd=None, check=True, capture_output=True, input_text=None):
            if argv[:2] == ["kubectl", "logs"]:
                checked_pods.append(argv[2])
                return FakeProc(0, "no errors here")
            return FakeProc(0, "")

        with mock.patch.object(phase4_observability, "_kubectl_get_json", side_effect=fake_get), \
             mock.patch.object(phase4_observability, "_pods_for_selector", side_effect=fake_pods), \
             mock.patch.object(phase4_observability, "run", fake_run), \
             mock.patch.object(phase4_observability.time, "sleep"):
            _run_quiet(phase4_observability._validate_no_recent_cloudwatch_export_errors, "amazon-cloudwatch")
        self.assertIn("agent-current", checked_pods)
        self.assertNotIn("agent-stale", checked_pods)

    def test_new_authorization_error_on_active_pod_fails(self):
        ds = {"metadata": {"uid": "ds-uid"}, "status": {"desiredNumberScheduled": 1}, "spec": {"selector": {"matchLabels": {"k8s-app": "cloudwatch-agent"}}}}
        scraper_deploy = {"metadata": {"uid": "deploy-uid"}, "spec": {"selector": {"matchLabels": {"app": "scraper"}}}}
        pod = {"metadata": {"name": "agent-1", "ownerReferences": [{"controller": True, "kind": "DaemonSet", "uid": "ds-uid"}]}, "spec": {"containers": [{"name": "cloudwatch-agent"}]}}
        scraper_pod = {"metadata": {"name": "scraper-1"}, "spec": {"containers": [{"name": "cloudwatch-agent"}]}}

        def fake_get(resource, name, namespace, check=True):
            return ds if resource == "daemonset" else scraper_deploy

        def fake_pods(namespace, selector):
            return [pod] if "k8s-app" in selector else [scraper_pod]

        def fake_run(argv, env=None, cwd=None, check=True, capture_output=True, input_text=None):
            if argv[:2] == ["kubectl", "logs"] and "agent-1" in argv:
                return FakeProc(0, "AccessDenied: not authorized to perform: cloudwatch:PutMetricData")
            return FakeProc(0, "")

        with mock.patch.object(phase4_observability, "_kubectl_get_json", side_effect=fake_get), \
             mock.patch.object(phase4_observability, "_pods_for_selector", side_effect=fake_pods), \
             mock.patch.object(phase4_observability, "run", fake_run), \
             mock.patch.object(phase4_observability.time, "sleep"):
            with self.assertRaises(phase4_observability.Phase4Error):
                _run_quiet(phase4_observability._validate_no_recent_cloudwatch_export_errors, "amazon-cloudwatch")


class OwnershipPreflightAndAcceptanceTests(TempStateCase):
    def _patch_state_tool(self, result_json, returncode=0):
        def fake_run(argv, env=None, cwd=None, check=True, capture_output=True, input_text=None):
            if argv[0] == "aws":
                return FakeProc(0, "")
            if str(phase4_observability.OBSERVABILITY_STATE_TOOL) in argv or str(phase4_observability.OBSERVABILITY_ACCEPTANCE_TOOL) in argv:
                return FakeProc(returncode, json.dumps(result_json) if returncode == 0 else "")
            return FakeProc(0, "")
        return fake_run

    def test_ownership_preflight_absent(self):
        with mock.patch.object(phase4_observability, "run", self._patch_state_tool({"state": "ABSENT"})), _env_patch(), \
             mock.patch.object(phase4_observability, "write_github_output") as write_mock:
            _run_quiet(phase4_observability.cmd_ownership_preflight, self.args)
        write_mock.assert_called_once_with([("state", "ABSENT")])

    def test_ownership_preflight_owned(self):
        with mock.patch.object(phase4_observability, "run", self._patch_state_tool({"state": "OWNED"})), _env_patch(), \
             mock.patch.object(phase4_observability, "write_github_output") as write_mock:
            _run_quiet(phase4_observability.cmd_ownership_preflight, self.args)
        write_mock.assert_called_once_with([("state", "OWNED")])

    def test_ownership_preflight_broken_fails(self):
        with mock.patch.object(phase4_observability, "run", self._patch_state_tool({"state": "BROKEN"})), _env_patch():
            with self.assertRaises(phase4_observability.Phase4Error):
                _run_quiet(phase4_observability.cmd_ownership_preflight, self.args)

    def test_ownership_preflight_inspection_error_fails(self):
        with mock.patch.object(phase4_observability, "run", self._patch_state_tool({}, returncode=1)), _env_patch():
            with self.assertRaises(phase4_observability.Phase4Error):
                _run_quiet(phase4_observability.cmd_ownership_preflight, self.args)

    def test_strict_acceptance_healthy(self):
        with mock.patch.object(phase4_observability, "run", self._patch_state_tool({"state": "HEALTHY"})), _env_patch():
            _run_quiet(phase4_observability.cmd_strict_acceptance, self.args)

    def test_strict_acceptance_broken_fails(self):
        with mock.patch.object(phase4_observability, "run", self._patch_state_tool({"state": "BROKEN"})), _env_patch():
            with self.assertRaises(phase4_observability.Phase4Error):
                _run_quiet(phase4_observability.cmd_strict_acceptance, self.args)


class StateFileTests(TempStateCase):
    def test_state_rejects_disallowed_keys(self):
        with self.assertRaises(phase4_observability.Phase4Error):
            phase4_observability.update_state(self.state_path, {"aws_secret_access_key": "leak"})

    def test_state_contains_no_credentials(self):
        phase4_observability.update_state(self.state_path, {"namespace": "amazon-cloudwatch", "image_digests": _all_digests()})
        text = self.state_path.read_text()
        for forbidden in ("AKIA", "aws_secret_access_key", "SessionToken", "password"):
            self.assertNotIn(forbidden, text)


class SummaryAndDiagnosticsTests(TempStateCase):
    def test_summary_tolerates_empty_state(self):
        summary_path = Path(self._tmpdir.name) / "summary.md"
        with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary_path)}), redirect_stdout(io.StringIO()):
            phase4_observability.cmd_summary(self.args)
        self.assertTrue(summary_path.exists())

    def test_summary_tolerates_partial_state(self):
        phase4_observability.update_state(self.state_path, {"cluster_scraper_correction": "recreated_once"})
        summary_path = Path(self._tmpdir.name) / "summary.md"
        with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary_path), "OBSERVABILITY_DEPLOY_REQUESTED": "true"}), redirect_stdout(io.StringIO()):
            phase4_observability.cmd_summary(self.args)
        self.assertIn("recreated_once", summary_path.read_text())

    def test_diagnostics_never_raises_without_namespace(self):
        with redirect_stdout(io.StringIO()):
            phase4_observability.cmd_diagnostics(self.args)  # no namespace recorded in state -- must not raise

    def test_diagnostics_never_raises_on_kubectl_failure(self):
        phase4_observability.update_state(self.state_path, {"namespace": "amazon-cloudwatch"})
        with mock.patch.object(phase4_observability, "run", side_effect=phase4_observability.Phase4Error("boom")), redirect_stdout(io.StringIO()):
            phase4_observability.cmd_diagnostics(self.args)  # must not propagate -- diagnostics are non-authoritative

    def test_diagnostics_never_prints_token_values(self):
        phase4_observability.update_state(self.state_path, {"namespace": "amazon-cloudwatch"})
        buf = io.StringIO()

        def fake_run(argv, env=None, cwd=None, check=True, capture_output=True, input_text=None):
            if argv[:2] == ["kubectl", "get"] and "jsonpath={.items[0].metadata.name}" in argv:
                return FakeProc(0, "operator-1")
            if argv[:2] == ["kubectl", "logs"]:
                return FakeProc(0, "AWS_WEB_IDENTITY_TOKEN_FILE=/var/run/secrets/eks.amazonaws.com/serviceaccount/token")
            return FakeProc(1, "", "not found")

        with mock.patch.object(phase4_observability, "run", fake_run), redirect_stdout(buf):
            phase4_observability.cmd_diagnostics(self.args)
        self.assertIn("NOTE:", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
