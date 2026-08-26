"""Offline tests for automation/orchestration/argocd_state.py (ownership-safety preflight: ABSENT/OWNED/BROKEN); run directly via `python3 automation/test-goldengate-argocd-state.py`. No live Kubernetes -- every kubectl response is a fake, injected fixture. Exercises the classifier's actual logic (never merely greps its source). Post-reconciliation acceptance (HEALTHY/BROKEN) is a separate module -- see automation/test-goldengate-argocd-acceptance.py."""
from __future__ import annotations

import importlib.util
import json
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL_PATH = os.path.join(REPO_ROOT, "automation", "orchestration", "argocd_state.py")


def _load_tool():
    spec = importlib.util.spec_from_file_location("argocd_state", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


argocd_state = _load_tool()

NAMESPACE = "argocd"


class FakeCluster:
    """Models exactly the subset of `kubectl get <resource> [name] [-n ns] -o json` behavior the classifier depends on -- never a real kubectl process."""

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
            elif args[idx] == "-o":
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


def _owned_labels(extra=None):
    labels = {"app.kubernetes.io/instance": argocd_state.ARGOCD_RELEASE_NAME}
    if extra:
        labels.update(extra)
    return labels


def _populate_owned_core(cluster):
    cluster.put("namespace", NAMESPACE, None, {"metadata": {}})
    for crd in argocd_state.REQUIRED_CRDS:
        cluster.put("crd", crd, None, {"metadata": {}})
    for name in argocd_state.CORE_DEPLOYMENTS:
        cluster.put("deployment", name, NAMESPACE, {"metadata": {"labels": _owned_labels()}})
    for name in argocd_state.CORE_STATEFULSETS:
        cluster.put("statefulset", name, NAMESPACE, {"metadata": {"labels": _owned_labels()}})
    for name in argocd_state.CORE_SERVICES:
        cluster.put("service", name, NAMESPACE, {"metadata": {"labels": _owned_labels()}})
    for kind, resource in (("serviceaccount", "serviceaccount"), ("role", "role"), ("rolebinding", "rolebinding"), ("cronjob", "cronjob")):
        cluster.put(resource, argocd_state.ECR_TOKEN_SYNC_NAME, NAMESPACE, {"metadata": {"labels": {"app.kubernetes.io/part-of": "argocd"}}})
    for name in argocd_state.REPOSITORY_SECRET_NAMES:
        cluster.put("secret", name, NAMESPACE, {"metadata": {}})
    return cluster


def _classify(cluster):
    return argocd_state.classify(cluster, environment="dev", namespace=NAMESPACE)


class ArgoCdOwnershipStateTests(unittest.TestCase):
    def test_completely_clean_cluster_is_absent(self):
        cluster = FakeCluster()
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_ABSENT)
        self.assertEqual(result["reasons"], [])

    def test_fully_owned_core_is_owned(self):
        cluster = _populate_owned_core(FakeCluster())
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_OWNED)
        self.assertEqual(result["reasons"], [])

    def test_partial_footprint_with_owned_labels_is_still_owned(self):
        # Deliberately NOT a completeness check: only 1 of 5 Deployments exists, but it is correctly ours -- OWNED, not BROKEN. This is exactly the property that lets 20-sub-argocd.yaml's idempotent helm upgrade --install finish the rollout without this classifier needing to know it was incomplete.
        cluster = FakeCluster()
        cluster.put("namespace", NAMESPACE, None, {"metadata": {}})
        cluster.put("deployment", "argocd-server", NAMESPACE, {"metadata": {"labels": _owned_labels()}})
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_OWNED)

    def test_missing_crds_alone_never_forces_broken(self):
        # Only 1 of 3 CRDs installed -- incomplete, but not itself an ownership conflict; a CRD has no per-release identity to check anyway.
        cluster = _populate_owned_core(FakeCluster())
        cluster.objects.pop(("crd", argocd_state.REQUIRED_CRDS[1], None))
        cluster.objects.pop(("crd", argocd_state.REQUIRED_CRDS[2], None))
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_OWNED)

    def test_missing_repository_secrets_never_forces_broken(self):
        # All four repository Secrets missing -- exactly the real live incident this architecture must auto-repair via normal reconciliation, never a special-cased "reconcilable" carve-out.
        cluster = _populate_owned_core(FakeCluster())
        for name in argocd_state.REPOSITORY_SECRET_NAMES:
            cluster.objects.pop(("secret", name, NAMESPACE))
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_OWNED)

    def test_foreign_deployment_label_is_broken(self):
        cluster = _populate_owned_core(FakeCluster())
        cluster.put("deployment", "argocd-server", NAMESPACE, {"metadata": {"labels": {"app.kubernetes.io/instance": "some-other-release"}}})
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_BROKEN)
        self.assertTrue(any("deployment/argocd-server" in r and "foreign" in r for r in result["reasons"]))

    def test_foreign_ecr_token_sync_serviceaccount_is_broken(self):
        cluster = _populate_owned_core(FakeCluster())
        cluster.put("serviceaccount", argocd_state.ECR_TOKEN_SYNC_NAME, NAMESPACE, {"metadata": {"labels": {"app.kubernetes.io/part-of": "someone-else"}}})
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_BROKEN)

    def test_footprint_without_owning_namespace_is_broken(self):
        # A resource exists but the namespace itself is absent -- ambiguous, mirrors runtime_state.py/monitor_state.py's own "Application does not exist but footprint exists" pattern.
        cluster = FakeCluster()
        cluster.put("deployment", "argocd-server", NAMESPACE, {"metadata": {"labels": _owned_labels()}})
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_BROKEN)

    def test_ingress_owned_present_is_owned(self):
        cluster = _populate_owned_core(FakeCluster())
        cluster.put("ingress", argocd_state.INGRESS_NAME, NAMESPACE, {"metadata": {"labels": dict(argocd_state.INGRESS_OWNERSHIP_LABELS)}})
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_OWNED)

    def test_ingress_foreign_labels_is_broken(self):
        cluster = _populate_owned_core(FakeCluster())
        cluster.put("ingress", argocd_state.INGRESS_NAME, NAMESPACE, {"metadata": {"labels": {"app.kubernetes.io/name": "someone-elses-ingress"}}})
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_BROKEN)
        self.assertTrue(any("ingress/" in r and "foreign" in r for r in result["reasons"]))

    def test_ingress_absent_is_never_a_reason(self):
        # Ownership does not care whether the Ingress is "desired" at all -- that is an acceptance-time concern. Its mere absence contributes nothing here.
        cluster = _populate_owned_core(FakeCluster())
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_OWNED)
        self.assertFalse(any("ingress" in r for r in result["reasons"]))

    def test_ingress_drift_never_blocks_ownership(self):
        # A completely wrong host/group/cert/scheme on an OWNED Ingress is a desired-state acceptance concern, never an ownership-safety concern -- proves the generic architecture goal directly (a values-driven field change never needs this classifier's help).
        cluster = _populate_owned_core(FakeCluster())
        cluster.put("ingress", argocd_state.INGRESS_NAME, NAMESPACE, {
            "metadata": {"labels": dict(argocd_state.INGRESS_OWNERSHIP_LABELS)},
            "spec": {"rules": [{"host": "totally-wrong-host.example.com"}]},
            "status": {},
        })
        result = _classify(cluster)
        self.assertEqual(result["state"], argocd_state.STATE_OWNED)

    def test_kubectl_command_error_raises_inspection_error_not_absent(self):
        cluster = FakeCluster()
        cluster.fail("namespace", NAMESPACE, None, "Unable to connect to the server: dial tcp: i/o timeout")
        with self.assertRaises(argocd_state.ClassifierInspectionError):
            _classify(cluster)

    def test_permission_denied_raises_inspection_error_not_absent(self):
        cluster = FakeCluster()
        cluster.fail("namespace", NAMESPACE, None, "Error from server (Forbidden): namespaces is forbidden")
        with self.assertRaises(argocd_state.ClassifierInspectionError):
            _classify(cluster)

    def test_malformed_json_raises_inspection_error(self):
        cluster = FakeCluster()

        def bad_run(args):
            if args[:2] == ["get", "namespace"]:
                return 0, "not valid json{{{", ""
            return cluster(args)

        with self.assertRaises(argocd_state.ClassifierInspectionError):
            argocd_state.classify(bad_run, environment="dev", namespace=NAMESPACE)

    def test_classify_signature_takes_no_desired_state_arguments(self):
        # Structural proof of the generic-architecture goal: ownership classify() takes only (run, environment, namespace) -- no ecr_registry/role-arn/host/group/certificate/ingress-values -- so it can never need updating merely because a desired-state value changed.
        import inspect
        params = list(inspect.signature(argocd_state.classify).parameters)
        self.assertEqual(params, ["run", "environment", "namespace"])


class ArgoCdStateNoMutationSourceSweepTests(unittest.TestCase):
    """Static source-safety proof: the classifier module must never construct a mutating kubectl/helm command."""

    FORBIDDEN_SUBSTRINGS = (
        "kubectl apply", "kubectl create", "kubectl delete", "kubectl patch",
        "kubectl annotate", "kubectl label",
        "helm install", "helm upgrade", "helm uninstall",
        '"apply"', "'apply'", '"create"', "'create'", '"delete"', "'delete'",
        '"patch"', "'patch'", '"annotate"', "'annotate'", '"label"', "'label'",
    )

    def test_source_contains_no_mutating_command(self):
        with open(TOOL_PATH) as f:
            source = f.read()
        hits = [s for s in self.FORBIDDEN_SUBSTRINGS if s in source]
        self.assertEqual(hits, [], f"classifier source contains a mutating-looking construct: {hits}")

    def test_every_kubectl_get_json_call_uses_get_verb_only(self):
        cluster = _populate_owned_core(FakeCluster())
        cluster.put("ingress", argocd_state.INGRESS_NAME, NAMESPACE, {"metadata": {"labels": dict(argocd_state.INGRESS_OWNERSHIP_LABELS)}})
        _classify(cluster)  # exercises every _get_json call site, including ingress


if __name__ == "__main__":
    unittest.main()
