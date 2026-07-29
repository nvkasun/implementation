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

logger = logging.getLogger("gg-monitor")

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
    lease-control loop and its polling loop (two independent threads)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._is_leader = False
        self._ready = False

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


def fetch_gg_processes(base, opener):
    """GoldenGate Admin REST polling (port 8443 only)."""
    _http_json(f"{base}/services/v2/deployments", opener)  # liveness probe
    procs = []
    for kind, ptype in (("extracts", "extract"), ("replicats", "replicat")):
        try:
            items = _http_json(f"{base}/services/v2/{kind}", opener).get("response", {}).get("items", [])
        except Exception as e:
            logger.warning("listing %s failed: %s", kind, e)
            items = []
        for it in items:
            name = str(it.get("name") or it.get("$id") or "unknown")
            detail = {}
            try:
                detail = _http_json(f"{base}/services/v2/{kind}/{name}", opener).get("response", {})
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
        items = _http_json(f"{base}/services/v2/sources", opener).get("response", {}).get("items", [])
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
            md.append({"MetricName": lag_metric, "Dimensions": dims, "Value": p["lagSeconds"], "Unit": "Seconds"})
        md.append({"MetricName": "AbendState", "Dimensions": dims,
                   "Value": 1.0 if p["abended"] else 0.0, "Unit": "Count"})
    return md


def _emit(cw, deployment, deployment_type, flags, extra_md=None):
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
    monitor pod unready."""
    pipeline = deployment["name"]
    user_file, pwd_file = cfgmod.credential_paths(pipeline)
    if not _read_secret_file(user_file):
        return False, f"credential file empty or unreadable: {user_file}"
    if not _read_secret_file(pwd_file):
        return False, f"credential file empty or unreadable: {pwd_file}"

    try:
        _build_ssl_context()
    except RuntimeError as e:
        return False, f"TLS context unavailable: {e}"

    try:
        config_item = read_config(table, pipeline)
    except Exception as e:
        return False, f"DynamoDB CONFIG read failed: {e}"

    if not config_item:
        return False, f"CONFIG item missing for {pipeline!r}"
    config_deployment_type = config_item.get("deploymentType")
    if config_deployment_type != deployment["type"]:
        return False, f"CONFIG deploymentType {config_deployment_type!r} != {deployment['type']!r}"

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
            extra_md = []

            user = _read_secret_file(user_file) or "oggadmin"
            pwd = _read_secret_file(pwd_file)
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
                if cloudwatch_enabled_for(cfg):
                    _emit(_cloudwatch_client(), pipeline, deployment_type, flags)
                _sleep_watching_leadership(interval)
                continue

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
                    extra_md.append({"MetricName": "AbendEvent",
                                     "Dimensions": [{"Name": "Deployment", "Value": pipeline},
                                                    {"Name": "DeploymentType", "Value": deployment_type},
                                                    {"Name": "Process", "Value": name}],
                                     "Value": 1.0, "Unit": "Count"})
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
                for svc, up in svc_up.items():
                    extra_md.append({"MetricName": "CriticalServiceDown",
                                     "Dimensions": [{"Name": "Deployment", "Value": pipeline},
                                                    {"Name": "DeploymentType", "Value": deployment_type},
                                                    {"Name": "Service", "Value": svc}],
                                     "Value": 0.0 if up else 1.0, "Unit": "Count"})
            else:
                cs_new = {}

            transitioned = ("UP" != last_dep_status)
            dep_snap = {"processType": "deployment", "status": "UP",
                        "recordedAt": cfgmod.now_epoch(), "criticalServices": cs_new}
            if transitioned:
                dep_snap["lastTransitionAt"] = cfgmod.now_epoch()
            _guarded_write("_deployment", dep_snap)
            last_dep_status = "UP"

            if cloudwatch_enabled_for(cfg):
                extra_md += build_metric_data(pipeline, deployment_type, procs)
                _emit(_cloudwatch_client(), pipeline, deployment_type, flags, extra_md)

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
        ready_state[pipeline] = state.is_ready()
        stop_event.wait(1)

    lease_thread.join()
    poll_thread.join()
