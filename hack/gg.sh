set -euo pipefail

OLD_EFS="fs-05cadf3570f23cd39"

PG_EFS="fs-09bb3373f132d01b0"
MSSQL_EFS="fs-03d4beaa58f19be78"

OLD_SC="efs-sc"

echo
echo "============================================================"
echo "LEGACY STORAGE CLEANUP"
echo "============================================================"

echo
echo "1. DISCOVER PVs REFERENCING DELETED EFS"
echo "------------------------------------------------------------"

mapfile -t OLD_PVS < <(
  kubectl get pv \
    -o custom-columns='NAME:.metadata.name,HANDLE:.spec.csi.volumeHandle' \
    --no-headers |
  awk -v fs="$OLD_EFS" '$2 ~ ("^" fs "::") {print $1}'
)

echo "Found ${#OLD_PVS[@]} PV(s)"

printf '%s\n' "${OLD_PVS[@]}"

if [ "${#OLD_PVS[@]}" -eq 0 ]; then
  echo "No stale PVs found."
else

  echo
  echo "2. FAIL-CLOSED VALIDATION"
  echo "------------------------------------------------------------"

  for PV in "${OLD_PVS[@]}"; do

    PHASE="$(kubectl get pv "$PV" -o jsonpath='{.status.phase}')"
    HANDLE="$(kubectl get pv "$PV" -o jsonpath='{.spec.csi.volumeHandle}')"

    echo
    echo "PV     : $PV"
    echo "Phase  : $PHASE"
    echo "Handle : $HANDLE"

    if [ "$PHASE" != "Released" ]; then
      echo "ERROR: $PV is not Released. Refusing cleanup."
      exit 1
    fi

    case "$HANDLE" in
      "${OLD_EFS}"::*)
        ;;
      *)
        echo "ERROR: $PV does not belong to old EFS."
        exit 1
        ;;
    esac

    case "$HANDLE" in
      "${PG_EFS}"::*|"${MSSQL_EFS}"::*)
        echo "ERROR: $PV references ACTIVE managed storage."
        exit 1
        ;;
    esac

    CLAIM_NS="$(kubectl get pv "$PV" \
      -o jsonpath='{.spec.claimRef.namespace}' 2>/dev/null || true)"

    CLAIM_NAME="$(kubectl get pv "$PV" \
      -o jsonpath='{.spec.claimRef.name}' 2>/dev/null || true)"

    if [ -n "$CLAIM_NS" ] && [ -n "$CLAIM_NAME" ]; then
      if kubectl -n "$CLAIM_NS" get pvc "$CLAIM_NAME" >/dev/null 2>&1; then
        echo "ERROR: PVC $CLAIM_NS/$CLAIM_NAME still exists."
        exit 1
      fi
    fi

    echo "Safe stale PV ✅"
  done

  echo
  echo "3. FINAL ACTIVE STORAGE SAFETY CHECK"
  echo "------------------------------------------------------------"

  for DEP in gg-postgresql-repltest-01 gg-mssql-repltest-01; do

    PVC="${DEP}-u02"
    PV="$(kubectl -n goldengate-dev get pvc "$PVC" \
      -o jsonpath='{.spec.volumeName}')"

    HANDLE="$(kubectl get pv "$PV" \
      -o jsonpath='{.spec.csi.volumeHandle}')"

    echo "$DEP → $HANDLE"

    case "$HANDLE" in
      "${OLD_EFS}"::*)
        echo "ERROR: ACTIVE runtime unexpectedly uses old EFS."
        exit 1
        ;;
    esac
  done

  echo "Active runtime storage safe ✅"

  echo
  echo "4. DELETE ONLY VERIFIED RELEASED OLD-EFS PVs"
  echo "------------------------------------------------------------"

  for PV in "${OLD_PVS[@]}"; do
    echo "Deleting stale PV: $PV"
    kubectl delete pv "$PV"
  done
fi

echo
echo "5. VERIFY OLD STORAGECLASS"
echo "------------------------------------------------------------"

if kubectl get storageclass "$OLD_SC" >/dev/null 2>&1; then

  SC_FS="$(kubectl get storageclass "$OLD_SC" \
    -o jsonpath='{.parameters.fileSystemId}')"

  echo "$OLD_SC → $SC_FS"

  if [ "$SC_FS" != "$OLD_EFS" ]; then
    echo "ERROR: $OLD_SC no longer points to expected deleted EFS."
    exit 1
  fi

  PVC_USERS="$(kubectl get pvc -A \
    -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,SC:.spec.storageClassName' \
    --no-headers |
    awk -v sc="$OLD_SC" '$3 == sc {print}')"

  if [ -n "$PVC_USERS" ]; then
    echo "ERROR: PVCs still reference $OLD_SC:"
    echo "$PVC_USERS"
    exit 1
  fi

  echo "Deleting stale StorageClass $OLD_SC ..."
  kubectl delete storageclass "$OLD_SC"

else
  echo "$OLD_SC already absent."
fi

echo
echo "6. POST-CLEANUP VERIFICATION"
echo "------------------------------------------------------------"

REMAINING="$(
  kubectl get pv \
    -o custom-columns='NAME:.metadata.name,HANDLE:.spec.csi.volumeHandle' \
    --no-headers |
  awk -v fs="$OLD_EFS" '$2 ~ ("^" fs "::")'
)"

if [ -n "$REMAINING" ]; then
  echo "ERROR: PVs still reference deleted EFS:"
  echo "$REMAINING"
  exit 1
fi

if kubectl get storageclass "$OLD_SC" >/dev/null 2>&1; then
  echo "ERROR: $OLD_SC still exists."
  exit 1
fi

echo "No PV references deleted EFS ✅"
echo "Old StorageClass removed ✅"

echo
echo "7. ACTIVE RUNTIMES"
echo "------------------------------------------------------------"

kubectl -n goldengate-dev get sts \
  gg-postgresql-repltest-01 \
  gg-mssql-repltest-01 \
  -o custom-columns='NAME:.metadata.name,READY:.status.readyReplicas,SA:.spec.template.spec.serviceAccountName'

echo
echo "LEGACY STORAGE CLEANUP PASSED"