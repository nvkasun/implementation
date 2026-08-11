set -euo pipefail

NS="goldengate-dev"
PG="gg-postgresql-repltest-01"
MSSQL="gg-mssql-repltest-01"

EXPECTED_PG_EFS="fs-09bb3373f132d01b0"
HISTORICAL_EFS="fs-05cadf3570f23cd39"

echo
echo "============================================================"
echo "1. PLATFORM SERVICE ACCOUNTS"
echo "============================================================"

for sa in gg-runtime-sa gg-oracle-sa gg-postgresql-sa; do
  echo "--- $sa ---"
  kubectl -n "$NS" get sa "$sa" \
    -o jsonpath='{.metadata.name}{" | role="}{.metadata.annotations.eks\.amazonaws\.com/role-arn}{"\n"}'
done

echo
echo "============================================================"
echo "2. STATEFULSET SERVICE ACCOUNTS"
echo "============================================================"

for dep in "$PG" "$MSSQL"; do
  echo "--- $dep ---"
  kubectl -n "$NS" get sts "$dep" \
    -o jsonpath='{.metadata.name}{" | ready="}{.status.readyReplicas}{"/"}{.spec.replicas}{" | serviceAccount="}{.spec.template.spec.serviceAccountName}{"\n"}'
done

echo
echo "============================================================"
echo "3. RUNTIME PODS"
echo "============================================================"

kubectl -n "$NS" get pods -o wide | grep -E \
  'NAME|gg-postgresql-repltest-01|gg-mssql-repltest-01'

echo
echo "============================================================"
echo "4. PVC STATUS"
echo "============================================================"

kubectl -n "$NS" get pvc \
  "${PG}-u02" \
  "${MSSQL}-u02" \
  -o wide

echo
echo "============================================================"
echo "5. PVC -> PV -> EFS/AP RESOLUTION"
echo "============================================================"

for dep in "$PG" "$MSSQL"; do
  PVC="${dep}-u02"
  PV="$(kubectl -n "$NS" get pvc "$PVC" -o jsonpath='{.spec.volumeName}')"
  HANDLE="$(kubectl get pv "$PV" -o jsonpath='{.spec.csi.volumeHandle}')"
  RECLAIM="$(kubectl get pv "$PV" -o jsonpath='{.spec.persistentVolumeReclaimPolicy}')"
  DRIVER="$(kubectl get pv "$PV" -o jsonpath='{.spec.csi.driver}')"
  MOUNTS="$(kubectl get pv "$PV" -o jsonpath='{.spec.mountOptions}')"

  EFS_ID="${HANDLE%%::*}"
  AP_ID="${HANDLE##*::}"

  echo
  echo "Deployment : $dep"
  echo "PVC        : $PVC"
  echo "PV         : $PV"
  echo "Driver     : $DRIVER"
  echo "Handle     : $HANDLE"
  echo "EFS        : $EFS_ID"
  echo "AccessPoint: $AP_ID"
  echo "Reclaim    : $RECLAIM"
  echo "MountOpts  : $MOUNTS"

  if [ "$dep" = "$PG" ]; then
    if [ "$EFS_ID" = "$EXPECTED_PG_EFS" ]; then
      echo "PG EFS     : EXPECTED ✅"
    else
      echo "PG EFS     : UNEXPECTED ❌ expected=$EXPECTED_PG_EFS"
    fi
  fi

  if [ "$dep" = "$MSSQL" ]; then
    if [ "$EFS_ID" = "$EXPECTED_PG_EFS" ]; then
      echo "MSSQL EFS  : ERROR - SHARES PG EFS ❌"
    elif [ "$EFS_ID" = "$HISTORICAL_EFS" ]; then
      echo "MSSQL EFS  : ERROR - USES HISTORICAL EFS ❌"
    else
      echo "MSSQL EFS  : DEDICATED EFS-B ✅"
    fi
  fi
done

echo
echo "============================================================"
echo "6. STORAGE CLASSES"
echo "============================================================"

for sc in \
  "gg-efs-dev-${PG}" \
  "gg-efs-dev-${MSSQL}"
do
  echo "--- $sc ---"
  kubectl get storageclass "$sc" \
    -o jsonpath='{.metadata.name}{" | provisioner="}{.provisioner}{" | reclaim="}{.reclaimPolicy}{" | binding="}{.volumeBindingMode}{" | mounts="}{.mountOptions}{"\n"}'
done

echo
echo "============================================================"
echo "7. /u02 + /u03 INSIDE BOTH RUNTIMES"
echo "============================================================"

for dep in "$PG" "$MSSQL"; do
  POD="$(kubectl -n "$NS" get pods \
    -l app.kubernetes.io/instance="$dep" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"

  if [ -z "$POD" ]; then
    POD="${dep}-0"
  fi

  echo
  echo "--- $dep / $POD ---"

  kubectl -n "$NS" exec "$POD" -- sh -c '
    echo "Directories:"
    ls -ld /u02 /u03

    echo
    echo "Relevant mounts:"
    mount | grep -E " /u02 | /u03 " || true
  '
done

echo
echo "============================================================"
echo "8. ARGO CD APPLICATION STATUS"
echo "============================================================"

kubectl -n argocd get applications.argoproj.io \
  goldengate-dev-platform \
  goldengate-dev-postgresql-repltest-01 \
  goldengate-dev-mssql-repltest-01 \
  goldengate-monitor \
  -o custom-columns='NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status'

echo
echo "============================================================"
echo "9. MSSQL SERVICE PORTS"
echo "============================================================"

kubectl -n "$NS" get svc "$MSSQL" \
  -o jsonpath='{range .spec.ports[*]}{.name}{"="}{.port}{" -> "}{.targetPort}{"\n"}{end}'

echo
echo "============================================================"
echo "10. FINAL RUNTIME SUMMARY"
echo "============================================================"

echo "Expected:"
echo "  MSSQL SA      = gg-runtime-sa"
echo "  MSSQL EFS-B   != $EXPECTED_PG_EFS"
echo "  MSSQL EFS-B   != $HISTORICAL_EFS"
echo "  PG EFS-A      = $EXPECTED_PG_EFS"
echo "  old SAs       = still present"
echo "  Argo apps     = Synced / Healthy"
echo
echo "READ-ONLY VERIFICATION COMPLETE"