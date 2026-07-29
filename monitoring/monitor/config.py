"""config.py: canonical deployment/topology loader and process configuration.

Single source of deployment data: envs/dev/goldengate-deployments.yaml (or
its ConfigMap-mounted copy at REPO_CONFIG_ROOT in-cluster). Internal
service host, TLS server name, and default ports are derived here, never
stored in the source file.
"""
from __future__ import annotations

import os
import time

import yaml

REPO_ROOT_ENV = "REPO_CONFIG_ROOT"
DEFAULT_REPO_ROOT = "/etc/gg-canonical"
DEPLOYMENTS_FILE_RELPATH = "goldengate-deployments.yaml"

DEFAULT_ADMIN_PORT = 8443
DEFAULT_METRICS_PORT = 9015

SUPPORTED_TYPES = ("oracle", "postgresql")

DEFAULTS = {
    "PORT": "8080",
    "STALE_AFTER_SECONDS": "120",
    "REFRESH_SECONDS": "30",
    "MONITOR_VERSION": "development",
    "LEGACY_FALLBACK_ENABLED": "true",
}


class ConfigError(Exception):
    """Raised for missing/invalid process or deployment configuration."""


def _repo_root():
    return os.environ.get(REPO_ROOT_ENV, DEFAULT_REPO_ROOT)


def _get_int(env, name, default):
    raw = env.get(name, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None


def _parse_bool_env(raw, default):
    if raw is None:
        return default
    return str(raw).strip().lower() in ("true", "1", "yes")


class MonitorConfig:
    def __init__(self, aws_region, dynamodb_table, port, stale_after_seconds,
                refresh_seconds, monitor_version, repo_config_root, legacy_fallback_enabled):
        self.aws_region = aws_region
        self.dynamodb_table = dynamodb_table
        self.port = port
        self.stale_after_seconds = stale_after_seconds
        self.refresh_seconds = refresh_seconds
        self.monitor_version = monitor_version
        self.repo_config_root = repo_config_root
        self.legacy_fallback_enabled = legacy_fallback_enabled


def load_config(env) -> MonitorConfig:
    missing = sorted(name for name in ("AWS_REGION", "DYNAMODB_TABLE") if not env.get(name))
    if missing:
        raise ConfigError("Missing required environment variables: " + ", ".join(missing))

    port = _get_int(env, "PORT", DEFAULTS["PORT"])
    if not (1 <= port <= 65535):
        raise ConfigError(f"PORT must be between 1 and 65535, got {port}")

    stale_after_seconds = _get_int(env, "STALE_AFTER_SECONDS", DEFAULTS["STALE_AFTER_SECONDS"])
    if stale_after_seconds <= 0:
        raise ConfigError(f"STALE_AFTER_SECONDS must be a positive integer, got {stale_after_seconds}")

    refresh_seconds = _get_int(env, "REFRESH_SECONDS", DEFAULTS["REFRESH_SECONDS"])
    if refresh_seconds <= 0:
        raise ConfigError(f"REFRESH_SECONDS must be a positive integer, got {refresh_seconds}")

    return MonitorConfig(
        aws_region=env["AWS_REGION"],
        dynamodb_table=env["DYNAMODB_TABLE"],
        port=port,
        stale_after_seconds=stale_after_seconds,
        refresh_seconds=refresh_seconds,
        monitor_version=env.get("MONITOR_VERSION", DEFAULTS["MONITOR_VERSION"]),
        repo_config_root=env.get("REPO_CONFIG_ROOT", DEFAULT_REPO_ROOT),
        legacy_fallback_enabled=_parse_bool_env(
            env.get("LEGACY_FALLBACK_ENABLED"), DEFAULTS["LEGACY_FALLBACK_ENABLED"] == "true"),
    )


def _admin_host(name, runtime_namespace):
    return f"{name}.{runtime_namespace}.svc.cluster.local"


def _tls_server_name(name, dns_domain):
    return f"{name}.{dns_domain}"


def credential_paths(name, mount_root="/mnt/secrets-store"):
    """CSI-mounted credential file paths, derived only from the canonical
    deployment name -- matches the SecretProviderClass alias convention in
    the runtime Helm chart, so both sides agree without cross-referencing."""
    return f"{mount_root}/{name}-admin-user", f"{mount_root}/{name}-admin-password"


def load_deployments_document(repo_root=None):
    repo_root = repo_root or _repo_root()
    path = os.path.join(repo_root, DEPLOYMENTS_FILE_RELPATH)
    try:
        with open(path) as f:
            doc = yaml.safe_load(f)
    except OSError as e:
        raise ConfigError(f"could not read {path}: {e}") from e
    if not isinstance(doc, dict):
        raise ConfigError(f"{path}: document must be a mapping")
    return doc


def load_deployments(repo_root=None):
    """Returns {"environment", "runtimeNamespace", "monitoringNamespace",
    "dnsDomain", "deployments": [...]}. Each deployment dict adds the
    derived adminHost/adminPort/tlsServerName/metricsPort fields."""
    doc = load_deployments_document(repo_root)
    for key in ("environment", "runtimeNamespace", "monitoringNamespace", "dnsDomain", "deployments"):
        if key not in doc:
            raise ConfigError(f"goldengate-deployments.yaml: missing required key {key!r}")

    entries = doc["deployments"]
    if not isinstance(entries, list) or not entries:
        raise ConfigError("goldengate-deployments.yaml: 'deployments' must be a non-empty list")

    runtime_namespace = doc["runtimeNamespace"]
    dns_domain = doc["dnsDomain"]

    seen = set()
    deployments = []
    for entry in entries:
        for field in ("name", "type", "pipeline", "role"):
            if not entry.get(field):
                raise ConfigError(f"deployment entry missing required field {field!r}: {entry!r}")
        name = entry["name"]
        if name in seen:
            raise ConfigError(f"duplicate deployment name {name!r}")
        seen.add(name)
        if entry["role"] not in ("source", "target"):
            raise ConfigError(f"{name}: role must be 'source' or 'target', got {entry['role']!r}")
        deployments.append({
            "name": name,
            "type": entry["type"],
            "pipeline": entry["pipeline"],
            "role": entry["role"],
            "enabled": bool(entry.get("enabled", False)),
            "adminSecret": entry.get("adminSecret", ""),
            "adminHost": _admin_host(name, runtime_namespace),
            "adminPort": int(entry.get("adminPort", DEFAULT_ADMIN_PORT)),
            "tlsServerName": _tls_server_name(name, dns_domain),
            "metricsPort": int(entry.get("metricsPort", DEFAULT_METRICS_PORT)),
        })

    return {
        "environment": doc["environment"],
        "runtimeNamespace": runtime_namespace,
        "monitoringNamespace": doc["monitoringNamespace"],
        "dnsDomain": dns_domain,
        "deployments": deployments,
    }


class StartupValidationError(Exception):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def validate_enabled_deployments(deployments):
    """Fails startup clearly for any enabled deployment missing what the
    collector needs -- never silently polls with an empty admin secret."""
    problems = []
    for d in deployments:
        if not d["enabled"]:
            continue
        if d["type"] not in SUPPORTED_TYPES:
            problems.append(f"{d['name']}: unsupported type {d['type']!r} (supported: {SUPPORTED_TYPES})")
        if not d["adminSecret"]:
            problems.append(f"{d['name']}: adminSecret is required")
    if problems:
        raise StartupValidationError(problems)


def build_logical_pipelines(deployments):
    """Groups canonical deployments by pipeline -> {role: name}. A logical
    pipeline is a relationship between deployments, never a runtime
    identity in its own right."""
    by_pipeline = {}
    for d in deployments:
        roles = by_pipeline.setdefault(d["pipeline"], {})
        if d["role"] in roles and roles[d["role"]] != d["name"]:
            raise ConfigError(f"pipeline {d['pipeline']!r} has conflicting {d['role']} roles")
        roles[d["role"]] = d["name"]
    return [{"pipelineId": pid, "roles": roles} for pid, roles in sorted(by_pipeline.items())]


def now_epoch():
    return int(time.time())
