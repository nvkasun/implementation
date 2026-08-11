set -euo pipefail

NS="goldengate-dev"
DEP="gg-mssql-repltest-01"
POD="${DEP}-0"
PVC="${DEP}-u02"

EXPECTED_EFS="fs-03d4beaa58f19be78"
EXPECTED_AP="fsap-07f0c6516b7c6c656"
EXPECTED_SA="gg-runtime-sa"

MARKER="gg-mssql-storage-test-$(date +%s)"
MARKER_PATH="/u02/${MARKER}"

echo
echo "============================================================"
echo "MSSQL /u02 PERSISTENCE TEST"
echo "============================================================"
echo
echo "Deployment : $DEP"
echo "Marker     : $MARKER"
echo

echo "============================================================"
echo "1. PRECONDITION - STATEFULSET"
echo "============================================================"

kubectl -n "$NS" get sts "$DEP" \
  -o jsonpath='{.metadata.name}{" | ready="}{.status.readyReplicas}{"/"}{.spec.replicas}{" | serviceAccount="}{.spec.template.spec.serviceAccountName}{"\n"}'

SA="$(kubectl -n "$NS" get sts "$DEP" \
  -o jsonpath='{.spec.template.spec.serviceAccountName}')"

if [ "$SA" != "$EXPECTED_SA" ]; then
  echo "ERROR: Unexpected ServiceAccount: $SA"
  exit 1
fi

echo "ServiceAccount = $EXPECTED_SA ✅"

echo
echo "============================================================"
echo "2. CAPTURE BEFORE STATE"
echo "============================================================"

OLD_UID="$(kubectl -n "$NS" get pod "$POD" \
  -o jsonpath='{.metadata.uid}')"

PV="$(kubectl -n "$NS" get pvc "$PVC" \
  -o jsonpath='{.spec.volumeName}')"

OLD_HANDLE="$(kubectl get pv "$PV" \
  -o jsonpath='{.spec.csi.volumeHandle}')"

OLD_EFS="${OLD_HANDLE%%::*}"
OLD_AP="${OLD_HANDLE##*::}"

echo "Pod        : $POD"
echo "Old UID    : $OLD_UID"
echo "PVC        : $PVC"
echo "PV         : $PV"
echo "Handle     : $OLD_HANDLE"
echo "EFS        : $OLD_EFS"
echo "AccessPoint: $OLD_AP"

if [ "$OLD_EFS" != "$EXPECTED_EFS" ]; then
  echo "ERROR: Unexpected EFS before test"
  exit 1
fi

if [ "$OLD_AP" != "$EXPECTED_AP" ]; then
  echo "ERROR: Unexpected Access Point before test"
  exit 1
fi

echo "Expected EFS-B confirmed ✅"
echo "Expected Access Point confirmed ✅"

echo
echo "============================================================"
echo "3. VERIFY /u02 MOUNT BEFORE TEST"
echo "============================================================"

kubectl -n "$NS" exec "$POD" -- sh -c '
  mount | grep " /u02 "
  test -d /u02
'

echo "/u02 mounted ✅"

echo
echo "============================================================"
echo "4. WRITE UNIQUE TEST MARKER"
echo "============================================================"

kubectl -n "$NS" exec "$POD" -- \
  sh -c "printf '%s\n' '$MARKER' > '$MARKER_PATH' && sync"

READ_BACK="$(kubectl -n "$NS" exec "$POD" -- \
  sh -c "cat '$MARKER_PATH'")"

if [ "$READ_BACK" != "$MARKER" ]; then
  echo "ERROR: Marker write verification failed"
  exit 1
fi

echo "Marker written and verified ✅"

echo
echo "============================================================"
echo "5. DELETE ONLY MSSQL POD"
echo "============================================================"

kubectl -n "$NS" delete pod "$POD" --wait=true

echo
echo "Waiting for StatefulSet to recreate $POD ..."

kubectl -n "$NS" wait \
  --for=condition=Ready \
  "pod/$POD" \
  --timeout=300s

echo "New pod Ready ✅"

echo
echo "============================================================"
echo "6. CAPTURE AFTER STATE"
echo "============================================================"

NEW_UID="$(kubectl -n "$NS" get pod "$POD" \
  -o jsonpath='{.metadata.uid}')"

NEW_PV="$(kubectl -n "$NS" get pvc "$PVC" \
  -o jsonpath='{.spec.volumeName}')"

NEW_HANDLE="$(kubectl get pv "$NEW_PV" \
  -o jsonpath='{.spec.csi.volumeHandle}')"

NEW_EFS="${NEW_HANDLE%%::*}"
NEW_AP="${NEW_HANDLE##*::}"

NEW_SA="$(kubectl -n "$NS" get pod "$POD" \
  -o jsonpath='{.spec.serviceAccountName}')"

echo "New UID    : $NEW_UID"
echo "PVC        : $PVC"
echo "PV         : $NEW_PV"
echo "Handle     : $NEW_HANDLE"
echo "EFS        : $NEW_EFS"
echo "AccessPoint: $NEW_AP"
echo "SA         : $NEW_SA"

echo
echo "============================================================"
echo "7. VERIFY IDENTITY / STORAGE STABILITY"
echo "============================================================"

[ "$NEW_UID" != "$OLD_UID" ] \
  && echo "Pod UID changed ✅" \
  || { echo "Pod UID DID NOT CHANGE ❌"; exit 1; }

[ "$NEW_PV" = "$PV" ] \
  && echo "PV retained ✅" \
  || { echo "PV CHANGED ❌"; exit 1; }

[ "$NEW_HANDLE" = "$OLD_HANDLE" ] \
  && echo "Volume handle retained ✅" \
  || { echo "Volume handle CHANGED ❌"; exit 1; }

[ "$NEW_EFS" = "$EXPECTED_EFS" ] \
  && echo "EFS-B retained ✅" \
  || { echo "EFS-B CHANGED ❌"; exit 1; }

[ "$NEW_AP" = "$EXPECTED_AP" ] \
  && echo "Access Point retained ✅" \
  || { echo "Access Point CHANGED ❌"; exit 1; }

[ "$NEW_SA" = "$EXPECTED_SA" ] \
  && echo "gg-runtime-sa retained ✅" \
  || { echo "ServiceAccount CHANGED ❌"; exit 1; }

echo
echo "============================================================"
echo "8. VERIFY /u02 REMOUNT"
echo "============================================================"

kubectl -n "$NS" exec "$POD" -- sh -c '
  mount | grep " /u02 "
  test -d /u02
'

echo "/u02 remounted ✅"

echo
echo "============================================================"
echo "9. VERIFY MARKER SURVIVED"
echo "============================================================"

SURVIVED="$(kubectl -n "$NS" exec "$POD" -- \
  sh -c "cat '$MARKER_PATH'")"

if [ "$SURVIVED" != "$MARKER" ]; then
  echo "ERROR: Persistence marker missing or changed"
  exit 1
fi

echo "Marker survived pod recreation ✅"

echo
echo "============================================================"
echo "10. CLEAN TEST MARKER"
echo "============================================================"

kubectl -n "$NS" exec "$POD" -- \
  rm -f "$MARKER_PATH"

if kubectl -n "$NS" exec "$POD" -- \
  test -e "$MARKER_PATH"; then
  echo "ERROR: Marker cleanup failed"
  exit 1
else
  echo "Marker cleaned ✅"
fi

echo
echo "============================================================"
echo "11. FINAL POD / ARGO STATUS"
echo "============================================================"

kubectl -n "$NS" get pod "$POD" -o wide

kubectl -n argocd get applications.argoproj.io \
  goldengate-dev-platform \
  goldengate-dev-postgresql-repltest-01 \
  goldengate-dev-mssql-repltest-01 \
  goldengate-monitor \
  -o custom-columns='NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status'

echo
echo "============================================================"
echo "MSSQL PERSISTENCE TEST RESULT"
echo "============================================================"

echo "Pod recreated                    ✅"
echo "Pod UID changed                  ✅"
echo "ServiceAccount = gg-runtime-sa   ✅"
echo "PVC retained                     ✅"
echo "PV retained                      ✅"
echo "EFS-B retained                   ✅"
echo "Access Point retained            ✅"
echo "/u02 remounted                   ✅"
echo "Marker survived                  ✅"
echo "Test marker cleaned              ✅"
echo
echo "MSSQL /u02 PERSISTENCE TEST PASSED"