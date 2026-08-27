"""Offline tests for automation/phases/phase5/runtime_state.py; run directly via `python3 automation/phases/phase5/tests/test_runtime_state.py`. No live Kubernetes -- every kubectl response is a fake, injected fixture. Exercises the classifier's actual logic (never merely greps its source)."""
from __future__ import annotations

import importlib.util
import json
import os
import unittest
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parents[4])
TOOL_PATH = os.path.join(REPO_ROOT, "automation", "phases", "phase5", "runtime_state.py")


def _load_tool():
    spec = importlib.util.spec_from_file_location("runtime_state", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime_state = _load_tool()

# Uses a real envs/dev descriptor -- describe_deployment() reads the real repository, never a scratch root (this classifier is not itself a second descriptor parser, so its tests exercise it against the real folder-driven model). Its own deployment.enabled value is irrelevant here: describe_deployment() resolves both active and inactive descriptors identically, and classify() itself never inspects deployment.enabled at all.
ENVIRONMENT = "dev"
DEPLOYMENT_ID = "gg-postgresql-repltest-01"
ARGOCD_NAMESPACE = "argocd"
RUNTIME_NAMESPACE = "goldengate-dev"
ECR_REGISTRY = "229410149234.dkr.ecr.eu-west-1.amazonaws.com"

APP_NAME = f"goldengate-{ENVIRONMENT}-postgresql-repltest-01"
STORAGECLASS_NAME = f"gg-efs-{ENVIRONMENT}-{DEPLOYMENT_ID}"


class FakeCluster:
    """Models exactly the subset of `kubectl get <resource> <name> [-n ns] -o json` behavior the classifier depends on -- never a real kubectl process. Every call here is a single-name get; unset keys default to NotFound, matching real kubectl semantics."""

    def __init__(self):
        self.objects = {}
        self.force_errors = {}

    def put(self, resource, name, namespace, obj):
        self.objects[(resource, name, namespace)] = obj

    def fail(self, resource, name, namespace, stderr):
        self.force_errors[(resource, name, namespace)] = stderr

    def __call__(self, args):
        assert args[0] == "get", f"classifier issued a non-read-only kubectl verb: {args}"
        resource = args[1]
        idx = 2
        name = None
        namespace = None
        if idx < len(args) and not args[idx].startswith("-"):
            name = args[idx]
            idx += 1
        while idx < len(args):
            if args[idx] == "-n":
                namespace = args[idx + 1]
                idx += 2
            else:
                idx += 1

        key = (resource, name, namespace)
        if key in self.force_errors:
            return 1, "", self.force_errors[key]
        obj = self.objects.get(key)
        if obj is None:
            return 1, "", f'Error from server (NotFound): {resource} "{name}" not found'
        return 0, json.dumps(obj), ""


def _runtime_labels(deployment_id=DEPLOYMENT_ID, environment=ENVIRONMENT):
    return {
        "app.kubernetes.io/name": "goldengate",
        "app.kubernetes.io/instance": deployment_id,
        "app.kubernetes.io/component": "runtime",
        "goldengate.adcb/environment": environment,
        "goldengate.adcb/deployment-name": deployment_id,
    }


def _storageclass_labels(deployment_id=DEPLOYMENT_ID, environment=ENVIRONMENT):
    return {
        "app.kubernetes.io/name": "goldengate",
        "app.kubernetes.io/managed-by": "argocd",
        "goldengate.adcb/environment": environment,
        "goldengate.adcb/deployment-id": deployment_id,
    }


def _app_obj(name=APP_NAME, environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, dest_ns=RUNTIME_NAMESPACE, repo_url=None, release_name=None):
    return {
        "metadata": {
            "name": name,
            "labels": {
                "goldengate.adcb/environment": environment,
                "goldengate.adcb/deployment-id": deployment_id,
            },
        },
        "spec": {
            "source": {
                "repoURL": repo_url if repo_url is not None else f"oci://{ECR_REGISTRY}/{runtime_state.HELM_REPO_PATH}",
                "helm": {"releaseName": release_name if release_name is not None else deployment_id},
            },
            "destination": {"namespace": dest_ns},
        },
    }


def _named_obj(name, labels):
    return {"metadata": {"name": name, "labels": labels}}


def _populate_owned_footprint(cluster, deployment_id=DEPLOYMENT_ID, environment=ENVIRONMENT):
    """Populates every expected-name resource with correctly-owned labels (footprint fully present, matching a prior successful reconciliation)."""
    cluster.put("statefulset", deployment_id, RUNTIME_NAMESPACE, _named_obj(deployment_id, _runtime_labels(deployment_id, environment)))
    cluster.put("service", deployment_id, RUNTIME_NAMESPACE, _named_obj(deployment_id, _runtime_labels(deployment_id, environment)))
    cluster.put("service", f"{deployment_id}-headless", RUNTIME_NAMESPACE, _named_obj(f"{deployment_id}-headless", _runtime_labels(deployment_id, environment)))
    cluster.put("persistentvolumeclaim", f"{deployment_id}-u02", RUNTIME_NAMESPACE, _named_obj(f"{deployment_id}-u02", _runtime_labels(deployment_id, environment)))
    cluster.put("storageclass", f"gg-efs-{environment}-{deployment_id}", None, _named_obj(f"gg-efs-{environment}-{deployment_id}", _storageclass_labels(deployment_id, environment)))
    cluster.put("secretproviderclass", f"{deployment_id}-admin", RUNTIME_NAMESPACE, _named_obj(f"{deployment_id}-admin", _runtime_labels(deployment_id, environment)))
    cluster.put("secretproviderclass", f"{deployment_id}-certificate", RUNTIME_NAMESPACE, _named_obj(f"{deployment_id}-certificate", _runtime_labels(deployment_id, environment)))
    cluster.put("ingress", f"{deployment_id}-ingress", RUNTIME_NAMESPACE, _named_obj(f"{deployment_id}-ingress", _runtime_labels(deployment_id, environment)))
    cluster.put("secret", f"{deployment_id}-admin", RUNTIME_NAMESPACE, {"metadata": {"name": f"{deployment_id}-admin"}})
    return cluster


def _classify(cluster, deployment_id=DEPLOYMENT_ID):
    return runtime_state.classify(
        cluster,
        environment=ENVIRONMENT,
        deployment_id=deployment_id,
        argocd_namespace=ARGOCD_NAMESPACE,
        runtime_namespace=RUNTIME_NAMESPACE,
        ecr_registry=ECR_REGISTRY,
    )


class RuntimeStateClassifierTests(unittest.TestCase):
    def test_1_no_app_no_footprint_is_absent(self):
        cluster = FakeCluster()
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_state.STATE_ABSENT)
        self.assertEqual(result["reasons"], [])

    def test_2_app_absent_statefulset_exists_is_broken(self):
        cluster = FakeCluster()
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _named_obj(DEPLOYMENT_ID, _runtime_labels()))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_state.STATE_BROKEN)
        self.assertTrue(any("does not exist" in r and "Application" in r for r in result["reasons"]))

    def test_3_app_absent_service_exists_is_broken(self):
        cluster = FakeCluster()
        cluster.put("service", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _named_obj(DEPLOYMENT_ID, _runtime_labels()))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_state.STATE_BROKEN)

    def test_4_app_absent_storageclass_exists_is_broken(self):
        cluster = FakeCluster()
        cluster.put("storageclass", STORAGECLASS_NAME, None, _named_obj(STORAGECLASS_NAME, _storageclass_labels()))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_state.STATE_BROKEN)

    def test_5_correct_app_no_workload_resources_yet_is_owned(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj())
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_state.STATE_OWNED)
        self.assertEqual(result["reasons"], [])

    def test_6_correct_app_workload_not_ready_is_still_owned(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj())
        not_ready_sts = _named_obj(DEPLOYMENT_ID, _runtime_labels())
        not_ready_sts["status"] = {"readyReplicas": 0}
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, not_ready_sts)
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_state.STATE_OWNED)

    def test_7_correct_app_outofsync_is_still_owned(self):
        cluster = FakeCluster()
        app = _app_obj()
        app["status"] = {"sync": {"status": "OutOfSync"}, "health": {"status": "Healthy"}}
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, app)
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_state.STATE_OWNED)

    def test_8_correct_app_health_degraded_is_still_owned(self):
        cluster = FakeCluster()
        app = _app_obj()
        app["status"] = {"sync": {"status": "Synced"}, "health": {"status": "Degraded"}}
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, app)
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_state.STATE_OWNED)

    def test_9_app_wrong_environment_label_is_broken(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(environment="sit"))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_state.STATE_BROKEN)
        self.assertTrue(any("goldengate.adcb/environment" in r for r in result["reasons"]))

    def test_10_app_wrong_deployment_id_label_is_broken(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(deployment_id="gg-some-other-deployment"))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_state.STATE_BROKEN)
        self.assertTrue(any("goldengate.adcb/deployment-id" in r for r in result["reasons"]))

    def test_11_app_wrong_destination_namespace_is_broken(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(dest_ns="some-other-namespace"))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_state.STATE_BROKEN)
        self.assertTrue(any("destination.namespace" in r for r in result["reasons"]))

    def test_12_app_wrong_repo_url_is_broken(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(repo_url="oci://wrong.example.com/helm/goldengate"))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_state.STATE_BROKEN)
        self.assertTrue(any("source.repoURL" in r for r in result["reasons"]))

    def test_13_app_wrong_release_name_is_broken(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj(release_name="some-other-release"))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_state.STATE_BROKEN)
        self.assertTrue(any("source.helm.releaseName" in r for r in result["reasons"]))

    def test_14_expected_statefulset_foreign_ownership_is_broken(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj())
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _named_obj(DEPLOYMENT_ID, _runtime_labels(deployment_id="gg-some-other-deployment")))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_state.STATE_BROKEN)
        self.assertTrue(any("statefulset" in r and "incompatible ownership" in r for r in result["reasons"]))

    def test_15_expected_service_foreign_ownership_is_broken(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj())
        cluster.put("service", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _named_obj(DEPLOYMENT_ID, _runtime_labels(environment="sit")))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_state.STATE_BROKEN)
        self.assertTrue(any("service" in r and "incompatible ownership" in r for r in result["reasons"]))

    def test_16_expected_pvc_and_storageclass_foreign_ownership_is_broken(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj())
        cluster.put("persistentvolumeclaim", f"{DEPLOYMENT_ID}-u02", RUNTIME_NAMESPACE, _named_obj(f"{DEPLOYMENT_ID}-u02", _runtime_labels(deployment_id="gg-foreign")))
        cluster.put("storageclass", STORAGECLASS_NAME, None, _named_obj(STORAGECLASS_NAME, _storageclass_labels(deployment_id="gg-foreign")))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_state.STATE_BROKEN)
        self.assertTrue(any("pvc" in r and "incompatible ownership" in r for r in result["reasons"]))
        self.assertTrue(any("storageclass" in r and "incompatible ownership" in r for r in result["reasons"]))

    def test_17_expected_ingress_and_spc_foreign_ownership_is_broken(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj())
        cluster.put("ingress", f"{DEPLOYMENT_ID}-ingress", RUNTIME_NAMESPACE, _named_obj(f"{DEPLOYMENT_ID}-ingress", _runtime_labels(deployment_id="gg-foreign")))
        cluster.put("secretproviderclass", f"{DEPLOYMENT_ID}-admin", RUNTIME_NAMESPACE, _named_obj(f"{DEPLOYMENT_ID}-admin", _runtime_labels(deployment_id="gg-foreign")))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_state.STATE_BROKEN)
        self.assertTrue(any("ingress" in r and "incompatible ownership" in r for r in result["reasons"]))
        self.assertTrue(any("admin_secretproviderclass" in r and "incompatible ownership" in r for r in result["reasons"]))

    def test_18_forbidden_or_api_error_raises_inspection_error_not_absent(self):
        cluster = FakeCluster()
        cluster.fail("application", APP_NAME, ARGOCD_NAMESPACE, "Error from server (Forbidden): applications.argoproj.io is forbidden")
        with self.assertRaises(runtime_state.ClassifierInspectionError):
            _classify(cluster)

    # GoldenGate Runtime Presence Contract -- Final Safety Correction, Gap 5: DEPLOYMENT_ID (gg-postgresql-repltest-01) is a real descriptor that declares chart-owned managed EFS persistence (persistence.enabled=true, provider=efs, efs.mode=managed, no existingClaim) -- exactly the shape the retained-PVC safe case requires.

    def test_19_app_absent_only_owned_retained_pvc_is_owned(self):
        # 25: App absent + owned retained PVC only is considered safe for re-enable -- the recognized "disabled runtime, durable /u02 data retained" shape.
        cluster = FakeCluster()
        cluster.put("persistentvolumeclaim", f"{DEPLOYMENT_ID}-u02", RUNTIME_NAMESPACE, _named_obj(f"{DEPLOYMENT_ID}-u02", _runtime_labels()))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_state.STATE_OWNED)
        self.assertEqual(result["reasons"], [])

    def test_20_app_absent_foreign_retained_pvc_is_broken(self):
        # 26: App absent + a same-named PVC whose ownership labels belong to a DIFFERENT deployment is BROKEN -- retained-persistence recognition never bypasses the ordinary per-resource ownership-label check.
        cluster = FakeCluster()
        cluster.put("persistentvolumeclaim", f"{DEPLOYMENT_ID}-u02", RUNTIME_NAMESPACE, _named_obj(f"{DEPLOYMENT_ID}-u02", _runtime_labels(deployment_id="gg-foreign")))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_state.STATE_BROKEN)
        self.assertTrue(any("pvc" in r and "incompatible ownership" in r for r in result["reasons"]))

    def test_21_app_absent_statefulset_still_running_is_broken_even_with_retained_pvc(self):
        # 27: App absent + StatefulSet still running is BROKEN -- retained-persistence recognition applies ONLY to the PVC kind; every other compute/workload footprint kind remains exactly as unsafe as before, even alongside a correctly-owned retained PVC.
        cluster = FakeCluster()
        cluster.put("persistentvolumeclaim", f"{DEPLOYMENT_ID}-u02", RUNTIME_NAMESPACE, _named_obj(f"{DEPLOYMENT_ID}-u02", _runtime_labels()))
        cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _named_obj(DEPLOYMENT_ID, _runtime_labels()))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_state.STATE_BROKEN)
        self.assertTrue(any("does not exist" in r and "Application" in r for r in result["reasons"]))

    def test_22_app_absent_retained_pvc_without_declared_persistence_is_broken(self):
        # A deployment that does NOT declare chart-owned EFS persistence has no legitimate reason for a retained PVC to exist under its expected name -- treated as an unexplained orphan, never silently adopted as the recognized retained-persistence shape. Monkeypatched descriptor (never a corrupted real repo file) purely to exercise this one branch in isolation, matching this file's own established describe_deployment monkeypatch convention.
        original_describe_deployment = runtime_state.describe_deployment

        def _no_persistence_descriptor(environment, deployment_id):
            return {"deploymentId": deployment_id, "efsMode": None, "pvcClaimName": ""}

        runtime_state.describe_deployment = _no_persistence_descriptor
        try:
            cluster = FakeCluster()
            cluster.put("persistentvolumeclaim", f"{DEPLOYMENT_ID}-u02", RUNTIME_NAMESPACE, _named_obj(f"{DEPLOYMENT_ID}-u02", _runtime_labels()))
            result = _classify(cluster)
            self.assertEqual(result["state"], runtime_state.STATE_BROKEN)
            self.assertTrue(any("does not declare chart-owned EFS persistence" in r for r in result["reasons"]))
        finally:
            runtime_state.describe_deployment = original_describe_deployment

    def test_23_app_absent_retained_pvc_with_existing_claim_mode_is_broken(self):
        # A descriptor that references a pre-existing PVC (runtime.storage.u02.existingClaim set) never owns/creates its own PVC via the chart at all -- pvcClaimName being non-empty must be treated the same as "not chart-owned" here, exactly like the "no persistence declared" case above.
        original_describe_deployment = runtime_state.describe_deployment

        def _existing_claim_descriptor(environment, deployment_id):
            return {"deploymentId": deployment_id, "efsMode": "existing", "pvcClaimName": "some-pre-existing-pvc"}

        runtime_state.describe_deployment = _existing_claim_descriptor
        try:
            cluster = FakeCluster()
            cluster.put("persistentvolumeclaim", f"{DEPLOYMENT_ID}-u02", RUNTIME_NAMESPACE, _named_obj(f"{DEPLOYMENT_ID}-u02", _runtime_labels()))
            result = _classify(cluster)
            self.assertEqual(result["state"], runtime_state.STATE_BROKEN)
        finally:
            runtime_state.describe_deployment = original_describe_deployment

    def test_25_physical_removal_no_hint_retained_pvc_reproduces_broken_bug(self):
        # Confirmed reproduction of the physical-removal retained-PVC bug: the descriptor was physically removed (describe_deployment raises "unknown deployment ID"), so declares_chart_owned_persistence can never be computed -- without the explicit hint, a legitimately retained PVC is misclassified BROKEN, which would falsely fail idempotent physical-removal cleanup.
        original_describe_deployment = runtime_state.describe_deployment
        runtime_state.describe_deployment = lambda environment, deployment_id: (_ for _ in ()).throw(ValueError(f"unknown deployment ID {deployment_id!r}"))
        try:
            cluster = FakeCluster()
            cluster.put("persistentvolumeclaim", f"{DEPLOYMENT_ID}-u02", RUNTIME_NAMESPACE, _named_obj(f"{DEPLOYMENT_ID}-u02", _runtime_labels()))
            result = _classify(cluster)
            self.assertEqual(result["state"], runtime_state.STATE_BROKEN)
        finally:
            runtime_state.describe_deployment = original_describe_deployment

    def test_26_physical_removal_with_hint_retained_pvc_is_owned(self):
        # The fix: passing retained_pvc_expected=True lets the classifier recognize the same "Application absent, only the correctly-owned expected-name PVC exists" shape as safe even when the descriptor itself was physically removed.
        original_describe_deployment = runtime_state.describe_deployment
        runtime_state.describe_deployment = lambda environment, deployment_id: (_ for _ in ()).throw(ValueError(f"unknown deployment ID {deployment_id!r}"))
        try:
            cluster = FakeCluster()
            cluster.put("persistentvolumeclaim", f"{DEPLOYMENT_ID}-u02", RUNTIME_NAMESPACE, _named_obj(f"{DEPLOYMENT_ID}-u02", _runtime_labels()))
            result = runtime_state.classify(
                cluster, environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, argocd_namespace=ARGOCD_NAMESPACE,
                runtime_namespace=RUNTIME_NAMESPACE, ecr_registry=ECR_REGISTRY, retained_pvc_expected=True,
            )
            self.assertEqual(result["state"], runtime_state.STATE_OWNED)
            self.assertEqual(result["reasons"], [])
        finally:
            runtime_state.describe_deployment = original_describe_deployment

    def test_27_physical_removal_with_hint_foreign_pvc_still_broken(self):
        # The hint never bypasses the ordinary per-resource ownership-label check -- a foreign/mislabeled PVC under the expected name is still BROKEN even with retained_pvc_expected=True. Never auto-adopt arbitrary PVCs.
        original_describe_deployment = runtime_state.describe_deployment
        runtime_state.describe_deployment = lambda environment, deployment_id: (_ for _ in ()).throw(ValueError(f"unknown deployment ID {deployment_id!r}"))
        try:
            cluster = FakeCluster()
            cluster.put("persistentvolumeclaim", f"{DEPLOYMENT_ID}-u02", RUNTIME_NAMESPACE, _named_obj(f"{DEPLOYMENT_ID}-u02", _runtime_labels(deployment_id="gg-foreign")))
            result = runtime_state.classify(
                cluster, environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, argocd_namespace=ARGOCD_NAMESPACE,
                runtime_namespace=RUNTIME_NAMESPACE, ecr_registry=ECR_REGISTRY, retained_pvc_expected=True,
            )
            self.assertEqual(result["state"], runtime_state.STATE_BROKEN)
            self.assertTrue(any("incompatible ownership" in r for r in result["reasons"]))
        finally:
            runtime_state.describe_deployment = original_describe_deployment

    def test_28_physical_removal_with_hint_but_empty_efs_mode_reason_no_pvc_present(self):
        # retained_pvc_expected=True with no PVC at all, and no other footprint, is simply the ordinary ABSENT case (nothing to retain, nothing owned) -- the hint must never manufacture a footprint that was never there.
        original_describe_deployment = runtime_state.describe_deployment
        runtime_state.describe_deployment = lambda environment, deployment_id: (_ for _ in ()).throw(ValueError(f"unknown deployment ID {deployment_id!r}"))
        try:
            cluster = FakeCluster()
            result = runtime_state.classify(
                cluster, environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, argocd_namespace=ARGOCD_NAMESPACE,
                runtime_namespace=RUNTIME_NAMESPACE, ecr_registry=ECR_REGISTRY, retained_pvc_expected=True,
            )
            self.assertEqual(result["state"], runtime_state.STATE_ABSENT)
        finally:
            runtime_state.describe_deployment = original_describe_deployment

    def test_29_physical_removal_with_hint_non_pvc_footprint_still_broken(self):
        # The hint applies ONLY to the PVC kind -- a StatefulSet still running (physically-removed descriptor, no Application) remains exactly as unsafe as before, hint or not.
        original_describe_deployment = runtime_state.describe_deployment
        runtime_state.describe_deployment = lambda environment, deployment_id: (_ for _ in ()).throw(ValueError(f"unknown deployment ID {deployment_id!r}"))
        try:
            cluster = FakeCluster()
            cluster.put("persistentvolumeclaim", f"{DEPLOYMENT_ID}-u02", RUNTIME_NAMESPACE, _named_obj(f"{DEPLOYMENT_ID}-u02", _runtime_labels()))
            cluster.put("statefulset", DEPLOYMENT_ID, RUNTIME_NAMESPACE, _named_obj(DEPLOYMENT_ID, _runtime_labels()))
            result = runtime_state.classify(
                cluster, environment=ENVIRONMENT, deployment_id=DEPLOYMENT_ID, argocd_namespace=ARGOCD_NAMESPACE,
                runtime_namespace=RUNTIME_NAMESPACE, ecr_registry=ECR_REGISTRY, retained_pvc_expected=True,
            )
            self.assertEqual(result["state"], runtime_state.STATE_BROKEN)
        finally:
            runtime_state.describe_deployment = original_describe_deployment

    def test_30_default_classify_call_sites_unaffected_by_new_parameter(self):
        # Defense-in-depth: every OTHER existing test in this file calls classify() via _classify() (which never passes retained_pvc_expected), and the new parameter defaults to False -- confirms the default keyword value itself, not merely test behavior.
        self.assertEqual(runtime_state.classify.__defaults__[-1], False)

    def test_24_app_found_retained_pvc_owned_is_still_owned(self):
        # Sanity: the Application-found path is entirely unaffected by the Gap 5 retained-PVC change -- a correctly-owned PVC alongside a correctly-owned Application remains OWNED exactly as before.
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj())
        cluster.put("persistentvolumeclaim", f"{DEPLOYMENT_ID}-u02", RUNTIME_NAMESPACE, _named_obj(f"{DEPLOYMENT_ID}-u02", _runtime_labels()))
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_state.STATE_OWNED)
        self.assertEqual(result["reasons"], [])

    def test_full_owned_footprint_with_correct_app_is_owned(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj())
        _populate_owned_footprint(cluster)
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_state.STATE_OWNED)
        self.assertEqual(result["reasons"], [])

    def test_admin_secret_presence_alone_is_never_a_foreign_ownership_reason(self):
        # The synced admin Secret carries no goldengate.adcb/* labels (created out-of-band by the Secrets Store CSI driver) -- its mere presence must never itself produce an "incompatible ownership" reason.
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj())
        cluster.put("secret", f"{DEPLOYMENT_ID}-admin", RUNTIME_NAMESPACE, {"metadata": {"name": f"{DEPLOYMENT_ID}-admin"}})
        result = _classify(cluster)
        self.assertEqual(result["state"], runtime_state.STATE_OWNED)

    def test_unknown_deployment_id_never_forces_configuration_error(self):
        # GoldenGate Runtime Presence Contract Finalization: this classifier is reused for a PHYSICALLY REMOVED descriptor too (ownership-safe delete, deletion_matrix reason=physical-removal), where by design no envs/dev/<id>/values.yaml exists any more -- an unknown deployment ID must never itself force a configuration error; it proceeds straight to cluster-based classification (ABSENT here, since nothing exists on the cluster for it either).
        cluster = FakeCluster()
        result = _classify(cluster, deployment_id="gg-does-not-exist-anywhere")
        self.assertEqual(result["state"], runtime_state.STATE_ABSENT)

    def test_genuinely_broken_folder_driven_model_still_raises_configuration_error(self):
        # The OTHER describe_deployment() failure mode -- the folder-driven model itself is inconsistent (an invalid descriptor or cross-descriptor problem exists elsewhere in the environment) -- must still fail closed, never silently proceed with an untrustworthy model. Monkeypatched here (never a corrupted real repo file) purely to exercise classify()'s own re-raise branch in isolation.
        original_describe_deployment = runtime_state.describe_deployment

        def _broken_model(environment, deployment_id):
            raise ValueError(f"the folder-driven deployment model for {environment!r} has validation problems -- refusing to classify runtime ownership against an inconsistent model")

        runtime_state.describe_deployment = _broken_model
        try:
            cluster = FakeCluster()
            with self.assertRaises(ValueError):
                _classify(cluster)
        finally:
            runtime_state.describe_deployment = original_describe_deployment

    def test_malformed_json_raises_inspection_error(self):
        cluster = FakeCluster()

        def bad_run(args):
            if args[:2] == ["get", "application"]:
                return 0, "not valid json{{{", ""
            return cluster(args)

        with self.assertRaises(runtime_state.ClassifierInspectionError):
            runtime_state.classify(
                bad_run,
                environment=ENVIRONMENT,
                deployment_id=DEPLOYMENT_ID,
                argocd_namespace=ARGOCD_NAMESPACE,
                runtime_namespace=RUNTIME_NAMESPACE,
                ecr_registry=ECR_REGISTRY,
            )


class RuntimeStateNoMutationSourceSweepTests(unittest.TestCase):
    """Static source-safety proof: the classifier module (and its shared k8s_common helper) must never construct a mutating kubectl/helm command."""

    FORBIDDEN_SUBSTRINGS = (
        "kubectl apply", "kubectl create", "kubectl delete", "kubectl patch",
        "kubectl annotate", "kubectl label",
        "helm install", "helm upgrade", "helm uninstall",
    )

    def test_source_contains_no_mutating_command(self):
        k8s_common_path = os.path.join(REPO_ROOT, "automation", "orchestration", "k8s_common.py")
        for path in (TOOL_PATH, k8s_common_path):
            with open(path) as f:
                source = f.read()
            hits = [s for s in self.FORBIDDEN_SUBSTRINGS if s in source]
            self.assertEqual(hits, [], f"{path} contains a mutating-looking construct: {hits}")

    def test_every_get_json_call_uses_get_verb_only(self):
        cluster = FakeCluster()
        cluster.put("application", APP_NAME, ARGOCD_NAMESPACE, _app_obj())
        _populate_owned_footprint(cluster)
        _classify(cluster)


if __name__ == "__main__":
    unittest.main()
