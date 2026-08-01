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
EKS_APP_WORKFLOW=".github/workflows/goldengate-eks-app.yaml"

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
  fail "monitoring/observer still exists -- Phase 5A requires observer source retirement"
else
  pass "monitoring/observer has been removed (Phase 5A observer retirement)"
fi

# ---------------------------------------------------------------------
# 12. Phase 4B2B: --follow-processes fixed detail allowlist exists and is
#     never wired into automatic startup.
# ---------------------------------------------------------------------
echo ""
echo "--- Contract-probe tool: --follow-processes fixed detail allowlist ---"
if grep -q '"process", "processPerformance", "threadPerformance", "serviceHealth", "heartbeat"' "$PROBE_TOOL" 2>/dev/null; then
  pass "gg_api_contract_probe.py defines the fixed --detail allowlist"
else
  fail "gg_api_contract_probe.py fixed --detail allowlist is missing or changed"
fi

if grep -q "MAX_FOLLOWED_PROCESSES = 20" "$PROBE_TOOL" 2>/dev/null; then
  pass "gg_api_contract_probe.py caps --follow-processes at 20 items"
else
  fail "gg_api_contract_probe.py no longer caps --follow-processes at 20 items"
fi

FOLLOW_WIRED="false"
for f in "${MONITOR_APP_DIR}/monitor.py" "${MONITOR_APP_DIR}/collector.py"; do
  [ -f "$f" ] || continue
  if grep -q "follow_processes\|follow-processes" "$f"; then
    fail "$(basename "$f") references follow_processes -- must never auto-run"
    FOLLOW_WIRED="true"
  fi
done
if [ "$FOLLOW_WIRED" = "false" ]; then
  pass "--follow-processes is never referenced by monitor.py/collector.py (manual-only)"
fi

# ---------------------------------------------------------------------
# 13. Phase 4C1: production PMS collection bounded, no new DynamoDB
#     record type, forbidden endpoints never referenced by name.
# ---------------------------------------------------------------------
echo ""
echo "--- Production PMS collection: bounded, no forbidden endpoints ---"
if grep -q "MAX_FOLLOWED_PMS_PROCESSES = 20" "${MONITOR_APP_DIR}/collector.py" 2>/dev/null; then
  pass "collector.py caps production PMS collection at 20 followed processes"
else
  fail "collector.py no longer caps production PMS collection at 20 followed processes"
fi

if grep -q 'PMS_DETAIL_KINDS = ("processPerformance", "serviceHealth")' "${MONITOR_APP_DIR}/collector.py" 2>/dev/null; then
  pass "collector.py production PMS detail calls remain processPerformance + serviceHealth only"
else
  fail "collector.py production PMS detail-call set changed unexpectedly"
fi

PMS_FORBIDDEN_FOUND="false"
# Skip the module docstring (lines 1..first closing triple-quote), which
# legitimately documents these endpoints as NOT used -- only code after it
# must never reference them.
COLLECTOR_CODE_TAIL="$(awk '/^"""$/{n++; next} n>=1' "${MONITOR_APP_DIR}/collector.py" 2>/dev/null)"
for pattern in '"/heartbeat"' '"/threadPerformance"' "statusChanges" "9015"; do
  if grep -qF "$pattern" <<< "$COLLECTOR_CODE_TAIL"; then
    fail "collector.py references forbidden PMS pattern outside its docstring: ${pattern}"
    PMS_FORBIDDEN_FOUND="true"
  fi
done
if [ "$PMS_FORBIDDEN_FOUND" = "false" ]; then
  pass "collector.py never references /heartbeat, /threadPerformance, statusChanges, or port 9015 outside its docstring"
fi

if grep -q '"pms" in snapshot' "${MONITOR_APP_DIR}/collector.py" 2>/dev/null \
    && grep -q 'f"STATE#{process}"' "${MONITOR_APP_DIR}/collector.py" 2>/dev/null; then
  pass "PMS enrichment folds into the existing STATE# write -- no new recordType"
else
  fail "PMS enrichment / existing STATE# recordType pattern changed unexpectedly"
fi

# ---------------------------------------------------------------------
# 14. Phase 4C1 correction: process-name/numeric bounds and stale-PMS
#     overwrite semantics remain in place.
# ---------------------------------------------------------------------
echo ""
echo "--- Production PMS collection: bounds and stale-state overwrite ---"
if grep -q "MAX_PMS_PROCESS_NAME_LENGTH = 128" "${MONITOR_APP_DIR}/collector.py" 2>/dev/null; then
  pass "collector.py bounds PMS process-name length"
else
  fail "collector.py no longer bounds PMS process-name length"
fi

if grep -q "PMS_MAX_SAFE_NUMBER = 10 \*\* 15" "${MONITOR_APP_DIR}/collector.py" 2>/dev/null; then
  pass "collector.py bounds PMS numeric values to a fixed DynamoDB-safe range"
else
  fail "collector.py no longer bounds PMS numeric values to a fixed DynamoDB-safe range"
fi

if grep -q "_pms_unavailable_snapshot" "${MONITOR_APP_DIR}/collector.py" 2>/dev/null; then
  pass "collector.py overwrites pms with a current sanitized snapshot on DOWN/unexpected-failure ticks"
else
  fail "collector.py stale-PMS overwrite helper is missing"
fi

# ---------------------------------------------------------------------
# 15. Phase 4C1 pre-deployment correction: total PMS collection time
#     budget stays fixed and comfortably under the deployed stale
#     threshold; serviceHealth validation stays tightened.
# ---------------------------------------------------------------------
echo ""
echo "--- Production PMS collection: total time budget ---"
if grep -q "PMS_REQUEST_TIMEOUT_SECONDS = 2" "${MONITOR_APP_DIR}/collector.py" 2>/dev/null \
    && grep -q "PMS_COLLECTION_BUDGET_SECONDS = 30" "${MONITOR_APP_DIR}/collector.py" 2>/dev/null; then
  pass "collector.py bounds total PMS collection to a fixed 30s time budget"
else
  fail "collector.py PMS request/budget timeout constants changed unexpectedly"
fi

if grep -q 'isinstance(response.get("isHealthy"), bool)' "${MONITOR_APP_DIR}/collector.py" 2>/dev/null; then
  pass "collector.py requires serviceHealth isHealthy to be a literal boolean"
else
  fail "collector.py no longer requires serviceHealth isHealthy to be a literal boolean"
fi

# ---------------------------------------------------------------------
# 16. Phase 4C2: manager-compatible portal -- GET /api/processes exists,
#     canonical STATE#-only (no Scan, no legacy fallback in that path).
# ---------------------------------------------------------------------
echo ""
echo "--- Manager-compatible portal: /api/processes ---"
if grep -q '"/api/processes"' "${MONITOR_APP_DIR}/monitor.py" 2>/dev/null \
    && grep -q "def build_processes_payload" "${MONITOR_APP_DIR}/monitor.py" 2>/dev/null; then
  pass "monitor.py exposes GET /api/processes backed by build_processes_payload"
else
  fail "monitor.py is missing the /api/processes endpoint"
fi

if grep -q "def read_deployment_processes_view" "${MONITOR_APP_DIR}/monitor.py" 2>/dev/null \
    && ! grep -q "\.scan(" "${MONITOR_APP_DIR}/monitor.py" 2>/dev/null; then
  pass "monitor.py's /api/processes view is canonical STATE#-only and never calls Scan"
else
  fail "monitor.py /api/processes canonical-view helper or no-Scan guarantee changed unexpectedly"
fi

# ---------------------------------------------------------------------
# 17. Phase 4D1: CloudWatch metric-path source hardening -- exact manager-
#     compatible metric contract, sanitized PutMetricData failure logging,
#     hard switch still gates client construction, no alarm/SNS/gg-alerter/
#     Fluent Bit or read/alarm CloudWatch IAM permission introduced.
# ---------------------------------------------------------------------
echo ""
echo "--- Phase 4D1: CloudWatch metric-path source hardening ---"

COLLECTOR_SRC="${MONITOR_APP_DIR}/collector.py"

METRIC_CONTRACT_OK="true"
for token in 'CLOUDWATCH_NAMESPACE = "GoldenGate/Pipelines"' '"LagBreached"' '"AbendFailure"' \
             '"DeploymentDown"' '"HeartbeatAgeSeconds"' '"CriticalServiceDown"' \
             '"ExtractLagSeconds"' '"ReplicatLagSeconds"' '"AbendState"' '"AbendEvent"'; do
  if ! grep -qF "$token" "$COLLECTOR_SRC" 2>/dev/null; then
    fail "collector.py is missing expected metric-contract token: ${token}"
    METRIC_CONTRACT_OK="false"
  fi
done
[ "$METRIC_CONTRACT_OK" = "true" ] && pass "collector.py defines the exact manager-compatible namespace/metric-name contract"

if grep -q "def build_metric_batch" "$COLLECTOR_SRC" 2>/dev/null \
    && grep -q "def publish_metric_batch" "$COLLECTOR_SRC" 2>/dev/null; then
  pass "collector.py keeps build_metric_batch (pure) and publish_metric_batch (boto3-isolated) as separate functions"
else
  fail "collector.py is missing build_metric_batch/publish_metric_batch"
fi

if grep -q "logger.exception(\"CloudWatch put_metric_data failed" "$COLLECTOR_SRC" 2>/dev/null; then
  fail "publish_metric_batch still uses raw logger.exception for PutMetricData failures"
else
  pass "publish_metric_batch no longer logs a raw exception/traceback on PutMetricData failure"
fi

if grep -q '"event": "cloudwatch_put_metric_data_failed"' "$COLLECTOR_SRC" 2>/dev/null; then
  pass "publish_metric_batch logs a sanitized structured event on PutMetricData failure"
else
  fail "publish_metric_batch is missing the sanitized cloudwatch_put_metric_data_failed log event"
fi

if grep -q "cloudwatch:GetMetricData\|cloudwatch:DescribeAlarms\|cloudwatch:ListMetrics\|cloudwatch:GetMetricStatistics" \
    envs/dev/policies/goldengate-monitor-read-dev/policies/*.json 2>/dev/null; then
  fail "goldengate-monitor-read-dev policy introduces a CloudWatch read/alarm permission"
else
  pass "goldengate-monitor-read-dev IAM policy grants CloudWatch PutMetricData only (no read/alarm actions)"
fi

ALARM_SNS_FOUND="false"
if find . -path ./.git -prune -o \( -iname "*gg-alerter*" -o -iname "*fluent-bit*" \) -print 2>/dev/null \
    | grep -q .; then
  ALARM_SNS_FOUND="true"
fi
if [ "$ALARM_SNS_FOUND" = "false" ]; then
  pass "no gg-alerter or Fluent Bit implementation exists yet"
else
  fail "unexpected gg-alerter/Fluent Bit file found -- out of scope for this phase"
fi

if [ -f "hack/comma.yaml" ]; then
  fail "hack/comma.yaml (unreferenced pasted operator note) still present"
else
  pass "hack/comma.yaml removed"
fi

# ---------------------------------------------------------------------
# 18. Phase 4D1 final correction: strict identity-based two-factor gate,
#     and CloudWatch client construction moved behind a sanitized,
#     non-raising protected publication boundary.
# ---------------------------------------------------------------------
echo ""
echo "--- Phase 4D1 correction: strict gate and protected publication boundary ---"

if grep -q 'return str(raw).strip().lower() == "true"' "$COLLECTOR_SRC" 2>/dev/null; then
  pass "_parse_strict_bool_env accepts only a trimmed, case-insensitive \"true\""
else
  fail "_parse_strict_bool_env no longer uses exact-match \"true\" parsing"
fi

if grep -q 'CLOUDWATCH_PUBLISH_ENABLED is True and cfg.get("metricsEnabled") is True' "$COLLECTOR_SRC" 2>/dev/null; then
  pass "cloudwatch_enabled_for uses literal Boolean identity checks on both sides of the gate"
else
  fail "cloudwatch_enabled_for no longer uses strict identity checks"
fi

if grep -q "def publish_metrics_if_enabled" "$COLLECTOR_SRC" 2>/dev/null; then
  pass "collector.py defines the single protected publication boundary (publish_metrics_if_enabled)"
else
  fail "collector.py is missing publish_metrics_if_enabled"
fi

if grep -q '"event": "cloudwatch_client_creation_failed"' "$COLLECTOR_SRC" 2>/dev/null; then
  pass "CloudWatch client-construction failure is logged as a sanitized structured event"
else
  fail "collector.py is missing the sanitized cloudwatch_client_creation_failed log event"
fi

DIRECT_CLIENT_CALLS="$(grep -c '_cloudwatch_client()' "$COLLECTOR_SRC" 2>/dev/null || true)"
if [ "${DIRECT_CLIENT_CALLS:-0}" -eq 2 ]; then
  pass "_cloudwatch_client() is only referenced in its definition and inside publish_metrics_if_enabled (both polling_loop call sites go through the boundary)"
else
  fail "_cloudwatch_client() is referenced ${DIRECT_CLIENT_CALLS:-0} times -- expected exactly 2 (definition + protected boundary)"
fi

# ---------------------------------------------------------------------
# 19. Phase 4D2: controlled DEV CloudWatch activation -- workflow_dispatch
#     Boolean control, Argo CD ownership of the value, fail-closed CONFIG
#     preflight (GetItem-only, no Scan, no new IAM), and post-deployment
#     verification/rollback. Base Helm default stays disabled.
# ---------------------------------------------------------------------
echo ""
echo "--- Phase 4D2: controlled CloudWatch DEV activation ---"

if grep -q "publishEnabled: false" "${MONITOR_CHART}/values.yaml" 2>/dev/null; then
  pass "helm/goldengate-monitor base chart default still keeps cloudwatch.publishEnabled: false"
else
  fail "helm/goldengate-monitor base chart no longer defaults cloudwatch.publishEnabled to false"
fi

if grep -q "cloudwatch" "envs/dev/goldengate-monitor/values.yaml" 2>/dev/null; then
  fail "envs/dev/goldengate-monitor/values.yaml now overrides cloudwatch.publishEnabled -- activation must stay a per-run workflow input, not a persisted values override"
else
  pass "envs/dev/goldengate-monitor/values.yaml does not override cloudwatch.publishEnabled (base default governs unless a run explicitly requests otherwise)"
fi

if grep -q "enable_cloudwatch_publication:" "$MONITOR_WORKFLOW" 2>/dev/null \
    && grep -A3 "enable_cloudwatch_publication:" "$MONITOR_WORKFLOW" | grep -q "type: boolean" \
    && grep -A5 "enable_cloudwatch_publication:" "$MONITOR_WORKFLOW" | grep -q "default: false"; then
  pass "goldengate-monitor.yaml defines enable_cloudwatch_publication as a required Boolean input defaulting to false"
else
  fail "goldengate-monitor.yaml is missing the expected enable_cloudwatch_publication Boolean workflow_dispatch input"
fi

if grep -q "name: CloudWatch publication preflight (CONFIG.metricsEnabled)" "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml defines the CloudWatch publication preflight step"
else
  fail "goldengate-monitor.yaml is missing the CloudWatch publication preflight step"
fi

if grep -q "table.get_item(" "$MONITOR_WORKFLOW" 2>/dev/null \
    && ! grep -qE '\.[Ss]can\(' "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml's CloudWatch preflight uses GetItem only, never Scan"
else
  fail "goldengate-monitor.yaml's CloudWatch preflight no longer uses GetItem-only reads"
fi

if grep -q "PREREQUISITE NOT MET: no Ready gg-monitor pod found" "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml documents the first-deployment prerequisite instead of bypassing the CONFIG check"
else
  fail "goldengate-monitor.yaml is missing the first-deployment prerequisite failure message"
fi

if grep -q -- "- name: cloudwatch.publishEnabled" "$MONITOR_WORKFLOW" 2>/dev/null \
    && grep -q 'value: "\${CLOUDWATCH_PUBLISH_ENABLED_VALUE}"' "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml persists the requested value through the Argo CD Application Helm parameters (same ownership path as image.repository/image.tag)"
else
  fail "goldengate-monitor.yaml no longer passes cloudwatch.publishEnabled through the Argo CD Application Helm parameters"
fi

if grep -q "cloudwatchPublishEnabled=" "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml's runtime verification confirms the deployed CLOUDWATCH_PUBLISH_ENABLED value"
else
  fail "goldengate-monitor.yaml's runtime verification no longer confirms the deployed CLOUDWATCH_PUBLISH_ENABLED value"
fi

if grep -q "cloudwatch:ListMetrics" "$MONITOR_WORKFLOW" 2>/dev/null \
    || grep -q "cloudwatch:GetMetricData" "$MONITOR_WORKFLOW" 2>/dev/null; then
  fail "goldengate-monitor.yaml references a CloudWatch read IAM action -- none should ever be introduced for this phase"
else
  pass "goldengate-monitor.yaml introduces no CloudWatch read IAM action (ListMetrics/GetMetricData)"
fi

# ---------------------------------------------------------------------
# 20. Phase 4D2 pre-deployment correction: runtime-image hash scoped to
#     Dockerfile inputs only, unit tests unconditional, POSIX-safe
#     discovery, unique per-attempt Helm OCI revision, Ready-pod selection.
# ---------------------------------------------------------------------
echo ""
echo "--- Phase 4D2 correction: image hash scope, POSIX awk, chart SemVer, Ready-pod selection ---"

if grep -q 'git rev-parse "HEAD:\${MONITOR_SOURCE_PATH}"' "$MONITOR_WORKFLOW" 2>/dev/null; then
  fail "goldengate-monitor.yaml still hashes the whole monitoring/monitor tree (would include README.md/tests/**)"
else
  pass "goldengate-monitor.yaml no longer hashes the whole monitoring/monitor tree"
fi

if grep -q "MONITOR_IMAGE_INPUT_PATHS=(" "$MONITOR_WORKFLOW" 2>/dev/null \
    && grep -q "git ls-tree -r HEAD -- " "$MONITOR_WORKFLOW" 2>/dev/null \
    && grep -q "git hash-object --stdin" "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml computes a deterministic Git-based hash over exactly the Dockerfile-copied paths"
else
  fail "goldengate-monitor.yaml is missing the scoped Dockerfile-input hash computation"
fi

HASH_INPUT_ARRAY="$(awk '
  /MONITOR_IMAGE_INPUT_PATHS=\($/ { capture=1; print; next }
  capture { print }
  capture && /^ *\)$/ { exit }
' "$MONITOR_WORKFLOW")"
if grep -q '"\${MONITOR_SOURCE_PATH}/tools"' <<< "$HASH_INPUT_ARRAY" \
    && ! grep -q 'README.md' <<< "$HASH_INPUT_ARRAY" \
    && ! grep -q 'requirements-test.txt' <<< "$HASH_INPUT_ARRAY" \
    && ! grep -q '/tests' <<< "$HASH_INPUT_ARRAY"; then
  pass "goldengate-monitor.yaml's hash inputs exclude README.md/requirements-test.txt/tests"
else
  fail "goldengate-monitor.yaml's hash inputs unexpectedly include a non-runtime path"
fi

UNIT_TEST_STEPS_UNCONDITIONAL="true"
for step_name in "Set up Python" "Install monitor runtime and test dependencies" \
                 "Validate monitor Python syntax" "Run monitor unit tests"; do
  STEP_BLOCK="$(awk -v marker="- name: ${step_name}\$" '
    $0 ~ marker { found=1; print; next }
    found && /^      - name:/ { exit }
    found { print }
  ' "$MONITOR_WORKFLOW")"
  if grep -q "if: env.IMAGE_EXISTED" <<< "$STEP_BLOCK"; then
    fail "goldengate-monitor.yaml step \"${step_name}\" is still conditional on IMAGE_EXISTED"
    UNIT_TEST_STEPS_UNCONDITIONAL="false"
  fi
done
[ "$UNIT_TEST_STEPS_UNCONDITIONAL" = "true" ] && pass "Python setup/install/syntax-validation/unit-test steps run unconditionally (not gated on IMAGE_EXISTED)"

DOCKER_STEPS_CONDITIONAL="true"
for step_name in "Verify Docker binary and daemon are functional" "Login to Amazon ECR" \
                 "Build monitor image" "Push monitor image"; do
  STEP_BLOCK="$(awk -v marker="- name: ${step_name}\$" '
    $0 ~ marker { found=1; print; next }
    found && /^      - name:/ { exit }
    found { print }
  ' "$MONITOR_WORKFLOW")"
  if ! grep -q "if: env.IMAGE_EXISTED != 'true'" <<< "$STEP_BLOCK"; then
    fail "goldengate-monitor.yaml step \"${step_name}\" is no longer conditional on IMAGE_EXISTED"
    DOCKER_STEPS_CONDITIONAL="false"
  fi
done
[ "$DOCKER_STEPS_CONDITIONAL" = "true" ] && pass "Docker daemon-check/login/build/push steps remain conditional on IMAGE_EXISTED"

if grep -q '\[\[:space:\]\]' "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml's CloudWatch deployment-discovery awk uses POSIX [[:space:]], not GNU-only \\s"
else
  fail "goldengate-monitor.yaml's CloudWatch deployment-discovery awk does not use POSIX [[:space:]]"
fi

# Functional execution of the exact extracted awk script (proving it
# returns precisely the two enabled canonical deployments, never
# hardcoded) is covered by the Python suite -- see
# WorkflowStaticAnalysisTests.test_deployment_discovery_awk_returns_exactly_both_enabled_deployments,
# already run above in section 3 ("Python unit tests").

if grep -q 'CHART_VERSION="0.\${{ github.run_number }}.\${{ github.run_attempt }}"' "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml's chart version is a SemVer containing both run_number and run_attempt"
else
  fail "goldengate-monitor.yaml's chart version does not include run_attempt -- reruns would collide on a mutable Helm OCI repository"
fi

if grep -q '.items\[0\].metadata.name' "$MONITOR_WORKFLOW" 2>/dev/null; then
  fail "goldengate-monitor.yaml still blindly selects .items[0] for pod discovery"
else
  pass "goldengate-monitor.yaml no longer blindly selects .items[0] -- pod selection filters on Running phase and container readiness"
fi

# ---------------------------------------------------------------------
# 21. Phase 4D2 supply-chain/pod-selection correction: .dockerignore
#     participates in the runtime-image hash, the Dockerfile requires an
#     explicitly supplied digest-pinned private base image (no public
#     default), and Ready-pod selection excludes terminating pods.
# ---------------------------------------------------------------------
echo ""
echo "--- Phase 4D2 correction: .dockerignore hash input, digest-pinned base image, non-terminating Ready pod ---"

if grep -q '"\${MONITOR_SOURCE_PATH}/.dockerignore"' "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml includes .dockerignore in the runtime-image hash inputs"
else
  fail "goldengate-monitor.yaml's runtime-image hash inputs no longer include .dockerignore"
fi

if [ -f "${MONITOR_APP_DIR}/.dockerignore" ]; then
  pass "monitoring/monitor/.dockerignore exists (a hashed, tracked input)"
else
  fail "monitoring/monitor/.dockerignore is missing"
fi

if grep -q '^ARG BASE_IMAGE$' "${MONITOR_APP_DIR}/Dockerfile" 2>/dev/null \
    && ! grep -q '^ARG BASE_IMAGE=' "${MONITOR_APP_DIR}/Dockerfile" 2>/dev/null \
    && ! grep -q 'python:3.12-slim' "${MONITOR_APP_DIR}/Dockerfile" 2>/dev/null; then
  pass "monitoring/monitor/Dockerfile requires an explicitly supplied BASE_IMAGE (no public default)"
else
  fail "monitoring/monitor/Dockerfile still has a public default base image"
fi

if grep -q "name: Validate approved base image reference" "$MONITOR_WORKFLOW" 2>/dev/null \
    && grep -q "vars.MONITOR_BASE_IMAGE" "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml validates an externally supplied MONITOR_BASE_IMAGE (vars.* convention, not hardcoded)"
else
  fail "goldengate-monitor.yaml is missing the base-image validation step"
fi

if grep -qE '@sha256:\[0-9a-f\]\{64\}\$' "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml requires MONITOR_BASE_IMAGE to be digest-pinned (@sha256:<64 lowercase hex>)"
else
  fail "goldengate-monitor.yaml no longer enforces digest-pinning on MONITOR_BASE_IMAGE"
fi

BASE_IMAGE_STEP="$(awk '
  /- name: Validate approved base image reference/ { capture=1 }
  capture { print }
  capture && /- name: Prepare monitor image variables/ { exit }
' "$MONITOR_WORKFLOW")"
# The value is only ever interpolated on ONE line -- the GITHUB_ENV write
# that hands it to later steps. The success confirmation is a generic
# message (no value at all), and none of the three failure branches above
# interpolate it either. Proven functionally (with a marker value) by
# MonitorBaseImageValidationTests.test_failure_never_prints_the_raw_malformed_value
# and .test_success_path_never_prints_the_full_raw_value_either.
BASE_IMAGE_INTERPOLATIONS="$(grep -c '\${MONITOR_BASE_IMAGE}' <<< "$BASE_IMAGE_STEP" || true)"
if [ "${BASE_IMAGE_INTERPOLATIONS:-0}" -eq 1 ]; then
  pass "goldengate-monitor.yaml's base-image validation never prints the raw supplied value (only the GITHUB_ENV handoff interpolates it)"
else
  fail "goldengate-monitor.yaml's base-image validation interpolates \${MONITOR_BASE_IMAGE} ${BASE_IMAGE_INTERPOLATIONS:-0} times -- expected exactly 1 (GITHUB_ENV handoff only)"
fi

if grep -q "MONITOR_BASE_IMAGE_INPUT: \${{ vars.MONITOR_BASE_IMAGE }}" "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml passes the GitHub expression through step-level env (MONITOR_BASE_IMAGE_INPUT), never direct shell interpolation"
else
  fail "goldengate-monitor.yaml no longer passes vars.MONITOR_BASE_IMAGE through step-level env"
fi

if grep -qF -- '\${{ vars.MONITOR_BASE_IMAGE }}"' <<< "$BASE_IMAGE_STEP"; then
  fail "goldengate-monitor.yaml's base-image validation run script still directly interpolates \${{ vars.MONITOR_BASE_IMAGE }}"
else
  pass "goldengate-monitor.yaml's base-image validation run script contains no direct \${{ vars.MONITOR_BASE_IMAGE }} interpolation"
fi

if grep -Fq -- '--build-arg "BASE_IMAGE=${MONITOR_BASE_IMAGE}"' "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml passes the validated MONITOR_BASE_IMAGE into docker build via --build-arg"
else
  fail "goldengate-monitor.yaml no longer passes BASE_IMAGE into docker build"
fi

if grep -q 'echo "BASE_IMAGE \${MONITOR_BASE_IMAGE}"' "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml folds the resolved base-image reference into the same runtime-input hash"
else
  fail "goldengate-monitor.yaml's hash no longer incorporates the resolved base-image reference"
fi

TERMINATING_EXCLUSION_COUNT="$(grep -c 'deletionTimestamp == null' "$MONITOR_WORKFLOW" 2>/dev/null || true)"
if [ "${TERMINATING_EXCLUSION_COUNT:-0}" -eq 2 ]; then
  pass "both Ready-pod jq filters (preflight and post-deployment verification) exclude terminating pods"
else
  fail "expected exactly 2 Ready-pod jq filters excluding terminating pods, found ${TERMINATING_EXCLUSION_COUNT:-0}"
fi

# ---------------------------------------------------------------------
# 22. Phase 4D2 workflow-security and manager critical-service correction:
#     no direct GitHub-expression interpolation inside a run script, a
#     fully-anchored ECR repository+digest grammar (not prefix+suffix
#     only), and manager-compatible adminsrvr/distsrvr/recvsrvr coverage
#     for every deployment.
# ---------------------------------------------------------------------
echo ""
echo "--- Phase 4D2 correction: safe env passthrough, full ECR grammar, manager critical-service coverage ---"

if grep -qF -- 'MONITOR_BASE_IMAGE="${{ vars.MONITOR_BASE_IMAGE }}"' "$MONITOR_WORKFLOW" 2>/dev/null; then
  fail "goldengate-monitor.yaml still assigns \${{ vars.MONITOR_BASE_IMAGE }} directly inside a run script"
else
  pass "goldengate-monitor.yaml no longer assigns \${{ vars.MONITOR_BASE_IMAGE }} directly inside a run script"
fi

if grep -qE "MONITOR_BASE_IMAGE_PATTERN='\^\[a-z0-9\]\+" "$MONITOR_WORKFLOW" 2>/dev/null \
    && grep -q 'MONITOR_BASE_IMAGE_REMAINDER' "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml validates the full post-prefix remainder against an anchored repository+digest grammar (not prefix+suffix only)"
else
  fail "goldengate-monitor.yaml no longer validates the full ECR repository+digest grammar"
fi

if grep -q 'RECOGNIZED_CRITICAL_SERVICES = ("adminsrvr", "distsrvr", "recvsrvr")' "${MONITOR_APP_DIR}/health_rules.py" 2>/dev/null; then
  pass "health_rules.py defines the manager-compatible three-service recognized set (adminsrvr/distsrvr/recvsrvr)"
else
  fail "health_rules.py is missing the manager-compatible three-service recognized set"
fi

if grep -q "CRITICAL_SERVICES_BY_TYPE" "${MONITOR_APP_DIR}/health_rules.py" "${MONITOR_APP_DIR}/collector.py" 2>/dev/null; then
  fail "a per-type critical-service dict (CRITICAL_SERVICES_BY_TYPE) still exists -- Oracle/PostgreSQL must both default to the full three-service set"
else
  pass "no per-type critical-service dict remains -- every deployment defaults to the full three-service set"
fi

if grep -q "def resolve_critical_services" "${MONITOR_APP_DIR}/health_rules.py" 2>/dev/null; then
  pass "health_rules.py defines a bounded, fail-safe resolve_critical_services helper for the optional CONFIG override"
else
  fail "health_rules.py is missing resolve_critical_services"
fi

# ---------------------------------------------------------------------
# 23. Phase 5A: observer source/build/chart retirement, legacy-values
#     folder disablement without deletion, and gg-monitor legacy-fallback
#     removal.
# ---------------------------------------------------------------------
echo ""
echo "--- Phase 5A: legacy values folder disabled (retained, not deleted) ---"

if [ -f "$EKS_APP_WORKFLOW" ] && command -v python3 >/dev/null 2>&1; then
  # Extract the real, unmodified "Detect changed deployments" run script from
  # the workflow (never a reimplementation) and exercise its
  # is_active_deployment_values_file() function and its deletion-candidate
  # case statement directly, against the real repository files.
  python3 - "$EKS_APP_WORKFLOW" > "${WORKDIR}/detect_script.sh" <<'PYEOF'
import sys
import yaml
with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)
for step in doc["jobs"]["detect_changed_deployments"]["steps"]:
    if step.get("name") == "Detect changed deployments":
        sys.stdout.write(step["run"])
        break
else:
    sys.exit("step not found")
PYEOF

  awk '/^is_active_deployment_values_file\(\) \{/,/^\}$/' "${WORKDIR}/detect_script.sh" > "${WORKDIR}/is_active_fn.sh"

  # is_goldengate_deployment_values_file and its git-revision sibling both
  # depend on _classify_deployment_model_yaml -- all three must be extracted
  # and sourced together, in dependency order, or the classifier fails with
  # "command not found" while still being source-able (a broken harness that
  # silently produces no useful assertion, exactly the defect being fixed
  # here).
  {
    awk '/^_classify_deployment_model_yaml\(\) \{/,/^\}$/' "${WORKDIR}/detect_script.sh"
    echo ""
    awk '/^is_goldengate_deployment_values_file\(\) \{/,/^\}$/' "${WORKDIR}/detect_script.sh"
    echo ""
    awk '/^is_goldengate_deployment_values_file_at_ref\(\) \{/,/^\}$/' "${WORKDIR}/detect_script.sh"
  } > "${WORKDIR}/is_gg_fn.sh"

  # Fail loudly (not silently) if any expected function body failed to
  # extract -- an empty/missing body here would make every downstream
  # source-and-call test below meaningless.
  for required_fn in _classify_deployment_model_yaml is_goldengate_deployment_values_file is_goldengate_deployment_values_file_at_ref; do
    if ! grep -q "^${required_fn}() {" "${WORKDIR}/is_gg_fn.sh"; then
      fail "could not extract ${required_fn}() from ${EKS_APP_WORKFLOW} -- the classifier test harness cannot run"
    fi
  done

  cat > "${WORKDIR}/run_is_active_checks.sh" <<HARNESS
#!/bin/bash
set -euo pipefail
source "${WORKDIR}/is_active_fn.sh"

check_one() {
  local file="\$1" expect_status="\$2" label="\$3"
  set +e
  reason="\$(is_active_deployment_values_file "\$file")"
  status=\$?
  set -e
  if [ "\$status" -eq "\$expect_status" ]; then
    echo "PASS \$label (\$reason)"
  else
    echo "FAIL \$label (expected status \$expect_status, got \$status, reason: \$reason)"
  fi
}

check_one "envs/dev/payments-ora-to-pg-001/values.yaml" 1 "legacy-inactive"
check_one "envs/dev/gg-oracle-payments-01/values.yaml" 0 "oracle-active"
check_one "envs/dev/gg-postgresql-payments-01/values.yaml" 0 "postgresql-active"
HARNESS

  ACTIVE_CHECK_OUTPUT="$(bash "${WORKDIR}/run_is_active_checks.sh" 2>&1 || true)"
  echo "$ACTIVE_CHECK_OUTPUT"

  if echo "$ACTIVE_CHECK_OUTPUT" | grep -q "^PASS legacy-inactive"; then
    pass "the real workflow's is_active_deployment_values_file() reports payments-ora-to-pg-001 inactive"
  else
    fail "payments-ora-to-pg-001 is not reported inactive by the real workflow function"
  fi

  cat > "${WORKDIR}/run_is_gg_checks.sh" <<HARNESS
#!/bin/bash
set -euo pipefail
source "${WORKDIR}/is_gg_fn.sh"

check_one() {
  local file="\$1" expect_status="\$2" label="\$3"
  set +e
  reason="\$(is_goldengate_deployment_values_file "\$file")"
  status=\$?
  set -e
  if [ "\$status" -eq "\$expect_status" ]; then
    echo "PASS \$label (\$reason)"
  else
    echo "FAIL \$label (expected status \$expect_status, got \$status, reason: \$reason)"
  fi
}

check_one "envs/dev/gg-oracle-payments-01/values.yaml" 0 "oracle-is-gg"
check_one "envs/dev/gg-postgresql-payments-01/values.yaml" 0 "postgresql-is-gg"
check_one "envs/dev/payments-ora-to-pg-001/values.yaml" 0 "legacy-is-gg"
check_one "envs/dev/goldengate-monitor/values.yaml" 1 "monitor-is-not-gg"
check_one "envs/dev/argocd/values.yaml" 1 "argocd-is-not-gg"
HARNESS

  GG_CHECK_OUTPUT="$(bash "${WORKDIR}/run_is_gg_checks.sh" 2>&1 || true)"
  echo "$GG_CHECK_OUTPUT"

  if echo "$GG_CHECK_OUTPUT" | grep -q "^PASS oracle-is-gg" \
      && echo "$GG_CHECK_OUTPUT" | grep -q "^PASS postgresql-is-gg" \
      && echo "$GG_CHECK_OUTPUT" | grep -q "^PASS legacy-is-gg"; then
    pass "the real workflow's is_goldengate_deployment_values_file() classifies all three GoldenGate deployment folders correctly (regardless of active/inactive state)"
  else
    fail "one or more GoldenGate deployment folders are misclassified by is_goldengate_deployment_values_file()"
  fi

  if echo "$GG_CHECK_OUTPUT" | grep -q "^PASS monitor-is-not-gg" \
      && echo "$GG_CHECK_OUTPUT" | grep -q "^PASS argocd-is-not-gg"; then
    pass "the real workflow's is_goldengate_deployment_values_file() correctly rejects goldengate-monitor and argocd (no deploymentModel field)"
  else
    fail "goldengate-monitor and/or argocd are incorrectly classified as GoldenGate deployments"
  fi

  if echo "$ACTIVE_CHECK_OUTPUT" | grep -q "^PASS oracle-active" && echo "$ACTIVE_CHECK_OUTPUT" | grep -q "^PASS postgresql-active"; then
    pass "the real workflow's is_active_deployment_values_file() reports both canonical folders active"
  else
    fail "one or both canonical folders are not reported active by the real workflow function"
  fi

  # A shared-chart-change selection scans every envs/dev/<id>/values.yaml
  # (excluding argocd/) exactly as the workflow does, then filters through
  # the same two real functions, in the same order the workflow applies them
  # (is_goldengate_deployment_values_file first, then
  # is_active_deployment_values_file) -- proving the exact resulting active
  # set, using the actual discovery command.
  CANDIDATE_IDS="$(find envs/dev -mindepth 2 -maxdepth 2 -name values.yaml -not -path 'envs/dev/argocd/*' \
    | sed -E 's#^envs/dev/([^/]+)/values\.yaml$#\1#' | sort -u)"
  ACTIVE_IDS=""
  for id in $CANDIDATE_IDS; do
    set +e
    bash -c "source '${WORKDIR}/is_gg_fn.sh'; is_goldengate_deployment_values_file 'envs/dev/${id}/values.yaml'" >/dev/null 2>&1
    gg_st=$?
    set -e
    if [ "$gg_st" -ne 0 ]; then
      continue
    fi
    set +e
    bash -c "source '${WORKDIR}/is_active_fn.sh'; is_active_deployment_values_file 'envs/dev/${id}/values.yaml'" >/dev/null 2>&1
    st=$?
    set -e
    [ "$st" -eq 0 ] && ACTIVE_IDS="${ACTIVE_IDS} ${id}"
  done
  ACTIVE_IDS_SORTED="$(echo "$ACTIVE_IDS" | tr ' ' '\n' | sed '/^$/d' | sort -u | tr '\n' ' ' | sed -E 's/ $//')"
  echo "Active candidate IDs for a shared-chart-change selection: ${ACTIVE_IDS_SORTED}"

  EXPECTED_ACTIVE_IDS="gg-oracle-payments-01 gg-postgresql-payments-01"
  if [ "$ACTIVE_IDS_SORTED" = "$EXPECTED_ACTIVE_IDS" ]; then
    pass "a shared chart change produces exactly the canonical active set (${EXPECTED_ACTIVE_IDS}) -- no additional ID present"
  else
    fail "a shared chart change produced an unexpected active set: got [${ACTIVE_IDS_SORTED}], expected [${EXPECTED_ACTIVE_IDS}]"
  fi

  if echo "$ACTIVE_IDS_SORTED" | grep -qw "goldengate-monitor"; then
    fail "goldengate-monitor is present in the shared-chart-change active set -- it must never enter the GoldenGate matrix"
  else
    pass "goldengate-monitor is absent from the shared-chart-change active set"
  fi

  if echo "$ACTIVE_IDS_SORTED" | grep -qw "argocd"; then
    fail "argocd is present in the shared-chart-change active set -- it must never enter the GoldenGate matrix"
  else
    pass "argocd is absent from the shared-chart-change active set"
  fi

  # Deletion-candidate safeguard: extract the real case-statement logic and
  # exercise it, together with the REAL classifier functions (never stubs
  # for is_goldengate_deployment_values_file/_at_ref -- only jq is stubbed,
  # since its own JSON behavior is not what this test verifies), against a
  # throwaway, self-contained Git repository built specifically to exercise
  # all 7 required scenarios: existing/removed files, GoldenGate/non-
  # GoldenGate deploymentModel, and malformed/unknown content.
  awk '/^for CANDIDATE_ID in \$DELETION_CANDIDATE_IDS; do$/,/^done$/' "${WORKDIR}/detect_script.sh" > "${WORKDIR}/deletion_loop.sh"

  if [ -s "${WORKDIR}/deletion_loop.sh" ] && [ -s "${WORKDIR}/is_gg_fn.sh" ]; then
    DELETION_REPO="${WORKDIR}/deletion-repo"
    rm -rf "$DELETION_REPO"
    mkdir -p "$DELETION_REPO"

    mkdir -p "${DELETION_REPO}/envs/dev/case2-removed-canonical" \
             "${DELETION_REPO}/envs/dev/goldengate-monitor" \
             "${DELETION_REPO}/envs/dev/argocd" \
             "${DELETION_REPO}/envs/dev/case6-malformed" \
             "${DELETION_REPO}/envs/dev/case7-unknown-model"

    printf 'deploymentModel: singleRuntime\nrunning: at-base-revision\n' > "${DELETION_REPO}/envs/dev/case2-removed-canonical/values.yaml"
    printf 'global:\n  environment: dev\nnamespace:\n  create: true\n' > "${DELETION_REPO}/envs/dev/goldengate-monitor/values.yaml"
    printf 'server:\n  extraArgs: []\n' > "${DELETION_REPO}/envs/dev/argocd/values.yaml"
    printf 'deploymentModel: singleRuntime\n  bad indent: [unterminated\n' > "${DELETION_REPO}/envs/dev/case6-malformed/values.yaml"
    printf 'deploymentModel: someUnknownModel\n' > "${DELETION_REPO}/envs/dev/case7-unknown-model/values.yaml"

    git -C "$DELETION_REPO" init -q
    git -C "$DELETION_REPO" config user.email "test@test.invalid"
    git -C "$DELETION_REPO" config user.name "test"
    git -C "$DELETION_REPO" add -A
    git -C "$DELETION_REPO" commit -q -m "base revision"
    DELETION_BEFORE_SHA="$(git -C "$DELETION_REPO" rev-parse HEAD)"

    # Now mutate the working tree to the "after" state the loop actually
    # evaluates: case2/4/5/6/7 are removed (git rm, matching a real
    # removed/renamed deletion candidate); case1 and case3 are added fresh
    # in the working tree only (never committed -- they represent "still
    # exists, but now inactive" candidates, which is what the loop's
    # is_goldengate_deployment_values_file working-tree path reads).
    git -C "$DELETION_REPO" rm -rq envs/dev/case2-removed-canonical envs/dev/goldengate-monitor envs/dev/argocd envs/dev/case6-malformed envs/dev/case7-unknown-model

    mkdir -p "${DELETION_REPO}/envs/dev/case1-payments-ora-to-pg-001" "${DELETION_REPO}/envs/dev/case3-lifecycle-absent"
    printf 'deploymentModel: legacyPair\ndeployment:\n  enabled: false\n' > "${DELETION_REPO}/envs/dev/case1-payments-ora-to-pg-001/values.yaml"
    printf 'deploymentModel: legacyPair\nlifecycle:\n  state: absent\n' > "${DELETION_REPO}/envs/dev/case3-lifecycle-absent/values.yaml"

    DELETION_TEST_OUTPUT="$(cd "$DELETION_REPO" && bash -c '
      set -euo pipefail
      source "'"${WORKDIR}"'/is_gg_fn.sh"
      source "'"${WORKDIR}"'/is_active_fn.sh"
      jq() {
        shift
        local args=("$@") model="" id=""
        for i in "${!args[@]}"; do
          [ "${args[$i]}" = "deployment_id" ] && id="${args[$((i+1))]}"
          [ "${args[$i]}" = "deployment_model" ] && model="${args[$((i+1))]}"
        done
        echo "[ADDED id=${id} model=${model}]"
      }
      BEFORE_SHA="'"$DELETION_BEFORE_SHA"'"

      for id in case1-payments-ora-to-pg-001 case2-removed-canonical case3-lifecycle-absent goldengate-monitor argocd case6-malformed case7-unknown-model; do
        DELETION_MATRIX_ITEMS="[]"
        INACTIVE_LOG=""
        DELETION_CANDIDATE_IDS="$id"
        source "'"${WORKDIR}"'/deletion_loop.sh"
        echo "RESULT ${id} => ${DELETION_MATRIX_ITEMS}"
      done
    ' 2>&1)"
    DELETION_HARNESS_STATUS=$?
    echo "$DELETION_TEST_OUTPUT"

    # The harness itself must never silently swallow a broken classifier:
    # any command-not-found or Python traceback anywhere in the captured
    # output fails this test outright, regardless of what the individual
    # case assertions below would otherwise report.
    if [ "$DELETION_HARNESS_STATUS" -ne 0 ] \
        || echo "$DELETION_TEST_OUTPUT" | grep -qiE "command not found|Traceback \(most recent call last\)|: not found$"; then
      fail "the deletion-candidate test harness itself failed or is broken (command-not-found/traceback/non-zero exit) -- see output above"
    else
      pass "the deletion-candidate test harness ran the real classifier functions with no command-not-found/traceback"
    fi

    check_deletion_case() {
      local label="$1" pattern="$2"
      if echo "$DELETION_TEST_OUTPUT" | grep -qE "$pattern"; then
        pass "$label"
      else
        fail "$label -- expected pattern not found: ${pattern}"
      fi
    }

    check_deletion_case "1: existing payments-ora-to-pg-001 with deployment.enabled=false produces no deletion entry" \
      '^RESULT case1-payments-ora-to-pg-001 => \[\]$'
    check_deletion_case "2: removed canonical GoldenGate values (deploymentModel: singleRuntime) produces a deletion entry with deployment_model=singleRuntime" \
      '^RESULT case2-removed-canonical => \[ADDED id=case2-removed-canonical model=singleRuntime\]$'
    check_deletion_case "3: existing GoldenGate file with lifecycle.state=absent produces a deletion entry" \
      '^RESULT case3-lifecycle-absent => \[ADDED id=case3-lifecycle-absent model=legacyPair\]$'
    check_deletion_case "4: removed goldengate-monitor values (no deploymentModel) produces no deletion entry" \
      '^RESULT goldengate-monitor => \[\]$'
    check_deletion_case "5: removed argocd values (no deploymentModel) produces no deletion entry" \
      '^RESULT argocd => \[\]$'
    check_deletion_case "6: removed malformed YAML produces no deletion entry" \
      '^RESULT case6-malformed => \[\]$'
    check_deletion_case "7: removed unknown deploymentModel produces no deletion entry" \
      '^RESULT case7-unknown-model => \[\]$'

    rm -rf "$DELETION_REPO"
  else
    fail "could not extract the deletion-candidate loop and/or classifier functions from ${EKS_APP_WORKFLOW}"
  fi
else
  skip "Phase 5A legacy-folder behavioral checks -- ${EKS_APP_WORKFLOW} or python3 not available"
fi

echo ""
echo "--- Phase 5A: no Argo CD Application/namespace/PVC/EFS deletion command tied to disabling the legacy folder ---"
if [ -f "$EKS_APP_WORKFLOW" ]; then
  # The only place this workflow deletes an Argo CD Application or
  # namespace is delete_removed_argocd_applications, gated on
  # has_deletions=true from the deletion matrix -- already proven above to
  # exclude deployment.enabled=false. No separate, disable-triggered
  # deletion path may exist anywhere else in the file.
  DIRECT_DELETE_HITS="$(grep -n 'kubectl delete\|delete-repository\|efs delete-access-point\|delete_access_point' "$EKS_APP_WORKFLOW" | grep -v 'kubectl delete application\|kubectl delete namespace' || true)"
  if [ -z "$DIRECT_DELETE_HITS" ]; then
    pass "no unexpected delete command exists outside the guarded Argo CD Application/namespace cleanup path"
  else
    fail "unexpected delete command(s) found in ${EKS_APP_WORKFLOW}:"$'\n'"${DIRECT_DELETE_HITS}"
  fi

  if grep -q 'delete_removed_argocd_applications' "$EKS_APP_WORKFLOW" \
      && grep -q "needs.detect_changed_deployments.outputs.has_deletions == 'true'" "$EKS_APP_WORKFLOW"; then
    pass "Argo CD Application/namespace deletion remains gated on has_deletions (deletion-matrix-driven, never folder-disable-driven)"
  else
    fail "the deletion job's has_deletions gating condition is missing or changed"
  fi
else
  skip "deletion-command sweep -- ${EKS_APP_WORKFLOW} not found"
fi

echo ""
echo "--- Phase 5A: no direct \${{ inputs.* }} interpolation in run scripts; marker-file injection tests ---"

if [ -f "$EKS_APP_WORKFLOW" ]; then
  INPUTS_INTERP_HITS="$(grep -n '\${{ *inputs\.' "$EKS_APP_WORKFLOW" | grep -v '^\s*[0-9]*: *INPUT_[A-Z_]*: \${{ *inputs\.' || true)"
  # The only acceptable occurrences are inside a step-level `env:` mapping
  # (INPUT_X: ${{ inputs.x }}), never inside a run-script body. Re-check
  # precisely against the full line text (grep -v above already filtered
  # the common env-mapping shape; anything left over is a real hit).
  if [ -n "$INPUTS_INTERP_HITS" ]; then
    fail "\${{ inputs.* }} appears outside a step-level env: mapping in ${EKS_APP_WORKFLOW}:"$'\n'"${INPUTS_INTERP_HITS}"
  else
    pass "every \${{ inputs.* }} occurrence in ${EKS_APP_WORKFLOW} is confined to a step-level env: mapping, never a run-script body"
  fi
else
  skip "inputs.* interpolation sweep -- ${EKS_APP_WORKFLOW} not found"
fi

if [ -f "${WORKDIR}/detect_script.sh" ] && command -v python3 >/dev/null 2>&1; then
  # Marker-file proof: feed the real extracted "Detect changed deployments"
  # script a workflow_dispatch deployment_id containing shell metacharacters
  # via INPUT_DEPLOYMENT_ID (exactly how the real env: mapping delivers it),
  # and confirm the payload is never evaluated as shell code. github.actor/
  # github.event_name are the only remaining ${{ }} expressions in the run
  # body; substitute them the same way GitHub Actions itself would before
  # execution, since this script is never otherwise runnable standalone.
  sed -e 's/\${{ *github\.event_name *}}/workflow_dispatch/g' \
      "${WORKDIR}/detect_script.sh" > "${WORKDIR}/detect_script_resolved.sh"

  MARKER_DIR="${WORKDIR}/marker-test"
  mkdir -p "$MARKER_DIR"
  MARKER_FILE="${MARKER_DIR}/PWNED"

  INJECTION_FAILED="false"
  run_injection_case() {
    local label="$1" payload="$2"
    rm -f "$MARKER_FILE"
    INJECTION_OUTPUT="$(
      cd "$REPO_ROOT" && \
      INPUT_ENVIRONMENT="dev" \
      INPUT_DEPLOYMENT_ID="$payload" \
      INPUT_DEPLOY="true" \
      GITHUB_OUTPUT="$(mktemp)" \
      GITHUB_ENV="$(mktemp)" \
      MARKER_FILE_FOR_TEST="$MARKER_FILE" \
      bash "${WORKDIR}/detect_script_resolved.sh" 2>&1 || true
    )"
    if [ -f "$MARKER_FILE" ]; then
      fail "marker-file injection succeeded for ${label} (deployment_id=${payload@Q}) -- command execution occurred"
      INJECTION_FAILED="true"
    fi
  }

  # Payloads reference $MARKER_FILE_FOR_TEST (exported above) rather than an
  # embedded absolute path, so the exact same payload strings work
  # regardless of $WORKDIR's location.
  run_injection_case "command-substitution" '$(touch "$MARKER_FILE_FOR_TEST")'
  run_injection_case "backticks" '`touch "$MARKER_FILE_FOR_TEST"`'
  run_injection_case "double-quote-break" 'x"; touch "$MARKER_FILE_FOR_TEST"; echo "'
  run_injection_case "single-quote-break" "x'; touch \"\$MARKER_FILE_FOR_TEST\"; echo '"
  run_injection_case "semicolon" 'x; touch "$MARKER_FILE_FOR_TEST"'
  run_injection_case "newline" "$(printf 'x\ntouch "$MARKER_FILE_FOR_TEST"')"
  run_injection_case "dollar-brace-ifs" '${IFS}touch${IFS}"$MARKER_FILE_FOR_TEST"'
  run_injection_case "background-ampersand" 'x & touch "$MARKER_FILE_FOR_TEST"'
  run_injection_case "pipe" 'x | touch "$MARKER_FILE_FOR_TEST"'

  if [ "$INJECTION_FAILED" = "false" ]; then
    pass "9 shell-metacharacter payloads in deployment_id (\$(...), backticks, quotes, semicolons, newlines, \${IFS}, &, |) cannot execute commands (marker file never created)"
  fi

  rm -rf "$MARKER_DIR"
else
  skip "marker-file injection tests -- ${WORKDIR}/detect_script.sh or python3 not available"
fi

echo ""
echo "--- Phase 5A: observer Helm/template/chart-values retirement ---"

if [ -f "helm/goldengate/templates/_observer.tpl" ]; then
  fail "helm/goldengate/templates/_observer.tpl still exists"
else
  pass "helm/goldengate/templates/_observer.tpl no longer exists"
fi

if grep -q "^\s*observer:" "helm/goldengate/values.yaml" 2>/dev/null; then
  fail "helm/goldengate/values.yaml still exposes a monitoring.observer block"
else
  pass "helm/goldengate/values.yaml exposes no monitoring.observer block"
fi

if grep -A1 "^monitoring:" "helm/goldengate/values.yaml" 2>/dev/null | grep -q "labels:"; then
  pass "helm/goldengate/values.yaml still exposes monitoring.labels (preserved for shared monitoring/future logging)"
else
  fail "helm/goldengate/values.yaml no longer exposes monitoring.labels -- must be preserved"
fi

OBSERVER_TEMPLATE_HITS="$(grep -l -i "goldengate-observer\|monitoring\.observer\|observerContainer" helm/goldengate/templates/*.yaml 2>/dev/null || true)"
if [ -z "$OBSERVER_TEMPLATE_HITS" ]; then
  pass "no helm/goldengate template references an observer container/include/value"
else
  fail "observer references remain in: ${OBSERVER_TEMPLATE_HITS}"
fi

if [ "$HELM_AVAILABLE" = "true" ] && [ "$PYTHON_AVAILABLE" = "true" ]; then
  for pair in "gg-oracle-payments-01:goldengate-dev" "gg-postgresql-payments-01:goldengate-dev" "payments-ora-to-pg-001:gg-dev-payments-ora-to-pg-001"; do
    id="${pair%%:*}"; ns="${pair##*:}"
    VALUES_FILE="envs/dev/${id}/values.yaml"
    RENDERED="${WORKDIR}/${id}-observer-check.yaml"
    if helm template "$id" "$RUNTIME_CHART" --namespace "$ns" -f "$VALUES_FILE" \
        --set global.environment=dev --set global.deploymentId="$id" > "$RENDERED" 2>"${WORKDIR}/${id}-observer-check.err"; then
      if grep -qi "goldengate-observer\|observer-enabled" "$RENDERED"; then
        fail "${id}: rendered manifest still contains an observer container/annotation reference"
      else
        pass "${id}: rendered manifest contains no observer container/annotation reference"
      fi
    else
      fail "${id}: helm template failed during observer-absence render check"
      cat "${WORKDIR}/${id}-observer-check.err"
    fi
  done
else
  skip "rendered-manifest observer-absence check -- helm and/or python3/PyYAML not available"
fi

if [ -d "monitoring/observer" ]; then
  fail "monitoring/observer directory still exists"
else
  pass "monitoring/observer directory no longer exists"
fi

echo ""
echo "--- Phase 5A: observer image logic retired from ${EKS_APP_WORKFLOW} ---"
if [ -f "$EKS_APP_WORKFLOW" ]; then
  if grep -q "ensure_observer_image:" "$EKS_APP_WORKFLOW"; then
    fail "${EKS_APP_WORKFLOW} still defines the ensure_observer_image job"
  else
    pass "${EKS_APP_WORKFLOW} no longer defines the ensure_observer_image job"
  fi

  OBSERVER_ECR_HITS="$(grep -n "OBSERVER_ECR_REPOSITORY\|OBSERVER_SOURCE_PATH\|Ensure observer ECR repository\|observer ECR repository policy" "$EKS_APP_WORKFLOW" || true)"
  if [ -z "$OBSERVER_ECR_HITS" ]; then
    pass "${EKS_APP_WORKFLOW} contains no observer ECR repository creation/policy operation"
  else
    fail "${EKS_APP_WORKFLOW} still references observer ECR repository operations:"$'\n'"${OBSERVER_ECR_HITS}"
  fi

  if grep -q "AllowEksDevAccountPullGoldengateObserver" "$EKS_APP_WORKFLOW"; then
    fail "${EKS_APP_WORKFLOW} still defines the observer cross-account ECR repository policy statement"
  else
    pass "${EKS_APP_WORKFLOW} no longer defines the observer cross-account ECR repository policy statement"
  fi

  if grep -q "monitoring/observer" "$EKS_APP_WORKFLOW"; then
    fail "${EKS_APP_WORKFLOW} still references monitoring/observer (push trigger path or elsewhere)"
  else
    pass "${EKS_APP_WORKFLOW} no longer references monitoring/observer anywhere"
  fi
else
  skip "workflow observer-retirement checks -- ${EKS_APP_WORKFLOW} not found"
fi

echo ""
echo "--- Phase 5A: gg-monitor legacy-fallback removal ---"

if grep -q "legacyFallback" "helm/goldengate-monitor/values.yaml" 2>/dev/null; then
  fail "helm/goldengate-monitor/values.yaml still defines legacyFallback"
else
  pass "helm/goldengate-monitor/values.yaml no longer defines legacyFallback"
fi

if grep -q "LEGACY_FALLBACK_ENABLED" "helm/goldengate-monitor/templates/deployment.yaml" 2>/dev/null; then
  fail "helm/goldengate-monitor/templates/deployment.yaml still sets LEGACY_FALLBACK_ENABLED"
else
  pass "helm/goldengate-monitor/templates/deployment.yaml no longer sets LEGACY_FALLBACK_ENABLED"
fi

if grep -q "legacy_fallback_enabled\|LEGACY_FALLBACK_ENABLED" "${MONITOR_APP_DIR}/config.py" 2>/dev/null; then
  fail "config.py still has a legacy_fallback_enabled field"
else
  pass "config.py has no legacy_fallback_enabled field"
fi

if grep -q "compute_legacy_effective_status\|_LEGACY_STATUS_MAP\|legacy-observer-fallback" "${MONITOR_APP_DIR}/monitor.py" 2>/dev/null; then
  fail "monitor.py still contains legacy-observer status-conversion code or the legacy-observer-fallback data source"
else
  pass "monitor.py contains no legacy-observer status-conversion code or legacy-observer-fallback data source"
fi

if grep -q "gg-{pipeline_id}-{role}\|gg-payments-ora-to-pg-001-source\|gg-payments-ora-to-pg-001-target" "${MONITOR_APP_DIR}/monitor.py" 2>/dev/null; then
  fail "monitor.py still builds or hardcodes a legacy per-role observer partition key"
else
  pass "monitor.py never builds or hardcodes a legacy per-role observer partition key"
fi

if [ "$HELM_AVAILABLE" = "true" ]; then
  if helm template gg-monitor "$MONITOR_CHART_STAGED" --namespace goldengate-monitoring \
      --set image.repository=example.com/x --set image.tag=1 --set serviceAccount.roleArn=arn:aws:iam::000000000000:role/x \
      2>"${WORKDIR}/monitor-legacy-render.err" | grep -q "LEGACY_FALLBACK_ENABLED"; then
    fail "rendered goldengate-monitor Deployment still contains LEGACY_FALLBACK_ENABLED"
  else
    pass "rendered goldengate-monitor Deployment contains no LEGACY_FALLBACK_ENABLED variable"
  fi
else
  skip "rendered monitor Deployment legacy-fallback check -- helm not available"
fi

echo ""
echo "--- Phase 5A: no alarms/SNS/gg-alerter/Fluent Bit introduced; IAM unchanged ---"

# Structural signals only -- never a bare substring grep, which would
# false-positive on this repository's own negative-assertion code (e.g. a
# test's forbidden-string tuple, or FORBIDDEN_CONTAINER_SUBSTRINGS in the
# workflow's singleRuntime contract check -- both deliberately mention these
# names to prove their absence, not to implement them).
NOT_YET_HITS=""
[ -d "monitoring/gg-alerter" ] && NOT_YET_HITS="${NOT_YET_HITS} monitoring/gg-alerter/"
[ -d "helm/gg-alerter" ] && NOT_YET_HITS="${NOT_YET_HITS} helm/gg-alerter/"
FLUENTBIT_CHART_HITS="$(find helm -maxdepth 2 -iname "*fluent-bit*" -o -iname "*fluentbit*" 2>/dev/null | grep -v '^helm/argocd/' || true)"
[ -n "$FLUENTBIT_CHART_HITS" ] && NOT_YET_HITS="${NOT_YET_HITS} ${FLUENTBIT_CHART_HITS}"
DAEMONSET_HITS="$(grep -rl "kind: DaemonSet" helm/goldengate helm/goldengate-monitor 2>/dev/null || true)"
[ -n "$DAEMONSET_HITS" ] && NOT_YET_HITS="${NOT_YET_HITS} ${DAEMONSET_HITS}"
ALARM_SNS_HITS="$(grep -rl "aws_cloudwatch_metric_alarm\|aws cloudwatch put-metric-alarm\|sns:Publish\|sns:CreateTopic\|aws sns create-topic" \
  envs/dev "$EKS_APP_WORKFLOW" "$MONITOR_WORKFLOW" helm/goldengate-monitor 2>/dev/null || true)"
[ -n "$ALARM_SNS_HITS" ] && NOT_YET_HITS="${NOT_YET_HITS} ${ALARM_SNS_HITS}"

if [ -z "$NOT_YET_HITS" ]; then
  pass "no alarm/SNS/gg-alerter/Fluent Bit implementation was introduced"
else
  fail "unexpected alarm/SNS/gg-alerter/Fluent Bit implementation found in:${NOT_YET_HITS}"
fi

# Phase 5B1 legitimately changes envs/dev/policies/goldengate-secrets-read-dev
# (observer DynamoDB/CloudWatch statements removed) and envs/dev/iam.tf's
# comments/description text -- the monitor role's policy folder must remain
# completely untouched, and iam.tf's structural identifiers (role names,
# policy_folder attachments) must not change even though description text
# may. Whitespace/line-ending-only diffs are pre-existing baseline noise in
# this repository -- compare with --ignore-all-space so only substantive
# content changes count.
MONITOR_IAM_DIFF="$(git diff --ignore-all-space -- envs/dev/policies/goldengate-monitor-read-dev 2>/dev/null || true)"
if [ -z "$MONITOR_IAM_DIFF" ]; then
  pass "envs/dev/policies/goldengate-monitor-read-dev has no substantive changes (monitor IAM untouched)"
else
  fail "unexpected change detected in envs/dev/policies/goldengate-monitor-read-dev -- the monitor role must remain untouched"
fi

# ---------------------------------------------------------------------
# 27. Phase 5B1: runtime IAM least-privilege reduction (observer DynamoDB/
#     CloudWatch permissions removed; monitor IAM and Secrets Manager/KMS
#     access for canonical and legacy runtime pods unchanged).
# ---------------------------------------------------------------------
echo ""
echo "--- Phase 5B1: runtime IAM least-privilege reduction ---"

RUNTIME_POLICY_FILE="envs/dev/policies/goldengate-secrets-read-dev/policies/policies_1.json"
MONITOR_POLICY_FILE="envs/dev/policies/goldengate-monitor-read-dev/policies/policies_1.json"

if [ -f "$RUNTIME_POLICY_FILE" ] && [ -f "$MONITOR_POLICY_FILE" ] && command -v python3 >/dev/null 2>&1; then
  IAM_TEST_OUTPUT="$(python3 - "$RUNTIME_POLICY_FILE" "$MONITOR_POLICY_FILE" <<'PYEOF'
import json
import sys

runtime_path, monitor_path = sys.argv[1:3]

with open(runtime_path) as f:
    runtime = json.load(f)

with open(monitor_path) as f:
    monitor = json.load(f)

runtime_statements = runtime.get("Statement") or []
monitor_statements = monitor.get("Statement") or []


def actions_of(stmt):
    a = stmt.get("Action")
    if isinstance(a, str):
        return {a}
    return set(a or [])


def find_sid(statements, sid):
    for s in statements:
        if s.get("Sid") == sid:
            return s
    return None


results = []


def check(label, condition):
    results.append((label, bool(condition)))


# 1. Runtime policy grants no DynamoDB action anywhere (the entire
# monitoring-state statement, not just its Sid, must be gone).
runtime_dynamodb_actions = set()
for s in runtime_statements:
    runtime_dynamodb_actions |= {a for a in actions_of(s) if a.startswith("dynamodb:")}
check("1_no_dynamodb_actions", not runtime_dynamodb_actions)

# 2. Runtime policy grants no cloudwatch:PutMetricData (or any cloudwatch:*).
runtime_cloudwatch_actions = set()
for s in runtime_statements:
    runtime_cloudwatch_actions |= {a for a in actions_of(s) if a.startswith("cloudwatch:")}
check("2_no_cloudwatch_actions", not runtime_cloudwatch_actions)

# 3. Runtime role retains its Secrets Manager statement, byte-identical to
# the original (never broadened to compensate for the removed statements).
secrets_stmt = find_sid(runtime_statements, "AllowReadGoldenGateDevSecrets")
check(
    "3_secrets_manager_retained",
    secrets_stmt is not None
    and actions_of(secrets_stmt) == {"secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"}
    and secrets_stmt.get("Resource") == ["arn:aws:secretsmanager:eu-west-1:668311715351:secret:dev/goldengate/*"]
    and secrets_stmt.get("Effect") == "Allow",
)

# 4. Runtime role retains its KMS Decrypt statement, byte-identical.
kms_stmt = find_sid(runtime_statements, "AllowDecryptGoldenGateSecretsKms")
check(
    "4_kms_retained",
    kms_stmt is not None
    and actions_of(kms_stmt) == {"kms:Decrypt"}
    and kms_stmt.get("Resource") == "*"
    and kms_stmt.get("Effect") == "Allow",
)

# 5. Monitor role retains DynamoDB read/write (CONFIG reads + LEASE/STATE#
# writes travel over the same table-level actions) and PutMetricData scoped
# to GoldenGate/Pipelines.
monitor_ddb_stmt = find_sid(monitor_statements, "AllowReadWriteGoldenGateMonitoringState")
monitor_cw_stmt = find_sid(monitor_statements, "AllowPublishGoldenGateMonitoringMetrics")
check(
    "5_monitor_dynamodb_and_metrics_retained",
    monitor_ddb_stmt is not None
    and actions_of(monitor_ddb_stmt) == {
        "dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem",
        "dynamodb:UpdateItem", "dynamodb:DescribeTable",
    }
    and monitor_ddb_stmt.get("Resource") == "arn:aws:dynamodb:eu-west-1:668311715351:table/gg-eks-pipeline"
    and monitor_cw_stmt is not None
    and actions_of(monitor_cw_stmt) == {"cloudwatch:PutMetricData"}
    and (monitor_cw_stmt.get("Condition") or {}).get("StringEquals", {}).get("cloudwatch:namespace") == "GoldenGate/Pipelines",
)

# 6. Runtime and monitor policies remain distinct documents (never merged/
# aliased into each other).
check("6_roles_remain_separate", runtime_path != monitor_path and runtime_statements != monitor_statements)

# 9. No wildcard (Resource: "*") DynamoDB, CloudWatch, or Secrets Manager
# action exists in the runtime policy (the pre-existing KMS Decrypt
# Resource: "*" is a known, unchanged, intentionally broad grant -- not
# newly introduced by this phase -- so it is exempted here and covered by
# checks 3/4's byte-identical comparison instead).
wildcard_violations = []
for s in runtime_statements:
    if s.get("Resource") == "*":
        for a in actions_of(s):
            if a.startswith("dynamodb:") or a.startswith("cloudwatch:") or a.startswith("secretsmanager:"):
                wildcard_violations.append(a)
check("9_no_new_wildcard_access", not wildcard_violations)

print(f"RESULT={json.dumps(dict(results))}")
for label, ok in results:
    print(f"{'PASS' if ok else 'FAIL'} {label}")
PYEOF
  )"
  echo "$IAM_TEST_OUTPUT"

  if echo "$IAM_TEST_OUTPUT" | grep -q "^PASS 1_no_dynamodb_actions$"; then
    pass "1: runtime policy grants no dynamodb:* action (GetItem/Query/PutItem/UpdateItem/DescribeTable against gg-eks-pipeline removed)"
  else
    fail "1: runtime policy still grants a dynamodb:* action"
  fi

  if echo "$IAM_TEST_OUTPUT" | grep -q "^PASS 2_no_cloudwatch_actions$"; then
    pass "2: runtime policy grants no cloudwatch:PutMetricData (or any cloudwatch:*) action"
  else
    fail "2: runtime policy still grants a cloudwatch:* action"
  fi

  if echo "$IAM_TEST_OUTPUT" | grep -q "^PASS 3_secrets_manager_retained$"; then
    pass "3: runtime role retains its required Secrets Manager permissions, byte-identical to the original"
  else
    fail "3: runtime role's Secrets Manager permissions are missing or were altered"
  fi

  if echo "$IAM_TEST_OUTPUT" | grep -q "^PASS 4_kms_retained$"; then
    pass "4: runtime role retains its required KMS Decrypt permission, byte-identical to the original"
  else
    fail "4: runtime role's KMS Decrypt permission is missing or was altered"
  fi

  if echo "$IAM_TEST_OUTPUT" | grep -q "^PASS 5_monitor_dynamodb_and_metrics_retained$"; then
    pass "5: monitor role retains DynamoDB read/write (CONFIG reads, LEASE/STATE# writes) and PutMetricData scoped to GoldenGate/Pipelines"
  else
    fail "5: monitor role's DynamoDB or scoped CloudWatch permissions are missing or were altered"
  fi

  if echo "$IAM_TEST_OUTPUT" | grep -q "^PASS 6_roles_remain_separate$"; then
    pass "6: runtime and monitor IAM policies remain separate documents"
  else
    fail "6: runtime and monitor IAM policies are not distinct"
  fi

  if echo "$IAM_TEST_OUTPUT" | grep -q "^PASS 9_no_new_wildcard_access$"; then
    pass "9: no wildcard DynamoDB, CloudWatch, or Secrets Manager access exists in the runtime policy"
  else
    fail "9: a wildcard-resourced DynamoDB/CloudWatch/Secrets Manager action was found in the runtime policy"
  fi

  # 10. No statement was broadened to compensate: the runtime policy has
  # exactly the 2 retained statements, nothing more.
  RUNTIME_STMT_COUNT="$(python3 -c "import json; print(len((json.load(open('${RUNTIME_POLICY_FILE}')) or {}).get('Statement') or []))")"
  if [ "$RUNTIME_STMT_COUNT" = "2" ]; then
    pass "10: runtime policy has exactly 2 statements (no broadening or replacement compensation)"
  else
    fail "10: runtime policy has ${RUNTIME_STMT_COUNT} statements, expected exactly 2"
  fi
else
  skip "Phase 5B1 IAM least-privilege checks -- policy files or python3 not available"
fi

# 7. Canonical runtime ServiceAccounts still reference GoldenGateSecretsReadRole-dev.
RUNTIME_ROLE_REF_MISSING=""
for f in envs/dev/gg-oracle-payments-01/values.yaml envs/dev/gg-postgresql-payments-01/values.yaml; do
  grep -q "role/GoldenGateSecretsReadRole-dev" "$f" 2>/dev/null || RUNTIME_ROLE_REF_MISSING="${RUNTIME_ROLE_REF_MISSING} ${f}"
done
if [ -z "$RUNTIME_ROLE_REF_MISSING" ]; then
  pass "7: canonical runtime ServiceAccounts (Oracle, PostgreSQL) still reference GoldenGateSecretsReadRole-dev"
else
  fail "7: canonical runtime values file(s) no longer reference GoldenGateSecretsReadRole-dev:${RUNTIME_ROLE_REF_MISSING}"
fi

# 8. gg-monitor still references GoldenGateMonitorReadRole-dev.
if grep -q "role/GoldenGateMonitorReadRole-dev" "envs/dev/goldengate-monitor/values.yaml" 2>/dev/null; then
  pass "8: gg-monitor still references GoldenGateMonitorReadRole-dev"
else
  fail "8: envs/dev/goldengate-monitor/values.yaml no longer references GoldenGateMonitorReadRole-dev"
fi

# 11. Terraform references remain valid: iam.tf's module block still exists,
# still names the same role, and still attaches the same policy_folder.
if grep -q 'module "goldengate_secrets_read_role_dev"' envs/dev/iam.tf \
    && grep -q 'name          = "GoldenGateSecretsReadRole-dev"' envs/dev/iam.tf \
    && grep -q 'policy_folder = "goldengate-secrets-read-dev"' envs/dev/iam.tf \
    && grep -q 'module "goldengate_monitor_read_role_dev"' envs/dev/iam.tf \
    && grep -q 'name          = "GoldenGateMonitorReadRole-dev"' envs/dev/iam.tf \
    && grep -q 'policy_folder = "goldengate-monitor-read-dev"' envs/dev/iam.tf; then
  pass "11: envs/dev/iam.tf's module blocks still name the same roles and attach the same policy_folder values"
else
  fail "11: envs/dev/iam.tf's role/policy_folder identifiers appear to have changed"
fi

if command -v terraform >/dev/null 2>&1; then
  TF_FMT_OUTPUT="$(terraform fmt -check -recursive -diff envs/dev/ 2>&1 || true)"
  if [ -z "$TF_FMT_OUTPUT" ]; then
    pass "11b: terraform fmt -check -recursive reports no formatting differences"
  else
    fail "11b: terraform fmt -check -recursive found formatting differences"
    echo "$TF_FMT_OUTPUT"
  fi
else
  skip "terraform fmt -check -- terraform not available"
fi

# 12. No manager metric/DynamoDB/lease behavior changed: collector.py and
# monitor.py are untouched by this IAM-only phase.
IAM_PHASE_COLLECTOR_DIFF="$(git diff --stat -- monitoring/monitor/collector.py 2>/dev/null || true)"
IAM_PHASE_MONITOR_DIFF="$(git diff --stat -- monitoring/monitor/monitor.py 2>/dev/null || true)"
if [ -z "$IAM_PHASE_COLLECTOR_DIFF" ] && [ -z "$IAM_PHASE_MONITOR_DIFF" ]; then
  pass "12: collector.py and monitor.py are unchanged -- no manager metric/DynamoDB/lease behavior was altered"
else
  fail "12: collector.py and/or monitor.py were unexpectedly modified during an IAM-only phase"
fi

echo ""
echo "--- Phase 5A: stale ServiceManager.pid and Argo CD deletion safeguards preserved ---"

PID_GUARD_MISSING=""
for f in helm/goldengate/templates/source-statefulset.yaml helm/goldengate/templates/target-statefulset.yaml helm/goldengate/templates/runtime-statefulset.yaml; do
  [ -f "$f" ] || continue
  grep -q "ServiceManager.pid" "$f" || PID_GUARD_MISSING="${PID_GUARD_MISSING} ${f}"
done
if [ -z "$PID_GUARD_MISSING" ]; then
  pass "stale ServiceManager.pid cleanup remains present in every StatefulSet template"
else
  fail "stale ServiceManager.pid cleanup is missing from:${PID_GUARD_MISSING}"
fi

if grep -q "resources-finalizer.argocd.argoproj.io" "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -q "Refusing to delete namespace" "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -q "ownership labels" "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "Argo CD deletion safeguards (finalizer wait, shared-namespace refusal, ownership-label verification) remain present"
else
  fail "one or more Argo CD deletion safeguards appear to be missing from ${EKS_APP_WORKFLOW}"
fi

# ---------------------------------------------------------------------
# 24. No accidental pasted command-note files under hack/.
# ---------------------------------------------------------------------
echo ""
echo "--- No accidental command-note files under hack/ ---"

if [ -f "hack/test.yaml" ]; then
  fail "hack/test.yaml exists -- this was an accidental pasted VDR command note and is not a legitimate repository file"
else
  pass "hack/test.yaml does not exist"
fi

# Generic guard: any *.yaml/*.yml file anywhere under hack/ must actually
# parse as YAML -- a plain-prose/shell command note accidentally saved with
# a .yaml/.yml extension (exactly how hack/test.yaml happened) is caught
# here even if it is renamed or a new one is added later.
BAD_HACK_YAML=""
if command -v python3 >/dev/null 2>&1; then
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    if ! python3 -c "import sys, yaml; yaml.safe_load(open(sys.argv[1]))" "$f" >/dev/null 2>&1; then
      BAD_HACK_YAML="${BAD_HACK_YAML} ${f}"
    fi
  done <<< "$(find hack -type f \( -iname "*.yaml" -o -iname "*.yml" \) 2>/dev/null || true)"

  if [ -z "$BAD_HACK_YAML" ]; then
    pass "every *.yaml/*.yml file under hack/ parses as valid YAML (no pasted command notes)"
  else
    fail "file(s) under hack/ have a YAML extension but do not parse as YAML (likely an accidental command-note paste):${BAD_HACK_YAML}"
  fi
else
  skip "hack/ YAML-validity guard -- python3 not available"
fi

# ---------------------------------------------------------------------
# 25. Repository hygiene: proven-dead file cleanup regression checks.
# ---------------------------------------------------------------------
echo ""
echo "--- Repository hygiene: dead-file cleanup ---"

if [ -f ".github/workflows/build-monitor-base-image-once.yaml" ]; then
  fail "the temporary base-image workflow (build-monitor-base-image-once.yaml) still exists"
else
  pass "no temporary base-image workflow remains"
fi

JUNK_ARTIFACTS="$(find . -not -path "./.git/*" \( \
  -iname "__pycache__" -o -iname "*.pyc" -o -iname ".pytest_cache" -o -iname ".mypy_cache" \
  -o -iname ".DS_Store" -o -iname "Thumbs.db" -o -iname "*.tmp" -o -iname "*.bak" \
  -o -iname "*.orig" -o -iname "*.rej" -o -iname "*~" -o -iname "rendered" \
  \) 2>/dev/null || true)"
if [ -z "$JUNK_ARTIFACTS" ]; then
  pass "no Python cache, pytest/mypy cache, editor backup, or rendered/ artifacts exist in the repository"
else
  fail "junk/cache artifacts found:${JUNK_ARTIFACTS}"
fi

if [ -d "envs/dev/payments-ora-to-pg-001" ] && [ -f "envs/dev/payments-ora-to-pg-001/values.yaml" ]; then
  pass "the retired legacy values folder (envs/dev/payments-ora-to-pg-001) remains present"
else
  fail "the retired legacy values folder is missing -- it must be retained until Phase 5B"
fi

CANONICAL_PRESENCE_MISSING=""
for f in \
  envs/dev/gg-oracle-payments-01/values.yaml \
  envs/dev/gg-postgresql-payments-01/values.yaml \
  envs/dev/goldengate-monitor/values.yaml \
  helm/goldengate/templates/source-statefulset.yaml \
  helm/goldengate/templates/target-statefulset.yaml \
  helm/goldengate/templates/runtime-statefulset.yaml \
  helm/goldengate/templates/ingress.yaml \
  helm/goldengate/templates/namespace.yaml \
  helm/goldengate-monitor/templates/deployment.yaml \
  monitoring/monitor/monitor.py \
  monitoring/monitor/collector.py \
  monitoring/monitor/config.py \
  monitoring/monitor/health_rules.py \
  monitoring/monitor/tools/gg_api_contract_probe.py \
  monitoring/monitor/requirements-test.txt \
  ; do
  [ -e "$f" ] || CANONICAL_PRESENCE_MISSING="${CANONICAL_PRESENCE_MISSING} ${f}"
done
if [ -z "$CANONICAL_PRESENCE_MISSING" ]; then
  pass "all canonical runtime and monitor files remain present"
else
  fail "canonical runtime/monitor file(s) unexpectedly missing:${CANONICAL_PRESENCE_MISSING}"
fi

if [ -d "helm/argocd/charts/argo-cd" ] && [ -f "helm/argocd/Chart.lock" ]; then
  if [ "$HELM_AVAILABLE" = "true" ]; then
    if helm lint helm/argocd >"${WORKDIR}/argocd-lint.log" 2>&1 \
        && helm template argocd-hygiene-check helm/argocd --namespace argocd >"${WORKDIR}/argocd-template.log" 2>"${WORKDIR}/argocd-template.err"; then
      pass "the Argo CD vendored dependency (helm/argocd/charts/argo-cd) remains functional: helm lint and helm template both succeed"
    else
      fail "the Argo CD vendored dependency is present but helm lint/template failed"
      cat "${WORKDIR}/argocd-lint.log" "${WORKDIR}/argocd-template.err" 2>/dev/null
    fi
  else
    skip "Argo CD vendored dependency functional check -- helm not available"
  fi
else
  fail "the Argo CD vendored dependency directory or Chart.lock is missing -- helm/argocd/charts/argo-cd and helm/argocd/Chart.lock must be retained"
fi

if [ -f "helm/argocd/charts/argo-cd-9.3.7.tgz" ]; then
  echo "INFO: helm/argocd/charts/argo-cd-9.3.7.tgz is present (a redundant, Helm-regenerable duplicate of the vendored directory was removed when proven safe; its presence here is not itself a failure, only a note)."
fi

# ---------------------------------------------------------------------
# 26. EFS rendered-resource validation: strict basePath derivation
#     (matching goldengate.efsBasePath), fail-closed YAML parsing, no
#     fragile grep on an optional key under set -euo pipefail.
# ---------------------------------------------------------------------
echo ""
echo "--- EFS persistence validation: basePath derivation, strict parsing, StorageClass/PVC checks ---"

if [ "$HELM_AVAILABLE" = "true" ] && [ "$PYTHON_AVAILABLE" = "true" ]; then
  python3 - "$EKS_APP_WORKFLOW" > "${WORKDIR}/efs_validate.sh" <<'PYEOF'
import sys
import yaml
with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)
for step in doc["jobs"]["build_publish_and_deploy"]["steps"]:
    if step.get("name") == "Validate EFS persistence resources are rendered":
        sys.stdout.write(step["run"])
        break
else:
    sys.exit("step not found")
PYEOF

  if [ ! -s "${WORKDIR}/efs_validate.sh" ]; then
    fail "could not extract the 'Validate EFS persistence resources are rendered' step from ${EKS_APP_WORKFLOW}"
  else
    EFS_WORKDIR="${WORKDIR}/efs-test"
    mkdir -p "${EFS_WORKDIR}/rendered" "${EFS_WORKDIR}/values"

    run_efs_step() {
      ( cd "$EFS_WORKDIR" && \
        RELEASE_NAME="$1" VALUES_FILE="$2" DEPLOYMENT_ID="$3" DEPLOYMENT_MODEL="$4" ENVIRONMENT="$5" \
        bash "${WORKDIR}/efs_validate.sh" 2>&1 )
      return $?
    }

    helm template gg-oracle-payments-01 "$RUNTIME_CHART" --namespace goldengate-dev \
      --values "${REPO_ROOT}/envs/dev/gg-oracle-payments-01/values.yaml" \
      --set global.environment=dev --set global.deploymentId=gg-oracle-payments-01 \
      > "${EFS_WORKDIR}/rendered/gg-oracle-payments-01.yaml" 2>"${EFS_WORKDIR}/oracle-render.err" || true
    helm template gg-postgresql-payments-01 "$RUNTIME_CHART" --namespace goldengate-dev \
      --values "${REPO_ROOT}/envs/dev/gg-postgresql-payments-01/values.yaml" \
      --set global.environment=dev --set global.deploymentId=gg-postgresql-payments-01 \
      > "${EFS_WORKDIR}/rendered/gg-postgresql-payments-01.yaml" 2>"${EFS_WORKDIR}/postgres-render.err" || true
    helm template ogg-payments-ora-to-pg-001 "$RUNTIME_CHART" --namespace gg-dev-payments-ora-to-pg-001 \
      --values "${REPO_ROOT}/envs/dev/payments-ora-to-pg-001/values.yaml" \
      --set global.environment=dev --set global.deploymentId=payments-ora-to-pg-001 \
      > "${EFS_WORKDIR}/rendered/ogg-payments-ora-to-pg-001.yaml" 2>"${EFS_WORKDIR}/legacy-render.err" || true

    set +e
    ORACLE_OUT="$(run_efs_step "gg-oracle-payments-01" "${REPO_ROOT}/envs/dev/gg-oracle-payments-01/values.yaml" "gg-oracle-payments-01" "singleRuntime" "dev")"
    ORACLE_STATUS=$?
    set -e
    echo "$ORACLE_OUT"
    if [ "$ORACLE_STATUS" -eq 0 ] && echo "$ORACLE_OUT" | grep -qF "Expected EFS basePath: /gg-oracle-payments-01"; then
      pass "1: gg-oracle-payments-01 (no explicit basePath) resolves to /gg-oracle-payments-01"
    else
      fail "1: gg-oracle-payments-01 basePath derivation failed or produced an unexpected value"
    fi

    set +e
    POSTGRES_OUT="$(run_efs_step "gg-postgresql-payments-01" "${REPO_ROOT}/envs/dev/gg-postgresql-payments-01/values.yaml" "gg-postgresql-payments-01" "singleRuntime" "dev")"
    POSTGRES_STATUS=$?
    set -e
    echo "$POSTGRES_OUT"
    if [ "$POSTGRES_STATUS" -eq 0 ] && echo "$POSTGRES_OUT" | grep -qF "Expected EFS basePath: /gg-postgresql-payments-01"; then
      pass "2: gg-postgresql-payments-01 (no explicit basePath) resolves to /gg-postgresql-payments-01"
    else
      fail "2: gg-postgresql-payments-01 basePath derivation failed or produced an unexpected value"
    fi

    if [ "$ORACLE_STATUS" -eq 0 ] && [ "$POSTGRES_STATUS" -eq 0 ] \
        && echo "$ORACLE_OUT" | grep -qF "OK: EFS StorageClass, runtime PVC, and StatefulSet u02/u03" \
        && echo "$POSTGRES_OUT" | grep -qF "OK: EFS StorageClass, runtime PVC, and StatefulSet u02/u03"; then
      pass "3: both actual rendered manifests (Oracle and PostgreSQL) pass full EFS validation"
    else
      fail "3: one or both actual rendered manifests failed EFS validation"
    fi

    # 4: an explicit non-empty basePath override is honored.
    python3 -c "
import yaml
with open('${REPO_ROOT}/envs/dev/gg-oracle-payments-01/values.yaml') as f:
    data = yaml.safe_load(f)
data['persistence']['efs']['storageClass']['basePath'] = '/custom-override-path'
with open('${EFS_WORKDIR}/values/oracle-override.yaml', 'w') as f:
    yaml.dump(data, f)
"
    helm template gg-oracle-payments-01 "$RUNTIME_CHART" --namespace goldengate-dev \
      --values "${EFS_WORKDIR}/values/oracle-override.yaml" \
      --set global.environment=dev --set global.deploymentId=gg-oracle-payments-01 \
      > "${EFS_WORKDIR}/rendered/oracle-override.yaml" 2>"${EFS_WORKDIR}/override-render.err" || true

    set +e
    OVERRIDE_OUT="$(run_efs_step "oracle-override" "${EFS_WORKDIR}/values/oracle-override.yaml" "gg-oracle-payments-01" "singleRuntime" "dev")"
    OVERRIDE_STATUS=$?
    set -e
    if [ "$OVERRIDE_STATUS" -eq 0 ] && echo "$OVERRIDE_OUT" | grep -qF "Expected EFS basePath: /custom-override-path"; then
      pass "4: an explicit non-empty basePath override is honored"
    else
      fail "4: explicit basePath override was not honored"
      echo "$OVERRIDE_OUT"
    fi

    # 5: a missing fileSystemId fails with a clear controlled error (never
    # an unexplained shell abort).
    cat > "${EFS_WORKDIR}/values/missing-fsid.yaml" <<'EOF'
deploymentModel: singleRuntime
persistence:
  enabled: true
  provider: efs
  efs:
    storageClass:
      basePath: /x
EOF
    set +e
    MISSING_FSID_OUT="$(run_efs_step "x" "${EFS_WORKDIR}/values/missing-fsid.yaml" "gg-oracle-payments-01" "singleRuntime" "dev")"
    MISSING_FSID_STATUS=$?
    set -e
    if [ "$MISSING_FSID_STATUS" -ne 0 ] && echo "$MISSING_FSID_OUT" | grep -qF "persistence.efs.fileSystemId must be a non-empty string"; then
      pass "5: a missing fileSystemId fails with a clear controlled error"
    else
      fail "5: a missing fileSystemId did not fail with the expected controlled error"
      echo "$MISSING_FSID_OUT"
    fi

    # 6: malformed YAML fails closed.
    cat > "${EFS_WORKDIR}/values/malformed.yaml" <<'EOF'
deploymentModel: singleRuntime
persistence:
  enabled: true
  provider: efs
  efs:
    fileSystemId: fs-x
  bad indent: [unterminated
EOF
    set +e
    MALFORMED_OUT="$(run_efs_step "x" "${EFS_WORKDIR}/values/malformed.yaml" "gg-oracle-payments-01" "singleRuntime" "dev")"
    MALFORMED_STATUS=$?
    set -e
    if [ "$MALFORMED_STATUS" -ne 0 ] && echo "$MALFORMED_OUT" | grep -qF "is not valid YAML"; then
      pass "6: malformed YAML fails closed"
    else
      fail "6: malformed YAML did not fail closed as expected"
      echo "$MALFORMED_OUT"
    fi

    # 7: an unknown deploymentModel fails closed.
    cat > "${EFS_WORKDIR}/values/unknown-model.yaml" <<'EOF'
deploymentModel: someWeirdModel
persistence:
  enabled: true
  provider: efs
  efs:
    fileSystemId: fs-x
EOF
    set +e
    UNKNOWN_MODEL_OUT="$(run_efs_step "x" "${EFS_WORKDIR}/values/unknown-model.yaml" "gg-oracle-payments-01" "someWeirdModel" "dev")"
    UNKNOWN_MODEL_STATUS=$?
    set -e
    if [ "$UNKNOWN_MODEL_STATUS" -ne 0 ] && echo "$UNKNOWN_MODEL_OUT" | grep -qF "unknown deploymentModel"; then
      pass "7: an unknown deploymentModel fails closed"
    else
      fail "7: an unknown deploymentModel did not fail closed as expected"
      echo "$UNKNOWN_MODEL_OUT"
    fi

    # 8/9/11: mutate a real rendered manifest's StorageClass to prove the
    # rendered-resource checks have teeth (wrong basePath, wrong
    # fileSystemId, and a duplicate matching-name StorageClass).
    python3 -c "
import yaml
with open('${EFS_WORKDIR}/rendered/gg-oracle-payments-01.yaml') as f:
    docs = list(yaml.safe_load_all(f))
out = []
for d in docs:
    if d and d.get('kind') == 'StorageClass':
        d['parameters']['basePath'] = '/wrong-base-path'
    out.append(d)
with open('${EFS_WORKDIR}/rendered/wrong-basepath.yaml', 'w') as f:
    yaml.dump_all(out, f)
"
    set +e
    WRONG_BASEPATH_OUT="$(run_efs_step "wrong-basepath" "${REPO_ROOT}/envs/dev/gg-oracle-payments-01/values.yaml" "gg-oracle-payments-01" "singleRuntime" "dev")"
    WRONG_BASEPATH_STATUS=$?
    set -e
    if [ "$WRONG_BASEPATH_STATUS" -ne 0 ] && echo "$WRONG_BASEPATH_OUT" | grep -qF "parameters.basePath"; then
      pass "8: a rendered StorageClass with the wrong basePath fails"
    else
      fail "8: a rendered StorageClass with the wrong basePath did not fail as expected"
      echo "$WRONG_BASEPATH_OUT"
    fi

    python3 -c "
import yaml
with open('${EFS_WORKDIR}/rendered/gg-oracle-payments-01.yaml') as f:
    docs = list(yaml.safe_load_all(f))
out = []
for d in docs:
    if d and d.get('kind') == 'StorageClass':
        d['parameters']['fileSystemId'] = 'fs-wrongwrongwrong'
    out.append(d)
with open('${EFS_WORKDIR}/rendered/wrong-fsid.yaml', 'w') as f:
    yaml.dump_all(out, f)
"
    set +e
    WRONG_FSID_OUT="$(run_efs_step "wrong-fsid" "${REPO_ROOT}/envs/dev/gg-oracle-payments-01/values.yaml" "gg-oracle-payments-01" "singleRuntime" "dev")"
    WRONG_FSID_STATUS=$?
    set -e
    if [ "$WRONG_FSID_STATUS" -ne 0 ] && echo "$WRONG_FSID_OUT" | grep -qF "parameters.fileSystemId"; then
      pass "9: a rendered StorageClass with the wrong filesystem ID fails"
    else
      fail "9: a rendered StorageClass with the wrong filesystem ID did not fail as expected"
      echo "$WRONG_FSID_OUT"
    fi

    # 10: absence of the optional basePath key never causes an unexplained
    # shell exit -- structural proof (the fragile grep pattern is gone) plus
    # behavioral proof (tests 1/2 above already completed with a clean
    # PASS/FAIL verdict, not a raw "unbound variable"/pipefail abort).
    if grep -qE "grep.*basePath" "${WORKDIR}/efs_validate.sh"; then
      fail "10: the EFS validation step still greps for basePath in the values file -- the fragile fallback was not removed"
    else
      pass "10: the EFS validation step no longer greps for the optional basePath key (no set -e/pipefail exposure)"
    fi

    python3 -c "
import yaml
with open('${EFS_WORKDIR}/rendered/gg-oracle-payments-01.yaml') as f:
    docs = list(yaml.safe_load_all(f))
out = list(docs)
for d in docs:
    if d and d.get('kind') == 'StorageClass':
        out.append(dict(d))
        break
with open('${EFS_WORKDIR}/rendered/duplicate-storageclass.yaml', 'w') as f:
    yaml.dump_all(out, f)
"
    set +e
    DUP_SC_OUT="$(run_efs_step "duplicate-storageclass" "${REPO_ROOT}/envs/dev/gg-oracle-payments-01/values.yaml" "gg-oracle-payments-01" "singleRuntime" "dev")"
    DUP_SC_STATUS=$?
    set -e
    if [ "$DUP_SC_STATUS" -ne 0 ] && echo "$DUP_SC_OUT" | grep -qF "expected exactly one StorageClass"; then
      pass "11: exactly one expected StorageClass is required (a duplicate is rejected)"
    else
      fail "11: a duplicate matching-name StorageClass was not rejected as expected"
      echo "$DUP_SC_OUT"
    fi

    # 12: legacyPair source/target PVC and StatefulSet u02/u03 validation
    # continues passing (existing behavior, not weakened).
    set +e
    LEGACY_OUT="$(run_efs_step "ogg-payments-ora-to-pg-001" "${REPO_ROOT}/envs/dev/payments-ora-to-pg-001/values.yaml" "payments-ora-to-pg-001" "legacyPair" "dev")"
    LEGACY_STATUS=$?
    set -e
    if [ "$LEGACY_STATUS" -eq 0 ] && echo "$LEGACY_OUT" | grep -qF "OK: EFS StorageClass, source/target PVCs, and StatefulSet u02/u03"; then
      pass "12: legacyPair source/target PVC and StatefulSet u02/u03 validation continues passing"
    else
      fail "12: legacyPair EFS validation regressed"
      echo "$LEGACY_OUT"
    fi

    # 13: this EFS-only correction did not touch observer removal or the
    # workflow-matrix classifier logic elsewhere in the same file.
    PHASE5A_SPOTCHECK_OK="true"
    if grep -q "^  ensure_observer_image:" "$EKS_APP_WORKFLOW"; then
      PHASE5A_SPOTCHECK_OK="false"
    fi
    if ! grep -q "is_goldengate_deployment_values_file() {" "$EKS_APP_WORKFLOW"; then
      PHASE5A_SPOTCHECK_OK="false"
    fi
    if grep -q "LEGACY_FALLBACK_ENABLED" "helm/goldengate-monitor/templates/deployment.yaml" 2>/dev/null; then
      PHASE5A_SPOTCHECK_OK="false"
    fi
    if [ "$PHASE5A_SPOTCHECK_OK" = "true" ]; then
      pass "13: this EFS-only correction did not reintroduce observer/legacy-fallback logic or remove the workflow-matrix classifier"
    else
      fail "13: unexpected Phase 5A regression detected alongside the EFS correction"
    fi

    rm -rf "$EFS_WORKDIR"
  fi
else
  skip "EFS persistence validation regression tests -- helm and/or python3/PyYAML not available"
fi

echo ""
echo "=================================================="
echo "Summary: ${PASS_COUNT} passed, ${FAIL_COUNT} failed, ${SKIP_COUNT} skipped"
echo "=================================================="

if [ "$FAIL_COUNT" -gt 0 ]; then
  exit 1
fi
