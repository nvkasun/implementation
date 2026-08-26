#!/usr/bin/env python3
"""automation/orchestration/k8s_common.py: shared read-only Kubernetes inspection primitives for the Phase B2 classifiers (platform_state.py, observability_state.py). Never mutates the cluster: every helper here issues only `kubectl get` (read-only). This module is new and independent of automation/orchestration/argocd_state.py -- Phase B1's classifier/tests are byte-for-byte untouched by its existence."""
from __future__ import annotations

import json
import subprocess


class ClassifierInspectionError(Exception):
    """Raised when a classifier could not determine truth -- API unreachable, permission denied, malformed JSON, or an unexpected server error. Never conflated with "resource absent"; callers must fail closed on this, not downgrade it to ABSENT."""


class KubectlRunner:
    """Read-only kubectl wrapper. Every call site fed by this module's helpers issues only a `get` subcommand -- never apply/create/delete/patch/annotate/label."""

    def __init__(self, kubectl_bin="kubectl"):
        self.kubectl_bin = kubectl_bin

    def __call__(self, args):
        proc = subprocess.run([self.kubectl_bin, *args], capture_output=True, text=True)
        return proc.returncode, proc.stdout, proc.stderr


def get_json(run, resource, name=None, namespace=None):
    """Runs `kubectl get <resource> [name] [-n namespace] -o json` (read-only). Returns (True, obj) if found, (False, None) if the API server reported NotFound, and raises ClassifierInspectionError for any other failure (auth, connectivity, malformed JSON) -- "could not tell" is never silently treated as "absent"."""
    args = ["get", resource]
    if name:
        args.append(name)
    if namespace:
        args += ["-n", namespace]
    args += ["-o", "json"]
    rc, out, err = run(args)
    if rc == 0:
        try:
            return True, json.loads(out)
        except json.JSONDecodeError as exc:
            raise ClassifierInspectionError(f"kubectl get {resource} {name or ''} returned unparseable JSON: {exc}")
    if "(NotFound)" in err:
        return False, None
    raise ClassifierInspectionError(f"kubectl get {resource} {name or ''} failed: {err.strip() or out.strip() or 'unknown error'}")


def list_json(run, resource, namespace=None, label_selector=None):
    """Runs `kubectl get <resource> [-n namespace] [-l label_selector] -o json` (read-only, no name -- a list call). Returns the .items array (empty list if none match). A cluster where the resource's own CRD/kind was never registered ("doesn't have a resource type" / "no matches for kind") is treated as zero items -- an optional CR kind that was never installed is a legitimate, expected empty result, never an inspection error. Any other failure (auth, connectivity, malformed JSON) raises ClassifierInspectionError."""
    args = ["get", resource]
    if namespace:
        args += ["-n", namespace]
    if label_selector:
        args += ["-l", label_selector]
    args += ["-o", "json"]
    rc, out, err = run(args)
    if rc == 0:
        try:
            doc = json.loads(out)
        except json.JSONDecodeError as exc:
            raise ClassifierInspectionError(f"kubectl get {resource} returned unparseable JSON: {exc}")
        return doc.get("items") or []
    if "doesn't have a resource type" in err or "no matches for kind" in err:
        return []
    raise ClassifierInspectionError(f"kubectl get {resource} failed: {err.strip() or out.strip() or 'unknown error'}")


def _replicaset_like_ready(obj, ready_fields):
    """Shared Deployment/StatefulSet readiness check: observedGeneration caught up, and every field in ready_fields equals the desired replica count."""
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    metadata = obj.get("metadata") or {}
    desired = spec.get("replicas")
    if desired is None:
        desired = 1
    if desired <= 0:
        return False, "spec.replicas is not > 0"
    if metadata.get("generation") != status.get("observedGeneration"):
        return False, f"status.observedGeneration={status.get('observedGeneration')!r} does not match metadata.generation={metadata.get('generation')!r}"
    for field in ready_fields:
        if status.get(field) != desired:
            return False, f"status.{field}={status.get(field)!r}, expected desired replicas={desired}"
    return True, None


def deployment_ready(obj):
    return _replicaset_like_ready(obj, ("updatedReplicas", "readyReplicas", "availableReplicas"))


def daemonset_ready(obj):
    """Exact DaemonSet readiness: observedGeneration caught up, a positive desiredNumberScheduled, and current/updated/ready/available all equal to it with zero unavailable."""
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    metadata = obj.get("metadata") or {}
    if metadata.get("generation") != status.get("observedGeneration"):
        return False, f"status.observedGeneration={status.get('observedGeneration')!r} does not match metadata.generation={metadata.get('generation')!r}"
    desired = status.get("desiredNumberScheduled")
    if not desired or desired <= 0:
        return False, f"status.desiredNumberScheduled={desired!r} is not > 0"
    for field in ("currentNumberScheduled", "updatedNumberScheduled", "numberReady", "numberAvailable"):
        if status.get(field) != desired:
            return False, f"status.{field}={status.get(field)!r}, expected desiredNumberScheduled={desired}"
    if status.get("numberUnavailable", 0) not in (0, None):
        return False, f"status.numberUnavailable={status.get('numberUnavailable')!r}, expected 0"
    return True, None


def statefulset_ready(obj, desired_replicas):
    """Exact StatefulSet readiness: observedGeneration caught up, a positive desired replica count (from the canonical deployment model, never hardcoded), ready/current/updated all equal to it, and -- where the revision fields are present at all -- currentRevision == updateRevision (rollout fully settled, not mid-rollingUpdate)."""
    status = obj.get("status") or {}
    metadata = obj.get("metadata") or {}
    if metadata.get("generation") != status.get("observedGeneration"):
        return False, f"status.observedGeneration={status.get('observedGeneration')!r} does not match metadata.generation={metadata.get('generation')!r}"
    if not desired_replicas or desired_replicas <= 0:
        return False, f"desired replicas {desired_replicas!r} is not > 0"
    for field in ("readyReplicas", "currentReplicas", "updatedReplicas"):
        if status.get(field) != desired_replicas:
            return False, f"status.{field}={status.get(field)!r}, expected desired replicas={desired_replicas}"
    current_revision = status.get("currentRevision")
    update_revision = status.get("updateRevision")
    if current_revision is not None and update_revision is not None and current_revision != update_revision:
        return False, f"status.currentRevision={current_revision!r} != status.updateRevision={update_revision!r} -- rollout not yet settled"
    return True, None


def pod_template_images(obj):
    """Every container/initContainer image reference in a Deployment/DaemonSet/StatefulSet's spec.template.spec -- the actual scheduled pod template, never a Helm values guess."""
    pod_spec = (((obj.get("spec") or {}).get("template") or {}).get("spec")) or {}
    images = []
    for container in (pod_spec.get("containers") or []):
        image = container.get("image")
        if image:
            images.append(image)
    for container in (pod_spec.get("initContainers") or []):
        image = container.get("image")
        if image:
            images.append(image)
    return images
