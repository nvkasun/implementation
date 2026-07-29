"""gg_health_rules: pure health-EVALUATION logic ported from the manager
reference implementation's charts/gg-deployment/files/gg_health.py
(inspected read-only, not modified, not imported directly).

Ported: CONFIG resolution/defaults, per-process rule resolution, quiet-hours
window resolution, abend counters, distpath stall detection, lag-breach
classification, and HTTP-status service-up classification -- all pure
functions over plain dicts, exactly as in the manager original.

Deliberately NOT ported (confirmed manager active-healing execution paths,
excluded per the approved passive-monitor architecture):
  - heal_decision() and the whole serviceHealEnabled/maxHealAttempts/
    serviceDownChecks circuit-breaker state machine.
  - Any code path that would act on abend_step()'s "failover" flag (this
    module still COMPUTES it, for STATE-record/metric fidelity with the
    manager schema, but gg_monitor_core.py never triggers any restart/exit
    from it -- see that module's comments).
  - dispatchStallChecks: never used: the manager's OWN gg_health.py already
    uses the correct distpathStallChecks name (the typo is confined to the
    manager's Terraform seed, terraform/platform/dynamodb.tf, corrected in
    our own envs/dev/dynamodb.tf in Phase 3) -- ported as-is, correct name.

CONFIG DEFAULTS here mirror our own manager-aligned, passive-safe seeded
CONFIG schema (envs/dev/dynamodb.tf) exactly -- not the manager's own
(active-sidecar-oriented) DEFAULTS dict, which also carries
serviceHealEnabled/maxHealAttempts/serviceDownChecks/criticalServices
(healing-only fields never present in our CONFIG schema at all).

metricsEnabled defaults to False here (the manager's own seed defaults it
True) -- an intentional, documented deviation: in this repository
metricsEnabled is what gates CloudWatch publication (out of scope this
phase: PutMetricData, alarms, dashboards, Logs, SNS, Fluent Bit, CloudWatch
Agent, Container Insights). It has no effect on DynamoDB LEASE/STATE#*
writing, which is unconditional.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("gg-health-rules")

DEFAULTS = {
    "alertsEnabled": False,
    # CloudWatch publication is OPTIONAL and DISABLED by default this phase
    # (out of scope: PutMetricData, alarms, dashboards, Logs, SNS, Fluent
    # Bit, CloudWatch Agent, Container Insights -- see
    # monitoring/gg-monitor-core/README.md). gg_monitor_core._emit() is
    # gated on this field; the monitor must start and run correctly with
    # metricsEnabled=false and no CloudWatch permission at all.
    "metricsEnabled": False,
    "tz": "Asia/Dubai",
    "checkIntervalSeconds": 60,
    "startupGraceSeconds": 300,
    "defaults": {
        "lagMode": "alert",
        "lagThresholdSeconds": 300,
        "maxConsecutiveAbends": 3,
        "abendRecheckSeconds": 120,
        "alertEachAbend": False,
        "failoverEnabled": False,
        "distpathStallChecks": 3,
    },
}

_RULE_KEYS = tuple(DEFAULTS["defaults"].keys())

# Not CONFIG-derived (our CONFIG schema deliberately excludes healing-adjacent
# fields -- see Phase 3). The manager reads this list from
# CONFIG.criticalServices to gate its self-heal circuit breaker; we probe the
# same services purely for observational CriticalServiceDown metrics/state,
# per deployment TYPE (matches each runtime's actual configured ports -- see
# topologies/dev/payments-ora-to-pg-001.yaml: Oracle has no receiver port,
# PostgreSQL has no distribution port).
CRITICAL_SERVICES_BY_TYPE = {
    "oracle": ["adminsrvr", "distsrvr"],
    "postgresql": ["adminsrvr", "recvsrvr"],
}


def _to_int(val, fallback):
    if isinstance(val, bool):
        return fallback
    try:
        return int(val)
    except (TypeError, ValueError):
        return fallback


def _to_bool(val, fallback):
    if isinstance(val, bool):
        return val
    if val is not None:
        logger.warning("ignoring non-boolean config value %r", val)
    return fallback


def _coerce_rule_keys(dst, src):
    for k in _RULE_KEYS:
        if k not in src:
            continue
        base = dst[k]
        if isinstance(base, bool):
            dst[k] = _to_bool(src[k], base)
        elif k == "lagMode":
            dst[k] = src[k] if src[k] in ("alert", "relaxed", "skip") else base
        else:
            dst[k] = _to_int(src[k], base)


def resolve_config(raw):
    """Merge a raw CONFIG item over DEFAULTS. Tolerates any vintage/garbage."""
    raw = raw if isinstance(raw, dict) else {}
    cfg = {
        "alertsEnabled": _to_bool(raw.get("alertsEnabled"), DEFAULTS["alertsEnabled"]),
        "metricsEnabled": _to_bool(raw.get("metricsEnabled"), DEFAULTS["metricsEnabled"]),
        "tz": raw.get("tz") if isinstance(raw.get("tz"), str) else DEFAULTS["tz"],
        "checkIntervalSeconds": _to_int(raw.get("checkIntervalSeconds"), DEFAULTS["checkIntervalSeconds"]),
        "startupGraceSeconds": _to_int(raw.get("startupGraceSeconds"), DEFAULTS["startupGraceSeconds"]),
        "defaults": dict(DEFAULTS["defaults"]),
        "quietHours": raw.get("quietHours") if isinstance(raw.get("quietHours"), dict) else {},
        "overrides": raw.get("overrides") if isinstance(raw.get("overrides"), dict) else {},
    }
    incoming = raw.get("defaults") if isinstance(raw.get("defaults"), dict) else {}
    _coerce_rule_keys(cfg["defaults"], incoming)
    return cfg


def rule_for_process(cfg, process):
    """Per-process effective rule: overrides[PROC] -> deployment defaults."""
    rule = dict(cfg["defaults"])
    ov = cfg["overrides"].get(process)
    if isinstance(ov, dict):
        _coerce_rule_keys(rule, ov)
        if isinstance(ov.get("quietHours"), dict):
            rule["quietHours"] = ov["quietHours"]
    return rule


def _tzinfo(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        logger.warning("tz %r unavailable; using fixed UTC+4", name)
        return timezone(timedelta(hours=4))


def _window_rule(windows, hour, default_mode, default_thr):
    matches = []
    for key, val in sorted(windows.items()):
        try:
            start, end = (int(p) for p in str(key).split("-", 1))
        except ValueError:
            logger.warning("ignoring malformed window key %r", key)
            continue
        inside = (start <= hour < end) if start <= end else (hour >= start or hour < end)
        if inside:
            matches.append((key, val))
    if matches:
        key, val = matches[0]
        if isinstance(val, str):
            return (val if val in ("alert", "relaxed", "skip") else default_mode), default_thr
        if isinstance(val, dict):
            mode = val.get("mode") if val.get("mode") in ("alert", "relaxed", "skip") else default_mode
            return mode, _to_int(val.get("thresholdSeconds"), default_thr)
    return default_mode, default_thr


def lag_rule_now(cfg, process, now=None):
    """(mode, thresholdSeconds) in force for <process> at epoch <now>."""
    now = int(now if now is not None else time.time())
    rule = rule_for_process(cfg, process)
    windows = rule.get("quietHours", cfg["quietHours"])
    hour = datetime.fromtimestamp(now, _tzinfo(cfg["tz"])).hour
    return _window_rule(windows, hour, rule["lagMode"], rule["lagThresholdSeconds"])


def abend_step(status, state, now, rule, alerts_enabled):
    """One tick of the per-process abend counter machine.

    Still computes act["failover"] for schema fidelity with the manager's
    STATE fields, but gg_monitor_core.py never acts on it -- passive
    architecture, no restart/exit path exists anywhere in this application.
    """
    st = {
        "consecutiveAbends": _to_int(state.get("consecutiveAbends"), 0),
        "lastAbendAt": _to_int(state.get("lastAbendAt"), 0),
        "nextRecheckAt": _to_int(state.get("nextRecheckAt"), 0),
    }
    act = {"abend_event": False, "abend_failure": False, "failover": False}

    if status == "RUNNING":
        st["consecutiveAbends"] = 0
        return st, act
    if status != "ABENDED":
        return st, act

    if now >= st["nextRecheckAt"]:
        st["consecutiveAbends"] += 1
        st["lastAbendAt"] = now
        st["nextRecheckAt"] = now + rule["abendRecheckSeconds"]
        act["abend_event"] = bool(rule["alertEachAbend"]) and alerts_enabled

    if st["consecutiveAbends"] >= rule["maxConsecutiveAbends"] and alerts_enabled:
        act["abend_failure"] = True
        act["failover"] = bool(rule["failoverEnabled"])
    return st, act


BYTES_KEYS = ("bytesSent", "outputBytes", "bytes")


def distpath_step(state, bytes_now, source_active, stall_checks):
    """A distpath is 'stalled' when RUNNING but moving no bytes across
    <stall_checks> consecutive ticks while its source extract is active."""
    st = {"stallCount": _to_int(state.get("stallCount"), 0),
          "lastBytes": _to_int(state.get("lastBytes"), -1)}
    if stall_checks is None or stall_checks <= 0:
        st["stallCount"] = 0
        return st, False
    b = _to_int(bytes_now, None) if bytes_now is not None else None
    if bytes_now is None or b is None or not source_active:
        st["stallCount"] = 0
        return st, False
    if b == st["lastBytes"]:
        st["stallCount"] += 1
    else:
        st["stallCount"] = 0
    st["lastBytes"] = b
    return st, st["stallCount"] >= stall_checks


def lag_breached(cfg, process, lag_seconds, now=None):
    """True iff lag breaches the hour-resolved rule. skip => never breaches."""
    mode, thr = lag_rule_now(cfg, process, now=now)
    if mode == "skip":
        return False
    try:
        lag = float(lag_seconds)
    except (TypeError, ValueError):
        return False
    return lag > float(thr)


def classify_service_up(http_status):
    """A GG microservice is UP iff its proxied endpoint answers. 2xx or 401
    (auth challenge) => up. 5xx or None (refused/timeout) => down."""
    if http_status is None:
        return False
    return http_status < 500 or http_status == 401
