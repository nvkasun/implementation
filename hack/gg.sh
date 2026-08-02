set -euo pipefail

NS="goldengate-dev"
DS="gg-fluent-bit"

echo
echo "============================================================"
echo "1. DISCOVER THE CONFIGMAP USED BY THE DAEMONSET"
echo "============================================================"

CONFIGMAP_NAME="$(
  kubectl get daemonset "$DS" \
    -n "$NS" \
    -o jsonpath='{.spec.template.spec.volumes[?(@.name=="fluent-bit-config")].configMap.name}'
)"

echo "ConfigMap used by DaemonSet: ${CONFIGMAP_NAME:-NOT_FOUND}"

if [ -z "$CONFIGMAP_NAME" ]; then
  echo "FAIL: Could not derive Fluent Bit ConfigMap from the DaemonSet"
  exit 1
fi

kubectl get configmap "$CONFIGMAP_NAME" -n "$NS"

echo
echo "Available ConfigMap data keys:"

kubectl get configmap "$CONFIGMAP_NAME" \
  -n "$NS" \
  -o go-template='{{range $key, $value := .data}}{{printf "%s\n" $key}}{{end}}'

echo
echo "============================================================"
echo "2. READ THE ACTUAL FLUENT BIT CONFIGURATION"
echo "============================================================"

CONFIG="$(
  kubectl get configmap "$CONFIGMAP_NAME" \
    -n "$NS" \
    -o json |
  jq -r '.data["fluent-bit.conf"] // empty'
)"

if [ -z "$CONFIG" ]; then
  echo "FAIL: fluent-bit.conf was not found or was empty"
  exit 1
fi

printf '%s\n' "$CONFIG"

echo
echo "============================================================"
echo "3. VALIDATE REQUIRED CONFIGURATION"
echo "============================================================"

FAILURES=0

pass() {
  echo "PASS: $*"
}

fail() {
  echo "FAIL: $*" >&2
  FAILURES=$((FAILURES + 1))
}

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
grep -Fq '/adcb/goldengate/dev/runtime' \
  && pass "Runtime CloudWatch log group is configured" \
  || fail "Runtime CloudWatch log group is missing"

printf '%s\n' "$CONFIG" |
grep -Fq '/adcb/goldengate/dev/monitor' \
  && pass "Monitor CloudWatch log group is configured" \
  || fail "Monitor CloudWatch log group is missing"

OUTPUT_LIMIT_COUNT="$(
  printf '%s\n' "$CONFIG" |
  grep -Ec 'storage\.total_limit_size[[:space:]]+128M' || true
)"

[ "$OUTPUT_LIMIT_COUNT" -eq 2 ] \
  && pass "Both CloudWatch outputs have bounded 128M queues" \
  || fail "Expected two output queue limits; found ${OUTPUT_LIMIT_COUNT}"

AUTO_CREATE_COUNT="$(
  printf '%s\n' "$CONFIG" |
  grep -Ec 'auto_create_group[[:space:]]+false' || true
)"

[ "$AUTO_CREATE_COUNT" -eq 2 ] \
  && pass "Both outputs prohibit automatic log-group creation" \
  || fail "Expected two auto_create_group false entries; found ${AUTO_CREATE_COUNT}"

echo
echo "============================================================"
echo "4. RESULT"
echo "============================================================"

if [ "$FAILURES" -eq 0 ]; then
  echo
  echo "PHASE 6A FLUENT BIT CONFIGURATION VALIDATION PASSED"
  exit 0
fi

echo
echo "CONFIGURATION VALIDATION COMPLETED WITH ${FAILURES} FAILURE(S)"
exit 1