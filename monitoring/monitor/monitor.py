"""GoldenGate shared monitoring portal.

Read-only, cross-deployment operator view over the CANONICAL DynamoDB
monitoring contract (correction pass): CONFIG (GetItem), LEASE (GetItem),
and STATE#_deployment / STATE#<process> (GetItem / Query, begins_with
"STATE#"). Never writes to DynamoDB, never Scans, never calls GoldenGate or
the Kubernetes API.

This portal groups canonical runtimes by LOGICAL PIPELINE (source/target
role), reading pipelines/deployments.yaml + topologies/dev/*.yaml via
inventory.py -- the same canonical inventory the gg-monitor-core collector
reads (see monitoring/monitor/tests/test_inventory_drift.py for the
guarantee both loaders agree).

Legacy fallback (temporary, removable via legacyFallback.enabled=false):
for a role whose canonical STATE#_deployment record does not exist yet, and
only then, this portal falls back to reading the OLD observer's own
STATE#_deployment record under the legacy per-role key (derived as
"gg-<pipelineId>-<role>", never hardcoded), normalizing its
HEALTHY/DEGRADED/DOWN status into this module's own UP/STARTING/DOWN/
STALE/MISSING/UNKNOWN effective-status model. The canonical record always
wins the instant it exists -- this fallback never masks real canonical
data, and never writes or migrates anything.
"""

import html
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

import inventory

RECORD_TYPE_CONFIG = "CONFIG"
RECORD_TYPE_LEASE = "LEASE"
RECORD_TYPE_DEPLOYMENT_STATE = "STATE#_deployment"
STATE_PREFIX = "STATE#"

# Bounded botocore configuration. Not exposed as environment variables (the
# Helm chart does not set them) -- fixed, obviously-positive constants so an
# unavailable DynamoDB service can never hang an HTTP request indefinitely.
CONNECT_TIMEOUT_SECONDS = 3
READ_TIMEOUT_SECONDS = 5
MAX_RETRY_ATTEMPTS = 2

DEFAULTS = {
    "PORT": "8080",
    "STALE_AFTER_SECONDS": "120",
    "REFRESH_SECONDS": "30",
    "MONITOR_VERSION": "development",
    "LEGACY_FALLBACK_ENABLED": "true",
}

# Canonical deployment-level raw statuses (written by gg-monitor-core to
# STATE#_deployment.status) and the closed set of effective statuses this
# portal ever renders. A raw status outside CANONICAL_RAW_STATUSES
# normalizes to UNKNOWN rather than being passed through verbatim.
CANONICAL_RAW_STATUSES = ("UP", "STARTING", "DEPLOYMENT_DOWN")
EFFECTIVE_STATUSES = ("UP", "STARTING", "DOWN", "STALE", "MISSING", "UNKNOWN")
_CANONICAL_STATUS_MAP = {"UP": "UP", "STARTING": "STARTING", "DEPLOYMENT_DOWN": "DOWN"}

# Old observer statuses (legacy fallback only), normalized into the same
# closed EFFECTIVE_STATUSES set above. DEGRADED intentionally maps to
# UNKNOWN, not STARTING or DOWN: the old schema's "degraded" concept (up,
# but with an unspecified issue) does not correspond to either "starting
# up" or "confirmed down", and this fallback must never overclaim either
# direction.
_LEGACY_STATUS_MAP = {"HEALTHY": "UP", "DOWN": "DOWN", "DEGRADED": "UNKNOWN"}

# Closed process-status enum this portal ever renders -- a raw
# STATE#<process>.status outside this set (should not happen; gg-monitor-
# core always writes one of these) normalizes to UNKNOWN.
PROCESS_STATUSES = ("RUNNING", "STOPPED", "ABENDED", "UNKNOWN")

# A recordedAt more than this far in the future is not trusted as a real
# observation (clock skew beyond this is treated as a malformed timestamp).
FUTURE_TIMESTAMP_TOLERANCE_SECONDS = 300

# Fixed, client-safe message for any DynamoDB failure. The real botocore/AWS
# error (which may contain an IAM principal ARN, account ID, table ARN, or
# request ID) is only ever logged server-side via sanitize_error() -- it
# must never reach an API or HTML client.
CLIENT_SAFE_DYNAMODB_ERROR_MESSAGE = "Monitoring data is temporarily unavailable."

# Fixed, closed statusCode -> statusMessage map for per-process errors.
# STATE#<process>.errorMsg is the RAW text of whatever the GoldenGate Admin
# REST client last saw -- potentially hostnames, service URLs, schema
# names, internal paths, secret references, driver/TLS detail. That raw
# text is fine in DynamoDB (written by the collector, for a future
# alerter); it must NEVER reach this portal's HTML/JSON output. Only a
# fixed statusCode from this small, closed enum plus a fixed, generic
# statusMessage is ever exposed.
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

STATUS_COLORS = {
    "UP": "#1a7f37",
    "STARTING": "#9a6700",
    "DOWN": "#cf222e",
    "STALE": "#9a6700",
    "MISSING": "#cf222e",
    "UNKNOWN": "#57606a",
    "RUNNING": "#1a7f37",
    "STOPPED": "#9a6700",
    "ABENDED": "#cf222e",
}

logger = logging.getLogger("goldengate.monitor")
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(_handler)
logger.propagate = False


class ConfigError(Exception):
    """Raised when required monitor configuration is missing or invalid."""


class DynamoDbReadError(Exception):
    """Raised when a GetItem/Query call fails against AWS while building
    the status payload."""


@dataclass(frozen=True)
class MonitorConfig:
    aws_region: str
    dynamodb_table: str
    port: int
    stale_after_seconds: int
    refresh_seconds: int
    monitor_version: str
    repo_config_root: str
    legacy_fallback_enabled: bool


def _get_int(env, name, default):
    raw = env.get(name, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None


def _parse_bool_env(raw, default):
    if raw is None:
        return default
    return str(raw).strip().lower() in ("true", "1", "yes")


def load_config(env) -> MonitorConfig:
    missing = sorted(name for name in ("AWS_REGION", "DYNAMODB_TABLE") if not env.get(name))
    if missing:
        raise ConfigError("Missing required environment variables: " + ", ".join(missing))

    port = _get_int(env, "PORT", DEFAULTS["PORT"])
    if not (1 <= port <= 65535):
        raise ConfigError(f"PORT must be between 1 and 65535, got {port}")

    stale_after_seconds = _get_int(env, "STALE_AFTER_SECONDS", DEFAULTS["STALE_AFTER_SECONDS"])
    if stale_after_seconds <= 0:
        raise ConfigError(
            f"STALE_AFTER_SECONDS must be a positive integer, got {stale_after_seconds}"
        )

    refresh_seconds = _get_int(env, "REFRESH_SECONDS", DEFAULTS["REFRESH_SECONDS"])
    if refresh_seconds <= 0:
        raise ConfigError(f"REFRESH_SECONDS must be a positive integer, got {refresh_seconds}")

    return MonitorConfig(
        aws_region=env["AWS_REGION"],
        dynamodb_table=env["DYNAMODB_TABLE"],
        port=port,
        stale_after_seconds=stale_after_seconds,
        refresh_seconds=refresh_seconds,
        monitor_version=env.get("MONITOR_VERSION", DEFAULTS["MONITOR_VERSION"]),
        repo_config_root=env.get("REPO_CONFIG_ROOT", inventory.DEFAULT_REPO_ROOT),
        legacy_fallback_enabled=_parse_bool_env(
            env.get("LEGACY_FALLBACK_ENABLED"), DEFAULTS["LEGACY_FALLBACK_ENABLED"] == "true"
        ),
    )


# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------


def log_event(level, event, **fields):
    record = {"timestamp": time.time(), "level": level, "event": event}
    record.update({k: v for k, v in fields.items() if v is not None})
    line = json.dumps(record, default=str)
    if level == "ERROR":
        logger.error(line)
    else:
        logger.info(line)


def sanitize_error(exc: BaseException, max_len: int = 200) -> str:
    """Concise, sanitized error summary -- no stack trace, no credentials."""
    message = f"{type(exc).__name__}: {exc}".replace("\n", " ").replace("\r", " ")
    return message[:max_len]


# ---------------------------------------------------------------------------
# DynamoDB integration (read-only: GetItem / Query only, never Scan/writes)
# ---------------------------------------------------------------------------


def _boto_config() -> BotoConfig:
    return BotoConfig(
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        read_timeout=READ_TIMEOUT_SECONDS,
        retries={"max_attempts": MAX_RETRY_ATTEMPTS, "mode": "standard"},
    )


def create_dynamodb_table_factory(config: MonitorConfig):
    """Zero-argument callable, NOT a pre-built Table object (section 12):
    ThreadingHTTPServer hands each request its own thread, so a single
    shared boto3 Table/Resource object read/used concurrently across those
    threads would be a mutable-object-across-threads hazard. Every request
    that needs DynamoDB access calls this factory itself to obtain its OWN,
    independent Table object, used only within that single request/thread
    and then discarded."""
    def _factory():
        session = boto3.session.Session()
        resource = session.resource("dynamodb", region_name=config.aws_region, config=_boto_config())
        return resource.Table(config.dynamodb_table)
    return _factory


def check_dynamodb_ready(table):
    """Bounded readiness probe using DescribeTable (already granted to the role)."""
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
    """All STATE#<process> rows for one canonical pipeline, EXCLUDING the
    STATE#_deployment pseudo-row (read separately via
    get_deployment_state_item). Query only -- never Scan."""
    resp = table.query(
        KeyConditionExpression="pipeline = :p AND begins_with(recordType, :prefix)",
        ExpressionAttributeValues={":p": pipeline, ":prefix": STATE_PREFIX},
    )
    return [it for it in resp.get("Items", []) if it.get("recordType") != RECORD_TYPE_DEPLOYMENT_STATE]


# ---------------------------------------------------------------------------
# Normalization / data model
# ---------------------------------------------------------------------------


def decimal_to_jsonsafe(value):
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    return value


def _parse_epoch(raw):
    """Best-effort epoch-seconds int from a raw DynamoDB attribute. Returns
    None for a missing or malformed value instead of raising."""
    if raw is None:
        return None
    try:
        return int(decimal_to_jsonsafe(raw))
    except (TypeError, ValueError):
        return None


def _freshness(recorded_at, now):
    """Returns (age_seconds, plausible). plausible=False for a missing or
    implausibly-future recordedAt -- callers must treat that as UNKNOWN,
    never as a negative or fabricated age."""
    if recorded_at is None:
        return None, False
    age = now - recorded_at
    if age < 0:
        if age < -FUTURE_TIMESTAMP_TOLERANCE_SECONDS:
            return None, False
        age = 0  # small clock skew -- never expose a negative age
    return int(age), True


def normalize_process_status(raw):
    raw = str(raw or "UNKNOWN").upper()
    return raw if raw in PROCESS_STATUSES else "UNKNOWN"


def _classify_process_status_code(status, error_msg, stale):
    """Maps (status, raw error text, staleness) to one fixed, closed
    statusCode -- never the raw text itself. Staleness is checked first."""
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
    """Returns (hasError, statusCode, statusMessage) -- the only
    error-related fields this portal is permitted to expose."""
    code = _classify_process_status_code(status, error_msg, stale)
    has_error = code != "NONE"
    return has_error, code, _PROCESS_STATUS_CODE_MESSAGES[code]


def normalize_process_row(row, now, stale_after_seconds):
    recorded_at = _parse_epoch(row.get("recordedAt"))
    age, plausible = _freshness(recorded_at, now)
    stale = (not plausible) or age > stale_after_seconds
    status = normalize_process_status(row.get("status"))
    has_error, status_code, status_message = _sanitized_process_error(
        status, row.get("errorMsg", ""), stale
    )
    record_type = str(row.get("recordType", "STATE#?"))
    process_name = record_type.split("#", 1)[1] if "#" in record_type else record_type
    return {
        "process": process_name,
        "processType": str(row.get("processType", "?")),
        "status": status,
        "stale": stale,
        "recordedAt": recorded_at if plausible else None,
        "ageSeconds": age if plausible else None,
        "lagSeconds": decimal_to_jsonsafe(row.get("lagSeconds")) if row.get("lagSeconds") is not None else None,
        "hasError": has_error,
        "statusCode": status_code,
        "statusMessage": status_message,
    }


def compute_canonical_effective_status(item, now, stale_after_seconds):
    """Canonical STATE#_deployment.status (UP/STARTING/DEPLOYMENT_DOWN/
    UNKNOWN) -> effective portal status (UP/STARTING/DOWN/STALE/MISSING/
    UNKNOWN)."""
    if item is None:
        return {"effectiveStatus": "MISSING", "recordedAt": None, "ageSeconds": None, "fresh": False}
    recorded_at = _parse_epoch(item.get("recordedAt"))
    age, plausible = _freshness(recorded_at, now)
    if not plausible:
        return {"effectiveStatus": "UNKNOWN", "recordedAt": None, "ageSeconds": None, "fresh": False}
    if age > stale_after_seconds:
        return {"effectiveStatus": "STALE", "recordedAt": recorded_at, "ageSeconds": age, "fresh": False}
    raw_status = str(item.get("status", "UNKNOWN"))
    mapped = _CANONICAL_STATUS_MAP.get(raw_status, "UNKNOWN")
    return {"effectiveStatus": mapped, "recordedAt": recorded_at, "ageSeconds": age, "fresh": True}


def compute_legacy_effective_status(item, now, stale_after_seconds):
    """Legacy observer STATE#_deployment.status (HEALTHY/DEGRADED/DOWN)
    -> the SAME effective portal status enum as the canonical path."""
    if item is None:
        return {"effectiveStatus": "MISSING", "recordedAt": None, "ageSeconds": None, "fresh": False}
    recorded_at = _parse_epoch(item.get("recordedAt"))
    age, plausible = _freshness(recorded_at, now)
    if not plausible:
        return {"effectiveStatus": "UNKNOWN", "recordedAt": None, "ageSeconds": None, "fresh": False}
    if age > stale_after_seconds:
        return {"effectiveStatus": "STALE", "recordedAt": recorded_at, "ageSeconds": age, "fresh": False}
    raw_status = str(item.get("status", ""))
    mapped = _LEGACY_STATUS_MAP.get(raw_status, "UNKNOWN")
    return {"effectiveStatus": mapped, "recordedAt": recorded_at, "ageSeconds": age, "fresh": True}


def lease_view(lease_item, now):
    if lease_item is None:
        return None
    expires_at = _parse_epoch(lease_item.get("expiresAt"))
    fresh = expires_at is not None and expires_at > now
    return {"holder": str(lease_item.get("holder", "")), "expiresAt": expires_at, "fresh": fresh}


def read_runtime_view(table, pipeline_id, role, role_info, runtime_meta, legacy_fallback_enabled, now, stale_after_seconds):
    """Assemble one logical-pipeline role's full portal view.

    Canonical-first, legacy-fallback-second (section 11): the canonical
    STATE#_deployment record for this role's own canonical pipeline key is
    read FIRST and, if present, is ALWAYS used (dataSource=canonical-
    monitor) -- legacy data is never consulted once canonical data exists.
    Only when the canonical record is missing, and only when
    legacy_fallback_enabled, does this fall back to the legacy per-role key
    "gg-<pipelineId>-<role>" (derived here, never hardcoded) and normalize
    its old HEALTHY/DEGRADED/DOWN status (dataSource=legacy-observer-
    fallback). This function never writes to DynamoDB.
    """
    canonical_pipeline = role_info["pipeline"]
    deployment_type = role_info["deploymentType"]

    config_item = get_config_item(table, canonical_pipeline)
    lease_item = get_lease_item(table, canonical_pipeline)
    dep_item = get_deployment_state_item(table, canonical_pipeline)

    if dep_item is not None:
        data_source = "canonical-monitor"
        status_fields = compute_canonical_effective_status(dep_item, now, stale_after_seconds)
        process_rows = query_process_state_items(table, canonical_pipeline)
        processes = [normalize_process_row(r, now, stale_after_seconds) for r in process_rows]
        critical_services = dep_item.get("criticalServices") or {}
    elif legacy_fallback_enabled:
        legacy_pipeline = f"gg-{pipeline_id}-{role}"
        legacy_item = get_deployment_state_item(table, legacy_pipeline)
        data_source = "legacy-observer-fallback"
        status_fields = compute_legacy_effective_status(legacy_item, now, stale_after_seconds)
        processes = []  # the old observer schema has no per-process STATE rows
        critical_services = {}
    else:
        data_source = "canonical-monitor"
        status_fields = {"effectiveStatus": "MISSING", "recordedAt": None, "ageSeconds": None, "fresh": False}
        processes = []
        critical_services = {}

    return {
        "role": role,
        "deploymentName": canonical_pipeline,
        "deploymentType": deployment_type,
        "enabled": bool(runtime_meta.get("enabled", False)),
        "dataSource": data_source,
        "alertsEnabled": bool(config_item.get("alertsEnabled")) if config_item else None,
        "metricsEnabled": bool(config_item.get("metricsEnabled")) if config_item else None,
        "lease": lease_view(lease_item, now),
        "criticalServices": {k: bool((v or {}).get("reachable")) for k, v in (critical_services or {}).items()},
        "processes": processes,
        **status_fields,
    }


def build_status_payload(config: MonitorConfig, table, runtimes, logical_pipelines, clock=time.time):
    """Read every canonical logical pipeline / role and return the
    normalized, sanitized payload (section 16's recommended /api/status
    shape).

    Raises DynamoDbReadError if any GetItem/Query call fails against AWS.
    """
    now = int(clock())
    runtimes_by_pipeline = {r["pipeline"]: r for r in runtimes}

    logical_out = []
    try:
        for lp in logical_pipelines:
            runtimes_out = []
            for role in sorted(lp["roles"]):
                role_info = lp["roles"][role]
                runtime_meta = runtimes_by_pipeline.get(role_info["pipeline"], {})
                runtimes_out.append(
                    read_runtime_view(
                        table, lp["pipelineId"], role, role_info, runtime_meta,
                        config.legacy_fallback_enabled, now, config.stale_after_seconds,
                    )
                )
            logical_out.append({
                "pipelineId": lp["pipelineId"],
                "environment": lp["environment"],
                "runtimes": runtimes_out,
            })
    except (BotoCoreError, ClientError) as exc:
        summary = sanitize_error(exc)
        log_event("ERROR", "dynamodb_read_failed", message=summary)
        raise DynamoDbReadError(summary) from exc

    return {"generatedAt": now, "logicalPipelines": logical_out}


def _json_default(value):
    if isinstance(value, Decimal):
        return decimal_to_jsonsafe(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


# ---------------------------------------------------------------------------
# HTML rendering (inline CSS only, no external assets, everything escaped)
# ---------------------------------------------------------------------------


def _esc(value, default="-"):
    if value is None:
        return html.escape(default)
    return html.escape(str(value))


def _status_badge(status):
    color = STATUS_COLORS.get(status, STATUS_COLORS["UNKNOWN"])
    safe_status = html.escape(str(status))
    safe_color = html.escape(color)
    return (
        f'<span style="display:inline-block;padding:2px 10px;border-radius:4px;'
        f'background:{safe_color};color:#ffffff;font-weight:600;font-size:0.85em;">'
        f"{safe_status}</span>"
    )


def render_html(payload, config: MonitorConfig, error_message=None):
    sections = []

    if error_message:
        sections.append(
            '<div style="background:#ffebe9;border:1px solid #cf222e;color:#82071e;'
            'padding:10px 14px;border-radius:6px;margin-bottom:16px;">'
            f"Unable to read monitoring data: {html.escape(error_message)}</div>"
        )

    for lp in payload.get("logicalPipelines", []):
        rows = []
        for r in lp.get("runtimes", []):
            age = r.get("ageSeconds")
            age_text = f"{age}s ago" if age is not None else "-"
            if r["processes"]:
                proc_rows = "".join(
                    "<tr>"
                    f"<td>{_esc(p['process'])}</td>"
                    f"<td>{_status_badge(p['status'])}</td>"
                    f"<td>{_esc(p['lagSeconds'])}</td>"
                    f"<td>{_esc(p['statusMessage']) if p['hasError'] else ''}</td>"
                    "</tr>"
                    for p in r["processes"]
                )
                proc_table = (
                    '<table style="margin-top:4px;font-size:0.85em;">'
                    "<thead><tr><th>Process</th><th>Status</th><th>Lag</th><th>Error</th></tr></thead>"
                    f"<tbody>{proc_rows}</tbody></table>"
                )
            else:
                proc_table = "<p><em>No process STATE rows found.</em></p>"

            lease = r.get("lease")
            lease_text = (
                f"holder={_esc(lease['holder'] or 'none')} ({'valid' if lease['fresh'] else 'EXPIRED'})"
                if lease else "none"
            )

            rows.append(
                "<tr>"
                f"<td>{_esc(r.get('role'))}</td>"
                f"<td>{_esc(r.get('deploymentName'))}</td>"
                f"<td>{_esc(r.get('deploymentType'))}</td>"
                f"<td>{_status_badge(r.get('effectiveStatus'))}</td>"
                f"<td>{_esc(r.get('dataSource'))}</td>"
                f"<td>{html.escape(age_text)}</td>"
                f"<td>{html.escape(lease_text)}</td>"
                f"<td>{proc_table}</td>"
                "</tr>"
            )
        sections.append(
            f'<h2 style="margin-top:24px;">{_esc(lp.get("pipelineId"))} '
            f'<span style="font-size:0.6em;color:#57606a;">({_esc(lp.get("environment"))})</span></h2>'
            '<table style="border-collapse:collapse;width:100%;font-size:0.9em;">'
            '<thead><tr style="text-align:left;border-bottom:2px solid #d0d7de;">'
            "<th>Role</th><th>Deployment</th><th>Type</th><th>Status</th>"
            "<th>Source</th><th>Recorded</th><th>Lease</th><th>Processes</th>"
            "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )

    if not payload.get("logicalPipelines"):
        sections.append("<p>No logical pipelines found in the canonical topology.</p>")

    generated_at = html.escape(str(payload.get("generatedAt", "-")))
    stale_after = html.escape(str(config.stale_after_seconds))
    version = html.escape(str(config.monitor_version))
    refresh_seconds = int(config.refresh_seconds)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="{refresh_seconds}">
<title>GoldenGate Monitoring Portal</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif; margin: 24px; color: #1f2328; background: #ffffff; }}
  table {{ margin-top: 8px; }}
  th, td {{ padding: 6px 10px; border-bottom: 1px solid #eaeef2; vertical-align: top; }}
  footer {{ margin-top: 32px; color: #57606a; font-size: 0.8em; }}
</style>
</head>
<body>
<h1>GoldenGate Monitoring Portal</h1>
{"".join(sections)}
<footer>Generated at {generated_at} (epoch seconds) &middot; stale after {stale_after}s &middot; monitor {version} &middot; auto-refreshes every {refresh_seconds}s</footer>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none';",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
}


def _make_handler(config: MonitorConfig, table_factory, runtimes, logical_pipelines):
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

        def do_GET(self):  # noqa: N802 (stdlib naming convention)
            try:
                if self.path == "/healthz":
                    self._handle_healthz()
                elif self.path == "/readyz":
                    self._handle_readyz()
                elif self.path == "/api/status":
                    self._handle_api_status()
                elif self.path == "/":
                    self._handle_root()
                else:
                    self._write(404, "text/plain; charset=utf-8", b"not found")
            except Exception as exc:  # noqa: BLE001 -- the handler must never crash the server
                log_event("ERROR", "request_failed", path=self.path, message=sanitize_error(exc))
                try:
                    self._write(500, "text/plain; charset=utf-8", b"internal error")
                except Exception:  # noqa: BLE001 -- best-effort only, connection may already be gone
                    pass

        def _handle_healthz(self):
            # Process liveness only -- NEVER touches DynamoDB.
            body = json.dumps({"status": "ok", "version": config.monitor_version}).encode("utf-8")
            self._write(200, "application/json", body)

        def _handle_readyz(self):
            # Bounded readiness probe using a request-local Table object
            # (fresh from the factory, never shared across threads/requests).
            try:
                table = table_factory()
                check_dynamodb_ready(table)
            except Exception as exc:
                log_event("ERROR", "readyz_check_failed", message=sanitize_error(exc))
                body = json.dumps({"status": "not_ready"}).encode("utf-8")
                self._write(503, "application/json", body)
                return
            body = json.dumps({"status": "ready"}).encode("utf-8")
            self._write(200, "application/json", body)

        def _build_payload(self):
            """Obtains a Table object FRESH from the factory for this
            request only, and tolerates the factory call itself failing the
            same client-safe way a DynamoDB read failure is tolerated --
            never an unhandled exception, never raw AWS exception text
            reaching the caller."""
            try:
                table = table_factory()
                return build_status_payload(config, table, runtimes, logical_pipelines), None
            except DynamoDbReadError:
                return None, CLIENT_SAFE_DYNAMODB_ERROR_MESSAGE
            except Exception as exc:
                log_event("ERROR", "table_factory_failed", message=sanitize_error(exc))
                return None, CLIENT_SAFE_DYNAMODB_ERROR_MESSAGE

        def _handle_api_status(self):
            payload, error_message = self._build_payload()
            if error_message:
                body = json.dumps(
                    {"error": "dynamodb_unavailable", "message": error_message}
                ).encode("utf-8")
                self._write(503, "application/json", body)
                return
            body = json.dumps(payload, default=_json_default).encode("utf-8")
            self._write(200, "application/json", body)

        def _handle_root(self):
            payload, error_message = self._build_payload()
            if payload is None:
                payload = {"generatedAt": int(time.time()), "logicalPipelines": []}
            body = render_html(payload, config, error_message=error_message).encode("utf-8")
            self._write(200, "text/html; charset=utf-8", body)

        def log_message(self, fmt, *args):
            # Suppressed default access logging -- structured JSON events
            # (request_failed, etc.) cover observability without leaking
            # request internals to stderr in an unstructured format.
            return

    return Handler


def start_http_server(config: MonitorConfig, table_factory, runtimes, logical_pipelines):
    handler_cls = _make_handler(config, table_factory, runtimes, logical_pipelines)
    server = ThreadingHTTPServer(("0.0.0.0", config.port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main():
    try:
        config = load_config(os.environ)
    except ConfigError as exc:
        print(json.dumps({
            "timestamp": time.time(),
            "level": "ERROR",
            "event": "configuration_invalid",
            "message": str(exc),
        }))
        sys.exit(1)

    try:
        runtimes = inventory.load_runtimes(config.repo_config_root)
        logical_pipelines = inventory.build_logical_pipelines(config.repo_config_root)
    except inventory.InventoryError as exc:
        print(json.dumps({
            "timestamp": time.time(),
            "level": "ERROR",
            "event": "inventory_invalid",
            "message": str(exc),
        }))
        sys.exit(1)

    table_factory = create_dynamodb_table_factory(config)

    stop_event = threading.Event()

    def _handle_signal(signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    server = start_http_server(config, table_factory, runtimes, logical_pipelines)
    log_event(
        "INFO",
        "monitor_started",
        version=config.monitor_version,
        port=config.port,
        logicalPipelineCount=len(logical_pipelines),
        legacyFallbackEnabled=config.legacy_fallback_enabled,
    )

    stop_event.wait()

    log_event("INFO", "monitor_stopping")
    server.shutdown()
    server.server_close()


if __name__ == "__main__":
    main()
