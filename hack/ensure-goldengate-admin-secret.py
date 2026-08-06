#!/usr/bin/env python3
"""hack/ensure-goldengate-admin-secret.py: bootstraps the initial value of one managed GoldenGate admin secret; never reads an existing secret value."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

_ADMIN_USER = "oggadmin"


class BootstrapError(Exception):
    """A fixed, safe-to-print reason; never wraps raw AWS CLI output."""


class CommandRunner:
    """The only place that shells out; never logs argv or output (both may end up containing sensitive data)."""

    def run(self, args):
        result = subprocess.run(args, capture_output=True, text=True, check=True)
        return result.stdout


def _aws(args, region):
    return ["aws"] + args + ["--region", region, "--output", "json"]


def describe_secret(runner, secret_name, region):
    try:
        runner.run(_aws(["secretsmanager", "describe-secret", "--secret-id", secret_name], region))
    except subprocess.CalledProcessError as exc:
        raise BootstrapError(f"secret container does not exist or is not accessible: {secret_name}") from exc


def _version_stages(runner, secret_name, region):
    out = runner.run(_aws(["secretsmanager", "list-secret-version-ids", "--secret-id", secret_name,
                           "--include-deprecated"], region))
    data = json.loads(out) if out.strip() else {}
    stages = set()
    for version in data.get("Versions", []):
        stages.update(version.get("VersionStages", []))
    return stages


def has_awscurrent(runner, secret_name, region):
    return "AWSCURRENT" in _version_stages(runner, secret_name, region)


def get_random_password(runner, region):
    out = runner.run(_aws(["secretsmanager", "get-random-password", "--password-length", "32",
                           "--exclude-punctuation"], region))
    data = json.loads(out)
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
        runner.run(_aws(["secretsmanager", "put-secret-value", "--secret-id", secret_name,
                        "--secret-string", f"file://{path}"], region))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def ensure_admin_secret(runner, deployment_id, secret_name, managed, region):
    """Returns "unchanged" or "initialized"; raises BootstrapError with a fixed, safe reason on any failure."""
    describe_secret(runner, secret_name, region)
    current_exists = has_awscurrent(runner, secret_name, region)

    if not managed:
        if not current_exists:
            raise BootstrapError(f"{deployment_id}: managed=false admin secret has no AWSCURRENT version")
        return "unchanged"

    if current_exists:
        return "unchanged"

    password = get_random_password(runner, region)
    put_initial_secret_value(runner, secret_name, region, password)
    return "initialized"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--secret-name", required=True)
    parser.add_argument("--managed", required=True, choices=["true", "false"])
    parser.add_argument("--region", required=True)
    args = parser.parse_args(argv)

    runner = CommandRunner()
    try:
        outcome = ensure_admin_secret(runner, args.deployment_id, args.secret_name, args.managed == "true", args.region)
    except BootstrapError as exc:
        print(f"FAIL: {exc}")
        return 1
    print(f"{args.deployment_id}: {outcome}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
