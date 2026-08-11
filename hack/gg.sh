cat > check-gg-managed-storage.sh <<'EOF'
#!/usr/bin/env bash

set -u

# ============================================================
# GoldenGate managed-EFS read-only validation
# ============================================================
#
# Usage:
#
#   ./check-gg-managed-storage.sh
#
# or:
#
#   ./check-gg-managed-storage.sh gg-postgresql-repltest-01
#
# Optional:
#
#   NAMESPACE=goldengate-dev \
#   AWS_REGION=eu-west-1 \
#   ./check-gg-managed-storage.sh gg-postgresql-repltest-01
#
# This script is READ ONLY.
# It does NOT delete pods, write files, patch resources, or mutate AWS.
# ============================================================

DEPLOYMENT_ID="${1:-gg-postgresql-repltest-01}"
NAMESPACE="${NAMESPACE:-goldengate-dev}"
AWS_REGION="${AWS_REGION:-eu-west-1}"

# Historical shared filesystem.
# The managed repltest runtime MUST NOT resolve to this filesystem.
HISTORICAL_EFS_ID="${HISTORICAL_EFS_ID:-fs-05cadf3570f23cd39}"

EXPECTED_CREATION_TOKEN="${NAMESPACE%-dev}"
EXPECTED_CREATION_TOKEN="dev-${DEPLOYMENT_ID}-efs"

section() {
    echo
    echo "======================================================================"
    echo "$1"
    echo "======================================================================"
}

ok() {
    echo "✅ $*"
}

warn() {
    echo "⚠️  $*"
}

fail() {
    echo "❌ $*"
}

command_exists() {
    command -v "$1" >/dev/null 2>&1
}

section "GoldenGate managed storage validation"

echo "Deployment ID       : ${DEPLOYMENT_ID}"
echo "Namespace           : ${NAMESPACE}"
echo "AWS region          : ${AWS_REGION}"
echo "Expected EFS token  : ${EXPECTED_CREATION_TOKEN}"
echo "Historical EFS      : ${HISTORICAL_EFS_ID}"

# ----------------------------------------------------------------------
# 1. StatefulSet
# ----------------------------------------------------------------------

section "1. StatefulSet"

if kubectl get sts "${DEPLOYMENT_ID}" -n "${NAMESPACE}" >/dev/null 2>&1; then
    kubectl get sts "${DEPLOYMENT_ID}" -n "${NAMESPACE}" -o wide

    STS_READY="$(kubectl get sts "${DEPLOYMENT_ID}" -n "${NAMESPACE}" \
        -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)"

    STS_REPLICAS="$(kubectl get sts "${DEPLOYMENT_ID}" -n "${NAMESPACE}" \
        -o jsonpath='{.spec.replicas}' 2>/dev/null || true)"

    if [[ "${STS_READY:-0}" == "${STS_REPLICAS:-1}" ]]; then
        ok "StatefulSet is ready (${STS_READY}/${STS_REPLICAS})."
    else
        warn "StatefulSet readiness is ${STS_READY:-0}/${STS_REPLICAS:-unknown}."
    fi
else
    fail "StatefulSet ${DEPLOYMENT_ID} was not found."
fi

# ----------------------------------------------------------------------
# 2. Discover pod automatically
# ----------------------------------------------------------------------

section "2. GoldenGate pod"

POD="$(kubectl get pods -n "${NAMESPACE}" \
    -l "app.kubernetes.io/instance=${DEPLOYMENT_ID}" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"

if [[ -z "${POD}" ]]; then
    POD="$(kubectl get pods -n "${NAMESPACE}" \
        -o name 2>/dev/null \
        | sed 's#pod/##' \
        | grep "^${DEPLOYMENT_ID}-" \
        | head -1 || true)"
fi

if [[ -z "${POD}" ]]; then
    fail "Could not discover a pod for ${DEPLOYMENT_ID}."
else
    echo "Discovered pod: ${POD}"
    kubectl get pod "${POD}" -n "${NAMESPACE}" -o wide

    POD_PHASE="$(kubectl get pod "${POD}" -n "${NAMESPACE}" \
        -o jsonpath='{.status.phase}' 2>/dev/null || true)"

    if [[ "${POD_PHASE}" == "Running" ]]; then
        ok "Pod phase is Running."
    else
        warn "Pod phase is ${POD_PHASE:-unknown}."
    fi
fi

# ----------------------------------------------------------------------
# 3. Discover /u02 PVC
# ----------------------------------------------------------------------

section "3. /u02 PersistentVolumeClaim"

PVC="${DEPLOYMENT_ID}-u02"

if ! kubectl get pvc "${PVC}" -n "${NAMESPACE}" >/dev/null 2>&1; then
    PVC="$(kubectl get pvc -n "${NAMESPACE}" \
        -l "goldengate.adcb/deployment-name=${DEPLOYMENT_ID}" \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
fi

if [[ -z "${PVC}" ]]; then
    fail "Could not discover /u02 PVC."
else
    echo "Discovered PVC: ${PVC}"
    kubectl get pvc "${PVC}" -n "${NAMESPACE}" -o wide

    PVC_STATUS="$(kubectl get pvc "${PVC}" -n "${NAMESPACE}" \
        -o jsonpath='{.status.phase}' 2>/dev/null || true)"

    STORAGE_CLASS="$(kubectl get pvc "${PVC}" -n "${NAMESPACE}" \
        -o jsonpath='{.spec.storageClassName}' 2>/dev/null || true)"

    PV="$(kubectl get pvc "${PVC}" -n "${NAMESPACE}" \
        -o jsonpath='{.spec.volumeName}' 2>/dev/null || true)"

    echo
    echo "PVC status          : ${PVC_STATUS}"
    echo "StorageClass        : ${STORAGE_CLASS}"
    echo "Bound PV            : ${PV}"

    if [[ "${PVC_STATUS}" == "Bound" ]]; then
        ok "PVC is Bound."
    else
        fail "PVC is not Bound."
    fi
fi

# ----------------------------------------------------------------------
# 4. StorageClass
# ----------------------------------------------------------------------

section "4. StorageClass"

if [[ -n "${STORAGE_CLASS:-}" ]]; then
    kubectl get storageclass "${STORAGE_CLASS}" -o wide || true

    PROVISIONER="$(kubectl get storageclass "${STORAGE_CLASS}" \
        -o jsonpath='{.provisioner}' 2>/dev/null || true)"

    RECLAIM_POLICY="$(kubectl get storageclass "${STORAGE_CLASS}" \
        -o jsonpath='{.reclaimPolicy}' 2>/dev/null || true)"

    BINDING_MODE="$(kubectl get storageclass "${STORAGE_CLASS}" \
        -o jsonpath='{.volumeBindingMode}' 2>/dev/null || true)"

    echo
    echo "Provisioner         : ${PROVISIONER}"
    echo "Reclaim policy      : ${RECLAIM_POLICY}"
    echo "Binding mode        : ${BINDING_MODE}"

    [[ "${PROVISIONER}" == "efs.csi.aws.com" ]] \
        && ok "StorageClass uses EFS CSI." \
        || fail "Unexpected StorageClass provisioner: ${PROVISIONER}"

    [[ "${RECLAIM_POLICY}" == "Retain" ]] \
        && ok "StorageClass reclaimPolicy is Retain." \
        || warn "StorageClass reclaimPolicy is ${RECLAIM_POLICY}."
fi

# ----------------------------------------------------------------------
# 5. PersistentVolume / CSI relationship
# ----------------------------------------------------------------------

section "5. PersistentVolume and EFS CSI identity"

EFS_ID=""
ACCESS_POINT_ID=""
VOLUME_HANDLE=""

if [[ -n "${PV:-}" ]]; then
    kubectl get pv "${PV}" -o wide

    PV_STATUS="$(kubectl get pv "${PV}" \
        -o jsonpath='{.status.phase}' 2>/dev/null || true)"

    CSI_DRIVER="$(kubectl get pv "${PV}" \
        -o jsonpath='{.spec.csi.driver}' 2>/dev/null || true)"

    VOLUME_HANDLE="$(kubectl get pv "${PV}" \
        -o jsonpath='{.spec.csi.volumeHandle}' 2>/dev/null || true)"

    PV_RECLAIM_POLICY="$(kubectl get pv "${PV}" \
        -o jsonpath='{.spec.persistentVolumeReclaimPolicy}' 2>/dev/null || true)"

    MOUNT_OPTIONS="$(kubectl get pv "${PV}" \
        -o jsonpath='{.spec.mountOptions[*]}' 2>/dev/null || true)"

    echo
    echo "PV status           : ${PV_STATUS}"
    echo "CSI driver          : ${CSI_DRIVER}"
    echo "VolumeHandle        : ${VOLUME_HANDLE}"
    echo "PV reclaim policy   : ${PV_RECLAIM_POLICY}"
    echo "Mount options       : ${MOUNT_OPTIONS}"

    [[ "${PV_STATUS}" == "Bound" ]] \
        && ok "PV is Bound." \
        || fail "PV is not Bound."

    [[ "${CSI_DRIVER}" == "efs.csi.aws.com" ]] \
        && ok "PV is provisioned by AWS EFS CSI." \
        || fail "Unexpected PV CSI driver: ${CSI_DRIVER}"

    [[ " ${MOUNT_OPTIONS} " == *" tls "* ]] \
        && ok "TLS EFS mount option is enabled." \
        || warn "TLS mount option was not visible."

    if [[ "${VOLUME_HANDLE}" == *"::"* ]]; then
        EFS_ID="${VOLUME_HANDLE%%::*}"
        ACCESS_POINT_ID="${VOLUME_HANDLE##*::}"

        echo
        echo "Resolved EFS ID     : ${EFS_ID}"
        echo "Resolved AccessPoint: ${ACCESS_POINT_ID}"

        ok "Dynamic EFS access-point style volumeHandle detected."
    else
        warn "VolumeHandle did not have expected fs-...::fsap-... format."
    fi
fi

# ----------------------------------------------------------------------
# 6. Safety check against historical EFS
# ----------------------------------------------------------------------

section "6. Managed EFS isolation check"

if [[ -n "${EFS_ID}" ]]; then
    if [[ "${EFS_ID}" == "${HISTORICAL_EFS_ID}" ]]; then
        fail "Managed runtime is using the HISTORICAL EFS ${HISTORICAL_EFS_ID}."
    else
        ok "Managed runtime uses dedicated EFS ${EFS_ID}."
        ok "It is different from historical ${HISTORICAL_EFS_ID}."
    fi
else
    warn "Could not resolve EFS ID."
fi

# ----------------------------------------------------------------------
# 7. Pod volume configuration
# ----------------------------------------------------------------------

section "7. Pod volume mounts"

if [[ -n "${POD:-}" ]]; then
    echo "-- Container volume mounts --"

    kubectl get pod "${POD}" -n "${NAMESPACE}" \
        -o jsonpath='{range .spec.containers[*].volumeMounts[*]}{.name}{" => "}{.mountPath}{"\n"}{end}' \
        2>/dev/null || true

    echo
    echo "-- Pod volume sources --"

    kubectl get pod "${POD}" -n "${NAMESPACE}" \
        -o jsonpath='{range .spec.volumes[*]}{.name}{" PVC="}{.persistentVolumeClaim.claimName}{" emptyDir="}{.emptyDir}{"\n"}{end}' \
        2>/dev/null || true
fi

# ----------------------------------------------------------------------
# 8. Runtime view from inside container
# ----------------------------------------------------------------------

section "8. Runtime mount inspection"

if [[ -n "${POD:-}" ]]; then
    echo "-- df for /u02 --"
    kubectl exec -n "${NAMESPACE}" "${POD}" -- \
        sh -c 'df -hT /u02 2>/dev/null || df -h /u02' \
        2>&1 || warn "Could not execute df inside pod."

    echo
    echo "-- mount entry containing /u02 --"
    kubectl exec -n "${NAMESPACE}" "${POD}" -- \
        sh -c 'mount | grep -E " on /u02( |$)" || grep " /u02 " /proc/mounts || true' \
        2>&1 || warn "Could not inspect /u02 mount."

    echo
    echo "-- /u02 metadata (NO file contents displayed) --"
    kubectl exec -n "${NAMESPACE}" "${POD}" -- \
        sh -c '
            test -d /u02 &&
            echo "/u02 exists" &&
            ls -ld /u02
        ' 2>&1 || warn "Could not inspect /u02."

    echo
    echo "-- /u03 metadata --"
    kubectl exec -n "${NAMESPACE}" "${POD}" -- \
        sh -c '
            test -d /u03 &&
            echo "/u03 exists" &&
            ls -ld /u03
        ' 2>&1 || warn "Could not inspect /u03."
fi

# ----------------------------------------------------------------------
# 9. Best-effort AWS EFS validation
# ----------------------------------------------------------------------

section "9. AWS EFS validation (best effort)"

if ! command_exists aws; then
    warn "AWS CLI is not installed. Skipping AWS-side verification."
elif [[ -z "${EFS_ID}" ]]; then
    warn "No EFS ID was resolved. Skipping AWS-side verification."
else
    echo "Caller identity:"
    aws sts get-caller-identity --output table 2>&1 || true

    echo
    echo "-- EFS filesystem --"

    aws efs describe-file-systems \
        --region "${AWS_REGION}" \
        --file-system-id "${EFS_ID}" \
        --query 'FileSystems[0].{
          FileSystemId:FileSystemId,
          Name:Name,
          CreationToken:CreationToken,
          LifeCycleState:LifeCycleState,
          Encrypted:Encrypted,
          KmsKeyId:KmsKeyId,
          PerformanceMode:PerformanceMode,
          ThroughputMode:ThroughputMode,
          NumberOfMountTargets:NumberOfMountTargets
        }' \
        --output table 2>&1 || warn "Unable to describe EFS with current AWS credentials."

    echo
    echo "-- Creation-token cross-check --"

    TOKEN_EFS_ID="$(
        aws efs describe-file-systems \
            --region "${AWS_REGION}" \
            --creation-token "${EXPECTED_CREATION_TOKEN}" \
            --query 'FileSystems[0].FileSystemId' \
            --output text 2>/dev/null || true
    )"

    echo "Creation token lookup : ${TOKEN_EFS_ID:-unavailable}"

    if [[ -n "${TOKEN_EFS_ID}" && "${TOKEN_EFS_ID}" != "None" ]]; then
        if [[ "${TOKEN_EFS_ID}" == "${EFS_ID}" ]]; then
            ok "Creation token resolves to the SAME EFS used by the PV."
        else
            fail "Creation-token EFS (${TOKEN_EFS_ID}) differs from PV EFS (${EFS_ID})."
        fi
    fi

    echo
    echo "-- EFS mount targets --"

    aws efs describe-mount-targets \
        --region "${AWS_REGION}" \
        --file-system-id "${EFS_ID}" \
        --query 'MountTargets[].{
          MountTargetId:MountTargetId,
          SubnetId:SubnetId,
          AvailabilityZoneName:AvailabilityZoneName,
          LifeCycleState:LifeCycleState,
          IpAddress:IpAddress
        }' \
        --output table 2>&1 || warn "Unable to describe EFS mount targets."

    if [[ -n "${ACCESS_POINT_ID}" ]]; then
        echo
        echo "-- EFS access point --"

        aws efs describe-access-points \
            --region "${AWS_REGION}" \
            --access-point-id "${ACCESS_POINT_ID}" \
            --query 'AccessPoints[0].{
              AccessPointId:AccessPointId,
              FileSystemId:FileSystemId,
              LifeCycleState:LifeCycleState,
              RootDirectory:RootDirectory
            }' \
            --output json 2>&1 || warn "Unable to describe EFS access point."
    fi

    echo
    echo "-- Ownership tags --"

    aws efs list-tags-for-resource \
        --region "${AWS_REGION}" \
        --resource-id "${EFS_ID}" \
        --query 'Tags[?Key==`ManagedBy` || Key==`GoldenGateDeploymentId` || Key==`GoldenGateEnvironment` || Key==`GoldenGateStorage` || Key==`Name`].[Key,Value]' \
        --output table 2>&1 || warn "Unable to read EFS tags with current AWS credentials."
fi

# ----------------------------------------------------------------------
# 10. Kubernetes events
# ----------------------------------------------------------------------

section "10. Relevant Kubernetes events"

kubectl get events -n "${NAMESPACE}" \
    --sort-by='.lastTimestamp' 2>/dev/null \
    | grep -E "${DEPLOYMENT_ID}|${PVC:-__NO_PVC__}" \
    | tail -30 || true

# ----------------------------------------------------------------------
# Final summary
# ----------------------------------------------------------------------

section "RESULT SUMMARY"

echo "Deployment           : ${DEPLOYMENT_ID}"
echo "Pod                  : ${POD:-NOT FOUND}"
echo "PVC                  : ${PVC:-NOT FOUND}"
echo "PV                   : ${PV:-NOT FOUND}"
echo "StorageClass         : ${STORAGE_CLASS:-NOT FOUND}"
echo "CSI driver           : ${CSI_DRIVER:-NOT FOUND}"
echo "EFS ID               : ${EFS_ID:-NOT RESOLVED}"
echo "Access Point         : ${ACCESS_POINT_ID:-NOT RESOLVED}"
echo "Historical EFS       : ${HISTORICAL_EFS_ID}"
echo "Creation token       : ${EXPECTED_CREATION_TOKEN}"

echo
echo "READ-ONLY validation complete."
echo "No pod deletion, file creation, Kubernetes mutation, or AWS mutation was performed."
EOF

chmod +x check-gg-managed-storage.sh
./check-gg-managed-storage.sh