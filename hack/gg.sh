set -euo pipefail

STALE_NS="goldengate-monitoring-dev"
CANONICAL_NS="goldengate-monitoring"
PLATFORM_APP="goldengate-dev-platform"
ARGO_NS="argocd"
RUNTIME_NS="goldengate-dev"

echo
echo "============================================================"
echo "1. VERIFY CANONICAL MONITOR NAMESPACE"
echo "============================================================"

kubectl get namespace "$CANONICAL_NS"
kubectl get deployment gg-monitor -n "$CANONICAL_NS" -o wide
kubectl get pods -n "$CANONICAL_NS" -o wide

echo
echo "============================================================"
echo "2. INSPECT STALE NAMESPACE"
echo "============================================================"

kubectl get namespace "$STALE_NS" -o yaml

echo
echo "--- Workloads in stale namespace ---"

kubectl get \
  pods,deployments,statefulsets,daemonsets,services,jobs,cronjobs,pvc,ingress \
  -n "$STALE_NS" \
  --ignore-not-found

WORKLOAD_COUNT="$(
  {
    kubectl get pods -n "$STALE_NS" --no-headers 2>/dev/null || true
    kubectl get deployments -n "$STALE_NS" --no-headers 2>/dev/null || true
    kubectl get statefulsets -n "$STALE_NS" --no-headers 2>/dev/null || true
    kubectl get daemonsets -n "$STALE_NS" --no-headers 2>/dev/null || true
    kubectl get jobs -n "$STALE_NS" --no-headers 2>/dev/null || true
    kubectl get cronjobs -n "$STALE_NS" --no-headers 2>/dev/null || true
    kubectl get pvc -n "$STALE_NS" --no-headers 2>/dev/null || true
    kubectl get ingress -n "$STALE_NS" --no-headers 2>/dev/null || true
  } |
  sed '/^[[:space:]]*$/d' |
  wc -l |
  tr -d ' '
)"

echo "Workload/resource count requiring review: $WORKLOAD_COUNT"

if [ "$WORKLOAD_COUNT" -ne 0 ]; then
  echo
  echo "STOP: goldengate-monitoring-dev is not empty."
  echo "Do not delete it until the listed resources are reviewed."
  exit 1
fi

echo
echo "PASS: stale namespace contains no application workloads or PVCs"

echo
echo "============================================================"
echo "3. DELETE ONLY THE STALE NAMESPACE"
echo "============================================================"

kubectl delete namespace "$STALE_NS" --wait=true --timeout=5m

if kubectl get namespace "$STALE_NS" >/dev/null 2>&1; then
  echo "FAIL: stale namespace still exists"
  exit 1
fi

echo "PASS: stale namespace deleted"

echo
echo "============================================================"
echo "4. REFRESH AND CHECK ARGO CD APPLICATION"
echo "============================================================"

kubectl annotate application "$PLATFORM_APP" \
  -n "$ARGO_NS" \
  argocd.argoproj.io/refresh=hard \
  --overwrite

sleep 15

for i in $(seq 1 30); do
  STATUS="$(
    kubectl get application "$PLATFORM_APP" \
      -n "$ARGO_NS" \
      -o jsonpath='{.status.sync.status}{"|"}{.status.health.status}' \
      2>/dev/null || true
  )"

  echo "Attempt ${i}: ${STATUS:-UNKNOWN}"

  if [ "$STATUS" = "Synced|Healthy" ]; then
    break
  fi

  sleep 10
done

FINAL_STATUS="$(
  kubectl get application "$PLATFORM_APP" \
    -n "$ARGO_NS" \
    -o jsonpath='{.status.sync.status}{"|"}{.status.health.status}' \
    2>/dev/null || true
)"

echo "Final platform status: ${FINAL_STATUS:-UNKNOWN}"

if [ "$FINAL_STATUS" != "Synced|Healthy" ]; then
  echo "FAIL: platform Application is still not Synced and Healthy"
  kubectl describe application "$PLATFORM_APP" -n "$ARGO_NS"
  exit 1
fi

echo "PASS: platform Application is Synced and Healthy"

echo
echo "============================================================"
echo "5. VERIFY FLUENT BIT"
echo "============================================================"

kubectl get daemonset gg-fluent-bit -n "$RUNTIME_NS" -o wide

kubectl rollout status \
  daemonset/gg-fluent-bit \
  -n "$RUNTIME_NS" \
  --timeout=5m

kubectl get pods \
  -n "$RUNTIME_NS" \
  -l app.kubernetes.io/name=fluent-bit \
  -o wide

echo
echo "STALE PLATFORM NAMESPACE CLEANUP PASSED"
echo "ARGO CD PLATFORM APPLICATION IS SYNCED AND HEALTHY"
echo "FLUENT BIT DAEMONSET IS DEPLOYED"