"""Pure validation for the authorized DEV EFS access-point cleanup; phase1_readiness.py supplies the live AWS/Kubernetes data. Never deletes a filesystem."""
from __future__ import annotations

import re

_ACCESS_POINT_ID_RE = re.compile(r"^fsap-[0-9a-f]{8,40}\Z")
MAX_AUTHORIZED_ACCESS_POINTS = 64

EFS_CSI_DRIVER = "efs.csi.aws.com"
# Tag the EFS CSI driver sets on every access point it provisions; also the IAM condition of the delete permission.
CSI_OWNERSHIP_TAG = ("efs.csi.aws.com/cluster", "true")
DELETABLE_STATE = "available"
IN_PROGRESS_STATE = "deleting"


class CleanupError(Exception):
    """A single fixed, safe-to-print refusal."""


def parse_access_point_ids(raw):
    """Comma/space separated fsap- IDs. Empty means none; anything malformed or duplicated fails."""
    tokens = [t for t in re.split(r"[\s,]+", raw or "") if t]
    if len(tokens) > MAX_AUTHORIZED_ACCESS_POINTS:
        raise CleanupError(f"at most {MAX_AUTHORIZED_ACCESS_POINTS} access points may be authorized in one run.")
    for token in tokens:
        if not _ACCESS_POINT_ID_RE.match(token):
            raise CleanupError(f"authorized access point {token!r} is not a valid EFS access point ID.")
    if len(set(tokens)) != len(tokens):
        raise CleanupError("authorized access point IDs contain duplicates.")
    return sorted(tokens)


def runtime_ids_by_filesystem(authorized_filesystem_ids, sanitized_filesystems):
    """Maps each authorized filesystem to its GoldenGateDeploymentId tag; one runtime per filesystem and vice versa."""
    by_id = {fs.get("FileSystemId"): fs for fs in sanitized_filesystems}
    result = {}
    for filesystem_id in authorized_filesystem_ids:
        fs = by_id.get(filesystem_id)
        if fs is None:
            raise CleanupError(f"authorized filesystem {filesystem_id} is not in the AWS inventory.")
        tags = {t.get("Key"): t.get("Value") for t in fs.get("Tags") or []}
        runtime = tags.get("GoldenGateDeploymentId")
        if not isinstance(runtime, str) or not runtime:
            raise CleanupError(f"authorized filesystem {filesystem_id} has no GoldenGateDeploymentId tag.")
        result[filesystem_id] = runtime
    if len(set(result.values())) != len(result):
        raise CleanupError("two authorized filesystems are tagged with the same runtime; refusing an ambiguous scope.")
    return result


def _tags(access_point):
    return {t.get("Key"): t.get("Value") for t in access_point.get("Tags") or [] if isinstance(t, dict)}


def plan_access_point_deletions(access_points_by_filesystem, authorized_access_point_ids, workload_account_id):
    """{filesystem_id: [AccessPointDescription]} -> (to_delete, in_progress) IDs; raises on any unknown, foreign or non-CSI access point."""
    authorized = set(authorized_access_point_ids)
    to_delete, in_progress = [], []
    for filesystem_id, access_points in sorted(access_points_by_filesystem.items()):
        for ap in access_points:
            ap_id = ap.get("AccessPointId")
            if not isinstance(ap_id, str) or not _ACCESS_POINT_ID_RE.match(ap_id):
                raise CleanupError(f"filesystem {filesystem_id} returned an access point with a malformed ID.")
            if ap_id not in authorized:
                raise CleanupError(f"access point {ap_id} on {filesystem_id} was not authorized; refusing to leave or guess at unknown access points.")
            if ap.get("FileSystemId") != filesystem_id:
                raise CleanupError(f"access point {ap_id} reports filesystem {ap.get('FileSystemId')!r}, expected {filesystem_id}.")
            if ap.get("OwnerId") != workload_account_id:
                raise CleanupError(f"access point {ap_id} is owned by account {ap.get('OwnerId')!r}, expected {workload_account_id}.")
            if _tags(ap).get(CSI_OWNERSHIP_TAG[0]) != CSI_OWNERSHIP_TAG[1]:
                raise CleanupError(f"access point {ap_id} lacks the {CSI_OWNERSHIP_TAG[0]}={CSI_OWNERSHIP_TAG[1]} tag; only CSI-provisioned access points may be removed.")
            root = ((ap.get("RootDirectory") or {}).get("Path") or "").rstrip("/")
            if not root:
                raise CleanupError(f"access point {ap_id} exposes the filesystem root; refusing a shared access point.")
            state = ap.get("LifeCycleState")
            if state == DELETABLE_STATE:
                to_delete.append(ap_id)
            elif state == IN_PROGRESS_STATE:
                in_progress.append(ap_id)
            else:
                raise CleanupError(f"access point {ap_id} is in state {state!r}; refusing to act on it.")
    return sorted(to_delete), sorted(in_progress)


def _canonical_app_name(environment, runtime):
    # Mirrors phase5_runtime._canonical_argocd_app_name.
    suffix = runtime[len("gg-"):] if runtime.startswith("gg-") else runtime
    return f"goldengate-{environment}-{suffix}"


def _references_filesystem(handle, filesystem_ids, access_point_ids):
    if not isinstance(handle, str) or not handle:
        return False
    first = re.split(r"[:/]", handle, maxsplit=1)[0]
    return first in filesystem_ids or any(ap in handle for ap in access_point_ids)


def find_kubernetes_references(objects, filesystem_ids, access_point_ids, runtimes, environment, runtime_namespace):
    """Problems for every object that still uses, could re-provision on, or deploys onto the selected storage; objects maps kind -> items."""
    filesystem_ids = set(filesystem_ids)
    access_point_ids = set(access_point_ids)
    runtimes = set(runtimes)
    problems = []

    storage_classes = {f"gg-efs-{environment}-{r}" for r in runtimes}
    for sc in objects["storageclasses"]:
        name = sc.get("metadata", {}).get("name")
        if name in storage_classes or (sc.get("provisioner") == EFS_CSI_DRIVER and (sc.get("parameters") or {}).get("fileSystemId") in filesystem_ids):
            problems.append(f"StorageClass {name} still provisions onto a selected filesystem")
            storage_classes.add(name)

    flagged_pvs = set()
    for pv in objects["pv"]:
        spec = pv.get("spec") or {}
        name = pv.get("metadata", {}).get("name")
        csi = spec.get("csi") or {}
        nfs_server = (spec.get("nfs") or {}).get("server") or ""
        if _references_filesystem(csi.get("volumeHandle"), filesystem_ids, access_point_ids) or any(fs in nfs_server for fs in filesystem_ids) \
                or spec.get("storageClassName") in storage_classes:
            problems.append(f"PersistentVolume {name} still references a selected filesystem")
            flagged_pvs.add(name)

    flagged_claims = set()
    for pvc in objects["pvc"]:
        meta, spec = pvc.get("metadata", {}), pvc.get("spec") or {}
        key = (meta.get("namespace"), meta.get("name"))
        own_claim = meta.get("namespace") == runtime_namespace and meta.get("name") in {f"{r}-u02" for r in runtimes}
        if spec.get("volumeName") in flagged_pvs or spec.get("storageClassName") in storage_classes or own_claim:
            problems.append(f"PersistentVolumeClaim {key[0]}/{key[1]} still references a selected filesystem or runtime")
            flagged_claims.add(key)

    for pod in objects["pods"]:
        meta, spec = pod.get("metadata", {}), pod.get("spec") or {}
        namespace, name = meta.get("namespace"), meta.get("name") or ""
        uses = False
        for volume in spec.get("volumes") or []:
            claim = (volume.get("persistentVolumeClaim") or {}).get("claimName")
            csi = volume.get("csi") or {}
            if (namespace, claim) in flagged_claims or (csi.get("driver") == EFS_CSI_DRIVER and _references_filesystem((csi.get("volumeAttributes") or {}).get("volumeHandle") or csi.get("volumeHandle"), filesystem_ids, access_point_ids)):
                uses = True
        if namespace == runtime_namespace and any(name.startswith(f"{r}-") for r in runtimes):
            uses = True
        if uses:
            problems.append(f"Pod {namespace}/{name} still runs a selected runtime or mounts a selected filesystem")

    for sts in objects["statefulsets"]:
        meta = sts.get("metadata", {})
        if meta.get("namespace") == runtime_namespace and meta.get("name") in runtimes:
            problems.append(f"StatefulSet {meta.get('namespace')}/{meta.get('name')} still exists")

    app_names = {_canonical_app_name(environment, r) for r in runtimes}
    for app in objects["applications"]:
        meta, spec = app.get("metadata", {}), app.get("spec") or {}
        release = ((spec.get("source") or {}).get("helm") or {}).get("releaseName")
        if meta.get("name") in app_names or release in runtimes:
            problems.append(f"Argo CD Application {meta.get('name')} still deploys a selected runtime")
    for appset in objects["applicationsets"]:
        name = appset.get("metadata", {}).get("name")
        if name in {f"{a}-appset" for a in app_names}:
            problems.append(f"Argo CD ApplicationSet {name} would recreate a selected runtime")
    return problems
