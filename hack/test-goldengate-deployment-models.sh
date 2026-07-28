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
# Does not deploy, does not touch the cluster, does not require AWS
# credentials, does not install anything.
#
# Usage:
#   hack/test-goldengate-deployment-models.sh
#
# To also compare against a pre-change baseline (recommended before/after
# any helm/goldengate/** change), first capture one:
#   helm template ogg-payments-ora-to-pg-001 helm/goldengate \
#     --namespace gg-dev-payments-ora-to-pg-001 \
#     -f envs/dev/payments-ora-to-pg-001/values.yaml \
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

echo "=================================================="
echo "GoldenGate Phase 1 deployment-model validation"
echo "=================================================="
echo "Repository root: ${REPO_ROOT}"
echo "Helm available:  ${HELM_AVAILABLE}"
echo ""

if [ ! -f "$BASELINE_FILE" ]; then
  echo "NOTE: no baseline manifest found at ${BASELINE_FILE}."
  echo "Capture one BEFORE making chart changes with the command in this script's header comment."
  echo ""
fi

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
    # ingress behavior) must be byte-for-byte identical.
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
else
  skip "helm lint (legacy) -- helm not installed"
  skip "helm template (legacy) -- helm not installed"
  skip "legacy baseline comparison -- helm not installed"
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
# 8: resource contract assertions
# ---------------------------------------------------------------------
assert_contains() {
  local file="$1" pattern="$2" description="$3"
  if [ ! -s "$file" ]; then
    skip "$description -- rendered manifest not available"
    return
  fi
  if grep -qF -- "$pattern" "$file"; then
    pass "$description"
  else
    fail "$description (pattern not found: ${pattern})"
  fi
}

assert_count() {
  local file="$1" pattern="$2" expected="$3" description="$4"
  if [ ! -s "$file" ]; then
    skip "$description -- rendered manifest not available"
    return
  fi
  local actual
  actual="$(grep -cE -- "$pattern" "$file" || true)"
  if [ "$actual" -eq "$expected" ]; then
    pass "$description (found ${actual})"
  else
    fail "$description (expected ${expected}, found ${actual})"
  fi
}

assert_absent() {
  local file="$1" pattern="$2" description="$3"
  if [ ! -s "$file" ]; then
    skip "$description -- rendered manifest not available"
    return
  fi
  if grep -qF -- "$pattern" "$file"; then
    fail "$description (unexpectedly found: ${pattern})"
  else
    pass "$description"
  fi
}

echo "--- Oracle candidate resource contract ---"
assert_count    "$ORACLE_RENDERED" '^kind: StatefulSet$'          1 "exactly one StatefulSet"
assert_contains "$ORACLE_RENDERED" "- name: ogg-oracle"             "expected main container name (ogg-oracle)"
assert_absent   "$ORACLE_RENDERED" "goldengate-observer"            "no observer container"
assert_absent   "$ORACLE_RENDERED" "utility-sidecar"                "no manager utility-sidecar"
assert_absent   "$ORACLE_RENDERED" "fluent-bit"                     "no Fluent Bit sidecar"
assert_contains "$ORACLE_RENDERED" "name: gg-oracle-payments-01"    "runtime name gg-oracle-payments-01"
assert_contains "$ORACLE_RENDERED" 'namespace: "goldengate-dev"'    "namespace goldengate-dev"
assert_contains "$ORACLE_RENDERED" "serviceAccountName: gg-oracle-sa" "ServiceAccount gg-oracle-sa"
assert_contains "$ORACLE_RENDERED" "containerPort: 9013"            "dist port 9013 present"
assert_absent   "$ORACLE_RENDERED" "name: receiver"                 "receiver port absent"
assert_contains "$ORACLE_RENDERED" "containerPort: 9015"            "metrics port 9015 present"
assert_count    "$ORACLE_RENDERED" '^\s*- host:'                  1 "one expected hostname"
assert_absent   "$ORACLE_RENDERED" "kind: Namespace"                "no Namespace document"
assert_contains "$ORACLE_RENDERED" "ServiceManager.pid"             "stale PID cleanup present"
assert_contains "$ORACLE_RENDERED" "gg-efs-dev-gg-oracle-payments-01" "unique StorageClass name"
assert_contains "$ORACLE_RENDERED" "gg-oracle-payments-01-u02"      "unique PVC name"

echo ""
echo "--- PostgreSQL candidate resource contract ---"
assert_count    "$POSTGRESQL_RENDERED" '^kind: StatefulSet$'        1 "exactly one StatefulSet"
assert_contains "$POSTGRESQL_RENDERED" "- name: ogg-postgresql"       "expected main container name (ogg-postgresql)"
assert_absent   "$POSTGRESQL_RENDERED" "goldengate-observer"          "no observer container"
assert_absent   "$POSTGRESQL_RENDERED" "utility-sidecar"              "no manager utility-sidecar"
assert_absent   "$POSTGRESQL_RENDERED" "fluent-bit"                   "no Fluent Bit sidecar"
assert_contains "$POSTGRESQL_RENDERED" "name: gg-postgresql-payments-01" "runtime name gg-postgresql-payments-01"
assert_contains "$POSTGRESQL_RENDERED" 'namespace: "goldengate-dev"'  "namespace goldengate-dev"
assert_contains "$POSTGRESQL_RENDERED" "serviceAccountName: gg-postgresql-sa" "ServiceAccount gg-postgresql-sa"
assert_contains "$POSTGRESQL_RENDERED" "containerPort: 9014"          "receiver port 9014 present"
assert_absent   "$POSTGRESQL_RENDERED" "name: dist"                   "dist port absent"
assert_contains "$POSTGRESQL_RENDERED" "containerPort: 9015"          "metrics port 9015 present"
assert_count    "$POSTGRESQL_RENDERED" '^\s*- host:'                1 "one expected hostname"
assert_absent   "$POSTGRESQL_RENDERED" "kind: Namespace"              "no Namespace document"
assert_contains "$POSTGRESQL_RENDERED" "ServiceManager.pid"           "stale PID cleanup present"
assert_contains "$POSTGRESQL_RENDERED" "gg-efs-dev-gg-postgresql-payments-01" "unique StorageClass name"
assert_contains "$POSTGRESQL_RENDERED" "gg-postgresql-payments-01-u02" "unique PVC name"

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
else
  skip "static workflow assertions -- ${WORKFLOW_FILE} not found"
fi

echo ""
echo "=================================================="
echo "Summary: ${PASS_COUNT} passed, ${FAIL_COUNT} failed, ${SKIP_COUNT} skipped"
echo "=================================================="

if [ "$FAIL_COUNT" -gt 0 ]; then
  exit 1
fi
