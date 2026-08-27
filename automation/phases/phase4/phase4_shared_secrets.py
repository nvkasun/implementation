#!/usr/bin/env python3
"""Phase 4G | Shared GoldenGate secrets acceptance for the validate_shared_secrets_once job in .github/workflows/00-main-goldengate-orchestrator.yaml. Preserves the exact current security contract: GitHub OIDC -> RUNNER_ROLE_ARN (source/build account) -> this script -> sts:AssumeRole -> EKS_DEPLOY_ROLE_ARN (workload account) -> read-only Secrets Manager validation. This tool NEVER calls secretsmanager:GetSecretValue, never logs a secret value, and never persists source or workload AWS credentials outside a single subprocess-local environment mapping."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOYMENT_MODEL_TOOL = REPO_ROOT / "automation" / "goldengate-deployment-model.py"

_SAFE_TOKEN_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*\Z")
_ROLE_ARN_RE = re.compile(r"^arn:aws:iam::(\d{12}):role/[A-Za-z0-9+=,.@_/-]+\Z")


class Phase4Error(Exception):
    """A fail-closed Phase 4 shared-secrets error; main() reports it and exits non-zero."""


def is_safe_token(value):
    return isinstance(value, str) and bool(_SAFE_TOKEN_RE.match(value))


def require_environment_arg(environment):
    if not is_safe_token(environment):
        raise Phase4Error(f"environment {environment!r} is not a safe identifier.")
    return environment


def require_env(name):
    value = os.environ.get(name, "")
    if not value:
        raise Phase4Error(f"{name} is empty; canonical environment configuration must be loaded before this step.")
    return value


def run(argv, env=None, check=True):
    """Runs argv as an argument array; never a shell, never a pipeline. env, if given, replaces the subprocess's environment entirely -- callers are responsible for confining credentials to it."""
    proc = subprocess.run(argv, cwd=REPO_ROOT, env=env, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise Phase4Error(f"{' '.join(str(a) for a in argv)} failed (exit {proc.returncode}): {proc.stderr.strip()}")
    return proc


def parse_expected_workload_account(eks_deploy_role_arn):
    match = _ROLE_ARN_RE.match(eks_deploy_role_arn)
    if not match:
        raise Phase4Error(f"EKS_DEPLOY_ROLE_ARN {eks_deploy_role_arn!r} is not a well-formed IAM role ARN (expected arn:aws:iam::<12-digit-account>:role/<name>).")
    return match.group(1)


def assume_eks_deploy_role(eks_deploy_role_arn, run_id, run_attempt, base_env):
    """Assumes EKS_DEPLOY_ROLE_ARN using the caller's own (source-account) credentials and returns a fresh env dict carrying ONLY the assumed workload-account credentials. The source-account credentials in base_env are never mutated in place and never merged into the returned dict."""
    session_name = f"gg-shared-secrets-{run_id}-{run_attempt}"
    proc = run(["aws", "sts", "assume-role", "--role-arn", eks_deploy_role_arn, "--role-session-name", session_name, "--duration-seconds", "900", "--output", "json"], env=base_env, check=False)
    if proc.returncode != 0:
        raise Phase4Error(f"sts:AssumeRole into {eks_deploy_role_arn} failed: {proc.stderr.strip()}")
    try:
        creds = json.loads(proc.stdout)["Credentials"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise Phase4Error(f"sts:AssumeRole response for {eks_deploy_role_arn} was malformed: {exc}") from exc

    assumed_env = dict(base_env)
    assumed_env["AWS_ACCESS_KEY_ID"] = creds["AccessKeyId"]
    assumed_env["AWS_SECRET_ACCESS_KEY"] = creds["SecretAccessKey"]
    assumed_env["AWS_SESSION_TOKEN"] = creds["SessionToken"]
    return assumed_env


def verify_assumed_identity(assumed_env, expected_workload_account_id):
    """MUST run before any Secrets Manager call: verifies the assumed identity's account, using ONLY the assumed (workload-account) credentials, never the source-account ones."""
    proc = run(["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"], env=assumed_env, check=False)
    if proc.returncode != 0:
        raise Phase4Error(f"sts:GetCallerIdentity using the assumed role failed: {proc.stderr.strip()}")
    actual_account = proc.stdout.strip()
    if actual_account != expected_workload_account_id:
        raise Phase4Error(f"assumed identity account is {actual_account}, expected workload account {expected_workload_account_id} derived from EKS_DEPLOY_ROLE_ARN. Refusing to call Secrets Manager.")
    print(f"OK: assumed identity account is the expected workload account {expected_workload_account_id}.")


def canonical_secret_names(environment):
    """Obtains the canonical shared-secret names from automation/goldengate-deployment-model.py -- the single source of truth. Never hardcodes or re-derives secret names independently."""
    proc = run([sys.executable, str(DEPLOYMENT_MODEL_TOOL), "--environment", environment, "shared-secrets"])
    names = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not names:
        raise Phase4Error(f"automation/goldengate-deployment-model.py --environment {environment} shared-secrets returned no secret names.")
    unique_names = sorted(set(names))
    if len(unique_names) != len(names):
        raise Phase4Error(f"canonical shared-secret list contains duplicate entries: {names}")
    if len(unique_names) != 3:
        raise Phase4Error(f"expected exactly 3 unique canonical shared secrets, found {len(unique_names)}: {unique_names}")
    return names


def verify_secret_has_current_version(secret_name, assumed_env):
    """Uses ONLY secretsmanager:DescribeSecret and secretsmanager:ListSecretVersionIds -- secretsmanager:GetSecretValue is never called, and no secret value is ever read, printed, or logged."""
    describe = run(["aws", "secretsmanager", "describe-secret", "--secret-id", secret_name, "--output", "json"], env=assumed_env, check=False)
    if describe.returncode != 0:
        raise Phase4Error(f"secret {secret_name!r} is missing or inaccessible: {describe.stderr.strip()}")

    list_versions = run(["aws", "secretsmanager", "list-secret-version-ids", "--secret-id", secret_name, "--include-deprecated", "--output", "json"], env=assumed_env, check=False)
    if list_versions.returncode != 0:
        raise Phase4Error(f"could not list version ids for secret {secret_name!r}: {list_versions.stderr.strip()}")

    try:
        versions = json.loads(list_versions.stdout).get("Versions", [])
    except json.JSONDecodeError as exc:
        raise Phase4Error(f"list-secret-version-ids response for {secret_name!r} was malformed: {exc}") from exc

    has_current = any("AWSCURRENT" in (v.get("VersionStages") or []) for v in versions)
    if not has_current:
        raise Phase4Error(f"secret {secret_name!r} has no version with the AWSCURRENT stage.")
    print(f"OK: secret {secret_name!r} exists and has an AWSCURRENT version (value not read).")


def cmd_validate(args):
    environment = require_environment_arg(args.environment)
    eks_deploy_role_arn = require_env("EKS_DEPLOY_ROLE_ARN")
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")

    expected_workload_account_id = parse_expected_workload_account(eks_deploy_role_arn)
    print(f"Expected workload account (derived from EKS_DEPLOY_ROLE_ARN): {expected_workload_account_id}")

    base_env = dict(os.environ)
    assumed_env = assume_eks_deploy_role(eks_deploy_role_arn, run_id, run_attempt, base_env)
    verify_assumed_identity(assumed_env, expected_workload_account_id)

    secret_names = canonical_secret_names(environment)
    print(f"Canonical shared secrets for {environment!r}: {secret_names}")
    for secret_name in secret_names:
        verify_secret_has_current_version(secret_name, assumed_env)

    print(f"OK: all {len(secret_names)} shared GoldenGate secrets exist with an AWSCURRENT version in the expected workload account.")


def build_parser():
    parser = argparse.ArgumentParser(description="Phase 4G | Validate the shared GoldenGate secrets exist with an AWSCURRENT version, without ever reading their values.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--environment", required=True)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            cmd_validate(args)
    except Phase4Error as exc:
        print(f"FAIL: {exc}")
        return 1
    except subprocess.SubprocessError as exc:
        print(f"FAIL: subprocess execution error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
