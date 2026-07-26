"""GoldenGate shared monitoring portal (Phase 2).

Read-only, cross-deployment operator view. Reads only explicitly configured
DynamoDB STATE#_deployment records via GetItem. Never writes to DynamoDB,
never Scans, never calls GoldenGate or the Kubernetes API.
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
from typing import Optional

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

STATE_RECORD_TYPE = "STATE#_deployment"

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
}

ALLOWED_OBSERVED_STATUSES = ("HEALTHY", "DEGRADED", "DOWN")

# Effective status precedence used to pick a deployment's overall status:
# earlier entries are more severe.
SEVERITY_ORDER = ("DOWN", "MISSING", "STALE", "DEGRADED", "UNKNOWN", "HEALTHY")

STATUS_COLORS = {
    "HEALTHY": "#1a7f37",
    "DEGRADED": "#9a6700",
    "DOWN": "#cf222e",
    "STALE": "#9a6700",
    "MISSING": "#cf222e",
    "UNKNOWN": "#57606a",
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
    """Raised when a configured pipeline's GetItem call fails against AWS."""


@dataclass(frozen=True)
class MonitorConfig:
    aws_region: str
    dynamodb_table: str
    pipelines: tuple
    port: int
    stale_after_seconds: int
    refresh_seconds: int
    monitor_version: str


def _get_int(env, name, default):
    raw = env.get(name, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None


def _parse_pipelines(raw):
    items = [p.strip() for p in raw.split(",")]
    items = [p for p in items if p]
    seen = set()
    result = []
    for pipeline in items:
        if pipeline not in seen:
            seen.add(pipeline)
            result.append(pipeline)
    return tuple(result)


def load_config(env) -> MonitorConfig:
    missing = sorted(
        name for name in ("AWS_REGION", "DYNAMODB_TABLE", "PIPELINES") if not env.get(name)
    )
    if missing:
        raise ConfigError("Missing required environment variables: " + ", ".join(missing))

    pipelines = _parse_pipelines(env["PIPELINES"])
    if not pipelines:
        raise ConfigError("PIPELINES must contain at least one non-empty pipeline key")

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
        pipelines=pipelines,
        port=port,
        stale_after_seconds=stale_after_seconds,
        refresh_seconds=refresh_seconds,
        monitor_version=env.get("MONITOR_VERSION", DEFAULTS["MONITOR_VERSION"]),
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
# DynamoDB integration (read-only)
# ---------------------------------------------------------------------------


def _boto_config() -> BotoConfig:
    return BotoConfig(
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        read_timeout=READ_TIMEOUT_SECONDS,
        retries={"max_attempts": MAX_RETRY_ATTEMPTS, "mode": "standard"},
    )


def create_dynamodb_table(config: MonitorConfig):
    session = boto3.session.Session()
    resource = session.resource("dynamodb", region_name=config.aws_region, config=_boto_config())
    return resource.Table(config.dynamodb_table)


def get_pipeline_item(table, pipeline: str):
    """GetItem only (eventually consistent). Returns dict or None."""
    resp = table.get_item(
        Key={"pipeline": pipeline, "recordType": STATE_RECORD_TYPE},
        ConsistentRead=False,
    )
    return resp.get("Item")


def check_dynamodb_ready(table):
    """Bounded readiness probe using DescribeTable (already granted to the role)."""
    table.meta.client.describe_table(TableName=table.name)


# ---------------------------------------------------------------------------
# Normalization / data model
# ---------------------------------------------------------------------------


def decimal_to_jsonsafe(value):
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    return value


def derive_deployment_and_component(pipeline: str):
    """Fallback deploymentId/component derived from the pipeline key itself,
    used only when no item exists to read the real attributes from."""
    if pipeline.startswith("gg-") and pipeline.endswith("-source"):
        return pipeline[len("gg-"):-len("-source")], "source"
    if pipeline.startswith("gg-") and pipeline.endswith("-target"):
        return pipeline[len("gg-"):-len("-target")], "target"
    return pipeline, "unknown"


def compute_effective_status(item, now, stale_after_seconds):
    """Returns (effectiveStatus, ageSeconds, recordedAt)."""
    if item is None:
        return "MISSING", None, None

    recorded_at_raw = item.get("recordedAt")
    if recorded_at_raw is None:
        return "UNKNOWN", None, None

    recorded_at = int(decimal_to_jsonsafe(recorded_at_raw))
    age_seconds = int(now - recorded_at)

    if age_seconds > stale_after_seconds:
        return "STALE", age_seconds, recorded_at

    raw_status = item.get("status")
    if raw_status not in ALLOWED_OBSERVED_STATUSES:
        return "UNKNOWN", age_seconds, recorded_at

    return raw_status, age_seconds, recorded_at


def build_pipeline_status(pipeline: str, item, now, stale_after_seconds):
    """Normalize an item (or its absence) into the allowlisted schema.

    Only the fields explicitly listed below are ever read from the raw
    DynamoDB item -- no other attribute is passed through, regardless of
    what else the record may contain.
    """
    fallback_deployment_id, fallback_component = derive_deployment_and_component(pipeline)
    effective_status, age_seconds, recorded_at = compute_effective_status(
        item, now, stale_after_seconds
    )
    fresh = effective_status not in ("MISSING", "STALE")

    if item is None:
        return {
            "pipeline": pipeline,
            "deploymentId": fallback_deployment_id,
            "component": fallback_component,
            "engine": None,
            "observedStatus": None,
            "effectiveStatus": effective_status,
            "fresh": False,
            "ageSeconds": None,
            "recordedAt": None,
            "adminEndpointHealthy": None,
            "metricsEndpointHealthy": None,
            "u02Mounted": None,
            "podName": None,
            "namespace": None,
            "observerVersion": None,
            "errorSummary": None,
        }

    return {
        "pipeline": pipeline,
        "deploymentId": item.get("deploymentId", fallback_deployment_id),
        "component": item.get("component", fallback_component),
        "engine": item.get("engine"),
        "observedStatus": item.get("status"),
        "effectiveStatus": effective_status,
        "fresh": fresh,
        "ageSeconds": age_seconds,
        "recordedAt": recorded_at,
        "adminEndpointHealthy": item.get("adminEndpointHealthy"),
        "metricsEndpointHealthy": item.get("metricsEndpointHealthy"),
        "u02Mounted": item.get("u02Mounted"),
        "podName": item.get("podName"),
        "namespace": item.get("namespace"),
        "observerVersion": item.get("observerVersion"),
        "errorSummary": item.get("errorSummary"),
    }


def severity_rank(status):
    try:
        return SEVERITY_ORDER.index(status)
    except ValueError:
        return SEVERITY_ORDER.index("UNKNOWN")


def group_by_deployment(pipeline_statuses):
    """Group component rows by deploymentId, preserving first-seen order."""
    groups = {}
    order = []
    for row in pipeline_statuses:
        deployment_id = row["deploymentId"]
        if deployment_id not in groups:
            groups[deployment_id] = []
            order.append(deployment_id)
        groups[deployment_id].append(row)

    deployments = []
    for deployment_id in order:
        components = groups[deployment_id]
        overall_status = min(components, key=lambda c: severity_rank(c["effectiveStatus"]))[
            "effectiveStatus"
        ]
        deployments.append(
            {
                "deploymentId": deployment_id,
                "overallStatus": overall_status,
                "components": components,
            }
        )
    return deployments


def build_status_payload(config: MonitorConfig, table, clock=time.time):
    """Read every configured pipeline and return the normalized payload.

    Raises DynamoDbReadError if any configured pipeline's GetItem call fails
    against AWS -- a missing item (no such record yet) is not an error and
    normalizes to effectiveStatus=MISSING instead.
    """
    now = clock()
    pipeline_statuses = []
    for pipeline in config.pipelines:
        try:
            item = get_pipeline_item(table, pipeline)
        except (BotoCoreError, ClientError) as exc:
            summary = sanitize_error(exc)
            log_event("ERROR", "dynamodb_read_failed", pipeline=pipeline, message=summary)
            raise DynamoDbReadError(summary) from exc
        pipeline_statuses.append(
            build_pipeline_status(pipeline, item, now, config.stale_after_seconds)
        )

    return {
        "generatedAt": int(now),
        "staleAfterSeconds": config.stale_after_seconds,
        "deployments": group_by_deployment(pipeline_statuses),
    }


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


def _bool_cell(value):
    if value is True:
        return html.escape("yes")
    if value is False:
        return html.escape("no")
    return html.escape("-")


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

    for deployment in payload.get("deployments", []):
        deployment_id = _esc(deployment.get("deploymentId"))
        overall_badge = _status_badge(deployment.get("overallStatus"))
        rows = []
        for component in deployment.get("components", []):
            age = component.get("ageSeconds")
            age_text = f"{age}s ago" if age is not None else "-"
            rows.append(
                "<tr>"
                f"<td>{_esc(component.get('component'))}</td>"
                f"<td>{_esc(component.get('engine'))}</td>"
                f"<td>{_status_badge(component.get('effectiveStatus'))}</td>"
                f"<td>{_bool_cell(component.get('adminEndpointHealthy'))}</td>"
                f"<td>{_bool_cell(component.get('metricsEndpointHealthy'))}</td>"
                f"<td>{_bool_cell(component.get('u02Mounted'))}</td>"
                f"<td>{_esc(component.get('podName'))}</td>"
                f"<td>{_esc(component.get('namespace'))}</td>"
                f"<td>{_esc(component.get('observerVersion'))}</td>"
                f"<td>{html.escape(age_text)}</td>"
                f"<td>{_esc(component.get('errorSummary'), default='')}</td>"
                "</tr>"
            )
        sections.append(
            f'<h2 style="margin-top:24px;">{deployment_id} {overall_badge}</h2>'
            '<table style="border-collapse:collapse;width:100%;font-size:0.9em;">'
            '<thead><tr style="text-align:left;border-bottom:2px solid #d0d7de;">'
            "<th>Component</th><th>Engine</th><th>Status</th><th>Admin</th>"
            "<th>Metrics</th><th>/u02</th><th>Pod</th><th>Namespace</th>"
            "<th>Observer</th><th>Age</th><th>Error</th>"
            "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )

    if not payload.get("deployments"):
        sections.append("<p>No configured deployments found.</p>")

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
  th, td {{ padding: 6px 10px; border-bottom: 1px solid #eaeef2; }}
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


def _make_handler(config: MonitorConfig, table):
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
            # Process liveness only -- never touches DynamoDB.
            body = json.dumps({"status": "ok", "version": config.monitor_version}).encode("utf-8")
            self._write(200, "application/json", body)

        def _handle_readyz(self):
            try:
                check_dynamodb_ready(table)
            except (BotoCoreError, ClientError) as exc:
                log_event("ERROR", "readyz_check_failed", message=sanitize_error(exc))
                body = json.dumps({"status": "not_ready"}).encode("utf-8")
                self._write(503, "application/json", body)
                return
            body = json.dumps({"status": "ready"}).encode("utf-8")
            self._write(200, "application/json", body)

        def _handle_api_status(self):
            try:
                payload = build_status_payload(config, table)
            except DynamoDbReadError as exc:
                body = json.dumps(
                    {"error": "dynamodb_unavailable", "message": str(exc)}
                ).encode("utf-8")
                self._write(503, "application/json", body)
                return
            body = json.dumps(payload, default=_json_default).encode("utf-8")
            self._write(200, "application/json", body)

        def _handle_root(self):
            try:
                payload = build_status_payload(config, table)
                error_message = None
            except DynamoDbReadError as exc:
                payload = {
                    "generatedAt": int(time.time()),
                    "staleAfterSeconds": config.stale_after_seconds,
                    "deployments": [],
                }
                error_message = str(exc)
            body = render_html(payload, config, error_message=error_message).encode("utf-8")
            self._write(200, "text/html; charset=utf-8", body)

        def log_message(self, fmt, *args):
            # Suppressed default access logging -- structured JSON events
            # (request_failed, etc.) cover observability without leaking
            # request internals to stderr in an unstructured format.
            return

    return Handler


def start_http_server(config: MonitorConfig, table):
    handler_cls = _make_handler(config, table)
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

    table = create_dynamodb_table(config)

    stop_event = threading.Event()

    def _handle_signal(signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    server = start_http_server(config, table)
    log_event(
        "INFO",
        "monitor_started",
        version=config.monitor_version,
        port=config.port,
        pipelineCount=len(config.pipelines),
    )

    stop_event.wait()

    log_event("INFO", "monitor_stopping")
    server.shutdown()
    server.server_close()


if __name__ == "__main__":
    main()
