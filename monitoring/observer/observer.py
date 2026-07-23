"""GoldenGate monitoring observer.

Passive, fail-open sidecar: reports component health to DynamoDB and
CloudWatch. Never touches GoldenGate, /u02, /u03, or process control.
"""

import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

import boto3
from botocore.config import Config as BotoConfig

REQUIRED_ENV_VARS = (
    "AWS_REGION",
    "DYNAMODB_TABLE",
    "PIPELINE",
    "DEPLOYMENT_ID",
    "COMPONENT",
    "ENGINE",
    "POD_NAME",
    "POD_NAMESPACE",
)

ALLOWED_COMPONENTS = ("source", "target")

DEFAULTS = {
    "ADMIN_HOST": "127.0.0.1",
    "ADMIN_PORT": "8443",
    "METRICS_HOST": "127.0.0.1",
    "METRICS_PORT": "9015",
    "U02_PATH": "/u02",
    "CHECK_INTERVAL_SECONDS": "30",
    "CONNECT_TIMEOUT_SECONDS": "3",
    "HEALTH_LISTEN_HOST": "0.0.0.0",
    "HEALTH_LISTEN_PORT": "8080",
    "CLOUDWATCH_NAMESPACE": "GoldenGate/Pipelines",
    "OBSERVER_VERSION": "development",
}

STATE_RECORD_TYPE = "STATE#_deployment"

logger = logging.getLogger("goldengate.observer")
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(_handler)
logger.propagate = False


class ConfigError(Exception):
    """Raised when required observer configuration is missing or invalid."""


@dataclass(frozen=True)
class ObserverConfig:
    aws_region: str
    dynamodb_table: str
    pipeline: str
    deployment_id: str
    component: str
    engine: str
    pod_name: str
    pod_namespace: str
    admin_host: str
    admin_port: int
    metrics_host: str
    metrics_port: int
    u02_path: str
    check_interval_seconds: int
    connect_timeout_seconds: int
    health_listen_host: str
    health_listen_port: int
    cloudwatch_namespace: str
    observer_version: str


def _get_int(env, name, default):
    raw = env.get(name, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None


def load_config(env) -> ObserverConfig:
    missing = sorted(name for name in REQUIRED_ENV_VARS if not env.get(name))
    if missing:
        raise ConfigError(
            "Missing required environment variables: " + ", ".join(missing)
        )

    component = env["COMPONENT"]
    if component not in ALLOWED_COMPONENTS:
        raise ConfigError(
            f"COMPONENT must be one of {ALLOWED_COMPONENTS}, got {component!r}"
        )

    def opt(name):
        return env.get(name, DEFAULTS[name])

    return ObserverConfig(
        aws_region=env["AWS_REGION"],
        dynamodb_table=env["DYNAMODB_TABLE"],
        pipeline=env["PIPELINE"],
        deployment_id=env["DEPLOYMENT_ID"],
        component=component,
        engine=env["ENGINE"],
        pod_name=env["POD_NAME"],
        pod_namespace=env["POD_NAMESPACE"],
        admin_host=opt("ADMIN_HOST"),
        admin_port=_get_int(env, "ADMIN_PORT", DEFAULTS["ADMIN_PORT"]),
        metrics_host=opt("METRICS_HOST"),
        metrics_port=_get_int(env, "METRICS_PORT", DEFAULTS["METRICS_PORT"]),
        u02_path=opt("U02_PATH"),
        check_interval_seconds=_get_int(
            env, "CHECK_INTERVAL_SECONDS", DEFAULTS["CHECK_INTERVAL_SECONDS"]
        ),
        connect_timeout_seconds=_get_int(
            env, "CONNECT_TIMEOUT_SECONDS", DEFAULTS["CONNECT_TIMEOUT_SECONDS"]
        ),
        health_listen_host=opt("HEALTH_LISTEN_HOST"),
        health_listen_port=_get_int(
            env, "HEALTH_LISTEN_PORT", DEFAULTS["HEALTH_LISTEN_PORT"]
        ),
        cloudwatch_namespace=opt("CLOUDWATCH_NAMESPACE"),
        observer_version=opt("OBSERVER_VERSION"),
    )


# ---------------------------------------------------------------------------
# Passive health checks (pure / injectable, no AWS involved)
# ---------------------------------------------------------------------------


def tcp_check(host, port, timeout, connector=socket.create_connection) -> bool:
    """Open (and immediately close) a TCP connection. No payload is sent."""
    try:
        conn = connector((host, port), timeout=timeout)
    except OSError:
        return False
    try:
        conn.close()
    except OSError:
        pass
    return True


def get_u02_stats(path, statvfs_func=os.statvfs, isdir_func=os.path.isdir):
    """Return {totalBytes, freeBytes, usedPercent} for path, or None.

    Read-only: statvfs only, no file is created/chmod/chowned.
    """
    if not isdir_func(path):
        return None
    try:
        st = statvfs_func(path)
    except OSError:
        return None

    total = st.f_frsize * st.f_blocks
    free = st.f_frsize * st.f_bavail

    used_percent = None
    if total > 0:
        used = total - free
        used_percent = (Decimal(used) / Decimal(total) * Decimal(100)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    return {
        "totalBytes": int(total),
        "freeBytes": int(free),
        "usedPercent": used_percent,
    }


def compute_status(admin_ok, metrics_ok, u02_available) -> str:
    if admin_ok and metrics_ok and u02_available:
        return "HEALTHY"
    if not admin_ok and not metrics_ok:
        return "DOWN"
    return "DEGRADED"


def build_error_summary(admin_ok, metrics_ok, u02_stats) -> Optional[str]:
    reasons = []
    if not admin_ok:
        reasons.append("admin_endpoint_unreachable")
    if not metrics_ok:
        reasons.append("metrics_endpoint_unreachable")
    if u02_stats is None:
        reasons.append("u02_unavailable")
    return "; ".join(reasons) if reasons else None


def sanitize_error(exc: BaseException, max_len: int = 200) -> str:
    """Concise, sanitized error summary -- no stack trace, no credentials."""
    message = f"{type(exc).__name__}: {exc}".replace("\n", " ").replace("\r", " ")
    return message[:max_len]


# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------


def log_event(level, event, *, config: ObserverConfig, status=None, message=None):
    record = {
        "timestamp": time.time(),
        "level": level,
        "event": event,
        "pipeline": config.pipeline,
        "deploymentId": config.deployment_id,
        "component": config.component,
        "engine": config.engine,
        "status": status,
        "message": message,
    }
    line = json.dumps(record, default=str)
    if level == "ERROR":
        logger.error(line)
    else:
        logger.info(line)


# ---------------------------------------------------------------------------
# AWS integration (DynamoDB state write, CloudWatch metrics)
# ---------------------------------------------------------------------------


def _boto_config(config: ObserverConfig) -> BotoConfig:
    return BotoConfig(
        connect_timeout=config.connect_timeout_seconds,
        read_timeout=config.connect_timeout_seconds + 2,
        retries={"max_attempts": 2, "mode": "standard"},
    )


def create_dynamodb_table(config: ObserverConfig):
    session = boto3.session.Session()
    resource = session.resource(
        "dynamodb", region_name=config.aws_region, config=_boto_config(config)
    )
    return resource.Table(config.dynamodb_table)


def create_cloudwatch_client(config: ObserverConfig):
    session = boto3.session.Session()
    return session.client(
        "cloudwatch", region_name=config.aws_region, config=_boto_config(config)
    )


def build_dynamodb_item(
    *, config: ObserverConfig, status, admin_ok, metrics_ok, u02_stats,
    error_summary, recorded_at,
):
    item = {
        "pipeline": config.pipeline,
        "recordType": STATE_RECORD_TYPE,
        "deploymentId": config.deployment_id,
        "component": config.component,
        "engine": config.engine,
        "podName": config.pod_name,
        "namespace": config.pod_namespace,
        "status": status,
        "adminEndpointHealthy": bool(admin_ok),
        "metricsEndpointHealthy": bool(metrics_ok),
        "u02Mounted": u02_stats is not None,
        "recordedAt": int(recorded_at),
        "observerVersion": config.observer_version,
        "errorSummary": error_summary,
    }
    if u02_stats is not None:
        item["u02TotalBytes"] = u02_stats["totalBytes"]
        item["u02FreeBytes"] = u02_stats["freeBytes"]
        item["u02UsedPercent"] = u02_stats["usedPercent"]
    return item


def update_dynamodb_state(table, item):
    """Persist exactly one STATE#_deployment record. UpdateItem only."""
    key = {"pipeline": item["pipeline"], "recordType": item["recordType"]}
    attrs = {k: v for k, v in item.items() if k not in ("pipeline", "recordType")}

    names = {f"#{k}": k for k in attrs}
    values = {f":{k}": v for k, v in attrs.items()}
    expression = "SET " + ", ".join(f"#{k} = :{k}" for k in attrs)

    table.update_item(
        Key=key,
        UpdateExpression=expression,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def build_cloudwatch_metric_data(
    *, config: ObserverConfig, status, admin_ok, metrics_ok, u02_stats, timestamp
):
    dimensions = [
        {"Name": "Pipeline", "Value": config.pipeline},
        {"Name": "Component", "Value": config.component},
        {"Name": "Engine", "Value": config.engine},
    ]

    metric_data = [
        {
            "MetricName": "ObserverHeartbeat",
            "Dimensions": dimensions,
            "Timestamp": timestamp,
            "Value": 1,
            "Unit": "Count",
        },
        {
            "MetricName": "DeploymentHealthy",
            "Dimensions": dimensions,
            "Timestamp": timestamp,
            "Value": 1 if status == "HEALTHY" else 0,
            "Unit": "Count",
        },
        {
            "MetricName": "AdminEndpointHealthy",
            "Dimensions": dimensions,
            "Timestamp": timestamp,
            "Value": 1 if admin_ok else 0,
            "Unit": "Count",
        },
        {
            "MetricName": "MetricsEndpointHealthy",
            "Dimensions": dimensions,
            "Timestamp": timestamp,
            "Value": 1 if metrics_ok else 0,
            "Unit": "Count",
        },
        {
            "MetricName": "U02Mounted",
            "Dimensions": dimensions,
            "Timestamp": timestamp,
            "Value": 1 if u02_stats is not None else 0,
            "Unit": "Count",
        },
    ]

    if u02_stats is not None and u02_stats.get("usedPercent") is not None:
        metric_data.append(
            {
                "MetricName": "U02UsedPercent",
                "Dimensions": dimensions,
                "Timestamp": timestamp,
                "Value": float(u02_stats["usedPercent"]),
                "Unit": "Percent",
            }
        )

    return metric_data


def publish_cloudwatch_metrics(cw_client, config: ObserverConfig, metric_data):
    cw_client.put_metric_data(
        Namespace=config.cloudwatch_namespace, MetricData=metric_data
    )
    return [m["MetricName"] for m in metric_data]


# ---------------------------------------------------------------------------
# Self-health HTTP server (GET /healthz only)
# ---------------------------------------------------------------------------


class HealthState:
    def __init__(self, observer_version: str):
        self._lock = threading.Lock()
        self._observer_version = observer_version
        self._started = False
        self._last_cycle_at = None

    def mark_started(self):
        with self._lock:
            self._started = True

    def mark_cycle(self, timestamp):
        with self._lock:
            self._last_cycle_at = timestamp

    def snapshot(self):
        with self._lock:
            return {
                "status": "ok" if self._started else "starting",
                "observerVersion": self._observer_version,
                "lastCycleAt": self._last_cycle_at,
            }


def _make_health_handler(state: HealthState):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib naming convention)
            if self.path != "/healthz":
                self.send_response(404)
                self.end_headers()
                return

            payload = json.dumps(state.snapshot()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt, *args):
            return  # structured JSON logging covers observability instead

    return Handler


def start_health_server(config: ObserverConfig, state: HealthState):
    handler_cls = _make_health_handler(state)
    server = ThreadingHTTPServer(
        (config.health_listen_host, config.health_listen_port), handler_cls
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ---------------------------------------------------------------------------
# Observation cycle and main loop
# ---------------------------------------------------------------------------


def run_cycle(
    config: ObserverConfig,
    table,
    cw_client,
    *,
    tcp_check_fn=tcp_check,
    u02_stats_fn=get_u02_stats,
    clock=time.time,
) -> str:
    admin_ok = tcp_check_fn(config.admin_host, config.admin_port, config.connect_timeout_seconds)
    metrics_ok = tcp_check_fn(config.metrics_host, config.metrics_port, config.connect_timeout_seconds)
    u02_stats = u02_stats_fn(config.u02_path)

    status = compute_status(admin_ok, metrics_ok, u02_stats is not None)
    error_summary = build_error_summary(admin_ok, metrics_ok, u02_stats)
    now = clock()

    item = build_dynamodb_item(
        config=config,
        status=status,
        admin_ok=admin_ok,
        metrics_ok=metrics_ok,
        u02_stats=u02_stats,
        error_summary=error_summary,
        recorded_at=now,
    )

    try:
        update_dynamodb_state(table, item)
        log_event(
            "INFO", "dynamodb_update_succeeded", config=config, status=status,
            message=f"pipeline={config.pipeline} recordType={STATE_RECORD_TYPE}",
        )
    except Exception as exc:  # noqa: BLE001 -- fail-open: never propagate AWS errors
        log_event(
            "ERROR", "dynamodb_update_failed", config=config, status=status,
            message=sanitize_error(exc),
        )

    # CloudWatch is attempted independently: a DynamoDB failure above must
    # not prevent this attempt, and this attempt must not affect DynamoDB.
    try:
        metric_data = build_cloudwatch_metric_data(
            config=config, status=status, admin_ok=admin_ok, metrics_ok=metrics_ok,
            u02_stats=u02_stats, timestamp=now,
        )
        published = publish_cloudwatch_metrics(cw_client, config, metric_data)
        log_event(
            "INFO", "cloudwatch_publish_succeeded", config=config, status=status,
            message="published metrics: " + ", ".join(published),
        )
    except Exception as exc:  # noqa: BLE001 -- fail-open: never propagate AWS errors
        log_event(
            "ERROR", "cloudwatch_publish_failed", config=config, status=status,
            message=sanitize_error(exc),
        )

    log_event("INFO", "observation_completed", config=config, status=status)
    return status


def main():
    try:
        config = load_config(os.environ)
    except ConfigError as exc:
        # No AWS clients exist yet; config is not yet available to log_event
        # either, so this is the one place we log a plain (non-JSON) line.
        print(json.dumps({
            "timestamp": time.time(),
            "level": "ERROR",
            "event": "configuration_invalid",
            "message": str(exc),
        }))
        sys.exit(1)

    state = HealthState(config.observer_version)
    stop_event = threading.Event()

    def _handle_signal(signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    health_server = start_health_server(config, state)
    table = create_dynamodb_table(config)
    cw_client = create_cloudwatch_client(config)

    log_event("INFO", "observer_started", config=config)
    state.mark_started()

    while not stop_event.is_set():
        try:
            status = run_cycle(config, table, cw_client)
            state.mark_cycle(time.time())
        except Exception as exc:  # noqa: BLE001 -- the loop itself must never die
            log_event("ERROR", "observation_completed", config=config, message=sanitize_error(exc))
        stop_event.wait(config.check_interval_seconds)

    log_event("INFO", "observer_stopping", config=config)
    health_server.shutdown()
    health_server.server_close()


if __name__ == "__main__":
    main()
