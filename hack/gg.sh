cat > diagnose-fluent-bit.sh <<'SCRIPT'
#!/usr/bin/env bash
set -euo pipefail

FB_NS="goldengate-dev"
MON_NS="goldengate-monitoring"
TEST_ID="PHASE6A_CLOUDWATCH_TEST_20260802T204754Z"

FB_POD="$(
  kubectl get pods \
    -n "$FB_NS" \
    -l app.kubernetes.io/name=fluent-bit \
    --field-selector=status.phase=Running \
    -o jsonpath='{.items[0].metadata.name}'
)"

MON_POD="$(
  kubectl get pods \
    -n "$MON_NS" \
    -l app.kubernetes.io/name=gg-monitor \
    --field-selector=status.phase=Running \
    -o jsonpath='{.items[0].metadata.name}'
)"

FB_NODE="$(
  kubectl get pod "$FB_POD" \
    -n "$FB_NS" \
    -o jsonpath='{.spec.nodeName}'
)"

MON_NODE="$(
  kubectl get pod "$MON_POD" \
    -n "$MON_NS" \
    -o jsonpath='{.spec.nodeName}'
)"

echo
echo "============================================================"
echo "1. POD AND NODE PLACEMENT"
echo "============================================================"

echo "Fluent Bit pod: $FB_POD"
echo "Fluent Bit node: $FB_NODE"
echo "Monitor pod: $MON_POD"
echo "Monitor node: $MON_NODE"

if [ "$FB_NODE" = "$MON_NODE" ]; then
  echo "PASS: Fluent Bit and monitor are on the same node"
else
  echo "INFO: Monitor is on another node; confirm a Fluent Bit pod exists there"
  kubectl get pods \
    -n "$FB_NS" \
    -l app.kubernetes.io/name=fluent-bit \
    -o wide
fi

echo
echo "============================================================"
echo "2. VERIFY THE TEST LINE IN FLUENT BIT HOST MOUNT"
echo "============================================================"

kubectl exec \
  -n "$FB_NS" \
  "$FB_POD" \
  -- sh -c "
    echo 'Matching monitoring files:'
    ls -l /var/log/containers/*_goldengate-monitoring_*.log 2>&1 || true

    echo
    echo 'Searching for the controlled test line:'
    grep -H '${TEST_ID}' \
      /var/log/containers/*_goldengate-monitoring_*.log \
      2>&1 || true
  "

echo
echo "============================================================"
echo "3. FLUENT BIT PROCESS LOGS"
echo "============================================================"

kubectl logs \
  -n "$FB_NS" \
  "$FB_POD" \
  --since=60m \
  --tail=1000 \
  2>&1 || true

echo
echo "============================================================"
echo "4. START LOCAL METRICS PORT-FORWARD"
echo "============================================================"

kubectl port-forward \
  -n "$FB_NS" \
  "pod/$FB_POD" \
  2020:2020 \
  >/tmp/fluent-bit-port-forward.log 2>&1 &

PF_PID=$!

cleanup() {
  kill "$PF_PID" >/dev/null 2>&1 || true
}

trap cleanup EXIT

sleep 5

if ! kill -0 "$PF_PID" >/dev/null 2>&1; then
  echo "FAIL: port-forward stopped"
  cat /tmp/fluent-bit-port-forward.log
  exit 1
fi

echo "PASS: Fluent Bit metrics endpoint forwarded to localhost:2020"

echo
echo "============================================================"
echo "5. FLUENT BIT PROMETHEUS METRICS"
echo "============================================================"

curl -fsS \
  http://127.0.0.1:2020/api/v1/metrics/prometheus \
  >/tmp/fluent-bit-metrics.txt

grep -E \
  'fluentbit_(input|filter|output)_(records|bytes|proc_records|errors|retries|retries_failed|drop_records|add_records|emit_records)_total' \
  /tmp/fluent-bit-metrics.txt \
  || true

echo
echo "============================================================"
echo "6. FOCUSED INPUT AND OUTPUT COUNTERS"
echo "============================================================"

echo "--- Tail input ---"
grep -E \
  'fluentbit_input_(records|bytes)_total.*tail' \
  /tmp/fluent-bit-metrics.txt \
  || true

echo
echo "--- Runtime and monitor CloudWatch outputs ---"
grep -E \
  'fluentbit_output_(proc_records|errors|retries|retries_failed|dropped_records)_total.*cloudwatch' \
  /tmp/fluent-bit-metrics.txt \
  || true

echo
echo "--- Filters and emitter ---"
grep -E \
  'fluentbit_filter_|rewrite_tag|emitter' \
  /tmp/fluent-bit-metrics.txt \
  || true

echo
echo "============================================================"
echo "7. FLUENT BIT STORAGE STATUS"
echo "============================================================"

curl -fsS \
  http://127.0.0.1:2020/api/v1/storage \
  2>/dev/null |
jq . || true

echo
echo "============================================================"
echo "8. RESULT GUIDANCE"
echo "============================================================"

cat <<'EOF'
Interpretation:

A. Test ID missing from /var/log/containers:
   Host log mount/path or node-placement issue.

B. Test ID exists, but tail input records_total = 0:
   Fluent Bit Tail input/file discovery or position-database issue.

C. Tail input records_total > 0, but CloudWatch proc_records_total = 0:
   Kubernetes metadata, grep, rewrite_tag, or Match routing issue.

D. CloudWatch errors/retries > 0:
   IRSA, CloudWatch Logs endpoint, DNS, security group, or network issue.

E. CloudWatch proc_records_total > 0 and errors = 0:
   Delivery is occurring; verify the exact account, region, log group,
   and refresh/search all streams.
EOF
SCRIPT

chmod +x diagnose-fluent-bit.sh
./diagnose-fluent-bit.sh