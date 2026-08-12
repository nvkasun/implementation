set -euo pipefail

NS="goldengate-dev"

OLD_EFS="fs-05cadf3570f23cd39"

PG="gg-postgresql-repltest-01"
MSSQL="gg-mssql-repltest-01"

EXPECTED_PG_EFS="fs-09bb3373f132d01b0"
EXPECTED_PG_AP="fsap-05b0995fdcd1cf498"

EXPECTED_MSSQL_EFS="fs-03d4beaa58f19be78"
EXPECTED_MSSQL_AP="fsap-07f0c6516b7c6c656"

echo
echo "============================================================"
echo "1. SHARED NAMESPACE MUST STILL EXIST"
echo "============================================================"

kubectl get namespace "$NS"

echo
echo "Namespace $NS exists ✅"

echo
echo "============================================================"
echo "2. OLD ARGO APPLICATIONS MUST BE ABSENT"
echo "============================================================"

for APP in \
  goldengate-dev-oracle-payments-01 \
  goldengate-dev-postgresql-payments-01
do
  if kubectl -n argocd get application "$APP" >/dev/null 2>&1; then
    echo "$APP STILL EXISTS ❌"
    exit 1
  else
    echo "$APP absent ✅"
  fi
done

echo
echo "============================================================"
echo "3. OLD STATEFULSETS MUST BE ABSENT"
echo "============================================================"

for DEP in \
  gg-oracle-payments-01 \
  gg-postgresql-payments-01
do
  if kubectl -n "$NS" get sts "$DEP" >/dev/null 2>&1; then
    echo "$DEP StatefulSet STILL EXISTS ❌"
    exit 1
  else
    echo "$DEP StatefulSet absent ✅"
  fi
done

echo
echo "============================================================"
echo "4. OLD PODS MUST BE ABSENT"
echo "============================================================"

OLD_PODS="$(kubectl -n "$NS" get pods -o name | \
  grep -E 'gg-oracle-payments-01|gg-postgresql-payments-01' || true)"

if [ -n "$OLD_PODS" ]; then
  echo "Old pods still exist ❌"
  echo "$OLD_PODS"
  exit 1
else
  echo "No old runtime pods ✅"
fi

echo
echo "============================================================"
echo "5. OLD PVCs"
echo "============================================================"

for PVC in \
  gg-oracle-payments-01-u02 \
  gg-postgresql-payments-01-u02
do
  if kubectl -n "$NS" get pvc "$PVC" >/dev/null 2>&1; then
    echo "$PVC still exists ⚠️"
    kubectl -n "$NS" get pvc "$PVC" -o wide
  else
    echo "$PVC absent ✅"
  fi
done

echo
echo "============================================================"
echo "6. NEW MANAGED PAIR"
echo "============================================================"

for DEP in "$PG" "$MSSQL"; do
  kubectl -n "$NS" get sts "$DEP" \
    -o jsonpath='{.metadata.name}{" | ready="}{.status.readyReplicas}{"/"}{.spec.replicas}{" | SA="}{.spec.template.spec.serviceAccountName}{"\n"}'
done

echo
echo "============================================================"
echo "7. VERIFY NEW STORAGE IDENTITIES"
echo "============================================================"

for DEP in "$PG" "$MSSQL"; do

  PVC="${DEP}-u02"
  PV="$(kubectl -n "$NS" get pvc "$PVC" -o jsonpath='{.spec.volumeName}')"
  HANDLE="$(kubectl get pv "$PV" -o jsonpath='{.spec.csi.volumeHandle}')"

  EFS="${HANDLE%%::*}"
  AP="${HANDLE##*::}"

  echo
  echo "Deployment : $DEP"
  echo "PVC        : $PVC"
  echo "PV         : $PV"
  echo "Handle     : $HANDLE"
  echo "EFS        : $EFS"
  echo "AP         : $AP"

  if [ "$DEP" = "$PG" ]; then

    [ "$EFS" = "$EXPECTED_PG_EFS" ] \
      && echo "PG EFS-A unchanged ✅" \
      || { echo "PG EFS-A CHANGED ❌"; exit 1; }

    [ "$AP" = "$EXPECTED_PG_AP" ] \
      && echo "PG AP unchanged ✅" \
      || { echo "PG AP CHANGED ❌"; exit 1; }

  else

    [ "$EFS" = "$EXPECTED_MSSQL_EFS" ] \
      && echo "MSSQL EFS-B unchanged ✅" \
      || { echo "MSSQL EFS-B CHANGED ❌"; exit 1; }

    [ "$AP" = "$EXPECTED_MSSQL_AP" ] \
      && echo "MSSQL AP unchanged ✅" \
      || { echo "MSSQL AP CHANGED ❌"; exit 1; }

  fi
done

echo
echo "============================================================"
echo "8. INVENTORY ALL PVs STILL REFERENCING OLD EFS"
echo "============================================================"

FOUND_OLD=0

while read -r PV HANDLE PHASE CLAIM_NS CLAIM_NAME; do

  [ -z "$PV" ] && continue

  case "$HANDLE" in
    "${OLD_EFS}"::*)
      FOUND_OLD=1

      echo
      echo "PV         : $PV"
      echo "Handle     : $HANDLE"
      echo "Phase      : $PHASE"
      echo "Claim      : ${CLAIM_NS}/${CLAIM_NAME}"
      ;;
  esac

done < <(
  kubectl get pv \
    -o custom-columns='PV:.metadata.name,HANDLE:.spec.csi.volumeHandle,PHASE:.status.phase,CLAIM_NS:.spec.claimRef.namespace,CLAIM_NAME:.spec.claimRef.name' \
    --no-headers
)

if [ "$FOUND_OLD" -eq 0 ]; then
  echo "No PV currently references $OLD_EFS"
else
  echo
  echo "Old-EFS PV artifacts remain — expected with Retain."
fi

echo
echo "============================================================"
echo "9. ENSURE NO RUNNING POD USES OLD EFS"
echo "============================================================"

BAD=0

while read -r NAMESPACE POD PVC; do

  [ -z "$PVC" ] && continue

  PV="$(kubectl -n "$NAMESPACE" get pvc "$PVC" \
        -o jsonpath='{.spec.volumeName}' 2>/dev/null || true)"

  [ -z "$PV" ] && continue

  HANDLE="$(kubectl get pv "$PV" \
            -o jsonpath='{.spec.csi.volumeHandle}' 2>/dev/null || true)"

  case "$HANDLE" in
    "${OLD_EFS}"::*)
      echo "ACTIVE OLD-EFS REFERENCE ❌"
      echo "Namespace : $NAMESPACE"
      echo "Pod       : $POD"
      echo "PVC       : $PVC"
      echo "PV        : $PV"
      echo "Handle    : $HANDLE"
      BAD=1
      ;;
  esac

done < <(
  kubectl get pods -A \
    -o jsonpath='{range .items[*]}{range .spec.volumes[?(@.persistentVolumeClaim)]}{@.persistentVolumeClaim.claimName}{"|"}{end}{.metadata.namespace}{"|"}{.metadata.name}{"\n"}{end}' |
  awk -F'|' '
    {
      ns=$(NF-1)
      pod=$NF
      for (i=1;i<=NF-2;i++)
        if ($i!="")
          print ns, pod, $i
    }'
)

if [ "$BAD" -ne 0 ]; then
  echo "At least one running pod still references old EFS ❌"
  exit 1
fi

echo "No running pod references $OLD_EFS ✅"

echo
echo "============================================================"
echo "10. ACTIVE ARGO APPLICATIONS"
echo "============================================================"

kubectl -n argocd get applications.argoproj.io \
  goldengate-dev-platform \
  goldengate-dev-postgresql-repltest-01 \
  goldengate-dev-mssql-repltest-01 \
  goldengate-monitor \
  -o custom-columns='NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status'

echo
echo "============================================================"
echo "POST-RETIREMENT VALIDATION COMPLETE"
echo "============================================================"