"""collector.py: passive GoldenGate Admin REST poller and DynamoDB writer.

Owns LEASE acquisition/renewal and recordType=STATE#_deployment /
STATE#<process> writes -- one lease per deployment, renewed on its own
cadence independent of the poll interval. Never restarts, stops, or fences
a GoldenGate process, and never calls a Kubernetes API.
"""
from __future__ import annotations

import functools
import http.client
import json
import logging
import os
import secrets as _secrets
import socket
import ssl
import threading
import urllib.error
import urllib.request

import boto3
from botocore.exceptions import ClientError

import config as cfgmod
import health_rules as gh

logger = logging.getLogger("goldengate.monitor.collector")

LEASE_TTL = int(os.environ.get("LEASE_TTL", "30"))
RENEW_INTERVAL = int(os.environ.get("RENEW_INTERVAL", "5"))
GRACE = 60  # ttl attribute = expiresAt + GRACE (DynamoDB TTL janitor for abandoned leases)
POLL_SLEEP_GRANULARITY = min(RENEW_INTERVAL, 5)

CA_FILE = os.environ.get("CA_FILE", "/mnt/secrets-store/ca-chain-pem")
CLOUDWATCH_NAMESPACE = "GoldenGate/Pipelines"


def _parse_strict_bool_env(raw):
    if raw is None:
        return False
    return str(raw).strip().lower() in ("true", "1", "yes")


# Hard CloudWatch kill switch, independent of CONFIG.metricsEnabled: CONFIG
# is Terraform-owned and protected by lifecycle.ignore_changes, so an
# already-applied item could carry metricsEnabled=true forever. Publishing
# requires BOTH this env var AND CONFIG.metricsEnabled.
CLOUDWATCH_PUBLISH_ENABLED = _parse_strict_bool_env(os.environ.get("CLOUDWATCH_PUBLISH_ENABLED"))


def cloudwatch_enabled_for(cfg):
    return CLOUDWATCH_PUBLISH_ENABLED and bool(cfg.get("metricsEnabled", False))


def _ddb_safe(v):
    """Coerce a value into DynamoDB-resource-safe types (float -> Decimal)."""
    from decimal import Decimal
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


class LeaseManager:
    """One lease per deployment (pipeline = canonical DynamoDB key)."""

    def __init__(self, table, pipeline, holder, ttl=LEASE_TTL, clock=cfgmod.now_epoch):
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
                    ":h": self.holder, ":e": now + self.ttl, ":t": now + self.ttl + GRACE,
                    ":k": self.token, ":now": now,
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
                    ":e": now + self.ttl, ":t": now + self.ttl + GRACE,
                    ":me": self.holder, ":tok": self.token, ":now": now,
                },
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise


class LeaseState:
    """Thread-safe leader/readiness state shared between one deployment's
    lease-control loop and its polling loop (two independent threads).
    credentials_ok is set by the polling loop itself when the admin
    username/password file is missing or empty -- combined with is_ready()
    (lease-API health) wherever overall readiness is reported."""

    def __init__(self):
        self._lock = threading.Lock()
        self._is_leader = False
        self._ready = False
        self._credentials_ok = True

    def set_leader(self, value):
        with self._lock:
            self._is_leader = value

    def is_leader(self):
        with self._lock:
            return self._is_leader

    def set_ready(self, value=True):
        with self._lock:
            self._ready = value

    def is_ready(self):
        with self._lock:
            return self._ready

    def set_credentials_ok(self, value):
        with self._lock:
            self._credentials_ok = value

    def credentials_ok(self):
        with self._lock:
            return self._credentials_ok


def lease_control_loop(mgr, state, stop_event, renew_interval=RENEW_INTERVAL):
    """Acquire/renew on its own cadence, independent of the poll interval."""
    while not stop_event.is_set():
        try:
            ok = mgr.renew() if state.is_leader() else mgr.acquire()
            if ok:
                if not state.is_leader():
                    logger.info("Acquired lease for %s; this instance is leader.", mgr.pipeline)
                state.set_leader(True)
            else:
                if state.is_leader():
                    logger.warning("Lost lease for %s; demoting to standby.", mgr.pipeline)
                state.set_leader(False)
            state.set_ready(True)
        except Exception:
            logger.exception("lease control loop error for %s; standby/not ready", mgr.pipeline)
            state.set_leader(False)
            state.set_ready(False)
        stop_event.wait(renew_interval)


# TLS: full server identity verification always on (check_hostname=True,
# CERT_REQUIRED). The connect host and the TLS server-name-to-verify differ
# by design -- the shared wildcard cert's SAN matches the external Ingress
# hostname pattern, not *.svc.cluster.local.
_SSL_CTX = None


def _build_ssl_context(ca_file=CA_FILE):
    global _SSL_CTX
    if _SSL_CTX is not None:
        return _SSL_CTX
    if not ca_file or not os.path.exists(ca_file):
        raise RuntimeError(f"CA_FILE {ca_file!r} not found -- refusing to poll without TLS verification.")
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(ca_file)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    _SSL_CTX = ctx
    return ctx


def _read_secret_file(path):
    """Re-read each cycle so a rotated secret is picked up without a pod
    restart. Never logs content; a read failure degrades to empty string."""
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


class _SNIHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, tls_server_name=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._tls_server_name = tls_server_name

    def connect(self):
        sock = socket.create_connection((self.host, self.port), self.timeout)
        server_hostname = self._tls_server_name or self.host
        self.sock = self._context.wrap_socket(sock, server_hostname=server_hostname)


def _basic_opener(user, pwd, base, ssl_ctx, tls_server_name):
    pwd_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    pwd_mgr.add_password(None, base, user, pwd)
    auth_handler = urllib.request.HTTPBasicAuthHandler(pwd_mgr)
    conn_factory = functools.partial(_SNIHTTPSConnection, tls_server_name=tls_server_name)

    class _SNIHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(conn_factory, req, context=ssl_ctx)

    return urllib.request.build_opener(auth_handler, _SNIHTTPSHandler())


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


_KNOWN_PROCESS_STATUSES = ("RUNNING", "STOPPED", "ABENDED")


def _valid_process_name(raw):
    """A real GoldenGate process name only -- never a synthetic fallback
    (e.g. "unknown" or an internal $id). Returns None when the item carries
    no usable name, so the caller can skip it entirely rather than ever
    producing a STATE#unknown record."""
    name = str(raw).strip() if raw is not None else ""
    return name or None


def _normalize_status(raw):
    status = str(raw or "").upper()
    return status if status in _KNOWN_PROCESS_STATUSES else "UNKNOWN"


def _normalize_lag(raw):
    """Never non-negative, never an exception -- a malformed/negative value
    degrades to 0.0 rather than aborting the tick."""
    try:
        lag = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return lag if lag > 0 else 0.0


def fetch_gg_processes(base, opener):
    """GoldenGate Admin REST polling (port 8443 only). Tolerant of malformed
    per-item data: an item with no valid process name is skipped rather than
    recorded under a synthetic name, so STATE#unknown can never be produced.
    Duplicate (type, name) pairs -- e.g. a repeated list entry -- keep only
    the first occurrence. An empty process list is a valid result, never
    treated as a deployment failure. Never logs a raw response body or raw
    exception text -- only the endpoint kind and exception class."""
    _http_json(f"{base}/services/v2/deployments", opener)  # liveness probe
    procs = []
    seen = set()
    for kind, ptype in (("extracts", "extract"), ("replicats", "replicat")):
        try:
            items = _http_json(f"{base}/services/v2/{kind}", opener).get("response", {}).get("items", [])
        except Exception as e:
            logger.warning("listing %s failed: %s", kind, type(e).__name__)
            items = []
        if not isinstance(items, list):
            items = []
        for it in items:
            if not isinstance(it, dict):
                continue
            name = _valid_process_name(it.get("name"))
            if name is None or (ptype, name) in seen:
                continue
            detail = {}
            try:
                raw_detail = _http_json(f"{base}/services/v2/{kind}/{name}", opener).get("response", {})
                if isinstance(raw_detail, dict):
                    detail = raw_detail
            except Exception as e:
                logger.warning("detail fetch failed for %s process: %s", ptype, type(e).__name__)
            status = _normalize_status(detail.get("status", it.get("status")))
            lag = _normalize_lag(detail.get("lag", detail.get("lagSeconds", it.get("lagSeconds", 0))))
            err = str(detail.get("lastError") or detail.get("error")
                      or detail.get("message") or "") if status == "ABENDED" else ""
            seen.add((ptype, name))
            procs.append({"process": name, "type": ptype, "lagSeconds": lag,
                          "abended": status == "ABENDED", "status": status,
                          "metrics": detail or {}, "error": err})
    try:
        items = _http_json(f"{base}/services/v2/sources", opener).get("response", {}).get("items", [])
    except Exception as e:
        logger.debug("dispatch sources scrape skipped: %s", type(e).__name__)
        items = []
    if not isinstance(items, list):
        items = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = _valid_process_name(it.get("name"))
        if name is None or ("distpath", name) in seen:
            continue
        status = _normalize_status(it.get("status"))
        bytes_now = next((it.get(k) for k in gh.BYTES_KEYS if it.get(k) is not None), None)
        seen.add(("distpath", name))
        procs.append({"process": name, "type": "distpath", "lagSeconds": 0.0,
                      "abended": status == "ABENDED", "status": status,
                      "bytes": bytes_now, "metrics": it or {},
                      "error": "" if status != "ABENDED" else str(it.get("lastError") or "")})
    return procs


def discovery_counts(procs):
    counts = {"extract": 0, "replicat": 0, "distpath": 0}
    for p in procs:
        if p.get("type") in counts:
            counts[p["type"]] += 1
    return counts


def log_discovery_summary(pipeline, procs):
    """One structured, non-sensitive log line per deployment tick. Never
    logs the process payload itself -- only per-type counts."""
    counts = discovery_counts(procs)
    logger.info(json.dumps({
        "event": "process_discovery_summary",
        "deployment": pipeline,
        "extractCount": counts["extract"],
        "replicatCount": counts["replicat"],
        "distpathCount": counts["distpath"],
        "totalCount": counts["extract"] + counts["replicat"] + counts["distpath"],
    }))


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


_LAG_METRIC_BY_PROCESS_TYPE = {"extract": "ExtractLagSeconds", "replicat": "ReplicatLagSeconds"}


def build_metric_batch(pipeline, deployment_type, flags, procs=None,
                       critical_service_status=None, abend_events=None, heartbeat_ok=False):
    """Pure builder for the full manager-compatible metric contract: ordinary
    dicts only, no boto3 calls, no CloudWatch client -- safe to unit-test
    while CLOUDWATCH_PUBLISH_ENABLED stays false.

    flags: {"lag": 0/1, "abend": 0/1, "down": 0/1} deployment-level breach
    flags for this tick.
    procs: normalized process rows (process/type/lagSeconds/abended); unknown
    process types receive AbendState only, never a lag metric.
    critical_service_status: {serviceName: up_bool}.
    abend_events: process names that just transitioned into a countable abend
    event this tick (per the existing abend-rule cadence, not every tick).
    heartbeat_ok: True only when the caller has already completed a
    successful, fenced STATE#_deployment write for this tick -- see
    run_pipeline/polling_loop for the shared-monitor heartbeat semantics.
    """
    procs = procs or []
    critical_service_status = critical_service_status or {}
    abend_events = abend_events or ()

    dep_dims = [{"Name": "Deployment", "Value": pipeline}, {"Name": "DeploymentType", "Value": deployment_type}]
    md = [{"MetricName": n, "Dimensions": dep_dims, "Value": float(v), "Unit": "Count"}
          for n, v in (("LagBreached", flags.get("lag", 0)),
                       ("AbendFailure", flags.get("abend", 0)),
                       ("DeploymentDown", flags.get("down", 0)))]

    if heartbeat_ok:
        md.append({"MetricName": "HeartbeatAgeSeconds", "Dimensions": dep_dims, "Value": 0.0, "Unit": "Seconds"})

    for svc, up in critical_service_status.items():
        md.append({"MetricName": "CriticalServiceDown",
                   "Dimensions": dep_dims + [{"Name": "Service", "Value": svc}],
                   "Value": 0.0 if up else 1.0, "Unit": "Count"})

    for p in procs:
        proc_dims = dep_dims + [{"Name": "Process", "Value": p["process"]}]
        lag_metric = _LAG_METRIC_BY_PROCESS_TYPE.get(p.get("type"))
        if lag_metric:
            md.append({"MetricName": lag_metric, "Dimensions": proc_dims,
                       "Value": float(p.get("lagSeconds", 0) or 0), "Unit": "Seconds"})
        md.append({"MetricName": "AbendState", "Dimensions": proc_dims,
                   "Value": 1.0 if p.get("abended") else 0.0, "Unit": "Count"})

    for name in abend_events:
        md.append({"MetricName": "AbendEvent",
                   "Dimensions": dep_dims + [{"Name": "Process", "Value": name}],
                   "Value": 1.0, "Unit": "Count"})

    return md


def publish_metric_batch(cw, metric_data):
    """The only boto3-calling half of metric emission -- always called behind
    cloudwatch_enabled_for(cfg), never while the hard switch is false."""
    if not cw or not metric_data:
        return
    for i in range(0, len(metric_data), 20):  # PutMetricData max 20/call
        try:
            cw.put_metric_data(Namespace=CLOUDWATCH_NAMESPACE, MetricData=metric_data[i:i + 20])
        except Exception:
            logger.exception("CloudWatch put_metric_data failed; continuing")


class _FencedOff(Exception):
    pass


def write_process_state(table, mgr, pipeline, deployment_type, process, snapshot, is_leader_fn, counters=None):
    """Owns recordType=STATE#_deployment and STATE#<process> exclusively."""
    if not is_leader_fn():
        return False
    if not mgr.renew():
        logger.warning("state write fenced off for %s/%s (lease lost)", pipeline, process)
        return False
    names = {"#st": "status"}
    sets, vals = ["#st=:st", "recordedAt=:ra", "deploymentType=:dt"], {
        ":st": str(snapshot.get("status", "UNKNOWN")),
        ":ra": int(snapshot.get("recordedAt", cfgmod.now_epoch())),
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
    """CONFIG is Terraform-owned -- the collector only ever reads it."""
    resp = table.get_item(Key={"pipeline": pipeline, "recordType": "CONFIG"})
    return resp.get("Item", {})


def check_static_prerequisites(deployment, table):
    """Returns (ok, reason). A transient failure here just keeps retrying;
    it never crashes the process. Deliberately excludes GoldenGate Admin
    REST reachability -- the runtime API being down must not make the
    monitor pod unready.

    reason is always a fixed, generic string (plus, where noted, the
    canonical deployment name / expected-vs-actual type) -- never a
    credential file path, CA path, secret value, or raw AWS exception. The
    caller (run_pipeline) logs this reason on every retry, so it must stay
    safe to log repeatedly."""
    pipeline = deployment["name"]
    user_file, pwd_file = cfgmod.credential_paths(pipeline)
    if not _read_secret_file(user_file):
        return False, "admin username credential unavailable"
    if not _read_secret_file(pwd_file):
        return False, "admin password credential unavailable"

    try:
        _build_ssl_context()
    except RuntimeError:
        return False, "TLS trust bundle unavailable"

    try:
        config_item = read_config(table, pipeline)
    except Exception:
        return False, "DynamoDB CONFIG unavailable"

    if not config_item:
        return False, f"CONFIG item missing for {pipeline!r}"
    config_deployment_type = config_item.get("deploymentType")
    if config_deployment_type != deployment["type"]:
        return False, (
            f"CONFIG deploymentType mismatch for {pipeline!r}: "
            f"expected {deployment['type']!r}, got {config_deployment_type!r}"
        )

    return True, ""


def polling_loop(deployment, table, mgr, state, stop_event):
    pipeline = deployment["name"]
    deployment_type = deployment["type"]
    started = cfgmod.now_epoch()
    last_dep_status = None
    distpath_mem = {}

    base = f"https://{deployment['adminHost']}:{deployment['adminPort']}"
    tls_server_name = deployment["tlsServerName"]
    user_file, pwd_file = cfgmod.credential_paths(pipeline)

    def _guarded_write(proc, snap, counters=None):
        ok = write_process_state(table, mgr, pipeline, deployment_type, proc, snap, state.is_leader, counters=counters)
        if not ok:
            logger.warning("tick fenced off for %s/%s; aborting tick", pipeline, proc)
            raise _FencedOff()

    def _sleep_watching_leadership(total_seconds):
        leader_at_start = state.is_leader()
        remaining = total_seconds
        while remaining > 0 and not stop_event.is_set():
            if state.is_leader() != leader_at_start:
                break
            step = min(POLL_SLEEP_GRANULARITY, remaining)
            stop_event.wait(step)
            remaining -= step

    logger.info("polling loop started for %s (type=%s, base=%s)", pipeline, deployment_type, base)

    while not stop_event.is_set():
        interval = gh.DEFAULTS["checkIntervalSeconds"]
        try:
            cfg = gh.resolve_config(read_config(table, pipeline))
            interval = cfg["checkIntervalSeconds"]

            if not state.is_leader():
                _sleep_watching_leadership(interval)
                continue

            flags = {"lag": 0, "abend": 0, "down": 0}
            abend_event_names = []

            user = _read_secret_file(user_file)
            pwd = _read_secret_file(pwd_file)
            if not user or not pwd:
                # Fail closed: never guess a username, never attempt Basic
                # auth, never poll GoldenGate, never write a deployment
                # STATE this tick. Only the canonical deployment name is
                # logged -- never a file path or secret value.
                state.set_credentials_ok(False)
                logger.warning("admin credentials unavailable for %s; skipping this tick", pipeline)
                _sleep_watching_leadership(interval)
                continue
            state.set_credentials_ok(True)

            ssl_ctx = _build_ssl_context()
            opener = _basic_opener(user, pwd, base, ssl_ctx, tls_server_name)

            try:
                procs = fetch_gg_processes(base, opener)
            except Exception as e:
                in_grace = (cfgmod.now_epoch() - started) < cfg["startupGraceSeconds"]
                status = "STARTING" if in_grace else "DEPLOYMENT_DOWN"
                if not in_grace and cfg["alertsEnabled"]:
                    flags["down"] = 1
                transitioned = (status != last_dep_status)
                dep_snap = {"processType": "deployment", "status": status, "recordedAt": cfgmod.now_epoch()}
                if transitioned:
                    dep_snap["lastTransitionAt"] = cfgmod.now_epoch()
                _guarded_write("_deployment", dep_snap)
                last_dep_status = status
                logger.warning("GoldenGate Admin REST unreachable for %s (%s): %s", pipeline, status, e)
                # The _deployment write above just succeeded (it would have
                # raised _FencedOff otherwise) -- the monitor itself is alive
                # even though GoldenGate is unreachable, so the heartbeat
                # still fires here (shared-monitor semantics; see
                # build_metric_batch's heartbeat_ok docstring).
                if cloudwatch_enabled_for(cfg):
                    metric_data = build_metric_batch(pipeline, deployment_type, flags, heartbeat_ok=True)
                    publish_metric_batch(_cloudwatch_client(), metric_data)
                _sleep_watching_leadership(interval)
                continue

            log_discovery_summary(pipeline, procs)
            source_active = any(p["type"] == "extract" and p["status"] == "RUNNING" for p in procs)

            for p in procs:
                name, ptype, status = p["process"], p["type"], p["status"]
                rule = gh.rule_for_process(cfg, name)
                mode, thr = gh.lag_rule_now(cfg, name)
                prev = read_process_state(table, pipeline, name)
                counters, act = gh.abend_step(status=status, state=prev, now=cfgmod.now_epoch(),
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
                # act["failover"] is never acted on -- no restart/exit path exists.
                if act["abend_event"]:
                    abend_event_names.append(name)
                snap = {"processType": ptype, "status": status,
                        "lagSeconds": int(p["lagSeconds"]), "recordedAt": cfgmod.now_epoch(),
                        "resolvedThreshold": thr, "resolvedMode": mode,
                        "pipelineName": deployment["pipeline"],
                        "errorMsg": str(p.get("error", "")),
                        "performanceMetrics": p.get("metrics") or {}}
                if str(prev.get("status")) != status:
                    snap["lastTransitionAt"] = cfgmod.now_epoch()
                try:
                    _guarded_write(name, snap, counters=counters)
                except _FencedOff:
                    raise
                except Exception:
                    logger.exception("process %s evaluation failed; skipped", name)

            critical = gh.CRITICAL_SERVICES_BY_TYPE.get(deployment_type, [])
            if critical:
                svc_up = probe_critical_services(base, opener, critical)
                cs_new = {svc: {"reachable": bool(up)} for svc, up in svc_up.items()}
            else:
                svc_up = {}
                cs_new = {}

            transitioned = ("UP" != last_dep_status)
            dep_snap = {"processType": "deployment", "status": "UP",
                        "recordedAt": cfgmod.now_epoch(), "criticalServices": cs_new}
            if transitioned:
                dep_snap["lastTransitionAt"] = cfgmod.now_epoch()
            _guarded_write("_deployment", dep_snap)
            last_dep_status = "UP"

            # The _deployment write above just succeeded -- heartbeat fires.
            if cloudwatch_enabled_for(cfg):
                metric_data = build_metric_batch(pipeline, deployment_type, flags, procs=procs,
                                                 critical_service_status=svc_up,
                                                 abend_events=abend_event_names, heartbeat_ok=True)
                publish_metric_batch(_cloudwatch_client(), metric_data)

        except _FencedOff:
            pass
        except Exception:
            logger.exception("tick failed for %s; continuing next interval", pipeline)
        _sleep_watching_leadership(interval)


_CW_CLIENT = None
_CW_LOCK = threading.Lock()


def _cloudwatch_client():
    global _CW_CLIENT
    with _CW_LOCK:
        if _CW_CLIENT is None:
            _CW_CLIENT = boto3.client("cloudwatch", region_name=os.environ.get("AWS_REGION", "eu-west-1"))
        return _CW_CLIENT


def run_pipeline(deployment, stop_event, ready_state, aws_region, dynamodb_table, monitor_instance):
    """Sets up dedicated Table/LeaseManager pairs for the lease-control and
    polling loops (boto3 Table objects are not safe to share across
    concurrent update_item calls), waits for static prerequisites, then
    runs both loops as daemon threads."""
    pipeline = deployment["name"]

    lease_table = boto3.resource("dynamodb", region_name=aws_region).Table(dynamodb_table)
    health_table = boto3.resource("dynamodb", region_name=aws_region).Table(dynamodb_table)

    lease_mgr = LeaseManager(lease_table, pipeline, monitor_instance)
    health_mgr = LeaseManager(health_table, pipeline, monitor_instance)
    health_mgr.token = lease_mgr.token  # same lease identity -- fence semantics preserved

    state = LeaseState()

    prereq_interval = RENEW_INTERVAL
    while not stop_event.is_set():
        ok, reason = check_static_prerequisites(deployment, lease_table)
        if ok:
            break
        logger.warning("%s not ready yet: %s (retrying in %ss)", pipeline, reason, prereq_interval)
        stop_event.wait(prereq_interval)

    if stop_event.is_set():
        return

    lease_thread = threading.Thread(target=lease_control_loop, args=(lease_mgr, state, stop_event), daemon=True)
    poll_thread = threading.Thread(target=polling_loop, args=(deployment, health_table, health_mgr, state, stop_event), daemon=True)
    lease_thread.start()
    poll_thread.start()

    while not stop_event.is_set():
        ready_state[pipeline] = state.is_ready() and state.credentials_ok()
        stop_event.wait(1)

    lease_thread.join()
    poll_thread.join()
