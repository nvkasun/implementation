#!/usr/bin/env python3
"""automation/orchestration/runtime_acceptance.py: read-only GoldenGate runtime post-reconciliation acceptance classifier (Phase B3A) -- answers exactly one question, "is this active GoldenGate runtime deployment fully healthy right now?", as one of HEALTHY/BROKEN. Unlike automation/orchestration/runtime_state.py (a pre-reconciliation ownership-safety preflight), this tool DOES require full readiness/health: an active desired runtime that is missing, unhealthy, or shaped incorrectly is BROKEN. Never mutates the cluster: every kubectl invocation here is a `get` (read-only); no apply/create/delete/patch/annotate/label/helm call exists in this module, and it never calls AWS directly -- the expected managed-EFS filesystem ID (when applicable) is resolved read-only by the calling workflow and passed in via --expected-efs-file-system-id. Consumes deployment identity through automation/goldengate-deployment-model.py's `describe` output, never a second descriptor schema."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys

import yaml


def _load_sibling_module(name, filename):
    """Lazy import of a same-directory automation/orchestration/ module by explicit file path -- the same importlib.util convention this repo already uses for automation/goldengate-environment.py, so this module never depends on sys.path/CWD."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_k8s_common = _load_sibling_module("k8s_common", "k8s_common.py")
ClassifierInspectionError = _k8s_common.ClassifierInspectionError
KubectlRunner = _k8s_common.KubectlRunner
get_json = _k8s_common.get_json
list_json = _k8s_common.list_json
statefulset_ready = _k8s_common.statefulset_ready

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

_ENVIRONMENT_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "goldengate-environment.py")
_DEPLOYMENT_MODEL_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "goldengate-deployment-model.py")
_environment_module = None
_deployment_model_module = None


def _load_environment_module():
    """Lazy import of automation/goldengate-environment.py -- the single canonical environment-config parser/deriver. Never a second independent schema implementation."""
    global _environment_module
    if _environment_module is None:
        spec = importlib.util.spec_from_file_location("goldengate_environment", _ENVIRONMENT_MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _environment_module = module
    return _environment_module


def _load_deployment_model_module():
    """Lazy import of automation/goldengate-deployment-model.py -- the single canonical folder-driven descriptor resolver. Never a second independent descriptor schema."""
    global _deployment_model_module
    if _deployment_model_module is None:
        spec = importlib.util.spec_from_file_location("goldengate_deployment_model", _DEPLOYMENT_MODEL_MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _deployment_model_module = module
    return _deployment_model_module


def environment_derived_values(environment):
    """Loads+validates+derives envs/<environment>/environment.yaml via the canonical resolver."""
    env_module = _load_environment_module()
    env_module.REPO_ROOT = REPO_ROOT
    doc = env_module.load_environment_config(environment)
    return env_module.derive_values(doc)


def describe_deployment(environment, deployment_id):
    """Returns the canonical descriptor dict for one deployment ID via automation/goldengate-deployment-model.py's own scan/validation -- exactly what `describe` prints, never re-parsed independently. Raises ValueError (a configuration error, never a cluster inspection error) if the folder-driven model itself has a problem or the deployment ID is unknown."""
    gdm = _load_deployment_model_module()
    gdm.REPO_ROOT = REPO_ROOT
    active, inactive, invalid, problems = gdm._run_full_validation(environment)
    if invalid or problems:
        raise ValueError(f"the folder-driven deployment model for {environment!r} has validation problems -- refusing to accept a runtime against an inconsistent model")
    by_id = {d["deploymentId"]: d for d in active + inactive}
    descriptor = by_id.get(deployment_id)
    if descriptor is None:
        raise ValueError(f"unknown deployment ID {deployment_id!r} in environment {environment!r} -- no envs/{environment}/{deployment_id}/values.yaml descriptor was found")
    return descriptor


STATE_HEALTHY = "HEALTHY"
STATE_BROKEN = "BROKEN"

HELM_REPO_PATH = "helm/goldengate"
INIT_CONTAINER_NAME = "prepare-u02-permissions"
RUNTIME_SELECTOR_LABELS_TEMPLATE = {"app.kubernetes.io/name": "goldengate"}

# helm/goldengate/templates/runtime-statefulset.yaml's fixed CSI volume names and driver -- never guessed, never a second desired shape.
ADMIN_CSI_VOLUME_NAME = "ogg-admin-csi"
CERTIFICATE_CSI_VOLUME_NAME = "ogg-nginx-cert-csi"
SECRETS_STORE_CSI_DRIVER = "secrets-store.csi.k8s.io"


def _app_suffix(deployment_id):
    """APP_SUFFIX="${DEPLOYMENT_ID#gg-}" -- strips a leading "gg-" only if present, exactly like the real workflow's own bash parameter expansion."""
    if deployment_id.startswith("gg-"):
        return deployment_id[len("gg-"):]
    return deployment_id


def _check_application(run, reasons, environment, deployment_id, argocd_namespace, runtime_namespace, ecr_registry):
    app_suffix = _app_suffix(deployment_id)
    app_name = f"goldengate-{environment}-{app_suffix}"
    expected_repo_url = f"oci://{ecr_registry}/{HELM_REPO_PATH}"

    found, obj = get_json(run, "application", app_name, argocd_namespace)
    if not found:
        reasons.append(f"Application {app_name} does not exist in {argocd_namespace}")
        return

    status = obj.get("status") or {}
    sync_status = (status.get("sync") or {}).get("status")
    health_status = (status.get("health") or {}).get("status")
    if sync_status != "Synced":
        reasons.append(f"Application {app_name} sync status is {sync_status!r}, expected 'Synced'")
    if health_status != "Healthy":
        reasons.append(f"Application {app_name} health status is {health_status!r}, expected 'Healthy'")

    labels = (obj.get("metadata") or {}).get("labels") or {}
    if labels.get("goldengate.adcb/environment") != environment:
        reasons.append(f"Application {app_name} label goldengate.adcb/environment={labels.get('goldengate.adcb/environment')!r}, expected {environment!r}")
    if labels.get("goldengate.adcb/deployment-id") != deployment_id:
        reasons.append(f"Application {app_name} label goldengate.adcb/deployment-id={labels.get('goldengate.adcb/deployment-id')!r}, expected {deployment_id!r}")

    spec = obj.get("spec") or {}
    destination = spec.get("destination") or {}
    source = spec.get("source") or {}
    helm_source = source.get("helm") or {}

    if destination.get("namespace") != runtime_namespace:
        reasons.append(f"Application {app_name} destination.namespace={destination.get('namespace')!r}, expected {runtime_namespace!r}")
    if source.get("repoURL") != expected_repo_url:
        reasons.append(f"Application {app_name} source.repoURL={source.get('repoURL')!r}, expected {expected_repo_url!r}")
    if helm_source.get("releaseName") != deployment_id:
        reasons.append(f"Application {app_name} source.helm.releaseName={helm_source.get('releaseName')!r}, expected {deployment_id!r}")


def _expected_u02_claim_name(descriptor, deployment_id):
    """Mirrors helm/goldengate/templates/runtime-statefulset.yaml's u02 volume claimName resolution exactly for the two PVC-backed u02Type values. Returns None for emptyDir (no PVC) or an unrecognized/unset u02Type."""
    u02_type = descriptor.get("u02Type")
    override = descriptor.get("pvcClaimName") or ""
    if u02_type == "existingClaim":
        # The chart reads runtime.storage.u02.existingClaim directly in this branch -- never a fallback to claimName or a chart-derived name.
        return override or None
    if u02_type == "efs":
        # existingClaim (if set) takes priority over claimName; otherwise the chart-derived <deployment-id>-u02 name (helm/goldengate/templates/_helpers.tpl's goldengate.runtimeU02PVCName).
        return override if override else f"{deployment_id}-u02"
    return None


def _expected_pod_volumes(descriptor, deployment_id):
    """Exact expected spec.template.spec.volumes shape, mirroring helm/goldengate/templates/runtime-statefulset.yaml. Returns {name: {"kind": "emptyDir"|"pvc"|"csi", ...}}."""
    volumes = {}

    if descriptor.get("u02Type") == "emptyDir":
        volumes["u02"] = {"kind": "emptyDir"}
    else:
        volumes["u02"] = {"kind": "pvc", "claimName": _expected_u02_claim_name(descriptor, deployment_id)}

    volumes["u03"] = {"kind": "emptyDir"}

    if descriptor["csiEnabled"] and descriptor["csiAdminEnabled"]:
        volumes[ADMIN_CSI_VOLUME_NAME] = {"kind": "csi", "secretProviderClass": f"{deployment_id}-admin"}
    if descriptor["csiEnabled"] and descriptor["csiCertificateEnabled"]:
        volumes[CERTIFICATE_CSI_VOLUME_NAME] = {"kind": "csi", "secretProviderClass": f"{deployment_id}-certificate"}

    return volumes


def _check_pod_volumes(reasons, deployment_id, pod_spec, descriptor):
    """Proves the pod actually CONSUMES the storage/secret objects the other checks already proved exist -- correct EFS/CSI objects existing must also imply the StatefulSet mounts the correct claim, never merely that both happen to exist independently."""
    expected = _expected_pod_volumes(descriptor, deployment_id)
    extra_allowed = set(descriptor.get("extraVolumeNames") or [])
    actual_by_name = {v.get("name"): v for v in (pod_spec.get("volumes") or []) if v.get("name")}

    for name, spec in expected.items():
        if name not in actual_by_name:
            reasons.append(f"statefulset/{deployment_id} pod is missing expected volume {name!r}")
            continue
        actual = actual_by_name[name]
        if spec["kind"] == "emptyDir":
            if "emptyDir" not in actual:
                reasons.append(f"statefulset/{deployment_id} volume {name!r} is not emptyDir (got {sorted(actual.keys())!r})")
        elif spec["kind"] == "pvc":
            pvc = actual.get("persistentVolumeClaim")
            if not pvc:
                reasons.append(f"statefulset/{deployment_id} volume {name!r} is not a persistentVolumeClaim source (got {sorted(actual.keys())!r})")
            elif pvc.get("claimName") != spec["claimName"]:
                reasons.append(f"statefulset/{deployment_id} volume {name!r} claimName={pvc.get('claimName')!r}, expected {spec['claimName']!r}")
        elif spec["kind"] == "csi":
            csi = actual.get("csi")
            if not csi:
                reasons.append(f"statefulset/{deployment_id} volume {name!r} is not a CSI source (got {sorted(actual.keys())!r})")
            else:
                if csi.get("driver") != SECRETS_STORE_CSI_DRIVER:
                    reasons.append(f"statefulset/{deployment_id} volume {name!r} csi.driver={csi.get('driver')!r}, expected {SECRETS_STORE_CSI_DRIVER!r}")
                if csi.get("readOnly") is not True:
                    reasons.append(f"statefulset/{deployment_id} volume {name!r} csi.readOnly={csi.get('readOnly')!r}, expected true")
                actual_spc = (csi.get("volumeAttributes") or {}).get("secretProviderClass")
                if actual_spc != spec["secretProviderClass"]:
                    reasons.append(f"statefulset/{deployment_id} volume {name!r} csi.volumeAttributes.secretProviderClass={actual_spc!r}, expected {spec['secretProviderClass']!r}")

    unexpected = set(actual_by_name) - set(expected) - extra_allowed
    if unexpected:
        reasons.append(f"statefulset/{deployment_id} pod has unexpected volume(s) {sorted(unexpected)!r} -- not part of the canonical chart wiring or the descriptor's own declared runtime.extraVolumes")


def _expected_container_mounts(descriptor, include_certificate):
    mounts = {"u02": "/u02", "u03": "/u03"}
    if descriptor["csiEnabled"] and descriptor["csiAdminEnabled"]:
        mounts[ADMIN_CSI_VOLUME_NAME] = descriptor["csiAdminMountPath"]
    if include_certificate and descriptor["csiEnabled"] and descriptor["csiCertificateEnabled"]:
        mounts[CERTIFICATE_CSI_VOLUME_NAME] = descriptor["csiCertificateMountPath"]
    return mounts


def _check_container_mounts(reasons, label, container, expected_mounts, extra_allowed):
    actual_by_name = {m.get("name"): m for m in (container.get("volumeMounts") or []) if m.get("name")}

    for name, expected_path in expected_mounts.items():
        if name not in actual_by_name:
            reasons.append(f"{label} does not mount volume {name!r}")
            continue
        actual = actual_by_name[name]
        if actual.get("mountPath") != expected_path:
            reasons.append(f"{label} mounts {name!r} at {actual.get('mountPath')!r}, expected {expected_path!r}")
        if name in (ADMIN_CSI_VOLUME_NAME, CERTIFICATE_CSI_VOLUME_NAME) and actual.get("readOnly") is not True:
            reasons.append(f"{label} mount {name!r} readOnly={actual.get('readOnly')!r}, expected true")

    unexpected = set(actual_by_name) - set(expected_mounts) - extra_allowed
    if unexpected:
        reasons.append(f"{label} has unexpected volumeMount(s) {sorted(unexpected)!r}")


def _check_container_ports(reasons, label, container, expected_ports):
    """Proves named container ports actually resolve to the expected GoldenGate ports -- what a Service's named targetPort ultimately routes to."""
    actual_by_name = {p.get("name"): p for p in (container.get("ports") or []) if p.get("name")}
    if set(actual_by_name) != set(expected_ports):
        reasons.append(f"{label} container ports={sorted(actual_by_name)!r}, expected {sorted(expected_ports)!r}")
    for name, expected_port in expected_ports.items():
        actual = actual_by_name.get(name)
        if actual is None:
            continue
        if actual.get("containerPort") != expected_port:
            reasons.append(f"{label} container port {name!r} containerPort={actual.get('containerPort')!r}, expected {expected_port!r}")
        if actual.get("protocol", "TCP") != "TCP":
            reasons.append(f"{label} container port {name!r} protocol={actual.get('protocol')!r}, expected 'TCP'")


def _expected_selector_labels(deployment_id):
    return {"app.kubernetes.io/name": "goldengate", "app.kubernetes.io/instance": deployment_id}


def _check_statefulset_and_pod_shape(run, reasons, deployment_id, runtime_namespace, descriptor):
    found, obj = get_json(run, "statefulset", deployment_id, runtime_namespace)
    if not found:
        reasons.append(f"statefulset/{deployment_id} does not exist")
        return

    desired_replicas = descriptor["replicas"]
    ready, why = statefulset_ready(obj, desired_replicas)
    if not ready:
        reasons.append(f"statefulset/{deployment_id} not ready: {why}")

    spec = obj.get("spec") or {}
    expected_headless_name = f"{deployment_id}-headless"
    if spec.get("serviceName") != expected_headless_name:
        reasons.append(f"statefulset/{deployment_id} spec.serviceName={spec.get('serviceName')!r}, expected {expected_headless_name!r}")

    expected_selector = _expected_selector_labels(deployment_id)
    actual_match_labels = ((spec.get("selector") or {}).get("matchLabels")) or {}
    if actual_match_labels != expected_selector:
        reasons.append(f"statefulset/{deployment_id} spec.selector.matchLabels={actual_match_labels!r}, expected {expected_selector!r}")

    pod_template_metadata = (((spec.get("template") or {}).get("metadata")) or {})
    pod_labels = pod_template_metadata.get("labels") or {}
    for key, expected_value in expected_selector.items():
        if pod_labels.get(key) != expected_value:
            reasons.append(f"statefulset/{deployment_id} pod template label {key}={pod_labels.get(key)!r}, expected {expected_value!r}")

    pod_spec = (((spec.get("template") or {}).get("spec")) or {})
    containers = pod_spec.get("containers") or []
    init_containers = pod_spec.get("initContainers") or []
    expected_image = f"{descriptor['imageRepository']}:{descriptor['imageTag']}"
    expected_container_ports = {name: port for name, port in descriptor["servicePorts"].items() if port is not None}

    if len(containers) != 1:
        reasons.append(f"statefulset/{deployment_id} has {len(containers)} container(s) {[c.get('name') for c in containers]!r}, expected exactly 1 (named {descriptor['containerName']!r})")
    else:
        container = containers[0]
        if container.get("name") != descriptor["containerName"]:
            reasons.append(f"statefulset/{deployment_id}'s sole container is named {container.get('name')!r}, expected {descriptor['containerName']!r}")
        if container.get("image") != expected_image:
            reasons.append(f"statefulset/{deployment_id} container {descriptor['containerName']!r} image={container.get('image')!r}, expected {expected_image!r}")
        _check_container_mounts(
            reasons, f"statefulset/{deployment_id} container {descriptor['containerName']!r}",
            container, _expected_container_mounts(descriptor, include_certificate=True),
            set(descriptor.get("extraVolumeMountNames") or []),
        )
        _check_container_ports(reasons, f"statefulset/{deployment_id} container {descriptor['containerName']!r}", container, expected_container_ports)

    if descriptor["initPermissionsEnabled"]:
        if len(init_containers) != 1:
            reasons.append(f"statefulset/{deployment_id} has {len(init_containers)} initContainer(s) {[c.get('name') for c in init_containers]!r}, expected exactly 1 (named {INIT_CONTAINER_NAME!r})")
        else:
            init_container = init_containers[0]
            if init_container.get("name") != INIT_CONTAINER_NAME:
                reasons.append(f"statefulset/{deployment_id}'s sole initContainer is named {init_container.get('name')!r}, expected {INIT_CONTAINER_NAME!r}")
            if init_container.get("image") != expected_image:
                reasons.append(f"statefulset/{deployment_id} initContainer {INIT_CONTAINER_NAME!r} image={init_container.get('image')!r}, expected the same GoldenGate runtime image {expected_image!r}")
            # The chart never mounts the certificate CSI secret on the init container -- only u02/u03 and (when enabled) the admin CSI secret.
            _check_container_mounts(
                reasons, f"statefulset/{deployment_id} initContainer {INIT_CONTAINER_NAME!r}",
                init_container, _expected_container_mounts(descriptor, include_certificate=False),
                set(descriptor.get("extraVolumeMountNames") or []),
            )
    elif init_containers:
        reasons.append(f"statefulset/{deployment_id} has unexpected initContainer(s) {[c.get('name') for c in init_containers]!r}, but the canonical descriptor has runtime.initPermissions.enabled=false")

    actual_sa_name = pod_spec.get("serviceAccountName")
    if actual_sa_name != descriptor["runtimeServiceAccountName"]:
        reasons.append(f"statefulset/{deployment_id} pod template serviceAccountName={actual_sa_name!r}, expected {descriptor['runtimeServiceAccountName']!r}")

    _check_pod_volumes(reasons, deployment_id, pod_spec, descriptor)


def _check_storage(run, reasons, environment, deployment_id, runtime_namespace, descriptor, expected_efs_file_system_id):
    if descriptor["efsMode"] is None:
        return

    expected_fs_id = expected_efs_file_system_id if expected_efs_file_system_id is not None else descriptor.get("efsFileSystemId")
    if not expected_fs_id:
        raise ValueError(
            f"persistence.efs.mode={descriptor['efsMode']!r} but no expected EFS filesystem ID is available -- "
            "pass --expected-efs-file-system-id (managed mode always requires the caller to resolve it read-only via AWS first; "
            "existing mode falls back to the descriptor's own committed persistence.efs.fileSystemId)"
        )

    sc_name = f"gg-efs-{environment}-{deployment_id}"
    sc_found, sc_obj = get_json(run, "storageclass", sc_name)
    if not sc_found:
        reasons.append(f"storageclass/{sc_name} does not exist")
    else:
        params = sc_obj.get("parameters") or {}
        if sc_obj.get("provisioner") != "efs.csi.aws.com":
            reasons.append(f"storageclass/{sc_name} provisioner={sc_obj.get('provisioner')!r}, expected 'efs.csi.aws.com'")
        if params.get("provisioningMode") != "efs-ap":
            reasons.append(f"storageclass/{sc_name} parameters.provisioningMode={params.get('provisioningMode')!r}, expected 'efs-ap'")
        if params.get("fileSystemId") != expected_fs_id:
            reasons.append(f"storageclass/{sc_name} parameters.fileSystemId={params.get('fileSystemId')!r}, expected {expected_fs_id!r}")
        if sc_obj.get("reclaimPolicy") != "Retain":
            reasons.append(f"storageclass/{sc_name} reclaimPolicy={sc_obj.get('reclaimPolicy')!r}, expected 'Retain'")

    # Same claimName resolution as the pod-volume-wiring check above (chart-derived name unless the descriptor declares an explicit override) -- the EFS-object checks below must inspect the SAME PVC the pod actually mounts, never a hardcoded assumption.
    pvc_name = _expected_u02_claim_name(descriptor, deployment_id) or f"{deployment_id}-u02"
    pvc_found, pvc_obj = get_json(run, "persistentvolumeclaim", pvc_name, runtime_namespace)
    if not pvc_found:
        reasons.append(f"persistentvolumeclaim/{pvc_name} does not exist")
        return

    pvc_status = pvc_obj.get("status") or {}
    pvc_spec = pvc_obj.get("spec") or {}
    if pvc_status.get("phase") != "Bound":
        reasons.append(f"persistentvolumeclaim/{pvc_name} phase={pvc_status.get('phase')!r}, expected 'Bound'")
    if pvc_spec.get("storageClassName") != sc_name:
        reasons.append(f"persistentvolumeclaim/{pvc_name} storageClassName={pvc_spec.get('storageClassName')!r}, expected {sc_name!r}")

    volume_name = pvc_spec.get("volumeName")
    if not volume_name:
        reasons.append(f"persistentvolumeclaim/{pvc_name} has no bound volumeName")
        return

    pv_found, pv_obj = get_json(run, "persistentvolume", volume_name)
    if not pv_found:
        reasons.append(f"bound persistentvolume/{volume_name} does not exist")
        return

    pv_csi = (pv_obj.get("spec") or {}).get("csi") or {}
    if pv_csi.get("driver") != "efs.csi.aws.com":
        reasons.append(f"persistentvolume/{volume_name} spec.csi.driver={pv_csi.get('driver')!r}, expected 'efs.csi.aws.com'")
    volume_handle = pv_csi.get("volumeHandle") or ""
    if not volume_handle.startswith(f"{expected_fs_id}::"):
        reasons.append(f"persistentvolume/{volume_name} volumeHandle={volume_handle!r} does not reference a dynamically-provisioned EFS Access Point on expected filesystem {expected_fs_id!r}")


def _check_secretproviderclasses(run, reasons, deployment_id, runtime_namespace, aws_region, descriptor):
    for label, spc_suffix, expected_object_name in (
        ("admin", "admin", descriptor["adminSecretName"]),
        ("certificate", "certificate", descriptor["tlsSecretName"]),
    ):
        name = f"{deployment_id}-{spc_suffix}"
        found, obj = get_json(run, "secretproviderclass", name, runtime_namespace)
        if not found:
            reasons.append(f"secretproviderclass/{name} does not exist")
            continue
        spec = obj.get("spec") or {}
        if spec.get("provider") != "aws":
            reasons.append(f"secretproviderclass/{name} spec.provider={spec.get('provider')!r}, expected 'aws'")
        params = spec.get("parameters") or {}
        if params.get("region") != aws_region:
            reasons.append(f"secretproviderclass/{name} parameters.region={params.get('region')!r}, expected {aws_region!r}")

        objects_yaml = params.get("objects")
        try:
            objects = yaml.safe_load(objects_yaml) if objects_yaml else []
        except yaml.YAMLError:
            reasons.append(f"secretproviderclass/{name} parameters.objects is not valid YAML")
            continue
        object_names = [o.get("objectName") for o in (objects or []) if isinstance(o, dict)]
        if expected_object_name not in object_names:
            reasons.append(f"secretproviderclass/{name} parameters.objects does not reference expected objectName {expected_object_name!r} (found {object_names!r})")


def _check_admin_secret(run, reasons, deployment_id, runtime_namespace):
    name = f"{deployment_id}-admin"
    found, obj = get_json(run, "secret", name, runtime_namespace)
    if not found:
        reasons.append(f"secret/{name} does not exist")
        return
    # Presence of the required key NAMES only -- values are never decoded, logged, or compared.
    data_keys = set((obj.get("data") or {}).keys())
    for required_key in ("OGG_ADMIN", "OGG_ADMIN_PWD"):
        if required_key not in data_keys:
            reasons.append(f"secret/{name} is missing required key {required_key!r}")


def _check_service_port_contract(reasons, label, ports_list, expected_ports):
    """Proves the exact TCP target-port contract, not merely name/port -- helm/goldengate/templates/runtime-service.yaml and runtime-headless-service.yaml always set targetPort to the SAME named port and protocol: TCP; a Service that swaps in a numeric/wrong targetPort or a non-TCP protocol routes traffic incorrectly (or not at all) even though its name/port alone look correct."""
    actual_by_name = {p.get("name"): p for p in (ports_list or []) if p.get("name")}
    if set(actual_by_name) != set(expected_ports):
        reasons.append(f"{label} ports={sorted(actual_by_name)!r}, expected {sorted(expected_ports)!r}")
    for name, expected_port in expected_ports.items():
        p = actual_by_name.get(name)
        if p is None:
            continue
        if p.get("port") != expected_port:
            reasons.append(f"{label} port {name!r} port={p.get('port')!r}, expected {expected_port!r}")
        if p.get("targetPort") != name:
            reasons.append(f"{label} port {name!r} targetPort={p.get('targetPort')!r}, expected the same named target port {name!r}")
        if p.get("protocol", "TCP") != "TCP":
            reasons.append(f"{label} port {name!r} protocol={p.get('protocol')!r}, expected 'TCP'")


def _check_services(run, reasons, deployment_id, runtime_namespace, descriptor):
    expected_ports = {k: v for k, v in descriptor["servicePorts"].items() if v is not None}
    expected_selector = _expected_selector_labels(deployment_id)

    found, obj = get_json(run, "service", deployment_id, runtime_namespace)
    if not found:
        reasons.append(f"service/{deployment_id} does not exist")
    else:
        spec = obj.get("spec") or {}
        if spec.get("type") != descriptor["serviceType"]:
            reasons.append(f"service/{deployment_id} type={spec.get('type')!r}, expected {descriptor['serviceType']!r}")
        if (spec.get("selector") or {}) != expected_selector:
            reasons.append(f"service/{deployment_id} selector={spec.get('selector')!r}, expected {expected_selector!r}")
        _check_service_port_contract(reasons, f"service/{deployment_id}", spec.get("ports"), expected_ports)

    headless_name = f"{deployment_id}-headless"
    headless_found, headless_obj = get_json(run, "service", headless_name, runtime_namespace)
    if not headless_found:
        reasons.append(f"service/{headless_name} does not exist")
    else:
        spec = headless_obj.get("spec") or {}
        if spec.get("clusterIP") != "None":
            reasons.append(f"service/{headless_name} clusterIP={spec.get('clusterIP')!r}, expected 'None'")
        if (spec.get("selector") or {}) != expected_selector:
            reasons.append(f"service/{headless_name} selector={spec.get('selector')!r}, expected {expected_selector!r}")
        _check_service_port_contract(reasons, f"service/{headless_name}", spec.get("ports"), expected_ports)

    # At least one Ready backing endpoint for the main (routable) Service -- a Ready StatefulSet with no routable backend is still not acceptable.
    slices = list_json(run, "endpointslices.discovery.k8s.io", namespace=runtime_namespace, label_selector=f"kubernetes.io/service-name={deployment_id}")
    has_ready_endpoint = any(
        (ep.get("conditions") or {}).get("ready") is True
        for sl in slices
        for ep in (sl.get("endpoints") or [])
    )
    if not has_ready_endpoint:
        reasons.append(f"service/{deployment_id} has no Ready backing endpoint (checked EndpointSlices)")


def _check_ingress(run, reasons, deployment_id, runtime_namespace, dns_domain, alb_group_name, acm_certificate_arn, descriptor):
    if not descriptor["ingressEnabled"]:
        return

    name = f"{deployment_id}-ingress"
    found, obj = get_json(run, "ingress", name, runtime_namespace)
    if not found:
        reasons.append(f"ingress/{name} does not exist")
        return

    if (obj.get("metadata") or {}).get("namespace") != runtime_namespace:
        reasons.append(f"ingress/{name} namespace={(obj.get('metadata') or {}).get('namespace')!r}, expected {runtime_namespace!r}")

    spec = obj.get("spec") or {}
    if spec.get("ingressClassName") != descriptor["ingressClassName"]:
        reasons.append(f"ingress/{name} spec.ingressClassName={spec.get('ingressClassName')!r}, expected {descriptor['ingressClassName']!r}")

    rules = spec.get("rules") or []
    expected_host = f"{deployment_id}.{dns_domain}"
    if not any(r.get("host") == expected_host for r in rules):
        reasons.append(f"ingress/{name} has no rule with host {expected_host!r}")

    backend_ok = False
    for rule in rules:
        for path in ((rule.get("http") or {}).get("paths") or []):
            backend_service = (path.get("backend") or {}).get("service") or {}
            if backend_service.get("name") == deployment_id and (backend_service.get("port") or {}).get("name") == "https":
                backend_ok = True
    if not backend_ok:
        reasons.append(f"ingress/{name} has no path backend routing to service {deployment_id!r} port 'https'")

    annotations = (obj.get("metadata") or {}).get("annotations") or {}
    if annotations.get("alb.ingress.kubernetes.io/group.name") != alb_group_name:
        reasons.append(f"ingress/{name} annotation alb.ingress.kubernetes.io/group.name={annotations.get('alb.ingress.kubernetes.io/group.name')!r}, expected {alb_group_name!r}")
    if descriptor["albGroupOrder"] is not None:
        expected_group_order = str(descriptor["albGroupOrder"])
        if annotations.get("alb.ingress.kubernetes.io/group.order") != expected_group_order:
            reasons.append(f"ingress/{name} annotation alb.ingress.kubernetes.io/group.order={annotations.get('alb.ingress.kubernetes.io/group.order')!r}, expected {expected_group_order!r}")
    if annotations.get("alb.ingress.kubernetes.io/certificate-arn") != acm_certificate_arn:
        reasons.append(f"ingress/{name} annotation alb.ingress.kubernetes.io/certificate-arn={annotations.get('alb.ingress.kubernetes.io/certificate-arn')!r}, expected {acm_certificate_arn!r}")
    if annotations.get("alb.ingress.kubernetes.io/target-type") != "ip":
        reasons.append(f"ingress/{name} annotation alb.ingress.kubernetes.io/target-type={annotations.get('alb.ingress.kubernetes.io/target-type')!r}, expected 'ip'")


def classify(run, environment, deployment_id, argocd_namespace, runtime_namespace, ecr_registry, dns_domain, alb_group_name, acm_certificate_arn, aws_region, expected_efs_file_system_id=None):
    """Returns the stable {"state", "environment", "deployment_id", "namespace", "reasons", "checks"} shape (state is HEALTHY or BROKEN only -- there is no ABSENT here: an active desired runtime that is missing is itself BROKEN). Raises ClassifierInspectionError if Kubernetes access itself could not be trusted. Raises ValueError for a configuration error (unknown deployment ID, invalid folder-driven model, or a managed-EFS runtime with no resolvable expected filesystem ID) -- never ABSENT/HEALTHY/BROKEN cluster state."""
    descriptor = describe_deployment(environment, deployment_id)

    reasons = []
    checks = {"deployment_id": deployment_id}

    _check_application(run, reasons, environment, deployment_id, argocd_namespace, runtime_namespace, ecr_registry)
    _check_statefulset_and_pod_shape(run, reasons, deployment_id, runtime_namespace, descriptor)
    _check_storage(run, reasons, environment, deployment_id, runtime_namespace, descriptor, expected_efs_file_system_id)
    _check_secretproviderclasses(run, reasons, deployment_id, runtime_namespace, aws_region, descriptor)
    _check_admin_secret(run, reasons, deployment_id, runtime_namespace)
    _check_services(run, reasons, deployment_id, runtime_namespace, descriptor)
    _check_ingress(run, reasons, deployment_id, runtime_namespace, dns_domain, alb_group_name, acm_certificate_arn, descriptor)

    state = STATE_BROKEN if reasons else STATE_HEALTHY
    return {"state": state, "environment": environment, "deployment_id": deployment_id, "namespace": runtime_namespace, "reasons": reasons, "checks": checks}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--expected-efs-file-system-id", default=None, help="Read-only AWS-resolved expected EFS filesystem ID for persistence.efs.mode=managed runtimes; resolved by the calling workflow, never by this tool. Falls back to the descriptor's own committed fileSystemId for mode=existing when omitted.")
    parser.add_argument("--kubectl-bin", default="kubectl")
    args = parser.parse_args(argv)

    try:
        values = environment_derived_values(args.environment)
        run = KubectlRunner(args.kubectl_bin)
        result = classify(
            run,
            environment=args.environment,
            deployment_id=args.deployment_id,
            argocd_namespace=values["ARGOCD_NAMESPACE"],
            runtime_namespace=values["RUNTIME_NAMESPACE"],
            ecr_registry=values["ECR_REGISTRY"],
            dns_domain=values["DNS_DOMAIN"],
            alb_group_name=values["ALB_GROUP_NAME"],
            acm_certificate_arn=values["ACM_CERTIFICATE_ARN"],
            aws_region=values["AWS_REGION"],
            expected_efs_file_system_id=args.expected_efs_file_system_id,
        )
    except ValueError as exc:
        print(f"CONFIGURATION ERROR: {exc}", file=sys.stderr)
        return 1
    except (ClassifierInspectionError, OSError) as exc:
        print(f"INSPECTION ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result))
    if result["reasons"]:
        print("GoldenGate runtime acceptance diagnostics:", file=sys.stderr)
        for reason in result["reasons"]:
            print(f"  - {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
