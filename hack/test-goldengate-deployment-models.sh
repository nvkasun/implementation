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

echo "=================================================="
echo "GoldenGate Phase 1 deployment-model validation"
echo "=================================================="
echo "Repository root: ${REPO_ROOT}"
echo "Helm available:  ${HELM_AVAILABLE}"
echo "Python3+PyYAML available: ${PYTHON_AVAILABLE}"
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

  if [ -f "$BASELINE_FILE" ] && [ -s "$LEGACY_RENDERED" ]; then
    # Strip the one intentionally-volatile, harmless line (the Helm chart
    # version label) before comparing -- everything else (resources, names,
    # selectors, ports, volumes, probes, init logic, observer integration,
    # ingress behavior) must be byte-for-byte identical. Use the original
    # pre-Phase-1 revision/archive to produce ${BASELINE_FILE} -- see this
    # script's header comment for the exact capture command.
    sed -E '/helm\.sh\/chart:/d' "$BASELINE_FILE" > "${WORKDIR}/baseline-stripped.yaml"
    sed -E '/helm\.sh\/chart:/d' "$LEGACY_RENDERED" > "${WORKDIR}/after-stripped.yaml"

    if diff -u "${WORKDIR}/baseline-stripped.yaml" "${WORKDIR}/after-stripped.yaml" > "${WORKDIR}/legacy-diff.log"; then
      pass "legacy manifest is identical to the captured baseline (ignoring helm.sh/chart version label)"
    else
      fail "legacy manifest differs from the captured baseline -- see diff below"
      cat "${WORKDIR}/legacy-diff.log"
    fi
  else
    skip "legacy baseline comparison (baseline file missing or render failed)"
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
echo "=================================================="
echo "Summary: ${PASS_COUNT} passed, ${FAIL_COUNT} failed, ${SKIP_COUNT} skipped"
echo "=================================================="

if [ "$FAIL_COUNT" -gt 0 ]; then
  exit 1
fi
