set -euo pipefail

NS="goldengate-dev"

OLD_ORACLE="gg-oracle-payments-01"
OLD_PG="gg-postgresql-payments-01"
NEW_PG="gg-postgresql-repltest-01"
NEW_MSSQL="gg-mssql-repltest-01"

echo
echo "============================================================"
echo "1. CURRENT ARGO APPLICATIONS"
echo "============================================================"

kubectl -n argocd get applications.argoproj.io \
  goldengate-dev-oracle-payments-01 \
  goldengate-dev-postgresql-payments-01 \
  goldengate-dev-postgresql-repltest-01 \
  goldengate-dev-mssql-repltest-01 \
  goldengate-dev-platform \
  goldengate-monitor \
  -o custom-columns='NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status'

echo
echo "============================================================"
echo "2. CURRENT RUNTIME STATEFULSETS"
echo "============================================================"

kubectl -n "$NS" get sts \
  "$OLD_ORACLE" \
  "$OLD_PG" \
  "$NEW_PG" \
  "$NEW_MSSQL" \
  -o custom-columns='NAME:.metadata.name,READY:.status.readyReplicas,DESIRED:.spec.replicas,SA:.spec.template.spec.serviceAccountName'

echo
echo "============================================================"
echo "3. OLD PVC -> PV -> EFS/AP INVENTORY"
echo "============================================================"

for dep in "$OLD_ORACLE" "$OLD_PG"; do
  PVC="${dep}-u02"

  echo
  echo "--- $dep ---"

  if ! kubectl -n "$NS" get pvc "$PVC" >/dev/null 2>&1; then
    echo "PVC not found: $PVC"
    continue
  fi

  PV="$(kubectl -n "$NS" get pvc "$PVC" -o jsonpath='{.spec.volumeName}')"
  HANDLE="$(kubectl get pv "$PV" -o jsonpath='{.spec.csi.volumeHandle}')"

  echo "PVC    : $PVC"
  echo "PV     : $PV"
  echo "Handle : $HANDLE"
  echo "EFS    : ${HANDLE%%::*}"
  echo "AP     : ${HANDLE##*::}"
done

echo
echo "============================================================"
echo "4. NEW PAIR MUST REMAIN HEALTHY"
echo "============================================================"

for dep in "$NEW_PG" "$NEW_MSSQL"; do
  kubectl -n "$NS" get sts "$dep" \
    -o jsonpath='{.metadata.name}{" | ready="}{.status.readyReplicas}{"/"}{.spec.replicas}{" | SA="}{.spec.template.spec.serviceAccountName}{"\n"}'
done

echo
echo "PRE-DELETION INVENTORY COMPLETE"