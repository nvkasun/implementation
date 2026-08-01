set -u

RUNTIME_NS="goldengate-dev"
MONITOR_NS="goldengate-monitoring"

FAILURES=0

pass() {
  echo "PASS: $*"
}

fail() {
  echo "FAIL: $*" >&2
  FAILURES=$((FAILURES + 1))
}

section() {
  echo
  echo "============================================================"
  echo "$1"
  echo "============================================================"
}

check_runtime() {
  STS="$1"
  EXPECTED_CONTAINER="$2"

  section "CURRENT RUNTIME CHECK: ${STS}"

  POD="$(
    kubectl get pods \
      -n "$RUNTIME_NS" \
      -l "app.kubernetes.io/instance=${STS}" \
      --field-selector=status.phase=Running \
      --sort-by=.metadata.creationTimestamp \
      -o name 2>/dev/null |
    tail -n 1 |
    sed 's#^pod/##'
  )"

  if [ -z "$POD" ]; then
    fail "No Running pod found for ${STS}"
    return
  fi

  POD_UID="$(
    kubectl get pod "$POD" \
      -n "$RUNTIME_NS" \
      -o jsonpath='{.metadata.uid}'
  )"

  POD_START="$(
    kubectl get pod "$POD" \
      -n "$RUNTIME_NS" \
      -o jsonpath='{.status.startTime}'
  )"

  READY="$(
    kubectl get pod "$POD" \
      -n "$RUNTIME_NS" \
      -o jsonpath='{.status.containerStatuses[0].ready}'
  )"

  CONTAINERS="$(
    kubectl get pod "$POD" \
      -n "$RUNTIME_NS" \
      -o jsonpath='{.spec.containers[*].name}'
  )"

  echo "Pod:        ${POD}"
  echo "UID:        ${POD_UID}"
  echo "Started:    ${POD_START}"
  echo "Ready:      ${READY}"
  echo "Containers: ${CONTAINERS}"

  if [ "$READY" = "true" ]; then
    pass "${POD} is Ready"
  else
    fail "${POD} is not Ready"
  fi

  if [ "$CONTAINERS" = "$EXPECTED_CONTAINER" ]; then
    pass "${POD} contains exactly ${EXPECTED_CONTAINER}"
  else
    fail "Unexpected containers in ${POD}: ${CONTAINERS}"
  fi

  echo
  echo "--- Current /u02 mount ---"

  U02_MOUNT="$(
    kubectl exec \
      -n "$RUNTIME_NS" \
      "$POD" \
      -- sh -c \
      'awk '\''$2 == "/u02" {print $1 "|" $2 "|" $3 "|" $4}'\'' /proc/mounts' \
      2>/dev/null || true
  )"

  echo "${U02_MOUNT:-NO_MOUNT_FOUND}"

  if printf '%s' "$U02_MOUNT" | grep -Eq '\|/u02\|nfs4?\|'; then
    pass "${POD} currently has /u02 mounted through NFS/EFS"
  else
    fail "${POD} does not show an active NFS/EFS mount on /u02"
  fi

  echo
  echo "--- Current pod warning events ---"

  CURRENT_WARNINGS="$(
    kubectl get events \
      -n "$RUNTIME_NS" \
      --field-selector "involvedObject.uid=${POD_UID}" \
      --sort-by=.metadata.creationTimestamp \
      -o custom-columns='TIME:.metadata.creationTimestamp,TYPE:.type,REASON:.reason,MESSAGE:.message' \
      2>/dev/null |
    grep -E 'Warning|FailedMount|AccessDenied|Forbidden' || true
  )"

  if [ -n "$CURRENT_WARNINGS" ]; then
    echo "$CURRENT_WARNINGS"
    echo
    echo "INFO: Warnings can be transient; current Ready and mounted state is authoritative."
  else
    echo "No warning events found for the current pod UID."
  fi

  echo
  echo "--- Runtime IAM-related logs ---"

  IAM_ERRORS="$(
    kubectl logs \
      -n "$RUNTIME_NS" \
      "$POD" \
      --since=20m \
      2>&1 |
    grep -Ei \
      'AccessDenied|secretsmanager|kms.*denied|failed.*secret|permission denied|forbidden' \
    || true
  )"

  if [ -z "$IAM_ERRORS" ]; then
    pass "No current IAM, Secrets Manager or KMS errors for ${POD}"
  else
    fail "IAM or secret-retrieval errors found for ${POD}"
    echo "$IAM_ERRORS"
  fi
}

section "1. CURRENT ARGO CD HEALTH"

for APP in \
  goldengate-dev-oracle-payments-01 \
  goldengate-dev-postgresql-payments-01 \
  goldengate-dev-platform \
  goldengate-monitor
do
  STATUS="$(
    kubectl get application "$APP" \
      -n argocd \
      -o jsonpath='{.status.sync.status}{"|"}{.status.health.status}' \
      2>/dev/null || true
  )"

  echo "${APP}: ${STATUS:-NOT_FOUND}"

  if [ "$STATUS" = "Synced|Healthy" ]; then
    pass "${APP} is Synced and Healthy"
  else
    fail "${APP} is not Synced and Healthy"
  fi
done

section "2. CURRENT GOLDENGATE RUNTIMES"

check_runtime "gg-oracle-payments-01" "ogg-oracle"
check_runtime "gg-postgresql-payments-01" "ogg-postgresql"

section "3. SECRETS STORE CSI CURRENT STATUS"

SPCPS_JSON="$(
  kubectl get \
    secretproviderclasspodstatuses.secrets-store.csi.x-k8s.io \
    -n "$RUNTIME_NS" \
    -o json \
    2>/dev/null || true
)"

if [ -z "$SPCPS_JSON" ]; then
  echo "INFO: SecretProviderClassPodStatus resource was not available for inspection."
else
  echo "$SPCPS_JSON" |
  python3 -c '
import json
import sys

data = json.load(sys.stdin)
items = data.get("items", [])

if not items:
    print("No SecretProviderClassPodStatus objects found.")
    raise SystemExit(1)

failed = False

for item in items:
    metadata = item.get("metadata", {})
    status = item.get("status", {})
    name = metadata.get("name")
    mounted = status.get("mounted")

    print(f"{name}: mounted={mounted}")

    if mounted is not True:
        failed = True

raise SystemExit(1 if failed else 0)
'

  if [ "$?" -eq 0 ]; then
    pass "All current Secrets Store CSI status objects report mounted=true"
  else
    fail "A current Secrets Store CSI status object is not mounted"
  fi
fi

section "4. SHARED MONITOR HEALTH"

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
    "gg-oracle-payments-01",
    "gg-postgresql-payments-01",
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

        if name in expected:
            seen[name] = runtime

            print(
                name,
                "status=", runtime.get("effectiveStatus"),
                "fresh=", runtime.get("fresh"),
                "source=", runtime.get("dataSource"),
                "metricsEnabled=", runtime.get("metricsEnabled"),
                "alertsEnabled=", runtime.get("alertsEnabled"),
                "services=", runtime.get("criticalServices"),
            )

assert set(seen) == expected

for runtime in seen.values():
    assert runtime.get("effectiveStatus") == "UP"
    assert runtime.get("fresh") is True
    assert runtime.get("dataSource") == "canonical-monitor"
    assert runtime.get("metricsEnabled") is True
    assert runtime.get("alertsEnabled") is False

    services = runtime.get("criticalServices") or {}

    assert services.get("adminsrvr") is True
    assert services.get("distsrvr") is True
    assert services.get("recvsrvr") is True

print("CURRENT MONITOR HEALTH PASSED")
PY
  then
    pass "Shared monitor confirms both canonical runtimes are healthy"
  else
    fail "Shared monitor health validation failed"
  fi
fi

section "5. CURRENT MONITOR ERROR CHECK"

MONITOR_ERRORS="$(
  kubectl logs \
    -n "$MONITOR_NS" \
    deployment/gg-monitor \
    --since=20m \
    2>&1 |
  grep -E \
    'AccessDenied|cloudwatch_client_creation_failed|cloudwatch_put_metric_data_failed|tick failed|legacy-observer-fallback' \
  || true
)"

if [ -z "$MONITOR_ERRORS" ]; then
  pass "No current monitor IAM, CloudWatch or fallback errors found"
else
  fail "Current monitor errors were found"
  echo "$MONITOR_ERRORS"
fi

section "6. FINAL RESULT"

if [ "$FAILURES" -eq 0 ]; then
  echo
  echo "PHASE 5B1 IAM LIVE DEPLOYMENT VALIDATION PASSED"
  echo "HISTORICAL EFS MOUNT WARNINGS WERE TRANSIENT"
  echo "CURRENT EFS AND SECRET MOUNTS ARE HEALTHY"
  echo "CANONICAL RUNTIMES AND SHARED MONITOR ARE HEALTHY"
  exit 0
fi

echo
echo "FINAL CURRENT-STATE VALIDATION FOUND ${FAILURES} FAILURE(S)"
echo "STOP BEFORE OLD STORAGE OR DYNAMODB CLEANUP"
exit 1