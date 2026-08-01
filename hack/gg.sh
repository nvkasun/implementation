set -u

RUNTIME_NS="goldengate-dev"
MONITOR_NS="goldengate-monitoring"
ARGO_NS="argocd"

ORACLE_STS="gg-oracle-payments-01"
POSTGRES_STS="gg-postgresql-payments-01"
MONITOR_DEPLOY="gg-monitor"

EXPECTED_RUNTIME_ROLE="arn:aws:iam::668311715351:role/GoldenGateSecretsReadRole-dev"
EXPECTED_MONITOR_ROLE="arn:aws:iam::668311715351:role/GoldenGateMonitorReadRole-dev"

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

section "1. PRE-RESTART APPLICATION HEALTH"

for APP in \
  goldengate-dev-oracle-payments-01 \
  goldengate-dev-postgresql-payments-01 \
  goldengate-dev-platform \
  goldengate-monitor
do
  check_app "$APP"
done

section "2. SERVICE ACCOUNT AND IRSA ROLE MAPPING"

ORACLE_ROLE="$(
  kubectl get serviceaccount gg-oracle-sa \
    -n "$RUNTIME_NS" \
    -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}' \
    2>/dev/null || true
)"

POSTGRES_ROLE="$(
  kubectl get serviceaccount gg-postgresql-sa \
    -n "$RUNTIME_NS" \
    -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}' \
    2>/dev/null || true
)"

MONITOR_ROLE="$(
  kubectl get serviceaccount gg-monitor \
    -n "$MONITOR_NS" \
    -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}' \
    2>/dev/null || true
)"

echo "gg-oracle-sa role:     ${ORACLE_ROLE:-<missing>}"
echo "gg-postgresql-sa role: ${POSTGRES_ROLE:-<missing>}"
echo "gg-monitor role:       ${MONITOR_ROLE:-<missing>}"

if [ "$ORACLE_ROLE" = "$EXPECTED_RUNTIME_ROLE" ]; then
  pass "Oracle ServiceAccount retains the runtime IAM role"
else
  fail "Oracle ServiceAccount IAM role is unexpected"
fi

if [ "$POSTGRES_ROLE" = "$EXPECTED_RUNTIME_ROLE" ]; then
  pass "PostgreSQL ServiceAccount retains the runtime IAM role"
else
  fail "PostgreSQL ServiceAccount IAM role is unexpected"
fi

if [ "$MONITOR_ROLE" = "$EXPECTED_MONITOR_ROLE" ]; then
  pass "Monitor retains its separate monitor IAM role"
else
  fail "Monitor IAM role is unexpected"
fi

section "3. WAIT FOR IAM PROPAGATION"

echo "Waiting 30 seconds before controlled pod restarts..."
sleep 30

section "4. RESTART ORACLE AND VALIDATE"

kubectl rollout restart \
  statefulset/"$ORACLE_STS" \
  -n "$RUNTIME_NS"

if kubectl rollout status \
    statefulset/"$ORACLE_STS" \
    -n "$RUNTIME_NS" \
    --timeout=7m; then
  pass "Oracle restarted successfully with the reduced runtime IAM policy"
else
  fail "Oracle failed to restart"
fi

kubectl get pod \
  -n "$RUNTIME_NS" \
  -l "app.kubernetes.io/instance=${ORACLE_STS}" \
  -o custom-columns='POD:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[*].ready,CONTAINERS:.spec.containers[*].name,RESTARTS:.status.containerStatuses[*].restartCount,IMAGE:.spec.containers[*].image'

section "5. RESTART POSTGRESQL AND VALIDATE"

kubectl rollout restart \
  statefulset/"$POSTGRES_STS" \
  -n "$RUNTIME_NS"

if kubectl rollout status \
    statefulset/"$POSTGRES_STS" \
    -n "$RUNTIME_NS" \
    --timeout=7m; then
  pass "PostgreSQL restarted successfully with the reduced runtime IAM policy"
else
  fail "PostgreSQL failed to restart"
fi

kubectl get pod \
  -n "$RUNTIME_NS" \
  -l "app.kubernetes.io/instance=${POSTGRES_STS}" \
  -o custom-columns='POD:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[*].ready,CONTAINERS:.spec.containers[*].name,RESTARTS:.status.containerStatuses[*].restartCount,IMAGE:.spec.containers[*].image'

section "6. SECRETS STORE CSI AND STARTUP EVENTS"

EVENT_ERRORS="$(
  kubectl get events \
    -n "$RUNTIME_NS" \
    --sort-by=.metadata.creationTimestamp \
    -o custom-columns='TIME:.metadata.creationTimestamp,TYPE:.type,REASON:.reason,OBJECT:.involvedObject.name,MESSAGE:.message' \
    2>&1 |
  grep -Ei \
    'FailedMount|MountVolume|AccessDenied|secretsmanager|kms|forbidden|permission denied' \
  | tail -n 80 || true
)"

if [ -z "$EVENT_ERRORS" ]; then
  pass "No Secrets Store CSI, Secrets Manager or KMS mount errors found"
else
  fail "Secret or CSI-related events were found"
  echo "$EVENT_ERRORS"
fi

echo
echo "--- SecretProviderClasses ---"

kubectl get secretproviderclass \
  -n "$RUNTIME_NS" \
  -o custom-columns='NAME:.metadata.name,PROVIDER:.spec.provider' \
  2>&1

section "7. CANONICAL POD MODEL AFTER RESTART"

kubectl get pods \
  -n "$RUNTIME_NS" \
  -o custom-columns='POD:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[*].ready,CONTAINERS:.spec.containers[*].name,INIT:.spec.initContainers[*].name,RESTARTS:.status.containerStatuses[*].restartCount'

ORACLE_CONTAINERS="$(
  kubectl get statefulset "$ORACLE_STS" \
    -n "$RUNTIME_NS" \
    -o jsonpath='{.spec.template.spec.containers[*].name}' \
    2>/dev/null || true
)"

POSTGRES_CONTAINERS="$(
  kubectl get statefulset "$POSTGRES_STS" \
    -n "$RUNTIME_NS" \
    -o jsonpath='{.spec.template.spec.containers[*].name}' \
    2>/dev/null || true
)"

if [ "$ORACLE_CONTAINERS" = "ogg-oracle" ]; then
  pass "Oracle remains observer-free"
else
  fail "Unexpected Oracle containers: ${ORACLE_CONTAINERS:-<none>}"
fi

if [ "$POSTGRES_CONTAINERS" = "ogg-postgresql" ]; then
  pass "PostgreSQL remains observer-free"
else
  fail "Unexpected PostgreSQL containers: ${POSTGRES_CONTAINERS:-<none>}"
fi

OBSERVER_ROWS="$(
  kubectl get pods \
    -A \
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

section "8. RUNTIME LOG ERROR CHECK"

RUNTIME_ERRORS="$(
  for STS in "$ORACLE_STS" "$POSTGRES_STS"; do
    kubectl logs \
      -n "$RUNTIME_NS" \
      statefulset/"$STS" \
      --since=15m \
      2>&1 || true
  done |
  grep -Ei \
    'AccessDenied|secretsmanager|kms.*denied|failed.*secret|permission denied|FailedMount' \
  || true
)"

if [ -z "$RUNTIME_ERRORS" ]; then
  pass "No runtime IAM, Secrets Manager or KMS errors found"
else
  fail "Runtime IAM or secret-retrieval errors were found"
  echo "$RUNTIME_ERRORS"
fi

section "9. WAIT FOR SHARED MONITOR RECOVERY"

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
  fail "No Running gg-monitor pod found"
else
  if kubectl exec -i \
      -n "$MONITOR_NS" \
      "$MONITOR_POD" \
      -- python3 - <<'PY'
import json
import time
import urllib.request

base = "http://127.0.0.1:8080"

expected = {
    "gg-oracle-payments-01": ("oracle", "source"),
    "gg-postgresql-payments-01": ("postgresql", "target"),
}

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
        print("MONITOR RECOVERY AFTER IAM APPLY PASSED")
        raise SystemExit(0)

    time.sleep(10)

print("Last observed:", json.dumps(last, sort_keys=True))
raise SystemExit("Monitor did not recover within five minutes")
PY
  then
    pass "Shared monitor recovered both canonical runtimes"
  else
    fail "Shared monitor did not recover both canonical runtimes"
  fi
fi

section "10. MONITOR IAM AND CLOUDWATCH ERROR CHECK"

MONITOR_ERRORS="$(
  kubectl logs \
    -n "$MONITOR_NS" \
    deployment/"$MONITOR_DEPLOY" \
    --since=20m \
    2>&1 |
  grep -E \
    'AccessDenied|cloudwatch_client_creation_failed|cloudwatch_put_metric_data_failed|tick failed|legacy-observer-fallback' \
  || true
)"

if [ -z "$MONITOR_ERRORS" ]; then
  pass "No monitor IAM, CloudWatch or legacy-fallback errors found"
else
  fail "Monitor IAM or CloudWatch errors were found"
  echo "$MONITOR_ERRORS"
fi

section "11. OLD RETAINED PV SAFETY CHECK"

kubectl get pv \
  pvc-3a93c990-a9fa-4cca-99df-7c3375472074 \
  pvc-93251c3f-c408-4713-bd46-ebc5e0eafa8a \
  pvc-5c43940e-1054-43f5-8031-9db4b51a024a \
  pvc-bacb3e9d-d904-467c-959f-dea9548699c9 \
  -o custom-columns='PV:.metadata.name,STATUS:.status.phase,RECLAIM:.spec.persistentVolumeReclaimPolicy,STORAGECLASS:.spec.storageClassName,CLAIM_NAMESPACE:.spec.claimRef.namespace,CLAIM_NAME:.spec.claimRef.name,VOLUME_HANDLE:.spec.csi.volumeHandle' \
  2>&1

section "12. FINAL RESULT"

if [ "$FAILURES" -eq 0 ]; then
  echo
  echo "PHASE 5B1 IAM LIVE DEPLOYMENT VALIDATION PASSED"
  echo "RUNTIME OBSERVER PERMISSIONS ARE REMOVED"
  echo "CANONICAL SECRET RETRIEVAL AND RESTART PASSED"
  echo "MONITOR IAM AND MANAGER METRICS REMAIN HEALTHY"
  exit 0
fi

echo
echo "PHASE 5B1 IAM VALIDATION COMPLETED WITH ${FAILURES} FAILURE(S)"
echo "STOP BEFORE ANY FURTHER CLEANUP"
exit 1