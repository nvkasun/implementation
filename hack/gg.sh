#!/usr/bin/env bash
# Phase 5B recovery validation after recreating canonical Argo CD Applications.
# Read-only: no create/update/delete/apply/patch operations.

set -u

RUNTIME_NS="goldengate-dev"
MONITOR_NS="goldengate-monitoring"
ARGO_NS="argocd"

ORACLE_APP="goldengate-dev-oracle-payments-01"
POSTGRES_APP="goldengate-dev-postgresql-payments-01"
PLATFORM_APP="goldengate-dev-platform"
MONITOR_APP="goldengate-monitor"

ORACLE_STS="gg-oracle-payments-01"
POSTGRES_STS="gg-postgresql-payments-01"
MONITOR_DEPLOY="gg-monitor"

FAILURES=0

section() {
  printf '\n============================================================\n'
  printf '%s\n' "$1"
  printf '============================================================\n'
}

pass() { printf 'PASS: %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; FAILURES=$((FAILURES + 1)); }

check_app() {
  local app="$1"
  local status
  status="$(
    kubectl get application "$app" -n "$ARGO_NS" \
      -o jsonpath='{.status.sync.status}{"|"}{.status.health.status}' \
      2>/dev/null || true
  )"
  printf '%s: %s\n' "$app" "${status:-NOT_FOUND}"
  if [ "$status" = "Synced|Healthy" ]; then
    pass "$app is Synced and Healthy"
  else
    fail "$app is not Synced and Healthy"
  fi
}

check_rollout() {
  local resource="$1"
  local namespace="$2"
  if kubectl rollout status "$resource" -n "$namespace" --timeout=5m; then
    pass "$resource rollout completed"
  else
    fail "$resource rollout failed or timed out"
  fi
}

section "1. ARGO CD APPLICATION STATUS"

for app in \
  "$ORACLE_APP" \
  "$POSTGRES_APP" \
  "$PLATFORM_APP" \
  "$MONITOR_APP"
do
  check_app "$app"
done

if kubectl get application goldengate-payments-ora-to-pg-001 \
    -n "$ARGO_NS" >/dev/null 2>&1; then
  fail "Retired legacy Argo CD Application still exists"
else
  pass "Retired legacy Argo CD Application is absent"
fi

section "2. RUNTIME AND MONITOR ROLLOUTS"

check_rollout "statefulset/${ORACLE_STS}" "$RUNTIME_NS"
check_rollout "statefulset/${POSTGRES_STS}" "$RUNTIME_NS"
check_rollout "deployment/${MONITOR_DEPLOY}" "$MONITOR_NS"

section "3. CANONICAL POD MODEL"

kubectl get pods -n "$RUNTIME_NS" \
  -o custom-columns='POD:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[*].ready,CONTAINERS:.spec.containers[*].name,INIT:.spec.initContainers[*].name,IMAGES:.spec.containers[*].image'

ORACLE_CONTAINERS="$(
  kubectl get statefulset "$ORACLE_STS" -n "$RUNTIME_NS" \
    -o jsonpath='{.spec.template.spec.containers[*].name}' 2>/dev/null || true
)"
POSTGRES_CONTAINERS="$(
  kubectl get statefulset "$POSTGRES_STS" -n "$RUNTIME_NS" \
    -o jsonpath='{.spec.template.spec.containers[*].name}' 2>/dev/null || true
)"
ORACLE_INIT="$(
  kubectl get statefulset "$ORACLE_STS" -n "$RUNTIME_NS" \
    -o jsonpath='{.spec.template.spec.initContainers[*].name}' 2>/dev/null || true
)"
POSTGRES_INIT="$(
  kubectl get statefulset "$POSTGRES_STS" -n "$RUNTIME_NS" \
    -o jsonpath='{.spec.template.spec.initContainers[*].name}' 2>/dev/null || true
)"

[ "$ORACLE_CONTAINERS" = "ogg-oracle" ] \
  && pass "Oracle has exactly one GoldenGate application container" \
  || fail "Unexpected Oracle containers: ${ORACLE_CONTAINERS:-<none>}"

[ "$POSTGRES_CONTAINERS" = "ogg-postgresql" ] \
  && pass "PostgreSQL has exactly one GoldenGate application container" \
  || fail "Unexpected PostgreSQL containers: ${POSTGRES_CONTAINERS:-<none>}"

[ "$ORACLE_INIT" = "prepare-u02-permissions" ] \
  && pass "Oracle has the expected init container" \
  || fail "Unexpected Oracle init containers: ${ORACLE_INIT:-<none>}"

[ "$POSTGRES_INIT" = "prepare-u02-permissions" ] \
  && pass "PostgreSQL has the expected init container" \
  || fail "Unexpected PostgreSQL init containers: ${POSTGRES_INIT:-<none>}"

OBSERVER_ROWS="$(
  kubectl get pods -A \
    -o jsonpath='{range .items[*]}{.metadata.namespace}{"|"}{.metadata.name}{"|"}{.spec.containers[*].name}{"\n"}{end}' \
    2>/dev/null |
  grep -Ei 'goldengate-observer|observer-sidecar' || true
)"

if [ -z "$OBSERVER_ROWS" ]; then
  pass "No observer sidecar exists in any live pod"
else
  fail "Observer sidecar still exists in live pods"
  printf '%s\n' "$OBSERVER_ROWS"
fi

if kubectl get namespace gg-dev-payments-ora-to-pg-001 >/dev/null 2>&1; then
  fail "Retired legacy namespace still exists"
else
  pass "Retired legacy namespace is absent"
fi

section "4. SERVICES"

kubectl get services -n "$RUNTIME_NS" -o wide

for instance in "$ORACLE_STS" "$POSTGRES_STS"; do
  SERVICES="$(
    kubectl get services -n "$RUNTIME_NS" \
      -l "app.kubernetes.io/instance=${instance}" \
      -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.spec.clusterIP}{"|"}{.spec.type}{"\n"}{end}' \
      2>/dev/null || true
  )"

  NORMAL_COUNT="$(
    printf '%s\n' "$SERVICES" |
      awk -F'|' 'NF>=3 && $2!="None" && ($3=="ClusterIP" || $3=="") {n++} END {print n+0}'
  )"
  HEADLESS_COUNT="$(
    printf '%s\n' "$SERVICES" |
      awk -F'|' 'NF>=3 && $2=="None" {n++} END {print n+0}'
  )"

  if [ "$NORMAL_COUNT" -eq 1 ] && [ "$HEADLESS_COUNT" -eq 1 ]; then
    pass "$instance has exactly one normal and one headless Service"
  else
    fail "$instance Service contract invalid: normal=${NORMAL_COUNT}, headless=${HEADLESS_COUNT}"
  fi
done

section "5. PVC AND PV BINDINGS"

kubectl get pvc -n "$RUNTIME_NS" \
  -o custom-columns='PVC:.metadata.name,STATUS:.status.phase,VOLUME:.spec.volumeName,STORAGECLASS:.spec.storageClassName,CAPACITY:.status.capacity.storage,ACCESS_MODES:.spec.accessModes'

for pvc in \
  gg-oracle-payments-01-u02 \
  gg-postgresql-payments-01-u02
do
  STATUS="$(
    kubectl get pvc "$pvc" -n "$RUNTIME_NS" \
      -o jsonpath='{.status.phase}' 2>/dev/null || true
  )"
  PV="$(
    kubectl get pvc "$pvc" -n "$RUNTIME_NS" \
      -o jsonpath='{.spec.volumeName}' 2>/dev/null || true
  )"

  if [ "$STATUS" = "Bound" ] && [ -n "$PV" ]; then
    pass "$pvc is Bound to $PV"
  else
    fail "$pvc is not Bound"
  fi
done

echo
echo "--- All GoldenGate-related PVs ---"

kubectl get pv -o json |
python3 -c '
import json, sys
data = json.load(sys.stdin)
print("PV|STATUS|RECLAIM|STORAGECLASS|CLAIM_NAMESPACE|CLAIM_NAME|VOLUME_HANDLE")
for item in data.get("items", []):
    spec = item.get("spec", {})
    claim = spec.get("claimRef", {})
    sc = spec.get("storageClassName", "")
    ns = claim.get("namespace", "")
    if "gg-efs" in sc or "goldengate" in ns or ns.startswith("gg-dev-"):
        print("|".join([
            item["metadata"]["name"],
            item.get("status", {}).get("phase", ""),
            spec.get("persistentVolumeReclaimPolicy", ""),
            sc,
            ns,
            claim.get("name", ""),
            spec.get("csi", {}).get("volumeHandle", ""),
        ]))
'

section "6. MONITOR DEPLOYMENT CONFIGURATION"

MONITOR_IMAGE="$(
  kubectl get deployment "$MONITOR_DEPLOY" -n "$MONITOR_NS" \
    -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true
)"
CW_SWITCH="$(
  kubectl get deployment "$MONITOR_DEPLOY" -n "$MONITOR_NS" \
    -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="CLOUDWATCH_PUBLISH_ENABLED")].value}' \
    2>/dev/null || true
)"
LEGACY_SWITCH="$(
  kubectl get deployment "$MONITOR_DEPLOY" -n "$MONITOR_NS" \
    -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="LEGACY_FALLBACK_ENABLED")].value}' \
    2>/dev/null || true
)"

printf 'Monitor image: %s\n' "${MONITOR_IMAGE:-<missing>}"
printf 'CLOUDWATCH_PUBLISH_ENABLED=%s\n' "${CW_SWITCH:-<missing>}"
printf 'LEGACY_FALLBACK_ENABLED=%s\n' "${LEGACY_SWITCH:-<absent>}"

[ "$CW_SWITCH" = "true" ] \
  && pass "CloudWatch publication remains enabled" \
  || fail "CLOUDWATCH_PUBLISH_ENABLED is not true"

[ -z "$LEGACY_SWITCH" ] \
  && pass "Legacy fallback switch is absent" \
  || fail "LEGACY_FALLBACK_ENABLED still exists"

section "7. WAIT FOR CANONICAL MONITOR RECOVERY"

MONITOR_POD="$(
  kubectl get pods -n "$MONITOR_NS" \
    -l app.kubernetes.io/name=gg-monitor \
    --field-selector=status.phase=Running \
    --sort-by=.metadata.creationTimestamp \
    -o name 2>/dev/null |
  tail -n1 |
  sed 's#^pod/##'
)"

if [ -z "$MONITOR_POD" ]; then
  fail "No Running gg-monitor pod found"
else
  printf 'Selected monitor pod: %s\n' "$MONITOR_POD"

  if kubectl exec -i -n "$MONITOR_NS" "$MONITOR_POD" -- python3 - <<'PY'
import json
import time
import urllib.request

expected = {
    "gg-oracle-payments-01": ("oracle", "source"),
    "gg-postgresql-payments-01": ("postgresql", "target"),
}

base = "http://127.0.0.1:8080"
last = {}

for attempt in range(1, 31):
    with urllib.request.urlopen(base + "/api/status", timeout=10) as response:
        payload = json.load(response)

    seen = {}
    for logical in payload.get("logicalPipelines", []):
        for runtime in logical.get("runtimes", []):
            name = runtime.get("deploymentName")
            if name in expected:
                seen[name] = runtime

    summary = {
        name: {
            "status": value.get("effectiveStatus"),
            "fresh": value.get("fresh"),
            "source": value.get("dataSource"),
            "metricsEnabled": value.get("metricsEnabled"),
            "alertsEnabled": value.get("alertsEnabled"),
            "leaseHolder": (value.get("lease") or {}).get("holder"),
            "services": value.get("criticalServices"),
        }
        for name, value in seen.items()
    }
    print(f"Attempt {attempt}: {json.dumps(summary, sort_keys=True)}")
    last = seen

    healthy = set(seen) == set(expected)
    if healthy:
        for name, value in seen.items():
            expected_type, expected_role = expected[name]
            healthy = healthy and value.get("deploymentType") == expected_type
            healthy = healthy and value.get("role") == expected_role
            healthy = healthy and value.get("effectiveStatus") == "UP"
            healthy = healthy and value.get("fresh") is True
            healthy = healthy and value.get("dataSource") == "canonical-monitor"
            healthy = healthy and value.get("metricsEnabled") is True
            healthy = healthy and value.get("alertsEnabled") is False
            healthy = healthy and all(
                (value.get("criticalServices") or {}).get(service) is True
                for service in ("adminsrvr", "distsrvr", "recvsrvr")
            )

    if healthy:
        print("MONITOR RECOVERY PASSED")
        raise SystemExit(0)

    time.sleep(10)

print("Last observed:", json.dumps(last, sort_keys=True))
raise SystemExit("Monitor did not recover both canonical deployments within five minutes")
PY
  then
    pass "Shared monitor recovered both canonical runtimes"
  else
    fail "Shared monitor did not recover both canonical runtimes"
  fi
fi

section "8. MONITOR ERROR CHECK"

ERRORS="$(
  kubectl logs -n "$MONITOR_NS" deployment/"$MONITOR_DEPLOY" \
    --since=15m 2>&1 |
  grep -E \
    'cloudwatch_client_creation_failed|cloudwatch_put_metric_data_failed|tick failed|AccessDenied|legacy-observer-fallback' \
  || true
)"

if [ -z "$ERRORS" ]; then
  pass "No monitor publication, IAM or legacy-fallback errors found"
else
  fail "Monitor errors were found"
  printf '%s\n' "$ERRORS"
fi

section "9. FINAL RESULT"

if [ "$FAILURES" -eq 0 ]; then
  printf '\nCANONICAL APPLICATION REDEPLOYMENT VALIDATION PASSED\n'
  printf 'LEGACY OBSERVER RUNTIME IS RETIRED\n'
  printf 'PHASE 5B1 IAM APPLY CAN PROCEED\n'
  exit 0
fi

printf '\nVALIDATION COMPLETED WITH %s FAILURE(S)\n' "$FAILURES"
printf 'DO NOT RUN THE IAM WORKFLOW YET\n'
exit 1