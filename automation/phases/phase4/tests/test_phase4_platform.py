"""Offline tests for automation/phases/phase4/phase4_platform.py; run directly via `python3 automation/phases/phase4/tests/test_phase4_platform.py`. No live AWS/Kubernetes/Helm -- every subprocess call is intercepted via a scripted fake that asserts on the exact argv and returns a fabricated result. Covers: FLUENT_BIT_IMAGE format validation (reused from platform_acceptance.py, never duplicated), rendered-manifest structural validation, the ECR fail-closed repository/policy classification (including the exact bug this phase fixes), ownership-preflight/strict-acceptance delegation, and state-file credential-freedom."""
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

REPO_ROOT = Path(__file__).resolve().parents[4]
TOOL_PATH = REPO_ROOT / "automation" / "phases" / "phase4" / "phase4_platform.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("phase4_platform", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


phase4_platform = _load_tool()

ENVIRONMENT = "dev"
ECR_REGISTRY = "229410149234.dkr.ecr.eu-west-1.amazonaws.com"
ECR_ACCOUNT_ID = "229410149234"
FLUENT_BIT_DIGEST = "a" * 64
FLUENT_BIT_IMAGE = f"{ECR_REGISTRY}/aws-cloud-factory-fluent-bit@sha256:{FLUENT_BIT_DIGEST}"
RUNTIME_ROLE_ARN = "arn:aws:iam::668311715351:role/GoldenGateSecretsReadRole-dev"
PLATFORM_LOGGING_ROLE_ARN = "arn:aws:iam::668311715351:role/GoldenGatePlatformLoggingRole-dev"
ARGOCD_ECR_READ_ROLE_ARN = "arn:aws:iam::229410149234:role/ArgoCdEcrReadRole"


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class ScriptedRun:
    """Replaces phase4_platform.run with a scripted responder: a list of (predicate, FakeProc) pairs consulted in order, falling back to a default success. Every unmatched call is recorded for assertion."""

    def __init__(self, default=None):
        self.rules = []
        self.calls = []
        self.default = default if default is not None else FakeProc(0, "", "")

    def when(self, predicate, proc):
        self.rules.append((predicate, proc))
        return self

    def __call__(self, argv, env=None, cwd=None, check=True, capture_output=True, input_text=None):
        self.calls.append({"argv": list(argv), "env": env, "input_text": input_text})
        # Capture any file://-referenced temp file's content NOW -- production code deletes such temp files (e.g. the ECR repository-policy document) right after this call returns.
        for arg in argv:
            if isinstance(arg, str) and arg.startswith("file://") and Path(arg[len("file://"):]).is_file():
                self.calls[-1]["file_contents"] = Path(arg[len("file://"):]).read_text()
        # Later .when() registrations take precedence over earlier ones (e.g. a test-specific override registered after a shared base rule).
        for predicate, proc in reversed(self.rules):
            if predicate(argv):
                if check and proc.returncode != 0:
                    raise phase4_platform.Phase4Error(f"{' '.join(str(a) for a in argv)} failed: {proc.stdout}\n{proc.stderr}")
                return proc
        if check and self.default.returncode != 0:
            raise phase4_platform.Phase4Error(f"{' '.join(str(a) for a in argv)} failed: {self.default.stdout}\n{self.default.stderr}")
        return self.default


def _starts_with(*prefix):
    return lambda argv: list(argv[:len(prefix)]) == list(prefix)


class TempStateCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self._tmpdir.name) / "state.json"
        self.args = argparse_namespace(environment=ENVIRONMENT, state_path=self.state_path)

    def tearDown(self):
        self._tmpdir.cleanup()


class argparse_namespace:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _env_patch(**overrides):
    base = {
        "AWS_REGION": "eu-west-1",
        "EKS_CLUSTER_NAME": "gg-dev-cluster",
        "EKS_DEPLOY_ROLE_ARN": "arn:aws:iam::668311715351:role/GoldenGateEksDeployRole-dev",
        "ECR_REGISTRY": ECR_REGISTRY,
        "ECR_ACCOUNT_ID": ECR_ACCOUNT_ID,
        "GITHUB_RUN_NUMBER": "42",
        "FLUENT_BIT_IMAGE": FLUENT_BIT_IMAGE,
        "RUNTIME_NAMESPACE": "goldengate-dev",
        "MONITOR_NAMESPACE": "goldengate-monitor",
        "RUNTIME_ROLE_ARN": RUNTIME_ROLE_ARN,
        "PLATFORM_LOGGING_ROLE_ARN": PLATFORM_LOGGING_ROLE_ARN,
        "RUNTIME_LOG_GROUP": "/goldengate/dev/runtime",
        "MONITOR_LOG_GROUP": "/goldengate/dev/monitor",
        "GG_ENVIRONMENT": ENVIRONMENT,
        "ARGOCD_NAMESPACE": "argocd",
        "ARGOCD_ECR_READ_ROLE_ARN": ARGOCD_ECR_READ_ROLE_ARN,
    }
    base.update(overrides)
    return mock.patch.dict(os.environ, base, clear=False)


def _run_quiet(func, *args, **kwargs):
    with redirect_stdout(io.StringIO()):
        return func(*args, **kwargs)


class SafeTokenTests(unittest.TestCase):
    def test_unsafe_environment_rejected(self):
        with self.assertRaises(phase4_platform.Phase4Error):
            phase4_platform.require_environment_arg("dev; rm -rf /")

    def test_safe_environment_accepted(self):
        self.assertEqual(phase4_platform.require_environment_arg("dev"), "dev")


class FluentBitImageFormatTests(unittest.TestCase):
    def test_missing_image_is_configuration_error(self):
        with self.assertRaises(phase4_platform.Phase4Error):
            phase4_platform._validate_fluent_bit_image_format("", ECR_REGISTRY)

    def test_public_registry_image_fails(self):
        with self.assertRaises(phase4_platform.Phase4Error):
            phase4_platform._validate_fluent_bit_image_format("public.ecr.aws/x/aws-cloud-factory-fluent-bit@sha256:" + "a" * 64, ECR_REGISTRY)

    def test_tag_only_image_fails(self):
        with self.assertRaises(phase4_platform.Phase4Error):
            phase4_platform._validate_fluent_bit_image_format(f"{ECR_REGISTRY}/aws-cloud-factory-fluent-bit:latest", ECR_REGISTRY)

    def test_malformed_digest_fails(self):
        with self.assertRaises(phase4_platform.Phase4Error):
            phase4_platform._validate_fluent_bit_image_format(f"{ECR_REGISTRY}/aws-cloud-factory-fluent-bit@sha256:not-hex", ECR_REGISTRY)

    def test_wrong_repository_fails(self):
        with self.assertRaises(phase4_platform.Phase4Error):
            phase4_platform._validate_fluent_bit_image_format(f"{ECR_REGISTRY}/some-other-repo@sha256:{'a' * 64}", ECR_REGISTRY)

    def test_exact_private_digest_accepted(self):
        phase4_platform._validate_fluent_bit_image_format(FLUENT_BIT_IMAGE, ECR_REGISTRY)


def _minimal_valid_docs(fluent_bit_image=FLUENT_BIT_IMAGE, runtime_namespace="goldengate-dev"):
    return [
        {
            "kind": "ServiceAccount",
            "metadata": {"name": phase4_platform.RUNTIME_SA_NAME, "namespace": runtime_namespace,
                         "annotations": {"eks.amazonaws.com/role-arn": RUNTIME_ROLE_ARN, "argocd.argoproj.io/sync-options": "Prune=false,Delete=false"}},
        },
        {
            "kind": "ServiceAccount",
            "metadata": {"name": phase4_platform.FLUENT_BIT_SA_NAME, "namespace": runtime_namespace,
                         "annotations": {"eks.amazonaws.com/role-arn": PLATFORM_LOGGING_ROLE_ARN}},
        },
        {
            "kind": "DaemonSet",
            "metadata": {"name": phase4_platform.FLUENT_BIT_DAEMONSET_NAME},
            "spec": {"template": {"spec": {
                "hostNetwork": False,
                "volumes": [
                    {"name": "varlog", "hostPath": {"path": "/var/log"}},
                    {"name": "fluent-bit-state", "emptyDir": {"sizeLimit": "50Mi"}},
                ],
                "containers": [{
                    "name": "fluent-bit", "image": fluent_bit_image,
                    "securityContext": {"privileged": False},
                    "volumeMounts": [{"name": "varlog", "readOnly": True}],
                }],
            }}},
        },
        {
            "kind": "ConfigMap",
            "metadata": {"name": phase4_platform.FLUENT_BIT_CONFIGMAP_NAME},
            "data": {"fluent-bit.conf": (
                "[INPUT]\n    Name              tail\n    Path              /var/log/containers/*_goldengate-dev_*.log\n    Tag               runtime.*\n"
                "[INPUT]\n    Name              tail\n    Path              /var/log/containers/*_goldengate-monitor_*.log\n    Tag               monitor.*\n"
                "[FILTER]\n    Name              kubernetes\n    Match             runtime.*\n    Kube_Tag_Prefix   runtime.var.log.containers.\n"
                "[FILTER]\n    Name              kubernetes\n    Match             monitor.*\n    Kube_Tag_Prefix   monitor.var.log.containers.\n"
                "[OUTPUT]\n    Name                    cloudwatch_logs\n    Match                   runtime.*\n    log_group_name          /goldengate/dev/runtime\n    auto_create_group       false\n    storage.total_limit_size 5M\n"
                "[OUTPUT]\n    Name                    cloudwatch_logs\n    Match                   monitor.*\n    log_group_name          /goldengate/dev/monitor\n    auto_create_group       false\n    storage.total_limit_size 5M\n"
            )},
        },
    ]


class RenderedManifestValidationTests(unittest.TestCase):
    def _write_rendered(self, docs):
        import yaml
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        yaml.safe_dump_all(docs, tmp)
        tmp.close()
        return Path(tmp.name)

    def test_zero_namespace_documents_required(self):
        docs = _minimal_valid_docs() + [{"kind": "Namespace", "metadata": {"name": "goldengate-dev"}}]
        with self.assertRaises(phase4_platform.Phase4Error):
            phase4_platform._validate_zero_namespace_documents(docs)

    def test_zero_namespace_documents_passes(self):
        _run_quiet(phase4_platform._validate_zero_namespace_documents, _minimal_valid_docs())

    def test_runtime_service_account_contract(self):
        docs = _minimal_valid_docs()
        _run_quiet(phase4_platform._validate_runtime_service_account, docs, phase4_platform.RUNTIME_SA_NAME, RUNTIME_ROLE_ARN, "goldengate-dev", True)

    def test_runtime_service_account_wrong_role_fails(self):
        docs = _minimal_valid_docs()
        with self.assertRaises(phase4_platform.Phase4Error):
            phase4_platform._validate_runtime_service_account(docs, phase4_platform.RUNTIME_SA_NAME, "arn:aws:iam::1:role/wrong", "goldengate-dev", True)

    def test_runtime_service_account_missing_deletion_protection_fails(self):
        docs = _minimal_valid_docs()
        docs[0]["metadata"]["annotations"].pop("argocd.argoproj.io/sync-options")
        with self.assertRaises(phase4_platform.Phase4Error):
            phase4_platform._validate_runtime_service_account(docs, phase4_platform.RUNTIME_SA_NAME, RUNTIME_ROLE_ARN, "goldengate-dev", True)

    def test_fluent_bit_service_account_contract(self):
        docs = _minimal_valid_docs()
        _run_quiet(phase4_platform._validate_runtime_service_account, docs, phase4_platform.FLUENT_BIT_SA_NAME, PLATFORM_LOGGING_ROLE_ARN, "goldengate-dev", False)

    def test_service_account_set_exact(self):
        _run_quiet(phase4_platform._validate_service_account_set, _minimal_valid_docs())

    def test_service_account_set_rejects_extra(self):
        docs = _minimal_valid_docs() + [{"kind": "ServiceAccount", "metadata": {"name": "unexpected-sa"}}]
        with self.assertRaises(phase4_platform.Phase4Error):
            phase4_platform._validate_service_account_set(docs)

    def test_no_unexpected_irsa_role(self):
        docs = _minimal_valid_docs()
        _run_quiet(phase4_platform._validate_no_unexpected_irsa_role, docs, {RUNTIME_ROLE_ARN, PLATFORM_LOGGING_ROLE_ARN})

    def test_unexpected_irsa_role_fails(self):
        docs = _minimal_valid_docs()
        docs[0]["metadata"]["annotations"]["eks.amazonaws.com/role-arn"] = "arn:aws:iam::999:role/rogue"
        with self.assertRaises(phase4_platform.Phase4Error):
            phase4_platform._validate_no_unexpected_irsa_role(docs, {RUNTIME_ROLE_ARN, PLATFORM_LOGGING_ROLE_ARN})

    def test_no_forbidden_kinds_passes(self):
        _run_quiet(phase4_platform._validate_no_forbidden_kinds, _minimal_valid_docs())

    def test_forbidden_kind_statefulset_fails(self):
        docs = _minimal_valid_docs() + [{"kind": "StatefulSet", "metadata": {"name": "gg-oracle-01"}}]
        with self.assertRaises(phase4_platform.Phase4Error):
            phase4_platform._validate_no_forbidden_kinds(docs)

    def test_fluent_bit_daemonset_required(self):
        docs = [d for d in _minimal_valid_docs() if d["kind"] != "DaemonSet"]
        with self.assertRaises(phase4_platform.Phase4Error):
            phase4_platform._validate_fluent_bit_daemonset_shape(docs, FLUENT_BIT_IMAGE)

    def test_fluent_bit_daemonset_shape_passes(self):
        _run_quiet(phase4_platform._validate_fluent_bit_daemonset_shape, _minimal_valid_docs(), FLUENT_BIT_IMAGE)

    def test_fluent_bit_daemonset_exact_image(self):
        with self.assertRaises(phase4_platform.Phase4Error):
            phase4_platform._validate_fluent_bit_daemonset_shape(_minimal_valid_docs(fluent_bit_image=f"{ECR_REGISTRY}/aws-cloud-factory-fluent-bit@sha256:{'b' * 64}"), FLUENT_BIT_IMAGE)

    def test_fluent_bit_daemonset_privileged_fails(self):
        docs = _minimal_valid_docs()
        docs[2]["spec"]["template"]["spec"]["containers"][0]["securityContext"]["privileged"] = True
        with self.assertRaises(phase4_platform.Phase4Error):
            phase4_platform._validate_fluent_bit_daemonset_shape(docs, FLUENT_BIT_IMAGE)

    def test_fluent_bit_configmap_required(self):
        docs = [d for d in _minimal_valid_docs() if d["kind"] != "ConfigMap"]
        with self.assertRaises(phase4_platform.Phase4Error):
            phase4_platform._validate_fluent_bit_configmap(docs, "goldengate-dev", "goldengate-monitor", "/goldengate/dev/runtime", "/goldengate/dev/monitor")

    def test_fluent_bit_configmap_exact_namespace_routing(self):
        _run_quiet(phase4_platform._validate_fluent_bit_configmap, _minimal_valid_docs(), "goldengate-dev", "goldengate-monitor", "/goldengate/dev/runtime", "/goldengate/dev/monitor")

    def test_fluent_bit_configmap_wrong_namespace_routing_fails(self):
        with self.assertRaises(phase4_platform.Phase4Error):
            phase4_platform._validate_fluent_bit_configmap(_minimal_valid_docs(), "some-other-ns", "goldengate-monitor", "/goldengate/dev/runtime", "/goldengate/dev/monitor")

    def test_fluent_bit_configmap_exact_log_groups(self):
        with self.assertRaises(phase4_platform.Phase4Error):
            phase4_platform._validate_fluent_bit_configmap(_minimal_valid_docs(), "goldengate-dev", "goldengate-monitor", "/wrong/log/group", "/goldengate/dev/monitor")

    def test_fluent_bit_configmap_rejects_unrestricted_wildcard(self):
        docs = _minimal_valid_docs()
        docs[3]["data"]["fluent-bit.conf"] += "\nPath              /var/log/containers/*.log\n"
        with self.assertRaises(phase4_platform.Phase4Error):
            phase4_platform._validate_fluent_bit_configmap(docs, "goldengate-dev", "goldengate-monitor", "/goldengate/dev/runtime", "/goldengate/dev/monitor")

    def test_no_unresolved_placeholders_passes(self):
        rendered_path = self._write_rendered(_minimal_valid_docs())
        try:
            _run_quiet(phase4_platform._validate_no_unresolved_placeholders, rendered_path)
        finally:
            rendered_path.unlink()

    def test_unresolved_placeholder_fails(self):
        rendered_path = self._write_rendered(_minimal_valid_docs())
        try:
            with rendered_path.open("a") as f:
                f.write("\n# leftover: <no value>\n")
            with self.assertRaises(phase4_platform.Phase4Error):
                phase4_platform._validate_no_unresolved_placeholders(rendered_path)
        finally:
            rendered_path.unlink()

    def test_public_registry_in_rendered_manifest_fails(self):
        rendered_path = self._write_rendered(_minimal_valid_docs())
        try:
            with rendered_path.open("a") as f:
                f.write("\n# image: public.ecr.aws/foo/bar:latest\n")
            with self.assertRaises(phase4_platform.Phase4Error):
                phase4_platform._validate_no_public_registry_references(rendered_path)
        finally:
            rendered_path.unlink()

    def test_no_fluent_bit_sidecar_in_runtime_chart(self):
        _run_quiet(phase4_platform._validate_no_fluent_bit_sidecar_in_runtime_chart)


class FluentBitEcrPreflightTests(TempStateCase):
    """VDR live-run correction: state now always stores the CANONICAL ECR digest form (sha256:<64hex>) that cmd_prepare_and_validate() actually produces via _derive_fluent_bit_ecr_digest() -- the previous bare-hex state shape here was itself the bug (it made aws ecr describe-images --image-ids imageDigest=<64hex> fail ECR's own regex with InvalidParameterException in the live VDR run)."""

    CANONICAL_DIGEST = f"sha256:{FLUENT_BIT_DIGEST}"

    def setUp(self):
        super().setUp()
        phase4_platform.update_state(self.state_path, {
            "fluent_bit_ecr_repository": "aws-cloud-factory-fluent-bit",
            "fluent_bit_ecr_digest": self.CANONICAL_DIGEST,
        })

    # A + C: a valid canonical (sha256:<64hex>) state digest, with ECR describe-images returning the matching canonical imageDigest, results in successful verification -- this is the test that PREVIOUSLY was named "passes" but actually asserted Phase4Error, codifying the live-VDR bug.
    def test_digest_exists_passes(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "get-caller-identity"), FakeProc(0, ECR_ACCOUNT_ID + "\n"))
        scripted.when(_starts_with("aws", "ecr", "describe-images"), FakeProc(0, json.dumps({"imageDetails": [{"imageDigest": self.CANONICAL_DIGEST}]})))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            _run_quiet(phase4_platform.cmd_verify_fluent_bit_artifact, self.args)
        describe_calls = [c for c in scripted.calls if c["argv"][:3] == ["aws", "ecr", "describe-images"]]
        self.assertEqual(len(describe_calls), 1)

    # B: the exact mocked AWS argv must contain the canonical "imageDigest=sha256:<64hex>" form, never the bare-hex "imageDigest=<64hex>" that caused the live VDR InvalidParameterException.
    def test_verify_uses_canonical_image_digest_argv_never_bare_hex(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "get-caller-identity"), FakeProc(0, ECR_ACCOUNT_ID + "\n"))
        scripted.when(_starts_with("aws", "ecr", "describe-images"), FakeProc(0, json.dumps({"imageDetails": [{"imageDigest": self.CANONICAL_DIGEST}]})))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            _run_quiet(phase4_platform.cmd_verify_fluent_bit_artifact, self.args)
        describe_call = next(c for c in scripted.calls if c["argv"][:3] == ["aws", "ecr", "describe-images"])
        self.assertIn(f"imageDigest={self.CANONICAL_DIGEST}", describe_call["argv"])
        self.assertNotIn(f"imageDigest={FLUENT_BIT_DIGEST}", describe_call["argv"])

    # D: a wrong (mismatched) digest returned by ECR still fails closed, even though the request itself succeeded structurally.
    def test_wrong_returned_digest_fails_closed(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "get-caller-identity"), FakeProc(0, ECR_ACCOUNT_ID + "\n"))
        scripted.when(_starts_with("aws", "ecr", "describe-images"), FakeProc(0, json.dumps({"imageDetails": [{"imageDigest": f"sha256:{'b' * 64}"}]})))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error) as ctx:
                _run_quiet(phase4_platform.cmd_verify_fluent_bit_artifact, self.args)
        self.assertIn("ECR returned digest", str(ctx.exception))

    # F: producer -> consumer regression proof -- the REAL _derive_fluent_bit_ecr_digest() producer function's output is written to the real state-file boundary and consumed UNCHANGED by the REAL cmd_verify_fluent_bit_artifact(), never a value hand-computed/reimplemented in the test. This is exactly the seam that drifted in the live VDR failure (producer wrote bare hex, consumer sent it to AWS as-is) -- this test fails if the two ever disagree again.
    def test_producer_consumer_digest_contract_never_drifts(self):
        produced_digest = phase4_platform._derive_fluent_bit_ecr_digest(FLUENT_BIT_IMAGE, ECR_REGISTRY)
        self.assertTrue(produced_digest.startswith("sha256:"), f"producer must emit the canonical sha256:<hex> form, got {produced_digest!r}")

        state = phase4_platform.load_state(self.state_path)
        state["fluent_bit_ecr_digest"] = produced_digest
        phase4_platform.save_state(self.state_path, state)

        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "get-caller-identity"), FakeProc(0, ECR_ACCOUNT_ID + "\n"))
        scripted.when(_starts_with("aws", "ecr", "describe-images"), FakeProc(0, json.dumps({"imageDetails": [{"imageDigest": produced_digest}]})))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            _run_quiet(phase4_platform.cmd_verify_fluent_bit_artifact, self.args)
        describe_call = next(c for c in scripted.calls if c["argv"][:3] == ["aws", "ecr", "describe-images"])
        self.assertIn(f"imageDigest={produced_digest}", describe_call["argv"])

    # E: RepositoryNotFound/ImageNotFound/unknown AWS errors and wrong caller account all retain their existing fail-closed behavior against the canonical state shape.
    def test_repository_not_found_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "get-caller-identity"), FakeProc(0, ECR_ACCOUNT_ID + "\n"))
        scripted.when(_starts_with("aws", "ecr", "describe-images"), FakeProc(1, "", "An error occurred (RepositoryNotFoundException)"))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error) as ctx:
                _run_quiet(phase4_platform.cmd_verify_fluent_bit_artifact, self.args)
            self.assertIn("not found", str(ctx.exception))

    def test_image_not_found_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "get-caller-identity"), FakeProc(0, ECR_ACCOUNT_ID + "\n"))
        scripted.when(_starts_with("aws", "ecr", "describe-images"), FakeProc(1, "", "An error occurred (ImageNotFoundException)"))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error):
                _run_quiet(phase4_platform.cmd_verify_fluent_bit_artifact, self.args)

    def test_unknown_describe_images_error_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "get-caller-identity"), FakeProc(0, ECR_ACCOUNT_ID + "\n"))
        scripted.when(_starts_with("aws", "ecr", "describe-images"), FakeProc(1, "", "InternalServiceError"))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error):
                _run_quiet(phase4_platform.cmd_verify_fluent_bit_artifact, self.args)

    def test_wrong_build_account_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "get-caller-identity"), FakeProc(0, "999999999999\n"))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error):
                _run_quiet(phase4_platform.cmd_verify_fluent_bit_artifact, self.args)

    def test_digest_matches_with_prefix_passes(self):
        state = phase4_platform.load_state(self.state_path)
        state["fluent_bit_ecr_digest"] = f"sha256:{FLUENT_BIT_DIGEST}"
        phase4_platform.save_state(self.state_path, state)
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "get-caller-identity"), FakeProc(0, ECR_ACCOUNT_ID + "\n"))
        scripted.when(_starts_with("aws", "ecr", "describe-images"), FakeProc(0, json.dumps({"imageDetails": [{"imageDigest": f"sha256:{FLUENT_BIT_DIGEST}"}]})))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            _run_quiet(phase4_platform.cmd_verify_fluent_bit_artifact, self.args)


class EcrRepositoryFailClosedTests(unittest.TestCase):
    """Reproduces (and fixes) the exact known Phase 4 bug: the old `if describe; then exists; else create; fi` pattern treated every non-zero describe result as absence."""

    def test_reproduce_old_fail_open_bug_pattern(self):
        # Demonstrates the OLD unsafe bash pattern's behavior for contrast -- an AccessDenied describe would fall into the `else` branch and call create-repository. This is exactly what _ensure_ecr_repository below must NOT do.
        def old_unsafe_logic(describe_rc):
            return "create" if describe_rc != 0 else "exists"

        self.assertEqual(old_unsafe_logic(255), "create")  # the bug: AccessDenied (rc != 0) triggers create

    def test_describe_success_means_no_create(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "ecr", "describe-repositories"), FakeProc(0, "{}"))
        with mock.patch.object(phase4_platform, "run", scripted):
            _run_quiet(phase4_platform._ensure_ecr_repository, "helm/goldengate-platform", "eu-west-1")
        create_calls = [c for c in scripted.calls if c["argv"][:3] == ["aws", "ecr", "create-repository"]]
        self.assertEqual(create_calls, [])

    def test_explicit_repository_not_found_creates(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "ecr", "describe-repositories"), FakeProc(1, "", "An error occurred (RepositoryNotFoundException)"))
        scripted.when(_starts_with("aws", "ecr", "create-repository"), FakeProc(0, "{}"))
        with mock.patch.object(phase4_platform, "run", scripted):
            _run_quiet(phase4_platform._ensure_ecr_repository, "helm/goldengate-platform", "eu-west-1")
        create_calls = [c for c in scripted.calls if c["argv"][:3] == ["aws", "ecr", "create-repository"]]
        self.assertEqual(len(create_calls), 1)

    def _assert_fails_with_zero_create_calls(self, stderr_text):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "ecr", "describe-repositories"), FakeProc(255, "", stderr_text))
        with mock.patch.object(phase4_platform, "run", scripted):
            with self.assertRaises(phase4_platform.Phase4Error):
                _run_quiet(phase4_platform._ensure_ecr_repository, "helm/goldengate-platform", "eu-west-1")
        create_calls = [c for c in scripted.calls if c["argv"][:3] == ["aws", "ecr", "create-repository"]]
        self.assertEqual(create_calls, [], f"a create-repository call leaked through for error: {stderr_text}")

    def test_access_denied_fails_with_no_create(self):
        self._assert_fails_with_zero_create_calls("An error occurred (AccessDeniedException)")

    def test_expired_token_fails_with_no_create(self):
        self._assert_fails_with_zero_create_calls("An error occurred (ExpiredTokenException)")

    def test_invalid_client_token_fails_with_no_create(self):
        self._assert_fails_with_zero_create_calls("An error occurred (InvalidClientTokenId)")

    def test_unrecognized_client_fails_with_no_create(self):
        self._assert_fails_with_zero_create_calls("An error occurred (UnrecognizedClientException)")

    def test_throttling_fails_with_no_create(self):
        self._assert_fails_with_zero_create_calls("An error occurred (ThrottlingException)")

    def test_network_timeout_fails_with_no_create(self):
        self._assert_fails_with_zero_create_calls("connect timeout")

    def test_empty_error_fails_with_no_create(self):
        self._assert_fails_with_zero_create_calls("")

    def test_unknown_error_fails_with_no_create(self):
        self._assert_fails_with_zero_create_calls("some entirely unexpected failure text")

    def test_exact_create_settings(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "ecr", "describe-repositories"), FakeProc(1, "", "RepositoryNotFoundException"))
        scripted.when(_starts_with("aws", "ecr", "create-repository"), FakeProc(0, "{}"))
        with mock.patch.object(phase4_platform, "run", scripted):
            _run_quiet(phase4_platform._ensure_ecr_repository, "helm/goldengate-platform", "eu-west-1")
        create_call = next(c for c in scripted.calls if c["argv"][:3] == ["aws", "ecr", "create-repository"])
        argv = create_call["argv"]
        self.assertIn("helm/goldengate-platform", argv)
        self.assertIn("Key=ApplicationName,Value=CloudFactory", argv)
        self.assertIn("Key=DataClassification,Value=General", argv)
        self.assertIn("Key=BusinessCriticality,Value=Low", argv)
        self.assertIn("Key=BusinessUnit,Value=TechnologyPlatform", argv)
        self.assertIn("Key=CostCenter,Value=219", argv)
        self.assertIn("scanOnPush=true", argv)
        self.assertIn("MUTABLE", argv)

    def test_repository_already_exists_race_requires_fresh_describe(self):
        describe_calls = {"n": 0}

        def describe_predicate(argv):
            return argv[:3] == ["aws", "ecr", "describe-repositories"]

        scripted = ScriptedRun()

        def responder(argv, env=None, cwd=None, check=True, capture_output=True, input_text=None):
            scripted.calls.append({"argv": list(argv), "env": env, "input_text": input_text})
            if describe_predicate(argv):
                describe_calls["n"] += 1
                if describe_calls["n"] == 1:
                    if check:
                        raise phase4_platform.Phase4Error("RepositoryNotFoundException")
                    return FakeProc(1, "", "RepositoryNotFoundException")
                return FakeProc(0, "{}")
            if argv[:3] == ["aws", "ecr", "create-repository"]:
                raise phase4_platform.Phase4Error("An error occurred (RepositoryAlreadyExistsException)")
            return FakeProc(0, "")

        with mock.patch.object(phase4_platform, "run", responder):
            _run_quiet(phase4_platform._ensure_ecr_repository, "helm/goldengate-platform", "eu-west-1")
        self.assertEqual(describe_calls["n"], 2)


class EcrRepositoryPolicyTests(unittest.TestCase):
    def test_policy_not_found_initializes_empty(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "ecr", "get-repository-policy"), FakeProc(1, "", "An error occurred (RepositoryPolicyNotFoundException)"))
        scripted.when(_starts_with("aws", "ecr", "set-repository-policy"), FakeProc(0, "{}"))
        with mock.patch.object(phase4_platform, "run", scripted):
            _run_quiet(phase4_platform._ensure_ecr_repository_policy, "helm/goldengate-platform", "eu-west-1", ARGOCD_ECR_READ_ROLE_ARN)
        set_call = next(c for c in scripted.calls if c["argv"][:3] == ["aws", "ecr", "set-repository-policy"])
        policy = json.loads(set_call["file_contents"])
        sids = [s["Sid"] for s in policy["Statement"]]
        self.assertIn(phase4_platform.ARGOCD_ECR_STATEMENT_SID, sids)

    def test_access_denied_on_get_policy_fails_closed(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "ecr", "get-repository-policy"), FakeProc(1, "", "An error occurred (AccessDeniedException)"))
        with mock.patch.object(phase4_platform, "run", scripted):
            with self.assertRaises(phase4_platform.Phase4Error):
                _run_quiet(phase4_platform._ensure_ecr_repository_policy, "helm/goldengate-platform", "eu-west-1", ARGOCD_ECR_READ_ROLE_ARN)
        set_calls = [c for c in scripted.calls if c["argv"][:3] == ["aws", "ecr", "set-repository-policy"]]
        self.assertEqual(set_calls, [])

    def test_merge_preserves_unrelated_statements(self):
        existing_policy = {"Version": "2012-10-17", "Statement": [{"Sid": "SomeUnrelatedStatement", "Effect": "Allow", "Principal": {"AWS": "arn:aws:iam::1:role/other"}, "Action": ["ecr:GetDownloadUrlForLayer"]}]}
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "ecr", "get-repository-policy"), FakeProc(0, json.dumps(existing_policy)))
        scripted.when(_starts_with("aws", "ecr", "set-repository-policy"), FakeProc(0, "{}"))
        with mock.patch.object(phase4_platform, "run", scripted):
            _run_quiet(phase4_platform._ensure_ecr_repository_policy, "helm/goldengate-platform", "eu-west-1", ARGOCD_ECR_READ_ROLE_ARN)
        set_call = next(c for c in scripted.calls if c["argv"][:3] == ["aws", "ecr", "set-repository-policy"])
        policy = json.loads(set_call["file_contents"])
        sids = [s["Sid"] for s in policy["Statement"]]
        self.assertIn("SomeUnrelatedStatement", sids)
        self.assertIn(phase4_platform.ARGOCD_ECR_STATEMENT_SID, sids)

    def test_exact_argo_pull_statement(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "ecr", "get-repository-policy"), FakeProc(1, "", "RepositoryPolicyNotFoundException"))
        scripted.when(_starts_with("aws", "ecr", "set-repository-policy"), FakeProc(0, "{}"))
        with mock.patch.object(phase4_platform, "run", scripted):
            _run_quiet(phase4_platform._ensure_ecr_repository_policy, "helm/goldengate-platform", "eu-west-1", ARGOCD_ECR_READ_ROLE_ARN)
        set_call = next(c for c in scripted.calls if c["argv"][:3] == ["aws", "ecr", "set-repository-policy"])
        policy = json.loads(set_call["file_contents"])
        statement = next(s for s in policy["Statement"] if s["Sid"] == phase4_platform.ARGOCD_ECR_STATEMENT_SID)
        self.assertEqual(statement["Principal"]["AWS"], ARGOCD_ECR_READ_ROLE_ARN)
        self.assertEqual(sorted(statement["Action"]), sorted(phase4_platform.REPOSITORY_PULL_ACTIONS))


class PublishChartTests(TempStateCase):
    def setUp(self):
        super().setUp()
        self.package_path = REPO_ROOT / "packaged" / "goldengate-platform-0.1.42.tgz"
        phase4_platform.update_state(self.state_path, {
            "chart_version": "0.1.42",
            "package_path": "packaged/goldengate-platform-0.1.42.tgz",
            "helm_push_url": f"oci://{ECR_REGISTRY}/helm",
            "helm_ecr_repository": "helm/goldengate-platform",
            "helm_chart_ref": f"oci://{ECR_REGISTRY}/helm/goldengate-platform",
        })

    def test_ecr_password_passed_only_via_stdin(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "ecr", "get-login-password"), FakeProc(0, "super-secret-password\n"))
        scripted.when(_starts_with("aws", "ecr", "describe-repositories"), FakeProc(0, "{}"))
        scripted.when(_starts_with("aws", "ecr", "get-repository-policy"), FakeProc(1, "", "RepositoryPolicyNotFoundException"))
        scripted.when(_starts_with("aws", "ecr", "set-repository-policy"), FakeProc(0, "{}"))
        scripted.when(_starts_with("helm", "push"), FakeProc(0, ""))
        scripted.when(_starts_with("helm", "pull"), FakeProc(0, ""))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            _run_quiet(phase4_platform.cmd_publish_chart, self.args)
        login_call = next(c for c in scripted.calls if c["argv"][:2] == ["helm", "registry"])
        self.assertNotIn("super-secret-password", login_call["argv"])
        self.assertEqual(login_call["input_text"], "super-secret-password")
        for call in scripted.calls:
            self.assertNotIn("super-secret-password", " ".join(str(a) for a in call["argv"]))

    def test_password_never_stored_in_state(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "ecr", "get-login-password"), FakeProc(0, "super-secret-password\n"))
        scripted.when(_starts_with("aws", "ecr", "describe-repositories"), FakeProc(0, "{}"))
        scripted.when(_starts_with("aws", "ecr", "get-repository-policy"), FakeProc(1, "", "RepositoryPolicyNotFoundException"))
        scripted.when(_starts_with("aws", "ecr", "set-repository-policy"), FakeProc(0, "{}"))
        scripted.when(_starts_with("helm", "push"), FakeProc(0, ""))
        scripted.when(_starts_with("helm", "pull"), FakeProc(0, ""))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            _run_quiet(phase4_platform.cmd_publish_chart, self.args)
        state_text = self.state_path.read_text()
        self.assertNotIn("super-secret-password", state_text)

    def test_chart_push_failure_fails(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "ecr", "get-login-password"), FakeProc(0, "pw\n"))
        scripted.when(_starts_with("aws", "ecr", "describe-repositories"), FakeProc(0, "{}"))
        scripted.when(_starts_with("aws", "ecr", "get-repository-policy"), FakeProc(1, "", "RepositoryPolicyNotFoundException"))
        scripted.when(_starts_with("aws", "ecr", "set-repository-policy"), FakeProc(0, "{}"))
        scripted.when(_starts_with("helm", "push"), FakeProc(1, "", "push failed"))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error):
                _run_quiet(phase4_platform.cmd_publish_chart, self.args)

    def test_exact_pulled_chart_verification(self):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "ecr", "get-login-password"), FakeProc(0, "pw\n"))
        scripted.when(_starts_with("aws", "ecr", "describe-repositories"), FakeProc(0, "{}"))
        scripted.when(_starts_with("aws", "ecr", "get-repository-policy"), FakeProc(1, "", "RepositoryPolicyNotFoundException"))
        scripted.when(_starts_with("aws", "ecr", "set-repository-policy"), FakeProc(0, "{}"))
        scripted.when(_starts_with("helm", "push"), FakeProc(0, ""))
        scripted.when(_starts_with("helm", "pull"), FakeProc(0, ""))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            _run_quiet(phase4_platform.cmd_publish_chart, self.args)
        pull_call = next(c for c in scripted.calls if c["argv"][:2] == ["helm", "pull"])
        self.assertIn(f"oci://{ECR_REGISTRY}/helm/goldengate-platform", pull_call["argv"])
        self.assertIn("0.1.42", pull_call["argv"])


class ReconcileClusterTests(TempStateCase):
    def setUp(self):
        super().setUp()
        phase4_platform.update_state(self.state_path, {
            "values_file": f"platform/{ENVIRONMENT}/goldengate-platform/values.yaml",
            "chart_version": "0.1.42",
            "helm_chart_ref": f"oci://{ECR_REGISTRY}/helm/goldengate-platform",
            "release_name": f"goldengate-{ENVIRONMENT}-platform",
            "argocd_app_name": f"goldengate-{ENVIRONMENT}-platform",
            "namespace": "goldengate-dev",
            "fluent_bit_image": FLUENT_BIT_IMAGE,
        })

    def _base_scripted(self, secret_url=None):
        scripted = ScriptedRun()
        scripted.when(_starts_with("aws", "sts", "get-caller-identity"), FakeProc(0, "{}"))
        scripted.when(_starts_with("aws", "eks", "update-kubeconfig"), FakeProc(0, ""))
        scripted.when(_starts_with("kubectl", "config"), FakeProc(0, ""))
        scripted.when(_starts_with("kubectl", "version"), FakeProc(0, ""))
        scripted.when(_starts_with("kubectl", "get", "crd"), FakeProc(0, ""))
        import base64 as b64
        url = secret_url if secret_url is not None else f"oci://{ECR_REGISTRY}/helm/goldengate-platform"
        scripted.when(lambda argv: argv[:4] == ["kubectl", "get", "secret", phase4_platform.ARGOCD_PLATFORM_SECRET_NAME] and "-o" not in argv, FakeProc(0, ""))
        scripted.when(lambda argv: argv[:4] == ["kubectl", "get", "secret", phase4_platform.ARGOCD_PLATFORM_SECRET_NAME] and "-o" in argv, FakeProc(0, b64.b64encode(url.encode()).decode()))
        scripted.when(_starts_with("kubectl", "apply"), FakeProc(0, ""))
        scripted.when(_starts_with("kubectl", "annotate"), FakeProc(0, ""))
        return scripted

    def test_update_kubeconfig_exact_role_semantics(self):
        scripted = self._base_scripted()
        scripted.when(_starts_with("kubectl", "get", "application"), FakeProc(0, ""))
        scripted.when(lambda argv: argv[:2] == ["kubectl", "get"] and "jsonpath={.status.sync.status}" in argv, FakeProc(0, "Synced"))
        scripted.when(lambda argv: argv[:2] == ["kubectl", "get"] and "jsonpath={.status.health.status}" in argv, FakeProc(0, "Healthy"))
        with mock.patch.object(phase4_platform, "run", scripted), mock.patch.object(phase4_platform.time, "sleep"), _env_patch():
            _run_quiet(phase4_platform.cmd_reconcile_cluster, self.args)
        update_call = next(c for c in scripted.calls if c["argv"][:3] == ["aws", "eks", "update-kubeconfig"])
        role_idx = update_call["argv"].index("--role-arn") + 1
        assume_idx = update_call["argv"].index("--assume-role-arn") + 1
        self.assertEqual(update_call["argv"][role_idx], os.environ.get("EKS_DEPLOY_ROLE_ARN", update_call["argv"][role_idx]))
        self.assertEqual(update_call["argv"][role_idx], update_call["argv"][assume_idx])

    def test_application_crd_missing_fails(self):
        scripted = self._base_scripted()
        scripted.when(_starts_with("kubectl", "get", "crd"), FakeProc(1, "", "not found"))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error):
                _run_quiet(phase4_platform.cmd_reconcile_cluster, self.args)

    def test_repository_secret_missing_fails(self):
        scripted = self._base_scripted()
        scripted.when(lambda argv: argv[:4] == ["kubectl", "get", "secret", phase4_platform.ARGOCD_PLATFORM_SECRET_NAME] and "-o" not in argv, FakeProc(1, "", "not found"))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error):
                _run_quiet(phase4_platform.cmd_reconcile_cluster, self.args)

    def test_repository_secret_wrong_url_fails(self):
        scripted = self._base_scripted(secret_url="oci://wrong-registry/helm/goldengate-platform")
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error):
                _run_quiet(phase4_platform.cmd_reconcile_cluster, self.args)

    def test_application_manifest_contract(self):
        manifest = phase4_platform._build_application_manifest(
            "goldengate-dev-platform", "argocd", f"oci://{ECR_REGISTRY}/helm/goldengate-platform", "0.1.42", "goldengate-dev-platform",
            "goldengate-dev", "dev", RUNTIME_ROLE_ARN, PLATFORM_LOGGING_ROLE_ARN, "eu-west-1", "goldengate-monitor",
            "/goldengate/dev/runtime", "/goldengate/dev/monitor", FLUENT_BIT_IMAGE,
        )
        self.assertEqual(manifest["spec"]["source"]["repoURL"], f"oci://{ECR_REGISTRY}/helm/goldengate-platform")
        self.assertEqual(manifest["spec"]["source"]["targetRevision"], "0.1.42")
        self.assertEqual(manifest["spec"]["destination"]["namespace"], "goldengate-dev")
        self.assertIn("CreateNamespace=true", manifest["spec"]["syncPolicy"]["syncOptions"])
        self.assertTrue(manifest["spec"]["syncPolicy"]["automated"]["prune"])
        self.assertTrue(manifest["spec"]["syncPolicy"]["automated"]["selfHeal"])
        self.assertIn("managedNamespaceMetadata", manifest["spec"]["syncPolicy"])

    def test_argo_wait_success(self):
        scripted = self._base_scripted()
        scripted.when(_starts_with("kubectl", "get", "application"), FakeProc(0, ""))
        scripted.when(lambda argv: "jsonpath={.status.sync.status}" in argv, FakeProc(0, "Synced"))
        scripted.when(lambda argv: "jsonpath={.status.health.status}" in argv, FakeProc(0, "Healthy"))
        with mock.patch.object(phase4_platform, "run", scripted), mock.patch.object(phase4_platform.time, "sleep") as sleep_mock, _env_patch():
            _run_quiet(phase4_platform.cmd_reconcile_cluster, self.args)
        sleep_mock.assert_not_called()

    def test_argo_wait_timeout_at_600s(self):
        scripted = self._base_scripted()
        scripted.when(_starts_with("kubectl", "get", "application"), FakeProc(0, ""))
        scripted.when(lambda argv: "jsonpath={.status.sync.status}" in argv, FakeProc(0, "OutOfSync"))
        scripted.when(lambda argv: "jsonpath={.status.health.status}" in argv, FakeProc(0, "Progressing"))
        scripted.when(_starts_with("kubectl", "describe"), FakeProc(0, ""))
        with mock.patch.object(phase4_platform, "run", scripted), mock.patch.object(phase4_platform.time, "sleep") as sleep_mock, _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error) as ctx:
                _run_quiet(phase4_platform.cmd_reconcile_cluster, self.args)
            self.assertIn("Timed out after 600s", str(ctx.exception))
        total_slept = sum(c.args[0] for c in sleep_mock.call_args_list)
        self.assertGreaterEqual(total_slept, 600)

    def test_degraded_fails_immediately(self):
        scripted = self._base_scripted()
        scripted.when(_starts_with("kubectl", "get", "application"), FakeProc(0, ""))
        scripted.when(lambda argv: "jsonpath={.status.sync.status}" in argv, FakeProc(0, "OutOfSync"))
        scripted.when(lambda argv: "jsonpath={.status.health.status}" in argv, FakeProc(0, "Degraded"))
        with mock.patch.object(phase4_platform, "run", scripted), mock.patch.object(phase4_platform.time, "sleep") as sleep_mock, _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error) as ctx:
                _run_quiet(phase4_platform.cmd_reconcile_cluster, self.args)
            self.assertIn("Degraded", str(ctx.exception))
        sleep_mock.assert_not_called()


class PostDeployValidationTests(TempStateCase):
    def setUp(self):
        super().setUp()
        phase4_platform.update_state(self.state_path, {"namespace": "goldengate-dev", "release_name": "goldengate-dev-platform"})

    def _scripted(self, statefulset_proc, deployment_proc, daemonset_items_proc=None, daemonset_name_proc=None):
        """Builds the full ScriptedRun needed for cmd_post_deploy_validation, with the namespace/ServiceAccount/IRSA-annotation preamble fixed and only the workload-inventory/DaemonSet responses varying per test."""
        scripted = ScriptedRun()
        scripted.when(_starts_with("kubectl", "get", "namespace"), FakeProc(0, ""))
        scripted.when(lambda argv: argv[:4] == ["kubectl", "get", "serviceaccount", phase4_platform.RUNTIME_SA_NAME] and "-o" not in argv, FakeProc(0, ""))
        scripted.when(lambda argv: "jsonpath={.metadata.annotations.eks\\.amazonaws\\.com/role-arn}" in argv, FakeProc(0, RUNTIME_ROLE_ARN))
        scripted.when(_starts_with("kubectl", "get", "statefulset"), statefulset_proc)
        scripted.when(_starts_with("kubectl", "get", "deployment"), deployment_proc)
        scripted.when(lambda argv: argv[:3] == ["kubectl", "get", "daemonset"] and "-l" in argv,
                      daemonset_items_proc or FakeProc(0, json.dumps({"items": [{"metadata": {"name": "gg-fluent-bit"}}]})))
        scripted.when(lambda argv: argv[:3] == ["kubectl", "get", "daemonset"] and "-l" not in argv, daemonset_name_proc or FakeProc(0, ""))
        return scripted

    def test_required_live_resources_verified(self):
        scripted = self._scripted(FakeProc(0, json.dumps({"items": []})), FakeProc(0, json.dumps({"items": []})))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            _run_quiet(phase4_platform.cmd_post_deploy_validation, self.args)

    def test_unexpected_owned_workload_fails(self):
        scripted = self._scripted(FakeProc(0, json.dumps({"items": [{"metadata": {"name": "gg-oracle-01"}}]})), FakeProc(0, json.dumps({"items": []})))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error):
                _run_quiet(phase4_platform.cmd_post_deploy_validation, self.args)

    # 1. successful empty StatefulSet+Deployment queries passing is already covered by test_required_live_resources_verified above.

    # 2. owned StatefulSet still fails -- already covered by test_unexpected_owned_workload_fails above.

    def test_owned_deployment_still_fails(self):
        scripted = self._scripted(FakeProc(0, json.dumps({"items": []})), FakeProc(0, json.dumps({"items": [{"metadata": {"name": "gg-oracle-runtime"}}]})))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error):
                _run_quiet(phase4_platform.cmd_post_deploy_validation, self.args)

    def test_statefulset_forbidden_fails_closed(self):
        scripted = self._scripted(FakeProc(1, "", "Error from server (Forbidden): statefulsets.apps is forbidden: User cannot list resource"), FakeProc(0, json.dumps({"items": []})))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error):
                _run_quiet(phase4_platform.cmd_post_deploy_validation, self.args)

    def test_deployment_forbidden_fails_closed(self):
        scripted = self._scripted(FakeProc(0, json.dumps({"items": []})), FakeProc(1, "", "Error from server (Forbidden): deployments.apps is forbidden: User cannot list resource"))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error):
                _run_quiet(phase4_platform.cmd_post_deploy_validation, self.args)

    def test_unauthorized_fails_closed(self):
        scripted = self._scripted(FakeProc(1, "", "error: You must be logged in to the server (Unauthorized)"), FakeProc(0, json.dumps({"items": []})))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error):
                _run_quiet(phase4_platform.cmd_post_deploy_validation, self.args)

    def test_connection_network_failure_fails_closed(self):
        scripted = self._scripted(FakeProc(1, "", "Unable to connect to the server: dial tcp: connection refused"), FakeProc(0, json.dumps({"items": []})))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error):
                _run_quiet(phase4_platform.cmd_post_deploy_validation, self.args)

    def test_api_timeout_unknown_error_fails_closed(self):
        scripted = self._scripted(FakeProc(0, json.dumps({"items": []})), FakeProc(1, "", "error: the server was unable to return a response in the time allotted"))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error):
                _run_quiet(phase4_platform.cmd_post_deploy_validation, self.args)

    def test_malformed_statefulset_json_fails_closed(self):
        scripted = self._scripted(FakeProc(0, "{not valid json"), FakeProc(0, json.dumps({"items": []})))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error):
                _run_quiet(phase4_platform.cmd_post_deploy_validation, self.args)

    def test_malformed_deployment_json_fails_closed(self):
        scripted = self._scripted(FakeProc(0, json.dumps({"items": []})), FakeProc(0, "{not valid json"))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error):
                _run_quiet(phase4_platform.cmd_post_deploy_validation, self.args)

    def test_missing_items_key_fails_closed(self):
        scripted = self._scripted(FakeProc(0, json.dumps({"kind": "StatefulSetList"})), FakeProc(0, json.dumps({"items": []})))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error):
                _run_quiet(phase4_platform.cmd_post_deploy_validation, self.args)

    def test_items_not_a_list_fails_closed(self):
        scripted = self._scripted(FakeProc(0, json.dumps({"items": []})), FakeProc(0, json.dumps({"items": "not-a-list"})))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error):
                _run_quiet(phase4_platform.cmd_post_deploy_validation, self.args)

    def test_reproduces_pre_fix_false_success_before_fix(self):
        """Confirmed reproduction of the Phase 4 Python-extraction regression: StatefulSet lookup returns Forbidden, Deployment lookup returns a network/API failure, and DaemonSet lookup returns a valid one-item result. The pre-fix implementation (`if proc.returncode == 0: workload_count += ...`, called with check=False) silently treated both inspection failures as zero items and printed a false 'OK: no StatefulSet/Deployment resources are owned' -- exactly the false-success condition this correction closes. First mechanically replay the retired algorithm to confirm it really would have reported success for this input, then assert the current, fixed cmd_post_deploy_validation raises Phase4Error for the identical input instead."""
        statefulset_proc = FakeProc(1, "", "Error from server (Forbidden): statefulsets.apps is forbidden")
        deployment_proc = FakeProc(1, "", "Unable to connect to the server: dial tcp: connection refused")

        def retired_algorithm(procs):
            workload_count = 0
            for proc in procs:
                if proc.returncode == 0:
                    workload_count += len((json.loads(proc.stdout) or {}).get("items") or [])
            return workload_count == 0  # True means the retired algorithm would have (incorrectly) reported success.

        self.assertTrue(
            retired_algorithm([statefulset_proc, deployment_proc]),
            "the retired algorithm must be confirmed to falsely report zero workloads for this exact input before trusting that the new assertion below proves a real fix",
        )

        scripted = self._scripted(statefulset_proc, deployment_proc)
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error) as ctx:
                _run_quiet(phase4_platform.cmd_post_deploy_validation, self.args)
        self.assertNotIn("no StatefulSet/Deployment resources are owned", str(ctx.exception))

    def test_first_kind_failure_does_not_produce_false_zero_success(self):
        """The StatefulSet lookup (inspected first) fails; the Deployment lookup is never reached because run()'s own fail-closed check=True raises immediately -- proving a failure of the FIRST inspected kind alone is sufficient to fail the whole step closed, never silently treated as '0 StatefulSets, proceed to check Deployments, maybe pass'."""
        scripted = self._scripted(FakeProc(1, "", "Error from server (Forbidden): statefulsets.apps is forbidden"), FakeProc(0, json.dumps({"items": []})))
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error):
                _run_quiet(phase4_platform.cmd_post_deploy_validation, self.args)
        deployment_calls = [c for c in scripted.calls if c["argv"][:3] == ["kubectl", "get", "deployment"]]
        self.assertEqual(deployment_calls, [], "the Deployment kind must never be queried once the StatefulSet kind has already failed closed")

    def test_exact_one_daemonset_contract_remains_green(self):
        """The pre-existing exactly-one-DaemonSet-owned-by-the-release contract, and the DaemonSet/gg-fluent-bit existence check, must remain unweakened by this correction."""
        scripted = self._scripted(
            FakeProc(0, json.dumps({"items": []})),
            FakeProc(0, json.dumps({"items": []})),
            daemonset_items_proc=FakeProc(0, json.dumps({"items": [{"metadata": {"name": "gg-fluent-bit"}}, {"metadata": {"name": "gg-extra-daemonset"}}]})),
        )
        with mock.patch.object(phase4_platform, "run", scripted), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error) as ctx:
                _run_quiet(phase4_platform.cmd_post_deploy_validation, self.args)
        self.assertIn("expected exactly 1 DaemonSet", str(ctx.exception))


class ListOwnedWorkloadsTests(unittest.TestCase):
    """Direct unit coverage of the new _list_owned_workloads() helper, isolated from the rest of cmd_post_deploy_validation."""

    def test_successful_empty_result_returns_empty_list(self):
        with mock.patch.object(phase4_platform, "run", return_value=FakeProc(0, json.dumps({"items": []}))):
            self.assertEqual(phase4_platform._list_owned_workloads("statefulset", "goldengate-dev", "goldengate-dev-platform"), [])

    def test_successful_nonempty_result_returns_items(self):
        items = [{"metadata": {"name": "gg-oracle-01"}}]
        with mock.patch.object(phase4_platform, "run", return_value=FakeProc(0, json.dumps({"items": items}))):
            self.assertEqual(phase4_platform._list_owned_workloads("deployment", "goldengate-dev", "goldengate-dev-platform"), items)

    def test_malformed_json_fails_closed(self):
        with mock.patch.object(phase4_platform, "run", return_value=FakeProc(0, "{not valid json")):
            with self.assertRaises(phase4_platform.Phase4Error):
                phase4_platform._list_owned_workloads("statefulset", "goldengate-dev", "goldengate-dev-platform")

    def test_non_object_top_level_fails_closed(self):
        with mock.patch.object(phase4_platform, "run", return_value=FakeProc(0, json.dumps(["not", "an", "object"]))):
            with self.assertRaises(phase4_platform.Phase4Error):
                phase4_platform._list_owned_workloads("statefulset", "goldengate-dev", "goldengate-dev-platform")

    def test_missing_items_key_fails_closed(self):
        with mock.patch.object(phase4_platform, "run", return_value=FakeProc(0, json.dumps({"kind": "DeploymentList"}))):
            with self.assertRaises(phase4_platform.Phase4Error):
                phase4_platform._list_owned_workloads("deployment", "goldengate-dev", "goldengate-dev-platform")

    def test_items_not_a_list_fails_closed(self):
        with mock.patch.object(phase4_platform, "run", return_value=FakeProc(0, json.dumps({"items": {"not": "a list"}}))):
            with self.assertRaises(phase4_platform.Phase4Error):
                phase4_platform._list_owned_workloads("statefulset", "goldengate-dev", "goldengate-dev-platform")

    def test_null_items_fails_closed(self):
        with mock.patch.object(phase4_platform, "run", return_value=FakeProc(0, json.dumps({"items": None}))):
            with self.assertRaises(phase4_platform.Phase4Error):
                phase4_platform._list_owned_workloads("statefulset", "goldengate-dev", "goldengate-dev-platform")


class OwnershipPreflightAndAcceptanceTests(TempStateCase):
    def _patch_state_tool(self, result_json, returncode=0):
        def fake_run(argv, env=None, cwd=None, check=True, capture_output=True, input_text=None):
            if argv[0] == "aws":
                return FakeProc(0, "")
            if str(phase4_platform.PLATFORM_STATE_TOOL) in argv or str(phase4_platform.PLATFORM_ACCEPTANCE_TOOL) in argv:
                return FakeProc(returncode, json.dumps(result_json) if returncode == 0 else "")
            return FakeProc(0, "")
        return fake_run

    def test_ownership_preflight_accepts_absent(self):
        with mock.patch.object(phase4_platform, "run", self._patch_state_tool({"state": "ABSENT"})), _env_patch(), \
             mock.patch.object(phase4_platform, "write_github_output") as write_mock:
            _run_quiet(phase4_platform.cmd_ownership_preflight, self.args)
        write_mock.assert_called_once_with([("state", "ABSENT")])

    def test_ownership_preflight_accepts_owned(self):
        with mock.patch.object(phase4_platform, "run", self._patch_state_tool({"state": "OWNED"})), _env_patch(), \
             mock.patch.object(phase4_platform, "write_github_output") as write_mock:
            _run_quiet(phase4_platform.cmd_ownership_preflight, self.args)
        write_mock.assert_called_once_with([("state", "OWNED")])

    def test_ownership_preflight_rejects_broken(self):
        with mock.patch.object(phase4_platform, "run", self._patch_state_tool({"state": "BROKEN"})), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error):
                _run_quiet(phase4_platform.cmd_ownership_preflight, self.args)

    def test_ownership_preflight_inspection_error_fails(self):
        with mock.patch.object(phase4_platform, "run", self._patch_state_tool({}, returncode=1)), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error):
                _run_quiet(phase4_platform.cmd_ownership_preflight, self.args)

    def test_ownership_preflight_malformed_state_fails(self):
        with mock.patch.object(phase4_platform, "run", self._patch_state_tool({"state": "WEIRD"})), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error):
                _run_quiet(phase4_platform.cmd_ownership_preflight, self.args)

    def test_strict_acceptance_requires_healthy(self):
        with mock.patch.object(phase4_platform, "run", self._patch_state_tool({"state": "HEALTHY"})), _env_patch():
            _run_quiet(phase4_platform.cmd_strict_acceptance, self.args)

    def test_strict_acceptance_rejects_broken(self):
        with mock.patch.object(phase4_platform, "run", self._patch_state_tool({"state": "BROKEN"})), _env_patch():
            with self.assertRaises(phase4_platform.Phase4Error):
                _run_quiet(phase4_platform.cmd_strict_acceptance, self.args)


class StateFileTests(TempStateCase):
    def test_state_rejects_disallowed_keys(self):
        with self.assertRaises(phase4_platform.Phase4Error):
            phase4_platform.update_state(self.state_path, {"aws_secret_access_key": "leak"})

    def test_state_file_contains_no_credential_shaped_keys(self):
        phase4_platform.update_state(self.state_path, {"chart_version": "0.1.1", "namespace": "goldengate-dev"})
        state_text = self.state_path.read_text()
        for forbidden in ("AKIA", "aws_secret_access_key", "password", "SessionToken"):
            self.assertNotIn(forbidden, state_text)

    def test_atomic_write_no_partial_file(self):
        phase4_platform.update_state(self.state_path, {"chart_version": "0.1.1"})
        self.assertFalse(self.state_path.with_suffix(".json.tmp").exists())


class SummaryTests(TempStateCase):
    def test_summary_tolerates_empty_state(self):
        buf = io.StringIO()
        summary_path = Path(self._tmpdir.name) / "summary.md"
        with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary_path)}), redirect_stdout(buf):
            phase4_platform.cmd_summary(self.args)
        self.assertTrue(summary_path.exists())
        self.assertIn("unknown", summary_path.read_text())

    def test_summary_tolerates_partial_state(self):
        phase4_platform.update_state(self.state_path, {"chart_version": "0.1.7"})
        summary_path = Path(self._tmpdir.name) / "summary.md"
        with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary_path)}), redirect_stdout(io.StringIO()):
            phase4_platform.cmd_summary(self.args)
        self.assertIn("0.1.7", summary_path.read_text())


class DeployFalseContractTests(unittest.TestCase):
    def test_prepare_and_validate_calls_no_kubectl(self):
        # prepare-and-validate is the deploy=false-safe subcommand; it must never invoke kubectl.
        source = TOOL_PATH.read_text()
        prepare_fn_start = source.index("def cmd_prepare_and_validate")
        prepare_fn_end = source.index("\ndef ", prepare_fn_start + 1)
        self.assertNotIn("kubectl", source[prepare_fn_start:prepare_fn_end])


if __name__ == "__main__":
    unittest.main()
