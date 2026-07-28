#!/usr/bin/env python3
"""gg-monitor-core: shared, passive GoldenGate runtime poller/writer.

Takes over the manager reference implementation's per-pod utility-sidecar
writer responsibilities (LEASE ownership, STATE#_deployment / STATE#<process>
writes, REST/PMS polling, CloudWatch metric publication) as ONE shared,
external Deployment -- because our approved architecture has no utility
sidecar inside GoldenGate runtime pods (see
charts/gg-deployment/files/utility-sidecar.py in the manager reference
repository, inspected read-only, never modified or copied into runtime pods).

The manager's OWN gg-monitor (charts/gg-monitor/files/gg-monitor.py) is a
read-only DynamoDB viewer that holds no GoldenGate credentials and never
polls a runtime -- structurally different from this component. This module
is therefore modeled on the manager's utility-sidecar (for polling/writing
behavior) with every active-healing/fencing/failover execution path removed,
not on the manager's own gg-monitor.

Passive by construction: this file contains no code path that starts,
restarts, stops, or fences a GoldenGate process, calls a Kubernetes mutation
API, or pushes credentials into GoldenGate. See gg_health_rules.py for what
was deliberately left out and why.
"""
from __future__ import annotations

import json
import logging
import os
import secrets as _secrets
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3
from botocore.exceptions import ClientError

import gg_health_rules as gh
from inventory import (
    build_deployments_json,
    build_process_pipeline_map_json,
    load_runtimes,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("gg-monitor-core")

# --- tunables (env-overridable, same names/semantics as the manager's
# utility-sidecar for LEASE fields) ---
LEASE_TTL = int(os.environ.get("LEASE_TTL", "30"))
RENEW_INTERVAL = int(os.environ.get("RENEW_INTERVAL", "5"))
GRACE = 60  # ttl ATTRIBUTE = expiresAt + GRACE (DynamoDB TTL janitor for abandoned leases)

AWS_REGION = os.environ.get("AWS_REGION", "eu-west-1")
DDB_TABLE = os.environ.get("DDB_TABLE", "gg-eks-pipeline")
MONITOR_INSTANCE = os.environ.get("POD_NAME", "gg-monitor")
HTTP_PORT = int(os.environ.get("PORT", "8080"))

# Per-runtime-type admin credential file (mounted by the Secrets Store CSI
# Driver, same jmesPath-derived file-per-alias pattern already proven by the
# Oracle/PostgreSQL runtime SecretProviderClasses -- see
# helm/gg-monitor/templates/secretproviderclass.yaml). Re-read every tick so
# a rotated secret is picked up without a pod restart, exactly like the
# manager's _read_admin_password().
ADMIN_USER_FILE = {
    "oracle": os.environ.get("ORACLE_ADMIN_USER_FILE", "/mnt/secrets-store/oracle-ogg-admin"),
    "postgresql": os.environ.get("POSTGRESQL_ADMIN_USER_FILE", "/mnt/secrets-store/postgresql-ogg-admin"),
}
ADMIN_PASSWORD_FILE = {
    "oracle": os.environ.get("ORACLE_ADMIN_PASSWORD_FILE", "/mnt/secrets-store/oracle-ogg-admin-pwd"),
    "postgresql": os.environ.get("POSTGRESQL_ADMIN_PASSWORD_FILE", "/mnt/secrets-store/postgresql-ogg-admin-pwd"),
}
CA_FILE = os.environ.get("CA_FILE", "/mnt/secrets-store/ca-chain-pem")

CLOUDWATCH_NAMESPACE = "GoldenGate/Pipelines"


def now_epoch():
    return int(time.time())


def _ddb_safe(v):
    """Recursively coerce a value into DynamoDB-resource-safe types (the
    boto3 DynamoDB *resource* rejects float; PMS payloads contain floats)."""
    try:
        if isinstance(v, bool):
            return v
        if isinstance(v, float):
            return Decimal(str(v))
        if isinstance(v, dict):
            return {str(k): _ddb_safe(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [_ddb_safe(x) for x in v]
        return v
    except Exception:
        return str(v)


# ---------------------------------------------------------------------
# LEASE (ported from the manager's utility-sidecar LeaseManager, unchanged
# schema/fields/conditional expressions -- one lease per pipeline, so a
# future multi-replica gg-monitor cannot write conflicting state for the
# same pipeline even though today's replica count is 1).
# ---------------------------------------------------------------------
class LeaseManager:
    def __init__(self, table, pipeline, holder, ttl=LEASE_TTL, clock=now_epoch):
        self.table = table
        self.pipeline = pipeline
        self.holder = holder
        self.ttl = ttl
        self.clock = clock
        self.token = _secrets.token_hex(16)

    def _key(self):
        return {"pipeline": self.pipeline, "recordType": "LEASE"}

    def acquire(self):
        now = self.clock()
        try:
            self.table.update_item(
                Key=self._key(),
                UpdateExpression="SET holder=:h, expiresAt=:e, #ttl=:t, leaseToken=:k",
                ConditionExpression="attribute_not_exists(holder) OR expiresAt < :now",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":h": self.holder,
                    ":e": now + self.ttl,
                    ":t": now + self.ttl + GRACE,
                    ":k": self.token,
                    ":now": now,
                },
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def renew(self):
        now = self.clock()
        try:
            self.table.update_item(
                Key=self._key(),
                UpdateExpression="SET expiresAt=:e, #ttl=:t",
                ConditionExpression="holder = :me AND leaseToken = :tok AND expiresAt >= :now",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":e": now + self.ttl,
                    ":t": now + self.ttl + GRACE,
                    ":me": self.holder,
                    ":tok": self.token,
                    ":now": now,
                },
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise


# ---------------------------------------------------------------------
# TLS. Real network calls (not the sidecar's loopback case): TLS
# verification is always ON. The chain is verified against the shared
# GoldenGate TLS object's CA (dev/goldengate/tls-certificate, ca-chain.pem).
# check_hostname is disabled for the same class of reason the manager's own
# _pms_ssl_context() documents for its loopback call -- but for a genuinely
# different, equally honest reason here: the certificate's CN/SAN is the
# external Ingress hostname (*.goldengate-dev.adcbmis.local), not the
# internal Kubernetes Service DNS name we connect to
# (gg-<name>.goldengate-dev.svc.cluster.local). The chain (and therefore the
# certificate's authenticity) is still fully verified; only the hostname
# comparison is skipped, and only because the two names are legitimately
# different by design, not because verification was disabled outright. This
# is never a verify=false / CERT_NONE fallback -- if the CA file is missing,
# building the context raises instead of silently downgrading to unverified.
# ---------------------------------------------------------------------
_SSL_CTX = None


def _build_ssl_context(ca_file=CA_FILE):
    global _SSL_CTX
    if _SSL_CTX is not None:
        return _SSL_CTX
    if not ca_file or not os.path.exists(ca_file):
        raise RuntimeError(
            f"CA_FILE {ca_file!r} not found -- refusing to poll GoldenGate runtimes "
            "without TLS chain verification (never falls back to unverified)."
        )
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(ca_file)
    ctx.check_hostname = False  # CN is the external Ingress host, not internal Service DNS
    _SSL_CTX = ctx
    return ctx


def _read_secret_file(path):
    """Re-read each cycle so a refreshed/rotated secret (CSI Driver
    auto-rotation) is picked up without a pod restart. Never logs the
    content; a read failure degrades to an empty string, never raises into a
    log line that might otherwise include a path+errno detail an attacker
    could use, and definitely never the value."""
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _basic_opener(user, pwd, base, ssl_ctx):
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, base, user, pwd)
    handler = urllib.request.HTTPBasicAuthHandler(mgr)
    https_handler = urllib.request.HTTPSHandler(context=ssl_ctx)
    return urllib.request.build_opener(handler, https_handler)


def _http_json(url, opener, timeout=5):
    with opener.open(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _http_status(url, opener, timeout=5):
    try:
        with opener.open(url, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


# ---------------------------------------------------------------------
# REST/PMS polling (ported verbatim from the manager's utility-sidecar
# fetch_gg_processes -- same endpoints, same parsing, same tolerant
# per-process error handling). base = the runtime's internal Kubernetes
# Service DNS admin endpoint, never the external Ingress URL.
# ---------------------------------------------------------------------
def fetch_gg_processes(base, opener):
    _http_json(f"{base}/services/v2/deployments", opener)  # liveness probe
    procs = []
    for kind, ptype in (("extracts", "extract"), ("replicats", "replicat")):
        try:
            items = _http_json(f"{base}/services/v2/{kind}", opener).get(
                "response", {}).get("items", [])
        except Exception as e:
            logger.warning("listing %s failed: %s", kind, e)
            items = []
        for it in items:
            name = str(it.get("name") or it.get("$id") or "unknown")
            detail = {}
            try:
                detail = _http_json(f"{base}/services/v2/{kind}/{name}", opener).get(
                    "response", {})
            except Exception as e:
                logger.warning("detail %s/%s failed: %s", kind, name, e)
            status = str(detail.get("status", it.get("status", "")) or "UNKNOWN").upper()
            lag = detail.get("lag", detail.get("lagSeconds", 0)) or 0
            try:
                lag = float(lag)
            except (TypeError, ValueError):
                lag = 0.0
            err = str(detail.get("lastError") or detail.get("error")
                      or detail.get("message") or "") if status == "ABENDED" else ""
            procs.append({"process": name, "type": ptype, "lagSeconds": lag,
                          "abended": status == "ABENDED", "status": status,
                          "metrics": detail or {}, "error": err})
    try:
        items = _http_json(f"{base}/services/v2/sources", opener).get(
            "response", {}).get("items", [])
        for it in items:
            name = str(it.get("name") or "unknown")
            status = str(it.get("status", "") or "UNKNOWN").upper()
            bytes_now = next((it.get(k) for k in gh.BYTES_KEYS if it.get(k) is not None), None)
            procs.append({"process": name, "type": "distpath", "lagSeconds": 0.0,
                          "abended": status == "ABENDED", "status": status,
                          "bytes": bytes_now, "metrics": it or {},
                          "error": "" if status != "ABENDED" else str(it.get("lastError") or "")})
    except Exception as e:
        logger.debug("dispatch sources scrape skipped: %s", e)
    return procs


_SVC_PROBE_PATH = {"adminsrvr": "extracts", "distsrvr": "sources", "recvsrvr": "targets"}


def probe_critical_services(base, opener, critical):
    out = {}
    for svc in critical:
        path = _SVC_PROBE_PATH.get(svc)
        if not path:
            continue
        code = _http_status(f"{base}/services/v2/{path}", opener)
        out[svc] = gh.classify_service_up(code)
    return out


def build_metric_data(deployment, deployment_type, parsed):
    """CloudWatch PutMetricData entries -- same namespace/names/dimensions as
    the manager's build_metric_data(). Dimension set {Deployment,
    DeploymentType[, Process]} is preserved exactly."""
    lag_metric_by_type = {"extract": "ExtractLagSeconds", "replicat": "ReplicatLagSeconds"}
    md = []
    for p in parsed:
        dims = [
            {"Name": "Deployment", "Value": deployment},
            {"Name": "DeploymentType", "Value": deployment_type},
            {"Name": "Process", "Value": p["process"]},
        ]
        lag_metric = lag_metric_by_type.get(p["type"])
        if lag_metric:
            md.append({"MetricName": lag_metric, "Dimensions": dims,
                       "Value": p["lagSeconds"], "Unit": "Seconds"})
        else:
            logger.warning("Unknown GG process type %r for %s; emitting AbendState only",
                           p["type"], p["process"])
        md.append({"MetricName": "AbendState", "Dimensions": dims,
                   "Value": 1.0 if p["abended"] else 0.0, "Unit": "Count"})
    return md


def _emit(cw, deployment, deployment_type, flags, extra_md=None):
    """Publish per-deployment aggregate flags + raw per-process metrics.

    Deliberately EXCLUDES HeartbeatAgeSeconds: that metric is derived from a
    sidecar-local heartbeat file inside the GoldenGate pod (see the
    manager's lease_loop/_write_heartbeat) -- a remote, external poller has
    no equivalent local liveness signal to measure, so this is a structural
    adaptation, not an omission of a metric we could otherwise produce.
    """
    md = [{"MetricName": n,
           "Dimensions": [{"Name": "Deployment", "Value": deployment},
                          {"Name": "DeploymentType", "Value": deployment_type}],
           "Value": float(v), "Unit": u}
          for n, v, u in (("LagBreached", flags["lag"], "Count"),
                          ("AbendFailure", flags["abend"], "Count"),
                          ("DeploymentDown", flags["down"], "Count"))]
    md += (extra_md or [])
    if cw and md:
        for i in range(0, len(md), 20):  # PutMetricData max 20/call
            try:
                cw.put_metric_data(Namespace=CLOUDWATCH_NAMESPACE, MetricData=md[i:i + 20])
            except Exception:
                logger.exception("CloudWatch put_metric_data failed; continuing")


# ---------------------------------------------------------------------
# STATE writes (ported from the manager's write_process_state -- same
# field names/types, same fenced-by-lease-renewal semantics). Owns
# recordType=STATE#_deployment and recordType=STATE#<process> exclusively;
# never recordType=STATE (the manager's own inconsistent legacy read is not
# reproduced -- see gg-monitor.py's collect_status() in the manager repo,
# read-only, not used as a pattern here).
# ---------------------------------------------------------------------
class _FencedOff(Exception):
    pass


def write_process_state(table, mgr, pipeline, deployment_type, process, snapshot, is_leader_fn, counters=None):
    if not is_leader_fn():
        return False
    if not mgr.renew():
        logger.warning("state write fenced off for %s/%s (lease lost)", pipeline, process)
        return False
    names = {"#st": "status"}
    sets, vals = ["#st=:st", "recordedAt=:ra", "deploymentType=:dt"], {
        ":st": str(snapshot.get("status", "UNKNOWN")),
        ":ra": int(snapshot.get("recordedAt", now_epoch())),
        ":dt": deployment_type,
    }
    for key in ("processType", "lagSeconds", "lastTransitionAt",
                "resolvedThreshold", "resolvedMode", "pipelineName", "errorMsg"):
        if key in snapshot:
            sets.append(f"{key}=:{key}")
            v = snapshot[key]
            vals[f":{key}"] = int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)
    if "performanceMetrics" in snapshot:
        sets.append("performanceMetrics=:pm")
        vals[":pm"] = _ddb_safe(snapshot.get("performanceMetrics") or {})
    if "criticalServices" in snapshot:
        sets.append("criticalServices=:cs")
        vals[":cs"] = _ddb_safe(snapshot.get("criticalServices") or {})
    if counters is not None:
        for key in ("consecutiveAbends", "lastAbendAt", "nextRecheckAt"):
            sets.append(f"{key}=:{key}")
            vals[f":{key}"] = int(counters.get(key, 0))
    table.update_item(
        Key={"pipeline": pipeline, "recordType": f"STATE#{process}"},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=vals,
    )
    return True


def read_process_state(table, pipeline, process):
    resp = table.get_item(Key={"pipeline": pipeline, "recordType": f"STATE#{process}"})
    return resp.get("Item", {})


def read_config(table, pipeline):
    resp = table.get_item(Key={"pipeline": pipeline, "recordType": "CONFIG"})
    return resp.get("Item", {})


# ---------------------------------------------------------------------
# Per-pipeline poll/lease/write tick loop. One thread per ENABLED runtime
# (mirrors the manager's per-deployment sidecar isolation -- one pipeline's
# slowness/failure never blocks another -- consolidated into this single
# shared process because we have no per-pod sidecar). Each thread gets its
# own boto3 Table resource: boto3 Table objects are not thread-safe across
# concurrent update_item calls (same lesson as the manager's utility-sidecar
# main(), which gives health_thread its own resource for the same reason).
# ---------------------------------------------------------------------
def pipeline_thread(runtime, stop_event, ready_state):
    pipeline = runtime["pipeline"]
    deployment_type = runtime["type"]
    table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(DDB_TABLE)
    cw = boto3.client("cloudwatch", region_name=AWS_REGION)
    mgr = LeaseManager(table, pipeline, MONITOR_INSTANCE)
    is_leader = {"value": False}
    started = now_epoch()
    last_dep_status = None
    distpath_mem = {}

    endpoints = runtime.get("endpoints") or {}
    admin_ep = endpoints.get("admin") or {}
    base = None
    if admin_ep.get("host") and admin_ep.get("port"):
        base = f"{admin_ep.get('scheme', 'https')}://{admin_ep['host']}:{admin_ep['port']}"

    admin_user_file = ADMIN_USER_FILE.get(deployment_type)
    admin_password_file = ADMIN_PASSWORD_FILE.get(deployment_type)

    def _guarded_write(proc, snap, counters=None):
        ok = write_process_state(table, mgr, pipeline, deployment_type, proc, snap,
                                 lambda: is_leader["value"], counters=counters)
        if not ok:
            logger.warning("tick fenced off for %s/%s; aborting tick", pipeline, proc)
            raise _FencedOff()

    logger.info("pipeline thread started for %s (type=%s, base=%s)", pipeline, deployment_type, base)

    while not stop_event.is_set():
        interval = gh.DEFAULTS["checkIntervalSeconds"]
        try:
            cfg = gh.resolve_config(read_config(table, pipeline))
            interval = cfg["checkIntervalSeconds"]

            ok = mgr.renew() if is_leader["value"] else mgr.acquire()
            if ok:
                if not is_leader["value"]:
                    logger.info("Acquired lease for %s; this instance is leader.", pipeline)
                is_leader["value"] = True
            else:
                if is_leader["value"]:
                    logger.warning("Lost lease for %s; demoting to standby.", pipeline)
                is_leader["value"] = False

            ready_state[pipeline] = True

            if not is_leader["value"] or base is None:
                stop_event.wait(interval)
                continue

            flags = {"lag": 0, "abend": 0, "down": 0}
            extra_md = []

            if not admin_user_file or not admin_password_file:
                logger.error("no admin credential files configured for deployment type %r", deployment_type)
                stop_event.wait(interval)
                continue

            user = _read_secret_file(admin_user_file) or "oggadmin"
            pwd = _read_secret_file(admin_password_file)
            try:
                ssl_ctx = _build_ssl_context()
            except RuntimeError:
                logger.exception("TLS context unavailable; skipping this tick")
                stop_event.wait(interval)
                continue
            opener = _basic_opener(user, pwd, base, ssl_ctx)

            try:
                procs = fetch_gg_processes(base, opener)
            except Exception as e:
                in_grace = (now_epoch() - started) < cfg["startupGraceSeconds"]
                status = "STARTING" if in_grace else "DEPLOYMENT_DOWN"
                if not in_grace and cfg["alertsEnabled"]:
                    flags["down"] = 1
                transitioned = (status != last_dep_status)
                dep_snap = {"processType": "deployment", "status": status, "recordedAt": now_epoch()}
                if transitioned:
                    dep_snap["lastTransitionAt"] = now_epoch()
                _guarded_write("_deployment", dep_snap)
                last_dep_status = status
                logger.warning("GG API unreachable for %s (%s): %s", pipeline, status, e)
                _emit(cw, pipeline, deployment_type, flags)
                stop_event.wait(interval)
                continue

            source_active = any(p["type"] == "extract" and p["status"] == "RUNNING" for p in procs)
            pipe_map = {}  # process-pipeline routing: empty topology today, see inventory.py

            for p in procs:
                name, ptype, status = p["process"], p["type"], p["status"]
                rule = gh.rule_for_process(cfg, name)
                mode, thr = gh.lag_rule_now(cfg, name)
                prev = read_process_state(table, pipeline, name)
                counters, act = gh.abend_step(status=status, state=prev, now=now_epoch(),
                                              rule=rule, alerts_enabled=cfg["alertsEnabled"])
                if ptype == "distpath":
                    if status == "RUNNING":
                        distpath_mem[name], stalled = gh.distpath_step(
                            distpath_mem.get(name, {}), p.get("bytes"), source_active,
                            rule["distpathStallChecks"])
                        if stalled and cfg["alertsEnabled"] and mode != "skip":
                            flags["lag"] = 1
                    else:
                        distpath_mem.pop(name, None)
                elif status == "RUNNING" and cfg["alertsEnabled"] and gh.lag_breached(cfg, name, p["lagSeconds"]):
                    flags["lag"] = 1
                if act["abend_failure"]:
                    flags["abend"] = 1
                # act["failover"] is computed for schema fidelity only -- never
                # acted on. No restart/exit/mutation path exists in this
                # application for any value of this flag.
                if act["abend_event"]:
                    extra_md.append({"MetricName": "AbendEvent",
                                     "Dimensions": [{"Name": "Deployment", "Value": pipeline},
                                                    {"Name": "DeploymentType", "Value": deployment_type},
                                                    {"Name": "Process", "Value": name}],
                                     "Value": 1.0, "Unit": "Count"})
                snap = {"processType": ptype, "status": status,
                        "lagSeconds": int(p["lagSeconds"]), "recordedAt": now_epoch(),
                        "resolvedThreshold": thr, "resolvedMode": mode,
                        "pipelineName": pipe_map.get(name.upper(), ""),
                        "errorMsg": str(p.get("error", "")),
                        "performanceMetrics": p.get("metrics") or {}}
                if str(prev.get("status")) != status:
                    snap["lastTransitionAt"] = now_epoch()
                try:
                    _guarded_write(name, snap, counters=counters)
                except _FencedOff:
                    raise
                except Exception:
                    logger.exception("process %s evaluation failed; skipped", name)

            critical = gh.CRITICAL_SERVICES_BY_TYPE.get(deployment_type, [])
            if critical:
                svc_up = probe_critical_services(base, opener, critical)
                cs_state = (read_process_state(table, pipeline, "_deployment").get("criticalServices") or {})
                cs_new = {}
                for svc, up in svc_up.items():
                    cs_new[svc] = {"reachable": bool(up)}
                    extra_md.append({"MetricName": "CriticalServiceDown",
                                     "Dimensions": [{"Name": "Deployment", "Value": pipeline},
                                                    {"Name": "DeploymentType", "Value": deployment_type},
                                                    {"Name": "Service", "Value": svc}],
                                     "Value": 0.0 if up else 1.0, "Unit": "Count"})
                _ = cs_state  # no healing decision derived from prior state; observation only
            else:
                cs_new = {}

            transitioned = ("UP" != last_dep_status)
            dep_snap = {"processType": "deployment", "status": "UP",
                        "recordedAt": now_epoch(), "criticalServices": cs_new}
            if transitioned:
                dep_snap["lastTransitionAt"] = now_epoch()
            _guarded_write("_deployment", dep_snap)
            last_dep_status = "UP"

            extra_md += build_metric_data(pipeline, deployment_type, procs)
            _emit(cw, pipeline, deployment_type, flags, extra_md)

        except _FencedOff:
            pass
        except Exception:
            logger.exception("tick failed for %s; continuing next interval", pipeline)
        stop_event.wait(interval)


# ---------------------------------------------------------------------
# HTTP health endpoints (k8s probes only -- no status API/portal here; the
# existing read-only gg-monitor portal, helm/goldengate-monitor, continues
# to serve that role unchanged).
# ---------------------------------------------------------------------
def _make_handler(ready_state, expected_pipelines):
    class Handler(BaseHTTPRequestHandler):
        server_version = "gg-monitor-core"

        def _write(self, code, body):
            body_bytes = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)

        def do_GET(self):  # noqa: N802
            if self.path == "/healthz":
                self._write(200, json.dumps({"status": "ok"}))
            elif self.path == "/readyz":
                ready = all(ready_state.get(p) for p in expected_pipelines)
                self._write(200 if ready else 503, json.dumps({"status": "ready" if ready else "not_ready"}))
            else:
                self._write(404, json.dumps({"error": "not found"}))

        def log_message(self, fmt, *args):
            return

    return Handler


def start_http_server(ready_state, expected_pipelines):
    handler_cls = _make_handler(ready_state, expected_pipelines)
    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main():
    runtimes = load_runtimes()
    enabled = [r for r in runtimes if r["enabled"]]
    logger.info("loaded %d runtime(s), %d enabled: %s", len(runtimes), len(enabled),
               [r["pipeline"] for r in enabled])
    logger.info("manager-compatible deployments.json equivalent: %s", build_deployments_json(runtimes))
    logger.info("manager-compatible process-pipeline-map.json equivalent: %s",
               build_process_pipeline_map_json(runtimes))

    stop_event = threading.Event()
    ready_state = {}

    server = start_http_server(ready_state, [r["pipeline"] for r in enabled])

    threads = []
    for runtime in enabled:
        t = threading.Thread(target=pipeline_thread, args=(runtime, stop_event, ready_state), daemon=True)
        t.start()
        threads.append(t)

    try:
        while not stop_event.is_set():
            stop_event.wait(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
