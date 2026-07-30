#!/usr/bin/env bash
set -euo pipefail

# Orchestration/regression script for the GoldenGate EKS repository.
#
# Runs static parsing/Helm/Python checks derived from the ONE canonical
# deployment source (envs/dev/goldengate-deployments.yaml) -- no runtime
# names are hardcoded here. Detailed behavior tests live in the Python
# unit-test suites (monitoring/monitor/tests/).
#
# Does not deploy, does not touch the cluster, does not require AWS
# credentials.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# This script's own python3 invocations must never create __pycache__/*.pyc
# -- that would make the "no committed pycache" check below self-defeating.
export PYTHONDONTWRITEBYTECODE=1

CANONICAL_CONFIG="envs/dev/goldengate-deployments.yaml"
RUNTIME_CHART="helm/goldengate"
PLATFORM_CHART="helm/goldengate-platform"
MONITOR_CHART="helm/goldengate-monitor"
MONITOR_APP_DIR="monitoring/monitor"
MONITOR_WORKFLOW=".github/workflows/goldengate-monitor.yaml"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0

pass() { echo "PASS: $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "FAIL: $1"; FAIL_COUNT=$((FAIL_COUNT + 1)); }
skip() { echo "SKIP: $1"; SKIP_COUNT=$((SKIP_COUNT + 1)); }

HELM_AVAILABLE="false"
command -v helm >/dev/null 2>&1 && HELM_AVAILABLE="true"

PYTHON_AVAILABLE="false"
if command -v python3 >/dev/null 2>&1 && python3 -c "import yaml" >/dev/null 2>&1; then
  PYTHON_AVAILABLE="true"
fi

echo "=================================================="
echo "GoldenGate repository regression"
echo "=================================================="
echo "Repository root: ${REPO_ROOT}"
echo "Helm available:  ${HELM_AVAILABLE}"
echo "Python3+PyYAML available: ${PYTHON_AVAILABLE}"
echo ""

# ---------------------------------------------------------------------
# 1. Strict YAML parsing + duplicate-key rejection for the canonical config.
# ---------------------------------------------------------------------
echo "--- Canonical configuration: parsing and structure ---"
if [ "$PYTHON_AVAILABLE" = "true" ]; then
  CANONICAL_CHECK_LOG="${WORKDIR}/canonical-check.log"
  if python3 - "$CANONICAL_CONFIG" >"$CANONICAL_CHECK_LOG" 2>&1 <<'PYEOF'
import sys
import yaml

path = sys.argv[1]

class DupCheckLoader(yaml.SafeLoader):
    pass

def no_dup_construct_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping

DupCheckLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, no_dup_construct_mapping)

with open(path) as f:
    doc = yaml.load(f, Loader=DupCheckLoader)

for key in ("environment", "runtimeNamespace", "monitoringNamespace", "dnsDomain", "deployments"):
    assert key in doc, f"missing required key {key!r}"

deployments = doc["deployments"]
assert isinstance(deployments, list) and deployments, "'deployments' must be a non-empty list"

names = set()
enabled_count = 0
for d in deployments:
    for field in ("name", "type", "pipeline", "role"):
        assert d.get(field), f"deployment entry missing {field!r}: {d!r}"
    assert d["name"] not in names, f"duplicate deployment name {d['name']!r}"
    names.add(d["name"])
    assert d["role"] in ("source", "target"), f"invalid role {d['role']!r}"
    if d.get("enabled"):
        enabled_count += 1

print(f"OK: {len(deployments)} deployment(s), {enabled_count} enabled, names={sorted(names)}")
PYEOF
  then
    pass "$(cat "$CANONICAL_CHECK_LOG")"
  else
    fail "canonical config parsing/structure check failed"
    cat "$CANONICAL_CHECK_LOG"
  fi
else
  skip "canonical config parsing -- python3/PyYAML not available"
fi

# ---------------------------------------------------------------------
# 2. Both runtimes remain enabled (source of truth: canonical config AND
#    each runtime's own environment values file).
# ---------------------------------------------------------------------
echo ""
echo "--- Both runtimes remain enabled ---"
if [ "$PYTHON_AVAILABLE" = "true" ]; then
  ENABLED_NAMES="$(python3 -c "
import yaml
doc = yaml.safe_load(open('${CANONICAL_CONFIG}'))
for d in doc['deployments']:
    if d.get('enabled'):
        print(d['name'])
")"
  ENABLED_COUNT="$(echo "$ENABLED_NAMES" | grep -c . || true)"
  if [ "$ENABLED_COUNT" -lt 2 ]; then
    fail "expected at least 2 enabled deployments in ${CANONICAL_CONFIG}, found ${ENABLED_COUNT}"
  else
    pass "${ENABLED_COUNT} deployment(s) enabled in canonical config: $(echo "$ENABLED_NAMES" | tr '\n' ' ')"
  fi
  while IFS= read -r name; do
    [ -z "$name" ] && continue
    VALUES_FILE="envs/dev/${name}/values.yaml"
    if [ ! -f "$VALUES_FILE" ]; then
      fail "no environment values file found for enabled deployment ${name} (expected ${VALUES_FILE})"
      continue
    fi
    if grep -qE '^\s*enabled:\s*true' "$VALUES_FILE" && grep -qE '^\s*enabled:\s*true\s*$' <(sed -n '/^deployment:/,/^[a-zA-Z]/p' "$VALUES_FILE"); then
      pass "${name}: environment values file has deployment.enabled=true"
    else
      fail "${name}: environment values file does not have deployment.enabled=true"
    fi
  done <<< "$ENABLED_NAMES"
else
  skip "runtime-enabled check -- python3/PyYAML not available"
fi

# ---------------------------------------------------------------------
# 3. Python unit tests (monitoring/monitor).
# ---------------------------------------------------------------------
echo ""
echo "--- Python unit tests ---"
if command -v python3 >/dev/null 2>&1 && python3 -c "import boto3, moto, yaml" >/dev/null 2>&1; then
  set +e
  MONITOR_TEST_OUTPUT="$(cd "$MONITOR_APP_DIR" && python3 -m unittest discover -s tests -p "test_*.py" 2>&1)"
  MONITOR_TEST_STATUS=$?
  set -e
  if [ "$MONITOR_TEST_STATUS" -eq 0 ]; then
    RAN_LINE="$(echo "$MONITOR_TEST_OUTPUT" | grep -E '^Ran [0-9]+ test' | tail -1)"
    pass "monitoring/monitor unit tests: ${RAN_LINE:-all tests passed}"
  else
    fail "monitoring/monitor unit tests failed"
    echo "$MONITOR_TEST_OUTPUT"
  fi
else
  skip "monitoring/monitor unit tests -- python3/boto3/moto/PyYAML not available"
fi

if command -v python3 >/dev/null 2>&1; then
  if python3 -m py_compile "${MONITOR_APP_DIR}"/*.py; then
    pass "monitoring/monitor Python modules compile cleanly"
  else
    fail "monitoring/monitor Python modules failed to compile"
  fi
  # py_compile writes __pycache__/*.pyc regardless of
  # PYTHONDONTWRITEBYTECODE (it is an explicit compile request) -- clean up
  # immediately so this script's own run never leaves artifacts behind.
  find "$MONITOR_APP_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
else
  skip "py_compile -- python3 not available"
fi

# ---------------------------------------------------------------------
# 4. Helm lint: runtime chart, platform chart, monitor chart.
# ---------------------------------------------------------------------
echo ""
echo "--- Helm lint ---"
if [ "$HELM_AVAILABLE" = "true" ]; then
  if helm lint "$RUNTIME_CHART" >"${WORKDIR}/lint-runtime.log" 2>&1; then
    pass "helm lint ${RUNTIME_CHART}"
  else
    fail "helm lint ${RUNTIME_CHART}"
    cat "${WORKDIR}/lint-runtime.log"
  fi

  if helm lint "$PLATFORM_CHART" >"${WORKDIR}/lint-platform.log" 2>&1; then
    pass "helm lint ${PLATFORM_CHART}"
  else
    fail "helm lint ${PLATFORM_CHART}"
    cat "${WORKDIR}/lint-platform.log"
  fi

  # The monitor chart's ConfigMap reads a staged copy of the canonical
  # config from its own files/ directory -- never committed there (see
  # goldengate-monitor.yaml) -- so lint/render stage a throwaway copy here.
  MONITOR_CHART_STAGED="${WORKDIR}/goldengate-monitor"
  cp -a "$MONITOR_CHART" "$MONITOR_CHART_STAGED"
  mkdir -p "${MONITOR_CHART_STAGED}/files"
  cp "$CANONICAL_CONFIG" "${MONITOR_CHART_STAGED}/files/goldengate-deployments.yaml"

  if helm lint "$MONITOR_CHART_STAGED" >"${WORKDIR}/lint-monitor.log" 2>&1; then
    pass "helm lint ${MONITOR_CHART} (canonical config staged)"
  else
    fail "helm lint ${MONITOR_CHART}"
    cat "${WORKDIR}/lint-monitor.log"
  fi
else
  skip "helm lint -- helm not available"
fi

# ---------------------------------------------------------------------
# 5. Render every enabled deployment discovered from the canonical config;
#    validate exactly one StatefulSet per release and no runtime sidecar.
# ---------------------------------------------------------------------
echo ""
echo "--- Render enabled runtimes; one StatefulSet each, no sidecar ---"
if [ "$HELM_AVAILABLE" = "true" ] && [ "$PYTHON_AVAILABLE" = "true" ]; then
  while IFS= read -r name; do
    [ -z "$name" ] && continue
    VALUES_FILE="envs/dev/${name}/values.yaml"
    RENDERED="${WORKDIR}/${name}.yaml"
    if ! helm template "$name" "$RUNTIME_CHART" --namespace goldengate-dev \
        -f "$VALUES_FILE" > "$RENDERED" 2>"${WORKDIR}/${name}.err"; then
      fail "helm template failed for ${name}"
      cat "${WORKDIR}/${name}.err"
      continue
    fi

    STATEFULSET_COUNT="$(grep -c '^kind: StatefulSet$' "$RENDERED" || true)"
    if [ "$STATEFULSET_COUNT" -eq 1 ]; then
      pass "${name}: exactly 1 StatefulSet rendered"
    else
      fail "${name}: expected exactly 1 StatefulSet, found ${STATEFULSET_COUNT}"
    fi

    if grep -q "^kind: StatefulSet$" "$RENDERED" && \
       python3 -c "
import sys, yaml
docs = list(yaml.safe_load_all(open('$RENDERED')))
sts = [d for d in docs if d and d.get('kind') == 'StatefulSet']
assert sts, 'no StatefulSet document'
containers = sts[0]['spec']['template']['spec'].get('containers', [])
init_containers = sts[0]['spec']['template']['spec'].get('initContainers', [])
names = [c['name'] for c in containers] + [c['name'] for c in init_containers]
forbidden = [n for n in names if 'observer' in n.lower() or 'sidecar' in n.lower()]
assert not forbidden, f'forbidden sidecar container(s): {forbidden}'
assert len(containers) == 1, f'expected exactly 1 non-init container, found {[c[\"name\"] for c in containers]}'
print('OK')
" >"${WORKDIR}/${name}-sidecar.log" 2>&1; then
      pass "${name}: no runtime sidecar container (observer/utility-sidecar)"
    else
      fail "${name}: sidecar-absence check failed"
      cat "${WORKDIR}/${name}-sidecar.log"
    fi
  done <<< "$(python3 -c "
import yaml
doc = yaml.safe_load(open('${CANONICAL_CONFIG}'))
for d in doc['deployments']:
    if d.get('enabled'):
        print(d['name'])
")"
else
  skip "runtime rendering -- helm and/or python3/PyYAML not available"
fi

# ---------------------------------------------------------------------
# 6. Existing shared monitor resources; no duplicate monitor deployment.
# ---------------------------------------------------------------------
echo ""
echo "--- Shared monitor: single deployment, existing resources retained ---"
if [ -d "monitoring/gg-monitor-core" ] || [ -d "helm/gg-monitor" ] || [ -d "platform/dev/gg-monitor" ] \
    || [ -f ".github/workflows/gg-monitor-core.yaml" ]; then
  fail "a second collector application/chart/workflow still exists (monitoring/gg-monitor-core, helm/gg-monitor, platform/dev/gg-monitor, or gg-monitor-core.yaml)"
else
  pass "no second collector application/chart/workflow exists -- one shared monitor only"
fi

if [ -f "${MONITOR_APP_DIR}/monitor.py" ] && [ -f "${MONITOR_APP_DIR}/collector.py" ] \
    && [ -f "${MONITOR_APP_DIR}/config.py" ] && [ -f "${MONITOR_APP_DIR}/health_rules.py" ]; then
  pass "monitoring/monitor has the merged module structure (monitor/collector/config/health_rules)"
else
  fail "monitoring/monitor is missing one or more expected modules"
fi

if [ "$HELM_AVAILABLE" = "true" ] && [ "$PYTHON_AVAILABLE" = "true" ]; then
  MONITOR_RENDERED="${WORKDIR}/goldengate-monitor-rendered.yaml"
  if helm template gg-monitor "$MONITOR_CHART_STAGED" --namespace goldengate-monitoring \
      -f envs/dev/goldengate-monitor/values.yaml \
      --set image.repository=example.invalid/goldengate-monitor --set image.tag=test \
      > "$MONITOR_RENDERED" 2>"${WORKDIR}/monitor-render.err"; then
    for kind_name in "Deployment gg-monitor" "Service gg-monitor" "ServiceAccount gg-monitor" "ConfigMap goldengate-monitor-canonical-config"; do
      kind="${kind_name% *}"
      name="${kind_name#* }"
      if grep -q "^kind: ${kind}$" "$MONITOR_RENDERED"; then
        pass "goldengate-monitor renders ${kind}/${name}"
      else
        fail "goldengate-monitor is missing ${kind}/${name}"
      fi
    done
    if grep -q "kind: Ingress" "$MONITOR_RENDERED" 2>/dev/null || \
       helm template gg-monitor "$MONITOR_CHART_STAGED" --namespace goldengate-monitoring \
         -f envs/dev/goldengate-monitor/values.yaml \
         --set image.repository=example.invalid/goldengate-monitor --set image.tag=test \
         --set ingress.enabled=true --set ingress.host=monitor.goldengate-dev.adcbmis.local \
         --set ingress.alb.certificateArn=arn:aws:acm:eu-west-1:668311715351:certificate/test \
         2>/dev/null | grep -q "host: monitor.goldengate-dev.adcbmis.local"; then
      pass "goldengate-monitor Ingress renders with the existing hostname when enabled"
    else
      fail "goldengate-monitor Ingress does not render the existing hostname"
    fi
  else
    fail "helm template failed for goldengate-monitor"
    cat "${WORKDIR}/monitor-render.err"
  fi
else
  skip "goldengate-monitor render checks -- helm and/or python3/PyYAML not available"
fi

# ---------------------------------------------------------------------
# 7. No hardcoded runtime/pipeline names in application or workflow code.
# ---------------------------------------------------------------------
echo ""
echo "--- No hardcoded canonical deployment names outside the canonical config ---"
if [ "$PYTHON_AVAILABLE" = "true" ]; then
  HARDCODE_NAMES="$(python3 -c "
import yaml
doc = yaml.safe_load(open('${CANONICAL_CONFIG}'))
names = set()
for d in doc['deployments']:
    names.add(d['name'])
    names.add(d['pipeline'])
print('\n'.join(sorted(names)))
")"
else
  HARDCODE_NAMES="$(grep -E '^\s*-?\s*(name|pipeline):' "$CANONICAL_CONFIG" | sed -E 's/^\s*-?\s*(name|pipeline):\s*//' | tr -d '"'"'"'\r' | sort -u)"
fi

HARDCODE_FOUND="false"
for f in "${MONITOR_APP_DIR}"/monitor.py "${MONITOR_APP_DIR}"/collector.py "${MONITOR_APP_DIR}"/config.py "${MONITOR_APP_DIR}"/health_rules.py; do
  [ -f "$f" ] || continue
  while IFS= read -r nm; do
    [ -z "$nm" ] && continue
    if grep -Fq -- "$nm" "$f"; then
      fail "$(basename "$f") hardcodes canonical name: ${nm}"
      HARDCODE_FOUND="true"
    fi
  done <<< "$HARDCODE_NAMES"
done
if [ "$HARDCODE_FOUND" = "false" ]; then
  pass "no application module hardcodes a canonical deployment/pipeline name"
fi

if grep -qE "pipelines/deployments\.yaml|topologies/dev|files/pipelines|files/topologies" "$MONITOR_WORKFLOW" 2>/dev/null; then
  fail "goldengate-monitor.yaml still references the removed pipelines/topologies file layout"
else
  pass "goldengate-monitor.yaml does not reference the removed pipelines/topologies file layout"
fi

# ---------------------------------------------------------------------
# 8. No committed generated copies of the canonical config inside charts.
# ---------------------------------------------------------------------
echo ""
echo "--- No generated canonical-config copies committed in charts ---"
if [ -e "helm/goldengate-monitor/files/goldengate-deployments.yaml" ] \
    || [ -d "helm/goldengate-monitor/files/pipelines" ] \
    || [ -d "helm/goldengate-monitor/files/topologies" ]; then
  fail "helm/goldengate-monitor/files/ contains a committed generated copy of the canonical config"
else
  pass "helm/goldengate-monitor/files/ contains no committed generated copy"
fi

# ---------------------------------------------------------------------
# 9. No committed pycache/pyc.
# ---------------------------------------------------------------------
echo ""
echo "--- No committed __pycache__/*.pyc ---"
STRAY_PYCACHE="$(find . -type d -name "__pycache__" -not -path "*/node_modules/*" 2>/dev/null)"
STRAY_PYC="$(find . -type f -name "*.pyc" -not -path "*/node_modules/*" 2>/dev/null)"
if [ -z "$STRAY_PYCACHE" ] && [ -z "$STRAY_PYC" ]; then
  pass "no __pycache__ directories or *.pyc files present"
else
  fail "found stray __pycache__/*.pyc: ${STRAY_PYCACHE} ${STRAY_PYC}"
fi

# ---------------------------------------------------------------------
# 10. Phase 4B1: contract-probe tool packaged but never auto-run; CloudWatch
#     stays physically disabled by default.
# ---------------------------------------------------------------------
echo ""
echo "--- Contract-probe tool: packaged, never auto-run, CloudWatch stays disabled ---"
PROBE_TOOL="${MONITOR_APP_DIR}/tools/gg_api_contract_probe.py"
if [ -f "$PROBE_TOOL" ]; then
  pass "gg_api_contract_probe.py exists under monitoring/monitor/tools/"
else
  fail "gg_api_contract_probe.py is missing"
fi

if grep -q "COPY tools/ ./tools/" "${MONITOR_APP_DIR}/Dockerfile" 2>/dev/null; then
  pass "Dockerfile packages tools/ into the monitor image"
else
  fail "Dockerfile does not copy tools/ into the monitor image"
fi

if grep -q "^ENTRYPOINT \[\"python3\", \"monitor.py\"\]$" "${MONITOR_APP_DIR}/Dockerfile" 2>/dev/null; then
  pass "Dockerfile entrypoint is unchanged (monitor.py only)"
else
  fail "Dockerfile entrypoint was changed"
fi

PROBE_WIRED="false"
for f in "${MONITOR_APP_DIR}/monitor.py" "${MONITOR_APP_DIR}/collector.py"; do
  [ -f "$f" ] || continue
  if grep -q "gg_api_contract_probe" "$f"; then
    fail "$(basename "$f") references gg_api_contract_probe -- must never auto-run"
    PROBE_WIRED="true"
  fi
done
if [ "$PROBE_WIRED" = "false" ]; then
  pass "gg_api_contract_probe is never imported by monitor.py/collector.py (manual-only)"
fi

if grep -q "publishEnabled: false" "${MONITOR_CHART}/values.yaml" 2>/dev/null; then
  pass "helm/goldengate-monitor default values.yaml keeps cloudwatch.publishEnabled: false"
else
  fail "helm/goldengate-monitor default values.yaml no longer defaults CloudWatch publishing to false"
fi

# ---------------------------------------------------------------------
# 11. Phase 4B2A: confirmed secure PMS route documented; 9015 stays
#     unauthenticated-only; /services/v2/metrics not recommended as
#     production PMS.
# ---------------------------------------------------------------------
echo ""
echo "--- Contract-probe tool: confirmed secure PMS route frozen ---"
if grep -q "/services/v2/mpoints/processes" "$PROBE_TOOL" 2>/dev/null \
    && grep -q "/services/v2/monitoring/statusChanges" "$PROBE_TOOL" 2>/dev/null; then
  pass "gg_api_contract_probe.py documents the confirmed secure PMS routes"
else
  fail "gg_api_contract_probe.py does not document the confirmed secure PMS routes"
fi

if grep -qi "confirmed invalid" "$PROBE_TOOL" 2>/dev/null; then
  pass "gg_api_contract_probe.py marks /services/v2/metrics as confirmed invalid, not production PMS"
else
  fail "gg_api_contract_probe.py no longer marks /services/v2/metrics as confirmed invalid"
fi

if grep -q 'return f"http://{host}:{deployment\[.metricsPort.\]}"' "$PROBE_TOOL" 2>/dev/null; then
  pass "metricsPort 9015 stays plain HTTP (never HTTPS) in the probe tool"
else
  fail "metricsPort scheme handling in the probe tool changed unexpectedly"
fi

if [ -d "monitoring/observer" ]; then
  pass "legacy observer (monitoring/observer) remains present"
else
  fail "monitoring/observer is missing -- legacy observer must remain operational"
fi

echo ""
echo "=================================================="
echo "Summary: ${PASS_COUNT} passed, ${FAIL_COUNT} failed, ${SKIP_COUNT} skipped"
echo "=================================================="

if [ "$FAIL_COUNT" -gt 0 ]; then
  exit 1
fi
