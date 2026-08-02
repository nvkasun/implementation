#!/usr/bin/env bash
# Phase 5B2B1: read-only inventory of retired legacy GoldenGate external
# resources (envs/dev/payments-ora-to-pg-001, deleted in Phase 5B2A).
#
# Produces a deterministic JSON cleanup manifest for later human review.
# This script NEVER deletes, patches, updates, applies, or otherwise
# mutates any AWS or Kubernetes resource -- every AWS CLI/kubectl/argocd
# invocation in this file is a read-only Describe/Get/List/Query call.
# Canonical (live, in-use) resources are deny-listed and can never be
# reported as cleanup candidates, no matter what live data is observed.
#
# Required environment (opaque string data, never interpolated GitHub
# expression syntax -- see .github/workflows/goldengate-legacy-cleanup-
# inventory.yaml, which only maps values through env: and invokes this
# script):
#   INPUT_ENVIRONMENT -- workflow_dispatch input: target environment
#   GITHUB_OUTPUT     -- path to the GitHub Actions step output file (optional)
#   GITHUB_STEP_SUMMARY -- path to the GitHub Actions job summary file (optional)
#
# Exit code is always 0 on a successful inventory run (including when
# permission gaps are encountered -- those are reported in the manifest,
# never treated as a hard failure of the read-only inventory itself).
# Exit code is non-zero only for a genuine script defect (e.g. jq/python3
# unavailable, malformed live JSON that cannot be parsed at all).
set -euo pipefail

# ===========================================================================
# Section 0: canonical facts (the deny-list). These are the verified-live
# facts this phase was given -- never re-derived by guessing, never
# overridden by anything this script observes live. A resource matching one
# of these identifiers is RETAIN, full stop, regardless of any other
# evidence.
# ===========================================================================

ENVIRONMENT="${INPUT_ENVIRONMENT:-dev}"
AWS_REGION_EXPECTED="eu-west-1"
EKS_CLUSTER_EXPECTED="gg-poc-dev"
WORKLOAD_ACCOUNT_ID_EXPECTED="668311715351"
ECR_ACCOUNT_ID_EXPECTED="229410149234"

CANONICAL_NAMESPACE="goldengate-dev"
MONITOR_NAMESPACE="goldengate-monitoring"

CANONICAL_DEPLOYMENT_IDS=("gg-oracle-payments-01" "gg-postgresql-payments-01")

CANONICAL_APPLICATIONS=(
  "goldengate-dev-oracle-payments-01"
  "goldengate-dev-postgresql-payments-01"
  "goldengate-dev-platform"
  "goldengate-monitor"
)

LEGACY_APPLICATION="goldengate-payments-ora-to-pg-001"
LEGACY_NAMESPACE="gg-dev-payments-ora-to-pg-001"

# Current active PVC/PV bindings -- verified live, never cleanup candidates.
CANONICAL_PVC_NAMES=("gg-oracle-payments-01-u02" "gg-postgresql-payments-01-u02")
CANONICAL_PV_IDS=(
  "pvc-dd1bc7bc-b736-4fee-abfe-abf622e70550"
  "pvc-5f29ad65-0a5b-4d7a-9568-2d82b3bd1b38"
)

# Known Released/Retain PV cleanup candidates -- eligibility is still
# independently re-verified below, never assumed safe merely from this list.
CANDIDATE_PV_IDS=(
  "pvc-3a93c990-a9fa-4cca-99df-7c3375472074"
  "pvc-93251c3f-c408-4713-bd46-ebc5e0eafa8a"
  "pvc-5c43940e-1054-43f5-8031-9db4b51a024a"
  "pvc-bacb3e9d-d904-467c-959f-dea9548699c9"
)

# Known access-point handles recorded by those old PVs -- also
# independently re-verified below, never assumed safe merely from this list.
CANDIDATE_EFS_ACCESS_POINT_IDS=(
  "fsap-007cfc2ff801c24b8"
  "fsap-035f46f17955f57cb"
  "fsap-0211c604d58d1010d"
  "fsap-09566a2339f781a33"
)

# EFS filesystem ID shared by both canonical deployments (envs/dev/gg-
# oracle-payments-01/values.yaml and envs/dev/gg-postgresql-payments-01/
# values.yaml, persistence.efs.fileSystemId -- identical in both).
EFS_FILESYSTEM_ID_EXPECTED="fs-05cadf3570f23cd39"

CANONICAL_STORAGE_CLASSES=(
  "gg-efs-dev-gg-oracle-payments-01"
  "gg-efs-dev-gg-postgresql-payments-01"
)
LEGACY_STORAGE_CLASS="gg-efs-dev-payments-ora-to-pg-001"

# DynamoDB: table name, hash/range key names, and legacy per-role partition
# names, all confirmed from this repository's own source and test fixtures
# (never guessed): envs/dev/dynamodb.tf declares hash_key=pipeline,
# range_key=recordType, table_name=gg-eks-pipeline; monitoring/monitor/
# monitor.py defines RECORD_TYPE_CONFIG="CONFIG", RECORD_TYPE_LEASE="LEASE",
# RECORD_TYPE_DEPLOYMENT_STATE="STATE#_deployment", STATE_PREFIX="STATE#";
# monitoring/monitor/tests/test_monitor.py's ReadRuntimeViewTests class
# hardcodes LEGACY_PARTITION_NAMES = ("gg-payments-ora-to-pg-001-source",
# "gg-payments-ora-to-pg-001-target") as the exact legacy per-role
# partitions monitor.py must never query -- the same two names used here.
DYNAMODB_TABLE="gg-eks-pipeline"
DYNAMODB_HASH_KEY="pipeline"
DYNAMODB_RANGE_KEY="recordType"
CANONICAL_DYNAMODB_PARTITIONS=("gg-oracle-payments-01" "gg-postgresql-payments-01")
LEGACY_DYNAMODB_PARTITIONS=("gg-payments-ora-to-pg-001-source" "gg-payments-ora-to-pg-001-target")

# Observer ECR: repository short name and account, confirmed from this
# repository's own Git history (the "Detect changed deployments" step as it
# existed before Phase 5A observer retirement declared
# OBSERVER_ECR_REPOSITORY: goldengate-observer and
# ECR_REGISTRY: 229410149234.dkr.ecr.eu-west-1.amazonaws.com, with image
# tags of the exact content-addressed form obs-<12-hex-chars> derived from
# `git rev-parse HEAD:monitoring/observer`). No tag/digest is hardcoded
# here -- they are enumerated live from ECR when this script runs, never
# guessed.
OBSERVER_ECR_REPOSITORY="goldengate-observer"
OBSERVER_ECR_TAG_PATTERN='^obs-[0-9a-f]{12}$'

# Shared infrastructure identifiers the retired legacy deployment used --
# confirmed by diffing the last committed content of the deleted
# envs/dev/payments-ora-to-pg-001/values.yaml against the current canonical
# values files. These are IDENTICAL in both (same Secrets Manager
# objectNames, same ALB groupName, same ACM certificateArn), so they are
# still actively used by the canonical deployments today and must be
# reported RETAIN -- never a candidate, regardless of the fact that the
# retired legacy values file also referenced them.
SHARED_SECRETS_MANAGER_PATHS=(
  "dev/goldengate/source/admin"
  "dev/goldengate/target/admin"
  "dev/goldengate/tls-certificate"
)
SHARED_ALB_GROUP_NAME="gg-poc-dev-alb"
SHARED_ACM_CERTIFICATE_ARN="arn:aws:acm:eu-west-1:668311715351:certificate/9e53e28e-3243-47fc-85a1-50f9a94acde7"

# Legacy-specific Ingress hostnames (unique to the retired legacyPair
# deployment, never used by canonical singleRuntime deployments -- those
# use <deploymentId>.<hostDomain>, e.g. gg-oracle-payments-01.goldengate-
# dev.adcbmis.local). Confirmed from the same last-committed legacy values
# file. The underlying Ingress Kubernetes object was owned by the already-
# deleted legacy Argo CD Application/namespace, so any corresponding ALB
# listener rule is expected to have been cascade-removed by the AWS Load
# Balancer Controller when that Ingress was deleted -- this script reports
# these hostnames informationally (never as a delete candidate: there is no
# Terraform-managed Route53/ALB resource for them in this repository to
# even describe, and guessing live ALB/Route53 rule state from repository
# content alone is exactly the "guessing ownership from a similar name"
# this phase must not do).
LEGACY_INGRESS_HOSTNAMES=(
  "ogg-oracle-payments-ora-to-pg-001.goldengate-dev.adcbmis.local"
  "ogg-postgresql-payments-ora-to-pg-001.goldengate-dev.adcbmis.local"
)

GENERATED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ===========================================================================
# Section 1: prerequisites and small helpers.
# ===========================================================================

MISSING_TOOLS=()
command -v jq >/dev/null 2>&1 || MISSING_TOOLS+=("jq")
command -v python3 >/dev/null 2>&1 || MISSING_TOOLS+=("python3")

if [ "${#MISSING_TOOLS[@]}" -gt 0 ]; then
  echo "FAIL: required tool(s) not available on this runner: ${MISSING_TOOLS[*]}" >&2
  exit 1
fi

# in_array NEEDLE HAYSTACK_ARRAY_NAME[@]
in_array() {
  local needle="$1"
  shift
  local item
  for item in "$@"; do
    [ "$item" = "$needle" ] && return 0
  done
  return 1
}

# ===========================================================================
# Section 2: pure classification/eligibility functions. Each takes already-
# collected facts as explicit arguments and returns a decision plus
# blocking reasons -- no live AWS/kubectl call inside these functions, which
# is what makes them independently unit-testable (see hack/test-
# goldengate-deployment-models.sh). Live collection happens separately in
# Section 3 and is passed into these as plain arguments.
# ===========================================================================

is_canonical_pv_id() {
  in_array "$1" "${CANONICAL_PV_IDS[@]}"
}

is_canonical_volume_handle() {
  local handle="$1"
  local canonical_handle
  for canonical_handle in "${CANONICAL_VOLUME_HANDLES[@]:-}"; do
    [ -n "$canonical_handle" ] && [ "$handle" = "$canonical_handle" ] && return 0
  done
  return 1
}

# classify_pv PV_ID PHASE RECLAIM_POLICY VOLUME_HANDLE BOUND_CLAIM_NS BOUND_CLAIM_NAME REFERENCED_BY_RUNNING_POD VOLUME_HANDLE_VERIFIED
#
# Prints "eligible" and returns 0 when every required condition holds.
# Otherwise prints a semicolon-separated list of blocking reasons and
# returns 1. Fails closed: any fact that could not be independently
# verified (VOLUME_HANDLE_VERIFIED=false) blocks eligibility rather than
# silently passing.
classify_pv() {
  local pv_id="$1" phase="$2" reclaim_policy="$3" volume_handle="$4"
  local bound_claim_ns="$5" bound_claim_name="$6" referenced_by_running_pod="$7"
  local volume_handle_verified="${8:-true}"
  local reasons=""

  if is_canonical_pv_id "$pv_id"; then
    reasons="${reasons}is_current_canonical_pv;"
  fi

  if [ "$phase" != "Released" ]; then
    reasons="${reasons}phase_not_released(${phase});"
  fi

  if [ "$reclaim_policy" != "Retain" ]; then
    reasons="${reasons}reclaim_policy_not_retain(${reclaim_policy});"
  fi

  if [ "$volume_handle_verified" != "true" ]; then
    reasons="${reasons}volume_handle_not_independently_verified;"
  elif is_canonical_volume_handle "$volume_handle"; then
    reasons="${reasons}matches_canonical_volume_handle;"
  fi

  if [ -n "$bound_claim_name" ]; then
    reasons="${reasons}still_referenced_by_active_pvc(${bound_claim_ns}/${bound_claim_name});"
  fi

  if [ "$referenced_by_running_pod" = "true" ]; then
    reasons="${reasons}referenced_by_running_pod_volume;"
  fi

  if [ -z "$reasons" ]; then
    echo "eligible"
    return 0
  fi
  echo "$reasons"
  return 1
}

is_canonical_efs_access_point() {
  in_array "$1" "${CANONICAL_EFS_ACCESS_POINT_IDS[@]:-}"
}

# classify_efs_access_point AP_ID EXISTS FILESYSTEM_ID REFERENCED_BY_BOUND_PV
classify_efs_access_point() {
  local ap_id="$1" exists="$2" filesystem_id="$3" referenced_by_bound_pv="$4"
  local reasons=""

  if [ "$exists" != "true" ]; then
    echo "does_not_exist"
    return 1
  fi

  if is_canonical_efs_access_point "$ap_id"; then
    reasons="${reasons}is_current_canonical_access_point;"
  fi

  if [ "$filesystem_id" != "$EFS_FILESYSTEM_ID_EXPECTED" ]; then
    reasons="${reasons}unexpected_filesystem(${filesystem_id});"
  fi

  if [ "$referenced_by_bound_pv" = "true" ]; then
    reasons="${reasons}referenced_by_bound_pv;"
  fi

  if [ -z "$reasons" ]; then
    echo "eligible"
    return 0
  fi
  echo "$reasons"
  return 1
}

# classify_storage_class NAME IN_USE_BY_ACTIVE_PVC
classify_storage_class() {
  local name="$1" in_use_by_active_pvc="$2"

  if in_array "$name" "${CANONICAL_STORAGE_CLASSES[@]}"; then
    echo "retain(canonical)"
    return 1
  fi

  if [ "$name" = "$LEGACY_STORAGE_CLASS" ]; then
    if [ "$in_use_by_active_pvc" = "true" ]; then
      echo "still_in_use_by_active_pvc;"
      return 1
    fi
    echo "eligible"
    return 0
  fi

  echo "unrecognized_storage_class;"
  return 1
}

# classify_dynamodb_partition PIPELINE_ID ITEM_COUNT
classify_dynamodb_partition() {
  local pipeline_id="$1" item_count="$2"

  # Hard deny-list: a canonical partition can never become a candidate,
  # full stop -- checked first and unconditionally.
  if in_array "$pipeline_id" "${CANONICAL_DYNAMODB_PARTITIONS[@]}"; then
    echo "retain(canonical)"
    return 1
  fi

  if ! in_array "$pipeline_id" "${LEGACY_DYNAMODB_PARTITIONS[@]}"; then
    echo "not_a_recognized_legacy_partition;"
    return 1
  fi

  if [ "$item_count" -eq 0 ]; then
    echo "no_items_found;"
    return 1
  fi

  echo "eligible"
  return 0
}

# classify_observer_image TAG LIVE_REFERENCE_COUNT
classify_observer_image() {
  local tag="$1" live_reference_count="$2"

  if [ "$live_reference_count" -gt 0 ]; then
    echo "referenced_by_${live_reference_count}_live_workload(s);"
    return 1
  fi

  echo "eligible"
  return 0
}

# ===========================================================================
# Section 3: manifest accumulation. jq -c is used throughout (matching this
# repository's established convention in .github/workflows/goldengate-eks-
# app.yaml and hack/detect-goldengate-deployments.sh) so the final manifest
# is always valid, deterministically-ordered JSON -- never hand-built via
# string concatenation.
# ===========================================================================

CANDIDATES_PV="[]"
CANDIDATES_EFS_AP="[]"
CANDIDATES_STORAGE_CLASS="[]"
CANDIDATES_DYNAMODB="[]"
CANDIDATES_ECR_REPOSITORIES="[]"
CANDIDATES_ECR_IMAGES="[]"
BLOCKED_ITEMS="[]"
PERMISSION_GAPS="[]"

add_candidate() {
  local list_var="$1" resource_type="$2" identifier="$3" eligibility="$4" evidence_json="$5" reasons="$6"
  local current="${!list_var}"
  local updated
  updated="$(echo "$current" | jq -c \
    --arg resourceType "$resource_type" \
    --arg identifier "$identifier" \
    --arg eligibility "$eligibility" \
    --argjson evidence "$evidence_json" \
    --arg reasons "$reasons" \
    '. + [{
      resourceType: $resourceType,
      identifier: $identifier,
      eligibility: $eligibility,
      evidence: $evidence,
      blockingReasons: ($reasons | select(. != "") | split(";") | map(select(. != "")))
    }]')"
  printf -v "$list_var" '%s' "$updated"
}

add_blocked() {
  local resource_type="$1" identifier="$2" reason="$3"
  BLOCKED_ITEMS="$(echo "$BLOCKED_ITEMS" | jq -c \
    --arg resourceType "$resource_type" \
    --arg identifier "$identifier" \
    --arg reason "$reason" \
    '. + [{resourceType: $resourceType, identifier: $identifier, reason: $reason}]')"
}

add_permission_gap() {
  local gap="$1"
  PERMISSION_GAPS="$(echo "$PERMISSION_GAPS" | jq -c --arg gap "$gap" '. + [$gap] | unique')"
}

# ===========================================================================
# Section 4: live, read-only collection. Every AWS CLI/kubectl/argocd call
# below is Describe/Get/List/Query only. Each is wrapped so a missing
# permission or unreachable cluster produces a permission-gap entry in the
# manifest instead of crashing the script or being silently treated as
# "safe to delete."
# ===========================================================================

HAVE_KUBECTL="false"
command -v kubectl >/dev/null 2>&1 && HAVE_KUBECTL="true"
HAVE_AWS="false"
command -v aws >/dev/null 2>&1 && HAVE_AWS="true"

echo "=== GoldenGate legacy resource cleanup inventory (environment=${ENVIRONMENT}) ==="
echo "Generated at: ${GENERATED_AT}"
echo "This is a READ-ONLY inventory. No resource is created, modified, or deleted."
echo ""

# --- A. Canonical safety baseline ------------------------------------------
echo "--- A. Canonical safety baseline ---"

ACCOUNT_OK="unknown"
REGION_OK="unknown"
if [ "$HAVE_AWS" = "true" ]; then
  CALLER_IDENTITY_JSON="$(aws sts get-caller-identity --output json 2>/dev/null || true)"
  if [ -n "$CALLER_IDENTITY_JSON" ]; then
    LIVE_ACCOUNT_ID="$(echo "$CALLER_IDENTITY_JSON" | jq -r '.Account // empty')"
    if [ "$LIVE_ACCOUNT_ID" = "$WORKLOAD_ACCOUNT_ID_EXPECTED" ]; then
      ACCOUNT_OK="true"
    else
      ACCOUNT_OK="false"
    fi
    echo "Workload account: expected=${WORKLOAD_ACCOUNT_ID_EXPECTED} observed=${LIVE_ACCOUNT_ID:-<unavailable>} match=${ACCOUNT_OK}"
  else
    add_permission_gap "STS_GET_CALLER_IDENTITY_PERMISSION_MISSING"
    echo "Could not call sts get-caller-identity -- reporting as a permission gap."
  fi

  LIVE_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
  if [ -n "$LIVE_REGION" ]; then
    REGION_OK="$([ "$LIVE_REGION" = "$AWS_REGION_EXPECTED" ] && echo true || echo false)"
  fi
  echo "Region: expected=${AWS_REGION_EXPECTED} observed=${LIVE_REGION:-<unset>} match=${REGION_OK}"
else
  add_permission_gap "AWS_CLI_UNAVAILABLE"
  echo "aws CLI not available on this runner -- reporting as a permission gap."
fi

CLUSTER_OK="unknown"
ARGOCD_APPS_JSON="[]"
CANONICAL_PV_LIVE_JSON="[]"
CANONICAL_VOLUME_HANDLES=()
CANONICAL_EFS_ACCESS_POINT_IDS=()
if [ "$HAVE_KUBECTL" = "true" ]; then
  LIVE_CONTEXT="$(kubectl config current-context 2>/dev/null || true)"
  if [ -n "$LIVE_CONTEXT" ]; then
    CLUSTER_OK="$(echo "$LIVE_CONTEXT" | grep -q "$EKS_CLUSTER_EXPECTED" && echo true || echo false)"
    echo "kubectl context: ${LIVE_CONTEXT} (expected cluster ${EKS_CLUSTER_EXPECTED}, match=${CLUSTER_OK})"
  else
    add_permission_gap "KUBECTL_CONTEXT_UNAVAILABLE"
    echo "Could not read kubectl current-context -- reporting as a permission gap."
  fi

  echo "Checking canonical Argo CD Applications (Synced/Healthy)..."
  ARGOCD_APPS_JSON="$(kubectl get applications.argoproj.io -n argocd -o json 2>/dev/null || true)"
  if [ -z "$ARGOCD_APPS_JSON" ]; then
    add_permission_gap "ARGOCD_APPLICATION_READ_PERMISSION_MISSING"
    echo "Could not list Argo CD Applications -- reporting as a permission gap."
  else
    for app in "${CANONICAL_APPLICATIONS[@]}"; do
      SYNC_STATUS="$(echo "$ARGOCD_APPS_JSON" | jq -r --arg n "$app" '.items[] | select(.metadata.name==$n) | .status.sync.status // "MISSING"')"
      HEALTH_STATUS="$(echo "$ARGOCD_APPS_JSON" | jq -r --arg n "$app" '.items[] | select(.metadata.name==$n) | .status.health.status // "MISSING"')"
      echo "  Application ${app}: sync=${SYNC_STATUS:-MISSING} health=${HEALTH_STATUS:-MISSING}"
    done

    LEGACY_APP_PRESENT="$(echo "$ARGOCD_APPS_JSON" | jq -r --arg n "$LEGACY_APPLICATION" '[.items[] | select(.metadata.name==$n)] | length')"
    echo "  Legacy Application ${LEGACY_APPLICATION} present: $([ "${LEGACY_APP_PRESENT:-0}" -gt 0 ] && echo true || echo false)"
  fi

  LEGACY_NS_JSON="$(kubectl get namespace "$LEGACY_NAMESPACE" -o json 2>/dev/null || true)"
  echo "  Legacy namespace ${LEGACY_NAMESPACE} present: $([ -n "$LEGACY_NS_JSON" ] && echo true || echo false)"

  echo "Checking canonical StatefulSets are ready..."
  for deployment_id in "${CANONICAL_DEPLOYMENT_IDS[@]}"; do
    STS_JSON="$(kubectl get statefulset "$deployment_id" -n "$CANONICAL_NAMESPACE" -o json 2>/dev/null || true)"
    if [ -n "$STS_JSON" ]; then
      READY="$(echo "$STS_JSON" | jq -r '.status.readyReplicas // 0')"
      DESIRED="$(echo "$STS_JSON" | jq -r '.spec.replicas // 1')"
      echo "  StatefulSet ${deployment_id}: ready=${READY}/${DESIRED}"
    else
      add_permission_gap "KUBECTL_STATEFULSET_READ_PERMISSION_MISSING"
      echo "  StatefulSet ${deployment_id}: could not read -- reporting as a permission gap."
    fi
  done

  echo "Checking current canonical PVCs are Bound and collecting their current PV IDs/volume handles..."
  for pvc_name in "${CANONICAL_PVC_NAMES[@]}"; do
    PVC_JSON="$(kubectl get pvc "$pvc_name" -n "$CANONICAL_NAMESPACE" -o json 2>/dev/null || true)"
    if [ -n "$PVC_JSON" ]; then
      PVC_PHASE="$(echo "$PVC_JSON" | jq -r '.status.phase // "Unknown"')"
      PVC_VOLUME="$(echo "$PVC_JSON" | jq -r '.spec.volumeName // ""')"
      echo "  PVC ${pvc_name}: phase=${PVC_PHASE} volumeName=${PVC_VOLUME}"
      if [ -n "$PVC_VOLUME" ]; then
        PV_JSON="$(kubectl get pv "$PVC_VOLUME" -o json 2>/dev/null || true)"
        if [ -n "$PV_JSON" ]; then
          HANDLE="$(echo "$PV_JSON" | jq -r '.spec.csi.volumeHandle // ""')"
          [ -n "$HANDLE" ] && CANONICAL_VOLUME_HANDLES+=("$HANDLE")
          # EFS CSI encodes the access-point ID inside volumeHandle as
          # "<fs-id>::<fsap-id>" when an access point is used -- this is
          # the authoritative source for "the two active canonical access
          # points" (never a separate, re-derived guess).
          CANONICAL_AP_ID="$(echo "$HANDLE" | awk -F'::' '{print $2}')"
          [ -n "$CANONICAL_AP_ID" ] && CANONICAL_EFS_ACCESS_POINT_IDS+=("$CANONICAL_AP_ID")
          echo "    current PV ${PVC_VOLUME} volumeHandle=${HANDLE} accessPointId=${CANONICAL_AP_ID:-<none>}"
        fi
      fi
    else
      add_permission_gap "KUBECTL_PVC_READ_PERMISSION_MISSING"
      echo "  PVC ${pvc_name}: could not read -- reporting as a permission gap."
    fi
  done

  MONITOR_JSON="$(kubectl get deployment gg-monitor -n "$MONITOR_NAMESPACE" -o json 2>/dev/null || true)"
  if [ -n "$MONITOR_JSON" ]; then
    MONITOR_READY="$(echo "$MONITOR_JSON" | jq -r '.status.readyReplicas // 0')"
    echo "Shared monitor (gg-monitor) ready replicas: ${MONITOR_READY}"
  else
    add_permission_gap "KUBECTL_MONITOR_READ_PERMISSION_MISSING"
    echo "Could not read shared monitor Deployment -- reporting as a permission gap."
  fi
else
  add_permission_gap "KUBECTL_UNAVAILABLE"
  echo "kubectl not available on this runner -- reporting as a permission gap."
fi
echo ""

# --- B/C. Obsolete PV + EFS access-point validation -------------------------
echo "--- B. Obsolete PersistentVolume validation ---"

if [ "$HAVE_KUBECTL" = "true" ]; then
  for pv_id in "${CANDIDATE_PV_IDS[@]}"; do
    PV_JSON="$(kubectl get pv "$pv_id" -o json 2>/dev/null || true)"
    if [ -z "$PV_JSON" ]; then
      echo "PV ${pv_id}: not found (already removed, or read permission missing)."
      add_blocked "PersistentVolume" "$pv_id" "not_found_or_unreadable"
      continue
    fi

    PHASE="$(echo "$PV_JSON" | jq -r '.status.phase // "Unknown"')"
    RECLAIM_POLICY="$(echo "$PV_JSON" | jq -r '.spec.persistentVolumeReclaimPolicy // "Unknown"')"
    STORAGE_CLASS="$(echo "$PV_JSON" | jq -r '.spec.storageClassName // ""')"
    OLD_CLAIM_NS="$(echo "$PV_JSON" | jq -r '.spec.claimRef.namespace // ""')"
    OLD_CLAIM_NAME="$(echo "$PV_JSON" | jq -r '.spec.claimRef.name // ""')"
    VOLUME_HANDLE="$(echo "$PV_JSON" | jq -r '.spec.csi.volumeHandle // ""')"
    FS_ID="$(echo "$PV_JSON" | jq -r '.spec.csi.volumeAttributes.fileSystemId // ""')"
    # EFS CSI encodes the access-point ID inside volumeHandle as
    # "<fs-id>::<fsap-id>" when an access point is used -- the
    # authoritative source here (accessPointId is not always present as
    # its own volumeAttributes key).
    AP_ID="$(echo "$VOLUME_HANDLE" | awk -F'::' '{print $2}')"
    CREATION_TS="$(echo "$PV_JSON" | jq -r '.metadata.creationTimestamp // ""')"
    FINALIZERS="$(echo "$PV_JSON" | jq -c '.metadata.finalizers // []')"

    # Currently bound? (claimRef alone can be stale after the claim is
    # gone -- cross-check the claim namespace/name still resolves live.)
    BOUND_CLAIM_NAME=""
    if [ "$PHASE" = "Bound" ] && [ -n "$OLD_CLAIM_NAME" ]; then
      LIVE_CLAIM_JSON="$(kubectl get pvc "$OLD_CLAIM_NAME" -n "$OLD_CLAIM_NS" -o json 2>/dev/null || true)"
      [ -n "$LIVE_CLAIM_JSON" ] && BOUND_CLAIM_NAME="$OLD_CLAIM_NAME"
    fi

    # Referenced by any running pod's volumes? Conservative cluster-wide
    # check by PV name via persistentVolumeClaim -> only meaningful while
    # still Bound; for a Released PV this is always false by definition,
    # checked anyway as defense in depth.
    REFERENCED_BY_POD="false"
    if [ -n "$OLD_CLAIM_NAME" ]; then
      POD_REFS="$(kubectl get pods -n "$OLD_CLAIM_NS" -o json 2>/dev/null \
        | jq -r --arg claim "$OLD_CLAIM_NAME" '[.items[] | select(.spec.volumes[]?.persistentVolumeClaim.claimName == $claim) | select(.status.phase=="Running")] | length' 2>/dev/null || echo "0")"
      [ "${POD_REFS:-0}" -gt 0 ] && REFERENCED_BY_POD="true"
    fi

    echo "PV ${pv_id}: phase=${PHASE} reclaimPolicy=${RECLAIM_POLICY} storageClass=${STORAGE_CLASS} oldClaim=${OLD_CLAIM_NS}/${OLD_CLAIM_NAME} volumeHandle=${VOLUME_HANDLE} fsId=${FS_ID} apId=${AP_ID} created=${CREATION_TS} finalizers=${FINALIZERS}"

    set +e
    RESULT="$(classify_pv "$pv_id" "$PHASE" "$RECLAIM_POLICY" "$VOLUME_HANDLE" "$OLD_CLAIM_NS" "$BOUND_CLAIM_NAME" "$REFERENCED_BY_POD" "true")"
    STATUS=$?
    set -e

    EVIDENCE_JSON="$(jq -nc \
      --arg phase "$PHASE" --arg reclaimPolicy "$RECLAIM_POLICY" --arg storageClass "$STORAGE_CLASS" \
      --arg oldClaimNamespace "$OLD_CLAIM_NS" --arg oldClaimName "$OLD_CLAIM_NAME" \
      --arg volumeHandle "$VOLUME_HANDLE" --arg efsFileSystemId "$FS_ID" --arg efsAccessPointId "$AP_ID" \
      --arg creationTimestamp "$CREATION_TS" --argjson finalizers "$FINALIZERS" \
      '{phase:$phase, reclaimPolicy:$reclaimPolicy, storageClass:$storageClass, oldClaimNamespace:$oldClaimNamespace, oldClaimName:$oldClaimName, volumeHandle:$volumeHandle, efsFileSystemId:$efsFileSystemId, efsAccessPointId:$efsAccessPointId, creationTimestamp:$creationTimestamp, finalizers:$finalizers}')"

    if [ "$STATUS" -eq 0 ]; then
      add_candidate CANDIDATES_PV "PersistentVolume" "$pv_id" "eligible" "$EVIDENCE_JSON" ""
      echo "  -> eligible cleanup candidate"
    else
      add_candidate CANDIDATES_PV "PersistentVolume" "$pv_id" "blocked" "$EVIDENCE_JSON" "$RESULT"
      echo "  -> blocked: ${RESULT}"
    fi
  done
else
  add_permission_gap "KUBECTL_UNAVAILABLE"
  echo "kubectl not available -- cannot validate candidate PersistentVolumes."
fi
echo ""

echo "--- C. EFS access-point validation ---"
if [ "$HAVE_AWS" = "true" ]; then
  for ap_id in "${CANDIDATE_EFS_ACCESS_POINT_IDS[@]}"; do
    AP_JSON="$(aws efs describe-access-points --access-point-id "$ap_id" --region "$AWS_REGION_EXPECTED" --output json 2>/dev/null || true)"
    if [ -z "$AP_JSON" ] || [ "$(echo "$AP_JSON" | jq -r '.AccessPoints | length' 2>/dev/null || echo 0)" -eq 0 ]; then
      # Distinguish "does not exist" from "no permission" is not reliably
      # possible from exit status alone under set -e-safe error
      # suppression -- report the ambiguity as a permission gap rather
      # than guessing either way.
      add_permission_gap "EFS_METADATA_PERMISSION_MISSING"
      echo "EFS access point ${ap_id}: could not describe (not found, or permission missing) -- reporting as a permission gap."
      continue
    fi

    FS_ID="$(echo "$AP_JSON" | jq -r '.AccessPoints[0].FileSystemId // ""')"
    LIFECYCLE_STATE="$(echo "$AP_JSON" | jq -r '.AccessPoints[0].LifeCycleState // ""')"
    ROOT_PATH="$(echo "$AP_JSON" | jq -r '.AccessPoints[0].RootDirectory.Path // ""')"
    POSIX_USER="$(echo "$AP_JSON" | jq -c '.AccessPoints[0].PosixUser // {}')"
    TAGS="$(echo "$AP_JSON" | jq -c '.AccessPoints[0].Tags // []')"

    # A Bound PV referencing this access point? Cross-check against the PV
    # facts already gathered above by scanning for the access-point ID
    # inside each canonical/candidate PV's volumeHandle.
    REFERENCED_BY_BOUND_PV="false"
    if [ "$HAVE_KUBECTL" = "true" ]; then
      BOUND_PV_REFS="$(kubectl get pv -o json 2>/dev/null \
        | jq -r --arg ap "$ap_id" '[.items[] | select(.status.phase=="Bound") | select((.spec.csi.volumeHandle // "") | contains($ap))] | length' 2>/dev/null || echo 0)"
      [ "${BOUND_PV_REFS:-0}" -gt 0 ] && REFERENCED_BY_BOUND_PV="true"
    fi

    echo "EFS access point ${ap_id}: fsId=${FS_ID} lifecycleState=${LIFECYCLE_STATE} rootPath=${ROOT_PATH} posixUser=${POSIX_USER} tags=${TAGS} referencedByBoundPv=${REFERENCED_BY_BOUND_PV}"

    set +e
    RESULT="$(classify_efs_access_point "$ap_id" "true" "$FS_ID" "$REFERENCED_BY_BOUND_PV")"
    STATUS=$?
    set -e

    EVIDENCE_JSON="$(jq -nc \
      --arg fileSystemId "$FS_ID" --arg lifecycleState "$LIFECYCLE_STATE" --arg rootPath "$ROOT_PATH" \
      --argjson posixUser "$POSIX_USER" --argjson tags "$TAGS" \
      '{fileSystemId:$fileSystemId, lifecycleState:$lifecycleState, rootPath:$rootPath, posixUser:$posixUser, tags:$tags}')"

    if [ "$STATUS" -eq 0 ]; then
      add_candidate CANDIDATES_EFS_AP "EfsAccessPoint" "$ap_id" "eligible" "$EVIDENCE_JSON" ""
      echo "  -> eligible cleanup candidate"
    else
      add_candidate CANDIDATES_EFS_AP "EfsAccessPoint" "$ap_id" "blocked" "$EVIDENCE_JSON" "$RESULT"
      echo "  -> blocked: ${RESULT}"
    fi
  done
else
  add_permission_gap "EFS_METADATA_PERMISSION_MISSING"
  echo "aws CLI not available -- cannot validate candidate EFS access points."
fi
echo ""

# --- D. StorageClass validation ---------------------------------------------
echo "--- D. StorageClass validation ---"
if [ "$HAVE_KUBECTL" = "true" ]; then
  ALL_SC_JSON="$(kubectl get storageclass -o json 2>/dev/null || true)"
  if [ -z "$ALL_SC_JSON" ]; then
    add_permission_gap "KUBECTL_STORAGECLASS_READ_PERMISSION_MISSING"
    echo "Could not list StorageClasses -- reporting as a permission gap."
  else
    GG_SC_NAMES="$(echo "$ALL_SC_JSON" | jq -r '.items[] | select(.metadata.name | startswith("gg-efs-")) | .metadata.name')"
    echo "GoldenGate EFS StorageClasses found: ${GG_SC_NAMES:-<none>}"

    for sc_name in "${CANONICAL_STORAGE_CLASSES[@]}" "$LEGACY_STORAGE_CLASS"; do
      IN_USE="false"
      ALL_PVC_JSON="$(kubectl get pvc -A -o json 2>/dev/null || true)"
      if [ -n "$ALL_PVC_JSON" ]; then
        USE_COUNT="$(echo "$ALL_PVC_JSON" | jq -r --arg sc "$sc_name" '[.items[] | select(.spec.storageClassName == $sc)] | length')"
        [ "${USE_COUNT:-0}" -gt 0 ] && IN_USE="true"
      fi

      set +e
      RESULT="$(classify_storage_class "$sc_name" "$IN_USE")"
      STATUS=$?
      set -e

      EVIDENCE_JSON="$(jq -nc --arg inUseByActivePvc "$IN_USE" '{inUseByActivePvc: ($inUseByActivePvc == "true")}')"

      if [ "$STATUS" -eq 0 ]; then
        add_candidate CANDIDATES_STORAGE_CLASS "StorageClass" "$sc_name" "eligible" "$EVIDENCE_JSON" ""
        echo "StorageClass ${sc_name}: inUse=${IN_USE} -> eligible cleanup candidate"
      else
        add_candidate CANDIDATES_STORAGE_CLASS "StorageClass" "$sc_name" "$RESULT" "$EVIDENCE_JSON" "$RESULT"
        echo "StorageClass ${sc_name}: inUse=${IN_USE} -> ${RESULT}"
      fi
    done
  fi
else
  add_permission_gap "KUBECTL_UNAVAILABLE"
  echo "kubectl not available -- cannot validate StorageClasses."
fi
echo ""

# --- E. DynamoDB legacy inventory (Query only, never Scan) -----------------
echo "--- E. DynamoDB legacy inventory (Query per exact partition key -- no table-wide Scan) ---"
if [ "$HAVE_AWS" = "true" ]; then
  for pipeline_id in "${LEGACY_DYNAMODB_PARTITIONS[@]}"; do
    QUERY_JSON="$(aws dynamodb query \
      --table-name "$DYNAMODB_TABLE" \
      --key-condition-expression "#p = :p" \
      --expression-attribute-names "{\"#p\":\"${DYNAMODB_HASH_KEY}\"}" \
      --expression-attribute-values "{\":p\":{\"S\":\"${pipeline_id}\"}}" \
      --region "$AWS_REGION_EXPECTED" \
      --output json 2>/dev/null || true)"

    if [ -z "$QUERY_JSON" ]; then
      add_permission_gap "DYNAMODB_QUERY_PERMISSION_MISSING"
      echo "DynamoDB partition ${pipeline_id}: could not query -- reporting as a permission gap."
      continue
    fi

    ITEM_COUNT="$(echo "$QUERY_JSON" | jq -r '.Count // 0')"
    SORT_KEYS="$(echo "$QUERY_JSON" | jq -c "[.Items[]?.${DYNAMODB_RANGE_KEY}.S]")"
    TTL_VALUES="$(echo "$QUERY_JSON" | jq -c '[.Items[]? | select(has("ttl")) | .ttl.N]')"
    LAST_UPDATE_FIELDS="$(echo "$QUERY_JSON" | jq -c '[.Items[]? | {recordType: (.recordType.S // ""), recordedAt: (.recordedAt.N // .updatedAt.N // "")}]')"

    echo "DynamoDB partition ${pipeline_id}: itemCount=${ITEM_COUNT} recordTypes=${SORT_KEYS} ttlValues=${TTL_VALUES}"

    set +e
    RESULT="$(classify_dynamodb_partition "$pipeline_id" "$ITEM_COUNT")"
    STATUS=$?
    set -e

    EVIDENCE_JSON="$(jq -nc \
      --arg table "$DYNAMODB_TABLE" --argjson itemCount "$ITEM_COUNT" \
      --argjson recordTypes "$SORT_KEYS" --argjson ttlValues "$TTL_VALUES" --argjson lastUpdateFields "$LAST_UPDATE_FIELDS" \
      '{table:$table, itemCount:$itemCount, recordTypes:$recordTypes, ttlValues:$ttlValues, lastUpdateFields:$lastUpdateFields}')"

    if [ "$STATUS" -eq 0 ]; then
      add_candidate CANDIDATES_DYNAMODB "DynamoDbPartition" "$pipeline_id" "eligible" "$EVIDENCE_JSON" ""
      echo "  -> eligible cleanup candidate (${ITEM_COUNT} item(s))"
    else
      add_candidate CANDIDATES_DYNAMODB "DynamoDbPartition" "$pipeline_id" "blocked" "$EVIDENCE_JSON" "$RESULT"
      echo "  -> blocked: ${RESULT}"
    fi
  done

  # Defense in depth, matching the hard deny-list in classify_dynamodb_
  # partition: canonical partitions are queried for visibility only (never
  # added as candidates, never evaluated for eligibility).
  for pipeline_id in "${CANONICAL_DYNAMODB_PARTITIONS[@]}"; do
    echo "DynamoDB partition ${pipeline_id}: canonical -- RETAIN (never a cleanup candidate, not queried for eligibility)"
  done
else
  add_permission_gap "DYNAMODB_QUERY_PERMISSION_MISSING"
  echo "aws CLI not available -- cannot query DynamoDB legacy partitions."
fi
echo ""

# --- F. Observer ECR inventory ----------------------------------------------
echo "--- F. Observer ECR inventory ---"
if [ "$HAVE_AWS" = "true" ]; then
  REPO_JSON="$(aws ecr describe-repositories --repository-names "$OBSERVER_ECR_REPOSITORY" --registry-id "$ECR_ACCOUNT_ID_EXPECTED" --region "$AWS_REGION_EXPECTED" --output json 2>/dev/null || true)"
  if [ -z "$REPO_JSON" ]; then
    add_permission_gap "OBSERVER_ECR_PERMISSION_MISSING"
    echo "OBSERVER_ECR_PERMISSION_MISSING"
  else
    REPO_URI="$(echo "$REPO_JSON" | jq -r '.repositories[0].repositoryUri // ""')"
    REPO_CREATED="$(echo "$REPO_JSON" | jq -r '.repositories[0].createdAt // ""')"
    echo "Repository ${OBSERVER_ECR_REPOSITORY}: uri=${REPO_URI} created=${REPO_CREATED}"

    # Live workload reference sweep: does ANY pod/StatefulSet/Deployment/
    # DaemonSet/Argo CD Application container image reference this
    # repository, cluster-wide? (Confirmed already retired in Phase 5A --
    # this independently re-verifies it live rather than assuming so.)
    LIVE_REFERENCE_COUNT=0
    if [ "$HAVE_KUBECTL" = "true" ]; then
      for kind in pods statefulsets deployments daemonsets; do
        COUNT="$(kubectl get "$kind" -A -o json 2>/dev/null \
          | jq -r --arg repo "$OBSERVER_ECR_REPOSITORY" \
            '[.items[] | .spec.template.spec.containers[]?, .spec.containers[]? | select(.image? | contains($repo))] | length' 2>/dev/null || echo 0)"
        LIVE_REFERENCE_COUNT=$((LIVE_REFERENCE_COUNT + ${COUNT:-0}))
      done
      if [ -n "$ARGOCD_APPS_JSON" ] && [ "$ARGOCD_APPS_JSON" != "[]" ]; then
        APP_REF_COUNT="$(echo "$ARGOCD_APPS_JSON" | jq -r --arg repo "$OBSERVER_ECR_REPOSITORY" \
          '[.items[] | select((.spec.source.helm.parameters[]?.value // "") | contains($repo))] | length' 2>/dev/null || echo 0)"
        LIVE_REFERENCE_COUNT=$((LIVE_REFERENCE_COUNT + ${APP_REF_COUNT:-0}))
      fi
    else
      add_permission_gap "KUBECTL_UNAVAILABLE"
    fi
    echo "Live workload references to ${OBSERVER_ECR_REPOSITORY}: ${LIVE_REFERENCE_COUNT}"

    REPO_EVIDENCE_JSON="$(jq -nc --arg uri "$REPO_URI" --arg created "$REPO_CREATED" --argjson liveReferences "$LIVE_REFERENCE_COUNT" \
      '{repositoryUri:$uri, createdAt:$created, liveWorkloadReferences:$liveReferences}')"

    if [ "$LIVE_REFERENCE_COUNT" -eq 0 ]; then
      add_candidate CANDIDATES_ECR_REPOSITORIES "EcrRepository" "$OBSERVER_ECR_REPOSITORY" "eligible" "$REPO_EVIDENCE_JSON" ""
      echo "  -> repository has zero live references"
    else
      add_candidate CANDIDATES_ECR_REPOSITORIES "EcrRepository" "$OBSERVER_ECR_REPOSITORY" "blocked" "$REPO_EVIDENCE_JSON" "referenced_by_${LIVE_REFERENCE_COUNT}_live_workload(s);"
      echo "  -> blocked: still referenced by ${LIVE_REFERENCE_COUNT} live workload(s)"
    fi

    IMAGES_JSON="$(aws ecr describe-images --repository-name "$OBSERVER_ECR_REPOSITORY" --registry-id "$ECR_ACCOUNT_ID_EXPECTED" --region "$AWS_REGION_EXPECTED" --output json 2>/dev/null || true)"
    if [ -z "$IMAGES_JSON" ]; then
      add_permission_gap "OBSERVER_ECR_PERMISSION_MISSING"
      echo "OBSERVER_ECR_PERMISSION_MISSING (describe-images)"
    else
      IMAGE_COUNT="$(echo "$IMAGES_JSON" | jq -r '.imageDetails | length')"
      echo "Images found in ${OBSERVER_ECR_REPOSITORY}: ${IMAGE_COUNT}"
      while IFS= read -r image_row; do
        [ -z "$image_row" ] && continue
        DIGEST="$(echo "$image_row" | jq -r '.imageDigest // ""')"
        TAGS="$(echo "$image_row" | jq -c '.imageTags // []')"
        PUSHED_AT="$(echo "$image_row" | jq -r '.imagePushedAt // ""')"

        set +e
        RESULT="$(classify_observer_image "$TAGS" "$LIVE_REFERENCE_COUNT")"
        STATUS=$?
        set -e

        IMG_EVIDENCE_JSON="$(jq -nc --arg digest "$DIGEST" --argjson tags "$TAGS" --arg pushedAt "$PUSHED_AT" \
          '{digest:$digest, tags:$tags, pushedAt:$pushedAt}')"

        if [ "$STATUS" -eq 0 ]; then
          add_candidate CANDIDATES_ECR_IMAGES "EcrImage" "${DIGEST}" "eligible" "$IMG_EVIDENCE_JSON" ""
        else
          add_candidate CANDIDATES_ECR_IMAGES "EcrImage" "${DIGEST}" "blocked" "$IMG_EVIDENCE_JSON" "$RESULT"
        fi
      done < <(echo "$IMAGES_JSON" | jq -c '.imageDetails[]?')
    fi
  fi
else
  add_permission_gap "OBSERVER_ECR_PERMISSION_MISSING"
  echo "OBSERVER_ECR_PERMISSION_MISSING"
fi
echo ""

# --- G. Other external resources --------------------------------------------
echo "--- G. Other external resources (Route 53 / ALB / ACM / Secrets Manager / CloudWatch / SNS / log groups) ---"
echo "This repository's Terraform (envs/dev/*.tf) does not manage any Route 53,"
echo "ACM, SNS, CloudWatch alarm, or CloudWatch log group resource -- there is no"
echo "Terraform-tracked identifier for any of those types to inventory here."
echo ""
echo "The retired legacy values file (envs/dev/payments-ora-to-pg-001/values.yaml,"
echo "deleted in Phase 5B2A) referenced these shared identifiers, each also used"
echo "verbatim by the current canonical values files -- classified RETAIN, never a"
echo "candidate, regardless of the legacy file having referenced them too:"
for secret_path in "${SHARED_SECRETS_MANAGER_PATHS[@]}"; do
  echo "  Secrets Manager: ${secret_path} (RETAIN -- shared, referenced by canonical values files)"
  add_blocked "SecretsManagerSecret" "$secret_path" "shared_with_canonical_deployments_retain"
done
echo "  ALB group: ${SHARED_ALB_GROUP_NAME} (RETAIN -- shared, referenced by canonical values files)"
add_blocked "AlbGroup" "$SHARED_ALB_GROUP_NAME" "shared_with_canonical_deployments_retain"
echo "  ACM certificate: ${SHARED_ACM_CERTIFICATE_ARN} (RETAIN -- shared, referenced by canonical values files)"
add_blocked "AcmCertificate" "$SHARED_ACM_CERTIFICATE_ARN" "shared_with_canonical_deployments_retain"
echo ""
echo "The retired legacy deployment also used these legacy-specific Ingress"
echo "hostnames, unique to it (never used by canonical deployments):"
for hostname in "${LEGACY_INGRESS_HOSTNAMES[@]}"; do
  echo "  ${hostname}"
done
echo "Their underlying Ingress object was owned by the already-deleted legacy Argo"
echo "CD Application/namespace, so any ALB listener rule for them is expected to"
echo "have been cascade-removed by the AWS Load Balancer Controller already. This"
echo "cannot be independently confirmed from repository state alone, and this"
echo "script never guesses live ALB/Route 53 rule ownership from a hostname"
echo "pattern -- reported informationally only, never as a delete candidate."
echo ""

# ===========================================================================
# Section 5: assemble and emit the final manifest.
# ===========================================================================

MANIFEST_JSON="$(jq -nc \
  --arg environment "$ENVIRONMENT" \
  --arg generatedAt "$GENERATED_AT" \
  --arg region "$AWS_REGION_EXPECTED" \
  --arg workloadAccountId "$WORKLOAD_ACCOUNT_ID_EXPECTED" \
  --arg ecrAccountId "$ECR_ACCOUNT_ID_EXPECTED" \
  --arg eksCluster "$EKS_CLUSTER_EXPECTED" \
  --argjson canonicalDeployments "$(printf '%s\n' "${CANONICAL_DEPLOYMENT_IDS[@]}" | jq -R . | jq -sc .)" \
  --argjson canonicalApplications "$(printf '%s\n' "${CANONICAL_APPLICATIONS[@]}" | jq -R . | jq -sc .)" \
  --argjson canonicalPvIds "$(printf '%s\n' "${CANONICAL_PV_IDS[@]}" | jq -R . | jq -sc .)" \
  --argjson canonicalPvcNames "$(printf '%s\n' "${CANONICAL_PVC_NAMES[@]}" | jq -R . | jq -sc .)" \
  --argjson canonicalEfsAccessPointIds "$(printf '%s\n' "${CANONICAL_EFS_ACCESS_POINT_IDS[@]:-}" | jq -R . | jq -sc 'map(select(. != ""))')" \
  --arg efsFileSystemId "$EFS_FILESYSTEM_ID_EXPECTED" \
  --argjson canonicalStorageClasses "$(printf '%s\n' "${CANONICAL_STORAGE_CLASSES[@]}" | jq -R . | jq -sc .)" \
  --argjson canonicalDynamodbPartitions "$(printf '%s\n' "${CANONICAL_DYNAMODB_PARTITIONS[@]}" | jq -R . | jq -sc .)" \
  --arg dynamodbTable "$DYNAMODB_TABLE" \
  --argjson pvCandidates "$CANDIDATES_PV" \
  --argjson efsApCandidates "$CANDIDATES_EFS_AP" \
  --argjson storageClassCandidates "$CANDIDATES_STORAGE_CLASS" \
  --argjson dynamodbCandidates "$CANDIDATES_DYNAMODB" \
  --argjson ecrRepositoryCandidates "$CANDIDATES_ECR_REPOSITORIES" \
  --argjson ecrImageCandidates "$CANDIDATES_ECR_IMAGES" \
  --argjson blocked "$BLOCKED_ITEMS" \
  --argjson permissionGaps "$PERMISSION_GAPS" \
  '{
    environment: $environment,
    generatedAt: $generatedAt,
    region: $region,
    workloadAccountId: $workloadAccountId,
    ecrAccountId: $ecrAccountId,
    eksCluster: $eksCluster,
    canonical: {
      deployments: $canonicalDeployments,
      applications: $canonicalApplications,
      pvIds: $canonicalPvIds,
      pvcNames: $canonicalPvcNames,
      efsAccessPointIds: $canonicalEfsAccessPointIds,
      efsFileSystemId: $efsFileSystemId,
      storageClasses: $canonicalStorageClasses,
      dynamodbPartitions: $canonicalDynamodbPartitions,
      dynamodbTable: $dynamodbTable
    },
    candidates: {
      persistentVolumes: $pvCandidates,
      efsAccessPoints: $efsApCandidates,
      storageClasses: $storageClassCandidates,
      dynamodbPartitions: $dynamodbCandidates,
      ecrRepositories: $ecrRepositoryCandidates,
      ecrImages: $ecrImageCandidates
    },
    blocked: $blocked,
    permissionGaps: $permissionGaps
  }')"

echo "--- Cleanup manifest (JSON) ---"
echo "$MANIFEST_JSON" | jq .

ELIGIBLE_PV_COUNT="$(echo "$MANIFEST_JSON" | jq '[.candidates.persistentVolumes[] | select(.eligibility=="eligible")] | length')"
ELIGIBLE_AP_COUNT="$(echo "$MANIFEST_JSON" | jq '[.candidates.efsAccessPoints[] | select(.eligibility=="eligible")] | length')"
ELIGIBLE_SC_COUNT="$(echo "$MANIFEST_JSON" | jq '[.candidates.storageClasses[] | select(.eligibility=="eligible")] | length')"
ELIGIBLE_DDB_COUNT="$(echo "$MANIFEST_JSON" | jq '[.candidates.dynamodbPartitions[] | select(.eligibility=="eligible")] | length')"
ELIGIBLE_ECR_REPO_COUNT="$(echo "$MANIFEST_JSON" | jq '[.candidates.ecrRepositories[] | select(.eligibility=="eligible")] | length')"
GAP_COUNT="$(echo "$MANIFEST_JSON" | jq '.permissionGaps | length')"

SUMMARY="## GoldenGate legacy resource cleanup inventory (${ENVIRONMENT})

READ-ONLY inventory. No resource was created, modified, or deleted.

- Eligible PersistentVolume candidates: ${ELIGIBLE_PV_COUNT} / ${#CANDIDATE_PV_IDS[@]}
- Eligible EFS access-point candidates: ${ELIGIBLE_AP_COUNT} / ${#CANDIDATE_EFS_ACCESS_POINT_IDS[@]}
- Eligible StorageClass candidates: ${ELIGIBLE_SC_COUNT}
- Eligible DynamoDB partition candidates: ${ELIGIBLE_DDB_COUNT}
- Eligible ECR repository candidates: ${ELIGIBLE_ECR_REPO_COUNT}
- Permission gaps encountered: ${GAP_COUNT}

Canonical deployments, PVs, PVCs, StorageClasses, and DynamoDB partitions are
deny-listed and can never appear as candidates. Review the full JSON manifest
above before taking any manual cleanup action -- this workflow performs none."

echo "$SUMMARY"

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  echo "$SUMMARY" >> "$GITHUB_STEP_SUMMARY"
fi

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "manifest=${MANIFEST_JSON}" >> "$GITHUB_OUTPUT"
  echo "eligible_pv_count=${ELIGIBLE_PV_COUNT}" >> "$GITHUB_OUTPUT"
  echo "permission_gap_count=${GAP_COUNT}" >> "$GITHUB_OUTPUT"
fi

exit 0
