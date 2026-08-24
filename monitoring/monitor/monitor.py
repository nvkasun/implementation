"""monitor.py: runs the passive collector and the read-only monitoring portal in one process."""
import json
import logging
import math
import os
import signal
import sys
import threading
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

import collector
import config as cfgmod
import ui

RECORD_TYPE_CONFIG = "CONFIG"
RECORD_TYPE_LEASE = "LEASE"
RECORD_TYPE_DEPLOYMENT_STATE = "STATE#_deployment"
STATE_PREFIX = "STATE#"

CONNECT_TIMEOUT_SECONDS = 3
READ_TIMEOUT_SECONDS = 5
MAX_RETRY_ATTEMPTS = 2

CANONICAL_RAW_STATUSES = ("UP", "STARTING", "DEPLOYMENT_DOWN")
EFFECTIVE_STATUSES = ("UP", "STARTING", "DOWN", "STALE", "MISSING", "UNKNOWN")
_CANONICAL_STATUS_MAP = {"UP": "UP", "STARTING": "STARTING", "DEPLOYMENT_DOWN": "DOWN"}

PROCESS_STATUSES = ("RUNNING", "STOPPED", "ABENDED", "UNKNOWN")

FUTURE_TIMESTAMP_TOLERANCE_SECONDS = 300

CLIENT_SAFE_DYNAMODB_ERROR_MESSAGE = "Monitoring data is temporarily unavailable."

# errorMsg is raw GoldenGate REST text; only this fixed statusCode+message is ever exposed to a client.
_PROCESS_STATUS_CODE_MESSAGES = {
    "NONE": "No error.",
    "POLL_FAILED": "The last poll of this process reported an error.",
    "AUTH_FAILED": "Authentication to the GoldenGate Admin REST API failed.",
    "TLS_FAILED": "A TLS/certificate error occurred while contacting the GoldenGate Admin REST API.",
    "ENDPOINT_UNAVAILABLE": "The GoldenGate Admin REST API was unreachable.",
    "STALE": "Monitoring data for this process is stale.",
    "PROCESS_ABENDED": "This process has abended.",
    "UNKNOWN": "An unspecified error occurred.",
}

logger = logging.getLogger("goldengate.monitor")
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(_handler)
logger.propagate = False


class DynamoDbReadError(Exception):
    """Raised when a GetItem/Query call fails while building the payload."""


def log_event(level, event, **fields):
    record = {"timestamp": time.time(), "level": level, "event": event}
    record.update({k: v for k, v in fields.items() if v is not None})
    line = json.dumps(record, default=str)
    if level == "ERROR":
        logger.error(line)
    else:
        logger.info(line)


def sanitize_error(exc: BaseException, max_len: int = 200) -> str:
    message = f"{type(exc).__name__}: {exc}".replace("\n", " ").replace("\r", " ")
    return message[:max_len]


def _boto_config() -> BotoConfig:
    return BotoConfig(
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        read_timeout=READ_TIMEOUT_SECONDS,
        retries={"max_attempts": MAX_RETRY_ATTEMPTS, "mode": "standard"},
    )


def create_dynamodb_table_factory(config: cfgmod.MonitorConfig):
    """Zero-argument callable; each request gets its own Table object, never a shared one."""
    def _factory():
        session = boto3.session.Session()
        resource = session.resource("dynamodb", region_name=config.aws_region, config=_boto_config())
        return resource.Table(config.dynamodb_table)
    return _factory


def check_dynamodb_ready(table):
    table.meta.client.describe_table(TableName=table.name)


def get_config_item(table, pipeline):
    resp = table.get_item(Key={"pipeline": pipeline, "recordType": RECORD_TYPE_CONFIG})
    return resp.get("Item")


def get_lease_item(table, pipeline):
    resp = table.get_item(Key={"pipeline": pipeline, "recordType": RECORD_TYPE_LEASE})
    return resp.get("Item")


def get_deployment_state_item(table, pipeline):
    resp = table.get_item(Key={"pipeline": pipeline, "recordType": RECORD_TYPE_DEPLOYMENT_STATE})
    return resp.get("Item")


def query_process_state_items(table, pipeline):
    resp = table.query(
        KeyConditionExpression="pipeline = :p AND begins_with(recordType, :prefix)",
        ExpressionAttributeValues={":p": pipeline, ":prefix": STATE_PREFIX},
    )
    return [it for it in resp.get("Items", []) if it.get("recordType") != RECORD_TYPE_DEPLOYMENT_STATE]


def decimal_to_jsonsafe(value):
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    return value


def _parse_epoch(raw):
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, Decimal) and not raw.is_finite():
        return None
    try:
        value = decimal_to_jsonsafe(raw)
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _freshness(recorded_at, now):
    """Returns (age_seconds, plausible)."""
    if recorded_at is None:
        return None, False
    age = now - recorded_at
    if age < 0:
        if age < -FUTURE_TIMESTAMP_TOLERANCE_SECONDS:
            return None, False
        age = 0
    return int(age), True


def normalize_process_status(raw):
    raw = str(raw or "UNKNOWN").upper()
    return raw if raw in PROCESS_STATUSES else "UNKNOWN"


def _classify_process_status_code(status, error_msg, stale):
    if stale:
        return "STALE"
    if status == "ABENDED":
        return "PROCESS_ABENDED"
    if not error_msg:
        return "NONE"
    lowered = str(error_msg).lower()
    if any(k in lowered for k in ("unauthorized", "401", "403", "forbidden", "auth")):
        return "AUTH_FAILED"
    if any(k in lowered for k in ("ssl", "tls", "certificate", "handshake")):
        return "TLS_FAILED"
    if any(k in lowered for k in ("timeout", "timed out", "refused", "unreachable", "no route", "connection")):
        return "ENDPOINT_UNAVAILABLE"
    return "POLL_FAILED"


def _sanitized_process_error(status, error_msg, stale):
    code = _classify_process_status_code(status, error_msg, stale)
    return code != "NONE", code, _PROCESS_STATUS_CODE_MESSAGES[code]


def normalize_process_row(row, now, stale_after_seconds):
    """Normalizes a process STATE row to safe types; never raises on malformed/partial input."""
    recorded_at = _parse_epoch(row.get("recordedAt"))
    age, plausible = _freshness(recorded_at, now)
    stale = (not plausible) or age > stale_after_seconds
    status = normalize_process_status(row.get("status"))
    has_error, status_code, status_message = _sanitized_process_error(status, row.get("errorMsg", ""), stale)
    record_type = str(row.get("recordType", "STATE#?"))
    process_name = record_type.split("#", 1)[1] if "#" in record_type else record_type
    resolved_threshold = row.get("resolvedThreshold")
    resolved_mode = row.get("resolvedMode")
    consecutive_abends = row.get("consecutiveAbends")
    return {
        "process": process_name,
        "processType": str(row.get("processType", "?")),
        "status": status,
        "stale": stale,
        "recordedAt": recorded_at if plausible else None,
        "ageSeconds": age if plausible else None,
        "lagSeconds": decimal_to_jsonsafe(row.get("lagSeconds")) if row.get("lagSeconds") is not None else None,
        "resolvedThreshold": decimal_to_jsonsafe(resolved_threshold) if resolved_threshold is not None else None,
        "resolvedMode": str(resolved_mode) if resolved_mode is not None else None,
        "consecutiveAbends": decimal_to_jsonsafe(consecutive_abends) if consecutive_abends is not None else 0,
        "hasError": has_error,
        "statusCode": status_code,
        "statusMessage": status_message,
    }


def compute_canonical_effective_status(item, now, stale_after_seconds):
    if item is None:
        return {"effectiveStatus": "MISSING", "recordedAt": None, "ageSeconds": None, "fresh": False}
    recorded_at = _parse_epoch(item.get("recordedAt"))
    age, plausible = _freshness(recorded_at, now)
    if not plausible:
        return {"effectiveStatus": "UNKNOWN", "recordedAt": None, "ageSeconds": None, "fresh": False}
    if age > stale_after_seconds:
        return {"effectiveStatus": "STALE", "recordedAt": recorded_at, "ageSeconds": age, "fresh": False}
    mapped = _CANONICAL_STATUS_MAP.get(str(item.get("status", "UNKNOWN")), "UNKNOWN")
    return {"effectiveStatus": mapped, "recordedAt": recorded_at, "ageSeconds": age, "fresh": True}


def lease_view(lease_item, now):
    if lease_item is None:
        return None
    expires_at = _parse_epoch(lease_item.get("expiresAt"))
    fresh = expires_at is not None and expires_at > now
    return {"holder": str(lease_item.get("holder", "")), "expiresAt": expires_at, "fresh": fresh}


DISCOVERY_STATUSES = ("OK", "EMPTY", "PARTIAL", "UNAVAILABLE", "INVALID_RESPONSE")
ENDPOINT_DISCOVERY_STATUSES = ("OK", "EMPTY", "UNAVAILABLE", "INVALID_RESPONSE")
INCOMPLETE_DISCOVERY_STATUSES = ("PARTIAL", "UNAVAILABLE", "INVALID_RESPONSE")


def _non_negative_int(raw):
    """Never raises: NaN/Infinity (float or Decimal), Booleans, strings, negatives, and failed conversions all become 0."""
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, Decimal):
        if not raw.is_finite():
            return 0
        raw = int(raw)
    if not isinstance(raw, (int, float)):
        return 0
    if isinstance(raw, float) and not math.isfinite(raw):
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return 0
    return value if value >= 0 else 0


def normalize_process_discovery(raw):
    """Strict, additive-only normalization of STATE#_deployment.processDiscovery; malformed fields fail closed, never raises."""
    if not isinstance(raw, dict):
        return None
    status = str(raw.get("status") or "")
    if status not in DISCOVERY_STATUSES:
        status = "INVALID_RESPONSE"
    collected_at = _parse_epoch(raw.get("collectedAt"))

    def _endpoint_status(value):
        value = str(value or "")
        return value if value in ENDPOINT_DISCOVERY_STATUSES else "UNAVAILABLE"

    return {
        "status": status,
        "collectedAt": collected_at,
        "extractCount": _non_negative_int(raw.get("extractCount")),
        "replicatCount": _non_negative_int(raw.get("replicatCount")),
        "distpathCount": _non_negative_int(raw.get("distpathCount")),
        "totalCount": _non_negative_int(raw.get("totalCount")),
        "extractsStatus": _endpoint_status(raw.get("extractsStatus")),
        "replicatsStatus": _endpoint_status(raw.get("replicatsStatus")),
        "sourcesStatus": _endpoint_status(raw.get("sourcesStatus")),
        "detailFailureCount": _non_negative_int(raw.get("detailFailureCount")),
    }


def normalize_critical_services(raw):
    """Fail-closed: only a literal reachable=True entry counts as reachable, any other shape fails closed."""
    if not isinstance(raw, dict):
        return {}
    normalized = {}
    for name, value in raw.items():
        reachable = isinstance(value, dict) and value.get("reachable") is True
        normalized[str(name)] = reachable
    return normalized


def read_runtime_view(table, role, deployment_name, deployment_meta,
                      now, stale_after_seconds):
    """A missing STATE#_deployment record reports MISSING; process rows are queried independently of it."""
    deployment_type = deployment_meta["type"]

    config_item = get_config_item(table, deployment_name)
    lease_item = get_lease_item(table, deployment_name)
    dep_item = get_deployment_state_item(table, deployment_name)

    process_rows = query_process_state_items(table, deployment_name)
    processes = [normalize_process_row(r, now, stale_after_seconds) for r in process_rows]

    data_source = "canonical-monitor"
    if dep_item is not None:
        status_fields = compute_canonical_effective_status(dep_item, now, stale_after_seconds)
        critical_services = normalize_critical_services(dep_item.get("criticalServices"))
        process_discovery = normalize_process_discovery(dep_item.get("processDiscovery"))
    else:
        status_fields = {"effectiveStatus": "MISSING", "recordedAt": None, "ageSeconds": None, "fresh": False}
        critical_services = {}
        process_discovery = None

    return {
        "role": role,
        "deploymentName": deployment_name,
        "deploymentType": deployment_type,
        "enabled": bool(deployment_meta.get("enabled", False)),
        # "Open GoldenGate UI" portal link -- passed through verbatim from config.load_deployments() (already None whenever ingress is disabled or the canonical host is invalid); never recomputed here.
        "consoleUrl": deployment_meta.get("consoleUrl"),
        "dataSource": data_source,
        "alertsEnabled": bool(config_item.get("alertsEnabled")) if config_item else None,
        "metricsEnabled": bool(config_item.get("metricsEnabled")) if config_item else None,
        "lease": lease_view(lease_item, now),
        "criticalServices": critical_services,
        "processes": processes,
        "processDiscovery": process_discovery,
        **status_fields,
    }


def build_status_payload(config, table, deployments, logical_pipelines, clock=time.time):
    now = int(clock())
    deployments_by_name = {d["name"]: d for d in deployments}

    logical_out = []
    try:
        for lp in logical_pipelines:
            runtimes_out = []
            for role in sorted(lp["roles"]):
                deployment_name = lp["roles"][role]
                deployment_meta = deployments_by_name.get(deployment_name, {})
                runtimes_out.append(
                    read_runtime_view(table, role, deployment_name, deployment_meta,
                                      now, config.stale_after_seconds))
            logical_out.append({"pipelineId": lp["pipelineId"], "runtimes": runtimes_out})
    except (BotoCoreError, ClientError) as exc:
        summary = sanitize_error(exc)
        log_event("ERROR", "dynamodb_read_failed", message=summary)
        raise DynamoDbReadError(summary) from exc

    return {"generatedAt": now, "logicalPipelines": logical_out}


def read_deployment_processes_view(table, deployment_meta, now, stale_after_seconds):
    """Canonical STATE#-only view for one deployment; GetItem/Query only, never Scan or write."""
    deployment_name = deployment_meta["name"]
    config_item = get_config_item(table, deployment_name)
    lease_item = get_lease_item(table, deployment_name)
    dep_item = get_deployment_state_item(table, deployment_name)

    status_fields = compute_canonical_effective_status(dep_item, now, stale_after_seconds)
    process_rows = query_process_state_items(table, deployment_name)
    processes = [normalize_process_row(r, now, stale_after_seconds) for r in process_rows]
    critical_services = normalize_critical_services(dep_item.get("criticalServices")) if dep_item is not None else {}
    process_discovery = normalize_process_discovery(dep_item.get("processDiscovery")) if dep_item is not None else None

    return {
        "deploymentName": deployment_name,
        "deploymentType": deployment_meta.get("type"),
        "enabled": bool(deployment_meta.get("enabled", False)),
        "alertsEnabled": bool(config_item.get("alertsEnabled")) if config_item else None,
        "lease": lease_view(lease_item, now),
        "criticalServices": critical_services,
        "processes": processes,
        "processDiscovery": process_discovery,
        **status_fields,
    }


def build_processes_payload(config, table, deployments, clock=time.time):
    """/api/processes: one entry per configured deployment, not grouped by pipeline."""
    now = int(clock())
    try:
        deployments_out = [read_deployment_processes_view(table, d, now, config.stale_after_seconds)
                           for d in deployments]
    except (BotoCoreError, ClientError) as exc:
        summary = sanitize_error(exc)
        log_event("ERROR", "dynamodb_read_failed", message=summary)
        raise DynamoDbReadError(summary) from exc

    return {"generatedAt": now, "deployments": deployments_out}


def _json_default(value):
    if isinstance(value, Decimal):
        return decimal_to_jsonsafe(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


render_html = ui.render_html
format_relative_age = ui.format_relative_age
format_lag_threshold_mode = ui.format_lag_threshold_mode
SECURITY_HEADERS = ui.SECURITY_HEADERS


def _make_handler(config, table_factory, deployments, logical_pipelines, ready_state, expected_pipelines,
                  environment=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "gg-monitor"

        def _write(self, status_code, content_type, body_bytes):
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body_bytes)))
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body_bytes)

        def do_GET(self):  # noqa: N802
            try:
                if self.path == "/healthz":
                    self._handle_healthz()
                elif self.path == "/readyz":
                    self._handle_readyz()
                elif self.path == "/api/status":
                    self._handle_api_status()
                elif self.path == "/api/processes":
                    self._handle_api_processes()
                elif self.path == "/":
                    self._handle_root()
                else:
                    self._write(404, "text/plain; charset=utf-8", b"not found")
            except Exception as exc:  # noqa: BLE001 -- must never crash the server
                log_event("ERROR", "request_failed", path=self.path, message=sanitize_error(exc))
                try:
                    self._write(500, "text/plain; charset=utf-8", b"internal error")
                except Exception:
                    pass

        def _handle_healthz(self):
            # Process liveness only -- never touches DynamoDB.
            body = json.dumps({"status": "ok", "version": config.monitor_version}).encode("utf-8")
            self._write(200, "application/json", body)

        def _handle_readyz(self):
            """Collector readiness plus a bounded DescribeTable via a request-local Table."""
            if expected_pipelines and not all(ready_state.get(p) for p in expected_pipelines):
                self._write(503, "application/json", json.dumps({"status": "not_ready"}).encode("utf-8"))
                return
            try:
                table = table_factory()
                check_dynamodb_ready(table)
            except Exception as exc:
                log_event("ERROR", "readyz_check_failed", message=sanitize_error(exc))
                self._write(503, "application/json", json.dumps({"status": "not_ready"}).encode("utf-8"))
                return
            self._write(200, "application/json", json.dumps({"status": "ready"}).encode("utf-8"))

        def _build_payload(self):
            try:
                table = table_factory()
                return build_status_payload(config, table, deployments, logical_pipelines), None
            except DynamoDbReadError:
                return None, CLIENT_SAFE_DYNAMODB_ERROR_MESSAGE
            except Exception as exc:
                log_event("ERROR", "table_factory_failed", message=sanitize_error(exc))
                return None, CLIENT_SAFE_DYNAMODB_ERROR_MESSAGE

        def _handle_api_status(self):
            payload, error_message = self._build_payload()
            if error_message:
                body = json.dumps({"error": "dynamodb_unavailable", "message": error_message}).encode("utf-8")
                self._write(503, "application/json", body)
                return
            self._write(200, "application/json", json.dumps(payload, default=_json_default).encode("utf-8"))

        def _build_processes_payload(self):
            try:
                table = table_factory()
                return build_processes_payload(config, table, deployments), None
            except DynamoDbReadError:
                return None, CLIENT_SAFE_DYNAMODB_ERROR_MESSAGE
            except Exception as exc:
                log_event("ERROR", "table_factory_failed", message=sanitize_error(exc))
                return None, CLIENT_SAFE_DYNAMODB_ERROR_MESSAGE

        def _handle_api_processes(self):
            payload, error_message = self._build_processes_payload()
            if error_message:
                body = json.dumps({"error": "dynamodb_unavailable", "message": error_message}).encode("utf-8")
                self._write(503, "application/json", body)
                return
            self._write(200, "application/json", json.dumps(payload, default=_json_default).encode("utf-8"))

        def _handle_root(self):
            payload, error_message = self._build_payload()
            if payload is None:
                payload = {"generatedAt": int(time.time()), "logicalPipelines": []}
            body = render_html(payload, config, error_message=error_message, environment=environment).encode("utf-8")
            self._write(200, "text/html; charset=utf-8", body)

        def log_message(self, fmt, *args):
            return

    return Handler


def start_http_server(config, table_factory, deployments, logical_pipelines, ready_state, expected_pipelines,
                      environment=None):
    handler_cls = _make_handler(config, table_factory, deployments, logical_pipelines, ready_state,
                                expected_pipelines, environment=environment)
    server = ThreadingHTTPServer(("0.0.0.0", config.port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main():
    try:
        config = cfgmod.load_config(os.environ)
    except cfgmod.ConfigError as exc:
        print(json.dumps({"timestamp": time.time(), "level": "ERROR",
                          "event": "configuration_invalid", "message": str(exc)}))
        sys.exit(1)

    try:
        doc = cfgmod.load_deployments(config.repo_config_root)
    except cfgmod.ConfigError as exc:
        print(json.dumps({"timestamp": time.time(), "level": "ERROR",
                          "event": "deployments_invalid", "message": str(exc)}))
        sys.exit(1)

    deployments = doc["deployments"]
    try:
        cfgmod.validate_enabled_deployments(deployments)
    except cfgmod.StartupValidationError as exc:
        print(json.dumps({"timestamp": time.time(), "level": "ERROR",
                          "event": "startup_validation_failed", "message": str(exc)}))
        sys.exit(1)

    logical_pipelines = cfgmod.build_logical_pipelines(deployments)
    enabled = [d for d in deployments if d["enabled"]]
    monitor_instance = os.environ.get("POD_NAME", "gg-monitor")

    stop_event = threading.Event()
    ready_state = {}

    table_factory = create_dynamodb_table_factory(config)
    server = start_http_server(config, table_factory, deployments, logical_pipelines,
                               ready_state, [d["name"] for d in enabled], environment=doc["environment"])

    def _handle_signal(signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    collector_threads = []
    for deployment in enabled:
        t = threading.Thread(
            target=collector.run_pipeline,
            args=(deployment, stop_event, ready_state, config.aws_region, config.dynamodb_table, monitor_instance),
            daemon=True)
        t.start()
        collector_threads.append(t)

    log_event("INFO", "monitor_started", version=config.monitor_version, port=config.port,
             enabledDeployments=[d["name"] for d in enabled])

    try:
        while not stop_event.is_set():
            stop_event.wait(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        log_event("INFO", "monitor_stopping")
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
