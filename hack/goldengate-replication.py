#!/usr/bin/env python3
"""hack/goldengate-replication.py: PostgreSQL->MSSQL GoldenGate replication reconciler; create-only, fail-on-drift, never restarts/heals/deletes. Consumes hack/goldengate-deployment-model.py for all deployment parsing; never a second YAML parser. Oracle REST request/response shapes below are best-effort from the documented GoldenGate Microservices REST API and have not been verified against a live 23.26.2.0.1 instance in this offline session -- see the Phase 6D1 completion report."""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import importlib.util
import json
import os
import re
import ssl
import sys
import urllib.parse

import yaml

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_DEPLOYMENT_MODEL_PATH = os.path.join(os.path.dirname(__file__), "goldengate-deployment-model.py")

DEFAULT_TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 1_000_000
READINESS_RETRY_COUNT = 3
DEFAULT_JOB_TTL_SECONDS = 3600
NETWORK_CREDENTIAL_DOMAIN = "Network"

_gdm_module = None


def _gdm():
    """Lazy import: worker mode runs inside the reconciliation Job, which never has goldengate-deployment-model.py mounted."""
    global _gdm_module
    if _gdm_module is None:
        spec = importlib.util.spec_from_file_location("goldengate_deployment_model", _DEPLOYMENT_MODEL_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _gdm_module = module
    return _gdm_module


class ReplicationError(Exception):
    """A sanitized, fixed-reason failure; .reason never contains a secret, raw response body, or request payload."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class IndeterminateError(ReplicationError):
    """A mutating request whose outcome is unknown after a transport failure; must never be blindly retried."""


class DriftError(ReplicationError):
    """An existing GoldenGate object differs from the desired, non-secret configuration."""


# REST transport

def _build_ssl_context(ca_file):
    if not ca_file or not os.path.exists(ca_file):
        raise ReplicationError("TLS CA file is missing -- refusing to connect without certificate verification")
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(ca_file)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def read_secret_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        raise ReplicationError("a required mounted secret file is missing") from None


class GGClient:
    """One HTTPS+Basic-auth client bound to a single GoldenGate deployment's wildcard DNS host."""

    def __init__(self, host, username, password, ca_file, port=443, timeout=DEFAULT_TIMEOUT_SECONDS):
        self.host = host
        self.port = port
        self._username = username
        self._password = password
        self._ssl_ctx = _build_ssl_context(ca_file)
        self._timeout = timeout

    def _auth_header(self):
        token = base64.b64encode(f"{self._username}:{self._password}".encode()).decode()
        return {"Authorization": f"Basic {token}", "Accept": "application/json"}

    def _request(self, method, path, body=None, retry=0):
        headers = self._auth_header()
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"

        attempt = 0
        while True:
            conn = http.client.HTTPSConnection(self.host, self.port, timeout=self._timeout, context=self._ssl_ctx)
            try:
                conn.request(method, path, body=payload, headers=headers)
                resp = conn.getresponse()
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ReplicationError(f"{method} response exceeded the bounded size limit")
                return resp.status, raw.decode("utf-8", errors="replace")
            except ReplicationError:
                raise
            except Exception:
                if method == "GET" and attempt < retry:
                    attempt += 1
                    continue
                if method != "GET":
                    raise IndeterminateError(f"{method} {_redact_path(path)} outcome is unknown after a transport error") from None
                raise ReplicationError(f"GET {_redact_path(path)} failed: transport error") from None
            finally:
                conn.close()

    def get(self, path, retry=0):
        status, text = self._request("GET", path, retry=retry)
        return status, _parse_json_body(status, text)

    def post(self, path, body):
        status, text = self._request("POST", path, body=body)
        return status, _parse_json_body(status, text)


def _parse_json_body(status, text):
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        raise ReplicationError(f"response body for status {status} was not valid JSON") from None


_SENSITIVE_PATH_FRAGMENTS = ("credentials",)


def _redact_path(path):
    """Credential-alias paths may embed operator-chosen identifiers; the alias segment itself is never printed."""
    if any(fragment in path for fragment in _SENSITIVE_PATH_FRAGMENTS):
        return re.sub(r"/credentials/[^/]+/[^/?]+", "/credentials/[REDACTED]/[REDACTED]", path)
    return path


# REST endpoint paths (Task 10 contract; GET/POST only, no DELETE/PUT/PATCH)

def _quote(segment):
    return urllib.parse.quote(str(segment), safe="")


def credential_path(domain, alias):
    return f"/services/v2/credentials/{_quote(domain)}/{_quote(alias)}"


def credential_valid_path(domain, alias):
    return f"{credential_path(domain, alias)}/valid"


def trandata_path(connection):
    return f"/services/v2/connections/{_quote(connection)}/trandata/table"


def checkpoint_table_path(connection):
    return f"/services/v2/connections/{_quote(connection)}/tables/checkpoint"


def extract_path(name):
    return f"/services/v2/extracts/{_quote(name)}"


def replicat_path(name):
    return f"/services/v2/replicats/{_quote(name)}"


def distribution_path(name):
    return f"/services/v2/sources/{_quote(name)}"


def receiver_paths_path():
    return "/services/v2/targets"


def receiver_path_detail_path(name):
    return f"/services/v2/targets/{_quote(name)}"


def commands_execute_path():
    return "/services/v2/commands/execute"


def connection_name(domain, alias):
    return f"{domain}.{alias}"


# Credential reconciliation (Task 11)

def ensure_credential(client, domain, alias, userid, password):
    """GET first; POST only on a definite 404; an existing alias is never replaced or password-compared."""
    status, _body = client.get(credential_path(domain, alias))
    if status == 404:
        request_body = {"alias": alias, "userid": userid, "password": password}
        post_status, _post_body = client.post(credential_path(domain, alias), request_body)
        if post_status not in (200, 201):
            raise ReplicationError(f"credential alias creation failed with status {post_status}")
    elif status != 200:
        raise ReplicationError(f"unexpected status {status} checking credential alias")

    valid_status, valid_body = client.get(credential_valid_path(domain, alias))
    if valid_status != 200:
        raise ReplicationError("credential alias failed validation -- operator action required")
    if isinstance(valid_body, dict) and valid_body.get("valid") is False:
        raise ReplicationError("credential alias is invalid -- operator action required")


# PostgreSQL source preparation (Task 12)

def ensure_trandata(client, connection, table):
    """No documented GET-check endpoint exists for TRANDATA state; POST is treated as idempotent per Oracle's own semantics."""
    status, _body = client.post(trandata_path(connection), {"table": table})
    if status not in (200, 201):
        raise ReplicationError(f"TRANDATA request failed with status {status}")


def _normalize_extract_actual(body):
    if not isinstance(body, dict):
        return {}
    response = body.get("response", body)
    return {
        "trail": response.get("trail") or response.get("beginTrail") or response.get("extTrail"),
    }


def ensure_extract(client, alias, domain, extract_plan):
    """GET first; create only on 404; an existing Extract is compared on trail identity only and never mutated."""
    status, body = client.get(extract_path(extract_plan["name"]))
    if status == 404:
        request_body = {
            "name": extract_plan["name"],
            "description": extract_plan.get("description", ""),
            "source": {"type": extract_plan["pluginType"]},
            "begin": extract_plan["begin"],
            "trail": extract_plan["trail"]["name"],
            "credentials": {"alias": alias, "domain": domain},
            "parameters": [{"table": t} for t in extract_plan["tables"]],
        }
        post_status, _post_body = client.post(extract_path(extract_plan["name"]), request_body)
        if post_status not in (200, 201):
            raise ReplicationError(f"Extract creation failed with status {post_status}")
        return "created"
    if status != 200:
        raise ReplicationError(f"unexpected status {status} checking Extract")
    actual = _normalize_extract_actual(body)
    if actual.get("trail") not in (extract_plan["trail"]["name"], None):
        raise DriftError("existing Extract trail does not match the desired configuration")
    return "existing"


# MSSQL target preparation (Task 13)

def ensure_checkpoint_table(client, connection, checkpoint_plan):
    if not checkpoint_plan.get("createIfMissing"):
        return
    status, _body = client.post(checkpoint_table_path(connection), {"table": checkpoint_plan["table"]})
    if status not in (200, 201):
        raise ReplicationError(f"checkpoint-table request failed with status {status}")


def _normalize_replicat_actual(body):
    if not isinstance(body, dict):
        return {}
    response = body.get("response", body)
    return {
        "trail": response.get("trail") or response.get("beginTrail"),
    }


def ensure_replicat(client, alias, domain, replicat_plan, checkpoint_plan):
    """GET first; create only on 404; nonintegrated, nonparallel, stopped on create; never coordinated/parallel/integrated/DDL."""
    status, body = client.get(replicat_path(replicat_plan["name"]))
    if status == 404:
        request_body = {
            "name": replicat_plan["name"],
            "description": replicat_plan.get("description", ""),
            "target": {"type": "nonintegrated", "parallel": False},
            "begin": replicat_plan["begin"],
            "trail": replicat_plan["sourceTrailName"],
            "credentials": {"alias": alias, "domain": domain},
            "checkpointTable": checkpoint_plan["table"],
            "parameters": [{"source": m["source"], "target": m["target"]} for m in replicat_plan["mappings"]],
        }
        post_status, _post_body = client.post(replicat_path(replicat_plan["name"]), request_body)
        if post_status not in (200, 201):
            raise ReplicationError(f"Replicat creation failed with status {post_status}")
        return "created"
    if status != 200:
        raise ReplicationError(f"unexpected status {status} checking Replicat")
    actual = _normalize_replicat_actual(body)
    if actual.get("trail") not in (replicat_plan["sourceTrailName"], None):
        raise DriftError("existing Replicat trail does not match the desired configuration")
    return "existing"


# Network credential and Distribution path (Task 14)

def _normalize_distribution_actual(body):
    if not isinstance(body, dict):
        return {}
    response = body.get("response", body)
    return {
        "targetHost": response.get("target") or response.get("host"),
        "sourceTrail": response.get("trail") or response.get("sourceTrail"),
    }


def ensure_distribution_path(client, distribution_plan, target_host):
    """GET first; create only on 404; stopped on create; an existing path that differs from desired state fails closed."""
    status, body = client.get(distribution_path(distribution_plan["pathName"]))
    if status == 404:
        request_body = {
            "name": distribution_plan["pathName"],
            "trail": distribution_plan["sourceTrailName"],
            "target": {
                "host": target_host,
                "port": distribution_plan["port"],
                "protocol": distribution_plan["protocol"],
                "trail": distribution_plan["targetTrailName"],
            },
        }
        post_status, _post_body = client.post(distribution_path(distribution_plan["pathName"]), request_body)
        if post_status not in (200, 201):
            raise ReplicationError(f"Distribution path creation failed with status {post_status}")
        return "created"
    if status != 200:
        raise ReplicationError(f"unexpected status {status} checking Distribution path")
    actual = _normalize_distribution_actual(body)
    if actual.get("sourceTrail") not in (distribution_plan["sourceTrailName"], None):
        raise DriftError("existing Distribution path source trail does not match the desired configuration")
    if actual.get("targetHost") not in (target_host, None):
        raise DriftError("existing Distribution path target host does not match the desired configuration")
    return "existing"


# Receiver validation (Task 15)

def verify_receiver_path(client, expected_path_name, expected_trail):
    status, body = client.get(receiver_paths_path())
    if status != 200:
        raise ReplicationError(f"unexpected status {status} listing Receiver paths")
    entries = []
    if isinstance(body, dict):
        entries = (body.get("response") or {}).get("items", []) if isinstance(body.get("response"), dict) else body.get("items", [])
    names = [e.get("name") for e in entries if isinstance(e, dict)]
    if names.count(expected_path_name) > 1:
        raise ReplicationError("duplicate target Receiver path detected")
    detail_status, detail_body = client.get(receiver_path_detail_path(expected_path_name))
    if detail_status != 200:
        raise ReplicationError(f"Receiver path {expected_path_name!r} is not visible on the target -- unexpected status {detail_status}")
    detail_response = detail_body.get("response", detail_body) if isinstance(detail_body, dict) else {}
    actual_trail = detail_response.get("trail") if isinstance(detail_response, dict) else None
    if actual_trail not in (expected_trail, None):
        raise DriftError("Receiver path trail does not match the expected target trail")


# Process start semantics (Task 16)

_ACCEPTED_EXISTING_STATUSES = ("RUNNING",)
_OPERATOR_ACTION_STATUSES = ("STOPPED",)
_FAILING_STATUSES = ("ABENDED",)


_PATH_BUILDER_BY_KIND = {"replicat": replicat_path, "extract": extract_path, "source": distribution_path}


def _process_status(client, kind, name):
    status, body = client.get(_PATH_BUILDER_BY_KIND[kind](name))
    if status != 200:
        raise ReplicationError(f"unexpected status {status} checking process state")
    response = body.get("response", body) if isinstance(body, dict) else {}
    return str(response.get("status", "")).upper()


def start_process(client, kind, name):
    status, _body = client.post(commands_execute_path(), {"type": kind, "name": name, "command": "start"})
    if status not in (200, 201, 202):
        raise ReplicationError(f"start command for process failed with status {status}")


def ensure_process_running_state(client, kind, name, newly_created, start_on_create):
    """startOnCreate only ever starts an object created in this same reconciliation; never restarts/heals an existing one."""
    if newly_created:
        if start_on_create:
            start_process(client, kind, name)
        return
    current = _process_status(client, kind, name)
    if current in _ACCEPTED_EXISTING_STATUSES:
        return
    if current in _OPERATOR_ACTION_STATUSES:
        raise ReplicationError(f"process {name!r} is STOPPED -- operator action required")
    if current in _FAILING_STATUSES:
        raise ReplicationError(f"process {name!r} is ABENDED -- reconciliation fails closed")
    raise ReplicationError(f"process {name!r} is in an unknown state -- reconciliation fails closed")


# Top-level pipeline reconciliation (Task 6, 17)

def reconcile_pipeline(plan, source_client, target_client):
    """Create-only, fail-on-drift; configures the target before the source; starts target Replicat, then source Distribution path, then source Extract, in that order."""
    src, tgt = plan["source"], plan["target"]

    ensure_credential(target_client, tgt["databaseCredentialDomain"], tgt["databaseCredentialAlias"],
                      read_secret_file("/mnt/replication-secrets/target-db/userid"),
                      read_secret_file("/mnt/replication-secrets/target-db/password"))
    ensure_credential(source_client, src["databaseCredentialDomain"], src["databaseCredentialAlias"],
                      read_secret_file("/mnt/replication-secrets/source-db/userid"),
                      read_secret_file("/mnt/replication-secrets/source-db/password"))
    ensure_credential(source_client, plan["networkCredentialDomain"], plan["networkCredentialAlias"],
                      read_secret_file("/mnt/replication-secrets/target-admin/username"),
                      read_secret_file("/mnt/replication-secrets/target-admin/password"))

    target_connection = connection_name(tgt["databaseCredentialDomain"], tgt["databaseCredentialAlias"])
    source_connection = connection_name(src["databaseCredentialDomain"], src["databaseCredentialAlias"])

    ensure_checkpoint_table(target_client, target_connection, plan["checkpoint"])
    replicat_state = ensure_replicat(target_client, tgt["databaseCredentialAlias"], tgt["databaseCredentialDomain"],
                                     plan["replicat"], plan["checkpoint"])

    for table in plan["supplementalLogging"]["objects"]:
        ensure_trandata(source_client, source_connection, table)
    extract_state = ensure_extract(source_client, src["databaseCredentialAlias"], src["databaseCredentialDomain"], plan["extract"])

    distribution_state = ensure_distribution_path(source_client, plan["distribution"], tgt["runtimeHost"])

    ensure_process_running_state(target_client, "replicat", plan["replicat"]["name"],
                                 replicat_state == "created", plan["replicat"]["startOnCreate"])
    ensure_process_running_state(source_client, "source", plan["distribution"]["pathName"],
                                 distribution_state == "created", plan["distribution"]["startOnCreate"])
    ensure_process_running_state(source_client, "extract", plan["extract"]["name"],
                                 extract_state == "created", plan["extract"]["startOnCreate"])

    verify_receiver_path(target_client, plan["distribution"]["pathName"], plan["distribution"]["targetTrailName"])

    return {
        "pipelineId": plan["pipelineId"],
        "replicat": replicat_state,
        "extract": extract_state,
        "distribution": distribution_state,
    }


# Temporary Kubernetes manifests (Task 7, 8): Job, ConfigMap, SecretProviderClass

_NAME_SLUG_RE = re.compile(r"[^a-z0-9-]")


def plan_checksum(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()[:8]


def job_resource_name(pipeline_id, plan):
    slug = _NAME_SLUG_RE.sub("-", pipeline_id.lower())[:40].strip("-")
    return f"gg-repl-{slug}-{plan_checksum(plan)}"


def render_secret_provider_class(name, namespace, region, plan):
    src, tgt = plan["source"], plan["target"]
    objects = [
        {
            "objectName": src["adminSecret"], "objectType": "secretsmanager",
            "jmesPath": [
                {"path": "OGG_ADMIN", "objectAlias": "source-admin/username"},
                {"path": "OGG_ADMIN_PWD", "objectAlias": "source-admin/password"},
            ],
        },
        {
            "objectName": tgt["adminSecret"], "objectType": "secretsmanager",
            "jmesPath": [
                {"path": "OGG_ADMIN", "objectAlias": "target-admin/username"},
                {"path": "OGG_ADMIN_PWD", "objectAlias": "target-admin/password"},
            ],
        },
        {
            "objectName": src["databaseSecret"], "objectType": "secretsmanager",
            "jmesPath": [
                {"path": "OGG_DB_USERID", "objectAlias": "source-db/userid"},
                {"path": "OGG_DB_PASSWORD", "objectAlias": "source-db/password"},
            ],
        },
        {
            "objectName": tgt["databaseSecret"], "objectType": "secretsmanager",
            "jmesPath": [
                {"path": "OGG_DB_USERID", "objectAlias": "target-db/userid"},
                {"path": "OGG_DB_PASSWORD", "objectAlias": "target-db/password"},
            ],
        },
        {
            "objectName": plan["tlsSecret"], "objectType": "secretsmanager",
            "jmesPath": [{"path": '"ca-chain.pem"', "objectAlias": "tls/ca-chain.pem"}],
        },
    ]
    return {
        "apiVersion": "secrets-store.csi.x-k8s.io/v1",
        "kind": "SecretProviderClass",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "provider": "aws",
            "parameters": {
                "region": region,
                "objects": yaml.safe_dump(objects, sort_keys=False, default_flow_style=False),
            },
        },
    }


def render_config_map(name, namespace, plan, reconciler_source):
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": namespace},
        "data": {
            "goldengate-replication.py": reconciler_source,
            "plan.json": json.dumps(plan, indent=2, sort_keys=True),
        },
    }


def render_job(name, namespace, plan, configmap_name, spc_name, ttl_seconds=DEFAULT_JOB_TTL_SECONDS):
    source = plan["source"]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace, "labels": {"app.kubernetes.io/component": "goldengate-replication-reconciler"}},
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": ttl_seconds,
            "template": {
                "metadata": {"labels": {"app.kubernetes.io/component": "goldengate-replication-reconciler"}},
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": source["serviceAccount"],
                    "containers": [
                        {
                            "name": "reconciler",
                            "image": source["image"],
                            "command": ["python3", "/mnt/reconciler/goldengate-replication.py", "worker",
                                       "--plan", "/mnt/reconciler/plan.json",
                                       "--secrets-root", "/mnt/replication-secrets"],
                            "volumeMounts": [
                                {"name": "reconciler-script", "mountPath": "/mnt/reconciler", "readOnly": True},
                                {"name": "replication-secrets", "mountPath": "/mnt/replication-secrets", "readOnly": True},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "reconciler-script", "configMap": {"name": configmap_name}},
                        {
                            "name": "replication-secrets",
                            "csi": {
                                "driver": "secrets-store.csi.k8s.io", "readOnly": True,
                                "volumeAttributes": {"secretProviderClass": spc_name},
                            },
                        },
                    ],
                },
            },
        },
    }


def render_manifests(plan, namespace, region, reconciler_source):
    """Returns {kind: manifest_dict}; the caller decides whether to write these to disk or apply them."""
    name = job_resource_name(plan["pipelineId"], plan)
    return {
        "SecretProviderClass": render_secret_provider_class(name, namespace, region, plan),
        "ConfigMap": render_config_map(name, namespace, plan, reconciler_source),
        "Job": render_job(name, namespace, plan, name, name),
    }


# Read-only verification (no mutation; usable standalone or as a post-reconciliation diagnostic)

def verify_pipeline(plan, source_client, target_client):
    return {
        "pipelineId": plan["pipelineId"],
        "extract": _process_status(source_client, "extract", plan["extract"]["name"]),
        "distribution": _process_status(source_client, "source", plan["distribution"]["pathName"]),
        "replicat": _process_status(target_client, "replicat", plan["replicat"]["name"]),
    }


# CLI

def cmd_plan(args):
    gdm = _gdm()
    active, _inactive, invalid, problems = gdm._run_full_validation(args.environment)
    if invalid or problems:
        gdm._print_reasons(invalid)
        gdm._print_problems(problems)
        print("FAIL: refusing to build a replication plan while validation problems exist")
        return 1
    if args.pipeline_id not in gdm.replication_pipeline_ids(active):
        print(f"FAIL: {args.pipeline_id!r} is not an enabled replication pipeline")
        return 1
    source, target = gdm.find_replication_pipeline(active, args.pipeline_id)
    plan = gdm.build_replication_plan(source, target)
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


def cmd_render_job(args):
    gdm = _gdm()
    active, _inactive, invalid, problems = gdm._run_full_validation(args.environment)
    if invalid or problems:
        gdm._print_reasons(invalid)
        gdm._print_problems(problems)
        print("FAIL: refusing to render a reconciliation Job while validation problems exist")
        return 1
    if args.pipeline_id not in gdm.replication_pipeline_ids(active):
        print(f"FAIL: {args.pipeline_id!r} is not an enabled replication pipeline")
        return 1
    source, target = gdm.find_replication_pipeline(active, args.pipeline_id)
    plan = gdm.build_replication_plan(source, target)
    with open(os.path.abspath(__file__)) as f:
        reconciler_source = f.read()
    manifests = render_manifests(plan, args.namespace, args.region, reconciler_source)

    if args.output_dir:
        os.makedirs(args.output_dir, exist_ok=True)
        for kind, doc in manifests.items():
            path = os.path.join(args.output_dir, f"{kind.lower()}.yaml")
            with open(path, "w") as f:
                yaml.safe_dump(doc, f, sort_keys=False, default_flow_style=False)
            print(f"Rendered {kind}: {path}")
    else:
        for kind, doc in manifests.items():
            print(f"---# {kind}")
            print(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False))
    return 0


def _client_from_mounts(secrets_root, host, admin_dir, timeout):
    ca_file = os.path.join(secrets_root, "tls", "ca-chain.pem")
    username = read_secret_file(os.path.join(secrets_root, admin_dir, "username"))
    password = read_secret_file(os.path.join(secrets_root, admin_dir, "password"))
    return GGClient(host, username, password, ca_file, timeout=timeout)


def _load_plan(path):
    with open(path) as f:
        return json.load(f)


def cmd_worker(args):
    plan = _load_plan(args.plan)
    source_client = _client_from_mounts(args.secrets_root, plan["source"]["runtimeHost"], "source-admin", args.timeout)
    target_client = _client_from_mounts(args.secrets_root, plan["target"]["runtimeHost"], "target-admin", args.timeout)
    try:
        result = reconcile_pipeline(plan, source_client, target_client)
    except ReplicationError as exc:
        print(json.dumps({"pipelineId": plan.get("pipelineId"), "status": "FAILED", "reason": exc.reason}, sort_keys=True))
        return 1
    print(json.dumps({"pipelineId": plan.get("pipelineId"), "status": "OK", "result": result}, sort_keys=True))
    return 0


def cmd_verify(args):
    plan = _load_plan(args.plan)
    source_client = _client_from_mounts(args.secrets_root, plan["source"]["runtimeHost"], "source-admin", args.timeout)
    target_client = _client_from_mounts(args.secrets_root, plan["target"]["runtimeHost"], "target-admin", args.timeout)
    try:
        result = verify_pipeline(plan, source_client, target_client)
    except ReplicationError as exc:
        print(json.dumps({"pipelineId": plan.get("pipelineId"), "status": "FAILED", "reason": exc.reason}, sort_keys=True))
        return 1
    print(json.dumps({"pipelineId": plan.get("pipelineId"), "status": "OK", "result": result}, sort_keys=True))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--environment", default="dev")
    plan_parser.add_argument("pipeline_id")
    plan_parser.set_defaults(func=cmd_plan)

    render_parser = sub.add_parser("render-job")
    render_parser.add_argument("--environment", default="dev")
    render_parser.add_argument("--namespace", default="goldengate-dev")
    render_parser.add_argument("--region", required=True)
    render_parser.add_argument("--output-dir", default=None)
    render_parser.add_argument("pipeline_id")
    render_parser.set_defaults(func=cmd_render_job)

    worker_parser = sub.add_parser("worker")
    worker_parser.add_argument("--plan", required=True)
    worker_parser.add_argument("--secrets-root", default="/mnt/replication-secrets")
    worker_parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    worker_parser.set_defaults(func=cmd_worker)

    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--plan", required=True)
    verify_parser.add_argument("--secrets-root", default="/mnt/replication-secrets")
    verify_parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    verify_parser.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
