set -u

RUNTIME_NS="goldengate-dev"
MONITOR_NS="goldengate-monitoring"
ARGO_NS="argocd"

FAILURES=0

section() {
  echo
  echo "============================================================"
  echo "$1"
  echo "============================================================"
}

pass() {
  echo "PASS: $*"
}

fail() {
  echo "FAIL: $*" >&2
  FAILURES=$((FAILURES + 1))
}

check_app() {
  APP="$1"

  STATUS="$(
    kubectl get application "$APP" \
      -n "$ARGO_NS" \
      -o jsonpath='{.status.sync.status}{"|"}{.status.health.status}' \
      2>/dev/null || true
  )"

  echo "${APP}: ${STATUS:-NOT_FOUND}"

  if [ "$STATUS" = "Synced|Healthy" ]; then
    pass "${APP} is Synced and Healthy"
  else
    fail "${APP} is not Synced and Healthy"
  fi
}

section "1. ARGO CD APPLICATION STATUS"

for APP in \
  goldengate-dev-oracle-payments-01 \
  goldengate-dev-postgresql-payments-01 \
  goldengate-dev-platform \
  goldengate-monitor
do
  check_app "$APP"
done

if kubectl get application goldengate-payments-ora-to-pg-001 \
    -n "$ARGO_NS" >/dev/null 2>&1; then
  fail "Retired legacy Argo CD Application still exists"
else
  pass "Retired legacy Argo CD Application is absent"
fi

section "2. LEGACY NAMESPACE STATUS"

if kubectl get namespace gg-dev-payments-ora-to-pg-001 \
    >/dev/null 2>&1; then
  fail "Retired legacy namespace still exists"
else
  pass "Retired legacy namespace is absent"
fi

section "3. CANONICAL RUNTIME ROLLOUTS"

if kubectl rollout status \
    statefulset/gg-oracle-payments-01 \
    -n "$RUNTIME_NS" \
    --timeout=5m; then
  pass "Oracle StatefulSet rollout completed"
else
  fail "Oracle StatefulSet rollout failed"
fi

if kubectl rollout status \
    statefulset/gg-postgresql-payments-01 \
    -n "$RUNTIME_NS" \
    --timeout=5m; then
  pass "PostgreSQL StatefulSet rollout completed"
else
  fail "PostgreSQL StatefulSet rollout failed"
fi

if kubectl rollout status \
    deployment/gg-monitor \
    -n "$MONITOR_NS" \
    --timeout=5m; then
  pass "Shared monitor rollout completed"
else
  fail "Shared monitor rollout failed"
fi

section "4. CANONICAL POD MODEL"

kubectl get pods \
  -n "$RUNTIME_NS" \
  -o custom-columns='POD:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[*].ready,CONTAINERS:.spec.containers[*].name,INIT:.spec.initContainers[*].name,IMAGE:.spec.containers[*].image'

ORACLE_CONTAINERS="$(
  kubectl get statefulset gg-oracle-payments-01 \
    -n "$RUNTIME_NS" \
    -o jsonpath='{.spec.template.spec.containers[*].name}' \
    2>/dev/null || true
)"

POSTGRES_CONTAINERS="$(
  kubectl get statefulset gg-postgresql-payments-01 \
    -n "$RUNTIME_NS" \
    -o jsonpath='{.spec.template.spec.containers[*].name}' \
    2>/dev/null || true
)"

ORACLE_INIT="$(
  kubectl get statefulset gg-oracle-payments-01 \
    -n "$RUNTIME_NS" \
    -o jsonpath='{.spec.template.spec.initContainers[*].name}' \
    2>/dev/null || true
)"

POSTGRES_INIT="$(
  kubectl get statefulset gg-postgresql-payments-01 \
    -n "$RUNTIME_NS" \
    -o jsonpath='{.spec.template.spec.initContainers[*].name}' \
    2>/dev/null || true
)"

[ "$ORACLE_CONTAINERS" = "ogg-oracle" ] \
  && pass "Oracle has exactly one application container" \
  || fail "Unexpected Oracle containers: ${ORACLE_CONTAINERS:-<none>}"

[ "$POSTGRES_CONTAINERS" = "ogg-postgresql" ] \
  && pass "PostgreSQL has exactly one application container" \
  || fail "Unexpected PostgreSQL containers: ${POSTGRES_CONTAINERS:-<none>}"

[ "$ORACLE_INIT" = "prepare-u02-permissions" ] \
  && pass "Oracle retains prepare-u02-permissions" \
  || fail "Unexpected Oracle init containers: ${ORACLE_INIT:-<none>}"

[ "$POSTGRES_INIT" = "prepare-u02-permissions" ] \
  && pass "PostgreSQL retains prepare-u02-permissions" \
  || fail "Unexpected PostgreSQL init containers: ${POSTGRES_INIT:-<none>}"

OBSERVER_ROWS="$(
  kubectl get pods -A \
    -o jsonpath='{range .items[*]}{.metadata.namespace}{"|"}{.metadata.name}{"|"}{.spec.containers[*].name}{"\n"}{end}' \
    2>/dev/null |
  grep -Ei 'goldengate-observer|observer-sidecar' || true
)"

if [ -z "$OBSERVER_ROWS" ]; then
  pass "No observer container exists in any live pod"
else
  fail "Observer container still exists"
  echo "$OBSERVER_ROWS"
fi

section "5. SERVICE CONTRACT"

kubectl get services -n "$RUNTIME_NS" -o wide

for DEPLOYMENT in \
  gg-oracle-payments-01 \
  gg-postgresql-payments-01
do
  SERVICES="$(
    kubectl get services \
      -n "$RUNTIME_NS" \
      -l "app.kubernetes.io/instance=${DEPLOYMENT}" \
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

  if [ "$NORMAL_COUNT" -eq 1 ] &&
     [ "$HEADLESS_COUNT" -eq 1 ]; then
    pass "${DEPLOYMENT} has one normal and one headless Service"
  else
    fail "${DEPLOYMENT} Service contract invalid: normal=${NORMAL_COUNT}, headless=${HEADLESS_COUNT}"
  fi
done

section "6. CURRENT PVC BINDINGS"

kubectl get pvc \
  -n "$RUNTIME_NS" \
  -o custom-columns='PVC:.metadata.name,STATUS:.status.phase,VOLUME:.spec.volumeName,STORAGECLASS:.spec.storageClassName,CAPACITY:.status.capacity.storage,ACCESS_MODES:.spec.accessModes'

for PVC in \
  gg-oracle-payments-01-u02 \
  gg-postgresql-payments-01-u02
do
  STATUS="$(
    kubectl get pvc "$PVC" \
      -n "$RUNTIME_NS" \
      -o jsonpath='{.status.phase}' \
      2>/dev/null || true
  )"

  VOLUME="$(
    kubectl get pvc "$PVC" \
      -n "$RUNTIME_NS" \
      -o jsonpath='{.spec.volumeName}' \
      2>/dev/null || true
  )"

  if [ "$STATUS" = "Bound" ] &&
     [ -n "$VOLUME" ]; then
    pass "${PVC} is Bound to ${VOLUME}"
  else
    fail "${PVC} is not Bound"
  fi
done

section "7. OLD RETAINED PV INVENTORY"

kubectl get pv \
  pvc-3a93c990-a9fa-4cca-99df-7c3375472074 \
  pvc-93251c3f-c408-4713-bd46-ebc5e0eafa8a \
  pvc-5c43940e-1054-43f5-8031-9db4b51a024a \
  pvc-bacb3e9d-d904-467c-959f-dea9548699c9 \
  -o custom-columns='PV:.metadata.name,STATUS:.status.phase,RECLAIM:.spec.persistentVolumeReclaimPolicy,STORAGECLASS:.spec.storageClassName,CLAIM_NAMESPACE:.spec.claimRef.namespace,CLAIM_NAME:.spec.claimRef.name,VOLUME_HANDLE:.spec.csi.volumeHandle' \
  2>&1

section "8. SHARED MONITOR CONFIGURATION"

CW_SWITCH="$(
  kubectl get deployment gg-monitor \
    -n "$MONITOR_NS" \
    -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="CLOUDWATCH_PUBLISH_ENABLED")].value}' \
    2>/dev/null || true
)"

LEGACY_SWITCH="$(
  kubectl get deployment gg-monitor \
    -n "$MONITOR_NS" \
    -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="LEGACY_FALLBACK_ENABLED")].value}' \
    2>/dev/null || true
)"

echo "CLOUDWATCH_PUBLISH_ENABLED=${CW_SWITCH:-<missing>}"
echo "LEGACY_FALLBACK_ENABLED=${LEGACY_SWITCH:-<absent>}"

[ "$CW_SWITCH" = "true" ] \
  && pass "CloudWatch publication remains enabled" \
  || fail "CLOUDWATCH_PUBLISH_ENABLED is not true"

[ -z "$LEGACY_SWITCH" ] \
  && pass "Legacy fallback remains absent" \
  || fail "LEGACY_FALLBACK_ENABLED unexpectedly exists"

section "9. SHARED MONITOR STATUS"

MONITOR_POD="$(
  kubectl get pods \
    -n "$MONITOR_NS" \
    -l app.kubernetes.io/name=gg-monitor \
    --field-selector=status.phase=Running \
    --sort-by=.metadata.creationTimestamp \
    -o name 2>/dev/null |
  tail -n 1 |
  sed 's#^pod/##'
)"

echo "Selected monitor pod: ${MONITOR_POD:-NOT_FOUND}"

if [ -z "$MONITOR_POD" ]; then
  fail "No Running monitor pod found"
else
  if kubectl exec -i \
      -n "$MONITOR_NS" \
      "$MONITOR_POD" \
      -- python3 - <<'PY'
import json
import urllib.request

expected = {
    "gg-oracle-payments-01": ("oracle", "source"),
    "gg-postgresql-payments-01": ("postgresql", "target"),
}

with urllib.request.urlopen(
    "http://127.0.0.1:8080/api/status",
    timeout=10,
) as response:
    payload = json.load(response)

seen = {}

for logical in payload.get("logicalPipelines", []):
    for runtime in logical.get("runtimes", []):
        name = runtime.get("deploymentName")

        if name not in expected:
            continue

        seen[name] = runtime

        print(
            name,
            "status=" + str(runtime.get("effectiveStatus")),
            "fresh=" + str(runtime.get("fresh")),
            "source=" + str(runtime.get("dataSource")),
            "type=" + str(runtime.get("deploymentType")),
            "role=" + str(runtime.get("role")),
            "metricsEnabled=" + str(runtime.get("metricsEnabled")),
            "alertsEnabled=" + str(runtime.get("alertsEnabled")),
            "services=" + json.dumps(
                runtime.get("criticalServices") or {},
                sort_keys=True,
            ),
        )

assert set(seen) == set(expected)

for name, runtime in seen.items():
    expected_type, expected_role = expected[name]

    assert runtime.get("deploymentType") == expected_type
    assert runtime.get("role") == expected_role
    assert runtime.get("effectiveStatus") == "UP"
    assert runtime.get("fresh") is True
    assert runtime.get("dataSource") == "canonical-monitor"
    assert runtime.get("metricsEnabled") is True
    assert runtime.get("alertsEnabled") is False

    services = runtime.get("criticalServices") or {}

    assert services.get("adminsrvr") is True
    assert services.get("distsrvr") is True
    assert services.get("recvsrvr") is True

print("SHARED MONITOR VALIDATION PASSED")
PY
  then
    pass "Shared monitor confirms both canonical runtimes are healthy"
  else
    fail "Shared monitor validation failed"
  fi
fi

section "10. ERROR CHECK"

ERRORS="$(
  {
    kubectl logs \
      -n "$MONITOR_NS" \
      deployment/gg-monitor \
      --since=20m \
      2>&1 || true

    kubectl logs \
      -n "$RUNTIME_NS" \
      statefulset/gg-oracle-payments-01 \
      --since=20m \
      2>&1 || true

    kubectl logs \
      -n "$RUNTIME_NS" \
      statefulset/gg-postgresql-payments-01 \
      --since=20m \
      2>&1 || true
  } |
  grep -Ei \
    'AccessDenied|cloudwatch_client_creation_failed|cloudwatch_put_metric_data_failed|tick failed|legacy-observer-fallback|kms.*denied|failed.*secret|permission denied' \
  || true
)"

if [ -z "$ERRORS" ]; then
  pass "No runtime, monitor, IAM, secret or CloudWatch errors found"
else
  fail "Runtime or monitor errors were found"
  echo "$ERRORS"
fi

section "11. FINAL RESULT"

if [ "$FAILURES" -eq 0 ]; then
  echo
  echo "PHASE 5B2A LIVE VALIDATION PASSED"
  echo "GITHUB WORKFLOW CORRECTION IS OPERATIONAL"
  echo "CANONICAL SINGLERUNTIME APPLICATIONS ARE HEALTHY"
  echo "LEGACYPAIR LIVE RUNTIME REMAINS RETIRED"
  echo "MANAGER MONITORING CONTRACT REMAINS HEALTHY"
  exit 0
fi

echo
echo "PHASE 5B2A VALIDATION COMPLETED WITH ${FAILURES} FAILURE(S)"
echo "STOP BEFORE EXTERNAL LEGACY RESOURCE CLEANUP"
exit 1