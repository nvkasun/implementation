set -euo pipefail

NS="goldengate-dev"
PG="gg-postgresql-repltest-01"
MSSQL="gg-mssql-repltest-01"

EXPECTED_PG_EFS="fs-09bb3373f132d01b0"
EXPECTED_PG_AP="fsap-05b0995fdcd1cf498"

EXPECTED_MSSQL_EFS="fs-03d4beaa58f19be78"
EXPECTED_MSSQL_AP="fsap-07f0c6516b7c6c656"

echo
echo "============================================================"
echo "1. FINAL RUNTIME SERVICE ACCOUNTS"
echo "============================================================"

for dep in "$PG" "$MSSQL"; do
  kubectl -n "$NS" get sts "$dep" \
    -o jsonpath='{.metadata.name}{" | ready="}{.status.readyReplicas}{"/"}{.spec.replicas}{" | serviceAccount="}{.spec.template.spec.serviceAccountName}{"\n"}'
done

echo
echo "============================================================"
echo "2. PODS"
echo "============================================================"

kubectl -n "$NS" get pods -o wide | grep -E \
  'NAME|gg-postgresql-repltest-01|gg-mssql-repltest-01'

echo
echo "============================================================"
echo "3. VERIFY STORAGE IDENTITIES"
echo "============================================================"

for dep in "$PG" "$MSSQL"; do

  PVC="${dep}-u02"
  PV="$(kubectl -n "$NS" get pvc "$PVC" -o jsonpath='{.spec.volumeName}')"
  HANDLE="$(kubectl get pv "$PV" -o jsonpath='{.spec.csi.volumeHandle}')"

  EFS="${HANDLE%%::*}"
  AP="${HANDLE##*::}"

  echo
  echo "Deployment : $dep"
  echo "PVC        : $PVC"
  echo "PV         : $PV"
  echo "Handle     : $HANDLE"
  echo "EFS        : $EFS"
  echo "AP         : $AP"

  if [ "$dep" = "$PG" ]; then
    [ "$EFS" = "$EXPECTED_PG_EFS" ] \
      && echo "PG EFS unchanged ✅" \
      || echo "PG EFS CHANGED ❌"

    [ "$AP" = "$EXPECTED_PG_AP" ] \
      && echo "PG AP unchanged ✅" \
      || echo "PG AP CHANGED ❌"
  fi

  if [ "$dep" = "$MSSQL" ]; then
    [ "$EFS" = "$EXPECTED_MSSQL_EFS" ] \
      && echo "MSSQL EFS unchanged ✅" \
      || echo "MSSQL EFS CHANGED ❌"

    [ "$AP" = "$EXPECTED_MSSQL_AP" ] \
      && echo "MSSQL AP unchanged ✅" \
      || echo "MSSQL AP CHANGED ❌"
  fi
done

echo
echo "============================================================"
echo "4. /u02 MOUNTS"
echo "============================================================"

for dep in "$PG" "$MSSQL"; do

  POD="${dep}-0"

  echo
  echo "--- $dep ---"

  kubectl -n "$NS" exec "$POD" -- sh -c '
    mount | grep " /u02 "
    ls -ld /u02 /u03
  '
done

echo
echo "============================================================"
echo "5. ARGO"
echo "============================================================"

kubectl -n argocd get applications.argoproj.io \
  goldengate-dev-platform \
  goldengate-dev-postgresql-repltest-01 \
  goldengate-dev-mssql-repltest-01 \
  goldengate-monitor \
  -o custom-columns='NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status'

echo
echo "=========================================================.==="
echo "EXPECTED FINAL IDENTITY"
echo "============================================================"

echo "PG    -> gg-runtime-sa"
echo "MSSQL -> gg-runtime-sa"