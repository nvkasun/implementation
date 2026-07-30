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


def _summarize_collection(items, max_items=MAX_ITEMS_PER_COLLECTION,
                          max_field_names=MAX_FIELD_NAMES_PER_COLLECTION):
    """items is a confirmed list. Returns sanitized structural metadata only
    -- field NAMES and broad JSON TYPES, never a raw value. Non-dict members
    are skipped rather than raising. Nested objects/arrays are reported only
    as "object"/"array" -- never recursed into."""
    item_count = len(items)
    inspected = items[:max_items]
    field_types = {}
    field_name_cap_hit = False
    for item in inspected:
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            key = str(key)
            if key not in field_types:
                if len(field_types) >= max_field_names:
                    field_name_cap_hit = True
                    continue
                field_types[key] = set()
            field_types[key].add(_json_type_name(value))
    return {
        "itemCount": item_count,
        "itemFieldNames": sorted(field_types.keys()),
        "fieldTypes": {k: sorted(v) for k, v in field_types.items()},
        "truncated": bool(item_count > len(inspected) or field_name_cap_hit),
    }


def summarize_json(payload, max_items=MAX_ITEMS_PER_COLLECTION,
                   max_collection_keys=MAX_COLLECTION_KEYS,
                   max_field_names=MAX_FIELD_NAMES_PER_COLLECTION):
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
    sorted field name, so which are dropped is stable across runs)."""
    if not isinstance(payload, dict):
        return None
    top_level_keys = sorted(str(k) for k in payload.keys())
    response = payload.get("response")
    response_keys = sorted(str(k) for k in response.keys()) if isinstance(response, dict) else []

    collections = {}
    collections_truncated = False
    if isinstance(response, dict):
        list_valued_keys = sorted(str(k) for k in response.keys() if isinstance(response.get(k), list))
        if len(list_valued_keys) > max_collection_keys:
            collections_truncated = True
        for key in list_valued_keys[:max_collection_keys]:
            collections[key] = _summarize_collection(
                response[key], max_items=max_items, max_field_names=max_field_names)

    result = {
        "topLevelKeys": top_level_keys,
        "responseKeys": response_keys,
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


def run_probe(deployment, port_type, path, timeout=PROBE_TIMEOUT_SECONDS):
    """Performs exactly one read-only GET. Returns a sanitized result dict on
    success. Raises ProbeRequestError (with a closed category) on failure.
    Never returns or logs a raw response body, raw exception text, header
    value, or URL.

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
            raw_body = resp.read()
    except urllib.error.HTTPError as e:
        raise ProbeRequestError(_classify_request_error(e, e.code), http_status=e.code)
    except Exception as e:
        raise ProbeRequestError(_classify_request_error(e))

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ProbeRequestError("INVALID_JSON", http_status=http_status)

    summary = summarize_json(payload)
    if summary is None:
        raise ProbeRequestError("UNEXPECTED_RESPONSE", http_status=http_status)

    return {
        "deploymentName": pipeline,
        "deploymentType": deployment["type"],
        "portType": port_type,
        "path": path,
        "httpStatus": http_status,
        "contentType": content_type,
        **summary,
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
    parser.add_argument("--path", required=True, help="explicit /services/... path")
    args = parser.parse_args(argv)

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
