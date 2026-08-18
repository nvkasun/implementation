#!/usr/bin/env python3
"""hack/orchestration/monitor_acceptance.py: read-only GoldenGate shared monitor post-reconciliation acceptance classifier (Phase B3B) -- answers exactly one question, "is the shared gg-monitor deployment fully healthy right now, and does it see the SAME canonical registry as the current global active runtime inventory?", as one of HEALTHY/BROKEN. Unlike hack/orchestration/monitor_state.py (a pre-reconciliation ownership-safety preflight), this tool DOES require full readiness/health: a monitor that is missing, unhealthy, or shaped incorrectly after reconciliation is BROKEN. Never mutates the cluster: every kubectl invocation here is a `get`/`get -l` (read-only); no apply/create/delete/patch/annotate/label/helm call exists in this module, and it never performs an HTTP request itself -- the actual /healthz and /readyz in-pod HTTP checks are performed by the calling workflow (bounded kubectl exec, read-only) against the exact Ready pod this tool selects and returns; their results are only OPTIONALLY fed back in via --healthz-status/--readyz-status for a second, final pass. The canonical registry document (hack/goldengate-deployment-model.py registry) is always passed in as an already-generated local file -- this tool never independently rebuilds it."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys

import yaml


def _load_sibling_module(name, filename):
    """Lazy import of a same-directory hack/orchestration/ module by explicit file path -- the same importlib.util convention this repo already uses for hack/goldengate-environment.py, so this module never depends on sys.path/CWD."""
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
deployment_ready = _k8s_common.deployment_ready

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

_ENVIRONMENT_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "goldengate-environment.py")
_environment_module = None


def _load_environment_module():
    """Lazy import of hack/goldengate-environment.py -- the single canonical environment-config parser/deriver. Never a second independent schema implementation."""
    global _environment_module
    if _environment_module is None:
        spec = importlib.util.spec_from_file_location("goldengate_environment", _ENVIRONMENT_MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _environment_module = module
    return _environment_module


def environment_derived_values(environment):
    """Loads+validates+derives envs/<environment>/environment.yaml via the canonical resolver."""
    env_module = _load_environment_module()
    env_module.REPO_ROOT = REPO_ROOT
    doc = env_module.load_environment_config(environment)
    return env_module.derive_values(doc)


def _deep_merge(base, override):
    """Mirrors Helm's own values merge semantics closely enough for read-only acceptance purposes: dicts merge key-by-key recursively; any other type (scalar, list) is replaced outright by the override."""
    if not isinstance(base, dict) or not isinstance(override, dict):
        return override
    merged = dict(base)
    for key, value in override.items():
        merged[key] = _deep_merge(merged[key], value) if key in merged else value
    return merged


def _load_monitor_values(environment):
    """helm/goldengate-monitor/values.yaml (chart defaults) merged with envs/<environment>/goldengate-monitor/values.yaml (the same two files Argo CD's `valueFiles: [values-deployment.yaml]` + chart default resolve at install time) -- never a third, independently-authored values schema."""
    base_path = os.path.join(REPO_ROOT, "helm", "goldengate-monitor", "values.yaml")
    override_path = os.path.join(REPO_ROOT, "envs", environment, "goldengate-monitor", "values.yaml")
    with open(base_path) as f:
        base = yaml.safe_load(f) or {}
    override = {}
    if os.path.exists(override_path):
        with open(override_path) as f:
            override = yaml.safe_load(f) or {}
    return _deep_merge(base, override)


STATE_HEALTHY = "HEALTHY"
STATE_BROKEN = "BROKEN"

# Current Helm/main-workflow naming contract (helm/goldengate-monitor/templates/, .github/workflows/50-sub-monitor.yaml) -- verified against the real vendored chart, never guessed.
HELM_REPO_PATH = "helm/goldengate-monitor"
RELEASE_NAME = "gg-monitor"
ARGOCD_APP_NAME = "goldengate-monitor"

DEPLOYMENT_NAME = "gg-monitor"
SERVICE_NAME = "gg-monitor"
SERVICE_ACCOUNT_NAME = "gg-monitor"
SECRETPROVIDERCLASS_NAME = "gg-monitor-secrets"
CONFIGMAP_NAME = "goldengate-monitor-canonical-config"
INGRESS_NAME = "gg-monitor"
NETWORKPOLICY_NAME = "gg-monitor"
CONTAINER_NAME = "gg-monitor"

_SELECTOR_LABELS = {"app.kubernetes.io/name": "gg-monitor", "app.kubernetes.io/instance": RELEASE_NAME}

# helm/goldengate-monitor/templates/secretproviderclass.yaml's exact rendered contract -- verified against the real vendored template, never guessed. Every object (admin groups and the shared TLS object) is objectType=secretsmanager. Admin jmesPath entries read the plaintext OGG_ADMIN/OGG_ADMIN_PWD fields of the Secrets Manager JSON secret. The TLS jmesPath path is a JMESPath-quoted identifier (the template's own `path: '"ca-chain.pem"'` YAML scalar parses to the literal string `"ca-chain.pem"`, embedded quote characters included -- required because the field name contains a "." that JMESPath would otherwise treat as a path separator) -- never the bare "ca-chain.pem" without its quoting.
SPC_OBJECT_TYPE = "secretsmanager"
ADMIN_JMESPATH_PATH_USER = "OGG_ADMIN"
ADMIN_JMESPATH_PATH_PASSWORD = "OGG_ADMIN_PWD"
TLS_JMESPATH_PATH = '"ca-chain.pem"'
TLS_JMESPATH_ALIAS = "ca-chain-pem"


def _check_application(run, reasons, argocd_namespace, monitor_namespace, ecr_registry, expected_chart_version, expected_image_repository, expected_image_tag, expected_cloudwatch_publish_enabled, environment, aws_region, monitor_role_arn, monitor_host, alb_group_name, acm_certificate_arn):
    expected_repo_url = f"oci://{ecr_registry}/{HELM_REPO_PATH}"

    found, obj = get_json(run, "application", ARGOCD_APP_NAME, argocd_namespace)
    if not found:
        reasons.append(f"Application {ARGOCD_APP_NAME} does not exist in {argocd_namespace}")
        return

    status = obj.get("status") or {}
    sync_status = (status.get("sync") or {}).get("status")
    health_status = (status.get("health") or {}).get("status")
    if sync_status != "Synced":
        reasons.append(f"Application {ARGOCD_APP_NAME} sync status is {sync_status!r}, expected 'Synced'")
    if health_status != "Healthy":
        reasons.append(f"Application {ARGOCD_APP_NAME} health status is {health_status!r}, expected 'Healthy'")

    spec = obj.get("spec") or {}
    destination = spec.get("destination") or {}
    source = spec.get("source") or {}
    helm_source = source.get("helm") or {}

    if destination.get("namespace") != monitor_namespace:
        reasons.append(f"Application {ARGOCD_APP_NAME} destination.namespace={destination.get('namespace')!r}, expected {monitor_namespace!r}")
    if source.get("repoURL") != expected_repo_url:
        reasons.append(f"Application {ARGOCD_APP_NAME} source.repoURL={source.get('repoURL')!r}, expected {expected_repo_url!r}")
    if source.get("targetRevision") != expected_chart_version:
        reasons.append(f"Application {ARGOCD_APP_NAME} source.targetRevision={source.get('targetRevision')!r}, expected {expected_chart_version!r}")
    if helm_source.get("releaseName") != RELEASE_NAME:
        reasons.append(f"Application {ARGOCD_APP_NAME} source.helm.releaseName={helm_source.get('releaseName')!r}, expected {RELEASE_NAME!r}")

    # .github/workflows/50-sub-monitor.yaml's "Create or update Argo CD Application" step -- the exact parameter SET it sets, never a subset assumed sufficient because Argo health looked fine, and never merely the last of a duplicate name (a dict-comprehension collapse would silently hide a duplicate).
    expected_parameters = {
        "image.repository": expected_image_repository,
        "image.tag": expected_image_tag,
        "cloudwatch.publishEnabled": "true" if expected_cloudwatch_publish_enabled else "false",
        "global.environment": environment,
        "namespace.name": monitor_namespace,
        "aws.region": aws_region,
        "serviceAccount.roleArn": monitor_role_arn,
        "ingress.host": monitor_host,
        "ingress.alb.groupName": alb_group_name,
        "ingress.alb.certificateArn": acm_certificate_arn,
    }

    # An exact desired-state classifier must never silently discard malformed data: spec.source.helm.parameters must itself be a list, every member must be a mapping, and every mapping must carry a usable (non-empty, string) name -- any violation is itself a BROKEN condition, not merely excluded from the exact-set comparison below.
    raw_parameters = helm_source.get("parameters")
    if raw_parameters is None:
        raw_parameters = []
    elif not isinstance(raw_parameters, list):
        reasons.append(f"Application {ARGOCD_APP_NAME} source.helm.parameters is not a list: {raw_parameters!r}")
        raw_parameters = []

    parameter_entries = []
    for index, p in enumerate(raw_parameters):
        if not isinstance(p, dict):
            reasons.append(f"Application {ARGOCD_APP_NAME} source.helm.parameters row #{index} is not an object: {p!r}")
            continue
        param_name = p.get("name")
        if not isinstance(param_name, str) or not param_name:
            reasons.append(f"Application {ARGOCD_APP_NAME} source.helm.parameters row #{index} has a missing/empty/non-string name: {param_name!r}")
            continue
        parameter_entries.append(p)

    parameter_names_list = [p.get("name") for p in parameter_entries]

    # Detect a duplicate parameter NAME before it is ever collapsed into a dict -- {p["name"]: p["value"] for p in ...} would otherwise silently keep only the last occurrence.
    seen_counts = {}
    for name in parameter_names_list:
        seen_counts[name] = seen_counts.get(name, 0) + 1
    duplicate_names = sorted(name for name, count in seen_counts.items() if count > 1)
    if duplicate_names:
        reasons.append(f"Application {ARGOCD_APP_NAME} helm parameters contain duplicate name(s) {duplicate_names!r}")

    actual_parameter_names = set(parameter_names_list)
    expected_parameter_names = set(expected_parameters)

    missing_parameters = expected_parameter_names - actual_parameter_names
    if missing_parameters:
        reasons.append(f"Application {ARGOCD_APP_NAME} helm parameters are missing expected name(s) {sorted(missing_parameters)!r}")

    unexpected_parameters = actual_parameter_names - expected_parameter_names
    if unexpected_parameters:
        reasons.append(f"Application {ARGOCD_APP_NAME} helm parameters contain unexpected name(s) {sorted(unexpected_parameters)!r} -- 50-sub-monitor.yaml creates an exact canonical parameter set")

    actual_parameters = {p.get("name"): p.get("value") for p in parameter_entries}
    for name, expected_value in expected_parameters.items():
        if name not in actual_parameters:
            continue  # already reported as missing above
        actual_value = actual_parameters[name]
        if actual_value != expected_value:
            reasons.append(f"Application {ARGOCD_APP_NAME} helm parameter {name}={actual_value!r}, expected {expected_value!r}")


def _check_namespace_and_serviceaccount(run, reasons, monitor_namespace, environment, monitor_role_arn):
    found, obj = get_json(run, "namespace", monitor_namespace)
    if not found:
        reasons.append(f"namespace/{monitor_namespace} does not exist")
    else:
        # helm/goldengate-monitor/templates/namespace.yaml's own literal label set (never goldengate-monitor.labels).
        labels = (obj.get("metadata") or {}).get("labels") or {}
        if labels.get("app.kubernetes.io/name") != "gg-monitor":
            reasons.append(f"namespace/{monitor_namespace} label app.kubernetes.io/name={labels.get('app.kubernetes.io/name')!r}, expected 'gg-monitor'")
        if labels.get("app.kubernetes.io/managed-by") != "argocd":
            reasons.append(f"namespace/{monitor_namespace} label app.kubernetes.io/managed-by={labels.get('app.kubernetes.io/managed-by')!r}, expected 'argocd'")
        if labels.get("goldengate.adcb/environment") != environment:
            reasons.append(f"namespace/{monitor_namespace} label goldengate.adcb/environment={labels.get('goldengate.adcb/environment')!r}, expected {environment!r}")

    sa_found, sa_obj = get_json(run, "serviceaccount", SERVICE_ACCOUNT_NAME, monitor_namespace)
    if not sa_found:
        reasons.append(f"serviceaccount/{SERVICE_ACCOUNT_NAME} does not exist")
    else:
        annotations = (sa_obj.get("metadata") or {}).get("annotations") or {}
        if annotations.get("eks.amazonaws.com/role-arn") != monitor_role_arn:
            reasons.append(f"serviceaccount/{SERVICE_ACCOUNT_NAME} annotation eks.amazonaws.com/role-arn={annotations.get('eks.amazonaws.com/role-arn')!r}, expected {monitor_role_arn!r}")


def _check_container_port(reasons, container, expected_service_port):
    ports = container.get("ports") or []
    http_ports = [p for p in ports if p.get("name") == "http"]
    if len(ports) != 1 or not http_ports:
        reasons.append(f"deployment/{DEPLOYMENT_NAME} container {CONTAINER_NAME!r} ports={ports!r}, expected exactly one port named 'http'")
        return
    p = http_ports[0]
    if p.get("containerPort") != expected_service_port:
        reasons.append(f"deployment/{DEPLOYMENT_NAME} container {CONTAINER_NAME!r} port 'http' containerPort={p.get('containerPort')!r}, expected {expected_service_port!r}")
    if p.get("protocol", "TCP") != "TCP":
        reasons.append(f"deployment/{DEPLOYMENT_NAME} container {CONTAINER_NAME!r} port 'http' protocol={p.get('protocol')!r}, expected 'TCP'")


def _check_probes(reasons, container):
    for probe_name, expected_path in (("startupProbe", "/healthz"), ("livenessProbe", "/healthz"), ("readinessProbe", "/readyz")):
        probe = container.get(probe_name) or {}
        http_get = probe.get("httpGet") or {}
        if http_get.get("path") != expected_path:
            reasons.append(f"deployment/{DEPLOYMENT_NAME} container {CONTAINER_NAME!r} {probe_name}.httpGet.path={http_get.get('path')!r}, expected {expected_path!r}")
        if http_get.get("port") != "http":
            reasons.append(f"deployment/{DEPLOYMENT_NAME} container {CONTAINER_NAME!r} {probe_name}.httpGet.port={http_get.get('port')!r}, expected 'http'")


def _check_env_vars(reasons, container, expected_env):
    env_list = container.get("env") or []
    actual = {e["name"]: e.get("value") for e in env_list if "name" in e and "value" in e}
    for key, expected_value in expected_env.items():
        if key not in actual:
            reasons.append(f"deployment/{DEPLOYMENT_NAME} container {CONTAINER_NAME!r} is missing expected env var {key!r}")
        elif actual[key] != expected_value:
            reasons.append(f"deployment/{DEPLOYMENT_NAME} container {CONTAINER_NAME!r} env {key}={actual[key]!r}, expected {expected_value!r}")


def _check_container_volume_mounts(reasons, container, canonical_config_root):
    expected = {"tmp": "/tmp", "canonical-config": canonical_config_root, "secrets-store": "/mnt/secrets-store"}
    actual_by_name = {m.get("name"): m for m in (container.get("volumeMounts") or []) if m.get("name")}
    for name, expected_path in expected.items():
        if name not in actual_by_name:
            reasons.append(f"deployment/{DEPLOYMENT_NAME} container {CONTAINER_NAME!r} does not mount volume {name!r}")
            continue
        actual = actual_by_name[name]
        if actual.get("mountPath") != expected_path:
            reasons.append(f"deployment/{DEPLOYMENT_NAME} container {CONTAINER_NAME!r} mounts {name!r} at {actual.get('mountPath')!r}, expected {expected_path!r}")
        if name in ("canonical-config", "secrets-store") and actual.get("readOnly") is not True:
            reasons.append(f"deployment/{DEPLOYMENT_NAME} container {CONTAINER_NAME!r} mount {name!r} readOnly={actual.get('readOnly')!r}, expected true")
    unexpected = set(actual_by_name) - set(expected)
    if unexpected:
        reasons.append(f"deployment/{DEPLOYMENT_NAME} container {CONTAINER_NAME!r} has unexpected volumeMount(s) {sorted(unexpected)!r}")


def _check_pod_volumes(reasons, pod_spec):
    actual_by_name = {v.get("name"): v for v in (pod_spec.get("volumes") or []) if v.get("name")}
    expected_names = {"tmp", "canonical-config", "secrets-store"}

    if "tmp" in actual_by_name:
        if "emptyDir" not in actual_by_name["tmp"]:
            reasons.append(f"deployment/{DEPLOYMENT_NAME} volume 'tmp' is not emptyDir")
    else:
        reasons.append(f"deployment/{DEPLOYMENT_NAME} is missing expected volume 'tmp'")

    if "canonical-config" in actual_by_name:
        cm = actual_by_name["canonical-config"].get("configMap") or {}
        if cm.get("name") != CONFIGMAP_NAME:
            reasons.append(f"deployment/{DEPLOYMENT_NAME} volume 'canonical-config' configMap.name={cm.get('name')!r}, expected {CONFIGMAP_NAME!r}")
        keys = {i.get("key") for i in (cm.get("items") or [])}
        if "goldengate-deployments.yaml" not in keys:
            reasons.append(f"deployment/{DEPLOYMENT_NAME} volume 'canonical-config' does not project key 'goldengate-deployments.yaml'")
    else:
        reasons.append(f"deployment/{DEPLOYMENT_NAME} is missing expected volume 'canonical-config'")

    if "secrets-store" in actual_by_name:
        csi = actual_by_name["secrets-store"].get("csi") or {}
        if csi.get("driver") != "secrets-store.csi.k8s.io":
            reasons.append(f"deployment/{DEPLOYMENT_NAME} volume 'secrets-store' csi.driver={csi.get('driver')!r}, expected 'secrets-store.csi.k8s.io'")
        if csi.get("readOnly") is not True:
            reasons.append(f"deployment/{DEPLOYMENT_NAME} volume 'secrets-store' csi.readOnly={csi.get('readOnly')!r}, expected true")
        spc = (csi.get("volumeAttributes") or {}).get("secretProviderClass")
        if spc != SECRETPROVIDERCLASS_NAME:
            reasons.append(f"deployment/{DEPLOYMENT_NAME} volume 'secrets-store' csi.volumeAttributes.secretProviderClass={spc!r}, expected {SECRETPROVIDERCLASS_NAME!r}")
    else:
        reasons.append(f"deployment/{DEPLOYMENT_NAME} is missing expected volume 'secrets-store'")

    unexpected = set(actual_by_name) - expected_names
    if unexpected:
        reasons.append(f"deployment/{DEPLOYMENT_NAME} has unexpected volume(s) {sorted(unexpected)!r} -- no unexpected sidecar volume may silently pass HEALTHY")


def _check_deployment_and_pod_shape(run, reasons, monitor_namespace, expected_image_repository, expected_image_tag, expected_service_port, expected_env, canonical_config_root):
    found, obj = get_json(run, "deployment", DEPLOYMENT_NAME, monitor_namespace)
    if not found:
        reasons.append(f"deployment/{DEPLOYMENT_NAME} does not exist")
        return

    ready, why = deployment_ready(obj)
    if not ready:
        reasons.append(f"deployment/{DEPLOYMENT_NAME} not ready: {why}")

    spec = obj.get("spec") or {}
    pod_spec = (((spec.get("template") or {}).get("spec")) or {})
    containers = pod_spec.get("containers") or []
    init_containers = pod_spec.get("initContainers") or []

    if init_containers:
        reasons.append(f"deployment/{DEPLOYMENT_NAME} has unexpected initContainer(s) {[c.get('name') for c in init_containers]!r} -- the monitor chart never declares an init container")

    if len(containers) != 1:
        reasons.append(f"deployment/{DEPLOYMENT_NAME} has {len(containers)} container(s) {[c.get('name') for c in containers]!r}, expected exactly 1 (named {CONTAINER_NAME!r})")
    else:
        container = containers[0]
        if container.get("name") != CONTAINER_NAME:
            reasons.append(f"deployment/{DEPLOYMENT_NAME}'s sole container is named {container.get('name')!r}, expected {CONTAINER_NAME!r}")
        expected_image = f"{expected_image_repository}:{expected_image_tag}"
        if container.get("image") != expected_image:
            reasons.append(f"deployment/{DEPLOYMENT_NAME} container {CONTAINER_NAME!r} image={container.get('image')!r}, expected {expected_image!r}")
        _check_container_port(reasons, container, expected_service_port)
        _check_probes(reasons, container)
        _check_env_vars(reasons, container, expected_env)
        _check_container_volume_mounts(reasons, container, canonical_config_root)

    if pod_spec.get("serviceAccountName") != SERVICE_ACCOUNT_NAME:
        reasons.append(f"deployment/{DEPLOYMENT_NAME} pod template serviceAccountName={pod_spec.get('serviceAccountName')!r}, expected {SERVICE_ACCOUNT_NAME!r}")

    for field in ("hostNetwork", "hostPID", "hostIPC"):
        if bool(pod_spec.get(field, False)):
            reasons.append(f"deployment/{DEPLOYMENT_NAME} pod template {field}={pod_spec.get(field)!r}, expected false")

    _check_pod_volumes(reasons, pod_spec)


def _check_canonical_configmap(run, reasons, monitor_namespace, registry):
    found, obj = get_json(run, "configmap", CONFIGMAP_NAME, monitor_namespace)
    if not found:
        reasons.append(f"configmap/{CONFIGMAP_NAME} does not exist")
        return
    raw = (obj.get("data") or {}).get("goldengate-deployments.yaml")
    if raw is None:
        reasons.append(f"configmap/{CONFIGMAP_NAME} is missing data key 'goldengate-deployments.yaml'")
        return
    try:
        actual_doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        reasons.append(f"configmap/{CONFIGMAP_NAME} data key 'goldengate-deployments.yaml' is not valid YAML: {exc}")
        return
    if actual_doc != registry:
        reasons.append(
            f"configmap/{CONFIGMAP_NAME} canonical registry does not semantically match the expected GLOBAL active registry "
            "(hack/goldengate-deployment-model.py registry) -- the monitor may be serving a stale, missing, or extra deployment"
        )


def _check_secretproviderclass(run, reasons, monitor_namespace, aws_region, registry):
    found, obj = get_json(run, "secretproviderclass", SECRETPROVIDERCLASS_NAME, monitor_namespace)
    if not found:
        reasons.append(f"secretproviderclass/{SECRETPROVIDERCLASS_NAME} does not exist")
        return

    spec = obj.get("spec") or {}
    if spec.get("provider") != "aws":
        reasons.append(f"secretproviderclass/{SECRETPROVIDERCLASS_NAME} spec.provider={spec.get('provider')!r}, expected 'aws'")
    params = spec.get("parameters") or {}
    if params.get("region") != aws_region:
        reasons.append(f"secretproviderclass/{SECRETPROVIDERCLASS_NAME} parameters.region={params.get('region')!r}, expected {aws_region!r}")

    objects_yaml = params.get("objects")
    try:
        objects = yaml.safe_load(objects_yaml) if objects_yaml else []
    except yaml.YAMLError:
        reasons.append(f"secretproviderclass/{SECRETPROVIDERCLASS_NAME} parameters.objects is not valid YAML")
        return
    if not isinstance(objects, list):
        reasons.append(f"secretproviderclass/{SECRETPROVIDERCLASS_NAME} parameters.objects did not parse to a list")
        return

    # Expected admin-secret groups derived from the SAME canonical registry the ConfigMap was already proven to match -- never a second, independently-computed inventory.
    expected_groups = {}
    for d in registry.get("deployments") or []:
        if not d.get("enabled"):
            continue
        deployment_name = d.get("name")
        admin_secret = d.get("adminSecret")
        if deployment_name and admin_secret:
            expected_groups.setdefault(admin_secret, set()).add(deployment_name)
    tls_secret = registry.get("tlsSecret")

    # A non-object row in parameters.objects is itself a BROKEN condition -- never silently ignored while building actual_by_object_name (a valid canonical inventory plus one malformed extra row must not pass as HEALTHY).
    actual_by_object_name = {}
    for index, entry in enumerate(objects):
        if not isinstance(entry, dict):
            reasons.append(f"secretproviderclass/{SECRETPROVIDERCLASS_NAME} parameters.objects row #{index} is not an object: {entry!r}")
            continue
        actual_by_object_name.setdefault(entry.get("objectName"), []).append(entry)

    for object_name, entries in actual_by_object_name.items():
        if len(entries) > 1:
            reasons.append(f"secretproviderclass/{SECRETPROVIDERCLASS_NAME} parameters.objects has duplicate objectName {object_name!r} ({len(entries)} entries)")

    expected_object_names = set(expected_groups) | ({tls_secret} if tls_secret else set())
    actual_object_names = set(actual_by_object_name)

    missing_objects = expected_object_names - actual_object_names
    if missing_objects:
        reasons.append(f"secretproviderclass/{SECRETPROVIDERCLASS_NAME} parameters.objects is missing expected objectName(s) {sorted(missing_objects, key=str)!r}")

    unknown_objects = actual_object_names - expected_object_names
    if unknown_objects:
        reasons.append(f"secretproviderclass/{SECRETPROVIDERCLASS_NAME} parameters.objects has unexpected/unknown objectName(s) {sorted(unknown_objects, key=str)!r} -- not part of the canonical registry's adminSecret groups or tlsSecret")

    for admin_secret, expected_names in expected_groups.items():
        entries = actual_by_object_name.get(admin_secret)
        if not entries:
            continue  # already reported as missing above
        entry = entries[0]
        if entry.get("objectType") != SPC_OBJECT_TYPE:
            reasons.append(f"secretproviderclass/{SECRETPROVIDERCLASS_NAME} objectName {admin_secret!r} objectType={entry.get('objectType')!r}, expected {SPC_OBJECT_TYPE!r}")

        # Exact (path, objectAlias) PAIRS, never alias presence alone -- a swapped username/password path, a wrong source field, or a stray extra pair must all be rejected, not just a missing/extra alias NAME.
        expected_pairs = set()
        for deployment_name in expected_names:
            expected_pairs.add((ADMIN_JMESPATH_PATH_USER, f"{deployment_name}-admin-user"))
            expected_pairs.add((ADMIN_JMESPATH_PATH_PASSWORD, f"{deployment_name}-admin-password"))
        _check_jmespath_pairs(reasons, f"secretproviderclass/{SECRETPROVIDERCLASS_NAME} objectName {admin_secret!r}", entry, expected_pairs)

    if tls_secret:
        tls_entries = actual_by_object_name.get(tls_secret)
        if tls_entries:
            entry = tls_entries[0]
            if entry.get("objectType") != SPC_OBJECT_TYPE:
                reasons.append(f"secretproviderclass/{SECRETPROVIDERCLASS_NAME} objectName {tls_secret!r} (TLS) objectType={entry.get('objectType')!r}, expected {SPC_OBJECT_TYPE!r}")

            expected_tls_pairs = {(TLS_JMESPATH_PATH, TLS_JMESPATH_ALIAS)}
            _check_jmespath_pairs(reasons, f"secretproviderclass/{SECRETPROVIDERCLASS_NAME} objectName {tls_secret!r} (TLS)", entry, expected_tls_pairs)


def _check_jmespath_pairs(reasons, label, entry, expected_pairs):
    """Compares the entry's jmesPath list as exact (path, objectAlias) pairs against expected_pairs -- rejects a missing pair, an extra/unknown pair (which also catches a wrong path, a swapped username/password path, and a wrong objectAlias, since any of those changes the pair identity), and a duplicate pair. jmesPath must itself be a list; every member must be a mapping carrying a usable (non-empty, string) path and objectAlias -- any violation is itself a BROKEN condition, never silently discarded. Never inspects secret VALUES."""
    raw_jmespath = entry.get("jmesPath")
    if raw_jmespath is None:
        raw_jmespath = []
    elif not isinstance(raw_jmespath, list):
        reasons.append(f"{label} jmesPath is not a list: {raw_jmespath!r}")
        raw_jmespath = []

    actual_pairs_list = []
    for index, j in enumerate(raw_jmespath):
        if not isinstance(j, dict):
            reasons.append(f"{label} jmesPath row #{index} is not an object: {j!r}")
            continue
        path = j.get("path")
        object_alias = j.get("objectAlias")
        if not isinstance(path, str) or not path or not isinstance(object_alias, str) or not object_alias:
            reasons.append(f"{label} jmesPath row #{index} has a missing/empty/non-string path or objectAlias: path={path!r}, objectAlias={object_alias!r}")
            continue
        actual_pairs_list.append((path, object_alias))

    actual_pairs_set = set(actual_pairs_list)

    if len(actual_pairs_list) != len(actual_pairs_set):
        reasons.append(f"{label} jmesPath contains a duplicate (path, objectAlias) pair")

    missing_pairs = expected_pairs - actual_pairs_set
    if missing_pairs:
        reasons.append(f"{label} jmesPath is missing expected (path, objectAlias) pair(s) {sorted(missing_pairs, key=str)!r}")

    extra_pairs = actual_pairs_set - expected_pairs
    if extra_pairs:
        reasons.append(f"{label} jmesPath has unexpected (path, objectAlias) pair(s) {sorted(extra_pairs, key=str)!r}")


def _check_service(run, reasons, monitor_namespace, expected_service_port):
    found, obj = get_json(run, "service", SERVICE_NAME, monitor_namespace)
    if not found:
        reasons.append(f"service/{SERVICE_NAME} does not exist")
        return

    spec = obj.get("spec") or {}
    if spec.get("type") != "ClusterIP":
        reasons.append(f"service/{SERVICE_NAME} type={spec.get('type')!r}, expected 'ClusterIP'")
    selector = spec.get("selector") or {}
    if selector != _SELECTOR_LABELS:
        reasons.append(f"service/{SERVICE_NAME} selector={selector!r}, expected {_SELECTOR_LABELS!r}")

    ports = spec.get("ports") or []
    http_ports = [p for p in ports if p.get("name") == "http"]
    if len(ports) != 1 or not http_ports:
        reasons.append(f"service/{SERVICE_NAME} ports={ports!r}, expected exactly one port named 'http'")
    else:
        p = http_ports[0]
        if p.get("port") != expected_service_port:
            reasons.append(f"service/{SERVICE_NAME} port 'http' port={p.get('port')!r}, expected {expected_service_port!r}")
        if p.get("targetPort") != "http":
            reasons.append(f"service/{SERVICE_NAME} port 'http' targetPort={p.get('targetPort')!r}, expected 'http'")
        if p.get("protocol", "TCP") != "TCP":
            reasons.append(f"service/{SERVICE_NAME} port 'http' protocol={p.get('protocol')!r}, expected 'TCP'")

    slices = list_json(run, "endpointslices.discovery.k8s.io", namespace=monitor_namespace, label_selector=f"kubernetes.io/service-name={SERVICE_NAME}")
    has_ready_endpoint = any((ep.get("conditions") or {}).get("ready") is True for sl in slices for ep in (sl.get("endpoints") or []))
    if not has_ready_endpoint:
        reasons.append(f"service/{SERVICE_NAME} has no Ready backing endpoint (checked EndpointSlices)")


def _check_ingress(run, reasons, monitor_namespace, monitor_values, monitor_host, alb_group_name, acm_certificate_arn):
    ingress_cfg = monitor_values.get("ingress") or {}
    enabled = bool(ingress_cfg.get("enabled"))
    found, obj = get_json(run, "ingress", INGRESS_NAME, monitor_namespace)

    if not enabled:
        if found:
            reasons.append(f"ingress/{INGRESS_NAME} exists but ingress.enabled=false in the canonical monitor values -- must be absent")
        return

    if not found:
        reasons.append(f"ingress/{INGRESS_NAME} does not exist")
        return

    alb_cfg = ingress_cfg.get("alb") or {}
    spec = obj.get("spec") or {}
    expected_class = ingress_cfg.get("className") or "alb"
    if spec.get("ingressClassName") != expected_class:
        reasons.append(f"ingress/{INGRESS_NAME} spec.ingressClassName={spec.get('ingressClassName')!r}, expected {expected_class!r}")

    rules = spec.get("rules") or []
    if not any(r.get("host") == monitor_host for r in rules):
        reasons.append(f"ingress/{INGRESS_NAME} has no rule with host {monitor_host!r}")

    backend_ok = any(
        (path.get("backend") or {}).get("service", {}).get("name") == SERVICE_NAME
        and ((path.get("backend") or {}).get("service", {}).get("port") or {}).get("name") == "http"
        for rule in rules
        for path in ((rule.get("http") or {}).get("paths") or [])
    )
    if not backend_ok:
        reasons.append(f"ingress/{INGRESS_NAME} has no path backend routing to service {SERVICE_NAME!r} port 'http'")

    annotations = (obj.get("metadata") or {}).get("annotations") or {}
    expected_annotations = {
        "alb.ingress.kubernetes.io/group.name": alb_group_name,
        "alb.ingress.kubernetes.io/certificate-arn": acm_certificate_arn,
        "alb.ingress.kubernetes.io/target-type": alb_cfg.get("targetType") or "ip",
        "alb.ingress.kubernetes.io/backend-protocol": alb_cfg.get("backendProtocol") or "HTTP",
        "alb.ingress.kubernetes.io/healthcheck-protocol": alb_cfg.get("healthcheckProtocol") or "HTTP",
        "alb.ingress.kubernetes.io/healthcheck-path": alb_cfg.get("healthcheckPath") or "/healthz",
    }
    if alb_cfg.get("groupOrder"):
        expected_annotations["alb.ingress.kubernetes.io/group.order"] = str(alb_cfg.get("groupOrder"))
    for name, expected_value in expected_annotations.items():
        actual_value = annotations.get(name)
        if actual_value != expected_value:
            reasons.append(f"ingress/{INGRESS_NAME} annotation {name}={actual_value!r}, expected {expected_value!r}")


def _check_networkpolicy(run, reasons, monitor_namespace, monitor_values, expected_service_port):
    np_cfg = monitor_values.get("networkPolicy") or {}
    enabled = bool(np_cfg.get("enabled"))
    if not enabled:
        return  # disabled -- not required; never redesign networking here.

    found, obj = get_json(run, "networkpolicy", NETWORKPOLICY_NAME, monitor_namespace)
    if not found:
        reasons.append(f"networkpolicy/{NETWORKPOLICY_NAME} does not exist")
        return

    spec = obj.get("spec") or {}
    if ((spec.get("podSelector") or {}).get("matchLabels")) != _SELECTOR_LABELS:
        reasons.append(f"networkpolicy/{NETWORKPOLICY_NAME} podSelector.matchLabels={((spec.get('podSelector') or {}).get('matchLabels'))!r}, expected {_SELECTOR_LABELS!r}")
    if (spec.get("policyTypes") or []) != ["Ingress"]:
        reasons.append(f"networkpolicy/{NETWORKPOLICY_NAME} policyTypes={spec.get('policyTypes')!r}, expected ['Ingress']")

    ports_ok = any(
        p.get("protocol") == "TCP" and p.get("port") == expected_service_port
        for rule in (spec.get("ingress") or [])
        for p in (rule.get("ports") or [])
    )
    if not ports_ok:
        reasons.append(f"networkpolicy/{NETWORKPOLICY_NAME} does not allow ingress TCP/{expected_service_port}")


def _select_ready_pod(run, reasons, monitor_namespace):
    """Deployment -> ReplicaSet -> Pod ownership-chain Ready-pod selection -- never a blind `.items[0]` or a bare label-selector trust. Mirrors .github/workflows/50-sub-monitor.yaml's own CONFIG-gate-preflight pod selection exactly."""
    deploy_found, deploy_obj = get_json(run, "deployment", DEPLOYMENT_NAME, monitor_namespace)
    if not deploy_found:
        reasons.append(f"deployment/{DEPLOYMENT_NAME} does not exist -- cannot select a Ready pod")
        return None
    deploy_uid = (deploy_obj.get("metadata") or {}).get("uid")
    selector = ((deploy_obj.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
    if not selector:
        reasons.append(f"deployment/{DEPLOYMENT_NAME} has no spec.selector.matchLabels -- cannot select a Ready pod")
        return None
    label_selector = ",".join(f"{k}={v}" for k, v in sorted(selector.items()))

    for pod in list_json(run, "pods", namespace=monitor_namespace, label_selector=label_selector):
        metadata = pod.get("metadata") or {}
        if metadata.get("deletionTimestamp"):
            continue
        status = pod.get("status") or {}
        if status.get("phase") != "Running":
            continue
        ready_condition = next((c for c in (status.get("conditions") or []) if c.get("type") == "Ready"), None)
        if not ready_condition or ready_condition.get("status") != "True":
            continue
        pod_spec = pod.get("spec") or {}
        if pod_spec.get("serviceAccountName") != SERVICE_ACCOUNT_NAME:
            continue
        rs_owner = next((o for o in (metadata.get("ownerReferences") or []) if o.get("controller") and o.get("kind") == "ReplicaSet"), None)
        if not rs_owner or not rs_owner.get("name") or not rs_owner.get("uid"):
            continue
        rs_found, rs_obj = get_json(run, "replicaset", rs_owner.get("name"), monitor_namespace)
        if not rs_found:
            continue
        # The pod's OWN claimed ReplicaSet uid must match the ACTUAL fetched ReplicaSet's uid -- a name match alone does not prove the pod is owned by the CURRENT ReplicaSet object (a stale/recreated ReplicaSet can share a name with a different uid). Missing name/uid on either side is never treated as a valid ownership chain.
        rs_metadata = rs_obj.get("metadata") or {}
        if rs_owner.get("uid") != rs_metadata.get("uid"):
            continue
        deploy_owner = next((o for o in (rs_metadata.get("ownerReferences") or []) if o.get("controller") and o.get("kind") == "Deployment"), None)
        if not deploy_owner or not deploy_owner.get("uid") or not deploy_owner.get("name"):
            continue
        if deploy_owner.get("name") != DEPLOYMENT_NAME or deploy_owner.get("uid") != deploy_uid:
            continue
        return metadata.get("name")

    reasons.append(f"no Ready pod found for deployment/{DEPLOYMENT_NAME} via the Deployment->ReplicaSet->Pod ownership chain")
    return None


def classify(
    run,
    environment,
    argocd_namespace,
    monitor_namespace,
    ecr_registry,
    aws_region,
    dns_domain,
    alb_group_name,
    acm_certificate_arn,
    monitor_role_arn,
    expected_image_repository,
    expected_image_tag,
    expected_chart_version,
    expected_cloudwatch_publish_enabled,
    registry,
    healthz_status=None,
    readyz_status=None,
):
    """Returns the stable {"state", "environment", "namespace", "reasons", "checks"} shape (state is HEALTHY or BROKEN only -- there is no ABSENT here: an expected monitor that is missing after reconciliation is itself BROKEN). checks["ready_pod_name"] carries the ownership-chain-verified Ready pod name (or None) so the calling workflow can target its bounded in-pod HTTP checks at that exact, already-verified pod. Raises ClassifierInspectionError if Kubernetes access itself could not be trusted."""
    monitor_values = _load_monitor_values(environment)
    monitor_host = f"monitor.{dns_domain}"

    dynamodb_table = (monitor_values.get("dynamodb") or {}).get("tableName")
    canonical_config_root = (monitor_values.get("canonicalConfig") or {}).get("root")
    stale_after_seconds = monitor_values.get("staleAfterSeconds")
    refresh_seconds = monitor_values.get("refreshSeconds")
    service_port = (monitor_values.get("service") or {}).get("port")
    if not all([dynamodb_table, canonical_config_root, stale_after_seconds, refresh_seconds, service_port]):
        raise ValueError(
            f"canonical monitor values for {environment!r} are missing one or more required fields "
            "(dynamodb.tableName, canonicalConfig.root, staleAfterSeconds, refreshSeconds, service.port)"
        )

    expected_env = {
        "AWS_REGION": aws_region,
        "AWS_DEFAULT_REGION": aws_region,
        "DYNAMODB_TABLE": str(dynamodb_table),
        "REPO_CONFIG_ROOT": str(canonical_config_root),
        "PORT": str(service_port),
        "STALE_AFTER_SECONDS": str(stale_after_seconds),
        "REFRESH_SECONDS": str(refresh_seconds),
        "CLOUDWATCH_PUBLISH_ENABLED": "true" if expected_cloudwatch_publish_enabled else "false",
    }

    reasons = []
    checks = {}

    _check_application(
        run, reasons, argocd_namespace, monitor_namespace, ecr_registry, expected_chart_version,
        expected_image_repository, expected_image_tag, expected_cloudwatch_publish_enabled,
        environment, aws_region, monitor_role_arn, monitor_host, alb_group_name, acm_certificate_arn,
    )
    _check_namespace_and_serviceaccount(run, reasons, monitor_namespace, environment, monitor_role_arn)
    _check_deployment_and_pod_shape(run, reasons, monitor_namespace, expected_image_repository, expected_image_tag, service_port, expected_env, canonical_config_root)
    _check_canonical_configmap(run, reasons, monitor_namespace, registry)
    _check_secretproviderclass(run, reasons, monitor_namespace, aws_region, registry)
    _check_service(run, reasons, monitor_namespace, service_port)
    _check_ingress(run, reasons, monitor_namespace, monitor_values, monitor_host, alb_group_name, acm_certificate_arn)
    _check_networkpolicy(run, reasons, monitor_namespace, monitor_values, service_port)

    pod_name = _select_ready_pod(run, reasons, monitor_namespace)
    checks["ready_pod_name"] = pod_name

    # /healthz and /readyz are performed by the calling workflow (bounded, read-only kubectl exec against the exact pod_name above) -- this classifier only folds their results in when provided, on a second/final pass. Never treated as end-to-end success on their own, and never evaluated against an unverified pod.
    if healthz_status is not None or readyz_status is not None:
        if pod_name is None:
            reasons.append("cannot verify /healthz or /readyz -- no verified Ready pod was found")
        else:
            if healthz_status != 200:
                reasons.append(f"pod/{pod_name} GET http://127.0.0.1:8080/healthz returned HTTP {healthz_status!r}, expected 200")
            if readyz_status != 200:
                reasons.append(f"pod/{pod_name} GET http://127.0.0.1:8080/readyz returned HTTP {readyz_status!r}, expected 200")

    state = STATE_BROKEN if reasons else STATE_HEALTHY
    return {"state": state, "environment": environment, "namespace": monitor_namespace, "reasons": reasons, "checks": checks}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--expected-image-repository", required=True)
    parser.add_argument("--expected-image-tag", required=True)
    parser.add_argument("--expected-chart-version", required=True)
    parser.add_argument("--expected-cloudwatch-publish-enabled", required=True, choices=["true", "false"])
    parser.add_argument("--registry-file", required=True, help="Path to a locally-generated `hack/goldengate-deployment-model.py registry` YAML/JSON document -- never rebuilt independently by this tool.")
    parser.add_argument("--healthz-status", type=int, default=None, help="HTTP status already observed by the caller for GET /healthz against the verified Ready pod (bounded kubectl exec, performed by the calling workflow -- never by this tool).")
    parser.add_argument("--readyz-status", type=int, default=None, help="HTTP status already observed by the caller for GET /readyz against the verified Ready pod (bounded kubectl exec, performed by the calling workflow -- never by this tool).")
    parser.add_argument("--kubectl-bin", default="kubectl")
    args = parser.parse_args(argv)

    try:
        values = environment_derived_values(args.environment)
        with open(args.registry_file) as f:
            registry = yaml.safe_load(f)
        run = KubectlRunner(args.kubectl_bin)
        result = classify(
            run,
            environment=args.environment,
            argocd_namespace=values["ARGOCD_NAMESPACE"],
            monitor_namespace=values["MONITOR_NAMESPACE"],
            ecr_registry=values["ECR_REGISTRY"],
            aws_region=values["AWS_REGION"],
            dns_domain=values["DNS_DOMAIN"],
            alb_group_name=values["ALB_GROUP_NAME"],
            acm_certificate_arn=values["ACM_CERTIFICATE_ARN"],
            monitor_role_arn=values["MONITOR_ROLE_ARN"],
            expected_image_repository=args.expected_image_repository,
            expected_image_tag=args.expected_image_tag,
            expected_chart_version=args.expected_chart_version,
            expected_cloudwatch_publish_enabled=(args.expected_cloudwatch_publish_enabled == "true"),
            registry=registry,
            healthz_status=args.healthz_status,
            readyz_status=args.readyz_status,
        )
    except ValueError as exc:
        print(f"CONFIGURATION ERROR: {exc}", file=sys.stderr)
        return 1
    except (ClassifierInspectionError, OSError) as exc:
        print(f"INSPECTION ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result))
    if result["reasons"]:
        print("GoldenGate monitor acceptance diagnostics:", file=sys.stderr)
        for reason in result["reasons"]:
            print(f"  - {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
