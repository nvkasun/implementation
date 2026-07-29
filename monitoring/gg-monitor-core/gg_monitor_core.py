#!/usr/bin/env python3
"""gg-monitor-core: shared, passive GoldenGate runtime poller/writer.

Takes over the manager reference implementation's per-pod utility-sidecar
writer responsibilities (LEASE ownership, STATE#_deployment / STATE#<process>
writes, GoldenGate Admin REST polling, CloudWatch metric publication) as ONE
shared, external Deployment -- because our approved architecture has no
utility sidecar inside GoldenGate runtime pods (see
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

Terminology: this module polls the GoldenGate Admin REST API (port 8443)
only -- exactly what the manager utility-sidecar polls. It does NOT poll the
separate PMS/metrics endpoint (port 9015); that endpoint is listed in
topology for a later, explicitly implemented phase and is unused here. Do
not describe this module as "REST/PMS polling".
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
    StartupValidationError,
    build_deployments_json,
    build_process_pipeline_map_json,
    load_runtimes,
    validate_enabled_runtimes,
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
# How finely the polling loop's sleep is chopped up so it notices a lease
# demotion promptly instead of sleeping out the full checkIntervalSeconds
# (default 60s) window regardless of leadership.
POLL_SLEEP_GRANULARITY = min(RENEW_INTERVAL, 5)

AWS_REGION = os.environ.get("AWS_REGION", "eu-west-1")
DDB_TABLE = os.environ.get("DDB_TABLE", "gg-eks-pipeline")
MONITOR_INSTANCE = os.environ.get("POD_NAME", "gg-monitor")
HTTP_PORT = int(os.environ.get("PORT", "8080"))

# Admin credentials are DEPLOYMENT-level, not engine-type-level (manager-
# alignment correction, fix 1): each runtime dict carries its own
# credentialUserFile/credentialPasswordFile (inventory.load_runtimes,
# derived from that deployment's own secretReferences.admin), mounted by the
# Secrets Store CSI Driver (see helm/gg-monitor/templates/
# secretproviderclass.yaml, generated from the same canonical data -- no
# hardcoded per-engine dict on either side). Re-read every tick so a
# rotated secret is picked up without a pod restart, exactly like the
# manager's _read_admin_password(). There is deliberately no
# ADMIN_USER_FILE/ADMIN_PASSWORD_FILE dict here anymore: a second Oracle
# deployment with a different secret needs no Python change, just its own
# topology entry.
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


class LeaseState:
    """Thread-safe leader/readiness state shared between one pipeline's
    dedicated lease-control loop and its polling loop (fix 1: these are two
    independent loops/threads per pipeline, not one loop reusing the
    60-second poll interval for lease renewal too)."""

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
    """Dedicated lease acquire/renew loop -- runs on its OWN cadence
    (renew_interval, default 5s), completely independent of
    CONFIG.checkIntervalSeconds (default 60s). This is the fix for the
    deployment blocker: previously lease renewal only happened once per
    60-second poll tick, so a 30-second-TTL lease always expired mid-sleep.

    Mirrors the manager's utility-sidecar lease_loop in structure: renew if
    leader, else try to acquire; demote immediately (state.set_leader(False))
    the instant a renew/acquire call reports the lease is not (or no longer)
    held.

    state.is_ready() reflects CURRENT lease-API health, not a one-time latch:
    any successful acquire/renew CALL -- whether it wins the lease (True) or
    correctly reports a conflict because another valid holder already owns
    it (False, but still a successful DynamoDB round-trip) -- sets ready
    True. An EXCEPTION (DynamoDB unreachable, AccessDenied, etc.) sets both
    leader and ready False immediately; the next successful call (after
    DynamoDB recovers) restores ready True on its own, every iteration re-
    entering this same try block fresh. This is what lets run_pipeline's
    readiness loop continuously mirror real dependency health instead of
    latching "ready" forever after the first success.
    """
    while not stop_event.is_set():
        try:
            ok = mgr.renew() if state.is_leader() else mgr.acquire()
            if ok:
                if not state.is_leader():
                    logger.info("Acquired lease for %s; this instance is leader.", mgr.pipeline)
                state.set_leader(True)
            else:
                if state.is_leader():
                    logger.warning("Lost lease for %s; demoting to standby immediately.", mgr.pipeline)
                state.set_leader(False)
            # Reached only when the DynamoDB call itself succeeded -- true
            # regardless of which branch above ran (won the lease, or
            # correctly lost/deferred to a valid existing holder).
            state.set_ready(True)
        except Exception:
            logger.exception("lease control loop error for %s; treating as standby and not ready", mgr.pipeline)
            state.set_leader(False)
            state.set_ready(False)
        stop_event.wait(renew_interval)


# ---------------------------------------------------------------------
# TLS. Real network calls (not the sidecar's loopback case): full server
# identity verification is always ON -- check_hostname=True,
# verify_mode=CERT_REQUIRED, never CERT_NONE. The connect address (internal
# Kubernetes Service DNS) and the TLS server-identity-check name
# (tlsServerName, matching the shared wildcard certificate's SAN pattern)
# are DIFFERENT strings by design -- see _SNIHTTPSConnection below, which
# is the mechanism that lets urllib connect to one host while verifying
# against another (plain urllib/http.client conflate the two).
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
    # Explicit, not merely relying on create_default_context()'s own
    # defaults, so a future refactor can never silently weaken this:
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
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


class _SNIHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that connects to self.host (the internal Kubernetes
    Service DNS name, parsed from the request URL as usual) but sends SNI
    and verifies the server certificate against a SEPARATE tls_server_name
    -- required because the shared wildcard certificate's SAN matches the
    external Ingress hostname pattern, not *.svc.cluster.local. Never falls
    back to an unverified connection: wrap_socket always runs through the
    caller-supplied context, which is always check_hostname=True +
    CERT_REQUIRED (see _build_ssl_context)."""

    def __init__(self, *args, tls_server_name=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._tls_server_name = tls_server_name

    def connect(self):
        sock = socket.create_connection((self.host, self.port), self.timeout)
        server_hostname = self._tls_server_name or self.host
        self.sock = self._context.wrap_socket(sock, server_hostname=server_hostname)


def _basic_opener(user, pwd, base, ssl_ctx, tls_server_name):
    """Build a urllib OpenerDirector that performs HTTP Basic auth and TLS
    verification against tls_server_name (SNI + hostname check), while the
    TCP connection itself goes to the host embedded in `base` (internal
    Service DNS)."""
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


# ---------------------------------------------------------------------
# GoldenGate Admin REST polling (ported verbatim from the manager's
# utility-sidecar fetch_gg_processes -- same endpoints, same parsing, same
# tolerant per-process error handling). Port 8443 (Admin REST) only -- the
# separate PMS/metrics endpoint (port 9015) is not polled by this module.
# base = the runtime's internal Kubernetes Service DNS admin endpoint, never
# the external Ingress URL.
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
# Runtime readiness prerequisites (fix 2). Distinct from STARTUP validation
# (inventory.validate_enabled_runtimes, which is fatal/hard-fails the whole
# process before it ever starts serving): these are per-pipeline checks
# that must succeed at least once before this pipeline is reported Ready,
# but a transient failure here just keeps retrying -- it never crashes the
# process. Deliberately does NOT include "GoldenGate Admin REST reachable":
# the runtime API being down must not make the monitor pod unready.
#
# Deliberately does NOT call mgr.acquire()/mgr.renew() here: an early test
# acquire would set holder/leaseToken in DynamoDB using this function's own
# call, but leave LeaseState.is_leader() at its initial False -- the real
# lease_control_loop's first acquire() attempt would then be rejected by its
# own already-in-place condition (holder already set, expiresAt still in the
# future), silently delaying real leadership by up to LEASE_TTL for no
# reason. The lease API path is instead exercised (and read as ready) by
# lease_control_loop itself -- see LeaseState.is_ready(), set there after its
# own first successful acquire/renew call -- so there is exactly one place
# that ever calls the lease API for a given pipeline+table pair at a time.
# ---------------------------------------------------------------------
def check_static_prerequisites(runtime, table):
    """Returns (ok: bool, reason: str). reason is empty when ok=True."""
    deployment_type = runtime["type"]

    # Deployment-level credential identity (fix 1) -- runtime["type"] plays
    # no part in selecting WHICH credential files to check; each runtime
    # carries its own paths, derived from its own secretReferences.admin.
    user_file = runtime.get("credentialUserFile")
    pwd_file = runtime.get("credentialPasswordFile")
    if not user_file or not pwd_file:
        return False, f"no credential file paths on runtime {runtime.get('pipeline')!r}"
    if not _read_secret_file(user_file):
        return False, f"credential file empty or unreadable: {user_file}"
    if not _read_secret_file(pwd_file):
        return False, f"credential file empty or unreadable: {pwd_file}"

    try:
        _build_ssl_context()
    except RuntimeError as e:
        return False, f"TLS context unavailable: {e}"

    try:
        config_item = read_config(table, runtime["pipeline"])
    except Exception as e:
        return False, f"DynamoDB CONFIG read failed: {e}"

    # The canonical CONFIG item is Terraform-owned (envs/dev/dynamodb.tf) and
    # must exist for every enabled runtime before this pipeline is ready --
    # an empty/missing Item is a real configuration gap, not something to
    # paper over with manager-style in-code defaults.
    if not config_item:
        return False, f"CONFIG item missing for pipeline {runtime['pipeline']!r}"

    if "recordType" in config_item and config_item["recordType"] != "CONFIG":
        return False, f"CONFIG item has unexpected recordType {config_item.get('recordType')!r}"

    config_deployment_type = config_item.get("deploymentType")
    if not config_deployment_type:
        return False, f"CONFIG item for {runtime['pipeline']!r} is missing deploymentType"
    if config_deployment_type != deployment_type:
        return False, (
            f"CONFIG deploymentType {config_deployment_type!r} does not match "
            f"inventory runtime type {deployment_type!r} for {runtime['pipeline']!r}"
        )

    return True, ""


# ---------------------------------------------------------------------
# Per-pipeline poll/write tick loop (fix 1: lease renewal now lives in its
# own lease_control_loop above, on its own RENEW_INTERVAL cadence -- this
# loop only polls/writes, gated on state.is_leader(), and sleeps in short
# increments so it notices a lease demotion promptly instead of riding out
# the full checkIntervalSeconds window regardless of leadership).
# ---------------------------------------------------------------------
def polling_loop(runtime, table, mgr, state, stop_event, full_process_pipeline_map=None):
    """full_process_pipeline_map: the GLOBAL process-pipeline-map (built
    ONCE in main() across all enabled runtimes -- see
    inventory.build_process_pipeline_map_json), filtered HERE to just this
    deployment's own entries. Mirrors the manager's own two-stage pattern
    exactly (utility-sidecar.py: the map is read/built once, then
    build_process_pipeline_map(process_map, bare_key) filters it locally per
    deployment) -- process routing is process-topology data (concept C),
    not something this loop derives from its own runtime dict alone, since
    the same deployment's processes can be declared across multiple
    topology documents under different logical pipelines."""
    pipeline = runtime["pipeline"]
    deployment_type = runtime["type"]
    started = now_epoch()
    last_dep_status = None
    distpath_mem = {}

    admin_ep = (runtime.get("endpoints") or {}).get("admin") or {}
    base = f"{admin_ep.get('scheme', 'https')}://{admin_ep['host']}:{admin_ep['port']}"
    tls_server_name = admin_ep.get("tlsServerName")

    bare_key = runtime["name"]
    process_pipeline_map = {
        proc: meta for proc, meta in (full_process_pipeline_map or {}).items()
        if meta.get("deployment") == bare_key
    }

    def _guarded_write(proc, snap, counters=None):
        ok = write_process_state(table, mgr, pipeline, deployment_type, proc, snap,
                                 state.is_leader, counters=counters)
        if not ok:
            logger.warning("tick fenced off for %s/%s; aborting tick", pipeline, proc)
            raise _FencedOff()

    def _sleep_watching_leadership(total_seconds):
        """Sleeps up to total_seconds in POLL_SLEEP_GRANULARITY-sized steps,
        waking early if stop_event fires OR leadership changes -- so a lease
        loss/gain is noticed within one granularity step (default 5s)
        instead of riding out the full checkIntervalSeconds window (default
        60s) regardless of leadership. This is in addition to, not instead
        of, write-time fencing (write_process_state/_guarded_write already
        refuse to write the instant leadership is lost, mid-tick or not)."""
        leader_at_start = state.is_leader()
        remaining = total_seconds
        while remaining > 0 and not stop_event.is_set():
            if state.is_leader() != leader_at_start:
                break
            step = min(POLL_SLEEP_GRANULARITY, remaining)
            stop_event.wait(step)
            remaining -= step

    logger.info("polling loop started for %s (type=%s, base=%s, tlsServerName=%s)",
               pipeline, deployment_type, base, tls_server_name)

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

            user = _read_secret_file(runtime["credentialUserFile"]) or "oggadmin"
            pwd = _read_secret_file(runtime["credentialPasswordFile"])
            ssl_ctx = _build_ssl_context()
            opener = _basic_opener(user, pwd, base, ssl_ctx, tls_server_name)

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
                logger.warning("GoldenGate Admin REST unreachable for %s (%s): %s", pipeline, status, e)
                _emit(_cloudwatch_client(), pipeline, deployment_type, flags)
                _sleep_watching_leadership(interval)
                continue

            source_active = any(p["type"] == "extract" and p["status"] == "RUNNING" for p in procs)

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
                        # Resolved from the real process-pipeline map (fix 4):
                        # empty string only when no mapping exists for this
                        # process name -- never a hardcoded {} lookup.
                        "pipelineName": process_pipeline_map.get(name.upper(), {}).get("pipeline_name", ""),
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
                        "recordedAt": now_epoch(), "criticalServices": cs_new}
            if transitioned:
                dep_snap["lastTransitionAt"] = now_epoch()
            _guarded_write("_deployment", dep_snap)
            last_dep_status = "UP"

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
            _CW_CLIENT = boto3.client("cloudwatch", region_name=AWS_REGION)
        return _CW_CLIENT


# ---------------------------------------------------------------------
# Per-pipeline supervisor: sets up dedicated DynamoDB Table/LeaseManager
# pairs for the lease-control loop and the polling loop (boto3 Table
# objects are not thread-safe across concurrent update_item calls -- same
# lesson as the manager's utility-sidecar main(), which gives health_thread
# its own resource for the same reason), waits for runtime prerequisites
# (fix 2) before flipping readiness, then runs both loops as daemon threads.
# ---------------------------------------------------------------------
def run_pipeline(runtime, stop_event, ready_state, full_process_pipeline_map=None):
    pipeline = runtime["pipeline"]

    lease_table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(DDB_TABLE)
    health_table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(DDB_TABLE)

    lease_mgr = LeaseManager(lease_table, pipeline, MONITOR_INSTANCE)
    health_mgr = LeaseManager(health_table, pipeline, MONITOR_INSTANCE)
    health_mgr.token = lease_mgr.token  # same lease identity -- fence semantics preserved

    state = LeaseState()

    # Phase 1: static prerequisites (credentials, TLS, CONFIG read) -- no
    # lease API calls here (see check_static_prerequisites for why).
    prereq_interval = RENEW_INTERVAL
    while not stop_event.is_set():
        ok, reason = check_static_prerequisites(runtime, lease_table)
        if ok:
            break
        logger.warning("pipeline %s not ready yet: %s (retrying in %ss)", pipeline, reason, prereq_interval)
        stop_event.wait(prereq_interval)

    if stop_event.is_set():
        return

    # Phase 2: start the two independent loops. lease_control_loop's own
    # first successful acquire/renew call is what proves the lease API path
    # works -- reflected below via state.is_ready(), the single source of
    # truth for "has the lease API been successfully exercised".
    lease_thread = threading.Thread(
        target=lease_control_loop, args=(lease_mgr, state, stop_event), daemon=True)
    poll_thread = threading.Thread(
        target=polling_loop,
        args=(runtime, health_table, health_mgr, state, stop_event, full_process_pipeline_map),
        daemon=True)
    lease_thread.start()
    poll_thread.start()

    # Continuously mirror current lease-API health into ready_state for the
    # life of this pipeline -- never latch True once and stop watching.
    # GoldenGate Admin REST reachability plays no part here (see
    # polling_loop/check_static_prerequisites): only lease-API health drives
    # this pipeline's readiness after startup.
    while not stop_event.is_set():
        ready_state[pipeline] = state.is_ready()
        stop_event.wait(1)

    lease_thread.join()
    poll_thread.join()


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

    # Fatal startup validation (fix 2): an enabled runtime missing required
    # topology/endpoint/secret-reference/type configuration must fail
    # startup clearly, not silently continue with an unusable pipeline.
    # sys.exit here is a normal process-entrypoint exit on bad configuration
    # -- not a healing/mutation action (see gg_health_rules.py for what
    # active-healing exits were removed; this is not one of them).
    try:
        validate_enabled_runtimes(runtimes)
    except StartupValidationError as e:
        logger.error("startup validation failed: %s", e)
        sys.exit(1)

    enabled = [r for r in runtimes if r["enabled"]]
    logger.info("loaded %d runtime(s), %d enabled: %s", len(runtimes), len(enabled),
               [r["pipeline"] for r in enabled])
    logger.info("manager-compatible deployments.json equivalent: %s", build_deployments_json(runtimes))
    # Built ONCE across all enabled runtimes (concept C: process topology is
    # not a per-runtime field) and handed to every pipeline's own thread,
    # which filters it locally to its own deployment -- mirrors the
    # manager's own read-once/filter-per-deployment split exactly.
    full_process_pipeline_map = build_process_pipeline_map_json(runtimes)
    logger.info("manager-compatible process-pipeline-map.json equivalent: %s", full_process_pipeline_map)

    stop_event = threading.Event()
    ready_state = {}

    server = start_http_server(ready_state, [r["pipeline"] for r in enabled])

    threads = []
    for runtime in enabled:
        t = threading.Thread(
            target=run_pipeline,
            args=(runtime, stop_event, ready_state, full_process_pipeline_map),
            daemon=True)
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
