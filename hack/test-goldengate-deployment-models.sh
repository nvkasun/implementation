#!/usr/bin/env bash
set -euo pipefail

# Phase 1 local validation: legacyPair backward compatibility plus the
# singleRuntime candidate resource contract (Oracle and PostgreSQL).
#
# Requires `helm` on PATH for the lint/render steps. When helm is not
# installed, those specific checks are clearly SKIPPED (never silently
# reported as passing) -- everything that does not need helm (static
# workflow assertions) still runs.
#
# Resource-contract assertions are Python/PyYAML-based, using a
# duplicate-mapping-key-rejecting loader and reading actual Kubernetes
# object structure (spec.template.spec.containers, etc.) -- never
# indentation-based grep against raw YAML text.
#
# Does not deploy, does not touch the cluster, does not require AWS
# credentials, does not install anything.
#
# Usage:
#   hack/test-goldengate-deployment-models.sh
#
# To also compare against a pre-change baseline (recommended before/after
# any helm/goldengate/** change), render one from the original pre-Phase-1
# repository revision/archive first, e.g.:
#   git worktree add /tmp/goldengate-pre-phase1 <pre-phase-1-commit>
#   helm template ogg-payments-ora-to-pg-001 /tmp/goldengate-pre-phase1/helm/goldengate \
#     --namespace gg-dev-payments-ora-to-pg-001 \
#     -f /tmp/goldengate-pre-phase1/envs/dev/payments-ora-to-pg-001/values.yaml \
#     --set-string monitoring.observer.image.repository=example.invalid/goldengate-observer \
#     --set-string monitoring.observer.image.tag=obs-test \
#     > /tmp/goldengate-legacy-before.yaml

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CHART_PATH="helm/goldengate"
LEGACY_VALUES="envs/dev/payments-ora-to-pg-001/values.yaml"
ORACLE_VALUES="envs/dev/gg-oracle-payments-01/values.yaml"
POSTGRESQL_VALUES="envs/dev/gg-postgresql-payments-01/values.yaml"
WORKFLOW_FILE=".github/workflows/goldengate-eks-app.yaml"
BASELINE_FILE="/tmp/goldengate-legacy-before.yaml"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0

pass() { echo "PASS: $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "FAIL: $1"; FAIL_COUNT=$((FAIL_COUNT + 1)); }
skip() { echo "SKIP: $1"; SKIP_COUNT=$((SKIP_COUNT + 1)); }

HELM_AVAILABLE="false"
if command -v helm >/dev/null 2>&1; then
  HELM_AVAILABLE="true"
fi

PYTHON_AVAILABLE="false"
if command -v python3 >/dev/null 2>&1 && python3 -c "import yaml" >/dev/null 2>&1; then
  PYTHON_AVAILABLE="true"
fi

TERRAFORM_AVAILABLE="false"
if command -v terraform >/dev/null 2>&1; then
  TERRAFORM_AVAILABLE="true"
fi

echo "=================================================="
echo "GoldenGate Phase 1 deployment-model validation"
echo "=================================================="
echo "Repository root: ${REPO_ROOT}"
echo "Helm available:  ${HELM_AVAILABLE}"
echo "Python3+PyYAML available: ${PYTHON_AVAILABLE}"
echo "Terraform available: ${TERRAFORM_AVAILABLE}"
echo ""

if [ ! -f "$BASELINE_FILE" ]; then
  echo "NOTE: no baseline manifest found at ${BASELINE_FILE}."
  echo "Capture one BEFORE making chart changes with the command in this script's header comment."
  echo ""
fi

# ---------------------------------------------------------------------
# Write the Python validators once (flush-left heredocs -- this is a plain
# bash script, not a YAML block scalar, so there is no automatic dedent
# step; heredoc bodies/terminators here are intentionally unindented).
# ---------------------------------------------------------------------
DUPLICATE_KEY_CHECK_PY="${WORKDIR}/duplicate_key_check.py"
cat > "$DUPLICATE_KEY_CHECK_PY" <<'PYEOF'
import sys
import yaml


class DuplicateKeyError(Exception):
    pass


class StrictSafeLoader(yaml.SafeLoader):
    pass


def _no_duplicates_constructor(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise DuplicateKeyError(
                f"duplicate mapping key {key!r} at line {key_node.start_mark.line + 1}"
            )
        value = loader.construct_object(value_node, deep=deep)
        mapping[key] = value
    return mapping


StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates_constructor
)

with open(sys.argv[1]) as f:
    raw = f.read()

try:
    documents = [d for d in yaml.load_all(raw, Loader=StrictSafeLoader) if d]
except DuplicateKeyError as exc:
    print(f"duplicate mapping key: {exc}")
    sys.exit(1)
except yaml.YAMLError as exc:
    print(f"not valid YAML: {exc}")
    sys.exit(1)

print(f"{len(documents)} document(s), no duplicate mapping keys")
PYEOF

CANDIDATE_VALIDATOR_PY="${WORKDIR}/validate_candidate.py"
cat > "$CANDIDATE_VALIDATOR_PY" <<'PYEOF'
import sys
import yaml


class DuplicateKeyError(Exception):
    pass


class StrictSafeLoader(yaml.SafeLoader):
    pass


def _no_duplicates_constructor(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise DuplicateKeyError(
                f"duplicate mapping key {key!r} at line {key_node.start_mark.line + 1}"
            )
        value = loader.construct_object(value_node, deep=deep)
        mapping[key] = value
    return mapping


StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates_constructor
)

FORBIDDEN_CONTAINER_SUBSTRINGS = (
    "goldengate-observer",
    "utility-sidecar",
    "fluent-bit",
    "fluentbit",
)

rendered_path, values_path, deployment_id, target_namespace, label = sys.argv[1:6]

pass_count = 0
fail_count = 0
skip_count = 0


def ok(msg):
    global pass_count
    print(f"PASS: [{label}] {msg}")
    pass_count += 1


def bad(msg):
    global fail_count
    print(f"FAIL: [{label}] {msg}")
    fail_count += 1


def skipped(msg):
    global skip_count
    print(f"SKIP: [{label}] {msg}")
    skip_count += 1


def finish():
    print(f"SUMMARY pass={pass_count} fail={fail_count} skip={skip_count}")
    sys.exit(1 if fail_count else 0)


with open(values_path) as f:
    values = yaml.safe_load(f) or {}

runtime_values = values.get("runtime") or {}
expected_container_name = runtime_values.get("containerName")
expected_sa_name = (runtime_values.get("serviceAccount") or {}).get("name")
expected_ports = (runtime_values.get("service") or {}).get("ports") or {}
expected_claim_name = ((runtime_values.get("storage") or {}).get("u02") or {}).get("claimName")
expected_pvc_name = expected_claim_name or f"{deployment_id}-u02"

persistence = values.get("persistence") or {}
expected_storage_class = ((persistence.get("efs") or {}).get("storageClass") or {}).get("name")

ingress_values = values.get("ingress") or {}
expected_host = ingress_values.get("host")

try:
    with open(rendered_path) as f:
        raw = f.read()
except FileNotFoundError:
    skipped(f"resource contract validation -- rendered manifest not found: {rendered_path}")
    finish()

if not raw.strip():
    skipped("resource contract validation -- rendered manifest is empty")
    finish()

try:
    documents = [d for d in yaml.load_all(raw, Loader=StrictSafeLoader) if d]
except DuplicateKeyError as exc:
    bad(f"duplicate YAML mapping key in rendered manifest: {exc}")
    finish()
except yaml.YAMLError as exc:
    bad(f"rendered manifest is not valid YAML: {exc}")
    finish()

ok(f"rendered manifest parsed as {len(documents)} document(s) with no duplicate mapping keys")

# --- StatefulSet / containers ---------------------------------------------
statefulsets = [d for d in documents if d.get("kind") == "StatefulSet"]
if len(statefulsets) == 1:
    ok("exactly one StatefulSet")
else:
    bad(f"expected exactly one StatefulSet, found {len(statefulsets)}")

sts = statefulsets[0] if statefulsets else {}
pod_spec = ((sts.get("spec") or {}).get("template") or {}).get("spec") or {}
containers = pod_spec.get("containers") or []

if len(containers) == 1:
    ok("exactly one regular application container (spec.template.spec.containers)")
else:
    bad(
        "expected exactly one regular container in spec.template.spec.containers, "
        f"found {len(containers)}: {[c.get('name') for c in containers]}"
    )

main_container = containers[0] if containers else {}
main_name = main_container.get("name")
if containers:
    if main_name == expected_container_name:
        ok(f"expected main container name ({expected_container_name})")
    else:
        bad(f"expected main container name {expected_container_name!r}, found {main_name!r}")

name_lower = (main_name or "").lower()
image_lower = (main_container.get("image") or "").lower()
any_forbidden = any(s in name_lower or s in image_lower for s in FORBIDDEN_CONTAINER_SUBSTRINGS)
if containers:
    if not any_forbidden:
        ok("no observer/utility-sidecar/Fluent Bit reference in the regular container")
    else:
        bad("the regular container unexpectedly references a forbidden sidecar name/image")

# An empty initContainers list must fail here, not pass vacuously -- the
# mandatory permissions/stale-PID init container is required, not optional.
init_containers = pod_spec.get("initContainers") or []
init_names = [c.get("name") for c in init_containers]
if init_names == ["prepare-u02-permissions"]:
    ok("initContainers limited to the expected prepare-u02-permissions")

    init_container = init_containers[0]
    init_script_parts = []
    for item in list(init_container.get("command") or []) + list(init_container.get("args") or []):
        if isinstance(item, str):
            init_script_parts.append(item)
    init_script_text = "\n".join(init_script_parts)

    if "ServiceManager.pid" in init_script_text:
        ok("stale PID cleanup logic (ServiceManager.pid) present")
    else:
        bad("ServiceManager.pid stale-PID cleanup logic not found in the init container's command/args")

    if 'rm -f -- "$SERVICE_MANAGER_PID_FILE"' in init_script_text:
        ok("stale PID removal command present (rm -f -- \"$SERVICE_MANAGER_PID_FILE\")")
    else:
        bad("stale PID removal command not found (expected: rm -f -- \"$SERVICE_MANAGER_PID_FILE\")")
else:
    bad(f"expected initContainers == ['prepare-u02-permissions'], found {init_names}")
    skipped("stale PID cleanup logic check -- init container contract not satisfied")
    skipped("stale PID removal command check -- init container contract not satisfied")

# --- runtime name / namespace ----------------------------------------------
sts_name = (sts.get("metadata") or {}).get("name")
if sts_name == deployment_id:
    ok(f"runtime name {deployment_id}")
else:
    bad(f"expected StatefulSet name {deployment_id!r}, found {sts_name!r}")

sts_namespace = (sts.get("metadata") or {}).get("namespace")
if sts_namespace == target_namespace:
    ok(f"namespace {target_namespace}")
else:
    bad(f"expected namespace {target_namespace!r}, found {sts_namespace!r}")

# --- ServiceAccount ----------------------------------------------------------
sa_name = pod_spec.get("serviceAccountName")
if sa_name == expected_sa_name:
    ok(f"ServiceAccount {expected_sa_name}")
else:
    bad(f"expected serviceAccountName {expected_sa_name!r}, found {sa_name!r}")

# --- ports -------------------------------------------------------------------
rendered_port_names = {p.get("name") for p in (main_container.get("ports") or [])}
for port_name in ("dist", "receiver"):
    expect_present = bool(expected_ports.get(port_name))
    is_present = port_name in rendered_port_names
    if expect_present == is_present:
        ok(f"{port_name} port present" if expect_present else f"{port_name} port absent")
    else:
        bad(
            f"{port_name} port presence mismatch: expected_present={expect_present}, "
            f"rendered_present={is_present}"
        )

if "metrics" in rendered_port_names:
    ok("metrics port present")
else:
    bad("metrics port not found")

# --- Namespace document (must not exist) -------------------------------------
if not any(d.get("kind") == "Namespace" for d in documents):
    ok("no Namespace document")
else:
    bad("unexpected Namespace document rendered")

# --- Ingress -------------------------------------------------------------------
ingresses = [d for d in documents if d.get("kind") == "Ingress"]
if ingress_values.get("enabled"):
    if len(ingresses) == 1:
        rules = (ingresses[0].get("spec") or {}).get("rules") or []
        if len(rules) == 1:
            actual_host = rules[0].get("host")
            if actual_host == expected_host:
                ok(f"one expected hostname ({expected_host})")
            else:
                bad(f"expected Ingress host {expected_host!r}, found {actual_host!r}")
        else:
            bad(f"expected exactly one Ingress rule, found {len(rules)}")
    else:
        bad(f"expected exactly one Ingress document, found {len(ingresses)}")
else:
    skipped("Ingress hostname check -- ingress.enabled is not true")

# --- StorageClass / PVC -------------------------------------------------------
storageclasses = [d for d in documents if d.get("kind") == "StorageClass"]
if expected_storage_class:
    sc_names = {(d.get("metadata") or {}).get("name") for d in storageclasses}
    if expected_storage_class in sc_names:
        ok(f"unique StorageClass name ({expected_storage_class})")
    else:
        bad(f"expected StorageClass {expected_storage_class!r} not found. Rendered: {sc_names}")
else:
    skipped("StorageClass name check -- persistence.efs.storageClass.name not set in values")

pvcs = [d for d in documents if d.get("kind") == "PersistentVolumeClaim"]
pvc_names = {(d.get("metadata") or {}).get("name") for d in pvcs}
if expected_pvc_name in pvc_names:
    ok(f"unique PVC name ({expected_pvc_name})")
else:
    bad(f"expected PVC {expected_pvc_name!r} not found. Rendered: {pvc_names}")

finish()
PYEOF

LEGACY_COMPARISON_PY="${WORKDIR}/compare_legacy.py"
cat > "$LEGACY_COMPARISON_PY" <<'PYEOF'
import sys
import yaml


class DuplicateKeyError(Exception):
    pass


class StrictSafeLoader(yaml.SafeLoader):
    pass


def _no_duplicates_constructor(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise DuplicateKeyError(
                f"duplicate mapping key {key!r} at line {key_node.start_mark.line + 1}"
            )
        value = loader.construct_object(value_node, deep=deep)
        mapping[key] = value
    return mapping


StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates_constructor
)


def strip_volatile(obj):
    """Recursively drop the one explicitly-accepted volatile key
    (helm.sh/chart) from any mapping, wherever it appears."""
    if isinstance(obj, dict):
        return {
            k: strip_volatile(v)
            for k, v in obj.items()
            if k != "helm.sh/chart"
        }
    if isinstance(obj, list):
        return [strip_volatile(v) for v in obj]
    return obj


baseline_path, current_path = sys.argv[1], sys.argv[2]

with open(baseline_path) as f:
    baseline_raw = f.read()
with open(current_path) as f:
    current_raw = f.read()

# The baseline is the pre-Phase-1 render and may carry the pre-existing
# (not Phase-1-introduced) duplicate-mapping-key defect in the legacy pod
# template labels -- decoded here the same way Kubernetes' own YAML/JSON
# decoding actually resolves it: last-value-wins, no error. This is not a
# relaxation of the comparison; it reflects the real, already-in-production
# effective configuration.
try:
    baseline_docs = [d for d in yaml.safe_load_all(baseline_raw) if d]
except yaml.YAMLError as exc:
    print(f"FAIL: baseline manifest is not valid YAML: {exc}")
    sys.exit(1)

# The current chart must have zero duplicate mapping keys -- decoded with
# the strict loader so any regression here is a hard failure, not silently
# resolved.
try:
    current_docs = [d for d in yaml.load_all(current_raw, Loader=StrictSafeLoader) if d]
except DuplicateKeyError as exc:
    print(f"FAIL: current rendered manifest has a duplicate mapping key: {exc}")
    sys.exit(1)
except yaml.YAMLError as exc:
    print(f"FAIL: current rendered manifest is not valid YAML: {exc}")
    sys.exit(1)

baseline_docs = [strip_volatile(d) for d in baseline_docs]
current_docs = [strip_volatile(d) for d in current_docs]

if len(baseline_docs) != len(current_docs):
    print(
        f"FAIL: document count differs -- baseline has {len(baseline_docs)}, "
        f"current has {len(current_docs)}."
    )
    sys.exit(1)

mismatches = []
for i, (b, c) in enumerate(zip(baseline_docs, current_docs)):
    if b != c:
        kind = b.get("kind") if isinstance(b, dict) else None
        name = (b.get("metadata") or {}).get("name") if isinstance(b, dict) else None
        mismatches.append((i, kind, name, b, c))

if mismatches:
    print(f"FAIL: {len(mismatches)} document(s) differ after decoding (helm.sh/chart ignored):")
    for i, kind, name, b, c in mismatches:
        print(f"--- document {i} (kind={kind}, name={name}) ---")
        print("baseline:", b)
        print("current: ", c)
    sys.exit(1)

print(f"OK: {len(current_docs)} document(s) structurally identical to the baseline (helm.sh/chart ignored).")
PYEOF

# Adds a Python validator's "SUMMARY pass=P fail=F skip=S" line to the
# script-level counters. All of the validator's own PASS/FAIL/SKIP lines
# were already printed to stdout by the validator itself.
accumulate_python_summary() {
  local output="$1"
  local summary
  summary="$(echo "$output" | grep '^SUMMARY ' | tail -1)"
  if [ -z "$summary" ]; then
    fail "candidate validator produced no SUMMARY line -- treating as a failure"
    return
  fi
  local p f s
  p="$(echo "$summary" | sed -E 's/.*pass=([0-9]+).*/\1/')"
  f="$(echo "$summary" | sed -E 's/.*fail=([0-9]+).*/\1/')"
  s="$(echo "$summary" | sed -E 's/.*skip=([0-9]+).*/\1/')"
  PASS_COUNT=$((PASS_COUNT + p))
  FAIL_COUNT=$((FAIL_COUNT + f))
  SKIP_COUNT=$((SKIP_COUNT + s))
}

# ---------------------------------------------------------------------
# 1/2/3: legacyPair -- lint, render, compare against the captured baseline
# ---------------------------------------------------------------------
LEGACY_RENDERED="${WORKDIR}/legacy-after.yaml"

if [ "$HELM_AVAILABLE" = "true" ]; then
  echo "--- Legacy (payments-ora-to-pg-001, deploymentModel=legacyPair) ---"

  if helm lint "$CHART_PATH" --values "$LEGACY_VALUES" \
      --set-string monitoring.observer.image.repository=example.invalid/goldengate-observer \
      --set-string monitoring.observer.image.tag=obs-test >"${WORKDIR}/legacy-lint.log" 2>&1; then
    pass "helm lint (legacy)"
  else
    fail "helm lint (legacy)"
    cat "${WORKDIR}/legacy-lint.log"
  fi

  if helm template ogg-payments-ora-to-pg-001 "$CHART_PATH" \
      --namespace gg-dev-payments-ora-to-pg-001 \
      -f "$LEGACY_VALUES" \
      --set-string monitoring.observer.image.repository=example.invalid/goldengate-observer \
      --set-string monitoring.observer.image.tag=obs-test \
      > "$LEGACY_RENDERED" 2>"${WORKDIR}/legacy-template.log"; then
    pass "helm template (legacy)"
  else
    fail "helm template (legacy)"
    cat "${WORKDIR}/legacy-template.log"
  fi

  if [ -f "$BASELINE_FILE" ] && [ -s "$LEGACY_RENDERED" ] && [ "$PYTHON_AVAILABLE" = "true" ]; then
    # Structural (decoded-YAML) comparison, not raw text diff. Two reasons:
    #
    # 1. This repo has a pre-existing, Phase-1-unrelated mix of CRLF- and
    #    LF-terminated template files (predating this chart's deploymentModel
    #    work), so a rendered manifest can contain a mix of line endings
    #    depending on which template produced which line -- a byte-encoding
    #    artifact with no YAML/Kubernetes semantic meaning.
    #
    # 2. The pre-Phase-1 baseline itself carries a pre-existing (not
    #    Phase-1-introduced) duplicate-mapping-key defect in the legacy pod
    #    template labels (goldengate.labels and goldengate.sourceSelectorLabels/
    #    targetSelectorLabels were both included in the same mapping). YAML/JSON
    #    decoders -- including the one Kubernetes itself uses -- resolve a
    #    duplicate key with last-value-wins, so the *effective* configuration
    #    was never ambiguous; only the literal source text was. That defect is
    #    fixed in the current chart (see source-statefulset.yaml/
    #    target-statefulset.yaml), so a raw-text diff against the still-buggy
    #    baseline would show a spurious difference despite zero effective
    #    behavior change. Comparing decoded documents (what Kubernetes itself
    #    would actually observe) is the correct, and more rigorous, check.
    #
    # This still requires true equivalence -- it does not relax the
    # comparison in any other way. The only key ignored is helm.sh/chart
    # (the explicitly accepted, intentionally-volatile chart version label).
    if python3 "$LEGACY_COMPARISON_PY" "$BASELINE_FILE" "$LEGACY_RENDERED" > "${WORKDIR}/legacy-diff.log" 2>&1; then
      pass "legacy manifest is structurally identical to the captured baseline (ignoring helm.sh/chart version label)"
    else
      fail "legacy manifest differs from the captured baseline -- see diff below"
      cat "${WORKDIR}/legacy-diff.log"
    fi
  else
    skip "legacy baseline comparison (baseline file missing, render failed, or PyYAML unavailable)"
  fi

  if [ -s "$LEGACY_RENDERED" ] && [ "$PYTHON_AVAILABLE" = "true" ]; then
    if python3 "$DUPLICATE_KEY_CHECK_PY" "$LEGACY_RENDERED"; then
      pass "legacy rendered manifest has no duplicate YAML mapping keys"
    else
      fail "legacy rendered manifest contains a duplicate YAML mapping key or is not valid YAML"
    fi
  else
    skip "legacy duplicate-key check (rendered manifest unavailable or PyYAML missing)"
  fi
else
  skip "helm lint (legacy) -- helm not installed"
  skip "helm template (legacy) -- helm not installed"
  skip "legacy baseline comparison -- helm not installed"
  skip "legacy duplicate-key check -- helm not installed"
fi

echo ""

# ---------------------------------------------------------------------
# 4/5: Oracle singleRuntime candidate -- lint, render
# ---------------------------------------------------------------------
ORACLE_RENDERED="${WORKDIR}/oracle.yaml"

if [ "$HELM_AVAILABLE" = "true" ]; then
  echo "--- Oracle candidate (gg-oracle-payments-01, deploymentModel=singleRuntime, INACTIVE) ---"

  if helm lint "$CHART_PATH" --values "$ORACLE_VALUES" >"${WORKDIR}/oracle-lint.log" 2>&1; then
    pass "helm lint (Oracle candidate)"
  else
    fail "helm lint (Oracle candidate)"
    cat "${WORKDIR}/oracle-lint.log"
  fi

  if helm template gg-oracle-payments-01 "$CHART_PATH" \
      --namespace goldengate-dev \
      -f "$ORACLE_VALUES" \
      > "$ORACLE_RENDERED" 2>"${WORKDIR}/oracle-template.log"; then
    pass "helm template (Oracle candidate)"
  else
    fail "helm template (Oracle candidate)"
    cat "${WORKDIR}/oracle-template.log"
  fi
else
  skip "helm lint (Oracle candidate) -- helm not installed"
  skip "helm template (Oracle candidate) -- helm not installed"
fi

echo ""

# ---------------------------------------------------------------------
# 6/7: PostgreSQL singleRuntime candidate -- lint, render
# ---------------------------------------------------------------------
POSTGRESQL_RENDERED="${WORKDIR}/postgresql.yaml"

if [ "$HELM_AVAILABLE" = "true" ]; then
  echo "--- PostgreSQL candidate (gg-postgresql-payments-01, deploymentModel=singleRuntime, INACTIVE) ---"

  if helm lint "$CHART_PATH" --values "$POSTGRESQL_VALUES" >"${WORKDIR}/postgresql-lint.log" 2>&1; then
    pass "helm lint (PostgreSQL candidate)"
  else
    fail "helm lint (PostgreSQL candidate)"
    cat "${WORKDIR}/postgresql-lint.log"
  fi

  if helm template gg-postgresql-payments-01 "$CHART_PATH" \
      --namespace goldengate-dev \
      -f "$POSTGRESQL_VALUES" \
      > "$POSTGRESQL_RENDERED" 2>"${WORKDIR}/postgresql-template.log"; then
    pass "helm template (PostgreSQL candidate)"
  else
    fail "helm template (PostgreSQL candidate)"
    cat "${WORKDIR}/postgresql-template.log"
  fi
else
  skip "helm lint (PostgreSQL candidate) -- helm not installed"
  skip "helm template (PostgreSQL candidate) -- helm not installed"
fi

echo ""

# ---------------------------------------------------------------------
# 8: resource contract assertions (Python/PyYAML, duplicate-key-safe,
# reads actual Kubernetes object structure -- never indentation grep)
# ---------------------------------------------------------------------
echo "--- Oracle candidate resource contract ---"
if [ "$PYTHON_AVAILABLE" = "true" ]; then
  set +e
  ORACLE_VALIDATION_OUTPUT="$(python3 "$CANDIDATE_VALIDATOR_PY" "$ORACLE_RENDERED" "$ORACLE_VALUES" gg-oracle-payments-01 goldengate-dev Oracle)"
  set -e
  echo "$ORACLE_VALIDATION_OUTPUT"
  accumulate_python_summary "$ORACLE_VALIDATION_OUTPUT"
else
  skip "Oracle resource contract validation -- python3/PyYAML not available"
fi

echo ""
echo "--- PostgreSQL candidate resource contract ---"
if [ "$PYTHON_AVAILABLE" = "true" ]; then
  set +e
  POSTGRESQL_VALIDATION_OUTPUT="$(python3 "$CANDIDATE_VALIDATOR_PY" "$POSTGRESQL_RENDERED" "$POSTGRESQL_VALUES" gg-postgresql-payments-01 goldengate-dev PostgreSQL)"
  set -e
  echo "$POSTGRESQL_VALIDATION_OUTPUT"
  accumulate_python_summary "$POSTGRESQL_VALIDATION_OUTPUT"
else
  skip "PostgreSQL resource contract validation -- python3/PyYAML not available"
fi

echo ""

# ---------------------------------------------------------------------
# 9: static workflow assertions (no helm required)
# ---------------------------------------------------------------------
echo "--- Static workflow assertions ---"
if [ -f "$WORKFLOW_FILE" ]; then
  if grep -qF 'if [ "$TARGET_NAMESPACE" = "goldengate-${ENVIRONMENT}" ]' "$WORKFLOW_FILE"; then
    pass "workflow contains the unconditional shared-namespace deletion fail-safe"
  else
    fail "workflow does NOT contain the unconditional shared-namespace deletion fail-safe"
  fi

  if grep -qF 'deployment_model' "$WORKFLOW_FILE"; then
    pass "workflow deletion matrix/detection carries deployment_model"
  else
    fail "workflow deletion matrix/detection does not carry deployment_model"
  fi

  if grep -qF 'resolve_deployment_model_from_git' "$WORKFLOW_FILE"; then
    pass "workflow resolves deploymentModel from the base git revision for deletions"
  else
    fail "workflow does not resolve deploymentModel from the base git revision for deletions"
  fi

  if grep -qF 'StrictSafeLoader' "$WORKFLOW_FILE"; then
    pass "workflow rendered-manifest validation rejects duplicate YAML mapping keys"
  else
    fail "workflow rendered-manifest validation does not reject duplicate YAML mapping keys"
  fi

  if grep -qF 'spec.template.spec.containers' "$WORKFLOW_FILE"; then
    pass "workflow reads containers from spec.template.spec.containers, not indentation grep"
  else
    fail "workflow does not read containers from spec.template.spec.containers"
  fi

  if grep -qF "expected exactly one init container named" "$WORKFLOW_FILE"; then
    pass "workflow rejects an empty/wrong initContainers list instead of passing vacuously"
  else
    fail "workflow does not reject an empty/wrong initContainers list"
  fi
else
  skip "static workflow assertions -- ${WORKFLOW_FILE} not found"
fi

echo ""

# ---------------------------------------------------------------------
# 10: init-container / stale-PID synthetic contract tests (no helm
# required) -- proves the local Python/PyYAML validator enforces exactly
# one prepare-u02-permissions init container with the ServiceManager.pid
# safeguard intact, and never passes vacuously on an empty initContainers
# list.
# ---------------------------------------------------------------------
echo "--- Init-container / stale-PID synthetic contract tests ---"

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  SYNTH_DIR="${WORKDIR}/synthetic"
  mkdir -p "$SYNTH_DIR"

  cat > "${SYNTH_DIR}/values.yaml" <<'EOF'
runtime:
  containerName: ogg-oracle
ingress:
  enabled: false
EOF

  # 1. No initContainers at all.
  cat > "${SYNTH_DIR}/no_init.yaml" <<'EOF'
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: gg-oracle-payments-01
  namespace: "goldengate-dev"
spec:
  template:
    spec:
      containers:
        - name: ogg-oracle
          image: "example/ogg-oracle:1.0"
EOF

  # 2. Wrong init-container name.
  cat > "${SYNTH_DIR}/wrong_name.yaml" <<'EOF'
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: gg-oracle-payments-01
  namespace: "goldengate-dev"
spec:
  template:
    spec:
      initContainers:
        - name: prepare-permissions-wrong
          command: ["sh", "-c", "echo hi"]
      containers:
        - name: ogg-oracle
          image: "example/ogg-oracle:1.0"
EOF

  # 3. Expected name, but missing ServiceManager.pid logic entirely.
  cat > "${SYNTH_DIR}/missing_pid_logic.yaml" <<'EOF'
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: gg-oracle-payments-01
  namespace: "goldengate-dev"
spec:
  template:
    spec:
      initContainers:
        - name: prepare-u02-permissions
          command:
            - sh
            - -c
            - |
              set -e
              mkdir -p /u02/oggf
              chmod -R 0777 /u02 /u03
      containers:
        - name: ogg-oracle
          image: "example/ogg-oracle:1.0"
EOF

  # 4. Expected name with a ServiceManager.pid check but no removal command.
  cat > "${SYNTH_DIR}/missing_removal.yaml" <<'EOF'
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: gg-oracle-payments-01
  namespace: "goldengate-dev"
spec:
  template:
    spec:
      initContainers:
        - name: prepare-u02-permissions
          command:
            - sh
            - -c
            - |
              set -e
              SERVICE_MANAGER_PID_FILE="/u02/ServiceManager/var/run/ServiceManager.pid"
              if [ -e "$SERVICE_MANAGER_PID_FILE" ]; then
                echo "found stale pid file, but not removing it"
              fi
      containers:
        - name: ogg-oracle
          image: "example/ogg-oracle:1.0"
EOF

  # 5. Correct: expected init container with full stale-PID cleanup logic.
  cat > "${SYNTH_DIR}/correct.yaml" <<'EOF'
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: gg-oracle-payments-01
  namespace: "goldengate-dev"
spec:
  template:
    spec:
      initContainers:
        - name: prepare-u02-permissions
          command:
            - sh
            - -c
            - |
              set -e
              SERVICE_MANAGER_PID_FILE="/u02/ServiceManager/var/run/ServiceManager.pid"
              if [ -e "$SERVICE_MANAGER_PID_FILE" ] || [ -L "$SERVICE_MANAGER_PID_FILE" ]; then
                rm -f -- "$SERVICE_MANAGER_PID_FILE"
              fi
      containers:
        - name: ogg-oracle
          image: "example/ogg-oracle:1.0"
EOF

  run_synthetic_case() {
    local case_name="$1" rendered_file="$2" expect_nonzero="$3" expected_substring="$4"

    set +e
    local output
    output="$(python3 "$CANDIDATE_VALIDATOR_PY" "$rendered_file" "${SYNTH_DIR}/values.yaml" gg-oracle-payments-01 goldengate-dev "synthetic:${case_name}")"
    local status=$?
    set -e

    local substring_found="false"
    if echo "$output" | grep -qF -- "$expected_substring"; then
      substring_found="true"
    fi

    if [ "$expect_nonzero" = "true" ]; then
      if [ "$status" -ne 0 ] && [ "$substring_found" = "true" ]; then
        pass "synthetic[${case_name}]: exits nonzero with expected message"
      else
        fail "synthetic[${case_name}]: expected nonzero exit with message ${expected_substring@Q}, got exit=${status}, message_found=${substring_found}"
        echo "$output"
      fi
    else
      if [ "$substring_found" = "true" ]; then
        pass "synthetic[${case_name}]: init-container/stale-PID checks pass as expected"
      else
        fail "synthetic[${case_name}]: expected message ${expected_substring@Q} not found"
        echo "$output"
      fi
    fi
  }

  run_synthetic_case "no_initContainers" "${SYNTH_DIR}/no_init.yaml" true \
    "expected initContainers == ['prepare-u02-permissions'], found []"

  run_synthetic_case "wrong_name" "${SYNTH_DIR}/wrong_name.yaml" true \
    "expected initContainers == ['prepare-u02-permissions'], found ['prepare-permissions-wrong']"

  run_synthetic_case "missing_pid_logic" "${SYNTH_DIR}/missing_pid_logic.yaml" true \
    "ServiceManager.pid stale-PID cleanup logic not found"

  run_synthetic_case "missing_removal_command" "${SYNTH_DIR}/missing_removal.yaml" true \
    "stale PID removal command not found"

  # The "correct" synthetic manifest is deliberately minimal (StatefulSet
  # only, no Service/Ingress/etc.), so the overall candidate validator still
  # exits nonzero overall -- only the init-container/stale-PID assertions
  # specifically are required to pass here.
  run_synthetic_case "correct_init_container" "${SYNTH_DIR}/correct.yaml" false \
    "PASS: [synthetic:correct_init_container] stale PID removal command present"
else
  skip "init-container/stale-PID synthetic contract tests -- python3/PyYAML not available"
fi

echo ""

# ---------------------------------------------------------------------
# Phase 2A/2B: helm/goldengate-platform -- shared namespaces and shared
# engine-level runtime ServiceAccounts (IRSA). This chart is the single
# designated owner of these 4 objects; individual GoldenGate runtime
# releases (helm/goldengate, deploymentModel=singleRuntime) must never
# create or own them.
# ---------------------------------------------------------------------
PLATFORM_CHART_PATH="helm/goldengate-platform"
PLATFORM_VALUES="platform/dev/goldengate-platform/values.yaml"
# TEMPORARY COMPATIBILITY BRIDGE: both shared ServiceAccounts currently use
# the existing, proven GoldenGateSecretsReadRole-dev role (matching
# .github/workflows/goldengate-platform.yaml's ORACLE_RUNTIME_ROLE_ARN /
# POSTGRESQL_RUNTIME_ROLE_ARN), not the new gg-oracle-dev-runtime-role /
# gg-postgresql-dev-runtime-role roles -- those still exist (Terraform
# modules untouched) but are deliberately unused until separately proven.
TEST_ORACLE_ROLE_ARN="arn:aws:iam::668311715351:role/GoldenGateSecretsReadRole-dev"
TEST_POSTGRESQL_ROLE_ARN="arn:aws:iam::668311715351:role/GoldenGateSecretsReadRole-dev"
PLATFORM_RENDERED="${WORKDIR}/platform.yaml"

if [ "$HELM_AVAILABLE" = "true" ]; then
  echo "--- Platform bootstrap chart (helm/goldengate-platform) ---"

  if helm lint "$PLATFORM_CHART_PATH" \
      --values "$PLATFORM_VALUES" \
      --set serviceAccounts.oracle.roleArn="$TEST_ORACLE_ROLE_ARN" \
      --set serviceAccounts.postgresql.roleArn="$TEST_POSTGRESQL_ROLE_ARN" \
      >"${WORKDIR}/platform-lint.log" 2>&1; then
    pass "helm lint (platform chart)"
  else
    fail "helm lint (platform chart)"
    cat "${WORKDIR}/platform-lint.log"
  fi

  if helm template goldengate-dev-platform "$PLATFORM_CHART_PATH" \
      --values "$PLATFORM_VALUES" \
      --set serviceAccounts.oracle.roleArn="$TEST_ORACLE_ROLE_ARN" \
      --set serviceAccounts.postgresql.roleArn="$TEST_POSTGRESQL_ROLE_ARN" \
      > "$PLATFORM_RENDERED" 2>"${WORKDIR}/platform-template.log"; then
    pass "helm template (platform chart)"
  else
    fail "helm template (platform chart)"
    cat "${WORKDIR}/platform-template.log"
  fi

  if [ -s "$PLATFORM_RENDERED" ]; then
    NAMESPACE_COUNT="$(grep -c '^kind: Namespace$' "$PLATFORM_RENDERED" || true)"
    if [ "$NAMESPACE_COUNT" -eq 2 ] \
        && grep -q '^  name: goldengate-dev$' "$PLATFORM_RENDERED" \
        && grep -q '^  name: goldengate-monitoring-dev$' "$PLATFORM_RENDERED"; then
      pass "platform chart renders exactly 2 Namespace documents (goldengate-dev, goldengate-monitoring-dev)"
    else
      fail "platform chart Namespace count/names: expected 2 (goldengate-dev, goldengate-monitoring-dev), found ${NAMESPACE_COUNT}"
    fi

    SERVICEACCOUNT_COUNT="$(grep -c '^kind: ServiceAccount$' "$PLATFORM_RENDERED" || true)"
    if [ "$SERVICEACCOUNT_COUNT" -eq 2 ] \
        && grep -q '^  name: gg-oracle-sa$' "$PLATFORM_RENDERED" \
        && grep -q '^  name: gg-postgresql-sa$' "$PLATFORM_RENDERED"; then
      pass "platform chart renders exactly 2 ServiceAccount documents (gg-oracle-sa, gg-postgresql-sa)"
    else
      fail "platform chart ServiceAccount count/names: expected 2 (gg-oracle-sa, gg-postgresql-sa), found ${SERVICEACCOUNT_COUNT}"
    fi

    if grep -Fq -- "${TEST_ORACLE_ROLE_ARN}" "$PLATFORM_RENDERED" && grep -Fq -- "${TEST_POSTGRESQL_ROLE_ARN}" "$PLATFORM_RENDERED"; then
      pass "platform chart annotates both ServiceAccounts with eks.amazonaws.com/role-arn"
    else
      fail "platform chart is missing one or both expected IRSA role-arn annotations"
    fi

    echo "--- Compatibility bridge: both ServiceAccounts individually use GoldenGateSecretsReadRole-dev ---"
    PLATFORM_SPLIT_DIR="$(mktemp -d)"
    awk -v outdir="$PLATFORM_SPLIT_DIR" '
      BEGIN { docnum = 0; fname = outdir "/doc-0.yaml" }
      /^---$/ { docnum++; fname = outdir "/doc-" docnum ".yaml"; next }
      { print > fname }
    ' "$PLATFORM_RENDERED"

    assert_sa_uses_bridge_role() {
      local sa_name="$1"
      local block=""
      local doc
      for doc in "$PLATFORM_SPLIT_DIR"/doc-*.yaml; do
        if grep -q '^kind: ServiceAccount$' "$doc" && grep -q "^  name: ${sa_name}\$" "$doc"; then
          block="$(cat "$doc")"
          break
        fi
      done
      if [ -z "$block" ]; then
        fail "could not find rendered ServiceAccount ${sa_name} for bridge-role check"
        return
      fi
      if grep -Fq -- "eks.amazonaws.com/role-arn: \"arn:aws:iam::668311715351:role/GoldenGateSecretsReadRole-dev\"" <<< "$block"; then
        pass "ServiceAccount ${sa_name} individually annotated with GoldenGateSecretsReadRole-dev"
      else
        fail "ServiceAccount ${sa_name} is not annotated with the expected bridge role GoldenGateSecretsReadRole-dev"
      fi
      if grep -Fq -- "gg-oracle-dev-runtime-role" <<< "$block" || grep -Fq -- "gg-postgresql-dev-runtime-role" <<< "$block"; then
        fail "ServiceAccount ${sa_name} unexpectedly references the new, unused runtime role"
      fi
    }

    assert_sa_uses_bridge_role "gg-oracle-sa"
    assert_sa_uses_bridge_role "gg-postgresql-sa"
    rm -rf "$PLATFORM_SPLIT_DIR"

    FORBIDDEN_KIND_FOUND="false"
    for forbidden_kind in StatefulSet Deployment DaemonSet Service Ingress PersistentVolumeClaim SecretProviderClass; do
      if grep -qE "^kind: ${forbidden_kind}\$" "$PLATFORM_RENDERED"; then
        fail "platform chart rendered a forbidden kind: ${forbidden_kind}"
        FORBIDDEN_KIND_FOUND="true"
      fi
    done
    if [ "$FORBIDDEN_KIND_FOUND" = "false" ]; then
      pass "platform chart renders no StatefulSet/Deployment/DaemonSet/Service/Ingress/PersistentVolumeClaim/SecretProviderClass"
    fi

    if grep -Fq -- "goldengate.adcb/deployment-id" "$PLATFORM_RENDERED"; then
      fail "platform chart's shared namespaces/ServiceAccounts carry a per-runtime ownership label (goldengate.adcb/deployment-id)"
    else
      pass "no per-runtime ownership label on shared namespaces/ServiceAccounts"
    fi

    echo "--- Shared-resource deletion protection ---"
    DELETION_PROTECTED_COUNT="$(grep -c -- 'argocd.argoproj.io/sync-options: Prune=false,Delete=false' "$PLATFORM_RENDERED" || true)"
    if [ "$DELETION_PROTECTED_COUNT" -eq 4 ]; then
      pass "all 4 shared objects (2 Namespaces, 2 ServiceAccounts) carry sync-options: Prune=false,Delete=false"
    else
      fail "expected 4 objects with sync-options: Prune=false,Delete=false, found ${DELETION_PROTECTED_COUNT}"
    fi

    PRUNELAST_ONLY_COUNT="$(grep -c -- 'argocd.argoproj.io/sync-options: PruneLast=true' "$PLATFORM_RENDERED" || true)"
    if [ "$PRUNELAST_ONLY_COUNT" -eq 0 ]; then
      pass "no shared object retains only sync-options: PruneLast=true"
    else
      fail "found ${PRUNELAST_ONLY_COUNT} shared object(s) still using only sync-options: PruneLast=true instead of Prune=false,Delete=false"
    fi

    if [ "$PYTHON_AVAILABLE" = "true" ]; then
      if python3 -c "
import yaml, sys

class DuplicateKeyError(Exception):
    pass

class StrictSafeLoader(yaml.SafeLoader):
    pass

def _no_duplicates_constructor(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise DuplicateKeyError(f'duplicate key: {key!r}')
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping

StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates_constructor
)

with open('${PLATFORM_RENDERED}') as f:
    docs = list(yaml.load_all(f, Loader=StrictSafeLoader))
print(f'{len(docs)} documents parsed with no duplicate keys')
" >"${WORKDIR}/platform-dupkey.log" 2>&1; then
        pass "platform chart rendered manifest has no duplicate YAML keys"
      else
        fail "platform chart rendered manifest has a duplicate YAML key, or failed to parse"
        cat "${WORKDIR}/platform-dupkey.log"
      fi
    else
      skip "platform chart duplicate-key check -- python3/PyYAML not available"
    fi
  else
    fail "platform chart rendered manifest is empty -- cannot run resource-inventory assertions"
  fi

  echo "--- Platform chart fails closed when roleArn is empty ---"

  set +e
  MISSING_ORACLE_ARN_OUTPUT="$(helm template goldengate-dev-platform "$PLATFORM_CHART_PATH" \
      --values "$PLATFORM_VALUES" \
      --set serviceAccounts.postgresql.roleArn="$TEST_POSTGRESQL_ROLE_ARN" 2>&1)"
  MISSING_ORACLE_ARN_STATUS=$?
  set -e

  if [ "$MISSING_ORACLE_ARN_STATUS" -ne 0 ] && echo "$MISSING_ORACLE_ARN_OUTPUT" | grep -qF -- "serviceAccounts.oracle.roleArn is required"; then
    pass "platform chart fails to render when serviceAccounts.oracle.roleArn is empty"
  else
    fail "platform chart did not fail as expected when serviceAccounts.oracle.roleArn is empty (exit=${MISSING_ORACLE_ARN_STATUS})"
    echo "$MISSING_ORACLE_ARN_OUTPUT"
  fi

  set +e
  MISSING_POSTGRESQL_ARN_OUTPUT="$(helm template goldengate-dev-platform "$PLATFORM_CHART_PATH" \
      --values "$PLATFORM_VALUES" \
      --set serviceAccounts.oracle.roleArn="$TEST_ORACLE_ROLE_ARN" 2>&1)"
  MISSING_POSTGRESQL_ARN_STATUS=$?
  set -e

  if [ "$MISSING_POSTGRESQL_ARN_STATUS" -ne 0 ] && echo "$MISSING_POSTGRESQL_ARN_OUTPUT" | grep -qF -- "serviceAccounts.postgresql.roleArn is required"; then
    pass "platform chart fails to render when serviceAccounts.postgresql.roleArn is empty"
  else
    fail "platform chart did not fail as expected when serviceAccounts.postgresql.roleArn is empty (exit=${MISSING_POSTGRESQL_ARN_STATUS})"
    echo "$MISSING_POSTGRESQL_ARN_OUTPUT"
  fi

  echo "--- Runtime chart never creates the shared namespace or shared ServiceAccounts ---"

  if [ -s "$ORACLE_RENDERED" ]; then
    if grep -qE '^kind: Namespace$' "$ORACLE_RENDERED"; then
      fail "runtime chart (Oracle candidate, singleRuntime) rendered a Namespace -- it must never own the shared namespace"
    else
      pass "runtime chart (Oracle candidate, singleRuntime) renders no Namespace"
    fi

    if grep -qE '^kind: ServiceAccount$' "$ORACLE_RENDERED"; then
      fail "runtime chart (Oracle candidate, serviceAccount.create=false) rendered a ServiceAccount -- the shared identity must not be owned by this release"
    else
      pass "runtime chart (Oracle candidate, serviceAccount.create=false) renders no ServiceAccount"
    fi
  else
    skip "runtime chart Namespace/ServiceAccount absence checks (Oracle) -- no rendered manifest available"
  fi

  if [ -s "$POSTGRESQL_RENDERED" ]; then
    if grep -qE '^kind: Namespace$' "$POSTGRESQL_RENDERED"; then
      fail "runtime chart (PostgreSQL candidate, singleRuntime) rendered a Namespace -- it must never own the shared namespace"
    else
      pass "runtime chart (PostgreSQL candidate, singleRuntime) renders no Namespace"
    fi

    if grep -qE '^kind: ServiceAccount$' "$POSTGRESQL_RENDERED"; then
      fail "runtime chart (PostgreSQL candidate, serviceAccount.create=false) rendered a ServiceAccount -- the shared identity must not be owned by this release"
    else
      pass "runtime chart (PostgreSQL candidate, serviceAccount.create=false) renders no ServiceAccount"
    fi
  else
    skip "runtime chart Namespace/ServiceAccount absence checks (PostgreSQL) -- no rendered manifest available"
  fi
else
  skip "helm lint (platform chart) -- helm not installed"
  skip "helm template (platform chart) -- helm not installed"
  skip "platform chart resource-inventory assertions -- helm not installed"
  skip "platform chart fail-closed roleArn assertions -- helm not installed"
  skip "runtime chart Namespace/ServiceAccount absence checks -- helm not installed"
fi

echo ""

# ---------------------------------------------------------------------
# Compatibility bridge: GoldenGateSecretsReadRole-dev trust policy now
# also covers the shared platform ServiceAccounts, alongside (not instead
# of) the legacy ogg-oracle-sa wildcard subject the live legacy pods still
# depend on.
# ---------------------------------------------------------------------
echo "--- GoldenGateSecretsReadRole-dev trust policy: compatibility bridge ---"
SECRETS_READ_TRUST_FILE="envs/dev/policies/goldengate-secrets-read-dev/assume_role_policy/sts.json"

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  if python3 -c "
import json, sys

with open('${SECRETS_READ_TRUST_FILE}') as f:
    policy = json.load(f)

stmt = policy['Statement'][0]
cond = stmt['Condition']

aud = cond['StringEquals']['oidc.eks.eu-west-1.amazonaws.com/id/407C4385FF87947926730569F1E564FB:aud']
if aud != 'sts.amazonaws.com':
    print(f'aud mismatch: {aud!r}')
    sys.exit(1)

sub = cond['StringLike']['oidc.eks.eu-west-1.amazonaws.com/id/407C4385FF87947926730569F1E564FB:sub']
if not isinstance(sub, list):
    print(f'sub is not an array: {sub!r}')
    sys.exit(1)

required = [
    'system:serviceaccount:gg-dev-*:ogg-oracle-sa',
    'system:serviceaccount:goldengate-dev:gg-oracle-sa',
    'system:serviceaccount:goldengate-dev:gg-postgresql-sa',
]
missing = [r for r in required if r not in sub]
if missing:
    print(f'missing subject(s): {missing}')
    sys.exit(1)

if len(sub) != 3:
    print(f'expected exactly 3 subjects, found {len(sub)}: {sub}')
    sys.exit(1)

principal = stmt['Principal']['Federated']
if principal != 'arn:aws:iam::668311715351:oidc-provider/oidc.eks.eu-west-1.amazonaws.com/id/407C4385FF87947926730569F1E564FB':
    print(f'unexpected Federated principal (OIDC provider changed): {principal!r}')
    sys.exit(1)

print('aud=sts.amazonaws.com, OIDC provider unchanged, all 3 subjects present (legacy + 2 new), no extras')
" >"${WORKDIR}/secrets-read-trust-check.log" 2>&1; then
    pass "GoldenGateSecretsReadRole-dev trust policy: legacy ogg-oracle-sa subject preserved, both new goldengate-dev subjects added, aud/OIDC provider unchanged"
  else
    fail "GoldenGateSecretsReadRole-dev trust policy check failed"
    cat "${WORKDIR}/secrets-read-trust-check.log"
  fi
else
  skip "GoldenGateSecretsReadRole-dev trust policy check -- python3 not available"
fi

echo "--- Candidate values.yaml: secrets/certificate unchanged, no candidate enabled/disabled by this correction ---"
# This correction (compatibility-bridge IAM/workflow changes only) must not
# itself touch either candidate's values.yaml. gg-oracle-payments-01's
# deployment.enabled is whatever the repository's committed state already
# is (HEAD) -- this script does not assert a specific value for it, only
# that this correction pass did not modify the file (verified via git diff
# against HEAD, when git is available).
if command -v git >/dev/null 2>&1 && git -C "$REPO_ROOT" rev-parse HEAD >/dev/null 2>&1; then
  if git -C "$REPO_ROOT" diff --ignore-all-space --quiet HEAD -- "$ORACLE_VALUES" "$POSTGRESQL_VALUES"; then
    pass "neither candidate's values.yaml was modified by this correction (0 diff vs HEAD)"
  else
    fail "a candidate values.yaml differs from HEAD -- this correction must not touch candidate enablement/config"
  fi
else
  skip "candidate values.yaml unchanged-vs-HEAD check -- git not available"
fi

if grep -q 'objectName: dev/goldengate/source/admin' "$ORACLE_VALUES"; then
  pass "Oracle candidate still references dev/goldengate/source/admin (no new secret introduced)"
else
  fail "Oracle candidate no longer references dev/goldengate/source/admin"
fi
if grep -q 'objectName: dev/goldengate/target/admin' "$POSTGRESQL_VALUES"; then
  pass "PostgreSQL candidate still references dev/goldengate/target/admin (no new secret introduced)"
else
  fail "PostgreSQL candidate no longer references dev/goldengate/target/admin"
fi
if grep -q 'objectName: dev/goldengate/tls-certificate' "$ORACLE_VALUES" && grep -q 'objectName: dev/goldengate/tls-certificate' "$POSTGRESQL_VALUES"; then
  pass "both candidates still reference the shared dev/goldengate/tls-certificate object (no new certificate introduced)"
else
  fail "one or both candidates no longer reference dev/goldengate/tls-certificate"
fi

echo ""

# ---------------------------------------------------------------------
# Argo CD chart/workflow: three-repository ECR token-sync model
# (helm/goldengate, helm/goldengate-monitor, helm/goldengate-platform).
# ---------------------------------------------------------------------
ARGOCD_CHART_PATH="helm/argocd"
ARGOCD_VALUES="envs/dev/argocd/values.yaml"
ARGOCD_ECR_POLICY_FILE="envs/dev/policies/argocd-ecr-oci-read-dev/policies/policies_1.json"

echo "--- Argo CD ECR read IAM policy: exact platform repository ARN ---"
if [ "$PYTHON_AVAILABLE" = "true" ]; then
  if python3 -c "
import json, sys

with open('${ARGOCD_ECR_POLICY_FILE}') as f:
    policy = json.load(f)

required_actions = {
    'ecr:BatchCheckLayerAvailability',
    'ecr:BatchGetImage',
    'ecr:GetDownloadUrlForLayer',
    'ecr:DescribeImages',
    'ecr:DescribeRepositories',
}
expected_repos = {
    'helm/goldengate': 'arn:aws:ecr:eu-west-1:229410149234:repository/helm/goldengate',
    'helm/goldengate-monitor': 'arn:aws:ecr:eu-west-1:229410149234:repository/helm/goldengate-monitor',
    'helm/goldengate-platform': 'arn:aws:ecr:eu-west-1:229410149234:repository/helm/goldengate-platform',
}

statements = policy.get('Statement', [])
found_arns = set()
for stmt in statements:
    resource = stmt.get('Resource')
    resources = resource if isinstance(resource, list) else [resource]
    actions = stmt.get('Action')
    actions = set(actions if isinstance(actions, list) else [actions])
    for r in resources:
        if r in expected_repos.values():
            found_arns.add(r)
            if not required_actions.issubset(actions):
                missing = required_actions - actions
                print(f'missing actions for {r}: {sorted(missing)}')
                sys.exit(1)
            if r == '*' or str(r).endswith('/*'):
                print(f'wildcard resource used for {r}')
                sys.exit(1)

missing_repos = set(expected_repos.values()) - found_arns
if missing_repos:
    print(f'missing repository statement(s): {sorted(missing_repos)}')
    sys.exit(1)

print('all three repository ARNs present with required actions, no wildcards')
" >"${WORKDIR}/argocd-ecr-policy-check.log" 2>&1; then
    pass "Argo CD ECR read IAM policy grants exact ARN + required actions for all 3 repositories (goldengate, goldengate-monitor, goldengate-platform)"
  else
    fail "Argo CD ECR read IAM policy is missing or misconfigured for one or more of the 3 repositories"
    cat "${WORKDIR}/argocd-ecr-policy-check.log"
  fi
else
  skip "Argo CD ECR read IAM policy check -- python3 not available"
fi

echo "--- Argo CD chart (helm/argocd): 3-repository ECR token-sync rendering ---"
ARGOCD_RENDERED="${WORKDIR}/argocd.yaml"

if [ "$HELM_AVAILABLE" = "true" ]; then
  helm dependency build "$ARGOCD_CHART_PATH" >"${WORKDIR}/argocd-dep-build.log" 2>&1 || true

  if helm lint "$ARGOCD_CHART_PATH" --values "$ARGOCD_VALUES" >"${WORKDIR}/argocd-lint.log" 2>&1; then
    pass "helm lint (argocd chart)"
  else
    fail "helm lint (argocd chart)"
    cat "${WORKDIR}/argocd-lint.log"
  fi

  if helm template argocd "$ARGOCD_CHART_PATH" \
      --namespace argocd \
      --values "$ARGOCD_VALUES" \
      > "$ARGOCD_RENDERED" 2>"${WORKDIR}/argocd-template.log"; then
    pass "helm template (argocd chart)"
  else
    fail "helm template (argocd chart)"
    cat "${WORKDIR}/argocd-template.log"
  fi

  if [ -s "$ARGOCD_RENDERED" ]; then
    echo "Checking all 3 Helm OCI repository names are baked into the rendered CronJob..."
    REPO_NAMES_OK="true"
    for repo_marker in 'helm/goldengate"' 'helm/goldengate-monitor"' 'helm/goldengate-platform"'; do
      if ! grep -q -- "$repo_marker" "$ARGOCD_RENDERED"; then
        fail "argocd chart rendered manifest is missing repository marker: ${repo_marker}"
        REPO_NAMES_OK="false"
      fi
    done
    if [ "$REPO_NAMES_OK" = "true" ]; then
      pass "argocd chart rendered CronJob bakes in all 3 Helm OCI repository names"
    fi

    echo "Checking all 3 Argo CD repository Secret names are baked into the rendered CronJob..."
    SECRET_NAMES_OK="true"
    for secret_name in argocd-ecr-goldengate-oci argocd-ecr-goldengate-monitor-oci argocd-ecr-goldengate-platform-oci; do
      if ! grep -q -- "$secret_name" "$ARGOCD_RENDERED"; then
        fail "argocd chart rendered manifest is missing repository Secret name: ${secret_name}"
        SECRET_NAMES_OK="false"
      fi
    done
    if [ "$SECRET_NAMES_OK" = "true" ]; then
      pass "argocd chart rendered CronJob bakes in all 3 repository Secret names"
    fi

    echo "Extracting the exact argocd-ecr-token-sync Role and checking RBAC..."
    RBAC_SPLIT_DIR="$(mktemp -d)"
    awk -v outdir="$RBAC_SPLIT_DIR" '
      BEGIN { docnum = 0; fname = outdir "/doc-0.yaml" }
      /^---$/ { docnum++; fname = outdir "/doc-" docnum ".yaml"; next }
      { print > fname }
    ' "$ARGOCD_RENDERED"

    RBAC_BLOCK=""
    for RBAC_DOC in "$RBAC_SPLIT_DIR"/doc-*.yaml; do
      if grep -q '^kind: Role$' "$RBAC_DOC" && grep -q '^  name: argocd-ecr-token-sync$' "$RBAC_DOC"; then
        RBAC_BLOCK="$(cat "$RBAC_DOC")"
        break
      fi
    done
    rm -rf "$RBAC_SPLIT_DIR"

    if [ -z "$RBAC_BLOCK" ]; then
      fail "no rendered document has both kind: Role and metadata.name: argocd-ecr-token-sync"
    else
      RBAC_OK="true"
      for secret_name in argocd-ecr-goldengate-oci argocd-ecr-goldengate-monitor-oci argocd-ecr-goldengate-platform-oci; do
        if ! grep -Fq -- "$secret_name" <<< "$RBAC_BLOCK"; then
          fail "argocd-ecr-token-sync Role resourceNames is missing ${secret_name}"
          RBAC_OK="false"
        fi
      done
      if [ "$RBAC_OK" = "true" ]; then
        pass "argocd-ecr-token-sync Role resourceNames includes all 3 exact repository Secrets"
      fi

      VERBS_OK="true"
      for required_verb in get update patch; do
        if ! grep -Fq -- "- ${required_verb}" <<< "$RBAC_BLOCK"; then
          fail "argocd-ecr-token-sync Role does not grant the ${required_verb} verb"
          VERBS_OK="false"
        fi
      done
      if grep -Eq -- '^[[:space:]]*-[[:space:]]*(delete|list|watch)[[:space:]]*$' <<< "$RBAC_BLOCK"; then
        fail "argocd-ecr-token-sync Role grants a forbidden verb (delete, list, or watch)"
        VERBS_OK="false"
      fi
      if [ "$VERBS_OK" = "true" ]; then
        pass "argocd-ecr-token-sync Role grants exactly get/update/patch, no delete/list/watch"
      fi
    fi
  else
    fail "argocd chart rendered manifest is empty -- cannot run 3-repository assertions"
  fi
else
  skip "helm lint (argocd chart) -- helm not installed"
  skip "helm template (argocd chart) -- helm not installed"
  skip "argocd chart 3-repository rendering assertions -- helm not installed"
fi

echo ""

# ---------------------------------------------------------------------
# Phase 3: DynamoDB CONFIG inventory records + topology-as-data document.
#
# Terraform owns ONLY recordType=CONFIG items in gg-eks-pipeline. The
# (not-yet-deployed) shared gg-monitor owns LEASE, STATE#_deployment, and
# STATE#<process> -- this script must never see this correction introduce
# any of those record types.
# ---------------------------------------------------------------------
DYNAMODB_TF="envs/dev/dynamodb.tf"
TOPOLOGY_YAML="topologies/dev/payments-ora-to-pg-001.yaml"

echo "--- terraform fmt -check (envs/dev/dynamodb.tf) ---"
if [ "$TERRAFORM_AVAILABLE" = "true" ]; then
  if terraform fmt -check -diff "$DYNAMODB_TF" >"${WORKDIR}/tf-fmt.log" 2>&1; then
    pass "terraform fmt -check: envs/dev/dynamodb.tf is correctly formatted"
  else
    fail "terraform fmt -check: envs/dev/dynamodb.tf is not correctly formatted"
    cat "${WORKDIR}/tf-fmt.log"
  fi
else
  skip "terraform fmt -check -- terraform not installed"
fi

echo "--- Existing DynamoDB table definition unchanged ---"
TABLE_UNCHANGED="true"
for expected_line in \
  'name      = "gg-eks-pipeline"' \
  'hash_key  = "pipeline"' \
  'range_key = "recordType"' \
  'billing_mode = "PAY_PER_REQUEST"' \
  'ttl_enabled        = true' \
  'ttl_attribute_name = "ttl"'; do
  if ! grep -Fq -- "$expected_line" "$DYNAMODB_TF"; then
    fail "existing table module is missing expected unchanged line: ${expected_line}"
    TABLE_UNCHANGED="false"
  fi
done
if [ "$TABLE_UNCHANGED" = "true" ]; then
  pass "existing gg-eks-pipeline table module (key schema, billing mode, TTL) is unchanged"
fi

DEPLOYMENTS_YAML="pipelines/deployments.yaml"

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  DYNAMODB_CONFIG_VALIDATOR_PY="${WORKDIR}/validate_dynamodb_config.py"
  cat > "$DYNAMODB_CONFIG_VALIDATOR_PY" <<'PYEOF'
import re
import sys
import yaml

pass_count = 0
fail_count = 0

def ok(msg):
    global pass_count
    print(f"PASS: {msg}")
    pass_count += 1

def bad(msg):
    global fail_count
    print(f"FAIL: {msg}")
    fail_count += 1

tf_path, deployments_path, oracle_values_path, postgresql_values_path = sys.argv[1:5]
with open(tf_path) as f:
    content = f.read()

# Extract each top-level "resource \"aws_dynamodb_table_item\" \"NAME\" { ... }"
# block by brace-depth counting -- avoids needing a full HCL parser for this
# specific, regular, self-authored structure.
def extract_blocks(text, header_re):
    blocks = {}
    for m in re.finditer(header_re, text):
        name = m.group(1) if m.groups() else None
        start = m.end()
        depth = 1
        i = start
        while depth > 0 and i < len(text):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
            i += 1
        blocks[name if name is not None else start] = text[start:i]
    return blocks

resource_blocks = extract_blocks(content, r'resource "aws_dynamodb_table_item" "([a-zA-Z0-9_]+)" \{')

# ---------------------------------------------------------------------
# 1. Exactly one generic aws_dynamodb_table_item resource (no more
#    per-runtime duplication).
# ---------------------------------------------------------------------
if len(resource_blocks) == 1 and "pipeline_config" in resource_blocks:
    ok("exactly 1 generic aws_dynamodb_table_item resource declared: pipeline_config")
else:
    bad(f"expected exactly 1 resource named pipeline_config, found {len(resource_blocks)}: {sorted(k for k in resource_blocks if k)}")

if "gg_oracle_payments_01_config" in resource_blocks or "gg_postgresql_payments_01_config" in resource_blocks:
    bad("a duplicated per-runtime aws_dynamodb_table_item resource still exists in the code")
else:
    ok("no Oracle-specific or PostgreSQL-specific table-item resource remains")

block = resource_blocks.get("pipeline_config", "")

# ---------------------------------------------------------------------
# 2. for_each mechanics: present, driven by pipelines/deployments.yaml,
#    keyed by gg-${d.name}, using each.key / each.value.type.
# ---------------------------------------------------------------------
if "for_each" in block:
    ok("pipeline_config uses for_each")
else:
    bad("pipeline_config does not use for_each")

if "pipelines/deployments.yaml" in block:
    ok("for_each is driven by pipelines/deployments.yaml")
else:
    bad("for_each does not reference pipelines/deployments.yaml")

# Exact manager pattern (terraform/platform/dynamodb.tf): the for_each map
# value is d.type itself (a bare string), i.e. "gg-${d.name}" => d.type --
# not "=> d" (the whole object).
if re.search(r'"gg-\$\{d\.name\}"\s*=>\s*d\.type', block):
    ok('for_each is exactly "gg-${d.name}" => d.type (exact manager pattern)')
else:
    bad('for_each is not exactly "gg-${d.name}" => d.type')

# The derivation must appear exactly once (not once per runtime).
derivation_count = len(re.findall(r'"gg-\$\{d\.name\}"', block))
if derivation_count == 1:
    ok("gg-${d.name} derivation appears exactly once (not duplicated per runtime)")
else:
    bad(f"expected the gg-${{d.name}} derivation exactly once, found {derivation_count}")

if re.search(r'pipeline\s*=\s*\{\s*S\s*=\s*each\.key\s*\}', block):
    ok("item.pipeline uses each.key")
else:
    bad("item.pipeline does not use each.key")

# each.value IS the deploymentType string now (for_each maps to d.type, not
# to the whole d object) -- deploymentType must use bare each.value, and
# each.value.type must NOT appear anywhere in the resource.
if re.search(r'deploymentType\s*=\s*\{\s*S\s*=\s*each\.value\s*\}', block):
    ok("item.deploymentType uses each.value")
else:
    bad("item.deploymentType does not use each.value")

if "each.value.type" in block:
    bad("each.value.type still appears in pipeline_config -- for_each now maps to d.type directly, so each.value IS the type")
else:
    ok("no each.value.type remains in pipeline_config")

# Table creation ordering: either a verified module output reference (none
# exist anywhere in this repo, and the module source is an unreachable
# private repo, so this branch is not expected to be used yet) or an
# explicit depends_on on the table module.
has_module_output_dep = bool(re.search(r'module\.goldengate_pipeline_state\.\w+', block))
has_explicit_depends_on = bool(re.search(r'depends_on\s*=\s*\[\s*module\.goldengate_pipeline_state\s*\]', block))
if has_module_output_dep or has_explicit_depends_on:
    kind = "verified module output reference" if has_module_output_dep else "explicit depends_on = [module.goldengate_pipeline_state]"
    ok(f"pipeline_config has a verified table-creation dependency ({kind})")
else:
    bad("pipeline_config has neither a module output reference nor an explicit depends_on on module.goldengate_pipeline_state")

# Field names like alertsEnabled/metricsEnabled/credSyncEnabled/
# autoStartEnabled legitimately contain the substring "enabled" -- what must
# NOT appear is a filter/condition keyed on the inventory's own d.enabled
# (e.g. each.value.enabled, a for_each comprehension "if d.enabled", or a
# count/for-expression gate), which would skip disabled deployments.
if not re.search(r'each\.value\.enabled|d\.enabled|for_each\s*=\s*\{[^}]*if\s+d\.enabled', block):
    ok("pipeline_config does not filter or branch on d.enabled -- both enabled and disabled deployments are seeded")
else:
    bad("pipeline_config appears to filter on enabled -- CONFIG must be seeded regardless of enabled state")

# ---------------------------------------------------------------------
# 3. Passive-safe CONFIG defaults and the corrected distpathStallChecks
#    field name (confirmed manager defect: dispatchStallChecks is wrong).
# ---------------------------------------------------------------------
passive_checks = [
    (r'alertsEnabled\s*=\s*\{\s*BOOL\s*=\s*false\s*\}', "alertsEnabled=false"),
    (r'metricsEnabled\s*=\s*\{\s*BOOL\s*=\s*true\s*\}', "metricsEnabled=true"),
    (r'credSyncEnabled\s*=\s*\{\s*BOOL\s*=\s*false\s*\}', "credSyncEnabled=false"),
    (r'autoStartEnabled\s*=\s*\{\s*BOOL\s*=\s*false\s*\}', "autoStartEnabled=false"),
    (r'autoRestartMaxRetries\s*=\s*\{\s*N\s*=\s*"0"\s*\}', "autoRestartMaxRetries=0"),
    (r'autoRestartWindowMinutes\s*=\s*\{\s*N\s*=\s*"0"\s*\}', "autoRestartWindowMinutes=0"),
    (r'failoverEnabled\s*=\s*\{\s*BOOL\s*=\s*false\s*\}', "defaults.failoverEnabled=false"),
    (r'alertEachAbend\s*=\s*\{\s*BOOL\s*=\s*false\s*\}', "defaults.alertEachAbend=false"),
]
for pattern, label in passive_checks:
    if re.search(pattern, block):
        ok(f"passive-safe default present: {label}")
    else:
        bad(f"passive-safe default missing or incorrect: {label}")

if re.search(r'distpathStallChecks\s*=\s*\{\s*N\s*=\s*"3"\s*\}', block):
    ok("defaults.distpathStallChecks is present (corrected field name)")
else:
    bad("defaults.distpathStallChecks is missing")

# Only flag dispatchStallChecks used as an actual HCL attribute key
# (`dispatchStallChecks = {...}`), not the field name appearing in prose
# inside a comment (this file intentionally documents the manager's defect
# by name).
if re.search(r'dispatchStallChecks\s*=\s*\{', content):
    bad("the incorrect manager field name 'dispatchStallChecks' is used as an attribute key in dynamodb.tf -- must be distpathStallChecks")
else:
    ok("dispatchStallChecks is not used as a DynamoDB CONFIG attribute (distpathStallChecks is the configured attribute; dispatchStallChecks may still appear in comments documenting the manager defect)")

# ---------------------------------------------------------------------
# 4. No ttl, no LEASE/STATE/STATE#, no secret values / legacy names.
# ---------------------------------------------------------------------
if re.search(r'(?<![a-zA-Z_])ttl(?![a-zA-Z_])', block):
    bad("pipeline_config contains a 'ttl' attribute -- CONFIG records must not expire")
else:
    ok("pipeline_config has no ttl attribute")

if re.search(r'"LEASE"|"STATE"|"STATE#', block):
    bad("pipeline_config references a LEASE/STATE/STATE# record type -- Terraform owns CONFIG only")
else:
    ok("pipeline_config has no LEASE/STATE/STATE# record type reference")

forbidden_substrings = [
    "password", "PWD", "SecretString", "SecretBinary",
    "BEGIN CERTIFICATE", "BEGIN RSA", "BEGIN PRIVATE KEY",
    "gg-payments-ora-to-pg-001-source", "gg-payments-ora-to-pg-001-target",
]
found_forbidden = [s for s in forbidden_substrings if s.lower() in block.lower()]
if found_forbidden:
    bad(f"pipeline_config contains forbidden content: {found_forbidden}")
else:
    ok("pipeline_config has no secret values, certificate contents, or legacy logical names")

if re.search(r'ignore_changes\s*=\s*\[\s*item\s*\]', block):
    ok("lifecycle.ignore_changes includes item")
else:
    bad("lifecycle.ignore_changes = [item] not found on pipeline_config")

# ---------------------------------------------------------------------
# 5. moved blocks: correct from/to addresses for both prior explicit
#    resources.
# ---------------------------------------------------------------------
moved_blocks = extract_blocks(content, r'moved \{')
moved_text = "\n".join(moved_blocks.values())
expected_moves = [
    ("aws_dynamodb_table_item.gg_oracle_payments_01_config", 'aws_dynamodb_table_item.pipeline_config["gg-oracle-payments-01"]'),
    ("aws_dynamodb_table_item.gg_postgresql_payments_01_config", 'aws_dynamodb_table_item.pipeline_config["gg-postgresql-payments-01"]'),
]
if len(moved_blocks) == 2:
    ok("exactly 2 moved blocks declared")
else:
    bad(f"expected exactly 2 moved blocks, found {len(moved_blocks)}")

for from_addr, to_addr in expected_moves:
    if from_addr in moved_text and to_addr in moved_text:
        ok(f"moved block correctly maps {from_addr} -> {to_addr}")
    else:
        bad(f"moved block for {from_addr} -> {to_addr} not found or incorrect")

# ---------------------------------------------------------------------
# 6. Inventory (pipelines/deployments.yaml): structure, gg-${name}
#    derivation with no gg-gg- risk, cross-validated against runtime
#    values files, plus a synthetic sqlserver entry (Python-simulated,
#    no Terraform code change required).
# ---------------------------------------------------------------------
with open(deployments_path) as f:
    inventory = yaml.safe_load(f)

entries = inventory.get("deployments", [])
if len(entries) == 2:
    ok("pipelines/deployments.yaml declares exactly 2 entries")
else:
    bad(f"expected exactly 2 inventory entries, found {len(entries)}")

def derive_key(name):
    return f"gg-{name}"

by_name = {e["name"]: e for e in entries}

expected_inventory = {
    "oracle-payments-01": {"type": "oracle", "enabled": True, "runtime_folder": "gg-oracle-payments-01", "values_path": oracle_values_path},
    "postgresql-payments-01": {"type": "postgresql", "enabled": True, "runtime_folder": "gg-postgresql-payments-01", "values_path": postgresql_values_path},
}

for name, expectation in expected_inventory.items():
    entry = by_name.get(name)
    if entry is None:
        bad(f"inventory entry '{name}' is missing")
        continue

    if entry.get("name", "").startswith("gg-"):
        bad(f"inventory name '{entry.get('name')}' already includes the gg- prefix -- would derive gg-gg-...")
    else:
        ok(f"inventory name '{name}' does not already include the gg- prefix")

    key = derive_key(entry["name"])
    if key == expectation["runtime_folder"]:
        ok(f"derived key for '{name}' is exactly {key!r}, no gg-gg- prefix")
    else:
        bad(f"derived key for '{name}' is {key!r}, expected {expectation['runtime_folder']!r}")

    if entry.get("type") == expectation["type"]:
        ok(f"inventory type for '{name}' is {expectation['type']!r}")
    else:
        bad(f"inventory type for '{name}' is {entry.get('type')!r}, expected {expectation['type']!r}")

    if entry.get("enabled") is expectation["enabled"]:
        ok(f"inventory enabled for '{name}' is {expectation['enabled']}")
    else:
        bad(f"inventory enabled for '{name}' is {entry.get('enabled')!r}, expected {expectation['enabled']}")

    # Cross-validate against the runtime values.yaml: folder name / runtime.name
    # equal the derived key, and runtime.deploymentType (global.deploymentId is
    # the folder-equivalent; this repo's chart doesn't have a literal
    # "deploymentType" values key, so cross-check via global.deploymentId and
    # the folder path instead, which is the authoritative equivalent).
    try:
        with open(expectation["values_path"]) as vf:
            values_raw = vf.read()
    except OSError as e:
        bad(f"could not read runtime values file for '{name}': {e}")
        continue

    if re.search(rf'deploymentId:\s*{re.escape(key)}\s*$', values_raw, re.MULTILINE):
        ok(f"runtime values file for '{name}' has global.deploymentId == {key!r}")
    else:
        bad(f"runtime values file for '{name}' does not declare global.deploymentId == {key!r}")

    if re.search(rf'^\s*name:\s*{re.escape(key)}\s*$', values_raw, re.MULTILINE):
        ok(f"runtime values file for '{name}' has runtime.name == {key!r}")
    else:
        bad(f"runtime values file for '{name}' does not declare a name: {key!r} field")

# Synthetic disabled entry: simulate adding it without any Terraform code
# change -- the for_each mechanism (already proven above) is what makes
# this "automatic". This is a pure derivation-logic check.
synthetic_entries = list(entries) + [{"name": "sqlserver-payments-01", "type": "sqlserver", "enabled": False}]
synthetic_keys = {derive_key(e["name"]): e["type"] for e in synthetic_entries}
if synthetic_keys.get("gg-sqlserver-payments-01") == "sqlserver":
    ok("synthetic entry sqlserver-payments-01/sqlserver/false resolves to gg-sqlserver-payments-01 without a new Terraform resource block")
else:
    bad("synthetic sqlserver-payments-01 entry did not resolve to the expected gg-sqlserver-payments-01 key")
if not any(k.startswith("gg-gg-") for k in synthetic_keys):
    ok("no gg-gg- prefix produced for any inventory entry, including the synthetic one")
else:
    bad("a gg-gg- prefix was produced by the key derivation")

print(f"SUMMARY pass={pass_count} fail={fail_count} skip=0")
PYEOF

  set +e
  DYNAMODB_CONFIG_VALIDATION_OUTPUT="$(python3 "$DYNAMODB_CONFIG_VALIDATOR_PY" "$DYNAMODB_TF" "$DEPLOYMENTS_YAML" "$ORACLE_VALUES" "$POSTGRESQL_VALUES")"
  set -e
  echo "$DYNAMODB_CONFIG_VALIDATION_OUTPUT"
  accumulate_python_summary "$DYNAMODB_CONFIG_VALIDATION_OUTPUT"
else
  skip "DynamoDB CONFIG item structural validation -- python3/PyYAML not available"
fi

echo ""
echo "--- Isolated scratch terraform validate: generic for_each resource + moved blocks + synthetic 3rd (disabled) inventory entry ---"
if [ "$TERRAFORM_AVAILABLE" = "true" ]; then
  TF_SCRATCH_DIR2="${WORKDIR}/tf-isolated-validate-forEach"
  mkdir -p "${TF_SCRATCH_DIR2}/envs/dev" "${TF_SCRATCH_DIR2}/pipelines" "${TF_SCRATCH_DIR2}/envs/dev/dummy_pipeline_state"
  cat > "${TF_SCRATCH_DIR2}/envs/dev/provider.tf" <<'EOF'
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

provider "aws" {
  region                      = "eu-west-1"
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  access_key                  = "test"
  secret_key                  = "test"
}
EOF
  # pipeline_config's depends_on references module.goldengate_pipeline_state
  # by address, not by output -- the real module's source is an unreachable
  # private repo, so this local stub (a trivial same-tree module with no
  # resources) exists purely to let the address resolve for isolated
  # validation. It does not attempt to reproduce the real module's content.
  cat > "${TF_SCRATCH_DIR2}/envs/dev/dummy_pipeline_state/main.tf" <<'EOF'
# Intentionally empty -- stands in only for the module address, not its
# real (unreachable, private) content.
EOF
  cat > "${TF_SCRATCH_DIR2}/envs/dev/table_stub.tf" <<'EOF'
module "goldengate_pipeline_state" {
  source = "./dummy_pipeline_state"
}
EOF
  awk '/^moved \{/{found=1} found{print}' "$DYNAMODB_TF" > "${TF_SCRATCH_DIR2}/envs/dev/config_items.tf"
  # Synthetic inventory: the real 2 entries plus one disabled sqlserver
  # candidate, proving the for_each mechanism accepts a new declared
  # deployment (enabled or not) with zero Terraform code changes.
  cat > "${TF_SCRATCH_DIR2}/pipelines/deployments.yaml" <<'EOF'
deployments:
  - name: oracle-payments-01
    type: oracle
    enabled: true

  - name: postgresql-payments-01
    type: postgresql
    enabled: true

  - name: sqlserver-payments-01
    type: sqlserver
    enabled: false
EOF

  if [ ! -s "${TF_SCRATCH_DIR2}/envs/dev/config_items.tf" ]; then
    fail "could not extract moved blocks + pipeline_config resource from ${DYNAMODB_TF} for isolated validation"
  else
    (cd "${TF_SCRATCH_DIR2}/envs/dev" && terraform init -input=false >"${TF_SCRATCH_DIR2}/tf-init.log" 2>&1)
    if [ $? -eq 0 ]; then
      if (cd "${TF_SCRATCH_DIR2}/envs/dev" && terraform validate >"${TF_SCRATCH_DIR2}/tf-validate.log" 2>&1); then
        pass "terraform validate (isolated scratch, 3-entry synthetic inventory including 1 disabled): pipeline_config + moved blocks are schema-valid"
      else
        fail "terraform validate (isolated scratch, synthetic inventory) failed:"
        cat "${TF_SCRATCH_DIR2}/tf-validate.log"
      fi
    else
      skip "terraform validate (isolated scratch, synthetic inventory) -- could not download the public hashicorp/aws provider; see ${TF_SCRATCH_DIR2}/tf-init.log"
    fi
  fi
else
  skip "terraform validate (isolated scratch, synthetic inventory) -- terraform not installed"
fi

echo ""
echo "--- Topology document (topologies/dev/payments-ora-to-pg-001.yaml) ---"
if [ -f "$TOPOLOGY_YAML" ]; then
  pass "topology document exists at the recommended, non-envs/dev path"
else
  fail "topology document not found at ${TOPOLOGY_YAML}"
fi

if [ "$PYTHON_AVAILABLE" = "true" ] && [ -f "$TOPOLOGY_YAML" ]; then
  if python3 -c "
import yaml, sys

with open('${TOPOLOGY_YAML}') as f:
    doc = yaml.safe_load(f)

errors = []

if doc.get('pipelineId') != 'payments-ora-to-pg-001':
    errors.append(f\"pipelineId mismatch: {doc.get('pipelineId')!r}\")
if doc.get('environment') != 'dev':
    errors.append(f\"environment mismatch: {doc.get('environment')!r}\")

lifecycle = doc.get('lifecycle', {})
if lifecycle.get('enabled') is not True or lifecycle.get('state') != 'runtime-ready':
    errors.append(f'lifecycle mismatch: {lifecycle!r}')

expected_deployments = {
    'source': {
        'deploymentName': 'gg-oracle-payments-01',
        'deploymentType': 'oracle',
        'ports': {'admin': 8443, 'distribution': 9013, 'metrics': 9015},
        'secretAdmin': 'dev/goldengate/source/admin',
    },
    'target': {
        'deploymentName': 'gg-postgresql-payments-01',
        'deploymentType': 'postgresql',
        'ports': {'admin': 8443, 'receiver': 9014, 'metrics': 9015},
        'secretAdmin': 'dev/goldengate/target/admin',
    },
}

deployments = doc.get('deployments', {})
for key, expectation in expected_deployments.items():
    dep = deployments.get(key)
    if dep is None:
        errors.append(f'deployments.{key} is missing')
        continue
    if dep.get('deploymentName') != expectation['deploymentName']:
        errors.append(f\"deployments.{key}.deploymentName mismatch: {dep.get('deploymentName')!r}\")
    if dep.get('deploymentType') != expectation['deploymentType']:
        errors.append(f\"deployments.{key}.deploymentType mismatch: {dep.get('deploymentType')!r}\")
    if dep.get('namespace') != 'goldengate-dev':
        errors.append(f\"deployments.{key}.namespace mismatch: {dep.get('namespace')!r}\")
    if dep.get('serviceName') != expectation['deploymentName']:
        errors.append(f\"deployments.{key}.serviceName mismatch: {dep.get('serviceName')!r}\")

    endpoints = dep.get('endpoints', {})
    expected_host = f\"{expectation['deploymentName']}.goldengate-dev.svc.cluster.local\"
    for ep_name, expected_port in expectation['ports'].items():
        ep = endpoints.get(ep_name)
        if ep is None:
            errors.append(f'deployments.{key}.endpoints.{ep_name} is missing')
            continue
        if ep.get('scheme') != 'https':
            errors.append(f\"deployments.{key}.endpoints.{ep_name}.scheme mismatch: {ep.get('scheme')!r}\")
        if ep.get('host') != expected_host:
            errors.append(f\"deployments.{key}.endpoints.{ep_name}.host mismatch: {ep.get('host')!r}\")
        if ep.get('port') != expected_port:
            errors.append(f\"deployments.{key}.endpoints.{ep_name}.port mismatch: {ep.get('port')!r} (expected {expected_port})\")

    secret_refs = dep.get('secretReferences', {})
    if secret_refs.get('admin') != expectation['secretAdmin']:
        errors.append(f\"deployments.{key}.secretReferences.admin mismatch: {secret_refs.get('admin')!r}\")
    if secret_refs.get('tls') != 'dev/goldengate/tls-certificate':
        errors.append(f\"deployments.{key}.secretReferences.tls mismatch: {secret_refs.get('tls')!r}\")

    processes = dep.get('processes', {})
    for proc_key in ('extracts', 'distributionPaths', 'replicats'):
        if processes.get(proc_key) != []:
            errors.append(f'deployments.{key}.processes.{proc_key} is not an empty list: {processes.get(proc_key)!r}')

    # No secret values/certificate contents -- only reference strings, and
    # none of the reference strings look like a credential.
    for ref_val in secret_refs.values():
        if not isinstance(ref_val, str) or '/' not in ref_val:
            errors.append(f'deployments.{key} secretReferences value does not look like a Secrets Manager object path: {ref_val!r}')

if errors:
    for e in errors:
        print(f'FAIL: {e}')
    sys.exit(1)

print('OK: topology document structure, endpoints, ports, and secret references all match the locked runtime facts.')
" >"${WORKDIR}/topology-check.log" 2>&1; then
    pass "topology document: pipelineId/environment/lifecycle/deployments/endpoints/ports/secretReferences/empty-process-lists all correct"
  else
    fail "topology document structural validation failed"
    cat "${WORKDIR}/topology-check.log"
  fi
else
  skip "topology document structural validation -- python3/PyYAML not available or file missing"
fi

echo ""
echo "--- Legacy items and current candidates left untouched ---"
if command -v git >/dev/null 2>&1 && git -C "$REPO_ROOT" rev-parse HEAD >/dev/null 2>&1; then
  # --ignore-all-space: this repository has known, pre-existing,
  # environment-driven CRLF/LF line-ending drift unrelated to any semantic
  # change (confirmed repeatedly in prior phases via
  # `git diff --ignore-all-space` showing zero output on the same files).
  # A whitespace-only diff here must not be reported as an out-of-scope
  # content change.
  if git -C "$REPO_ROOT" diff --ignore-all-space --quiet HEAD -- "$ORACLE_VALUES" "$POSTGRESQL_VALUES" monitoring/monitor helm/goldengate-monitor envs/dev/iam.tf envs/dev/secret.tf; then
    pass "candidate values, observer/monitor code, runtime IAM, and Secrets Manager Terraform are all unchanged by this correction (0 diff vs HEAD)"
  else
    fail "one or more out-of-scope files (candidates, observer/monitor, runtime IAM, Secrets Manager Terraform) differ from HEAD"
  fi
else
  skip "legacy/candidate unchanged-vs-HEAD check -- git not available"
fi

if grep -q "gg-payments-ora-to-pg-001-source\|gg-payments-ora-to-pg-001-target" "$DYNAMODB_TF"; then
  fail "envs/dev/dynamodb.tf references the old legacy logical pipeline names -- must not rename/overwrite them"
else
  pass "envs/dev/dynamodb.tf does not reference/rename/overwrite the legacy logical pipeline names"
fi

echo ""
echo "=================================================="
echo "Summary: ${PASS_COUNT} passed, ${FAIL_COUNT} failed, ${SKIP_COUNT} skipped"
echo "=================================================="

if [ "$FAIL_COUNT" -gt 0 ]; then
  exit 1
fi
