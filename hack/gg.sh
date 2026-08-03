cat > diagnose-fluent-bit.sh <<'SCRIPT'
#!/usr/bin/env bash
set -u

FB_NS="goldengate-dev"
FB_DS="gg-fluent-bit"

MON_NS="goldengate-monitoring"
MON_DEPLOYMENT="gg-monitor"

LOCAL_PORT="12020"

section() {
  echo
  echo "============================================================"
  echo "$1"
  echo "============================================================"
}

section "1. DISCOVER LIVE WORKLOAD SELECTORS"

FB_SELECTOR="$(
  kubectl get daemonset "$FB_DS" \
    -n "$FB_NS" \
    -o json 2>/dev/null |
  jq -r '
    .spec.selector.matchLabels
    | to_entries
    | map("\(.key)=\(.value)")
    | join(",")
  '
)"

MON_SELECTOR="$(
  kubectl get deployment "$MON_DEPLOYMENT" \
    -n "$MON_NS" \
    -o json 2>/dev/null |
  jq -r '
    .spec.selector.matchLabels
    | to_entries
    | map("\(.key)=\(.value)")
    | join(",")
  '
)"

echo "Fluent Bit selector: ${FB_SELECTOR:-NOT_FOUND}"
echo "Monitor selector:    ${MON_SELECTOR:-NOT_FOUND}"

if [ -z "$FB_SELECTOR" ] || [ "$FB_SELECTOR" = "null" ]; then
  echo "FAIL: Could not derive Fluent Bit selector from the DaemonSet"
  exit 1
fi

if [ -z "$MON_SELECTOR" ] || [ "$MON_SELECTOR" = "null" ]; then
  echo "FAIL: Could not derive monitor selector from the Deployment"
  exit 1
fi

section "2. DISCOVER RUNNING MONITOR POD"

MON_PODS_JSON="$(
  kubectl get pods \
    -n "$MON_NS" \
    -l "$MON_SELECTOR" \
    --field-selector=status.phase=Running \
    -o json
)"

MON_POD="$(
  printf '%s' "$MON_PODS_JSON" |
  jq -r '.items[0].metadata.name // empty'
)"

if [ -z "$MON_POD" ]; then
  echo "FAIL: No Running monitor pod matched:"
  echo "$MON_SELECTOR"

  kubectl get pods -n "$MON_NS" --show-labels
  exit 1
fi

MON_NODE="$(
  kubectl get pod "$MON_POD" \
    -n "$MON_NS" \
    -o jsonpath='{.spec.nodeName}'
)"

echo "Monitor pod:  $MON_POD"
echo "Monitor node: $MON_NODE"

section "3. DISCOVER FLUENT BIT POD ON THE MONITOR NODE"

FB_PODS_JSON="$(
  kubectl get pods \
    -n "$FB_NS" \
    -l "$FB_SELECTOR" \
    --field-selector=status.phase=Running \
    -o json
)"

FB_POD="$(
  printf '%s' "$FB_PODS_JSON" |
  jq -r --arg node "$MON_NODE" '
    [
      .items[]
      | select(.spec.nodeName == $node)
      | .metadata.name
    ][0] // empty
  '
)"

if [ -z "$FB_POD" ]; then
  echo "FAIL: No Running Fluent Bit pod exists on monitor node $MON_NODE"
  echo
  echo "All Fluent Bit pods:"

  kubectl get pods \
    -n "$FB_NS" \
    -l "$FB_SELECTOR" \
    -o wide \
    --show-labels

  exit 1
fi

FB_NODE="$(
  kubectl get pod "$FB_POD" \
    -n "$FB_NS" \
    -o jsonpath='{.spec.nodeName}'
)"

echo "Fluent Bit pod:  $FB_POD"
echo "Fluent Bit node: $FB_NODE"
echo
echo "PASS: Monitor and Fluent Bit are on the same node"

section "4. GENERATE A FRESH CONTROLLED MONITOR LOG"

TEST_ID="PHASE6A_CLOUDWATCH_TEST_$(date -u +%Y%m%dT%H%M%SZ)"

echo "Test ID: $TEST_ID"

kubectl exec \
  -n "$MON_NS" \
  "$MON_POD" \
  -- python3 -c \
  "import os; fd=os.open('/proc/1/fd/1', os.O_WRONLY); os.write(fd, b'${TEST_ID}\n'); os.close(fd)"

sleep 5

if kubectl logs \
    -n "$MON_NS" \
    "$MON_POD" \
    --since=2m |
   grep -F "$TEST_ID"; then
  echo "PASS: Test line reached the Kubernetes monitor container log"
else
  echo "FAIL: Test line is missing from kubectl logs"
  exit 1
fi

section "5. VERIFY TEST LINE THROUGH FLUENT BIT HOST MOUNT"

if kubectl exec \
    -n "$FB_NS" \
    "$FB_POD" \
    -- sh -c "
      echo 'Matching files:'
      ls -l /var/log/containers/*_goldengate-monitoring_*.log 2>&1 || true

      echo
      echo 'Searching for test ID:'
      grep -H -- '${TEST_ID}' \
        /var/log/containers/*_goldengate-monitoring_*.log \
        2>&1 || true
    "
then
  echo
  echo "Host-mounted log inspection completed"
else
  echo
  echo "INFO: The Fluent Bit image may not contain a usable shell."
  echo "Continuing with process logs and HTTP metrics."
fi

section "6. FLUENT BIT PROCESS LOGS"

kubectl logs \
  -n "$FB_NS" \
  "$FB_POD" \
  --since=60m \
  --tail=1000 \
  2>&1 || true

section "7. START FLUENT BIT METRICS PORT-FORWARD"

kubectl port-forward \
  -n "$FB_NS" \
  "pod/$FB_POD" \
  "${LOCAL_PORT}:2020" \
  >/tmp/fluent-bit-port-forward.log 2>&1 &

PF_PID=$!

cleanup() {
  kill "$PF_PID" >/dev/null 2>&1 || true
}

trap cleanup EXIT

sleep 5

if ! kill -0 "$PF_PID" >/dev/null 2>&1; then
  echo "FAIL: Fluent Bit port-forward stopped"
  cat /tmp/fluent-bit-port-forward.log
  exit 1
fi

if ! curl -fsS \
    "http://127.0.0.1:${LOCAL_PORT}/api/v1/metrics/prometheus" \
    >/tmp/fluent-bit-metrics.txt; then
  echo "FAIL: Could not query Fluent Bit metrics"
  cat /tmp/fluent-bit-port-forward.log
  exit 1
fi

echo "PASS: Fluent Bit metrics endpoint is reachable"

section "8. INPUT, FILTER AND OUTPUT METRICS"

echo "--- Tail input ---"

grep -E \
  'fluentbit_input_(records|bytes)_total' \
  /tmp/fluent-bit-metrics.txt \
  || true

echo
echo "--- Filters and rewrite-tag emitter ---"

grep -Ei \
  'fluentbit_filter_|rewrite_tag|emitter' \
  /tmp/fluent-bit-metrics.txt \
  || true

echo
echo "--- CloudWatch outputs ---"

grep -E \
  'fluentbit_output_(proc_records|errors|retries|retries_failed|dropped_records)_total' \
  /tmp/fluent-bit-metrics.txt \
  || true

section "9. FLUENT BIT STORAGE STATUS"

curl -fsS \
  "http://127.0.0.1:${LOCAL_PORT}/api/v1/storage" \
  2>/dev/null |
jq . || true

section "10. FOCUSED ERROR SEARCH"

ERRORS="$(
  kubectl logs \
    -n "$FB_NS" \
    "$FB_POD" \
    --since=60m \
    --tail=2000 \
    2>&1 |
  grep -Ei \
    'AccessDenied|WebIdentity|InvalidIdentityToken|NoCredentialProviders|ResourceNotFound|cloudwatch|PutLogEvents|CreateLogStream|failed to flush|retry|connection refused|timed out|permission denied|cannot open|configuration error' \
  || true
)"

if [ -z "$ERRORS" ]; then
  echo "No matching Fluent Bit error lines found"
else
  echo "$ERRORS"
fi

section "11. RESULT INTERPRETATION"

cat <<EOF
Fresh test ID:
$TEST_ID

Interpret the counters as follows:

A. Test ID is absent from the host-mounted file:
   Container-log path, host mount, symlink, or node placement issue.

B. Test ID is present, but Tail input records are zero:
   Tail file discovery or position-database issue.

C. Tail input records are greater than zero, but both CloudWatch
   proc_records counters remain zero:
   grep/rewrite_tag/Match routing issue.

D. CloudWatch retries or errors are greater than zero:
   IRSA, IAM, DNS, VPC endpoint, security group, or network issue.

E. CloudWatch proc_records are greater than zero and errors are zero:
   Records were handed to CloudWatch; verify account, region, group,
   and refresh/search all streams.
EOF
SCRIPT

chmod +x diagnose-fluent-bit.sh
./diagnose-fluent-bit.sh