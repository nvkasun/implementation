#!/usr/bin/env python3
"""hack/ensure-goldengate-admin-secret.py: bootstraps the initial value of one managed GoldenGate admin secret; never reads an existing secret value. AWS Secrets Manager has no compare-and-set primitive, so the AWSCURRENT recheck immediately before PutSecretValue narrows but never closes the race window; callers must serialize bootstrap runs per secret via workflow/job-level concurrency."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

_ADMIN_USER = "oggadmin"
_DEPLOYMENT_ID_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*\Z")
_ENVIRONMENT_RE = re.compile(r"^[a-z][a-z0-9-]*\Z")
_REGION_RE = re.compile(r"^[a-z]{2}-[a-z]+-\d\Z")
_MAX_ID_LENGTH = 63


class BootstrapError(Exception):
    """A fixed, safe-to-print reason; never wraps raw AWS CLI/JSON output."""


class CommandRunner:
    """The only place that shells out; never logs argv or output (both may end up containing sensitive data)."""

    def run(self, args):
        result = subprocess.run(args, capture_output=True, text=True, check=True)
        return result.stdout


def _valid_deployment_id(value):
    return isinstance(value, str) and bool(value) and len(value) <= _MAX_ID_LENGTH and bool(_DEPLOYMENT_ID_RE.match(value))


def _valid_environment(value):
    return isinstance(value, str) and bool(value) and bool(_ENVIRONMENT_RE.match(value))


def _valid_region(value):
    return isinstance(value, str) and bool(_REGION_RE.match(value))


def _valid_secret_name(name, environment):
    """Environment-scoped, no ARN, no traversal, no leading slash, no whitespace/control characters."""
    if not isinstance(name, str) or not name:
        return False
    if name.startswith("arn:"):
        return False
    if ".." in name or name.startswith("/"):
        return False
    if not name.startswith(f"{environment}/"):
        return False
    if any(c.isspace() or ord(c) < 0x20 for c in name):
        return False
    return True


def _aws(args, region):
    return ["aws"] + args + ["--region", region, "--output", "json"]


def _run_aws(runner, args, region, failure_reason):
    try:
        return runner.run(_aws(args, region))
    except subprocess.CalledProcessError as exc:
        raise BootstrapError(failure_reason) from exc
    except OSError as exc:
        raise BootstrapError(failure_reason) from exc


def describe_secret(runner, secret_name, region):
    _run_aws(runner, ["secretsmanager", "describe-secret", "--secret-id", secret_name], region,
             f"secret container does not exist or is not accessible: {secret_name}")


def _version_stages(runner, secret_name, region):
    out = _run_aws(runner, ["secretsmanager", "list-secret-version-ids", "--secret-id", secret_name,
                            "--include-deprecated"], region,
                    f"could not list secret version stages: {secret_name}")
    try:
        data = json.loads(out) if out.strip() else {}
    except json.JSONDecodeError as exc:
        raise BootstrapError(f"could not parse secret version stages: {secret_name}") from exc
    stages = set()
    for version in data.get("Versions", []):
        stages.update(version.get("VersionStages", []))
    return stages


def has_awscurrent(runner, secret_name, region):
    return "AWSCURRENT" in _version_stages(runner, secret_name, region)


def get_random_password(runner, region):
    out = _run_aws(runner, ["secretsmanager", "get-random-password", "--password-length", "32",
                            "--exclude-punctuation"], region,
                    "could not generate a random password")
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise BootstrapError("could not parse GetRandomPassword response") from exc
    password = data.get("RandomPassword")
    if not isinstance(password, str) or not password:
        raise BootstrapError("GetRandomPassword returned an unusable value")
    return password


def put_initial_secret_value(runner, secret_name, region, password):
    payload = json.dumps({"OGG_ADMIN": _ADMIN_USER, "OGG_ADMIN_PWD": password})
    fd, path = tempfile.mkstemp(prefix="gg-admin-secret-", suffix=".json")
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        _run_aws(runner, ["secretsmanager", "put-secret-value", "--secret-id", secret_name,
                          "--secret-string", f"file://{path}"], region,
                  f"could not write the initial secret value: {secret_name}")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def ensure_admin_secret(runner, deployment_id, environment, secret_name, managed, region):
    """Returns "unchanged" or "initialized"; raises BootstrapError with a fixed, safe reason on any failure."""
    if not _valid_deployment_id(deployment_id):
        raise BootstrapError("deployment_id is not a safe lowercase token")
    if not _valid_environment(environment):
        raise BootstrapError("environment is not a safe lowercase token")
    if not _valid_region(region):
        raise BootstrapError("region is not a valid AWS region identifier")
    if not _valid_secret_name(secret_name, environment):
        raise BootstrapError("secret_name is not a safe environment-scoped secret name")

    describe_secret(runner, secret_name, region)
    current_exists = has_awscurrent(runner, secret_name, region)

    if not managed:
        if not current_exists:
            raise BootstrapError(f"{deployment_id}: managed=false admin secret has no AWSCURRENT version")
        return "unchanged"

    if current_exists:
        return "unchanged"

    password = get_random_password(runner, region)

    # Recheck immediately before the write to narrow (not close) the race window documented above.
    if has_awscurrent(runner, secret_name, region):
        return "unchanged"

    put_initial_secret_value(runner, secret_name, region, password)
    return "initialized"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--secret-name", required=True)
    parser.add_argument("--managed", required=True, choices=["true", "false"])
    parser.add_argument("--region", required=True)
    args = parser.parse_args(argv)

    runner = CommandRunner()
    try:
        outcome = ensure_admin_secret(runner, args.deployment_id, args.environment, args.secret_name,
                                       args.managed == "true", args.region)
    except BootstrapError as exc:
        print(f"FAIL: {exc}")
        return 1
    print(f"{args.deployment_id}: {outcome}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
