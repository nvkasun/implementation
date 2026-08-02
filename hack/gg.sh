set -u

NS="goldengate-dev"
ARGO_NS="argocd"
FAILURES=0

pass() {
  echo "PASS: $*"
}

fail() {
  echo "FAIL: $*" >&2
  FAILURES=$((FAILURES + 1))
}

echo
echo "============================================================"
echo "1. ARGO CD PLATFORM STATUS"
echo "============================================================"

APP_STATUS="$(
  kubectl get application goldengate-dev-platform \
    -n "$ARGO_NS" \
    -o jsonpath='{.status.sync.status}{"|"}{.status.health.status}' \
    2>/dev/null || true
)"

echo "goldengate-dev-platform: ${APP_STATUS:-NOT_FOUND}"

[ "$APP_STATUS" = "Synced|Healthy" ] \
  && pass "Platform Application is Synced and Healthy" \
  || fail "Platform Application is not Synced and Healthy"

echo
echo "============================================================"
echo "2. FLUENT BIT DAEMONSET"
echo "============================================================"

kubectl get daemonset gg-fluent-bit \
  -n "$NS" \
  -o wide

if kubectl rollout status daemonset/gg-fluent-bit \
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
  fail "Fluent Bit DaemonSet readiness does not match desired count"
fi

echo
echo "============================================================"
echo "3. PRIVATE IMMUTABLE IMAGE"
echo "============================================================"

EXPECTED_IMAGE="229410149234.dkr.ecr.eu-west-1.amazonaws.com/aws-cloud-factory-fluent-bit@sha256:366923ffc51dfde4966e743dcbd4ca05211b733d4f69c7591903bc7660fbf243"

LIVE_IMAGE="$(
  kubectl get daemonset gg-fluent-bit \
    -n "$NS" \
    -o jsonpath='{.spec.template.spec.containers[0].image}' \
    2>/dev/null || true
)"

echo "Expected: $EXPECTED_IMAGE"
echo "Live:     ${LIVE_IMAGE:-NOT_FOUND}"

[ "$LIVE_IMAGE" = "$EXPECTED_IMAGE" ] \
  && pass "DaemonSet uses the approved private immutable image" \
  || fail "DaemonSet image does not match the approved digest"

echo
echo "============================================================"
echo "4. SERVICEACCOUNT AND IRSA"
echo "============================================================"

kubectl get serviceaccount gg-fluent-bit \
  -n "$NS" \
  -o yaml |
grep -E 'name: gg-fluent-bit|eks.amazonaws.com/role-arn' || true

ROLE_ARN="$(
  kubectl get serviceaccount gg-fluent-bit \
    -n "$NS" \
    -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}' \
    2>/dev/null || true
)"

echo "IRSA role: ${ROLE_ARN:-MISSING}"

case "$ROLE_ARN" in
  arn:aws:iam::668311715351:role/*GoldenGatePlatformLoggingRole-dev*)
    pass "Fluent Bit ServiceAccount uses the dedicated logging role"
    ;;
  *)
    fail "Unexpected or missing Fluent Bit IRSA role"
    ;;
esac

echo
echo "============================================================"
echo "5. SECURITY CONTEXT AND HOST MOUNTS"
echo "============================================================"

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
{"\n"}'

PRIVILEGED="$(
  kubectl get daemonset gg-fluent-bit \
    -n "$NS" \
    -o jsonpath='{.spec.template.spec.containers[0].securityContext.privileged}'
)"

ALLOW_ESC="$(
  kubectl get daemonset gg-fluent-bit \
    -n "$NS" \
    -o jsonpath='{.spec.template.spec.containers[0].securityContext.allowPrivilegeEscalation}'
)"

READ_ONLY_ROOT="$(
  kubectl get daemonset gg-fluent-bit \
    -n "$NS" \
    -o jsonpath='{.spec.template.spec.containers[0].securityContext.readOnlyRootFilesystem}'
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

kubectl get daemonset gg-fluent-bit \
  -n "$NS" \
  -o jsonpath='{range .spec.template.spec.containers[0].volumeMounts[*]}{.name}{"|"}{.mountPath}{"|"}{.readOnly}{"\n"}{end}'

VARLOG_READONLY="$(
  kubectl get daemonset gg-fluent-bit \
    -n "$NS" \
    -o jsonpath='{.spec.template.spec.containers[0].volumeMounts[?(@.mountPath=="/var/log")].readOnly}' \
    2>/dev/null || true
)"

[ "$VARLOG_READONLY" = "true" ] \
  && pass "/var/log host mount is read-only" \
  || fail "/var/log host mount is not read-only"

echo
echo "============================================================"
echo "6. CONFIGURATION"
echo "============================================================"

CONFIG="$(
  kubectl get configmap gg-fluent-bit \
    -n "$NS" \
    -o jsonpath='{.data.fluent-bit\.conf}' \
    2>/dev/null || true
)"

printf '%s\n' "$CONFIG" |
grep -E \
  'Path |Name cloudwatch_logs|log_group_name|auto_create_group|storage.total_limit_size|Regex.*goldengate' \
  || true

printf '%s\n' "$CONFIG" |
grep -q '/var/log/containers/\*_goldengate-dev_\*\.log,/var/log/containers/\*_goldengate-monitoring_\*\.log' \
  && pass "Tail input is restricted to the two GoldenGate namespaces" \
  || fail "Tail input namespace restriction is missing"

if printf '%s\n' "$CONFIG" |
   grep -q '/var/log/containers/\*\.log'; then
  fail "Unrestricted cluster-wide Tail path is present"
else
  pass "No unrestricted cluster-wide Tail path exists"
fi

OUTPUT_LIMIT_COUNT="$(
  printf '%s\n' "$CONFIG" |
  grep -c 'storage.total_limit_size[[:space:]]\+128M' || true
)"

[ "$OUTPUT_LIMIT_COUNT" -eq 2 ] \
  && pass "Both CloudWatch outputs have bounded filesystem queues" \
  || fail "Expected two storage.total_limit_size entries, found ${OUTPUT_LIMIT_COUNT}"

printf '%s\n' "$CONFIG" |
grep -q 'auto_create_group[[:space:]]\+false' \
  && pass "Fluent Bit cannot create log groups" \
  || fail "auto_create_group false is missing"

echo
echo "============================================================"
echo "7. BOUNDED EMPTYDIR"
echo "============================================================"

STATE_SIZE="$(
  kubectl get daemonset gg-fluent-bit \
    -n "$NS" \
    -o jsonpath='{.spec.template.spec.volumes[?(@.name=="fluent-bit-state")].emptyDir.sizeLimit}' \
    2>/dev/null || true
)"

echo "fluent-bit-state sizeLimit=${STATE_SIZE:-MISSING}"

[ "$STATE_SIZE" = "300Mi" ] \
  && pass "Fluent Bit state volume is bounded to 300Mi" \
  || fail "Unexpected or missing state-volume sizeLimit"

echo
echo "============================================================"
echo "8. PODS AND RECENT LOGS"
echo "============================================================"

kubectl get pods \
  -n "$NS" \
  -l app.kubernetes.io/name=fluent-bit \
  -o wide

kubectl logs \
  -n "$NS" \
  daemonset/gg-fluent-bit \
  --since=15m \
  --tail=300 \
  2>&1 |
tail -n 300

ERRORS="$(
  kubectl logs \
    -n "$NS" \
    daemonset/gg-fluent-bit \
    --since=15m \
    --tail=1000 \
    2>&1 |
  grep -Ei \
    'AccessDenied|NoCredentialProviders|WebIdentityErr|InvalidIdentityToken|ResourceNotFoundException|failed to create log stream|PutLogEvents.*failed|cloudwatch_logs.*error|configuration error|permission denied|cannot open|read-only file system' \
  || true
)"

if [ -z "$ERRORS" ]; then
  pass "No Fluent Bit IAM, filesystem or CloudWatch output errors found"
else
  fail "Fluent Bit errors were found"
  echo "$ERRORS"
fi

echo
echo "============================================================"
echo "9. RUNTIME SIDECAR REGRESSION"
echo "============================================================"

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
      fail "${STS} was not found"
      ;;
    *)
      pass "${STS} remains sidecar-free"
      ;;
  esac
done

echo
echo "============================================================"
echo "10. FINAL RESULT"
echo "============================================================"

if [ "$FAILURES" -eq 0 ]; then
  echo
  echo "PHASE 6A LIVE VALIDATION PASSED"
  echo "CENTRALIZED CONTAINER LOGGING DAEMONSET IS HEALTHY"
  echo "PRIVATE IMMUTABLE FLUENT BIT IMAGE IS ACTIVE"
  echo "GOLDENGATE RUNTIME PODS REMAIN SIDECAR-FREE"
  echo "CLOUDWATCH LOG DELIVERY HAS NO DETECTED ERRORS"
  exit 0
fi

echo
echo "PHASE 6A VALIDATION COMPLETED WITH ${FAILURES} FAILURE(S)"
exit 1