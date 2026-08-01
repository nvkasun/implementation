set +e

AWS_REGION="eu-west-1"
EFS_FILE_SYSTEM_ID="fs-05cadf3570f23cd39"

echo
echo "============================================================"
echo "1. CURRENT CLUSTER AND APPLICATION STATE"
echo "============================================================"

aws sts get-caller-identity \
  --query '{Account:Account,Arn:Arn}' \
  --output table 2>&1

kubectl config current-context 2>&1

kubectl get applications.argoproj.io \
  -A \
  -o custom-columns='NAMESPACE:.metadata.namespace,NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status,DESTINATION:.spec.destination.namespace' \
  2>&1

kubectl get namespaces \
  -o custom-columns='NAME:.metadata.name,STATUS:.status.phase,CREATED:.metadata.creationTimestamp' \
  2>&1 |
grep -E 'NAME|goldengate|gg-dev|argocd' || true

echo
echo "============================================================"
echo "2. GOLDENGATE STORAGECLASSES"
echo "============================================================"

kubectl get storageclass \
  -o custom-columns='NAME:.metadata.name,PROVISIONER:.provisioner,RECLAIM_POLICY:.reclaimPolicy,FILESYSTEM_ID:.parameters.fileSystemId,BASE_PATH:.parameters.basePath,CREATED:.metadata.creationTimestamp' \
  2>&1 |
grep -E 'NAME|gg-efs|goldengate' || true

echo
echo "============================================================"
echo "3. ALL GOLDENGATE-RELATED PERSISTENT VOLUMES"
echo "============================================================"

kubectl get pv -o json 2>/dev/null |
jq -r '
  ["PV","STATUS","RECLAIM","STORAGECLASS","CLAIM_NAMESPACE","CLAIM_NAME","CAPACITY","VOLUME_HANDLE"],
  (
    .items[] |
    select(
      ((.spec.storageClassName // "") | contains("gg-efs")) or
      ((.spec.claimRef.namespace // "") | contains("goldengate")) or
      ((.spec.claimRef.namespace // "") | startswith("gg-dev-"))
    ) |
    [
      .metadata.name,
      (.status.phase // ""),
      (.spec.persistentVolumeReclaimPolicy // ""),
      (.spec.storageClassName // ""),
      (.spec.claimRef.namespace // ""),
      (.spec.claimRef.name // ""),
      (.spec.capacity.storage // ""),
      (.spec.csi.volumeHandle // "")
    ]
  ) |
  @tsv
' | column -t -s $'\t'

echo
echo "============================================================"
echo "4. RELEASED OR RETAINED PV DETAILS"
echo "============================================================"

PV_NAMES="$(
  kubectl get pv -o json 2>/dev/null |
  jq -r '
    .items[] |
    select(
      ((.spec.storageClassName // "") | contains("gg-efs")) or
      ((.spec.claimRef.namespace // "") | contains("goldengate")) or
      ((.spec.claimRef.namespace // "") | startswith("gg-dev-"))
    ) |
    .metadata.name
  '
)"

if [ -z "$PV_NAMES" ]; then
  echo "NO GOLDENGATE-RELATED PVs FOUND"
else
  for PV in $PV_NAMES; do
    echo
    echo "----- PV: ${PV} -----"

    kubectl get pv "$PV" \
      -o jsonpath='name={.metadata.name}{"\n"}status={.status.phase}{"\n"}reclaimPolicy={.spec.persistentVolumeReclaimPolicy}{"\n"}storageClass={.spec.storageClassName}{"\n"}claimNamespace={.spec.claimRef.namespace}{"\n"}claimName={.spec.claimRef.name}{"\n"}volumeHandle={.spec.csi.volumeHandle}{"\n"}capacity={.spec.capacity.storage}{"\n"}finalizers={.metadata.finalizers}{"\n"}' \
      2>&1
  done
fi

echo
echo "============================================================"
echo "5. EFS FILESYSTEM STATE"
echo "============================================================"

aws efs describe-file-systems \
  --region "$AWS_REGION" \
  --file-system-id "$EFS_FILE_SYSTEM_ID" \
  --query 'FileSystems[0].{FileSystemId:FileSystemId,State:LifeCycleState,Encrypted:Encrypted,KmsKeyId:KmsKeyId,MountTargets:NumberOfMountTargets,SizeBytes:SizeInBytes.Value,Name:Name}' \
  --output table 2>&1

echo
echo "============================================================"
echo "6. ALL EFS ACCESS POINTS AND ROOT PATHS"
echo "============================================================"

aws efs describe-access-points \
  --region "$AWS_REGION" \
  --file-system-id "$EFS_FILE_SYSTEM_ID" \
  --query 'AccessPoints[].{AccessPointId:AccessPointId,State:LifeCycleState,RootPath:RootDirectory.Path,Name:Tags[?Key==`Name`]|[0].Value}' \
  --output table 2>&1

echo
echo "============================================================"
echo "7. EFS BACKUP AND LIFECYCLE STATE"
echo "============================================================"

aws efs describe-backup-policy \
  --region "$AWS_REGION" \
  --file-system-id "$EFS_FILE_SYSTEM_ID" \
  --output table 2>&1 || true

aws efs describe-lifecycle-configuration \
  --region "$AWS_REGION" \
  --file-system-id "$EFS_FILE_SYSTEM_ID" \
  --output table 2>&1 || true

echo
echo "============================================================"
echo "8. RECENT NAMESPACE AND STORAGE EVENTS"
echo "============================================================"

kubectl get events \
  -A \
  --sort-by=.metadata.creationTimestamp \
  -o custom-columns='TIME:.metadata.creationTimestamp,NAMESPACE:.metadata.namespace,TYPE:.type,REASON:.reason,OBJECT:.involvedObject.kind/.involvedObject.name,MESSAGE:.message' \
  2>&1 |
grep -Ei 'goldengate|gg-oracle|gg-postgresql|payments-ora-to-pg|persistentvolume|persistentvolumeclaim|storageclass|namespace|argocd' |
tail -n 150 || true

echo
echo "============================================================"
echo "9. ARGO CD CONTROLLER DELETION EVIDENCE"
echo "============================================================"

kubectl logs \
  -n argocd \
  statefulset/argocd-application-controller \
  --since=6h \
  2>&1 |
grep -Ei 'goldengate|payments-01|payments-ora-to-pg|delete|deletion|prune|namespace|finalizer' |
tail -n 250 || true

echo
echo "============================================================"
echo "10. CURRENT MONITOR STATE"
echo "============================================================"

kubectl get deployment,pods,service,ingress \
  -n goldengate-monitoring \
  -o wide \
  2>&1

kubectl logs \
  -n goldengate-monitoring \
  deployment/gg-monitor \
  --since=30m \
  2>&1 |
grep -Ei 'deployment_down|unreachable|name or service not known|tick failed|cloudwatch|accessdenied' |
tail -n 80 || true

echo
echo "============================================================"
echo "11. LOCAL REPOSITORY CHECK, WHEN AVAILABLE"
echo "============================================================"

echo "Current directory:"
pwd

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  REPO_ROOT="$(git rev-parse --show-toplevel)"
  echo "Git repository: ${REPO_ROOT}"
  echo "Current commit:  $(git rev-parse HEAD)"

  echo
  echo "--- Expected environment files in current commit ---"

  for FILE in \
    envs/dev/gg-oracle-payments-01/values.yaml \
    envs/dev/gg-postgresql-payments-01/values.yaml \
    envs/dev/payments-ora-to-pg-001/values.yaml
  do
    if git cat-file -e "HEAD:${FILE}" 2>/dev/null; then
      echo "PRESENT: ${FILE}"

      git show "HEAD:${FILE}" 2>/dev/null |
      grep -E '^(deploymentModel:|[[:space:]]+enabled:|[[:space:]]+state:)' |
      head -n 10
    else
      echo "MISSING: ${FILE}"
    fi
  done

  echo
  echo "--- Recent commits affecting environment values ---"

  git log \
    --oneline \
    --name-status \
    -n 8 \
    -- \
    envs/dev/gg-oracle-payments-01 \
    envs/dev/gg-postgresql-payments-01 \
    envs/dev/payments-ora-to-pg-001
else
  echo "Current directory is not a Git working tree."
fi

echo
echo "============================================================"
echo "12. FINAL RECOVERY SAFETY MESSAGE"
echo "============================================================"

echo "Read-only recovery assessment completed."
echo "No resource was created, changed or deleted."
echo
echo "DO NOT RERUN THE GOLDENGATE DEPLOYMENT WORKFLOWS YET."
echo "DO NOT RUN THE IAM WORKFLOW."
echo "DO NOT DELETE ANY PV OR EFS ACCESS POINT."