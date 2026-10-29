#!/usr/bin/env python3
"""Resolves the GoldenGate deployment registered in the local Service Manager, and verifies rendered NGINX routes against it.

resolve: GET /services/v2/deployments, exclude ServiceManager, require exactly one valid name, print it on stdout.
verify-nginx: every /services/<name>/<service> route in the rendered NGINX config must use the expected name.
Diagnostics go to stderr. Credentials are read from OGG_ADMIN/OGG_ADMIN_PWD and never printed.
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:9011/services/v2/deployments"
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,62}\Z")
RESERVED_NAME = "servicemanager"
MAX_RESPONSE_BYTES = 1024 * 1024
ROUTE_RE = re.compile(r"/services/([^/\s(){}$]+)/(adminsrvr|distsrvr|pmsrvr|recvsrvr)\b")
TEMPLATE_PLACEHOLDER = "${OGG_DEPLOYMENT}"


class ResolutionError(Exception):
    """Unrecoverable: report and stop."""


class RetryableError(Exception):
    """Service Manager not ready yet."""


def log(message):
    print(f"goldengate-identity: {message}", file=sys.stderr, flush=True)


def _describe(name, limit=80):
    text = repr(name)
    return text if len(text) <= limit else text[:limit] + "..."


def valid_name(name):
    return isinstance(name, str) and bool(NAME_RE.match(name)) and name.lower() != RESERVED_NAME


def parse_registered_names(body):
    """Application deployment names from a /services/v2/deployments body; malformed content is an error, never skipped."""
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise ResolutionError("Service Manager returned a response that is not valid JSON")
    response = payload.get("response") if isinstance(payload, dict) else None
    items = response.get("items") if isinstance(response, dict) else None
    if not isinstance(items, list):
        raise ResolutionError("Service Manager response has no response.items list")
    names = set()
    for item in items:
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str):
            raise ResolutionError("Service Manager returned a deployment entry without a string name")
        if name.lower() == RESERVED_NAME:
            continue
        if not NAME_RE.match(name):
            raise ResolutionError(f"Service Manager returned an invalid deployment name {_describe(name)}")
        names.add(name)
    return sorted(names)


def select_registered_name(names, bootstrap_name, fresh_volume):
    if not names:
        raise RetryableError("no application deployment is registered yet")
    if len(names) > 1:
        raise ResolutionError(f"multiple application deployments are registered {names}; refusing to choose one")
    name = names[0]
    if fresh_volume and name != bootstrap_name:
        raise ResolutionError(f"fresh volume registered {name!r}, expected the bootstrap name {bootstrap_name!r}")
    return name


def fetch_deployments(url, user, password, timeout):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    request = urllib.request.Request(url, headers={"Authorization": f"Basic {token}", "Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ResolutionError(f"Service Manager rejected the administrator credentials (HTTP {exc.code})")
        if exc.code >= 500 or exc.code == 429:
            raise RetryableError(f"Service Manager returned HTTP {exc.code}")
        raise ResolutionError(f"Service Manager returned HTTP {exc.code}")
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
        raise RetryableError(f"Service Manager is not reachable ({type(exc).__name__})")
    if len(body) > MAX_RESPONSE_BYTES:
        raise ResolutionError("Service Manager response exceeds the size limit")
    return body


def resolve(url, user, password, bootstrap_name, fresh_volume, attempts, interval, timeout, fetch=fetch_deployments, sleep=time.sleep):
    if not valid_name(bootstrap_name):
        raise ResolutionError(f"bootstrap name OGG_DEPLOYMENT={_describe(bootstrap_name)} is not a valid GoldenGate deployment name")
    if not user or not password:
        raise ResolutionError("OGG_ADMIN/OGG_ADMIN_PWD are not set")
    last = None
    for attempt in range(1, attempts + 1):
        try:
            names = parse_registered_names(fetch(url, user, password, timeout))
            return select_registered_name(names, bootstrap_name, fresh_volume)
        except RetryableError as exc:
            last = exc
            if attempt < attempts:
                sleep(interval)
    raise ResolutionError(f"{last} after {attempts} attempts")


def verify_nginx(conf_glob, expected):
    paths = sorted(glob.glob(conf_glob))
    if not paths:
        raise ResolutionError(f"no NGINX configuration matches {conf_glob}")
    names = set()
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        if TEMPLATE_PLACEHOLDER in text:
            raise ResolutionError(f"{path} still contains an unrendered {TEMPLATE_PLACEHOLDER} placeholder")
        names.update(match.group(1) for match in ROUTE_RE.finditer(text))
    if not names:
        raise ResolutionError("rendered NGINX configuration has no deployment-scoped service routes")
    if names != {expected}:
        raise ResolutionError(f"rendered NGINX routes use {sorted(names)}, expected only {expected!r}")


def _bool(value):
    if value not in ("true", "false"):
        raise argparse.ArgumentTypeError("expected true or false")
    return value == "true"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    res = sub.add_parser("resolve")
    res.add_argument("--bootstrap-name", required=True)
    res.add_argument("--fresh-volume", type=_bool, required=True)
    res.add_argument("--url", default=os.environ.get("GOLDENGATE_IDENTITY_SM_URL", DEFAULT_URL))
    res.add_argument("--attempts", type=int, default=int(os.environ.get("GOLDENGATE_IDENTITY_ATTEMPTS", "90")))
    res.add_argument("--interval", type=float, default=float(os.environ.get("GOLDENGATE_IDENTITY_INTERVAL_SECONDS", "2")))
    res.add_argument("--timeout", type=float, default=5.0)
    ver = sub.add_parser("verify-nginx")
    ver.add_argument("--conf-glob", required=True)
    ver.add_argument("--expected", required=True)
    args = parser.parse_args(argv)

    try:
        if args.command == "resolve":
            name = resolve(args.url, os.environ.get("OGG_ADMIN", ""), os.environ.get("OGG_ADMIN_PWD", ""),
                           args.bootstrap_name, args.fresh_volume, max(1, args.attempts), max(0.0, args.interval), args.timeout)
            if name == args.bootstrap_name:
                log(f"registered deployment {name!r} matches the runtime bootstrap name")
            else:
                log(f"registered deployment {name!r} differs from the runtime bootstrap name {args.bootstrap_name!r}; using the registered name")
            print(name)
        else:
            verify_nginx(args.conf_glob, args.expected)
            log(f"NGINX deployment routes use the registered deployment {args.expected!r}")
    except ResolutionError as exc:
        log(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
