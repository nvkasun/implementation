set -euo pipefail

MON_NS="goldengate-monitoring"

MON_POD="$(
  kubectl get pods \
    -n "$MON_NS" \
    -l app.kubernetes.io/name=gg-monitor \
    --field-selector=status.phase=Running \
    -o jsonpath='{.items[0].metadata.name}'
)"

TEST_ID="PHASE6A_CLOUDWATCH_TEST_$(date -u +%Y%m%dT%H%M%SZ)"

echo "Monitor pod: $MON_POD"
echo "Test ID:     $TEST_ID"

kubectl exec \
  -n "$MON_NS" \
  "$MON_POD" \
  -- python3 -c \
  "import os; fd=os.open('/proc/1/fd/1', os.O_WRONLY); os.write(fd, b'${TEST_ID}\n'); os.close(fd)"

sleep 5

echo
echo "Confirming the test entered the Kubernetes container log..."

kubectl logs \
  -n "$MON_NS" \
  "$MON_POD" \
  --since=2m |
grep -F "$TEST_ID"

echo
echo "TEST LOG GENERATED SUCCESSFULLY"
echo "Wait 60–90 seconds, then refresh:"
echo "/adcb/goldengate/dev/monitor"