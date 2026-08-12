set -euo pipefail

OLD_EFS="fs-05cadf3570f23cd39"

echo
echo "============================================================"
echo "1. CURRENT GOLDENGATE RUNTIMES"
echo "============================================================"

kubectl -n goldengate-dev get sts \
  -o custom-columns='NAME:.metadata.name,READY:.status.readyReplicas,SA:.spec.template.spec.serviceAccountName'

echo
echo "============================================================"
echo "2. ALL PODS USING LEGACY SERVICE ACCOUNTS"
echo "============================================================"

LEGACY_PODS="$(
  kubectl get pods -A \
    -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,SA:.spec.serviceAccountName' \
    --no-headers |
  awk '$3=="gg-oracle-sa" || $3=="gg-postgresql-sa" || $3=="ogg-oracle-sa"'
)"

if [ -n "$LEGACY_PODS" ]; then
  echo "Legacy-SA consumers found:"
  echo "$LEGACY_PODS"
else
  echo "No pod uses gg-oracle-sa / gg-postgresql-sa / ogg-oracle-sa ✅"
fi

echo
echo "============================================================"
echo "3. LEGACY SERVICE ACCOUNT OBJECTS"
echo "============================================================"

kubectl get serviceaccounts -A \
  -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,ROLE:.metadata.annotations.eks\.amazonaws\.com/role-arn' |
grep -E 'NAME|gg-oracle-sa|gg-postgresql-sa|ogg-oracle-sa|gg-runtime-sa' || true

echo
echo "============================================================"
echo "4. ALL PVs STILL POINTING TO DELETED OLD EFS"
echo "============================================================"

kubectl get pv \
  -o custom-columns='PV:.metadata.name,PHASE:.status.phase,HANDLE:.spec.csi.volumeHandle,CLAIM_NS:.spec.claimRef.namespace,CLAIM:.spec.claimRef.name' \
  --no-headers |
awk -v fs="$OLD_EFS" '$3 ~ ("^" fs "::") || $3 == fs'

echo
echo "============================================================"
echo "5. STORAGECLASSES STILL POINTING TO OLD EFS"
echo "============================================================"

for SC in $(kubectl get storageclass -o name); do
  FS="$(kubectl get "$SC" -o jsonpath='{.parameters.fileSystemId}' 2>/dev/null || true)"

  if [ "$FS" = "$OLD_EFS" ]; then
    kubectl get "$SC" \
      -o custom-columns='NAME:.metadata.name,PROVISIONER:.provisioner,RECLAIM:.reclaimPolicy,FS:.parameters.fileSystemId'
  fi
done

echo
echo "============================================================"
echo "6. OLD NAMESPACES / RESOURCES"
echo "============================================================"

kubectl get namespaces \
  -o custom-columns='NAME:.metadata.name' |
grep -E '(^NAME$|^ogg$|^gg-dev-|goldengate)' || true

echo
echo "============================================================"
echo "7. ACTIVE MANAGED STORAGE — MUST REMAIN"
echo "============================================================"

for DEP in \
  gg-postgresql-repltest-01 \
  gg-mssql-repltest-01
do
  PVC="${DEP}-u02"

  PV="$(kubectl -n goldengate-dev get pvc "$PVC" \
    -o jsonpath='{.spec.volumeName}')"

  HANDLE="$(kubectl get pv "$PV" \
    -o jsonpath='{.spec.csi.volumeHandle}')"

  echo "$DEP"
  echo "  PVC    = $PVC"
  echo "  PV     = $PV"
  echo "  Handle = $HANDLE"
done

echo
echo "============================================================"
echo "8. ACTIVE ARGO"
echo "============================================================"

kubectl -n argocd get applications.argoproj.io \
  goldengate-dev-platform \
  goldengate-dev-postgresql-repltest-01 \
  goldengate-dev-mssql-repltest-01 \
  goldengate-monitor \
  -o custom-columns='NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status'

echo
echo "============================================================"
echo "READ-ONLY CLEANUP INVENTORY COMPLETE"
echo "============================================================"