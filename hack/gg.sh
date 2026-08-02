cat > validate-phase6a.sh <<'SCRIPT'
#!/usr/bin/env bash
set -u

NS="goldengate-dev"
MONITOR_NS="goldengate-monitoring"
ARGO_NS="argocd"
APP="goldengate-dev-platform"

EXPECTED_IMAGE="229410149234.dkr.ecr.eu-west-1.amazonaws.com/aws-cloud-factory-fluent-bit@sha256:366923ffc51dfde4966e743dcbd4ca05211b733d4f69c7591903bc7660fbf243"

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

section "1. ARGO CD PLATFORM STATUS"

APP_STATUS="$(
  kubectl get application "$APP" \
    -n "$ARGO_NS" \
    -o jsonpath='{.status.sync.status}{"|"}{.status.health.status}' \
    2>/dev/null || true
)"

echo "${APP}: ${APP_STATUS:-NOT_FOUND}"

[ "$APP_STATUS" = "Synced|Healthy" ] \
  && pass "Platform Application is Synced and Healthy" \
  || fail "Platform Application is not Synced and Healthy"

section "2. FLUENT BIT DAEMONSET ROLLOUT"

kubectl get daemonset gg-fluent-bit \
  -n "$NS" \
  -o wide 2>/dev/null || true

if kubectl rollout status \
    daemonset/gg-fluent-bit \
    -n "$NS" \
    --timeout=5m; then
  pass "Fluent Bit DaemonSet rollout completed"
else
  fail "Fluent Bit DaemonSet rollout failed"
fi

DESIRED="$(
  kubectl get daemonset gg-fluent-bit \
    -n "$NS" \
    -o jsonpath='{.status.desiredNumberScheduled}' \
    2>/dev/null || true
)"

READY="$(
  kubectl get daemonset gg-fluent-bit \
    -n "$NS" \
    -o jsonpath='{.status.numberReady}' \
    2>/dev/null || true
)"

AVAILABLE="$(
  kubectl get daemonset gg-fluent-bit \
    -n "$NS" \
    -o jsonpath='{.status.numberAvailable}' \
    2>/dev/null || true
)"

echo "desired=${DESIRED:-missing} ready=${READY:-missing} available=${AVAILABLE:-missing}"

if [ -n "$DESIRED" ] &&
   [ "$DESIRED" != "0" ] &&
   [ "$READY" = "$DESIRED" ] &&
   [ "$AVAILABLE" = "$DESIRED" ]; then
  pass "Fluent Bit is Ready on all scheduled nodes"
else
  fail "Fluent Bit readiness does not match desired count"
fi

section "3. PRIVATE IMMUTABLE IMAGE"

LIVE_IMAGE="$(
  kubectl get daemonset gg-fluent-bit \
    -n "$NS" \
    -o jsonpath='{.spec.template.spec.containers[0].image}' \
    2>/dev/null || true
)"

echo "Expected: $EXPECTED_IMAGE"
echo "Live:     ${LIVE_IMAGE:-NOT_FOUND}"

[ "$LIVE_IMAGE" = "$EXPECTED_IMAGE" ] \
  && pass "Approved private immutable Fluent Bit image is active" \
  || fail "Live Fluent Bit image does not match the approved digest"

section "4. SERVICEACCOUNT AND IRSA"

SERVICE_ACCOUNT="$(
  kubectl get daemonset gg-fluent-bit \
    -n "$NS" \
    -o jsonpath='{.spec.template.spec.serviceAccountName}' \
    2>/dev/null || true
)"

ROLE_ARN="$(
  kubectl get serviceaccount gg-fluent-bit \
    -n "$NS" \
    -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}' \
    2>/dev/null || true
)"

echo "ServiceAccount: ${SERVICE_ACCOUNT:-MISSING}"
echo "IRSA role:      ${ROLE_ARN:-MISSING}"

[ "$SERVICE_ACCOUNT" = "gg-fluent-bit" ] \
  && pass "DaemonSet uses the dedicated Fluent Bit ServiceAccount" \
  || fail "Unexpected Fluent Bit ServiceAccount"

case "$ROLE_ARN" in
  arn:aws:iam::668311715351:role/*GoldenGatePlatformLoggingRole-dev*)
    pass "Dedicated platform logging IRSA role is configured"
    ;;
  *)
    fail "Unexpected or missing Fluent Bit IRSA role"
    ;;
esac

section "5. SECURITY CONTEXT"

kubectl get daemonset gg-fluent-bit \
  -n "$NS" \
  -o jsonpath='
privileged={.spec.template.spec.containers[0].securityContext.privileged}
allowPrivilegeEscalation={.spec.template.spec.containers[0].securityContext.allowPrivilegeEscalation}
readOnlyRootFilesystem={.spec.template.spec.containers[0].securityContext.readOnlyRootFilesystem}
hostNetwork={.spec.template.spec.hostNetwork}
hostPID={.spec.template.spec.hostPID}
hostIPC={.spec.template.spec.hostIPC}
capabilitiesDrop={.spec.template.spec.containers[0].securityContext.capabilities.drop[*]}
{"\n"}' 2>/dev/null || true

PRIVILEGED="$(
  kubectl get daemonset gg-fluent-bit \
    -n "$NS" \
    -o jsonpath='{.spec.template.spec.containers[0].securityContext.privileged}' \
    2>/dev/null || true
)"

ALLOW_ESC="$(
  kubectl get daemonset gg-fluent-bit \
    -n "$NS" \
    -o jsonpath='{.spec.template.spec.containers[0].securityContext.allowPrivilegeEscalation}' \
    2>/dev/null || true
)"

READ_ONLY_ROOT="$(
  kubectl get daemonset gg-fluent-bit \
    -n "$NS" \
    -o jsonpath='{.spec.template.spec.containers[0].securityContext.readOnlyRootFilesystem}' \
    2>/dev/null || true
)"

[ "$PRIVILEGED" = "false" ] \
  && pass "privileged=false" \
  || fail "privileged is not false"

[ "$ALLOW_ESC" = "false" ] \
  && pass "allowPrivilegeEscalation=false" \
  || fail "allowPrivilegeEscalation is not false"

[ "$READ_ONLY_ROOT" = "true" ] \
  && pass "readOnlyRootFilesystem=true" \
  || fail "readOnlyRootFilesystem is not true"

section "6. HOST MOUNTS AND STATE STORAGE"

kubectl get daemonset gg-fluent-bit \
  -n "$NS" \
  -o jsonpath='{range .spec.template.spec.containers[0].volumeMounts[*]}{.name}{"|"}{.mountPath}{"|readOnly="}{.readOnly}{"\n"}{end}' \
  2>/dev/null || true

VARLOG_READONLY="$(
  kubectl get daemonset gg-fluent-bit \
    -n "$NS" \
    -o jsonpath='{.spec.template.spec.containers[0].volumeMounts[?(@.mountPath=="/var/log")].readOnly}' \
    2>/dev/null || true
)"

STATE_SIZE="$(
  kubectl get daemonset gg-fluent-bit \
    -n "$NS" \
    -o jsonpath='{.spec.template.spec.volumes[?(@.name=="fluent-bit-state")].emptyDir.sizeLimit}' \
    2>/dev/null || true
)"

echo "fluent-bit-state sizeLimit=${STATE_SIZE:-MISSING}"

[ "$VARLOG_READONLY" = "true" ] \
  && pass "/var/log host mount is read-only" \
  || fail "/var/log host mount is not read-only"

[ "$STATE_SIZE" = "300Mi" ] \
  && pass "Fluent Bit state volume is bounded to 300Mi" \
  || fail "Unexpected or missing Fluent Bit state-volume sizeLimit"

section "7. FLUENT BIT CONFIGURATION"

CONFIG="$(
  kubectl get configmap gg-fluent-bit \
    -n "$NS" \
    -o jsonpath='{.data.fluent-bit\.conf}' \
    2>/dev/null || true
)"

printf '%s\n' "$CONFIG" |
grep -E \
  'Path |Name cloudwatch_logs|log_group_name|auto_create_group|storage.total_limit_size|Regex' \
  || true

EXPECTED_PATH='/var/log/containers/*_goldengate-dev_*.log,/var/log/containers/*_goldengate-monitoring_*.log'

printf '%s\n' "$CONFIG" |
grep -Fq "$EXPECTED_PATH" \
  && pass "Tail input is restricted to GoldenGate namespaces" \
  || fail "Expected GoldenGate namespace Tail path is missing"

if printf '%s\n' "$CONFIG" |
   grep -Fq '/var/log/containers/*.log'; then
  fail "Unrestricted cluster-wide Tail path is present"
else
  pass "No unrestricted cluster-wide Tail path exists"
fi

printf '%s\n' "$CONFIG" |
grep -q '/adcb/goldengate/dev/runtime' \
  && pass "Runtime CloudWatch log group is configured" \
  || fail "Runtime CloudWatch log group is missing"

printf '%s\n' "$CONFIG" |
grep -q '/adcb/goldengate/dev/monitor' \
  && pass "Monitor CloudWatch log group is configured" \
  || fail "Monitor CloudWatch log group is missing"

OUTPUT_LIMIT_COUNT="$(
  printf '%s\n' "$CONFIG" |
  grep -c 'storage.total_limit_size[[:space:]]\+128M' || true
)"

[ "$OUTPUT_LIMIT_COUNT" -eq 2 ] \
  && pass "Both CloudWatch outputs have bounded 128M queues" \
  || fail "Expected two output queue limits, found ${OUTPUT_LIMIT_COUNT}"

AUTO_CREATE_COUNT="$(
  printf '%s\n' "$CONFIG" |
  grep -c 'auto_create_group[[:space:]]\+false' || true
)"

[ "$AUTO_CREATE_COUNT" -eq 2 ] \
  && pass "Both outputs prohibit automatic log-group creation" \
  || fail "Expected two auto_create_group false settings"

section "8. FLUENT BIT PODS"

kubectl get pods \
  -n "$NS" \
  -o custom-columns='POD:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[*].ready,NODE:.spec.nodeName,IMAGE:.spec.containers[*].image' |
grep -E 'POD|gg-fluent-bit' || true

section "9. FLUENT BIT RECENT LOGS"

FLUENT_LOGS="$(
  kubectl logs \
    -n "$NS" \
    daemonset/gg-fluent-bit \
    --since=20m \
    --tail=1000 \
    2>&1 || true
)"

printf '%s\n' "$FLUENT_LOGS" | tail -n 300

ERRORS="$(
  printf '%s\n' "$FLUENT_LOGS" |
  grep -Ei \
    'AccessDenied|NoCredentialProviders|WebIdentityErr|InvalidIdentityToken|ResourceNotFoundException|failed to create log stream|PutLogEvents.*failed|cloudwatch_logs.*error|configuration error|permission denied|cannot open|read-only file system|failed to flush chunk|retry in' \
  || true
)"

if [ -z "$ERRORS" ]; then
  pass "No Fluent Bit IAM, filesystem, configuration or CloudWatch errors found"
else
  fail "Fluent Bit errors were found"
  echo "$ERRORS"
fi

section "10. RUNTIME SIDECAR REGRESSION"

for STS in \
  gg-oracle-payments-01 \
  gg-postgresql-payments-01
do
  CONTAINERS="$(
    kubectl get statefulset "$STS" \
      -n "$NS" \
      -o jsonpath='{.spec.template.spec.containers[*].name}' \
      2>/dev/null || true
  )"

  echo "${STS}: ${CONTAINERS:-NOT_FOUND}"

  case "$CONTAINERS" in
    *fluent-bit*|*fluentbit*)
      fail "${STS} contains an unexpected Fluent Bit sidecar"
      ;;
    "")
      fail "${STS} StatefulSet was not found"
      ;;
    *)
      pass "${STS} remains Fluent Bit sidecar-free"
      ;;
  esac
done

section "11. MONITOR STILL HEALTHY"

MONITOR_STATUS="$(
  kubectl get deployment gg-monitor \
    -n "$MONITOR_NS" \
    -o jsonpath='{.status.readyReplicas}{"|"}{.status.replicas}' \
    2>/dev/null || true
)"

echo "gg-monitor ready/replicas: ${MONITOR_STATUS:-NOT_FOUND}"

case "$MONITOR_STATUS" in
  1\|1)
    pass "Shared monitor remains healthy"
    ;;
  *)
    fail "Shared monitor is not fully ready"
    ;;
esac

section "12. FINAL RESULT"

if [ "$FAILURES" -eq 0 ]; then
  echo
  echo "PHASE 6A LIVE KUBERNETES VALIDATION PASSED"
  echo "CENTRALIZED FLUENT BIT DAEMONSET IS HEALTHY"
  echo "PRIVATE IMMUTABLE FLUENT BIT IMAGE IS ACTIVE"
  echo "INPUT IS LIMITED TO GOLDENGATE NAMESPACES"
  echo "FILESYSTEM BUFFERING IS BOUNDED"
  echo "GOLDENGATE RUNTIMES REMAIN SIDECAR-FREE"
  exit 0
fi

echo
echo "PHASE 6A VALIDATION COMPLETED WITH ${FAILURES} FAILURE(S)"
exit 1
SCRIPT

chmod +x validate-phase6a.sh
./validate-phase6a.sh