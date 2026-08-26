#!/usr/bin/env python3
"""automation/goldengate-replication.py: PostgreSQL->MSSQL GoldenGate replication reconciler; create-only, fail-on-drift, never restarts/heals/deletes. Consumes automation/goldengate-deployment-model.py for all deployment parsing (host-side only); never a second YAML parser. worker/verify modes use the Python standard library only -- PyYAML and goldengate-deployment-model.py are never imported inside the reconciliation Job. Oracle REST request/response shapes below are best-effort from the documented GoldenGate Microservices REST API and have not been verified against a live 23.26.2.0.1 instance in this offline session -- see the Phase 6D1 completion report. Runs under the live source runtime image's Python 3.6.8 -- no deferred-annotation __future__ import, no argparse subparsers required= kwarg, no other 3.7+ syntax/API."""

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

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_DEPLOYMENT_MODEL_PATH = os.path.join(os.path.dirname(__file__), "goldengate-deployment-model.py")

DEFAULT_TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 1_000_000
READINESS_RETRY_COUNT = 3
DEFAULT_JOB_TTL_SECONDS = 3600
NETWORK_CREDENTIAL_DOMAIN = "Network"

_gdm_module = None


def _gdm():
    """Lazy import: worker mode runs inside the reconciliation Job, which never has goldengate-deployment-model.py or PyYAML available."""
    global _gdm_module
    if _gdm_module is None:
        spec = importlib.util.spec_from_file_location("goldengate_deployment_model", _DEPLOYMENT_MODEL_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _gdm_module = module
    return _gdm_module


def _yaml():
    """Lazy import: only host-side render-job functionality needs PyYAML; worker/verify never call this."""
    import yaml
    return yaml


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

    def patch(self, path, body):
        """Reserved exclusively for transitioning a newly-created Distribution path from stopped to running (Task 13); never used for credentials, Extract/Replicat configuration, or drift repair."""
        status, text = self._request("PATCH", path, body=body)
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


# REST endpoint paths (no DELETE, no credential/configuration PUT/PATCH; PATCH is reserved solely for Distribution path status)

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


# Credential reconciliation (Task 3, 4): the alias is a path parameter only, never a body field.

def _ensure_credential_exists(client, domain, alias, userid, password):
    """GET first; POST {"userid","password"} only on a definite 404; an existing alias is never replaced or password-compared."""
    status, _body = client.get(credential_path(domain, alias))
    if status == 404:
        request_body = {"userid": userid, "password": password}
        post_status, _post_body = client.post(credential_path(domain, alias), request_body)
        if post_status not in (200, 201):
            raise ReplicationError(f"credential alias creation failed with status {post_status}")
    elif status != 200:
        raise ReplicationError(f"unexpected status {status} checking credential alias")


def ensure_database_credential(client, domain, alias, userid, password):
    """Database credential: existence via GET/POST, then required GET .../valid; an invalid alias fails closed."""
    _ensure_credential_exists(client, domain, alias, userid, password)
    valid_status, valid_body = client.get(credential_valid_path(domain, alias))
    if valid_status != 200:
        raise ReplicationError("database credential alias failed validation -- operator action required")
    if isinstance(valid_body, dict) and valid_body.get("valid") is False:
        raise ReplicationError("database credential alias is invalid -- operator action required")


def ensure_network_credential(client, domain, alias, username, password):
    """Network credential (Distribution Service auth, not a database login): existence only, never validated via the database /valid endpoint -- the Distribution path connection itself is the functional validation."""
    _ensure_credential_exists(client, domain, alias, username, password)


# PostgreSQL source preparation (Task 5)

_TRANDATA_ENABLED_FIELDS = ("loggingEnabled", "enabled", "tranDataEnabled")


def _trandata_enabled(client, connection, table):
    status, body = client.post(trandata_path(connection), {"operation": "info", "tableName": table})
    if status != 200:
        raise ReplicationError(f"TRANDATA info request failed with status {status}")
    response = body.get("response", body) if isinstance(body, dict) else None
    if not isinstance(response, dict):
        raise ReplicationError("TRANDATA info response could not be normalized -- live schema confirmation required")
    for key in _TRANDATA_ENABLED_FIELDS:
        if key in response and isinstance(response[key], bool):
            return response[key]
    raise ReplicationError("TRANDATA info response did not contain a recognized logging-state field -- live schema confirmation required")


def ensure_trandata(client, connection, table):
    """Inspect first; add only when info definitively proves logging is not enabled; never issue operation=delete."""
    if _trandata_enabled(client, connection, table):
        return
    status, _body = client.post(trandata_path(connection), {"operation": "add", "tableName": table})
    if status not in (200, 201):
        raise ReplicationError(f"TRANDATA add request failed with status {status}")


_EXTRACT_SOURCE_TYPE = "tranlogs"
_EXTRACT_REQUIRED_RESPONSE_FIELDS = ("source", "pluginType", "credentials", "targets", "config")


def _generate_extract_config(extract_plan, alias, domain):
    """Internally generated GoldenGate parameter text; the operator never supplies raw config."""
    lines = [f"EXTRACT {extract_plan['name']}", f"USERIDALIAS {alias} DOMAIN {domain}", f"EXTTRAIL {extract_plan['trail']['name']}"]
    lines.extend(f"TABLE {table};" for table in extract_plan["tables"])
    return "\n".join(lines)


def _extract_request_body(alias, domain, extract_plan):
    return {
        "description": extract_plan.get("description", ""),
        "source": _EXTRACT_SOURCE_TYPE,
        "pluginType": extract_plan["pluginType"],
        "begin": extract_plan["begin"],
        "credentials": {"alias": alias, "domain": domain},
        "config": _generate_extract_config(extract_plan, alias, domain),
        "targets": [{"name": extract_plan["trail"]["name"], "type": "trail", "fileSize": extract_plan["trail"]["sizeMB"]}],
        "status": "stopped",
    }


def _normalize_extract_actual(body):
    """Fails closed (raises) on any missing required field; never treats an absent field as equivalent to desired."""
    if not isinstance(body, dict):
        raise ReplicationError("Extract retrieve response was not a JSON object -- INVALID_RESPONSE, fails closed")
    response = body.get("response", body)
    if not isinstance(response, dict):
        raise ReplicationError("Extract retrieve response could not be normalized -- INVALID_RESPONSE, fails closed")
    missing = [f for f in _EXTRACT_REQUIRED_RESPONSE_FIELDS if f not in response]
    if missing:
        raise ReplicationError(f"Extract retrieve response is missing required field(s) ({', '.join(missing)}) -- UNKNOWN, fails closed")
    credentials = response.get("credentials") or {}
    targets = response.get("targets") or []
    return {
        "source": response.get("source"),
        "pluginType": response.get("pluginType"),
        "credentialAlias": credentials.get("alias"),
        "credentialDomain": credentials.get("domain"),
        "targets": {t.get("name"): t for t in targets if isinstance(t, dict)},
        "config": response.get("config"),
    }


def ensure_extract(client, alias, domain, extract_plan):
    """GET first; create only on 404; an existing Extract must prove equivalence on every material field or fails closed."""
    status, body = client.get(extract_path(extract_plan["name"]))
    if status == 404:
        post_status, _post_body = client.post(extract_path(extract_plan["name"]), _extract_request_body(alias, domain, extract_plan))
        if post_status not in (200, 201):
            raise ReplicationError(f"Extract creation failed with status {post_status}")
        return "created"
    if status != 200:
        raise ReplicationError(f"unexpected status {status} checking Extract")

    actual = _normalize_extract_actual(body)
    drift = []
    if actual["source"] != _EXTRACT_SOURCE_TYPE:
        drift.append("source")
    if actual["pluginType"] != extract_plan["pluginType"]:
        drift.append("pluginType")
    if actual["credentialAlias"] != alias or actual["credentialDomain"] != domain:
        drift.append("credentials")
    target_entry = actual["targets"].get(extract_plan["trail"]["name"])
    if target_entry is None:
        drift.append("targets")
    elif "fileSize" in target_entry and target_entry["fileSize"] != extract_plan["trail"]["sizeMB"]:
        drift.append("targets.fileSize")
    if actual["config"] != _generate_extract_config(extract_plan, alias, domain):
        drift.append("config")
    if drift:
        raise DriftError(f"existing Extract differs from the desired configuration ({', '.join(drift)}) -- operator review required")
    return "existing"


# MSSQL target preparation (Task 13)

_CHECKPOINT_EXISTS_FIELDS = ("exists", "tableExists", "present")


def _checkpoint_table_exists(client, connection, table_name):
    status, body = client.post(checkpoint_table_path(connection), {"operation": "info", "name": table_name})
    if status != 200:
        raise ReplicationError(f"checkpoint-table info request failed with status {status}")
    response = body.get("response", body) if isinstance(body, dict) else None
    if not isinstance(response, dict):
        raise ReplicationError("checkpoint-table info response could not be normalized -- live schema confirmation required")
    for key in _CHECKPOINT_EXISTS_FIELDS:
        if key in response and isinstance(response[key], bool):
            return response[key]
    raise ReplicationError("checkpoint-table info response did not contain a recognized existence field -- live schema confirmation required")


def ensure_checkpoint_table(client, connection, checkpoint_plan):
    """Inspect first; accept and never modify an existing table; add only when definitively absent and createIfMissing=true."""
    if _checkpoint_table_exists(client, connection, checkpoint_plan["table"]):
        return
    if not checkpoint_plan.get("createIfMissing"):
        raise ReplicationError("checkpoint table is absent and createIfMissing=false -- operator action required")
    status, _body = client.post(checkpoint_table_path(connection), {"operation": "add", "name": checkpoint_plan["table"]})
    if status not in (200, 201):
        raise ReplicationError(f"checkpoint-table add request failed with status {status}")


_REPLICAT_MODE_TYPE = "nonintegrated"
_REPLICAT_REQUIRED_RESPONSE_FIELDS = ("source", "credentials", "checkpoint", "mode", "config")


def _generate_replicat_config(replicat_plan, alias, domain):
    """Internally generated GoldenGate parameter text; the operator never supplies raw config."""
    lines = [f"REPLICAT {replicat_plan['name']}", f"USERIDALIAS {alias} DOMAIN {domain}"]
    lines.extend(f"MAP {m['source']}, TARGET {m['target']};" for m in replicat_plan["mappings"])
    return "\n".join(lines)


def _replicat_request_body(alias, domain, replicat_plan, checkpoint_plan):
    return {
        "description": replicat_plan.get("description", ""),
        "begin": replicat_plan["begin"],
        "source": {"name": replicat_plan["sourceTrailName"], "type": "trail"},
        "credentials": {"alias": alias, "domain": domain},
        "checkpoint": {"table": checkpoint_plan["table"]},
        "mode": {"type": _REPLICAT_MODE_TYPE, "parallel": False},
        "config": _generate_replicat_config(replicat_plan, alias, domain),
        "status": "stopped",
    }


def _normalize_replicat_actual(body):
    """Fails closed (raises) on any missing required field; never treats an absent field as equivalent to desired."""
    if not isinstance(body, dict):
        raise ReplicationError("Replicat retrieve response was not a JSON object -- INVALID_RESPONSE, fails closed")
    response = body.get("response", body)
    if not isinstance(response, dict):
        raise ReplicationError("Replicat retrieve response could not be normalized -- INVALID_RESPONSE, fails closed")
    missing = [f for f in _REPLICAT_REQUIRED_RESPONSE_FIELDS if f not in response]
    if missing:
        raise ReplicationError(f"Replicat retrieve response is missing required field(s) ({', '.join(missing)}) -- UNKNOWN, fails closed")
    credentials = response.get("credentials") or {}
    source = response.get("source") or {}
    checkpoint = response.get("checkpoint") or {}
    mode = response.get("mode") or {}
    return {
        "sourceTrail": source.get("name"),
        "credentialAlias": credentials.get("alias"),
        "credentialDomain": credentials.get("domain"),
        "checkpointTable": checkpoint.get("table"),
        "modeType": mode.get("type"),
        "modeParallel": mode.get("parallel"),
        "config": response.get("config"),
    }


def ensure_replicat(client, alias, domain, replicat_plan, checkpoint_plan):
    """GET first; create only on 404; nonintegrated, nonparallel, stopped on create; an existing Replicat must prove equivalence or fails closed."""
    status, body = client.get(replicat_path(replicat_plan["name"]))
    if status == 404:
        post_status, _post_body = client.post(replicat_path(replicat_plan["name"]), _replicat_request_body(alias, domain, replicat_plan, checkpoint_plan))
        if post_status not in (200, 201):
            raise ReplicationError(f"Replicat creation failed with status {post_status}")
        return "created"
    if status != 200:
        raise ReplicationError(f"unexpected status {status} checking Replicat")

    actual = _normalize_replicat_actual(body)
    drift = []
    if actual["sourceTrail"] != replicat_plan["sourceTrailName"]:
        drift.append("source")
    if actual["credentialAlias"] != alias or actual["credentialDomain"] != domain:
        drift.append("credentials")
    if actual["checkpointTable"] != checkpoint_plan["table"]:
        drift.append("checkpoint")
    if actual["modeType"] != _REPLICAT_MODE_TYPE or actual["modeParallel"] is not False:
        drift.append("mode")
    if actual["config"] != _generate_replicat_config(replicat_plan, alias, domain):
        drift.append("config")
    if drift:
        raise DriftError(f"existing Replicat differs from the desired configuration ({', '.join(drift)}) -- operator review required")
    return "existing"


# Distribution path (Task 12, 14): source-initiated ogg:distPath, authenticated via the derived Network credential.

_DISTRIBUTION_SOURCE_URI_SCHEME = "localtrail"
_DISTRIBUTION_REQUIRED_RESPONSE_FIELDS = ("targetInitiated", "source", "target")


def _distribution_source_uri(source_trail_name):
    return f"{_DISTRIBUTION_SOURCE_URI_SCHEME}:{source_trail_name}"


def _distribution_target_uri(target_host, target_trail_name, protocol, port):
    return f"{protocol}://{target_host}:{port}/services/v2/targets?trail={target_trail_name}"


def _distribution_request_body(distribution_plan, target_host, network_alias, network_domain):
    return {
        "targetInitiated": False,
        "status": "stopped",
        "source": {"uri": _distribution_source_uri(distribution_plan["sourceTrailName"])},
        "target": {
            "uri": _distribution_target_uri(target_host, distribution_plan["targetTrailName"], distribution_plan["protocol"], distribution_plan["port"]),
            "authenticationMethod": {"alias": network_alias, "domain": network_domain},
        },
    }


def _normalize_distribution_actual(body):
    """Fails closed (raises) on any missing required field; never treats an absent field as equivalent to desired."""
    if not isinstance(body, dict):
        raise ReplicationError("Distribution path retrieve response was not a JSON object -- INVALID_RESPONSE, fails closed")
    response = body.get("response", body)
    if not isinstance(response, dict):
        raise ReplicationError("Distribution path retrieve response could not be normalized -- INVALID_RESPONSE, fails closed")
    missing = [f for f in _DISTRIBUTION_REQUIRED_RESPONSE_FIELDS if f not in response]
    if missing:
        raise ReplicationError(f"Distribution path retrieve response is missing required field(s) ({', '.join(missing)}) -- UNKNOWN, fails closed")
    source = response.get("source") or {}
    target = response.get("target") or {}
    auth = target.get("authenticationMethod") or {}
    return {
        "targetInitiated": response.get("targetInitiated"),
        "sourceUri": source.get("uri"),
        "targetUri": target.get("uri"),
        "authAlias": auth.get("alias"),
        "authDomain": auth.get("domain"),
    }


def ensure_distribution_path(client, distribution_plan, target_host, network_alias, network_domain):
    """GET first; create only on 404; stopped on create; an existing path must prove equivalence on every material field or fails closed."""
    status, body = client.get(distribution_path(distribution_plan["pathName"]))
    if status == 404:
        request_body = _distribution_request_body(distribution_plan, target_host, network_alias, network_domain)
        post_status, _post_body = client.post(distribution_path(distribution_plan["pathName"]), request_body)
        if post_status not in (200, 201):
            raise ReplicationError(f"Distribution path creation failed with status {post_status}")
        return "created"
    if status != 200:
        raise ReplicationError(f"unexpected status {status} checking Distribution path")

    actual = _normalize_distribution_actual(body)
    drift = []
    if actual["targetInitiated"] is not False:
        drift.append("targetInitiated")
    if actual["sourceUri"] != _distribution_source_uri(distribution_plan["sourceTrailName"]):
        drift.append("source")
    if actual["targetUri"] != _distribution_target_uri(target_host, distribution_plan["targetTrailName"], distribution_plan["protocol"], distribution_plan["port"]):
        drift.append("target")
    if actual["authAlias"] != network_alias or actual["authDomain"] != network_domain:
        drift.append("authenticationMethod")
    if drift:
        raise DriftError(f"existing Distribution path differs from the desired configuration ({', '.join(drift)}) -- operator review required")
    return "existing"


def start_distribution_path(client, path_name):
    """The only PATCH ever issued: transitions a newly-created Distribution path from stopped to running; never used for drift repair."""
    status, _body = client.patch(distribution_path(path_name), {"status": "running"})
    if status not in (200, 201, 202):
        raise ReplicationError(f"Distribution path status PATCH failed with status {status}")


# Receiver validation (Task 15): never assumes a source-initiated Distribution path creates a persistently-named target Receiver path.

def _normalize_receiver_entries(body):
    if not isinstance(body, dict):
        raise ReplicationError("Receiver target collection response was not a JSON object -- live schema confirmation required")
    response = body.get("response", body)
    items = response.get("items") if isinstance(response, dict) else None
    if not isinstance(items, list):
        raise ReplicationError("Receiver target collection response did not contain an items list -- live schema confirmation required")
    return [
        {"name": item.get("name"), "trail": item.get("trail") or item.get("targetTrail")}
        for item in items if isinstance(item, dict)
    ]


def verify_receiver_path(client, expected_target_trail):
    """Matches by expected target trail only, never by an assumed same-name path (Task 15 VDR gate)."""
    status, body = client.get(receiver_paths_path())
    if status != 200:
        raise ReplicationError(f"unexpected status {status} listing Receiver paths")
    entries = _normalize_receiver_entries(body)
    matches = [e for e in entries if e.get("trail") == expected_target_trail]
    if len(matches) > 1:
        raise ReplicationError("multiple Receiver entries reference the expected target trail -- operator action required")
    if not matches:
        raise ReplicationError("no Receiver entry references the expected target trail -- live VDR schema confirmation required before this pipeline is enabled")


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
    """Extract/Replicat only; Distribution paths are started exclusively via start_distribution_path (PATCH)."""
    status, _body = client.post(commands_execute_path(), {"name": "start", "processName": name, "processType": kind})
    if status not in (200, 201, 202):
        raise ReplicationError(f"start command for process failed with status {status}")


def ensure_process_running_state(client, kind, name, newly_created, start_on_create):
    """startOnCreate only ever starts an object created in this same reconciliation; never restarts/heals an existing one."""
    if newly_created:
        if start_on_create:
            if kind == "source":
                start_distribution_path(client, name)
            else:
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

def reconcile_pipeline(plan, source_client, target_client, secrets_root="/mnt/replication-secrets"):
    """Create-only, fail-on-drift; configures the target before the source; starts target Replicat, then source Distribution path, then source Extract, in that order."""
    src, tgt = plan["source"], plan["target"]

    ensure_database_credential(target_client, tgt["databaseCredentialDomain"], tgt["databaseCredentialAlias"],
                               read_secret_file(os.path.join(secrets_root, "target-db-userid")),
                               read_secret_file(os.path.join(secrets_root, "target-db-password")))
    ensure_database_credential(source_client, src["databaseCredentialDomain"], src["databaseCredentialAlias"],
                               read_secret_file(os.path.join(secrets_root, "source-db-userid")),
                               read_secret_file(os.path.join(secrets_root, "source-db-password")))
    ensure_network_credential(source_client, plan["networkCredentialDomain"], plan["networkCredentialAlias"],
                              read_secret_file(os.path.join(secrets_root, "target-admin-username")),
                              read_secret_file(os.path.join(secrets_root, "target-admin-password")))

    target_connection = connection_name(tgt["databaseCredentialDomain"], tgt["databaseCredentialAlias"])
    source_connection = connection_name(src["databaseCredentialDomain"], src["databaseCredentialAlias"])

    ensure_checkpoint_table(target_client, target_connection, plan["checkpoint"])
    replicat_state = ensure_replicat(target_client, tgt["databaseCredentialAlias"], tgt["databaseCredentialDomain"],
                                     plan["replicat"], plan["checkpoint"])

    for table in plan["supplementalLogging"]["objects"]:
        ensure_trandata(source_client, source_connection, table)
    extract_state = ensure_extract(source_client, src["databaseCredentialAlias"], src["databaseCredentialDomain"], plan["extract"])

    distribution_state = ensure_distribution_path(source_client, plan["distribution"], tgt["runtimeHost"],
                                                  plan["networkCredentialAlias"], plan["networkCredentialDomain"])

    ensure_process_running_state(target_client, "replicat", plan["replicat"]["name"],
                                 replicat_state == "created", plan["replicat"]["startOnCreate"])
    ensure_process_running_state(source_client, "source", plan["distribution"]["pathName"],
                                 distribution_state == "created", plan["distribution"]["startOnCreate"])
    ensure_process_running_state(source_client, "extract", plan["extract"]["name"],
                                 extract_state == "created", plan["extract"]["startOnCreate"])

    verify_receiver_path(target_client, plan["distribution"]["targetTrailName"])

    return {
        "pipelineId": plan["pipelineId"],
        "replicat": replicat_state,
        "extract": extract_state,
        "distribution": distribution_state,
    }


# Temporary Kubernetes manifests (Task 7, 8, 17): Job, ConfigMap, SecretProviderClass

_NAME_SLUG_RE = re.compile(r"[^a-z0-9-]")
_EXECUTION_ID_RE = re.compile(r"[^a-z0-9-]")
_MAX_EXECUTION_ID_LENGTH = 20
DETERMINISTIC_DRY_RUN_EXECUTION_ID = "dry-run"


def plan_checksum(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()[:8]


def sanitize_execution_id(raw):
    """Bounded, DNS-1123-safe execution token; callers must supply a real run identifier or the deterministic dry-run token."""
    slug = _EXECUTION_ID_RE.sub("-", str(raw).lower()).strip("-")
    if not slug:
        raise ReplicationError("execution-id sanitizes to empty -- refusing to render an execution-scoped manifest")
    return slug[:_MAX_EXECUTION_ID_LENGTH]


def desired_state_name(pipeline_id, plan):
    """Desired-state identity only (pipeline + plan checksum); stable across reruns of the same plan, never used alone as a Job name."""
    slug = _NAME_SLUG_RE.sub("-", pipeline_id.lower())[:40].strip("-")
    return f"gg-repl-{slug}-{plan_checksum(plan)}"


def job_resource_name(pipeline_id, plan, execution_id):
    """Execution identity: desired-state name plus a rerun-safe execution suffix; a retained failed Job never collides with the next rerun."""
    return f"{desired_state_name(pipeline_id, plan)}-{sanitize_execution_id(execution_id)}"


def render_secret_provider_class(name, namespace, region, plan):
    yaml = _yaml()
    src, tgt = plan["source"], plan["target"]
    objects = [
        {
            "objectName": src["adminSecret"], "objectType": "secretsmanager",
            "jmesPath": [
                {"path": "OGG_ADMIN", "objectAlias": "source-admin-username"},
                {"path": "OGG_ADMIN_PWD", "objectAlias": "source-admin-password"},
            ],
        },
        {
            "objectName": tgt["adminSecret"], "objectType": "secretsmanager",
            "jmesPath": [
                {"path": "OGG_ADMIN", "objectAlias": "target-admin-username"},
                {"path": "OGG_ADMIN_PWD", "objectAlias": "target-admin-password"},
            ],
        },
        {
            "objectName": src["databaseSecret"], "objectType": "secretsmanager",
            "jmesPath": [
                {"path": "OGG_DB_USERID", "objectAlias": "source-db-userid"},
                {"path": "OGG_DB_PASSWORD", "objectAlias": "source-db-password"},
            ],
        },
        {
            "objectName": tgt["databaseSecret"], "objectType": "secretsmanager",
            "jmesPath": [
                {"path": "OGG_DB_USERID", "objectAlias": "target-db-userid"},
                {"path": "OGG_DB_PASSWORD", "objectAlias": "target-db-password"},
            ],
        },
        {
            "objectName": plan["tlsSecret"], "objectType": "secretsmanager",
            "jmesPath": [{"path": '"ca-chain.pem"', "objectAlias": "tls-ca-chain.pem"}],
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


def render_job(name, namespace, plan, configmap_name, spc_name, checksum, ttl_seconds=DEFAULT_JOB_TTL_SECONDS):
    source = plan["source"]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name, "namespace": namespace,
            "labels": {"app.kubernetes.io/component": "goldengate-replication-reconciler"},
            "annotations": {"goldengate.adcb/plan-checksum": checksum},
        },
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


def render_manifests(plan, namespace, region, reconciler_source, execution_id):
    """Returns {kind: manifest_dict}; the caller decides whether to write these to disk or apply them."""
    name = job_resource_name(plan["pipelineId"], plan, execution_id)
    checksum = plan_checksum(plan)
    return {
        "SecretProviderClass": render_secret_provider_class(name, namespace, region, plan),
        "ConfigMap": render_config_map(name, namespace, plan, reconciler_source),
        "Job": render_job(name, namespace, plan, name, name, checksum),
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
    yaml = _yaml()
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
    manifests = render_manifests(plan, args.namespace, args.region, reconciler_source, args.execution_id)

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


def _client_from_mounts(secrets_root, host, admin_prefix, timeout):
    ca_file = os.path.join(secrets_root, "tls-ca-chain.pem")
    username = read_secret_file(os.path.join(secrets_root, f"{admin_prefix}-username"))
    password = read_secret_file(os.path.join(secrets_root, f"{admin_prefix}-password"))
    return GGClient(host, username, password, ca_file, timeout=timeout)


def _load_plan(path):
    with open(path) as f:
        return json.load(f)


def cmd_worker(args):
    plan = _load_plan(args.plan)
    source_client = _client_from_mounts(args.secrets_root, plan["source"]["runtimeHost"], "source-admin", args.timeout)
    target_client = _client_from_mounts(args.secrets_root, plan["target"]["runtimeHost"], "target-admin", args.timeout)
    try:
        result = reconcile_pipeline(plan, source_client, target_client, secrets_root=args.secrets_root)
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
    sub = parser.add_subparsers(dest="command")

    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--environment", default="dev")
    plan_parser.add_argument("pipeline_id")
    plan_parser.set_defaults(func=cmd_plan)

    render_parser = sub.add_parser("render-job")
    render_parser.add_argument("--environment", default="dev")
    render_parser.add_argument("--namespace", default="goldengate-dev")
    render_parser.add_argument("--region", required=True)
    render_parser.add_argument("--output-dir", default=None)
    render_parser.add_argument("--execution-id", required=True)
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
    if not hasattr(args, "func"):
        parser.error("a command is required")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
