cat > validate-gg-u02-persistence.sh <<'EOF'
#!/usr/bin/env bash

set -euo pipefail

DEPLOYMENT_ID="${1:-gg-postgresql-repltest-01}"
NAMESPACE="${NAMESPACE:-goldengate-dev}"
PVC="${DEPLOYMENT_ID}-u02"

TEST_DIR="/u02/.gg-vdr-persistence-test"
TEST_FILE="${TEST_DIR}/marker.txt"
TEST_ID="gg-storage-test-$(date +%s)"

section() {
    echo
    echo "======================================================================"
    echo "$1"
    echo "======================================================================"
}

ok() {
    echo "✅ $*"
}

fail() {
    echo "❌ $*" >&2
    exit 1
}

get_pod() {
    kubectl get pods -n "${NAMESPACE}" \
        -l "app.kubernetes.io/instance=${DEPLOYMENT_ID}" \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true
}

section "GoldenGate /u02 persistence validation"

echo "Deployment : ${DEPLOYMENT_ID}"
echo "Namespace  : ${NAMESPACE}"
echo "PVC        : ${PVC}"
echo "Test ID    : ${TEST_ID}"

# ----------------------------------------------------------------------
# Pre-flight
# ----------------------------------------------------------------------

section "1. Pre-flight checks"

kubectl get sts "${DEPLOYMENT_ID}" -n "${NAMESPACE}" >/dev/null \
    || fail "StatefulSet ${DEPLOYMENT_ID} not found."

POD="$(get_pod)"

[[ -n "${POD}" ]] || fail "Could not discover deployment pod."

POD_PHASE="$(
    kubectl get pod "${POD}" -n "${NAMESPACE}" \
        -o jsonpath='{.status.phase}'
)"

[[ "${POD_PHASE}" == "Running" ]] \
    || fail "Pod ${POD} is not Running."

kubectl get pvc "${PVC}" -n "${NAMESPACE}" >/dev/null \
    || fail "PVC ${PVC} not found."

PVC_PHASE="$(
    kubectl get pvc "${PVC}" -n "${NAMESPACE}" \
        -o jsonpath='{.status.phase}'
)"

[[ "${PVC_PHASE}" == "Bound" ]] \
    || fail "PVC ${PVC} is not Bound."

OLD_UID="$(
    kubectl get pod "${POD}" -n "${NAMESPACE}" \
        -o jsonpath='{.metadata.uid}'
)"

OLD_PV="$(
    kubectl get pvc "${PVC}" -n "${NAMESPACE}" \
        -o jsonpath='{.spec.volumeName}'
)"

OLD_HANDLE="$(
    kubectl get pv "${OLD_PV}" \
        -o jsonpath='{.spec.csi.volumeHandle}'
)"

OLD_EFS="${OLD_HANDLE%%::*}"
OLD_AP="${OLD_HANDLE##*::}"

echo "Pod        : ${POD}"
echo "Pod UID    : ${OLD_UID}"
echo "PVC        : ${PVC}"
echo "PV         : ${OLD_PV}"
echo "Handle     : ${OLD_HANDLE}"
echo "EFS        : ${OLD_EFS}"
echo "AccessPoint: ${OLD_AP}"

[[ "${OLD_HANDLE}" == fs-*::fsap-* ]] \
    || fail "Unexpected EFS CSI volumeHandle: ${OLD_HANDLE}"

ok "Initial storage identity captured."

# ----------------------------------------------------------------------
# Write marker
# ----------------------------------------------------------------------

section "2. Write persistence marker"

kubectl exec -n "${NAMESPACE}" "${POD}" -- \
    sh -c "
        mkdir -p '${TEST_DIR}'
        printf '%s\n' '${TEST_ID}' > '${TEST_FILE}'
        sync
    "

MARKER_BEFORE="$(
    kubectl exec -n "${NAMESPACE}" "${POD}" -- \
        cat "${TEST_FILE}"
)"

[[ "${MARKER_BEFORE}" == "${TEST_ID}" ]] \
    || fail "Marker could not be verified before restart."

echo "Marker: ${MARKER_BEFORE}"

ok "Marker successfully written to /u02."

# ----------------------------------------------------------------------
# Recreate pod
# ----------------------------------------------------------------------

section "3. Recreate only the GoldenGate StatefulSet pod"

echo "Deleting pod only:"
echo "  ${NAMESPACE}/${POD}"
echo
echo "PVC/PV/EFS are NOT being deleted."

kubectl delete pod "${POD}" -n "${NAMESPACE}" --wait=false

echo
echo "Waiting for StatefulSet pod UID to change..."

NEW_UID=""

for i in $(seq 1 120); do

    CURRENT_UID="$(
        kubectl get pod "${POD}" -n "${NAMESPACE}" \
            -o jsonpath='{.metadata.uid}' 2>/dev/null || true
    )"

    if [[ -n "${CURRENT_UID}" && "${CURRENT_UID}" != "${OLD_UID}" ]]; then
        NEW_UID="${CURRENT_UID}"
        break
    fi

    sleep 5
done

[[ -n "${NEW_UID}" ]] \
    || fail "Replacement pod was not observed with a new UID."

echo "Old UID: ${OLD_UID}"
echo "New UID: ${NEW_UID}"

ok "StatefulSet recreated the pod."

# ----------------------------------------------------------------------
# Wait for readiness
# ----------------------------------------------------------------------

section "4. Wait for replacement pod readiness"

kubectl wait \
    --for=condition=Ready \
    "pod/${POD}" \
    -n "${NAMESPACE}" \
    --timeout=10m

NEW_PHASE="$(
    kubectl get pod "${POD}" -n "${NAMESPACE}" \
        -o jsonpath='{.status.phase}'
)"

[[ "${NEW_PHASE}" == "Running" ]] \
    || fail "Replacement pod phase is ${NEW_PHASE}."

kubectl get pod "${POD}" -n "${NAMESPACE}" -o wide

ok "Replacement pod is Running and Ready."

# ----------------------------------------------------------------------
# Verify same storage identity
# ----------------------------------------------------------------------

section "5. Verify storage identity after recreation"

NEW_PVC_PHASE="$(
    kubectl get pvc "${PVC}" -n "${NAMESPACE}" \
        -o jsonpath='{.status.phase}'
)"

NEW_PV="$(
    kubectl get pvc "${PVC}" -n "${NAMESPACE}" \
        -o jsonpath='{.spec.volumeName}'
)"

NEW_HANDLE="$(
    kubectl get pv "${NEW_PV}" \
        -o jsonpath='{.spec.csi.volumeHandle}'
)"

NEW_EFS="${NEW_HANDLE%%::*}"
NEW_AP="${NEW_HANDLE##*::}"

echo "PVC status : ${NEW_PVC_PHASE}"
echo "Old PV     : ${OLD_PV}"
echo "New PV     : ${NEW_PV}"
echo "Old handle : ${OLD_HANDLE}"
echo "New handle : ${NEW_HANDLE}"
echo "Old EFS    : ${OLD_EFS}"
echo "New EFS    : ${NEW_EFS}"
echo "Old AP     : ${OLD_AP}"
echo "New AP     : ${NEW_AP}"

[[ "${NEW_PVC_PHASE}" == "Bound" ]] \
    || fail "PVC is no longer Bound."

[[ "${NEW_PV}" == "${OLD_PV}" ]] \
    || fail "PV changed after pod recreation."

[[ "${NEW_HANDLE}" == "${OLD_HANDLE}" ]] \
    || fail "CSI volumeHandle changed after pod recreation."

[[ "${NEW_EFS}" == "${OLD_EFS}" ]] \
    || fail "EFS filesystem changed."

[[ "${NEW_AP}" == "${OLD_AP}" ]] \
    || fail "EFS access point changed."

ok "PVC/PV/EFS/access-point identity remained unchanged."

# ----------------------------------------------------------------------
# Verify /u02 is still mounted
# ----------------------------------------------------------------------

section "6. Verify /u02 mount after recreation"

kubectl exec -n "${NAMESPACE}" "${POD}" -- \
    sh -c 'df -hT /u02 2>/dev/null || df -h /u02'

kubectl exec -n "${NAMESPACE}" "${POD}" -- \
    sh -c 'mount | grep -E " on /u02( |$)" || grep " /u02 " /proc/mounts'

ok "/u02 is mounted after recreation."

# ----------------------------------------------------------------------
# Verify marker survived
# ----------------------------------------------------------------------

section "7. Verify persisted data"

MARKER_AFTER="$(
    kubectl exec -n "${NAMESPACE}" "${POD}" -- \
        cat "${TEST_FILE}" 2>/dev/null || true
)"

echo "Expected marker: ${TEST_ID}"
echo "Actual marker  : ${MARKER_AFTER}"

[[ "${MARKER_AFTER}" == "${TEST_ID}" ]] \
    || fail "Persistence marker did NOT survive pod recreation."

ok "Persistence marker survived pod recreation."

# ----------------------------------------------------------------------
# Cleanup only our test artifact
# ----------------------------------------------------------------------

section "8. Cleanup test marker"

kubectl exec -n "${NAMESPACE}" "${POD}" -- \
    rm -rf "${TEST_DIR}"

if kubectl exec -n "${NAMESPACE}" "${POD}" -- \
    test -e "${TEST_DIR}" 2>/dev/null; then

    fail "Test directory still exists after cleanup."
fi

ok "Test marker cleaned up."

# ----------------------------------------------------------------------
# Final result
# ----------------------------------------------------------------------

section "PERSISTENCE TEST PASSED"

echo "Deployment   : ${DEPLOYMENT_ID}"
echo "Pod          : ${POD}"
echo
echo "Old pod UID  : ${OLD_UID}"
echo "New pod UID  : ${NEW_UID}"
echo
echo "PVC          : ${PVC}"
echo "PV           : ${NEW_PV}"
echo "EFS          : ${NEW_EFS}"
echo "Access Point : ${NEW_AP}"
echo
echo "Result:"
echo "  Pod recreated              ✅"
echo "  Pod UID changed            ✅"
echo "  PVC retained               ✅"
echo "  PV retained                ✅"
echo "  EFS retained               ✅"
echo "  Access Point retained      ✅"
echo "  /u02 remounted             ✅"
echo "  Marker survived            ✅"
echo "  Test marker cleaned        ✅"
EOF

chmod +x validate-gg-u02-persistence.sh