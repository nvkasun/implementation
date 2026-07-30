"""gg_api_contract_probe.py: standalone, read-only GoldenGate Admin/Metrics
REST contract-probe tool for controlled, operator-invoked use only.

Never run automatically -- not imported by monitor.py or collector.py, not
exposed through the portal or any HTTP endpoint, not part of the startup
path. Invoke manually, e.g.:

    kubectl exec -n goldengate-monitoring <pod> -- \\
        python3 tools/gg_api_contract_probe.py \\
        --deployment gg-oracle-payments-01 --port admin \\
        --path /services/v2/mpoints/processes

It performs exactly one read-only GET and prints sanitized STRUCTURAL
metadata only (top-level keys, per-collection item count, per-field names
and JSON types) -- never a raw field value, process name, status value, ID,
credential, secret ARN, path, hostname, stack trace, or full URL. It never
writes DynamoDB, never publishes a CloudWatch metric, and never issues any
request that could modify a GoldenGate deployment.

CONFIRMED secure PMS routes (live-environment verified on both Oracle and
PostgreSQL -- HTTP 200): always reached with --port admin (HTTPS through
adminPort 8443, authenticated with the same CSI-mounted credentials, CA
chain, and TLS/SNI verification the collector itself uses):

    /services/v2/mpoints/processes         -> response.processes
    /services/v2/monitoring/statusChanges  -> response.statusChange

Direct metricsPort 9015 is CONFIRMED PLAIN HTTP in the current deployment
and is NOT an approved authenticated collection path. --port metrics issues
a plain, UNAUTHENTICATED HTTP request only -- the mounted admin credentials
are never read, built into an opener, or transmitted for a metrics-port
request. There is no automatic HTTPS<->HTTP fallback: the scheme is a fixed
function of --port, chosen explicitly by the operator on each invocation.

/services/v2/metrics is CONFIRMED INVALID in the live environment (HTTP
404) -- it is NOT the production PMS endpoint and must never be used as a
recommended example. The path remains generically accepted, like any other
/services/... path, purely for ad hoc diagnostic compatibility (e.g.
confirming it still 404s); its response is never used to inform STATE# or
CloudWatch logic anywhere in this application, and no speculative PMS
parser exists for it.

--follow-processes: manual, structural-only per-process detail capture.
Also invoked manually only, also never run during normal monitor startup,
also never exposed through the portal. GETs the confirmed process
inventory (/services/v2/mpoints/processes) once, then issues up to
MAX_FOLLOWED_PROCESSES (20) SEQUENTIAL, bounded detail GETs -- one per
process, always over authenticated HTTPS adminPort 8443 (--port admin is
required; direct metricsPort 9015 is never used for this mode, since it is
not an approved authenticated path). --detail selects a FIXED endpoint
suffix only (process, processPerformance, threadPerformance, serviceHealth,
heartbeat) -- never an arbitrary operator-supplied suffix. It never outputs
a process name, process ID, or constructed detail URL -- only counts, HTTP
status counts, closed error-category counts, and a merged structural schema
(field names / broad JSON types / truncation flags). It does not write any
monitoring state (no DynamoDB write), does not publish CloudWatch, and does
not implement production PMS polling/parsing. Example:

    python3 tools/gg_api_contract_probe.py \\
        --deployment gg-oracle-payments-01 --port admin \\
        --follow-processes --detail processPerformance
"""
from __future__ import annotations

import argparse
import json
import os
import posixpath
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collector  # noqa: E402
import config as cfgmod  # noqa: E402

PROBE_TIMEOUT_SECONDS = 5

# A response body larger than this is never parsed, sized, or echoed in any
# form -- just a fixed, sanitized UNEXPECTED_RESPONSE. Not operator-tunable
# (no CLI option raises or disables this).
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

ERROR_CATEGORIES = (
    "AUTH_FAILED", "TLS_FAILED", "NOT_FOUND", "ENDPOINT_UNAVAILABLE",
    "INVALID_JSON", "UNEXPECTED_RESPONSE", "UNKNOWN",
)


class ProbeValidationError(Exception):
    """A local pre-flight validation failure (bad args, unknown/disabled
    deployment, unsafe path) -- never an HTTP-request-outcome category."""


class ProbeRequestError(Exception):
    """A classified HTTP/TLS/network outcome. category is always one of
    ERROR_CATEGORIES; http_status is the raw status code where one exists."""

    def __init__(self, category, http_status=None):
        self.category = category
        self.http_status = http_status
        super().__init__(category)


_CONTROL_CHARS = frozenset(chr(c) for c in list(range(0x00, 0x20)) + [0x7f])


def _has_control_chars(s):
    return any(c in _CONTROL_CHARS for c in s)


def _reject_unsafe_segments(s, context):
    for segment in s.split("/"):
        if segment in (".", ".."):
            raise ProbeValidationError(f"path must not contain a {context} '.' or '..' segment")


def _reject_unsafe_decoded_form(path, decoded):
    """A decoded round that introduces a new backslash, control character,
    slash (i.e. a percent-encoded '/'), or '.'/'..' segment is rejected --
    percent-encoding must never be able to smuggle something the literal
    path forbids."""
    if "\\" in decoded or _has_control_chars(decoded):
        raise ProbeValidationError("path must not contain a percent-encoded backslash/control character")
    if decoded.count("/") != path.count("/"):
        raise ProbeValidationError("path must not contain a percent-encoded slash")
    _reject_unsafe_segments(decoded, "percent-encoded")


def _reject_unsafe_percent_encoding(path, max_rounds=5):
    """Bounded, iterative percent-decoding (guards against double-encoding
    evasion, e.g. %252e%252e) using only urllib.parse.unquote. Stops as soon
    as decoding stabilizes; raises if the safe-decode depth is exceeded."""
    current = path
    for _ in range(max_rounds):
        try:
            decoded = urllib.parse.unquote(current, errors="strict")
        except UnicodeDecodeError:
            raise ProbeValidationError("path contains malformed percent-encoding")
        if decoded == current:
            return
        _reject_unsafe_decoded_form(path, decoded)
        current = decoded
    raise ProbeValidationError("path percent-encoding exceeds safe decode depth")


def validate_path(path):
    """Only a bare, already-normalized /services/... path -- never a URL,
    scheme, host, query string, fragment, backslash, control character, or
    any literal/percent-encoded traversal smuggled in through --path. Uses
    only stdlib URL/path parsing (urllib.parse.unquote, posixpath.normpath)
    -- no broad allowlist, so any other legitimate /services/... endpoint
    remains probeable."""
    if not isinstance(path, str) or not path:
        raise ProbeValidationError("path is required")
    if "://" in path:
        raise ProbeValidationError("path must not contain a scheme")
    if path.startswith("//"):
        raise ProbeValidationError("path must not specify a host")
    if "?" in path or "#" in path:
        raise ProbeValidationError("path must not contain a query string or fragment")
    if any(c.isspace() for c in path):
        raise ProbeValidationError("path must not contain whitespace")
    if _has_control_chars(path):
        raise ProbeValidationError("path must not contain control characters")
    if "\\" in path:
        raise ProbeValidationError("path must not contain a backslash")

    _reject_unsafe_segments(path, "literal")
    _reject_unsafe_percent_encoding(path)

    # A canonical path is already normalized -- any difference (duplicate
    # slashes, "." / ".." segments, a trailing slash, etc.) is rejected
    # rather than silently resolved.
    if posixpath.normpath(path) != path:
        raise ProbeValidationError("path is not already normalized")

    if not path.startswith("/services/"):
        raise ProbeValidationError("path must start with /services/")
    return path


def resolve_deployment(name, repo_config_root=None):
    """Loads the canonical deployment list and returns the one matching
    name. Rejects an unknown or disabled deployment."""
    doc = cfgmod.load_deployments(repo_config_root)
    by_name = {d["name"]: d for d in doc["deployments"]}
    deployment = by_name.get(name)
    if deployment is None:
        raise ProbeValidationError(f"unknown deployment: {name!r}")
    if not deployment.get("enabled"):
        raise ProbeValidationError(f"deployment disabled: {name!r}")
    return deployment


def _port_and_base(deployment, port_type):
    """admin -> HTTPS through adminPort 8443 (the confirmed, authenticated,
    TLS-verified PMS route). metrics -> PLAIN HTTP through metricsPort 9015
    (confirmed plain HTTP in the live environment) -- see run_probe, which
    never attaches credentials to a metrics-port request. The scheme is a
    fixed function of port_type, chosen explicitly by the operator; there is
    no automatic HTTPS<->HTTP fallback."""
    host = deployment["adminHost"]  # same internal Service; port selects the listener
    if port_type == "admin":
        return f"https://{host}:{deployment['adminPort']}"
    return f"http://{host}:{deployment['metricsPort']}"


def _contains_tls_error(exc, max_nodes=10):
    """Bounded, cycle-safe search for an ssl.SSLError (ssl.SSLCertVerificationError
    subclasses it) anywhere in exc's chain: the exception itself,
    urllib.error.URLError.reason, and __cause__/__context__ at every node.
    Never returns or logs the exception text -- classification only."""
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


def _classify_request_error(exc, http_status=None):
    if http_status in (401, 403):
        return "AUTH_FAILED"
    if _contains_tls_error(exc):
        return "TLS_FAILED"
    if http_status == 404:
        return "NOT_FOUND"
    if http_status is not None and http_status >= 500:
        return "ENDPOINT_UNAVAILABLE"
    if isinstance(exc, (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError)):
        return "ENDPOINT_UNAVAILABLE"
    return "UNKNOWN"


def _json_type_name(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


# Bounds on collection inspection -- a large/hostile PMS payload must never
# be able to consume unbounded memory or produce unbounded output. itemCount
# always reports the TRUE list length (never truncated); only per-item/
# per-field-name inspection is capped, and a "truncated" flag says so.
MAX_COLLECTION_KEYS = 20
MAX_ITEMS_PER_COLLECTION = 50
MAX_FIELD_NAMES_PER_COLLECTION = 100

# Bounds on key-name OUTPUT (topLevelKeys/responseKeys arrays, and each
# collection's own item field names). A key longer than MAX_KEY_LENGTH is
# omitted entirely -- never emitted partial/truncated -- and the relevant
# truncated flag is set.
MAX_TOP_LEVEL_KEYS = 50
MAX_RESPONSE_KEYS = 100
MAX_KEY_LENGTH = 128


def _bounded_sorted_keys(keys, max_count, max_key_length=MAX_KEY_LENGTH):
    """Sorted, deterministic key list for direct output. A key longer than
    max_key_length is dropped entirely (never emitted partial); the result
    is then capped to max_count. Returns (keys, truncated) -- truncated is
    True if anything was omitted for either reason."""
    all_keys = sorted(str(k) for k in keys)
    length_ok = [k for k in all_keys if len(k) <= max_key_length]
    truncated = len(length_ok) < len(all_keys) or len(length_ok) > max_count
    return length_ok[:max_count], truncated


def _summarize_collection(items, max_items=MAX_ITEMS_PER_COLLECTION,
                          max_field_names=MAX_FIELD_NAMES_PER_COLLECTION,
                          max_key_length=MAX_KEY_LENGTH):
    """items is a confirmed list. Returns sanitized structural metadata only
    -- field NAMES and broad JSON TYPES, never a raw value. Non-dict members
    are skipped rather than raising. Nested objects/arrays are reported only
    as "object"/"array" -- never recursed into. A field name longer than
    max_key_length is omitted entirely (never emitted partial) and marks
    this collection truncated; itemCount semantics are unaffected."""
    item_count = len(items)
    inspected = items[:max_items]
    field_types = {}
    extra_truncation = False
    for item in inspected:
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            key = str(key)
            if len(key) > max_key_length:
                extra_truncation = True
                continue
            if key not in field_types:
                if len(field_types) >= max_field_names:
                    extra_truncation = True
                    continue
                field_types[key] = set()
            field_types[key].add(_json_type_name(value))
    return {
        "itemCount": item_count,
        "itemFieldNames": sorted(field_types.keys()),
        "fieldTypes": {k: sorted(v) for k, v in field_types.items()},
        "truncated": bool(item_count > len(inspected) or extra_truncation),
    }


def summarize_json(payload, max_items=MAX_ITEMS_PER_COLLECTION,
                   max_collection_keys=MAX_COLLECTION_KEYS,
                   max_field_names=MAX_FIELD_NAMES_PER_COLLECTION,
                   max_key_length=MAX_KEY_LENGTH):
    """Sanitized structural metadata only -- collection field NAMES and
    broad JSON TYPES, never a raw value, process name, status value, ID,
    link, hostname, or nested raw payload. Returns None when payload is not
    a top-level JSON object (caller treats that as UNEXPECTED_RESPONSE).

    Every list-valued field directly under response.* becomes its own entry
    in "collections" -- not just response.items (e.g. response.processes,
    response.statusChange, and any future list-valued field). Non-list
    response fields are excluded. collectionsTruncated is True when there
    were more list-valued response fields than max_collection_keys (the
    excess collections are simply not inspected -- deterministically, by
    sorted field name, so which are dropped is stable across runs).

    topLevelKeys/responseKeys are each sorted, length- and count-bounded
    (MAX_KEY_LENGTH/MAX_TOP_LEVEL_KEYS/MAX_RESPONSE_KEYS); the corresponding
    *Truncated flag says so without ever emitting a partial key or any value
    for an omitted key."""
    if not isinstance(payload, dict):
        return None
    top_level_keys, top_level_keys_truncated = _bounded_sorted_keys(
        payload.keys(), MAX_TOP_LEVEL_KEYS, max_key_length=max_key_length)
    response = payload.get("response")
    if isinstance(response, dict):
        response_keys, response_keys_truncated = _bounded_sorted_keys(
            response.keys(), MAX_RESPONSE_KEYS, max_key_length=max_key_length)
    else:
        response_keys, response_keys_truncated = [], False

    collections = {}
    collections_truncated = False
    if isinstance(response, dict):
        all_list_valued_keys = sorted(str(k) for k in response.keys() if isinstance(response.get(k), list))
        # A collection name longer than max_key_length is omitted entirely
        # (never partial) -- and its items are never inspected at all, since
        # the filter runs before the summarization loop below.
        list_valued_keys = [k for k in all_list_valued_keys if len(k) <= max_key_length]
        if len(list_valued_keys) < len(all_list_valued_keys):
            collections_truncated = True
        if len(list_valued_keys) > max_collection_keys:
            collections_truncated = True
        for key in list_valued_keys[:max_collection_keys]:
            collections[key] = _summarize_collection(
                response[key], max_items=max_items, max_field_names=max_field_names,
                max_key_length=max_key_length)

    result = {
        "topLevelKeys": top_level_keys,
        "topLevelKeysTruncated": top_level_keys_truncated,
        "responseKeys": response_keys,
        "responseKeysTruncated": response_keys_truncated,
        "collections": collections,
        "collectionsTruncated": collections_truncated,
    }

    # Legacy flat fields, retained only for backward compatibility, and only
    # when response.items itself exists -- never map a different collection
    # (e.g. response.processes) into these.
    if "items" in collections:
        legacy = collections["items"]
        result["itemCount"] = legacy["itemCount"]
        result["itemFieldNames"] = legacy["itemFieldNames"]
        result["fieldTypes"] = legacy["fieldTypes"]

    return result


def _fetch_json(deployment, port_type, path, timeout=PROBE_TIMEOUT_SECONDS):
    """Performs exactly one read-only GET for path. Returns
    (http_status, content_type, payload) on success. Raises
    ProbeRequestError (a closed category) on any failure -- oversized body,
    HTTP error, network/TLS error, or invalid JSON. Never returns or logs a
    raw response body, raw exception text, header value, or URL. Shared by
    both the explicit-path probe mode (run_probe) and --follow-processes.

    port_type="admin": HTTPS through adminPort 8443, authenticated with the
    same CSI-mounted credentials/CA chain/TLS-SNI the collector uses -- the
    confirmed secure PMS route.
    port_type="metrics": plain HTTP through metricsPort 9015 (confirmed
    plain HTTP in the live environment). Always unauthenticated -- the
    mounted admin credentials are never read or attached to this request."""
    pipeline = deployment["name"]
    base = _port_and_base(deployment, port_type)
    url = f"{base}{path}"

    if port_type == "admin":
        user_file, pwd_file = cfgmod.credential_paths(pipeline)
        user = collector._read_secret_file(user_file)
        pwd = collector._read_secret_file(pwd_file)
        if not user or not pwd:
            raise ProbeValidationError("admin credentials unavailable")
        try:
            ssl_ctx = collector._build_ssl_context()
        except RuntimeError:
            raise ProbeValidationError("TLS trust bundle unavailable")
        tls_server_name = deployment["tlsServerName"]
        opener = collector._basic_opener(user, pwd, base, ssl_ctx, tls_server_name)
    else:
        # Plain HTTP, unauthenticated -- never build an auth handler or read
        # a credential file for a metrics-port request.
        opener = urllib.request.build_opener()

    http_status = None
    content_type = None
    try:
        with opener.open(url, timeout=timeout) as resp:
            http_status = resp.status
            content_type = resp.headers.get("Content-Type")
            # Read at most one byte past the limit: if that many bytes come
            # back, the true body is over the limit -- without reading it
            # unboundedly first to find out.
            raw_body = resp.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise ProbeRequestError(_classify_request_error(e, e.code), http_status=e.code)
    except Exception as e:
        raise ProbeRequestError(_classify_request_error(e))

    if len(raw_body) > MAX_RESPONSE_BYTES:
        # Oversized: never parsed, never sized or echoed in the output --
        # a fixed, sanitized closed category only.
        raise ProbeRequestError("UNEXPECTED_RESPONSE", http_status=http_status)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ProbeRequestError("INVALID_JSON", http_status=http_status)

    return http_status, content_type, payload


def run_probe(deployment, port_type, path, timeout=PROBE_TIMEOUT_SECONDS):
    """Performs exactly one read-only GET. Returns a sanitized result dict on
    success. Raises ProbeRequestError (with a closed category) on failure.
    Never returns or logs a raw response body, raw exception text, header
    value, or URL."""
    http_status, content_type, payload = _fetch_json(deployment, port_type, path, timeout=timeout)

    summary = summarize_json(payload)
    if summary is None:
        raise ProbeRequestError("UNEXPECTED_RESPONSE", http_status=http_status)

    return {
        "deploymentName": deployment["name"],
        "deploymentType": deployment["type"],
        "portType": port_type,
        "path": path,
        "httpStatus": http_status,
        "contentType": content_type,
        **summary,
    }


# --follow-processes: fixed detail-endpoint allowlist -- never an arbitrary
# operator-supplied suffix.
DETAIL_ENDPOINTS = ("process", "processPerformance", "threadPerformance", "serviceHealth", "heartbeat")

# Never operator-tunable (no CLI option raises or disables this).
MAX_FOLLOWED_PROCESSES = 20

INVENTORY_PATH = "/services/v2/mpoints/processes"


def _valid_inventory_process_names(payload):
    """Returns (names, inventory_item_count): unique processName strings
    pulled from response.processes, in first-seen order -- never processId,
    never any other field. Non-dict items, items with no valid (non-empty
    string) processName, and repeat processName values (after the first)
    are skipped -- a duplicate name must never cause a second detail
    request. inventory_item_count is the TRUE response.processes list
    length, unaffected by validity or dedup."""
    if not isinstance(payload, dict):
        return [], 0
    response = payload.get("response")
    if not isinstance(response, dict):
        return [], 0
    processes = response.get("processes")
    if not isinstance(processes, list):
        return [], 0
    names = []
    seen = set()
    for item in processes:
        if not isinstance(item, dict):
            continue
        name = item.get("processName")
        if isinstance(name, str) and name and name not in seen:
            seen.add(name)
            names.append(name)
    return names, len(processes)


def _process_detail_path(name, detail):
    """Encodes name as exactly one URL path segment and builds the fixed
    detail path. urllib.parse.quote(name, safe="") turns any literal '/'
    or '\\' in name into a percent-encoded byte -- it can never introduce a
    new path segment -- but by design (RFC 3986 unreserved characters) it
    leaves '.' and '..' themselves unescaped, so those two exact values are
    rejected outright rather than ever being sent as a traversal-equivalent
    segment. (validate_path's own percent-slash rejection is deliberately
    NOT reused here: it exists to stop an operator smuggling extra segments
    via --path, which is the opposite of this single, already-opaque,
    intentionally-encoded segment.)"""
    if name in (".", ".."):
        raise ProbeValidationError("process name is unsafe to use as a path segment")
    if _has_control_chars(name):
        raise ProbeValidationError("process name contains control characters")
    encoded = urllib.parse.quote(name, safe="")
    return f"/services/v2/mpoints/{encoded}/{detail}"


def _merge_detail_schema(payload, agg):
    """Merges one successful detail response's structural schema into the
    running aggregate (agg). payload is discarded by the caller immediately
    after this call returns -- no response body is retained past schema
    merging."""
    top_keys, top_trunc = _bounded_sorted_keys(payload.keys(), MAX_TOP_LEVEL_KEYS)
    agg["topLevelKeys"].update(top_keys)
    if top_trunc:
        agg["truncated"] = True

    response_obj = payload.get("response")
    if isinstance(response_obj, dict):
        own = _summarize_collection([response_obj])
        agg["fieldNames"].update(own["itemFieldNames"])
        for fname, ftypes in own["fieldTypes"].items():
            agg["fieldTypes"].setdefault(fname, set()).update(ftypes)
        if own["truncated"]:
            agg["truncated"] = True

    summary = summarize_json(payload)
    if summary is not None:
        if summary.get("responseKeysTruncated") or summary.get("collectionsTruncated"):
            agg["truncated"] = True
        for cname, cdata in summary["collections"].items():
            bucket = agg["collections"].setdefault(
                cname, {"fieldNames": set(), "fieldTypes": {}, "truncated": False})
            bucket["fieldNames"].update(cdata["itemFieldNames"])
            for fname, ftypes in cdata["fieldTypes"].items():
                bucket["fieldTypes"].setdefault(fname, set()).update(ftypes)
            if cdata["truncated"]:
                bucket["truncated"] = True


def _finalize_schema(agg):
    """Bounds the merged aggregate one final time (merging many responses
    could otherwise still exceed the per-response limits) and renders it
    into the sanitized output shape."""
    top_keys = sorted(agg["topLevelKeys"])
    if len(top_keys) > MAX_TOP_LEVEL_KEYS:
        agg["truncated"] = True
        top_keys = top_keys[:MAX_TOP_LEVEL_KEYS]

    field_names = sorted(agg["fieldNames"])
    if len(field_names) > MAX_FIELD_NAMES_PER_COLLECTION:
        agg["truncated"] = True
        field_names = field_names[:MAX_FIELD_NAMES_PER_COLLECTION]
    kept = set(field_names)
    field_types = {k: sorted(v) for k, v in agg["fieldTypes"].items() if k in kept}

    all_collection_names = sorted(agg["collections"].keys())
    # A merged collection NAME longer than MAX_KEY_LENGTH is omitted
    # entirely (never partial) -- its data is never inspected/output at all,
    # since the filter runs before the per-collection loop below.
    collection_names = [n for n in all_collection_names if len(n) <= MAX_KEY_LENGTH]
    if len(collection_names) < len(all_collection_names):
        agg["truncated"] = True
    if len(collection_names) > MAX_COLLECTION_KEYS:
        agg["truncated"] = True
        collection_names = collection_names[:MAX_COLLECTION_KEYS]
    collections_out = {}
    for name in collection_names:
        data = agg["collections"][name]
        # Re-apply the per-collection field-name bounds one final time:
        # merging many responses' field names can otherwise still exceed
        # MAX_FIELD_NAMES_PER_COLLECTION even though each individual
        # response stayed within it.
        all_field_names = sorted(data["fieldNames"])
        length_ok_field_names = [f for f in all_field_names if len(f) <= MAX_KEY_LENGTH]
        overlong_field_name_omitted = len(length_ok_field_names) < len(all_field_names)
        final_field_names = length_ok_field_names[:MAX_FIELD_NAMES_PER_COLLECTION]
        field_count_capped = len(length_ok_field_names) > MAX_FIELD_NAMES_PER_COLLECTION
        kept = set(final_field_names)
        final_field_types = {k: sorted(v) for k, v in data["fieldTypes"].items() if k in kept}
        collection_truncated = bool(
            data["truncated"] or overlong_field_name_omitted or field_count_capped)
        if collection_truncated:
            agg["truncated"] = True
        collections_out[name] = {
            "fieldNames": final_field_names,
            "fieldTypes": final_field_types,
            "truncated": collection_truncated,
        }

    return {
        "topLevelKeys": top_keys,
        "collections": collections_out,
        "fieldNames": field_names,
        "fieldTypes": field_types,
        "truncated": agg["truncated"],
    }


def follow_processes(deployment, detail, timeout=PROBE_TIMEOUT_SECONDS,
                     max_followed=MAX_FOLLOWED_PROCESSES):
    """--follow-processes mode: GETs the confirmed process inventory
    (/services/v2/mpoints/processes, authenticated HTTPS adminPort 8443),
    then issues up to max_followed SEQUENTIAL, bounded detail GETs -- one
    per valid processName (never falling back to processId) -- merging
    their structural schemas. One failed detail request never stops the
    remaining ones. Never outputs a process name, process ID, or
    constructed URL anywhere in the return value -- only counts, HTTP
    status counts, closed error-category counts, and merged structural
    schema (field names / broad JSON types / truncation flags).

    Raises ProbeValidationError if detail is not in DETAIL_ENDPOINTS, or if
    no valid process inventory item exists. Raises ProbeRequestError if the
    inventory request itself fails. Otherwise always returns a result dict
    -- even if every individual detail request failed -- so the operator
    still sees the aggregate error-category counts; the caller (main)
    decides the process exit code from successCount."""
    if detail not in DETAIL_ENDPOINTS:
        raise ProbeValidationError(f"unsupported --detail value: {detail!r}")

    _inv_status, _inv_ct, inventory_payload = _fetch_json(deployment, "admin", INVENTORY_PATH, timeout=timeout)
    names, inventory_item_count = _valid_inventory_process_names(inventory_payload)
    attempted_names = names[:max_followed]
    if not attempted_names:
        raise ProbeValidationError("no valid process inventory items found")

    http_status_counts = {}
    error_category_counts = {}
    success_count = 0
    agg = {"topLevelKeys": set(), "fieldNames": set(), "fieldTypes": {}, "collections": {}, "truncated": False}

    for name in attempted_names:
        try:
            detail_path = _process_detail_path(name, detail)
        except ProbeValidationError:
            error_category_counts["UNKNOWN"] = error_category_counts.get("UNKNOWN", 0) + 1
            continue

        try:
            status, _ct, payload = _fetch_json(deployment, "admin", detail_path, timeout=timeout)
        except ProbeRequestError as e:
            category = e.category if e.category in ERROR_CATEGORIES else "UNKNOWN"
            error_category_counts[category] = error_category_counts.get(category, 0) + 1
            if e.http_status is not None:
                key = str(e.http_status)
                http_status_counts[key] = http_status_counts.get(key, 0) + 1
            continue

        http_status_counts[str(status)] = http_status_counts.get(str(status), 0) + 1
        if not isinstance(payload, dict):
            error_category_counts["UNEXPECTED_RESPONSE"] = error_category_counts.get("UNEXPECTED_RESPONSE", 0) + 1
            continue

        _merge_detail_schema(payload, agg)
        success_count += 1
        # payload/detail_path fall out of scope here -- never retained.

    attempted_count = len(attempted_names)
    failure_count = attempted_count - success_count

    return {
        "deploymentName": deployment["name"],
        "deploymentType": deployment["type"],
        "portType": "admin",
        "sourcePath": INVENTORY_PATH,
        "detail": detail,
        "inventoryItemCount": inventory_item_count,
        "attemptedCount": attempted_count,
        "successCount": success_count,
        "failureCount": failure_count,
        "httpStatusCounts": http_status_counts,
        "errorCategoryCounts": error_category_counts,
        "schema": _finalize_schema(agg),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only GoldenGate Admin/Metrics REST contract probe (structural metadata only).")
    parser.add_argument("--deployment", required=True, help="canonical deployment name")
    parser.add_argument("--port", required=True, choices=("admin", "metrics"),
                        help="admin: HTTPS+authenticated (confirmed secure PMS route, "
                             "use this for /services/v2/mpoints/processes and "
                             "/services/v2/monitoring/statusChanges). "
                             "metrics: plain HTTP, unauthenticated only (port 9015 is not "
                             "an approved authenticated path)")
    parser.add_argument("--path", help="explicit /services/... path (not used with --follow-processes)")
    parser.add_argument("--follow-processes", action="store_true", dest="follow_processes",
                        help="manual, structural-only mode: GET the confirmed process inventory "
                             "(/services/v2/mpoints/processes) then capture per-process --detail "
                             "structure over up to %d sequential, bounded GETs. Never outputs "
                             "process names, IDs, or constructed URLs. Requires --port admin." % (
                                 MAX_FOLLOWED_PROCESSES))
    parser.add_argument("--detail", choices=DETAIL_ENDPOINTS,
                        help="required with --follow-processes: fixed detail endpoint to capture")
    args = parser.parse_args(argv)

    if args.follow_processes:
        if args.path:
            print(json.dumps({"error": "INVALID_ARGUMENT",
                              "reason": "--path is not used with --follow-processes"}), file=sys.stderr)
            return 2
        if not args.detail:
            print(json.dumps({"error": "INVALID_ARGUMENT",
                              "reason": "--detail is required with --follow-processes"}), file=sys.stderr)
            return 2
        if args.port != "admin":
            print(json.dumps({"error": "INVALID_ARGUMENT",
                              "reason": "--follow-processes requires --port admin"}), file=sys.stderr)
            return 2
        try:
            deployment = resolve_deployment(args.deployment)
            result = follow_processes(deployment, args.detail)
        except ProbeValidationError as e:
            print(json.dumps({"error": "INVALID_ARGUMENT", "reason": str(e)}), file=sys.stderr)
            return 2
        except ProbeRequestError as e:
            print(json.dumps({
                "deploymentName": args.deployment,
                "portType": "admin",
                "sourcePath": INVENTORY_PATH,
                "detail": args.detail,
                "httpStatus": e.http_status,
                "error": e.category,
            }))
            return 1
        print(json.dumps(result, indent=2))
        return 0 if result["successCount"] > 0 else 1

    if not args.path:
        print(json.dumps({"error": "INVALID_ARGUMENT", "reason": "--path is required"}), file=sys.stderr)
        return 2

    try:
        path = validate_path(args.path)
        deployment = resolve_deployment(args.deployment)
        result = run_probe(deployment, args.port, path)
    except ProbeValidationError as e:
        print(json.dumps({"error": "INVALID_ARGUMENT", "reason": str(e)}), file=sys.stderr)
        return 2
    except ProbeRequestError as e:
        print(json.dumps({
            "deploymentName": args.deployment,
            "portType": args.port,
            "path": args.path,
            "httpStatus": e.http_status,
            "error": e.category,
        }))
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
