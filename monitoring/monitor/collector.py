"""collector.py: passive GoldenGate Admin REST poller and DynamoDB writer; never restarts/fences a process."""
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
import time
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
    """Only a trimmed, case-insensitive "true" parses to True; no permissive truthy-string aliases."""
    if raw is None:
        return False
    return str(raw).strip().lower() == "true"


# Hard CloudWatch kill switch: publishing requires BOTH this env var AND CONFIG.metricsEnabled.
CLOUDWATCH_PUBLISH_ENABLED = _parse_strict_bool_env(os.environ.get("CLOUDWATCH_PUBLISH_ENABLED"))


def cloudwatch_enabled_for(cfg):
    """Fail-closed by identity: only the literal Boolean True on both sides enables publication."""
    return CLOUDWATCH_PUBLISH_ENABLED is True and cfg.get("metricsEnabled") is True


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
    """Thread-safe leader/readiness state shared between one deployment's lease-control and polling loops."""

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


# Connect host and TLS server-name-to-verify differ by design: the shared wildcard cert's SAN matches Ingress, not *.svc.cluster.local.
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
    """Re-read each cycle so a rotated secret is picked up without a pod restart; never logs content."""
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

_DISCOVERY_STATUSES = ("OK", "EMPTY", "PARTIAL", "UNAVAILABLE", "INVALID_RESPONSE")
_ENDPOINT_STATUSES = ("OK", "EMPTY", "UNAVAILABLE", "INVALID_RESPONSE")
_INCOMPLETE_DISCOVERY_STATUSES = ("PARTIAL", "UNAVAILABLE", "INVALID_RESPONSE")
_VALID_ENDPOINT_STATUSES = ("OK", "EMPTY")

MAX_PROCESS_NAME_LENGTH = 128
_PROCESS_CONTROL_CHARS = frozenset(chr(c) for c in list(range(0x00, 0x20)) + [0x7f])


def _safe_process_name(raw):
    """A real, path-safe process name only; used for both STATE keys and detail-request URLs, never a fallback."""
    if not isinstance(raw, str):
        return None
    if not raw.strip():
        return None
    if len(raw) > MAX_PROCESS_NAME_LENGTH:
        return None
    if any(c in _PROCESS_CONTROL_CHARS for c in raw):
        return None
    if _has_surrogate_codepoint(raw):
        return None
    if "/" in raw or "\\" in raw:
        return None
    if raw in (".", ".."):
        return None
    return raw


def _process_detail_url(base, kind, name):
    """Encodes name as exactly one URL path segment; returns None for anything _safe_process_name would reject."""
    if _safe_process_name(name) is None:
        return None
    return f"{base}/services/v2/{kind}/{urllib.parse.quote(name, safe='')}"


def _normalize_status(raw):
    status = str(raw or "").upper()
    return status if status in _KNOWN_PROCESS_STATUSES else "UNKNOWN"


def _normalize_lag(raw):
    """A malformed or negative value degrades to 0.0 rather than aborting the tick."""
    try:
        lag = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return lag if lag > 0 else 0.0


def _valid_gg_inventory_shape(payload):
    """True iff payload.response.items is a list; an empty list is valid, any other shape is not."""
    if not isinstance(payload, dict):
        return False
    response = payload.get("response")
    if not isinstance(response, dict):
        return False
    return isinstance(response.get("items"), list)


def _fetch_gg_inventory(kind, url, opener):
    """Returns (endpoint_status, items) for one Extract/Replicat/Distribution inventory endpoint."""
    try:
        payload = _http_json(url, opener)
    except Exception as e:
        logger.warning("listing %s failed: %s", kind, type(e).__name__)
        return "UNAVAILABLE", []
    if not _valid_gg_inventory_shape(payload):
        logger.warning("listing %s returned an unexpected response shape", kind)
        return "INVALID_RESPONSE", []
    items = payload["response"]["items"]
    return ("OK" if items else "EMPTY"), items


def _collect_named_processes(base, kind, ptype, items, opener, seen, detail_failures):
    """Builds process dicts for one Extract/Replicat inventory; detail_failures[0] counts failed/unsafe detail fetches."""
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = _safe_process_name(it.get("name"))
        if name is None or (ptype, name) in seen:
            continue
        seen.add((ptype, name))
        detail = {}
        poll_status = "OK"
        detail_url = _process_detail_url(base, kind, name)
        if detail_url is None:
            poll_status = "DETAIL_UNAVAILABLE"
            detail_failures[0] += 1
        else:
            try:
                raw_detail = _http_json(detail_url, opener).get("response", {})
                if isinstance(raw_detail, dict):
                    detail = raw_detail
                else:
                    poll_status = "DETAIL_UNAVAILABLE"
                    detail_failures[0] += 1
            except Exception as e:
                logger.warning("detail fetch failed for %s process: %s", ptype, type(e).__name__)
                poll_status = "DETAIL_UNAVAILABLE"
                detail_failures[0] += 1
        # A failed detail fetch leaves detail={}, so status/lag fall back to the list item's own fields.
        status = _normalize_status(detail.get("status", it.get("status")))
        lag = _normalize_lag(detail.get("lag", detail.get("lagSeconds", it.get("lagSeconds", 0))))
        err = str(detail.get("lastError") or detail.get("error")
                  or detail.get("message") or "") if status == "ABENDED" else ""
        out.append({"process": name, "type": ptype, "lagSeconds": lag,
                    "abended": status == "ABENDED", "status": status,
                    "metrics": detail or {}, "error": err, "pollStatus": poll_status})
    return out


def discovery_counts(procs):
    counts = {"extract": 0, "replicat": 0, "distpath": 0}
    for p in procs:
        if p.get("type") in counts:
            counts[p["type"]] += 1
    return counts


def discover_processes(base, opener):
    """Structured Extract/Replicat/Distribution discovery that distinguishes a valid empty inventory from a failed one."""
    _http_json(f"{base}/services/v2/deployments", opener)  # liveness probe; a failure aborts discovery for this tick

    seen = set()
    detail_failures = [0]

    extracts_status, extract_items = _fetch_gg_inventory("extracts", f"{base}/services/v2/extracts", opener)
    replicats_status, replicat_items = _fetch_gg_inventory("replicats", f"{base}/services/v2/replicats", opener)
    sources_status, source_items = _fetch_gg_inventory("sources", f"{base}/services/v2/sources", opener)

    extract_procs = (_collect_named_processes(base, "extracts", "extract", extract_items, opener, seen, detail_failures)
                     if extracts_status in _VALID_ENDPOINT_STATUSES else [])
    replicat_procs = (_collect_named_processes(base, "replicats", "replicat", replicat_items, opener, seen, detail_failures)
                      if replicats_status in _VALID_ENDPOINT_STATUSES else [])

    distpath_procs = []
    if sources_status in _VALID_ENDPOINT_STATUSES:
        for it in source_items:
            if not isinstance(it, dict):
                continue
            name = _safe_process_name(it.get("name"))
            if name is None or ("distpath", name) in seen:
                continue
            seen.add(("distpath", name))
            status = _normalize_status(it.get("status"))
            bytes_now = next((it.get(k) for k in gh.BYTES_KEYS if it.get(k) is not None), None)
            distpath_procs.append({"process": name, "type": "distpath", "lagSeconds": 0.0,
                                   "abended": status == "ABENDED", "status": status,
                                   "bytes": bytes_now, "metrics": it or {},
                                   "error": "" if status != "ABENDED" else str(it.get("lastError") or ""),
                                   "pollStatus": "OK"})

    processes = extract_procs + replicat_procs + distpath_procs
    counts = discovery_counts(processes)
    detail_failure_count = detail_failures[0]

    core_both_valid = extracts_status in _VALID_ENDPOINT_STATUSES and replicats_status in _VALID_ENDPOINT_STATUSES
    core_either_valid = extracts_status in _VALID_ENDPOINT_STATUSES or replicats_status in _VALID_ENDPOINT_STATUSES

    if core_both_valid:
        if detail_failure_count > 0:
            combined = "PARTIAL"
        elif extract_procs or replicat_procs:
            combined = "OK"
        else:
            combined = "EMPTY"
    elif core_either_valid:
        combined = "PARTIAL"
    elif extracts_status == "INVALID_RESPONSE" or replicats_status == "INVALID_RESPONSE":
        combined = "INVALID_RESPONSE"  # malformed structure outranks a plain request failure when neither core inventory is usable
    else:
        combined = "UNAVAILABLE"

    return {
        "processes": processes,
        "status": combined,
        "collectedAt": cfgmod.now_epoch(),
        "extractCount": counts["extract"],
        "replicatCount": counts["replicat"],
        "distpathCount": counts["distpath"],
        "totalCount": counts["extract"] + counts["replicat"] + counts["distpath"],
        "extractsStatus": extracts_status,
        "replicatsStatus": replicats_status,
        "sourcesStatus": sources_status,
        "detailFailureCount": detail_failure_count,
    }


def fetch_gg_processes(base, opener):
    """Compatibility wrapper over discover_processes: returns only the flat process list."""
    return discover_processes(base, opener)["processes"]


def log_discovery_summary(pipeline, discovery):
    """One structured, non-sensitive log line per deployment tick; never logs the process payload itself."""
    logger.info(json.dumps({
        "event": "process_discovery_summary",
        "deployment": pipeline,
        "discoveryStatus": discovery.get("status"),
        "extractCount": discovery.get("extractCount", 0),
        "replicatCount": discovery.get("replicatCount", 0),
        "distpathCount": discovery.get("distpathCount", 0),
        "totalCount": discovery.get("totalCount", 0),
        "detailFailureCount": discovery.get("detailFailureCount", 0),
        "extractsStatus": discovery.get("extractsStatus"),
        "replicatsStatus": discovery.get("replicatsStatus"),
        "sourcesStatus": discovery.get("sourcesStatus"),
    }))
    if discovery.get("status") in _INCOMPLETE_DISCOVERY_STATUSES:
        logger.warning(json.dumps({
            "event": "process_discovery_incomplete",
            "deployment": pipeline,
            "discoveryStatus": discovery.get("status"),
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


# PMS collection (production, bounded): /heartbeat 404s live and is never called; /threadPerformance, /process are deferred.
PMS_INVENTORY_PATH = "/services/v2/mpoints/processes"
PMS_DETAIL_KINDS = ("processPerformance", "serviceHealth")
MAX_FOLLOWED_PMS_PROCESSES = 20

# Fixed safety net: caps each request and the whole pass so a slow PMS collection can never outlast the stale threshold.
PMS_REQUEST_TIMEOUT_SECONDS = 2
PMS_COLLECTION_BUDGET_SECONDS = 30

# Mirrors (does not import) the contract-probe tool's bound: an oversized PMS response is never parsed, sized, or logged.
PMS_MAX_RESPONSE_BYTES = 2 * 1024 * 1024

PMS_ERROR_CATEGORIES = (
    "OK", "PARTIAL", "UNAVAILABLE", "AUTH_FAILED", "TLS_FAILED",
    "ENDPOINT_UNAVAILABLE", "INVALID_RESPONSE",
)

_PMS_CONTROL_CHARS = frozenset(chr(c) for c in list(range(0x00, 0x20)) + [0x7f])

# Fixed safety limit, not a tuning knob: a name longer than this is skipped entirely (see _valid_pms_process_name).
MAX_PMS_PROCESS_NAME_LENGTH = 128

# Fixed safety bound within DynamoDB Number precision and IEEE-754 exact-integer range; out-of-range values become 0.
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
    """Like _http_json but reads at most max_bytes+1 bytes; an oversized response is never parsed. PMS-only."""
    with opener.open(url, timeout=timeout) as resp:
        raw = resp.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("PMS response body exceeds the bounded limit")
    return json.loads(raw.decode())


def _has_surrogate_codepoint(s):
    """True if s contains a lone Unicode surrogate code point, which cannot be UTF-8 encoded."""
    return any(0xD800 <= ord(c) <= 0xDFFF for c in s)


def _valid_pms_process_name(raw):
    """Returns raw unmodified if it's a safe, bounded PMS process name (no control chars, not "." or ".."), else None."""
    if not isinstance(raw, str):
        return None
    if not raw.strip():
        return None
    if len(raw) > MAX_PMS_PROCESS_NAME_LENGTH:
        return None
    if any(c in _PMS_CONTROL_CHARS for c in raw):
        return None
    if _has_surrogate_codepoint(raw):
        return None
    if raw in (".", ".."):
        return None
    return raw


def _normalize_pms_number(raw):
    """Malformed, NaN, infinite, negative, boolean, or out-of-range input all become 0; never raises."""
    if isinstance(raw, bool):
        return 0
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):  # OverflowError: e.g. float(10**400) on a huge raw int
        return 0
    if not math.isfinite(value) or value < 0 or value > PMS_MAX_SAFE_NUMBER:
        return 0
    return int(value) if value == int(value) else value


def normalize_pms_inventory_item(raw):
    """Only the confirmed inventory fields, safe types only; unknown fields ignored, missing fields absent."""
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
    """Only the confirmed numeric processPerformance fields; cumulative counters preserved as-is, never rated."""
    if not isinstance(raw, dict):
        return {}
    return {key: _normalize_pms_number(raw[key])
           for key in _PMS_PERFORMANCE_NUMERIC_FIELDS if key in raw}


def normalize_pms_service_health(raw):
    """isHealthy/criticalResourcesHealthy/criticalResourcesUnhealthy with safe defaults; isHealthy fails closed."""
    if not isinstance(raw, dict):
        return {"isHealthy": False, "criticalResourcesHealthy": 0, "criticalResourcesUnhealthy": 0}
    is_healthy = raw.get("isHealthy")
    return {
        "isHealthy": is_healthy if isinstance(is_healthy, bool) else False,
        "criticalResourcesHealthy": _normalize_pms_number(raw.get("criticalResourcesHealthy", 0)),
        "criticalResourcesUnhealthy": _normalize_pms_number(raw.get("criticalResourcesUnhealthy", 0)),
    }


def heartbeat_age_seconds(last_heartbeat, now=None):
    """Non-negative age in seconds, or None if malformed/naive; never raises. A future timestamp clamps to 0."""
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
    """True iff payload.response.processes is a list; an empty list is valid, a wrong shape is INVALID_RESPONSE."""
    if not isinstance(payload, dict):
        return False
    response = payload.get("response")
    if not isinstance(response, dict):
        return False
    return isinstance(response.get("processes"), list)


def _pms_valid_process_names(payload):
    """Returns (unique valid processNames in first-seen order, raw response.processes length pre-dedup)."""
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
    """Encodes name as one URL path segment; returns None for anything _valid_pms_process_name would reject."""
    if _valid_pms_process_name(name) is None:
        return None
    try:
        encoded = urllib.parse.quote(name, safe="")
    except UnicodeEncodeError:
        return None
    return f"/services/v2/mpoints/{encoded}/{kind}"


def _valid_pms_performance_shape(response):
    """Must be a dict with at least one confirmed numeric field, else counted as a failed detail request."""
    return isinstance(response, dict) and any(k in response for k in _PMS_PERFORMANCE_NUMERIC_FIELDS)


def _valid_pms_service_health_shape(response):
    """Must be a dict whose isHealthy field is a literal bool, else counted as a failed detail request."""
    return isinstance(response, dict) and isinstance(response.get("isHealthy"), bool)


def _contains_pms_tls_error(exc, max_nodes=10):
    """Bounded, cycle-safe search for an ssl.SSLError anywhere in exc's chain; classification only, never raw text."""
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
    """Bounded, closed classification for a PMS request failure; never returns the raw exception text."""
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
    """A sanitized, empty PMS snapshot for a tick where PMS collection didn't run; collectedAt is always now."""
    return {
        "status": status, "collectedAt": cfgmod.now_epoch(),
        "inventoryCount": 0, "followedCount": 0, "successCount": 0, "failureCount": 0,
        "heartbeatAgeSeconds": None, "processes": {},
    }


def _collect_pms_impl(base, opener, now=None, clock=time.monotonic,
                      budget_seconds=PMS_COLLECTION_BUDGET_SECONDS):
    """One bounded PMS pass: GETs inventory once, follows up to MAX_FOLLOWED_PMS_PROCESSES with detail GETs, stopping once the budget_seconds deadline passes."""
    deadline = clock() + budget_seconds
    collected_at = cfgmod.now_epoch()

    def _next_timeout():
        remaining = deadline - clock()
        if remaining <= 0:
            return None  # budget exhausted -- caller must not issue this request
        return min(PMS_REQUEST_TIMEOUT_SECONDS, remaining)

    inv_timeout = _next_timeout()
    if inv_timeout is None:
        return {
            "status": "UNAVAILABLE", "collectedAt": collected_at,
            "inventoryCount": 0, "followedCount": 0, "successCount": 0, "failureCount": 0,
            "heartbeatAgeSeconds": None, "processes": {},
        }

    try:
        inventory_payload = _http_json_bounded(f"{base}{PMS_INVENTORY_PATH}", opener, timeout=inv_timeout)
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
        if _next_timeout() is None:
            break  # budget exhausted -- stop issuing further PMS requests

        inv_norm = normalize_pms_inventory_item(inventory_by_name.get(name, {}))
        age = heartbeat_age_seconds(inv_norm.get("lastHeartbeat"), now=now)
        if age is not None:
            ages.append(age)

        perf, health, process_ok = {}, {}, True
        budget_exhausted = False
        for kind in PMS_DETAIL_KINDS:
            timeout = _next_timeout()
            if timeout is None:
                # Budget exhausted: counts the same as a network failure for PARTIAL/UNAVAILABLE purposes.
                process_ok = False
                detail_failure_count += 1
                budget_exhausted = True
                break  # stop issuing further PMS requests for this process and beyond
            path = _pms_detail_path(name, kind)
            if path is None:
                process_ok = False
                detail_failure_count += 1
                continue
            try:
                detail_payload = _http_json_bounded(f"{base}{path}", opener, timeout=timeout)
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

        if budget_exhausted:
            break

    # Processes that never got a turn before the budget tripped count as failures too.
    unattempted = len(followed) - len(processes_out)
    if unattempted > 0:
        detail_failure_count += unattempted * len(PMS_DETAIL_KINDS)
        failure_count += unattempted

    followed_count = len(followed)
    if followed_count == 0:
        status = "OK"  # valid inventory shape, simply nothing to follow this tick
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


def collect_pms(base, opener, now=None, clock=time.monotonic,
                budget_seconds=PMS_COLLECTION_BUDGET_SECONDS):
    """Public entry point: a pure network/normalization operation, no DynamoDB/CloudWatch I/O, and never raises."""
    try:
        return _collect_pms_impl(base, opener, now=now, clock=clock, budget_seconds=budget_seconds)
    except Exception:
        return {
            "status": "INVALID_RESPONSE", "collectedAt": cfgmod.now_epoch(),
            "inventoryCount": 0, "followedCount": 0, "successCount": 0, "failureCount": 0,
            "heartbeatAgeSeconds": None, "processes": {},
        }


_LAG_METRIC_BY_PROCESS_TYPE = {"extract": "ExtractLagSeconds", "replicat": "ReplicatLagSeconds"}


def build_metric_batch(pipeline, deployment_type, flags, procs=None,
                       critical_service_status=None, abend_events=None, heartbeat_ok=False,
                       process_inventory_complete=True):
    """Pure builder for the full metric contract; process_inventory_complete=False omits LagBreached/AbendFailure only."""
    procs = procs or []
    critical_service_status = critical_service_status or {}
    abend_events = abend_events or ()

    dep_dims = [{"Name": "Deployment", "Value": pipeline}, {"Name": "DeploymentType", "Value": deployment_type}]
    md = [{"MetricName": "DeploymentDown", "Dimensions": dep_dims, "Value": float(flags.get("down", 0)), "Unit": "Count"}]
    if process_inventory_complete:
        md.append({"MetricName": "LagBreached", "Dimensions": dep_dims,
                   "Value": float(flags.get("lag", 0)), "Unit": "Count"})
        md.append({"MetricName": "AbendFailure", "Dimensions": dep_dims,
                   "Value": float(flags.get("abend", 0)), "Unit": "Count"})

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


def publish_metric_batch(cw, metric_data, pipeline=None):
    """The only boto3-calling half of metric emission; a PutMetricData failure is swallowed, sanitized, no retries."""
    if not cw or not metric_data:
        return
    batches = [metric_data[i:i + 20] for i in range(0, len(metric_data), 20)]  # PutMetricData max 20/call
    for batch_index, batch in enumerate(batches):
        try:
            cw.put_metric_data(Namespace=CLOUDWATCH_NAMESPACE, MetricData=batch)
        except Exception as exc:
            logger.error(json.dumps({
                "event": "cloudwatch_put_metric_data_failed",
                "deployment": pipeline,
                "metricCount": len(batch),
                "batchIndex": batch_index,
                "batchCount": len(batches),
                "errorCategory": type(exc).__name__,
            }))


def publish_metrics_if_enabled(cfg, pipeline, metric_data):
    """The single protected publication boundary; re-checks the double gate itself and never lets a client-construction failure escape."""
    if not cloudwatch_enabled_for(cfg):
        return
    try:
        cw = _cloudwatch_client()
    except Exception as exc:
        logger.error(json.dumps({
            "event": "cloudwatch_client_creation_failed",
            "deployment": pipeline,
            "errorCategory": type(exc).__name__,
        }))
        return
    publish_metric_batch(cw, metric_data, pipeline=pipeline)


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
                "resolvedThreshold", "resolvedMode", "pipelineName", "errorMsg", "pollStatus"):
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
    if "processDiscovery" in snapshot:
        sets.append("processDiscovery=:pd")
        vals[":pd"] = _ddb_safe(snapshot.get("processDiscovery") or {})
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
    """Returns (ok, reason); reason is always a fixed, generic, safe-to-log string, excludes Admin REST reachability."""
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
                # Fail closed: never guess credentials, poll, or write STATE this tick.
                state.set_credentials_ok(False)
                logger.warning("admin credentials unavailable for %s; skipping this tick", pipeline)
                _sleep_watching_leadership(interval)
                continue
            state.set_credentials_ok(True)

            ssl_ctx = _build_ssl_context()
            opener = _basic_opener(user, pwd, base, ssl_ctx, tls_server_name)

            try:
                discovery = discover_processes(base, opener)
            except Exception as e:
                in_grace = (cfgmod.now_epoch() - started) < cfg["startupGraceSeconds"]
                status = "STARTING" if in_grace else "DEPLOYMENT_DOWN"
                if not in_grace and cfg["alertsEnabled"]:
                    flags["down"] = 1
                transitioned = (status != last_dep_status)
                dep_snap = {"processType": "deployment", "status": status, "recordedAt": cfgmod.now_epoch()}
                # PMS isn't attempted here; always overwrite any stale pms map with a current UNAVAILABLE snapshot.
                dep_snap["pms"] = _pms_unavailable_snapshot("ENDPOINT_UNAVAILABLE")
                if transitioned:
                    dep_snap["lastTransitionAt"] = cfgmod.now_epoch()
                _guarded_write("_deployment", dep_snap)
                last_dep_status = status
                logger.warning("GoldenGate Admin REST unreachable for %s (%s): %s", pipeline, status, e)
                # Write succeeded so heartbeat fires, but discovery never ran this tick -- never a healthy 0.
                metric_data = build_metric_batch(pipeline, deployment_type, flags, heartbeat_ok=True,
                                                 process_inventory_complete=False)
                publish_metrics_if_enabled(cfg, pipeline, metric_data)
                _sleep_watching_leadership(interval)
                continue

            procs = discovery["processes"]
            log_discovery_summary(pipeline, discovery)
            process_inventory_complete = discovery["status"] in _VALID_ENDPOINT_STATUSES
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
                        "performanceMetrics": p.get("metrics") or {},
                        "pollStatus": str(p.get("pollStatus", "OK"))}
                if str(prev.get("status")) != status:
                    snap["lastTransitionAt"] = cfgmod.now_epoch()
                try:
                    _guarded_write(name, snap, counters=counters)
                except _FencedOff:
                    raise
                except Exception:
                    logger.exception("process %s evaluation failed; skipped", name)

            # cfg["criticalServices"] already resolved any CONFIG override -- see resolve_critical_services.
            critical = cfg["criticalServices"]
            svc_up = probe_critical_services(base, opener, critical)
            cs_new = {svc: {"reachable": bool(up)} for svc, up in svc_up.items()}

            # PMS is additional observability only; a PMS failure must never mark the deployment DOWN.
            try:
                pms_result = collect_pms(base, opener)
            except Exception:
                logger.warning("PMS collection unavailable for %s; using sanitized current-tick state", pipeline)
                pms_result = _pms_unavailable_snapshot("UNAVAILABLE")

            # Only the sanitized summary fields are persisted -- never process names, raw bodies, or URLs.
            discovery_snapshot = {k: discovery[k] for k in (
                "status", "collectedAt", "extractsStatus", "replicatsStatus", "sourcesStatus",
                "extractCount", "replicatCount", "distpathCount", "totalCount", "detailFailureCount")}

            transitioned = ("UP" != last_dep_status)
            dep_snap = {"processType": "deployment", "status": "UP",
                        "recordedAt": cfgmod.now_epoch(), "criticalServices": cs_new,
                        "pms": pms_result, "processDiscovery": discovery_snapshot}
            if transitioned:
                dep_snap["lastTransitionAt"] = cfgmod.now_epoch()
            _guarded_write("_deployment", dep_snap)
            last_dep_status = "UP"

            # The _deployment write above just succeeded -- heartbeat fires.
            metric_data = build_metric_batch(pipeline, deployment_type, flags, procs=procs,
                                             critical_service_status=svc_up,
                                             abend_events=abend_event_names, heartbeat_ok=True,
                                             process_inventory_complete=process_inventory_complete)
            publish_metrics_if_enabled(cfg, pipeline, metric_data)

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
    """Sets up dedicated Table/LeaseManager pairs (not safe to share across threads) and runs both loops."""
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
