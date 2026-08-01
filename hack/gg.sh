set -o pipefail
set -u

INVENTORY_SCRIPT="phase5b2-pre-retirement-inventory.sh"
OUTPUT_FILE="phase5b2-pre-retirement-inventory-output.txt"

LEGACY_APP="goldengate-payments-ora-to-pg-001"
LEGACY_NAMESPACE="gg-dev-payments-ora-to-pg-001"
LEGACY_STORAGECLASS="gg-efs-dev-payments-ora-to-pg-001"

RUNTIME_NAMESPACE="goldengate-dev"
MONITOR_NAMESPACE="goldengate-monitoring"
ARGOCD_NAMESPACE="argocd"

section() {
  echo
  echo "============================================================"
  echo "$1"
  echo "============================================================"
}

section "1. RUN READ-ONLY INVENTORY"

chmod +x "$INVENTORY_SCRIPT"

bash "./${INVENTORY_SCRIPT}" 2>&1 | tee "$OUTPUT_FILE"
INVENTORY_STATUS=${PIPESTATUS[0]}

echo
echo "Inventory script exit code: ${INVENTORY_STATUS}"

LATEST_DIR="$(
  ls -1dt phase5b2-legacy-inventory-* 2>/dev/null |
  grep -v '\.tar\.gz$' |
  head -n 1
)"

LATEST_ARCHIVE="$(
  ls -1t phase5b2-legacy-inventory-*.tar.gz 2>/dev/null |
  head -n 1
)"

echo "Evidence directory: ${LATEST_DIR:-NOT_FOUND}"
echo "Evidence archive:   ${LATEST_ARCHIVE:-NOT_FOUND}"

section "2. INVENTORY FINAL RESULT"

tail -n 35 "$OUTPUT_FILE"

section "3. ARGO CD APPLICATION STATUS"

kubectl get application \
  goldengate-dev-oracle-payments-01 \
  goldengate-dev-postgresql-payments-01 \
  goldengate-monitor \
  "$LEGACY_APP" \
  -n "$ARGOCD_NAMESPACE" \
  -o custom-columns='NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status,DESTINATION:.spec.destination.namespace,FINALIZERS:.metadata.finalizers'

section "4. LEGACY KUBERNETES RESOURCES"

kubectl get statefulsets,pods,services,ingress,pvc,serviceaccounts \
  -n "$LEGACY_NAMESPACE" \
  -o wide

echo
echo "--- Legacy pod containers and images ---"

kubectl get pods \
  -n "$LEGACY_NAMESPACE" \
  -o custom-columns='POD:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[*].ready,CONTAINERS:.spec.containers[*].name,IMAGES:.spec.containers[*].image,IMAGE_IDS:.status.containerStatuses[*].imageID,NODE:.spec.nodeName'

echo
echo "--- SecretProviderClasses ---"

kubectl get secretproviderclass \
  -n "$LEGACY_NAMESPACE" \
  -o custom-columns='NAME:.metadata.name,PROVIDER:.spec.provider' \
  2>/dev/null || true

section "5. LEGACY PVC, PV, STORAGECLASS AND EFS"

kubectl get pvc \
  -n "$LEGACY_NAMESPACE" \
  -o custom-columns='PVC:.metadata.name,STATUS:.status.phase,VOLUME:.spec.volumeName,STORAGECLASS:.spec.storageClassName,CAPACITY:.status.capacity.storage,ACCESS_MODES:.spec.accessModes'

PV_NAMES="$(
  kubectl get pvc \
    -n "$LEGACY_NAMESPACE" \
    -o jsonpath='{range .items[*]}{.spec.volumeName}{"\n"}{end}' |
  sed '/^$/d'
)"

for PV in $PV_NAMES; do
  echo
  echo "--- PV: ${PV} ---"

  kubectl get pv "$PV" \
    -o custom-columns='PV:.metadata.name,STATUS:.status.phase,RECLAIM_POLICY:.spec.persistentVolumeReclaimPolicy,STORAGECLASS:.spec.storageClassName,CAPACITY:.spec.capacity.storage,VOLUME_HANDLE:.spec.csi.volumeHandle'
done

echo
echo "--- Legacy StorageClass ---"

kubectl get storageclass "$LEGACY_STORAGECLASS" \
  -o jsonpath='name={.metadata.name}{"\n"}provisioner={.provisioner}{"\n"}reclaimPolicy={.reclaimPolicy}{"\n"}fileSystemId={.parameters.fileSystemId}{"\n"}basePath={.parameters.basePath}{"\n"}subPathPattern={.parameters.subPathPattern}{"\n"}ensureUniqueDirectory={.parameters.ensureUniqueDirectory}{"\n"}'

EFS_FS_IDS="$(
  {
    kubectl get storageclass "$LEGACY_STORAGECLASS" \
      -o jsonpath='{.parameters.fileSystemId}' 2>/dev/null
    echo

    for PV in $PV_NAMES; do
      kubectl get pv "$PV" \
        -o jsonpath='{.spec.csi.volumeHandle}' 2>/dev/null
      echo
    done
  } |
  grep -oE 'fs-[0-9a-f]+' |
  sort -u
)"

EFS_AP_IDS="$(
  for PV in $PV_NAMES; do
    kubectl get pv "$PV" \
      -o jsonpath='{.spec.csi.volumeHandle}' 2>/dev/null
    echo
  done |
  grep -oE 'fsap-[0-9a-f]+' |
  sort -u
)"

for FS_ID in $EFS_FS_IDS; do
  echo
  echo "--- EFS filesystem: ${FS_ID} ---"

  aws efs describe-file-systems \
    --region eu-west-1 \
    --file-system-id "$FS_ID" \
    --query 'FileSystems[0].{FileSystemId:FileSystemId,LifeCycleState:LifeCycleState,Encrypted:Encrypted,KmsKeyId:KmsKeyId,NumberOfMountTargets:NumberOfMountTargets,Name:Name}' \
    --output table || true
done

for AP_ID in $EFS_AP_IDS; do
  echo
  echo "--- EFS access point: ${AP_ID} ---"

  aws efs describe-access-points \
    --region eu-west-1 \
    --access-point-id "$AP_ID" \
    --query 'AccessPoints[0].{AccessPointId:AccessPointId,FileSystemId:FileSystemId,LifeCycleState:LifeCycleState,RootPath:RootDirectory.Path,PosixUser:PosixUser,Tags:Tags}' \
    --output table || true
done

section "6. LEGACY INGRESS, ALB AND ROUTE 53"

kubectl get ingress \
  -n "$LEGACY_NAMESPACE" \
  -o custom-columns='NAME:.metadata.name,CLASS:.spec.ingressClassName,HOSTS:.spec.rules[*].host,ALB:.status.loadBalancer.ingress[*].hostname'

echo
echo "--- Important Ingress annotations ---"

kubectl get ingress \
  -n "$LEGACY_NAMESPACE" \
  -o json |
jq -r '
  .items[] |
  "Ingress=\(.metadata.name)",
  "Hosts=\([.spec.rules[].host] | join(","))",
  "GroupName=\(.metadata.annotations["alb.ingress.kubernetes.io/group.name"] // "<none>")",
  "GroupOrder=\(.metadata.annotations["alb.ingress.kubernetes.io/group.order"] // "<none>")",
  "CertificateArn=\(.metadata.annotations["alb.ingress.kubernetes.io/certificate-arn"] // "<none>")",
  "Scheme=\(.metadata.annotations["alb.ingress.kubernetes.io/scheme"] // "<none>")",
  ""
'

LEGACY_HOSTS="$(
  kubectl get ingress \
    -n "$LEGACY_NAMESPACE" \
    -o jsonpath='{range .items[*].spec.rules[*]}{.host}{"\n"}{end}' |
  sed '/^$/d' |
  sort -u
)"

for HOST in $LEGACY_HOSTS; do
  echo
  echo "--- DNS host: ${HOST} ---"

  HOSTED_ZONE_ID="$(
    aws route53 list-hosted-zones-by-name \
      --dns-name "$HOST" \
      --query 'HostedZones[0].Id' \
      --output text 2>/dev/null |
    sed 's#^/hostedzone/##'
  )"

  echo "Hosted zone ID: ${HOSTED_ZONE_ID:-NOT_FOUND}"

  if [ -n "${HOSTED_ZONE_ID:-}" ] &&
     [ "$HOSTED_ZONE_ID" != "None" ]; then

    aws route53 list-resource-record-sets \
      --hosted-zone-id "$HOSTED_ZONE_ID" \
      --query "ResourceRecordSets[?Name=='${HOST}.']" \
      --output table || true
  fi
done

section "7. REFERENCED SECRETS MANAGER METADATA"

SECRET_NAMES="$(
  kubectl get secretproviderclass \
    -n "$LEGACY_NAMESPACE" \
    -o json 2>/dev/null |
  jq -r '.items[].spec.parameters.objects // empty' |
  sed -nE 's/^[[:space:]]*-[[:space:]]*objectName:[[:space:]]*"?([^"]+)"?[[:space:]]*$/\1/p' |
  sort -u
)"

if [ -z "$SECRET_NAMES" ]; then
  echo "No SecretProviderClass objectName values discovered."
else
  for SECRET_NAME in $SECRET_NAMES; do
    echo
    echo "--- Secret metadata: ${SECRET_NAME} ---"

    aws secretsmanager describe-secret \
      --region eu-west-1 \
      --secret-id "$SECRET_NAME" \
      --query '{Name:Name,ARN:ARN,KmsKeyId:KmsKeyId,LastChangedDate:LastChangedDate,DeletedDate:DeletedDate}' \
      --output table || true
  done
fi

echo
echo "No secret values were requested or printed."

section "8. LEGACY OBSERVER IMAGE AND ECR"

OBSERVER_ROWS="$(
  kubectl get pods \
    -n "$LEGACY_NAMESPACE" \
    -o json |
  jq -r '
    .items[] as $pod |
    ($pod.spec.containers // [])[] as $container |
    select(
      ($container.name | ascii_downcase) |
      contains("observer")
    ) |
    [
      $pod.metadata.name,
      $container.name,
      $container.image,
      (
        ($pod.status.containerStatuses // []) |
        map(select(.name == $container.name)) |
        .[0].imageID // ""
      )
    ] |
    @tsv
  '
)"

printf 'POD\tCONTAINER\tIMAGE\tIMAGE_ID\n%s\n' "$OBSERVER_ROWS"

while IFS=$'\t' read -r POD_NAME CONTAINER_NAME IMAGE IMAGE_ID; do
  [ -n "${IMAGE:-}" ] || continue

  REGISTRY="${IMAGE%%/*}"
  IMAGE_PATH="${IMAGE#*/}"
  REPOSITORY="${IMAGE_PATH%%:*}"
  TAG="${IMAGE_PATH##*:}"
  REGISTRY_ID="${REGISTRY%%.*}"

  echo
  echo "--- ECR observer image ---"
  echo "Registry account: ${REGISTRY_ID}"
  echo "Repository:       ${REPOSITORY}"
  echo "Tag:              ${TAG}"
  echo "Runtime image ID: ${IMAGE_ID}"

  aws ecr describe-images \
    --region eu-west-1 \
    --registry-id "$REGISTRY_ID" \
    --repository-name "$REPOSITORY" \
    --image-ids "imageTag=${TAG}" \
    --query 'imageDetails[0].{Digest:imageDigest,Tags:imageTags,PushedAt:imagePushedAt,ScanStatus:imageScanStatus.status}' \
    --output table || true

done <<< "$OBSERVER_ROWS"

section "9. DYNAMODB LEGACY AND CANONICAL PARTITIONS"

for PIPELINE in \
  gg-payments-ora-to-pg-001-source \
  gg-payments-ora-to-pg-001-target \
  gg-oracle-payments-01 \
  gg-postgresql-payments-01
do
  echo
  echo "--- DynamoDB partition: ${PIPELINE} ---"

  DDB_JSON="$(
    aws dynamodb query \
      --region eu-west-1 \
      --table-name gg-eks-pipeline \
      --key-condition-expression '#pk = :pk' \
      --expression-attribute-names '{"#pk":"pipeline"}' \
      --expression-attribute-values "{\":pk\":{\"S\":\"${PIPELINE}\"}}" \
      --consistent-read \
      --output json 2>/dev/null
  )"

  if [ -z "$DDB_JSON" ]; then
    echo "Unable to query this partition."
    continue
  fi

  echo "$DDB_JSON" |
  jq -r '
    "Count=\(.Count)",
    (
      .Items[]? |
      "recordType=\(.recordType.S // "<missing>") " +
      "status=\(.status.S // .effectiveStatus.S // "<none>") " +
      "updatedAt=\(.updatedAt.S // .lastUpdated.S // .timestamp.S // "<none>")"
    )
  '
done

section "10. CLOUDWATCH METRIC INVENTORY"

CW_JSON="$(
  aws cloudwatch list-metrics \
    --region eu-west-1 \
    --namespace GoldenGate/Pipelines \
    --output json 2>/dev/null
)"

if [ -z "$CW_JSON" ]; then
  echo "Unable to list GoldenGate/Pipelines metrics."
else
  echo "$CW_JSON" |
  jq -r '
    .Metrics |
    map(
      select(
        (tostring | contains("payments-ora-to-pg-001")) or
        (tostring | contains("gg-oracle-payments-01")) or
        (tostring | contains("gg-postgresql-payments-01"))
      )
    ) |
    sort_by(.MetricName)[] |
    "\(.MetricName) | " +
    (
      [.Dimensions[]? | "\(.Name)=\(.Value)"] |
      join(", ")
    )
  '
fi

section "11. CANONICAL RUNTIME AND MONITOR HEALTH"

kubectl get application \
  goldengate-dev-oracle-payments-01 \
  goldengate-dev-postgresql-payments-01 \
  goldengate-monitor \
  -n "$ARGOCD_NAMESPACE" \
  -o custom-columns='NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status'

kubectl get statefulset \
  gg-oracle-payments-01 \
  gg-postgresql-payments-01 \
  -n "$RUNTIME_NAMESPACE" \
  -o custom-columns='NAME:.metadata.name,READY:.status.readyReplicas,REPLICAS:.status.replicas,CURRENT:.status.currentRevision,UPDATE:.status.updateRevision'

kubectl get deployment gg-monitor \
  -n "$MONITOR_NAMESPACE" \
  -o custom-columns='NAME:.metadata.name,READY:.status.readyReplicas,AVAILABLE:.status.availableReplicas,UPDATED:.status.updatedReplicas'

kubectl get pods \
  -n "$RUNTIME_NAMESPACE" \
  -o custom-columns='POD:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[*].ready,CONTAINERS:.spec.containers[*].name'

echo
echo "--- Monitor switches ---"

kubectl get deployment gg-monitor \
  -n "$MONITOR_NAMESPACE" \
  -o jsonpath='{range .spec.template.spec.containers[0].env[?(@.name=="CLOUDWATCH_PUBLISH_ENABLED")]}{.name}={.value}{"\n"}{end}{range .spec.template.spec.containers[0].env[?(@.name=="LEGACY_FALLBACK_ENABLED")]}{.name}={.value}{"\n"}{end}'

MONITOR_POD="$(
  kubectl get pods \
    -n "$MONITOR_NAMESPACE" \
    -l app.kubernetes.io/name=gg-monitor \
    --field-selector=status.phase=Running \
    --sort-by=.metadata.creationTimestamp \
    -o name |
  tail -n 1 |
  sed 's#^pod/##'
)"

echo
echo "Selected monitor pod: ${MONITOR_POD:-NOT_FOUND}"

if [ -n "${MONITOR_POD:-}" ]; then
  kubectl exec -i \
    -n "$MONITOR_NAMESPACE" \
    "$MONITOR_POD" \
    -- python3 - <<'PY'
import json
import urllib.request

base = "http://127.0.0.1:8080"

for path in ("/healthz", "/readyz"):
    with urllib.request.urlopen(base + path, timeout=10) as response:
        print(path, response.status, json.load(response))

with urllib.request.urlopen(base + "/api/status", timeout=10) as response:
    payload = json.load(response)

for pipeline in payload.get("logicalPipelines", []):
    for runtime in pipeline.get("runtimes", []):
        print(
            runtime.get("deploymentName"),
            "status=" + str(runtime.get("effectiveStatus")),
            "fresh=" + str(runtime.get("fresh")),
            "source=" + str(runtime.get("dataSource")),
            "metricsEnabled=" + str(runtime.get("metricsEnabled")),
            "alertsEnabled=" + str(runtime.get("alertsEnabled")),
            "leaseOwner=" + str(runtime.get("leaseOwner")),
            "services=" + json.dumps(
                runtime.get("criticalServices") or {},
                sort_keys=True,
            ),
        )
PY
fi

echo
echo "--- Monitor errors from last 60 minutes ---"

kubectl logs \
  -n "$MONITOR_NAMESPACE" \
  deployment/gg-monitor \
  --since=60m 2>&1 |
grep -E \
  'cloudwatch_client_creation_failed|cloudwatch_put_metric_data_failed|tick failed|legacy-observer-fallback|LEGACY_FALLBACK_ENABLED' \
|| true

section "12. SCREENSHOT REVIEW VERDICT"

echo "Inventory exit code: ${INVENTORY_STATUS}"
echo
echo "No deletion was performed."
echo "No IAM policy was applied."
echo
echo "DO NOT RUN gg-iam-secrets-deployment.yaml YET."
echo "DO NOT DELETE THE LEGACY ARGO CD APPLICATION YET."
echo
echo "Share screenshots of Sections 2 through 12."