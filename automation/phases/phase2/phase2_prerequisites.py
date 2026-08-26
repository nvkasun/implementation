#!/usr/bin/env python3
"""Phase 2A | Validate AWS Application Prerequisites orchestration entrypoint for the validate_environment_config job in .github/workflows/10-sub-iam-secrets.yaml; a thin orchestration/service layer that never reimplements environment.yaml parsing, IAM policy rendering, or region derivation (those stay owned by automation/goldengate-environment.py) and never executes Terraform -- the corporate ADCB reusable workflow (called by the sibling apply job) remains the sole Terraform engine. Non-secret orchestration state is threaded between subcommands through a JSON state file under the runner temp directory."""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

ENVIRONMENT_TOOL = REPO_ROOT / "automation" / "goldengate-environment.py"

# Mirrors automation/phases/phase1/phase1_readiness.py's own _SAFE_TOKEN_RE -- each tool in this repository intentionally keeps its own local copy of this grammar rather than importing it across modules; used here only for defense-in-depth path-safety before an environment name is ever interpolated into a filesystem path, never as the canonical acceptance/rejection of an environment (that remains automation/goldengate-environment.py's own concern).
_SAFE_TOKEN_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*\Z")

CANONICAL_OUTPUT_KEYS = ("aws_region", "terraform_governance_override", "terraform_governance_override_reason")


class Phase2Error(Exception):
    """A fail-closed Phase 2 error; main() reports it and exits non-zero."""


def is_safe_token(value):
    return isinstance(value, str) and bool(_SAFE_TOKEN_RE.match(value))


def require_literal_bool(name, value):
    """Fail closed unless value is exactly the literal string 'true' or 'false' -- never truthy/falsy-coerced."""
    if value not in ("true", "false"):
        raise Phase2Error(f"{name} is {value!r}, expected literal 'true' or 'false'.")
    return value


# Phase 2 state file

def default_state_path():
    """${RUNNER_TEMP}/goldengate-phase2-state.json, or a repo-local fallback outside CI."""
    runner_temp = os.environ.get("RUNNER_TEMP")
    if runner_temp:
        return Path(runner_temp) / "goldengate-phase2-state.json"
    return Path(os.environ.get("TMPDIR", "/tmp")) / "goldengate-phase2-state.json"


def load_state(state_path):
    if not state_path.exists():
        return {}
    try:
        with state_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise Phase2Error(f"Phase 2 state file {state_path} is unreadable/malformed: {exc}") from exc
    if not isinstance(data, dict):
        raise Phase2Error(f"Phase 2 state file {state_path} did not contain a JSON object.")
    return data


def save_state(state_path, state):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, sort_keys=True, indent=2)
        f.write("\n")
    tmp_path.replace(state_path)


def update_state(state_path, updates):
    state = load_state(state_path)
    state.update(updates)
    save_state(state_path, state)
    return state


def require_state_value(state, key):
    if key not in state or state[key] in (None, ""):
        raise Phase2Error(f"Phase 2 state is missing required key {key!r}; an earlier step did not complete.")
    return state[key]


# GitHub Actions special-file helpers

def _github_output_delimiter(value):
    """Generates a random heredoc delimiter for a GITHUB_OUTPUT multiline value and verifies it does not occur inside that value -- a constant delimiter could be spoofed by a caller-supplied value (e.g. the governance reason) to inject additional NAME=value fragments."""
    while True:
        delimiter = f"ggPhase2Delim_{secrets.token_hex(16)}"
        if delimiter not in value:
            return delimiter


def _requires_heredoc(value):
    """True if value contains any line-break character (LF, CR, or CRLF) a line-oriented GITHUB_OUTPUT/GITHUB_ENV parser could treat as a record separator -- a bare "\\r" alone is just as capable of smuggling a second NAME=value fragment as "\\n" is."""
    return "\n" in value or "\r" in value


def write_github_output(pairs, output_path=None):
    """Appends name=value lines to $GITHUB_OUTPUT. Output names are always fixed literals supplied by this module's own code, never caller-controlled. Any value containing a line-break character (LF, CR, or CRLF) uses the heredoc form with a fresh, collision-checked random delimiter per call -- never a constant string a caller-supplied value could spoof to inject extra output fragments. No-op (never raises) when GITHUB_OUTPUT is unset."""
    path = output_path if output_path is not None else os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for name, value in pairs:
            if _requires_heredoc(value):
                delimiter = _github_output_delimiter(value)
                f.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")
            else:
                f.write(f"{name}={value}\n")


def write_step_summary(text, summary_path=None):
    path = summary_path if summary_path is not None else os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        print(text)
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)
        if not text.endswith("\n"):
            f.write("\n")


# Safe subprocess execution

def run(argv, env=None, cwd=None, check=True, capture_output=True):
    """Runs argv as an argument array (never shell=True). Fails closed with the tool's own stderr/stdout on a non-zero exit when check=True."""
    proc = subprocess.run(
        argv,
        cwd=cwd or REPO_ROOT,
        env=env,
        capture_output=capture_output,
        text=True,
    )
    if check and proc.returncode != 0:
        raise Phase2Error(
            f"{' '.join(str(a) for a in argv)} failed (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
        )
    return proc


def run_environment_tool(environment, args, **kwargs):
    return run([sys.executable, str(ENVIRONMENT_TOOL), "--environment", environment, *args], **kwargs)


# Subcommands

def cmd_prerequisites(args):
    if sys.version_info < (3, 0):
        raise Phase2Error("python3 is required.")
    try:
        import yaml  # noqa: F401
    except ImportError as exc:
        raise Phase2Error("PyYAML is required and is not available on this runner.") from exc
    print("OK: python3 and PyYAML are available.")


def cmd_validate_environment(args):
    target_environment = os.environ.get("TARGET_ENVIRONMENT", "")
    if not target_environment:
        raise Phase2Error("TARGET_ENVIRONMENT (inputs.environment) is empty -- refusing to guess a default environment.")
    if not is_safe_token(target_environment):
        raise Phase2Error(f"TARGET_ENVIRONMENT {target_environment!r} is not a safe identifier; refusing to use it in a filesystem path.")

    update_state(args.state_path, {"selected_environment": target_environment})
    run_environment_tool(target_environment, ["validate"])
    print(f"OK: envs/{target_environment}/environment.yaml is valid.")


def cmd_validate_iam_policies(args):
    state = load_state(args.state_path)
    environment = require_state_value(state, "selected_environment")
    run_environment_tool(environment, ["render-iam-policies", "--check"])
    print("OK: generated IAM policies are in sync with environment.yaml.")


def cmd_resolve_region(args):
    state = load_state(args.state_path)
    environment = require_state_value(state, "selected_environment")
    proc = run_environment_tool(environment, ["get", "AWS_REGION"])
    aws_region = proc.stdout.strip()
    if not aws_region:
        raise Phase2Error(f"canonical AWS_REGION resolved empty for environment {environment!r} from envs/{environment}/environment.yaml.")

    update_state(args.state_path, {"aws_region": aws_region})
    write_github_output([("aws_region", aws_region)])
    print(f"OK: canonical Terraform region is {aws_region!r}.")


def cmd_validate_governance(args):
    input_override = os.environ.get("INPUT_OVERRIDE", "")
    input_reason = os.environ.get("INPUT_REASON", "")

    require_literal_bool("terraform_governance_override", input_override)
    trimmed_reason = input_reason.strip()
    if input_override == "true":
        if not trimmed_reason:
            raise Phase2Error(
                "terraform_governance_override=true requires a non-empty, non-whitespace "
                "terraform_governance_override_reason. Refusing to activate the corporate Terraform "
                "governance break-glass without a written justification."
            )
        override, reason = "true", input_reason
        print("Terraform governance override is ENABLED for this run -- the corporate reusable workflow will "
              "receive override_noncompliance=true with the supplied override_reason. This is not a bypass: "
              "non-compliance remains recorded, and manual approval itself cannot be overridden.")
    else:
        if trimmed_reason:
            print("INFO: terraform_governance_override_reason was provided but terraform_governance_override is false -- the reason is ignored; normal corporate PR-governance behavior applies.")
        override, reason = "false", ""
        print("OK: Terraform governance override is disabled -- normal corporate PR-governance/Kosli attestation applies.")

    update_state(args.state_path, {"terraform_governance_override": override, "terraform_governance_override_reason": reason})
    write_github_output([("terraform_governance_override", override), ("terraform_governance_override_reason", reason)])


def cmd_publish_outputs(args):
    state = load_state(args.state_path)
    missing = [k for k in CANONICAL_OUTPUT_KEYS if k not in state]
    if missing:
        raise Phase2Error(f"Phase 2 state is missing canonical output key(s): {missing}")
    write_github_output([(k, state[k]) for k in CANONICAL_OUTPUT_KEYS])
    print("OK: published canonical Phase 2 outputs.")


def cmd_acceptance(args):
    state = load_state(args.state_path)

    selected_environment = require_state_value(state, "selected_environment")
    if not is_safe_token(selected_environment):
        raise Phase2Error(f"selected_environment {selected_environment!r} is not a safe identifier.")

    aws_region = require_state_value(state, "aws_region")
    override = require_literal_bool("terraform_governance_override", require_state_value(state, "terraform_governance_override"))
    reason = state.get("terraform_governance_override_reason", "")

    if override == "true" and not reason.strip():
        raise Phase2Error("terraform_governance_override=true requires a non-empty terraform_governance_override_reason.")
    if override == "false" and reason != "":
        raise Phase2Error("terraform_governance_override=false must publish an empty terraform_governance_override_reason.")

    override_summary = "ENABLED (written justification supplied)" if override == "true" else "disabled"
    summary_lines = [
        "## Phase 2A | Validate AWS Application Prerequisites",
        "",
        f"- Environment: {selected_environment}",
        f"- Terraform region: {aws_region}",
        "- IAM policy sync: passed",
        f"- Governance override: {override_summary}",
        "- Result: PASSED",
        "",
    ]
    write_step_summary("\n".join(summary_lines))
    print("OK: Phase 2A | Validate AWS Application Prerequisites succeeded.")


# CLI wiring

_SUBCOMMANDS = {
    "prerequisites": cmd_prerequisites,
    "validate-environment": cmd_validate_environment,
    "validate-iam-policies": cmd_validate_iam_policies,
    "resolve-region": cmd_resolve_region,
    "validate-governance": cmd_validate_governance,
    "publish-outputs": cmd_publish_outputs,
    "acceptance": cmd_acceptance,
}


def build_parser():
    parser = argparse.ArgumentParser(description="Phase 2A | Validate AWS Application Prerequisites orchestrator.")
    parser.add_argument("--state-file", type=Path, default=None, help="Override the Phase 2 state file path (default: $RUNNER_TEMP/goldengate-phase2-state.json).")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in _SUBCOMMANDS:
        subparsers.add_parser(name)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.state_path = args.state_file if args.state_file is not None else default_state_path()

    try:
        _SUBCOMMANDS[args.command](args)
    except Phase2Error as exc:
        print(f"FAIL: {exc}")
        return 1
    except subprocess.SubprocessError as exc:
        print(f"FAIL: subprocess execution error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
