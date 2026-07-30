"""collector.py: passive GoldenGate Admin REST poller and DynamoDB writer.

Owns LEASE acquisition/renewal and recordType=STATE#_deployment /
STATE#<process> writes -- one lease per deployment, renewed on its own
cadence independent of the poll interval. Never restarts, stops, or fences
a GoldenGate process, and never calls a Kubernetes API.

PMS collection (production, bounded): once per successful leader tick, the
same authenticated/TLS-verified HTTPS adminPort 8443 opener used for the
rest of Admin REST polling is reused to GET the confirmed process inventory
(/services/v2/mpoints/processes) exactly once, then up to 20 unique,
deduplicated processName values are followed with sequential, bounded
processPerformance + serviceHealth GETs only. Heartbeat age is derived from
inventory.lastHeartbeat -- the /heartbeat endpoint returned 404 in the
validated live environment and is never called. /threadPerformance,
/process, /services/v2/monitoring/statusChanges, /services/v2/metrics, and
direct authenticated HTTP port 9015 are never used by this production path
either. A PMS failure is recorded as its own bounded, sanitized status
(collect_pms never raises) and never marks an otherwise-healthy Admin REST
deployment DOWN. The result is folded into the existing guarded/fenced
STATE#_deployment write only -- no new DynamoDB table, recordType, or
per-PMS-process STATE# row is created.
"""
from __future__ import annotations

import functools
import http.client
import json
import logging
import math
import os
import secrets as _secrets
import socket
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

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


# ---------------------------------------------------------------------------
# PMS collection (production, bounded) -- see module docstring for the full
# request-model summary. Live-confirmed contract only: /heartbeat 404s in
# the validated environment and is never called; /threadPerformance and
# /process are intentionally not polled (redundant with inventory / high
# cardinality, deferred).
# ---------------------------------------------------------------------------

PMS_INVENTORY_PATH = "/services/v2/mpoints/processes"
PMS_DETAIL_KINDS = ("processPerformance", "serviceHealth")
MAX_FOLLOWED_PMS_PROCESSES = 20

# Mirrors (does not import -- collector.py must not depend on tools/) the
# contract-probe tool's proven bound: an oversized PMS response is never
# parsed, sized, or logged.
PMS_MAX_RESPONSE_BYTES = 2 * 1024 * 1024

PMS_ERROR_CATEGORIES = (
    "OK", "PARTIAL", "UNAVAILABLE", "AUTH_FAILED", "TLS_FAILED",
    "ENDPOINT_UNAVAILABLE", "INVALID_RESPONSE",
)

_PMS_CONTROL_CHARS = frozenset(chr(c) for c in list(range(0x00, 0x20)) + [0x7f])

# Production PMS process-name bound: not a runtime tuning knob, a fixed
# safety limit. A name longer than this is skipped entirely rather than
# truncated -- see _valid_pms_process_name.
MAX_PMS_PROCESS_NAME_LENGTH = 128

# Comfortably within DynamoDB's Number precision (up to 38 digits) and far
# beyond any plausible value for a PMS counter/byte-count field -- a fixed,
# documented safety bound, not a real-world limit. A value outside
# [0, PMS_MAX_SAFE_NUMBER] becomes 0 rather than ever being stored. Kept at
# or below 2**53 (IEEE-754 double's exact-integer range) so the boundary
# itself is never blurred by float rounding -- one quadrillion is already
# far beyond any real cumulative counter/byte-count value.
PMS_MAX_SAFE_NUMBER = 10 ** 15

_PMS_INVENTORY_STRING_FIELDS = (
    "processName", "processType", "processMode", "processState",
    "startTime", "stateTime", "lastHeartbeat",
)
_PMS_INVENTORY_NUMERIC_FIELDS = ("processId", "portNumber", "firstMessage", "lastMessage")

_PMS_PERFORMANCE_NUMERIC_FIELDS = (
    "cpuTimeUs", "kernelTimeUs", "userTimeUs", "workingSetSize", "peakWorkingSetSize",
    "privateBytes", "threadCount", "handleCount", "pageFaults",
    "ioReadBytes", "ioReadCount", "ioWriteBytes", "ioWriteCount",
    "ioOtherBytes", "ioOtherCount", "processStartTime", "processId",
)

_PMS_SERVICE_HEALTH_FIELDS = ("isHealthy", "criticalResourcesHealthy", "criticalResourcesUnhealthy")


def _http_json_bounded(url, opener, timeout=5, max_bytes=PMS_MAX_RESPONSE_BYTES):
    """Like _http_json but the body is read to at most max_bytes+1 bytes --
    an oversized response is never parsed. PMS-request-only; the existing
    extract/replicat/distpath discovery path (_http_json) is unchanged."""
    with opener.open(url, timeout=timeout) as resp:
        raw = resp.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("PMS response body exceeds the bounded limit")
    return json.loads(raw.decode())


def _valid_pms_process_name(raw):
    """A production-safe PMS process name: a string with at least one
    non-whitespace character, no longer than MAX_PMS_PROCESS_NAME_LENGTH,
    containing no ASCII control character, and never '.' or '..' (the only
    values urllib.parse.quote(safe="") leaves unescaped that could act as a
    traversal-equivalent path segment). Returns the name EXACTLY as given
    when valid -- never rewritten, stripped, or truncated -- or None to
    signal "skip this item" when invalid."""
    if not isinstance(raw, str):
        return None
    if not raw.strip():
        return None
    if len(raw) > MAX_PMS_PROCESS_NAME_LENGTH:
        return None
    if any(c in _PMS_CONTROL_CHARS for c in raw):
        return None
    if raw in (".", ".."):
        return None
    return raw


def _normalize_pms_number(raw):
    """A PMS number is never allowed to become an exception or a silently
    wrong type: malformed, NaN, infinite, negative, boolean, or
    out-of-DynamoDB-safe-range input all become 0 rather than propagating
    or overflowing. Cumulative counters within the safe range are preserved
    exactly -- never converted into a rate/percentage."""
    if isinstance(raw, bool):
        return 0
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: e.g. float(10**400) on a huge raw int -- Python
        # raises rather than returning inf for int->float, unlike the
        # string-parsing path below.
        return 0
    if not math.isfinite(value) or value < 0 or value > PMS_MAX_SAFE_NUMBER:
        return 0
    return int(value) if value == int(value) else value


def normalize_pms_inventory_item(raw):
    """Bounded, pure: only the confirmed inventory fields, safe types only.
    Unknown fields are ignored; missing fields are simply absent (never
    invented)."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key in _PMS_INVENTORY_STRING_FIELDS:
        value = raw.get(key)
        if isinstance(value, str):
            out[key] = value
    for key in _PMS_INVENTORY_NUMERIC_FIELDS:
        if key in raw:
            out[key] = _normalize_pms_number(raw[key])
    return out


def normalize_pms_performance(raw):
    """Bounded, pure: only the confirmed numeric processPerformance fields.
    cpuTimeUs/kernelTimeUs/userTimeUs are cumulative counters -- preserved
    as-is here, never converted to a rate/percentage (that needs two
    validated time samples and an approved manager contract, neither of
    which exist yet)."""
    if not isinstance(raw, dict):
        return {}
    return {key: _normalize_pms_number(raw[key])
           for key in _PMS_PERFORMANCE_NUMERIC_FIELDS if key in raw}


def normalize_pms_service_health(raw):
    """Bounded, pure: {isHealthy, criticalResourcesHealthy,
    criticalResourcesUnhealthy} with safe defaults. isHealthy fails closed
    to False for anything that isn't literally a bool (never silently
    accepts a truthy non-boolean as healthy)."""
    if not isinstance(raw, dict):
        return {"isHealthy": False, "criticalResourcesHealthy": 0, "criticalResourcesUnhealthy": 0}
    is_healthy = raw.get("isHealthy")
    return {
        "isHealthy": is_healthy if isinstance(is_healthy, bool) else False,
        "criticalResourcesHealthy": _normalize_pms_number(raw.get("criticalResourcesHealthy", 0)),
        "criticalResourcesUnhealthy": _normalize_pms_number(raw.get("criticalResourcesUnhealthy", 0)),
    }


def heartbeat_age_seconds(last_heartbeat, now=None):
    """Pure, timezone-aware. Returns a non-negative int age in seconds, or
    None when last_heartbeat is missing/malformed -- never raises, never
    logs the raw value. now is injectable (defaults to real current UTC
    time) for deterministic tests. A naive (timezone-less) timestamp is
    treated as unusable rather than assumed to be local or UTC. A future
    timestamp clamps to age 0 rather than going negative."""
    if not isinstance(last_heartbeat, str) or not last_heartbeat.strip():
        return None
    raw = last_heartbeat.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    now = now if now is not None else datetime.now(timezone.utc)
    age = (now - parsed).total_seconds()
    return 0 if age < 0 else int(age)


def _valid_pms_inventory_shape(payload):
    """True only when payload is a dict whose "response" is a dict whose
    "processes" is a list -- the three required shape checks. An empty
    processes list IS a valid shape (status OK, zero followed); anything
    else about the shape being wrong (missing/null/non-dict response,
    non-list processes, or a non-dict top level) is not, and must be
    classified INVALID_RESPONSE rather than silently treated as empty."""
    if not isinstance(payload, dict):
        return False
    response = payload.get("response")
    if not isinstance(response, dict):
        return False
    return isinstance(response.get("processes"), list)


def _pms_valid_process_names(payload):
    """Returns (names, inventory_item_count): unique, production-safe
    processName strings from response.processes, first-seen order
    preserved -- never processId, never any other field. Only call this
    after _valid_pms_inventory_shape confirms the shape. Non-dict items,
    items with no valid processName (see _valid_pms_process_name), and
    repeat names are skipped. inventory_item_count is the TRUE raw
    response.processes list length, unaffected by validity or dedup."""
    if not isinstance(payload, dict):
        return [], 0
    response = payload.get("response")
    if not isinstance(response, dict):
        return [], 0
    processes = response.get("processes")
    if not isinstance(processes, list):
        return [], 0
    names, seen = [], set()
    for item in processes:
        if not isinstance(item, dict):
            continue
        name = _valid_pms_process_name(item.get("processName"))
        if name is not None and name not in seen:
            seen.add(name)
            names.append(name)
    return names, len(processes)


def _pms_detail_path(name, kind):
    """Encodes name as exactly one URL path segment. Returns None (skip)
    for any name _valid_pms_process_name itself would reject -- defense in
    depth, since callers already filter through that function first."""
    if _valid_pms_process_name(name) is None:
        return None
    encoded = urllib.parse.quote(name, safe="")
    return f"/services/v2/mpoints/{encoded}/{kind}"


def _valid_pms_performance_shape(response):
    """A processPerformance response must be a dict containing at least
    one of the confirmed numeric fields -- missing, null, scalar, list, or
    empty all fail this check (counted as a failed detail request, never a
    silent success with an empty performance map)."""
    return isinstance(response, dict) and any(k in response for k in _PMS_PERFORMANCE_NUMERIC_FIELDS)


def _valid_pms_service_health_shape(response):
    """A serviceHealth response must be a dict containing at least one of
    the confirmed fields -- see _valid_pms_performance_shape."""
    return isinstance(response, dict) and any(k in response for k in _PMS_SERVICE_HEALTH_FIELDS)


def _contains_pms_tls_error(exc, max_nodes=10):
    """Bounded, cycle-safe search for an ssl.SSLError (ssl.SSLCertVerificationError
    subclasses it) anywhere in exc's chain: the exception itself,
    urllib.error.URLError.reason, and __cause__/__context__ at every node.
    Mirrors (does not import -- collector.py must not depend on tools/) the
    contract-probe tool's proven equivalent. Never inspects or returns the
    raw exception text -- classification only."""
    if exc is None:
        return False
    seen_ids = set()
    stack = [exc]
    checked = 0
    while stack and checked < max_nodes:
        current = stack.pop()
        if current is None:
            continue
        cid = id(current)
        if cid in seen_ids:
            continue
        seen_ids.add(cid)
        checked += 1
        if isinstance(current, ssl.SSLError):
            return True
        reason = getattr(current, "reason", None)
        if isinstance(reason, BaseException):
            stack.append(reason)
        cause = getattr(current, "__cause__", None)
        if isinstance(cause, BaseException):
            stack.append(cause)
        context = getattr(current, "__context__", None)
        if isinstance(context, BaseException):
            stack.append(context)
    return False


def _classify_pms_error(exc):
    """Bounded, closed classification for a PMS request failure. Never
    inspects or returns the raw exception text."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return "AUTH_FAILED"
        return "ENDPOINT_UNAVAILABLE"
    if _contains_pms_tls_error(exc):
        return "TLS_FAILED"
    if isinstance(exc, ValueError):
        return "INVALID_RESPONSE"
    return "ENDPOINT_UNAVAILABLE"


def _pms_unavailable_snapshot(status):
    """A current, sanitized, empty PMS snapshot for a tick where PMS
    collection did not run at all (Admin REST itself is unreachable) or
    where collect_pms raised unexpectedly. collectedAt is always "now" for
    THIS tick -- a stale snapshot from a prior successful tick must never
    survive unattributed to a new one."""
    return {
        "status": status, "collectedAt": cfgmod.now_epoch(),
        "inventoryCount": 0, "followedCount": 0, "successCount": 0, "failureCount": 0,
        "heartbeatAgeSeconds": None, "processes": {},
    }


def collect_pms(base, opener, now=None):
    """One bounded PMS collection pass for this tick. Reuses the caller's
    already-authenticated, TLS-verified opener. GETs the confirmed process
    inventory once; follows up to MAX_FOLLOWED_PMS_PROCESSES unique,
    deduplicated, production-safe processName values with sequential,
    bounded processPerformance + serviceHealth GETs only. Never raises --
    always returns a bounded, sanitized summary dict; the caller decides
    whether to persist it (this function performs no DynamoDB I/O and does
    not know about lease/fencing -- it is a pure network/normalization
    operation only). Never logs a raw exception, response body, or process
    name anywhere in this function.

    A structurally invalid inventory response (not the three required
    shape checks) is INVALID_RESPONSE -- distinct from a genuinely empty
    processes list, which is a valid OK result. status is derived from
    whether any individual detail GET actually succeeded this tick, not
    merely from whether a process's BOTH details succeeded -- so a tick
    where every process got exactly one of its two details is correctly
    PARTIAL, never UNAVAILABLE. An individual detail failure (network error
    or a malformed/empty response shape) only affects that one detail call
    -- the remaining calls and processes are still attempted."""
    collected_at = cfgmod.now_epoch()

    try:
        inventory_payload = _http_json_bounded(f"{base}{PMS_INVENTORY_PATH}", opener)
    except Exception as e:
        return {
            "status": _classify_pms_error(e), "collectedAt": collected_at,
            "inventoryCount": 0, "followedCount": 0, "successCount": 0, "failureCount": 0,
            "heartbeatAgeSeconds": None, "processes": {},
        }

    if not _valid_pms_inventory_shape(inventory_payload):
        return {
            "status": "INVALID_RESPONSE", "collectedAt": collected_at,
            "inventoryCount": 0, "followedCount": 0, "successCount": 0, "failureCount": 0,
            "heartbeatAgeSeconds": None, "processes": {},
        }

    names, inventory_count = _pms_valid_process_names(inventory_payload)
    inventory_by_name = {}
    for item in inventory_payload["response"]["processes"]:
        if isinstance(item, dict) and isinstance(item.get("processName"), str):
            inventory_by_name.setdefault(item["processName"], item)

    followed = names[:MAX_FOLLOWED_PMS_PROCESSES]
    processes_out = {}
    success_count = 0
    failure_count = 0
    detail_success_count = 0
    detail_failure_count = 0
    ages = []

    for name in followed:
        inv_norm = normalize_pms_inventory_item(inventory_by_name.get(name, {}))
        age = heartbeat_age_seconds(inv_norm.get("lastHeartbeat"), now=now)
        if age is not None:
            ages.append(age)

        perf, health, process_ok = {}, {}, True
        for kind in PMS_DETAIL_KINDS:
            path = _pms_detail_path(name, kind)
            if path is None:
                process_ok = False
                detail_failure_count += 1
                continue
            try:
                detail_payload = _http_json_bounded(f"{base}{path}", opener)
            except Exception:
                process_ok = False
                detail_failure_count += 1
                continue
            detail_response = detail_payload.get("response") if isinstance(detail_payload, dict) else None
            if kind == "processPerformance":
                if not _valid_pms_performance_shape(detail_response):
                    process_ok = False
                    detail_failure_count += 1
                    continue
                perf = normalize_pms_performance(detail_response)
            else:
                if not _valid_pms_service_health_shape(detail_response):
                    process_ok = False
                    detail_failure_count += 1
                    continue
                health = normalize_pms_service_health(detail_response)
            detail_success_count += 1

        if process_ok:
            success_count += 1
        else:
            failure_count += 1
        processes_out[name] = {"performance": perf, "serviceHealth": health, "heartbeatAgeSeconds": age}
        # detail_payload/inv_norm fall out of scope here -- never retained.

    followed_count = len(followed)
    if followed_count == 0:
        # Inventory GET succeeded with a valid shape; there is simply
        # nothing (valid) to follow this tick -- OK, not UNAVAILABLE.
        status = "OK"
    elif detail_success_count == 0:
        status = "UNAVAILABLE"  # zero detail GETs succeeded this tick
    elif detail_failure_count > 0:
        status = "PARTIAL"
    else:
        status = "OK"

    return {
        "status": status, "collectedAt": collected_at,
        "inventoryCount": inventory_count, "followedCount": followed_count,
        "successCount": success_count, "failureCount": failure_count,
        "heartbeatAgeSeconds": max(ages) if ages else None,
        "processes": processes_out,
    }


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
    if "pms" in snapshot:
        sets.append("pms=:pms")
        vals[":pms"] = _ddb_safe(snapshot.get("pms") or {})
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
                # PMS depends on the same Admin REST connectivity that just
                # failed, so it is not attempted -- but a prior successful
                # tick's pms map must never be left attached looking current.
                # Overwrite it with a sanitized, current-tick ENDPOINT_UNAVAILABLE
                # snapshot every time, in the same guarded write.
                dep_snap["pms"] = _pms_unavailable_snapshot("ENDPOINT_UNAVAILABLE")
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

            # PMS is additional observability only: collect_pms never raises
            # and its result never influences the deployment's own UP status
            # above -- a PMS failure must not mark an otherwise-healthy
            # Admin REST deployment DOWN. Belt-and-suspenders try/except in
            # case of an unexpected bug in this still-new code path -- even
            # then, a CURRENT sanitized snapshot is written, never a stale
            # one left over from a prior successful tick, and never a raw
            # exception/traceback in the log.
            try:
                pms_result = collect_pms(base, opener)
            except Exception:
                logger.warning("PMS collection unavailable for %s; using sanitized current-tick state", pipeline)
                pms_result = _pms_unavailable_snapshot("UNAVAILABLE")

            transitioned = ("UP" != last_dep_status)
            dep_snap = {"processType": "deployment", "status": "UP",
                        "recordedAt": cfgmod.now_epoch(), "criticalServices": cs_new,
                        "pms": pms_result}
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
