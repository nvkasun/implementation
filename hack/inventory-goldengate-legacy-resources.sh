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
MONITOR_SERVICE_NAME="gg-monitor"
MONITOR_SERVICE_PORT="8080"
ENVIRONMENT_EXPECTED="dev"

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

# EFS CSI volumeHandle is either "<fs-id>" or "<fs-id>::<fsap-id>". Any
# other shape is never trusted -- fsId/apId extraction blocks rather than
# guesses when the handle doesn't match this exact shape.
EFS_VOLUME_HANDLE_REGEX='^fs-[0-9a-f]+(::fsap-[0-9a-f]+)?$'

# Only an access point actually available for use is ever eligible -- one
# already mid-delete/errored is never a "candidate".
EFS_ACCEPTABLE_LIFECYCLE_STATES=("available")

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
OBSERVER_ECR_REPOSITORY_URI_EXPECTED="${ECR_ACCOUNT_ID_EXPECTED}.dkr.ecr.${AWS_REGION_EXPECTED}.amazonaws.com/${OBSERVER_ECR_REPOSITORY}"

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

# run_kubectl_json ARG... -- runs `kubectl ARG... -o json`, capturing
# stdout separately from stderr so a genuine "not found" (single-resource
# get) can be distinguished from any other failure (permission denied, API
# unreachable, timeout, ...). A failed or ambiguous read is NEVER treated
# as "not found" -- only an explicit NotFound error is.
# Sets: LAST_KUBECTL_OK ("true"/"false"), LAST_KUBECTL_JSON (valid only
# when OK), LAST_KUBECTL_NOTFOUND ("true" only for a confirmed NotFound on
# a single-resource get; "false" in every other case, including success).
run_kubectl_json() {
  local stderr_file out status
  stderr_file="$(mktemp)"
  set +e
  out="$(kubectl "$@" -o json 2>"$stderr_file")"
  status=$?
  set -e
  if [ "$status" -eq 0 ]; then
    LAST_KUBECTL_OK="true"
    LAST_KUBECTL_JSON="$out"
    LAST_KUBECTL_NOTFOUND="false"
  else
    LAST_KUBECTL_OK="false"
    LAST_KUBECTL_JSON=""
    if grep -qiE "\(NotFound\)|not found|NotFound" "$stderr_file"; then
      LAST_KUBECTL_NOTFOUND="true"
    else
      LAST_KUBECTL_NOTFOUND="false"
    fi
  fi
  rm -f "$stderr_file"
}

# run_kubectl_raw PATH -- `kubectl get --raw PATH`, for a read-only HTTP GET
# against a Service's own API through the API server's built-in proxy
# (never a port-forward/exec shell). Sets LAST_KUBECTL_RAW_OK,
# LAST_KUBECTL_RAW_BODY.
run_kubectl_raw() {
  local path="$1" out status
  set +e
  out="$(kubectl get --raw "$path" 2>/dev/null)"
  status=$?
  set -e
  if [ "$status" -eq 0 ] && [ -n "$out" ]; then
    LAST_KUBECTL_RAW_OK="true"
    LAST_KUBECTL_RAW_BODY="$out"
  else
    LAST_KUBECTL_RAW_OK="false"
    LAST_KUBECTL_RAW_BODY=""
  fi
}

# run_aws_json ARG... -- runs `aws ARG... --output json`, distinguishing a
# confirmed "does not exist" AWS error from any other failure the same way
# run_kubectl_json does. Sets LAST_AWS_OK, LAST_AWS_JSON, LAST_AWS_NOTFOUND.
run_aws_json() {
  local stderr_file out status
  stderr_file="$(mktemp)"
  set +e
  out="$(aws "$@" --output json 2>"$stderr_file")"
  status=$?
  set -e
  if [ "$status" -eq 0 ]; then
    LAST_AWS_OK="true"
    LAST_AWS_JSON="$out"
    LAST_AWS_NOTFOUND="false"
  else
    LAST_AWS_OK="false"
    LAST_AWS_JSON=""
    if grep -qiE "ResourceNotFoundException|NotFoundException|does not exist|NoSuchEntity|RepositoryNotFoundException|ImageNotFoundException" "$stderr_file"; then
      LAST_AWS_NOTFOUND="true"
    else
      LAST_AWS_NOTFOUND="false"
    fi
  fi
  rm -f "$stderr_file"
}

is_valid_efs_volume_handle() {
  [[ "$1" =~ $EFS_VOLUME_HANDLE_REGEX ]]
}

efs_fs_id_from_handle() {
  echo "$1" | awk -F'::' '{print $1}'
}

efs_ap_id_from_handle() {
  echo "$1" | awk -F'::' '{print $2}'
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

# classify_pv PV_ID PHASE RECLAIM_POLICY VOLUME_HANDLE HANDLE_FORMAT_VALID
#             BOUND_CLAIM_NAME POD_REFERENCE_CHECK_VERIFIED
#             REFERENCED_BY_RUNNING_POD CANONICAL_BASELINE_VERIFIED
#
# Prints "eligible" and returns 0 only when every required condition holds
# AND every relevant verification flag is exactly "true". Otherwise prints
# a semicolon-separated list of blocking reasons and returns 1. Fails
# closed: a failed/unavailable read is never converted into "zero
# references" or "eligible" -- POD_REFERENCE_CHECK_VERIFIED=false and
# CANONICAL_BASELINE_VERIFIED=false each independently block, always.
classify_pv() {
  local pv_id="$1" phase="$2" reclaim_policy="$3" volume_handle="$4"
  local handle_format_valid="$5" bound_claim_name="$6"
  local pod_reference_check_verified="$7" referenced_by_running_pod="$8"
  local canonical_baseline_verified="$9"
  local reasons=""

  if [ "$canonical_baseline_verified" != "true" ]; then
    reasons="${reasons}canonical_safety_baseline_not_verified;"
  fi

  if is_canonical_pv_id "$pv_id"; then
    reasons="${reasons}is_current_canonical_pv;"
  fi

  if [ "$phase" != "Released" ]; then
    reasons="${reasons}phase_not_released(${phase});"
  fi

  if [ "$reclaim_policy" != "Retain" ]; then
    reasons="${reasons}reclaim_policy_not_retain(${reclaim_policy});"
  fi

  if [ "$handle_format_valid" != "true" ]; then
    reasons="${reasons}volume_handle_format_unrecognized(${volume_handle});"
  elif is_canonical_volume_handle "$volume_handle"; then
    reasons="${reasons}matches_canonical_volume_handle;"
  fi

  if [ -n "$bound_claim_name" ]; then
    reasons="${reasons}still_referenced_by_active_pvc(${bound_claim_name});"
  fi

  if [ "$pod_reference_check_verified" != "true" ]; then
    reasons="${reasons}pod_reference_check_not_verified;"
  elif [ "$referenced_by_running_pod" = "true" ]; then
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

is_acceptable_efs_lifecycle_state() {
  in_array "$1" "${EFS_ACCEPTABLE_LIFECYCLE_STATES[@]}"
}

# classify_efs_access_point AP_ID DESCRIBE_OK FILESYSTEM_ID LIFECYCLE_STATE
#                            BOUND_PV_REFERENCE_CHECK_VERIFIED
#                            REFERENCED_BY_BOUND_PV CANONICAL_BASELINE_VERIFIED
classify_efs_access_point() {
  local ap_id="$1" describe_ok="$2" filesystem_id="$3" lifecycle_state="$4"
  local bound_pv_reference_check_verified="$5" referenced_by_bound_pv="$6"
  local canonical_baseline_verified="$7"
  local reasons=""

  if [ "$describe_ok" != "true" ]; then
    echo "describe_call_did_not_succeed;"
    return 1
  fi

  if [ "$canonical_baseline_verified" != "true" ]; then
    reasons="${reasons}canonical_safety_baseline_not_verified;"
  fi

  if is_canonical_efs_access_point "$ap_id"; then
    reasons="${reasons}is_current_canonical_access_point;"
  fi

  if [ "$filesystem_id" != "$EFS_FILESYSTEM_ID_EXPECTED" ]; then
    reasons="${reasons}unexpected_filesystem(${filesystem_id});"
  fi

  if ! is_acceptable_efs_lifecycle_state "$lifecycle_state"; then
    reasons="${reasons}unacceptable_lifecycle_state(${lifecycle_state});"
  fi

  if [ "$bound_pv_reference_check_verified" != "true" ]; then
    reasons="${reasons}bound_pv_reference_check_not_verified;"
  elif [ "$referenced_by_bound_pv" = "true" ]; then
    reasons="${reasons}referenced_by_bound_pv;"
  fi

  if [ -z "$reasons" ]; then
    echo "eligible"
    return 0
  fi
  echo "$reasons"
  return 1
}

# classify_legacy_storage_class EXISTS_STATE PVC_USAGE_CHECK_VERIFIED
#                                IN_USE CANONICAL_BASELINE_VERIFIED
#
# EXISTS_STATE is one of: "true" (confirmed present), "false" (confirmed
# absent -- a real kubectl NotFound), "unknown" (existence read failed for
# a reason other than NotFound). Only ever called for the legacy
# StorageClass -- canonical StorageClasses are never classified, only
# recorded RETAIN directly by the caller (add_blocked), so they can never
# appear in candidates.storageClasses.
#
# Return codes: 0 = eligible, 1 = blocked, 2 = already_absent.
classify_legacy_storage_class() {
  local exists_state="$1" pvc_usage_check_verified="$2" in_use="$3"
  local canonical_baseline_verified="$4"
  local reasons=""

  if [ "$exists_state" = "false" ]; then
    echo "already_absent;"
    return 2
  fi

  if [ "$exists_state" != "true" ]; then
    echo "existence_not_verified;"
    return 1
  fi

  if [ "$canonical_baseline_verified" != "true" ]; then
    reasons="${reasons}canonical_safety_baseline_not_verified;"
  fi

  if [ "$pvc_usage_check_verified" != "true" ]; then
    reasons="${reasons}pvc_usage_check_not_verified;"
  elif [ "$in_use" = "true" ]; then
    reasons="${reasons}still_in_use_by_active_pvc;"
  fi

  if [ -z "$reasons" ]; then
    echo "eligible"
    return 0
  fi
  echo "$reasons"
  return 1
}

# classify_dynamodb_partition PIPELINE_ID QUERY_VERIFIED ITEM_COUNT
#                              CANONICAL_BASELINE_VERIFIED
#
# Only ever called for a recognized legacy partition -- canonical
# partitions are never classified, only recorded RETAIN directly by the
# caller (add_blocked); the hard deny-list is therefore enforced at the
# call site, not here, so a canonical partition can never even reach this
# function.
classify_dynamodb_partition() {
  local pipeline_id="$1" query_verified="$2" item_count="$3"
  local canonical_baseline_verified="$4"
  local reasons=""

  if ! in_array "$pipeline_id" "${LEGACY_DYNAMODB_PARTITIONS[@]}"; then
    echo "not_a_recognized_legacy_partition;"
    return 1
  fi

  if [ "$query_verified" != "true" ]; then
    echo "query_not_verified;"
    return 1
  fi

  if [ "$canonical_baseline_verified" != "true" ]; then
    reasons="${reasons}canonical_safety_baseline_not_verified;"
  fi

  if [ "$item_count" -eq 0 ]; then
    reasons="${reasons}no_items_found;"
  fi

  if [ -z "$reasons" ]; then
    echo "eligible"
    return 0
  fi
  echo "$reasons"
  return 1
}

# classify_ecr_repository REPOSITORY_URI EXPECTED_URI_MATCH
#                          WORKLOAD_REFERENCE_CHECK_VERIFIED
#                          ARGO_REFERENCE_CHECK_VERIFIED
#                          LIVE_REFERENCE_COUNT CANONICAL_BASELINE_VERIFIED
classify_ecr_repository() {
  local expected_uri_match="$2" workload_verified="$3" argo_verified="$4"
  local live_reference_count="$5" canonical_baseline_verified="$6"
  local reasons=""

  if [ "$canonical_baseline_verified" != "true" ]; then
    reasons="${reasons}canonical_safety_baseline_not_verified;"
  fi

  if [ "$expected_uri_match" != "true" ]; then
    reasons="${reasons}unexpected_repository_uri;"
  fi

  if [ "$workload_verified" != "true" ]; then
    reasons="${reasons}workload_reference_check_not_verified;"
  fi

  if [ "$argo_verified" != "true" ]; then
    reasons="${reasons}argo_reference_check_not_verified;"
  fi

  if [ "$workload_verified" = "true" ] && [ "$argo_verified" = "true" ] && [ "$live_reference_count" -gt 0 ]; then
    reasons="${reasons}referenced_by_${live_reference_count}_live_workload(s);"
  fi

  if [ -z "$reasons" ]; then
    echo "eligible"
    return 0
  fi
  echo "$reasons"
  return 1
}

# classify_observer_image TAGS_JSON WORKLOAD_REFERENCE_CHECK_VERIFIED
#                          ARGO_REFERENCE_CHECK_VERIFIED
#                          LIVE_REFERENCE_COUNT CANONICAL_BASELINE_VERIFIED
classify_observer_image() {
  local tags_json="$1" workload_verified="$2" argo_verified="$3"
  local live_reference_count="$4" canonical_baseline_verified="$5"
  local reasons=""

  if [ "$canonical_baseline_verified" != "true" ]; then
    reasons="${reasons}canonical_safety_baseline_not_verified;"
  fi

  local matches_pattern
  matches_pattern="$(echo "$tags_json" | jq -r --arg pat "$OBSERVER_ECR_TAG_PATTERN" '[.[] | select(test($pat))] | length > 0')"
  if [ "$matches_pattern" != "true" ]; then
    reasons="${reasons}no_tag_matches_observer_pattern(${tags_json});"
  fi

  if [ "$workload_verified" != "true" ]; then
    reasons="${reasons}workload_reference_check_not_verified;"
  fi

  if [ "$argo_verified" != "true" ]; then
    reasons="${reasons}argo_reference_check_not_verified;"
  fi

  if [ "$workload_verified" = "true" ] && [ "$argo_verified" = "true" ] && [ "$live_reference_count" -gt 0 ]; then
    reasons="${reasons}referenced_by_${live_reference_count}_live_workload(s);"
  fi

  if [ -z "$reasons" ]; then
    echo "eligible"
    return 0
  fi
  echo "$reasons"
  return 1
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

BASELINE_ENVIRONMENT_OK="false"
[ "$ENVIRONMENT" = "$ENVIRONMENT_EXPECTED" ] && BASELINE_ENVIRONMENT_OK="true"
echo "environment: expected=${ENVIRONMENT_EXPECTED} observed=${ENVIRONMENT} match=${BASELINE_ENVIRONMENT_OK}"

BASELINE_ACCOUNT_OK="false"
BASELINE_REGION_OK="false"
if [ "$HAVE_AWS" = "true" ]; then
  run_aws_json sts get-caller-identity
  if [ "$LAST_AWS_OK" = "true" ]; then
    LIVE_ACCOUNT_ID="$(echo "$LAST_AWS_JSON" | jq -r '.Account // empty')"
    [ "$LIVE_ACCOUNT_ID" = "$WORKLOAD_ACCOUNT_ID_EXPECTED" ] && BASELINE_ACCOUNT_OK="true"
    echo "Workload account: expected=${WORKLOAD_ACCOUNT_ID_EXPECTED} observed=${LIVE_ACCOUNT_ID:-<unavailable>} match=${BASELINE_ACCOUNT_OK}"
  else
    add_permission_gap "STS_GET_CALLER_IDENTITY_PERMISSION_MISSING"
    echo "Could not call sts get-caller-identity -- reporting as a permission gap. BASELINE_ACCOUNT_OK remains false."
  fi

  LIVE_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
  if [ -n "$LIVE_REGION" ] && [ "$LIVE_REGION" = "$AWS_REGION_EXPECTED" ]; then
    BASELINE_REGION_OK="true"
  fi
  echo "Region: expected=${AWS_REGION_EXPECTED} observed=${LIVE_REGION:-<unset>} match=${BASELINE_REGION_OK}"
else
  add_permission_gap "AWS_CLI_UNAVAILABLE"
  echo "aws CLI not available on this runner -- reporting as a permission gap. BASELINE_ACCOUNT_OK/BASELINE_REGION_OK remain false."
fi

BASELINE_CLUSTER_OK="false"
BASELINE_APPLICATIONS_OK="false"
BASELINE_LEGACY_APPLICATION_ABSENT="false"
BASELINE_LEGACY_NAMESPACE_ABSENT="false"
BASELINE_STATEFULSETS_READY="false"
BASELINE_PVCS_BOUND="false"
BASELINE_CANONICAL_PV_INFO_OBTAINED="false"
ARGOCD_APPS_JSON=""
ARGOCD_APPS_VERIFIED="false"
CANONICAL_VOLUME_HANDLES=()
CANONICAL_EFS_ACCESS_POINT_IDS=()
PVC_LIST_JSON=""
PVC_LIST_VERIFIED="false"
PV_LIST_JSON=""
PV_LIST_VERIFIED="false"

if [ "$HAVE_KUBECTL" = "true" ]; then
  LIVE_CONTEXT="$(kubectl config current-context 2>/dev/null || true)"
  if [ -n "$LIVE_CONTEXT" ]; then
    [ "$(echo "$LIVE_CONTEXT" | grep -c "$EKS_CLUSTER_EXPECTED")" -gt 0 ] && BASELINE_CLUSTER_OK="true"
    echo "kubectl context: ${LIVE_CONTEXT} (expected cluster ${EKS_CLUSTER_EXPECTED}, match=${BASELINE_CLUSTER_OK})"
  else
    add_permission_gap "KUBECTL_CONTEXT_UNAVAILABLE"
    echo "Could not read kubectl current-context -- reporting as a permission gap."
  fi

  echo "Checking canonical Argo CD Applications (Synced/Healthy)..."
  run_kubectl_json get applications.argoproj.io -n argocd
  if [ "$LAST_KUBECTL_OK" = "true" ]; then
    ARGOCD_APPS_JSON="$LAST_KUBECTL_JSON"
    ARGOCD_APPS_VERIFIED="true"
    ALL_APPS_HEALTHY="true"
    for app in "${CANONICAL_APPLICATIONS[@]}"; do
      SYNC_STATUS="$(echo "$ARGOCD_APPS_JSON" | jq -r --arg n "$app" '.items[] | select(.metadata.name==$n) | .status.sync.status // "MISSING"')"
      HEALTH_STATUS="$(echo "$ARGOCD_APPS_JSON" | jq -r --arg n "$app" '.items[] | select(.metadata.name==$n) | .status.health.status // "MISSING"')"
      echo "  Application ${app}: sync=${SYNC_STATUS:-MISSING} health=${HEALTH_STATUS:-MISSING}"
      { [ "$SYNC_STATUS" = "Synced" ] && [ "$HEALTH_STATUS" = "Healthy" ]; } || ALL_APPS_HEALTHY="false"
    done
    BASELINE_APPLICATIONS_OK="$ALL_APPS_HEALTHY"

    LEGACY_APP_COUNT="$(echo "$ARGOCD_APPS_JSON" | jq -r --arg n "$LEGACY_APPLICATION" '[.items[] | select(.metadata.name==$n)] | length')"
    [ "${LEGACY_APP_COUNT:-1}" -eq 0 ] && BASELINE_LEGACY_APPLICATION_ABSENT="true"
    echo "  Legacy Application ${LEGACY_APPLICATION} present: $([ "$BASELINE_LEGACY_APPLICATION_ABSENT" = "true" ] && echo false || echo true)"
  else
    add_permission_gap "ARGOCD_APPLICATION_READ_PERMISSION_MISSING"
    echo "Could not list Argo CD Applications -- reporting as a permission gap. Applications/legacy-Application checks remain unverified (false)."
  fi

  run_kubectl_json get namespace "$LEGACY_NAMESPACE"
  if [ "$LAST_KUBECTL_NOTFOUND" = "true" ]; then
    BASELINE_LEGACY_NAMESPACE_ABSENT="true"
    echo "  Legacy namespace ${LEGACY_NAMESPACE}: confirmed absent (NotFound)."
  elif [ "$LAST_KUBECTL_OK" = "true" ]; then
    echo "  Legacy namespace ${LEGACY_NAMESPACE}: still exists."
  else
    add_permission_gap "KUBECTL_NAMESPACE_READ_PERMISSION_MISSING"
    echo "  Legacy namespace ${LEGACY_NAMESPACE}: could not verify (neither confirmed present nor confirmed absent) -- reporting as a permission gap."
  fi

  echo "Checking canonical StatefulSets are fully ready..."
  ALL_STS_READY="true"
  for deployment_id in "${CANONICAL_DEPLOYMENT_IDS[@]}"; do
    run_kubectl_json get statefulset "$deployment_id" -n "$CANONICAL_NAMESPACE"
    if [ "$LAST_KUBECTL_OK" = "true" ]; then
      READY="$(echo "$LAST_KUBECTL_JSON" | jq -r '.status.readyReplicas // 0')"
      DESIRED="$(echo "$LAST_KUBECTL_JSON" | jq -r '.spec.replicas // 1')"
      echo "  StatefulSet ${deployment_id}: ready=${READY}/${DESIRED}"
      { [ "$READY" -gt 0 ] && [ "$READY" = "$DESIRED" ]; } || ALL_STS_READY="false"
    else
      add_permission_gap "KUBECTL_STATEFULSET_READ_PERMISSION_MISSING"
      echo "  StatefulSet ${deployment_id}: could not read -- reporting as a permission gap."
      ALL_STS_READY="false"
    fi
  done
  BASELINE_STATEFULSETS_READY="$ALL_STS_READY"

  echo "Checking current canonical PVCs are Bound and collecting their current PV IDs/volume handles..."
  ALL_PVCS_BOUND="true"
  ALL_PV_INFO_OBTAINED="true"
  for pvc_name in "${CANONICAL_PVC_NAMES[@]}"; do
    run_kubectl_json get pvc "$pvc_name" -n "$CANONICAL_NAMESPACE"
    if [ "$LAST_KUBECTL_OK" = "true" ]; then
      PVC_PHASE="$(echo "$LAST_KUBECTL_JSON" | jq -r '.status.phase // "Unknown"')"
      PVC_VOLUME="$(echo "$LAST_KUBECTL_JSON" | jq -r '.spec.volumeName // ""')"
      echo "  PVC ${pvc_name}: phase=${PVC_PHASE} volumeName=${PVC_VOLUME}"
      [ "$PVC_PHASE" = "Bound" ] || ALL_PVCS_BOUND="false"
      if [ -n "$PVC_VOLUME" ]; then
        run_kubectl_json get pv "$PVC_VOLUME"
        if [ "$LAST_KUBECTL_OK" = "true" ]; then
          HANDLE="$(echo "$LAST_KUBECTL_JSON" | jq -r '.spec.csi.volumeHandle // ""')"
          if [ -n "$HANDLE" ] && is_valid_efs_volume_handle "$HANDLE"; then
            CANONICAL_VOLUME_HANDLES+=("$HANDLE")
            CANONICAL_AP_ID="$(efs_ap_id_from_handle "$HANDLE")"
            [ -n "$CANONICAL_AP_ID" ] && CANONICAL_EFS_ACCESS_POINT_IDS+=("$CANONICAL_AP_ID")
            echo "    current PV ${PVC_VOLUME} volumeHandle=${HANDLE} accessPointId=${CANONICAL_AP_ID:-<none>}"
          else
            echo "    current PV ${PVC_VOLUME} volumeHandle=${HANDLE:-<empty>} -- unrecognized format, not trusted"
            ALL_PV_INFO_OBTAINED="false"
          fi
        else
          add_permission_gap "KUBECTL_PV_READ_PERMISSION_MISSING"
          echo "    could not read current PV ${PVC_VOLUME} -- reporting as a permission gap."
          ALL_PV_INFO_OBTAINED="false"
        fi
      else
        ALL_PV_INFO_OBTAINED="false"
      fi
    else
      add_permission_gap "KUBECTL_PVC_READ_PERMISSION_MISSING"
      echo "  PVC ${pvc_name}: could not read -- reporting as a permission gap."
      ALL_PVCS_BOUND="false"
      ALL_PV_INFO_OBTAINED="false"
    fi
  done
  BASELINE_PVCS_BOUND="$ALL_PVCS_BOUND"
  [ "$ALL_PV_INFO_OBTAINED" = "true" ] && [ "${#CANONICAL_VOLUME_HANDLES[@]}" -eq "${#CANONICAL_PVC_NAMES[@]}" ] && BASELINE_CANONICAL_PV_INFO_OBTAINED="true"

  # Global list reads reused across Sections C/D/F -- read exactly once,
  # verified flag reused everywhere it applies rather than re-reading (and
  # rather than letting one section's failure be masked by another
  # section's independent, possibly-successful read of the same kind).
  run_kubectl_json get pvc -A
  if [ "$LAST_KUBECTL_OK" = "true" ]; then
    PVC_LIST_JSON="$LAST_KUBECTL_JSON"
    PVC_LIST_VERIFIED="true"
  else
    add_permission_gap "KUBECTL_PVC_LIST_PERMISSION_MISSING"
    echo "Could not list PVCs cluster-wide -- reporting as a permission gap. StorageClass usage checks remain unverified."
  fi

  run_kubectl_json get pv
  if [ "$LAST_KUBECTL_OK" = "true" ]; then
    PV_LIST_JSON="$LAST_KUBECTL_JSON"
    PV_LIST_VERIFIED="true"
  else
    add_permission_gap "KUBECTL_PV_LIST_PERMISSION_MISSING"
    echo "Could not list PVs cluster-wide -- reporting as a permission gap. EFS access-point Bound-PV checks remain unverified."
  fi
else
  add_permission_gap "KUBECTL_UNAVAILABLE"
  echo "kubectl not available on this runner -- reporting as a permission gap."
fi

# --- Shared monitor functional validation (task section 3) -----------------
echo "Validating shared monitor (gg-monitor) functionally via its own read-only API..."
BASELINE_MONITOR_VALIDATED="false"
if [ "$HAVE_KUBECTL" = "true" ]; then
  run_kubectl_raw "/api/v1/namespaces/${MONITOR_NAMESPACE}/services/${MONITOR_SERVICE_NAME}:${MONITOR_SERVICE_PORT}/proxy/api/status"
  if [ "$LAST_KUBECTL_RAW_OK" = "true" ] && echo "$LAST_KUBECTL_RAW_BODY" | jq -e . >/dev/null 2>&1; then
    MONITOR_STATUS_JSON="$LAST_KUBECTL_RAW_BODY"
    MONITOR_ALL_OK="true"
    for deployment_id in "${CANONICAL_DEPLOYMENT_IDS[@]}"; do
      RUNTIME_JSON="$(echo "$MONITOR_STATUS_JSON" | jq -c --arg n "$deployment_id" \
        '[.logicalPipelines[]?.runtimes[]? | select(.deploymentName == $n)] | first // {}')"
      EFFECTIVE_STATUS="$(echo "$RUNTIME_JSON" | jq -r '.effectiveStatus // "MISSING"')"
      FRESH="$(echo "$RUNTIME_JSON" | jq -r '.fresh // false')"
      DATA_SOURCE="$(echo "$RUNTIME_JSON" | jq -r '.dataSource // ""')"
      METRICS_ENABLED="$(echo "$RUNTIME_JSON" | jq -r '.metricsEnabled // false')"
      ALERTS_ENABLED="$(echo "$RUNTIME_JSON" | jq -r '.alertsEnabled // true')"
      ADMINSRVR="$(echo "$RUNTIME_JSON" | jq -r '.criticalServices.adminsrvr // false')"
      DISTSRVR="$(echo "$RUNTIME_JSON" | jq -r '.criticalServices.distsrvr // false')"
      RECVSRVR="$(echo "$RUNTIME_JSON" | jq -r '.criticalServices.recvsrvr // false')"

      echo "  monitor(${deployment_id}): effectiveStatus=${EFFECTIVE_STATUS} fresh=${FRESH} dataSource=${DATA_SOURCE} metricsEnabled=${METRICS_ENABLED} alertsEnabled=${ALERTS_ENABLED} adminsrvr=${ADMINSRVR} distsrvr=${DISTSRVR} recvsrvr=${RECVSRVR}"

      if [ "$EFFECTIVE_STATUS" != "UP" ] || [ "$FRESH" != "true" ] || [ "$DATA_SOURCE" != "canonical-monitor" ] \
          || [ "$METRICS_ENABLED" != "true" ] || [ "$ALERTS_ENABLED" != "false" ] \
          || [ "$ADMINSRVR" != "true" ] || [ "$DISTSRVR" != "true" ] || [ "$RECVSRVR" != "true" ]; then
        MONITOR_ALL_OK="false"
      fi
    done
    BASELINE_MONITOR_VALIDATED="$MONITOR_ALL_OK"
  else
    add_permission_gap "MONITOR_API_READ_PERMISSION_MISSING"
    echo "Could not query the shared monitor's /api/status via the read-only Service proxy -- reporting as a permission gap."
  fi
else
  add_permission_gap "KUBECTL_UNAVAILABLE"
fi

CANONICAL_BASELINE_VERIFIED="false"
if [ "$BASELINE_ENVIRONMENT_OK" = "true" ] && [ "$BASELINE_ACCOUNT_OK" = "true" ] && [ "$BASELINE_REGION_OK" = "true" ] \
    && [ "$BASELINE_CLUSTER_OK" = "true" ] && [ "$BASELINE_APPLICATIONS_OK" = "true" ] \
    && [ "$BASELINE_LEGACY_APPLICATION_ABSENT" = "true" ] && [ "$BASELINE_LEGACY_NAMESPACE_ABSENT" = "true" ] \
    && [ "$BASELINE_STATEFULSETS_READY" = "true" ] && [ "$BASELINE_PVCS_BOUND" = "true" ] \
    && [ "$BASELINE_CANONICAL_PV_INFO_OBTAINED" = "true" ] && [ "$BASELINE_MONITOR_VALIDATED" = "true" ]; then
  CANONICAL_BASELINE_VERIFIED="true"
fi
echo "CANONICAL_BASELINE_VERIFIED=${CANONICAL_BASELINE_VERIFIED}"
if [ "$CANONICAL_BASELINE_VERIFIED" != "true" ]; then
  echo "One or more canonical safety baseline requirements are false or unknown -- EVERY cleanup candidate in this run will be blocked, regardless of its own individual evidence."
fi
echo ""

# --- B. Obsolete PersistentVolume validation --------------------------------
echo "--- B. Obsolete PersistentVolume validation ---"

if [ "$HAVE_KUBECTL" = "true" ]; then
  for pv_id in "${CANDIDATE_PV_IDS[@]}"; do
    run_kubectl_json get pv "$pv_id"
    if [ "$LAST_KUBECTL_NOTFOUND" = "true" ]; then
      echo "PV ${pv_id}: not found (already removed)."
      add_blocked "PersistentVolume" "$pv_id" "not_found"
      continue
    fi
    if [ "$LAST_KUBECTL_OK" != "true" ]; then
      add_permission_gap "KUBECTL_PV_READ_PERMISSION_MISSING"
      echo "PV ${pv_id}: could not read (permission missing) -- reporting as a permission gap and blocking."
      add_candidate CANDIDATES_PV "PersistentVolume" "$pv_id" "blocked" "{}" "pv_read_not_verified;"
      continue
    fi

    PV_JSON="$LAST_KUBECTL_JSON"
    PHASE="$(echo "$PV_JSON" | jq -r '.status.phase // "Unknown"')"
    RECLAIM_POLICY="$(echo "$PV_JSON" | jq -r '.spec.persistentVolumeReclaimPolicy // "Unknown"')"
    STORAGE_CLASS="$(echo "$PV_JSON" | jq -r '.spec.storageClassName // ""')"
    OLD_CLAIM_NS="$(echo "$PV_JSON" | jq -r '.spec.claimRef.namespace // ""')"
    OLD_CLAIM_NAME="$(echo "$PV_JSON" | jq -r '.spec.claimRef.name // ""')"
    VOLUME_HANDLE="$(echo "$PV_JSON" | jq -r '.spec.csi.volumeHandle // ""')"
    CREATION_TS="$(echo "$PV_JSON" | jq -r '.metadata.creationTimestamp // ""')"
    FINALIZERS="$(echo "$PV_JSON" | jq -c '.metadata.finalizers // []')"

    HANDLE_FORMAT_VALID="false"
    FS_ID=""
    AP_ID=""
    if [ -n "$VOLUME_HANDLE" ] && is_valid_efs_volume_handle "$VOLUME_HANDLE"; then
      HANDLE_FORMAT_VALID="true"
      FS_ID="$(efs_fs_id_from_handle "$VOLUME_HANDLE")"
      AP_ID="$(efs_ap_id_from_handle "$VOLUME_HANDLE")"
    fi

    # Currently bound? (claimRef alone can be stale after the claim is
    # gone -- cross-check the claim namespace/name still resolves live.)
    BOUND_CLAIM_NAME=""
    if [ "$PHASE" = "Bound" ] && [ -n "$OLD_CLAIM_NAME" ]; then
      run_kubectl_json get pvc "$OLD_CLAIM_NAME" -n "$OLD_CLAIM_NS"
      [ "$LAST_KUBECTL_OK" = "true" ] && BOUND_CLAIM_NAME="$OLD_CLAIM_NAME"
    fi

    # Referenced by any running pod's volumes? Only meaningful (and only
    # attempted) when there is a claim namespace/name to check against --
    # with nothing to check, the check is vacuously verified-true and
    # referenced=false, never "unperformed but assumed safe". A failed
    # read is never converted into "zero references".
    POD_REFERENCE_CHECK_VERIFIED="true"
    REFERENCED_BY_POD="false"
    if [ -n "$OLD_CLAIM_NAME" ]; then
      run_kubectl_json get pods -n "$OLD_CLAIM_NS"
      if [ "$LAST_KUBECTL_OK" = "true" ]; then
        POD_REFS="$(echo "$LAST_KUBECTL_JSON" | jq -r --arg claim "$OLD_CLAIM_NAME" \
          '[.items[] | select(.spec.volumes[]?.persistentVolumeClaim.claimName == $claim) | select(.status.phase=="Running")] | length')"
        [ "${POD_REFS:-0}" -gt 0 ] && REFERENCED_BY_POD="true"
      else
        POD_REFERENCE_CHECK_VERIFIED="false"
        add_permission_gap "KUBECTL_POD_LIST_PERMISSION_MISSING"
      fi
    fi

    echo "PV ${pv_id}: phase=${PHASE} reclaimPolicy=${RECLAIM_POLICY} storageClass=${STORAGE_CLASS} oldClaim=${OLD_CLAIM_NS}/${OLD_CLAIM_NAME} volumeHandle=${VOLUME_HANDLE} handleFormatValid=${HANDLE_FORMAT_VALID} fsId=${FS_ID} apId=${AP_ID} created=${CREATION_TS} finalizers=${FINALIZERS} podReferenceCheckVerified=${POD_REFERENCE_CHECK_VERIFIED} referencedByRunningPod=${REFERENCED_BY_POD}"

    set +e
    RESULT="$(classify_pv "$pv_id" "$PHASE" "$RECLAIM_POLICY" "$VOLUME_HANDLE" "$HANDLE_FORMAT_VALID" "$BOUND_CLAIM_NAME" "$POD_REFERENCE_CHECK_VERIFIED" "$REFERENCED_BY_POD" "$CANONICAL_BASELINE_VERIFIED")"
    STATUS=$?
    set -e

    EVIDENCE_JSON="$(jq -nc \
      --arg phase "$PHASE" --arg reclaimPolicy "$RECLAIM_POLICY" --arg storageClass "$STORAGE_CLASS" \
      --arg oldClaimNamespace "$OLD_CLAIM_NS" --arg oldClaimName "$OLD_CLAIM_NAME" \
      --arg volumeHandle "$VOLUME_HANDLE" --argjson handleFormatValid "$([ "$HANDLE_FORMAT_VALID" = "true" ] && echo true || echo false)" \
      --arg efsFileSystemId "$FS_ID" --arg efsAccessPointId "$AP_ID" \
      --arg creationTimestamp "$CREATION_TS" --argjson finalizers "$FINALIZERS" \
      --argjson podReferenceCheckVerified "$([ "$POD_REFERENCE_CHECK_VERIFIED" = "true" ] && echo true || echo false)" \
      --argjson referencedByRunningPod "$([ "$REFERENCED_BY_POD" = "true" ] && echo true || echo false)" \
      '{phase:$phase, reclaimPolicy:$reclaimPolicy, storageClass:$storageClass, oldClaimNamespace:$oldClaimNamespace, oldClaimName:$oldClaimName, volumeHandle:$volumeHandle, handleFormatValid:$handleFormatValid, efsFileSystemId:$efsFileSystemId, efsAccessPointId:$efsAccessPointId, creationTimestamp:$creationTimestamp, finalizers:$finalizers, podReferenceCheckVerified:$podReferenceCheckVerified, referencedByRunningPod:$referencedByRunningPod}')"

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
  for pv_id in "${CANDIDATE_PV_IDS[@]}"; do
    add_candidate CANDIDATES_PV "PersistentVolume" "$pv_id" "blocked" "{}" "kubectl_unavailable;"
  done
fi
echo ""

echo "--- C. EFS access-point validation ---"
if [ "$HAVE_AWS" = "true" ]; then
  for ap_id in "${CANDIDATE_EFS_ACCESS_POINT_IDS[@]}"; do
    run_aws_json efs describe-access-points --access-point-id "$ap_id" --region "$AWS_REGION_EXPECTED"
    if [ "$LAST_AWS_NOTFOUND" = "true" ]; then
      echo "EFS access point ${ap_id}: confirmed not found."
      add_blocked "EfsAccessPoint" "$ap_id" "not_found"
      continue
    fi
    if [ "$LAST_AWS_OK" != "true" ] || [ "$(echo "$LAST_AWS_JSON" | jq -r '.AccessPoints | length')" -eq 0 ]; then
      # A failed or ambiguous describe call is never converted into
      # "does not exist"/"unused" -- always a permission gap plus a
      # blocked candidate.
      add_permission_gap "EFS_METADATA_PERMISSION_MISSING"
      echo "EFS access point ${ap_id}: describe call did not succeed or returned no access point -- reporting as a permission gap and blocking."
      add_candidate CANDIDATES_EFS_AP "EfsAccessPoint" "$ap_id" "blocked" "{}" "describe_call_did_not_succeed;"
      continue
    fi

    AP_JSON="$LAST_AWS_JSON"
    FS_ID="$(echo "$AP_JSON" | jq -r '.AccessPoints[0].FileSystemId // ""')"
    LIFECYCLE_STATE="$(echo "$AP_JSON" | jq -r '.AccessPoints[0].LifeCycleState // ""')"
    ROOT_PATH="$(echo "$AP_JSON" | jq -r '.AccessPoints[0].RootDirectory.Path // ""')"
    POSIX_USER="$(echo "$AP_JSON" | jq -c '.AccessPoints[0].PosixUser // {}')"
    TAGS="$(echo "$AP_JSON" | jq -c '.AccessPoints[0].Tags // []')"

    # A Bound PV referencing this access point? Uses the single global PV
    # list already fetched (and its verified flag) in Section A -- never a
    # fresh, independently-fallible read per access point, and never
    # converted into "false" when that list read failed.
    REFERENCED_BY_BOUND_PV="false"
    if [ "$PV_LIST_VERIFIED" = "true" ]; then
      BOUND_PV_REFS="$(echo "$PV_LIST_JSON" | jq -r --arg ap "$ap_id" \
        '[.items[] | select(.status.phase=="Bound") | select((.spec.csi.volumeHandle // "") | contains($ap))] | length')"
      [ "${BOUND_PV_REFS:-0}" -gt 0 ] && REFERENCED_BY_BOUND_PV="true"
    fi

    echo "EFS access point ${ap_id}: fsId=${FS_ID} lifecycleState=${LIFECYCLE_STATE} rootPath=${ROOT_PATH} posixUser=${POSIX_USER} tags=${TAGS} boundPvReferenceCheckVerified=${PV_LIST_VERIFIED} referencedByBoundPv=${REFERENCED_BY_BOUND_PV}"

    set +e
    RESULT="$(classify_efs_access_point "$ap_id" "true" "$FS_ID" "$LIFECYCLE_STATE" "$PV_LIST_VERIFIED" "$REFERENCED_BY_BOUND_PV" "$CANONICAL_BASELINE_VERIFIED")"
    STATUS=$?
    set -e

    EVIDENCE_JSON="$(jq -nc \
      --arg fileSystemId "$FS_ID" --arg lifecycleState "$LIFECYCLE_STATE" --arg rootPath "$ROOT_PATH" \
      --argjson posixUser "$POSIX_USER" --argjson tags "$TAGS" \
      --argjson boundPvReferenceCheckVerified "$([ "$PV_LIST_VERIFIED" = "true" ] && echo true || echo false)" \
      --argjson referencedByBoundPv "$([ "$REFERENCED_BY_BOUND_PV" = "true" ] && echo true || echo false)" \
      '{fileSystemId:$fileSystemId, lifecycleState:$lifecycleState, rootPath:$rootPath, posixUser:$posixUser, tags:$tags, boundPvReferenceCheckVerified:$boundPvReferenceCheckVerified, referencedByBoundPv:$referencedByBoundPv}')"

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
  for ap_id in "${CANDIDATE_EFS_ACCESS_POINT_IDS[@]}"; do
    add_candidate CANDIDATES_EFS_AP "EfsAccessPoint" "$ap_id" "blocked" "{}" "aws_cli_unavailable;"
  done
fi
echo ""

# --- D. StorageClass validation ---------------------------------------------
echo "--- D. StorageClass validation ---"
if [ "$HAVE_KUBECTL" = "true" ]; then
  # Canonical StorageClasses: RETAIN unconditionally, recorded only in
  # blocked (informational) -- never classified, never placed in
  # candidates.storageClasses.
  for sc_name in "${CANONICAL_STORAGE_CLASSES[@]}"; do
    add_blocked "StorageClass" "$sc_name" "retain_canonical"
    echo "StorageClass ${sc_name}: RETAIN (canonical) -- recorded in blocked, never a candidate."
  done

  # Legacy StorageClass: existence must be proven first (a real kubectl
  # NotFound, not merely an empty/failed read).
  run_kubectl_json get storageclass "$LEGACY_STORAGE_CLASS"
  LEGACY_SC_EXISTS_STATE="unknown"
  if [ "$LAST_KUBECTL_NOTFOUND" = "true" ]; then
    LEGACY_SC_EXISTS_STATE="false"
  elif [ "$LAST_KUBECTL_OK" = "true" ]; then
    LEGACY_SC_EXISTS_STATE="true"
  else
    add_permission_gap "KUBECTL_STORAGECLASS_READ_PERMISSION_MISSING"
  fi

  IN_USE="false"
  if [ "$LEGACY_SC_EXISTS_STATE" = "true" ] && [ "$PVC_LIST_VERIFIED" = "true" ]; then
    USE_COUNT="$(echo "$PVC_LIST_JSON" | jq -r --arg sc "$LEGACY_STORAGE_CLASS" '[.items[] | select(.spec.storageClassName == $sc)] | length')"
    [ "${USE_COUNT:-0}" -gt 0 ] && IN_USE="true"
  fi

  echo "StorageClass ${LEGACY_STORAGE_CLASS}: existsState=${LEGACY_SC_EXISTS_STATE} pvcUsageCheckVerified=${PVC_LIST_VERIFIED} inUse=${IN_USE}"

  set +e
  RESULT="$(classify_legacy_storage_class "$LEGACY_SC_EXISTS_STATE" "$PVC_LIST_VERIFIED" "$IN_USE" "$CANONICAL_BASELINE_VERIFIED")"
  STATUS=$?
  set -e

  EVIDENCE_JSON="$(jq -nc \
    --arg existsState "$LEGACY_SC_EXISTS_STATE" \
    --argjson pvcUsageCheckVerified "$([ "$PVC_LIST_VERIFIED" = "true" ] && echo true || echo false)" \
    --argjson inUseByActivePvc "$([ "$IN_USE" = "true" ] && echo true || echo false)" \
    '{existsState:$existsState, pvcUsageCheckVerified:$pvcUsageCheckVerified, inUseByActivePvc:$inUseByActivePvc}')"

  case "$STATUS" in
    0) add_candidate CANDIDATES_STORAGE_CLASS "StorageClass" "$LEGACY_STORAGE_CLASS" "eligible" "$EVIDENCE_JSON" ""
       echo "  -> eligible cleanup candidate" ;;
    2) add_candidate CANDIDATES_STORAGE_CLASS "StorageClass" "$LEGACY_STORAGE_CLASS" "already_absent" "$EVIDENCE_JSON" "$RESULT"
       echo "  -> already_absent (not eligible -- nothing to clean up)" ;;
    *) add_candidate CANDIDATES_STORAGE_CLASS "StorageClass" "$LEGACY_STORAGE_CLASS" "blocked" "$EVIDENCE_JSON" "$RESULT"
       echo "  -> blocked: ${RESULT}" ;;
  esac
else
  add_permission_gap "KUBECTL_UNAVAILABLE"
  echo "kubectl not available -- cannot validate StorageClasses."
  add_candidate CANDIDATES_STORAGE_CLASS "StorageClass" "$LEGACY_STORAGE_CLASS" "blocked" "{}" "kubectl_unavailable;"
fi
echo ""

# --- E. DynamoDB legacy inventory (Query only, never Scan) -----------------
echo "--- E. DynamoDB legacy inventory (Query per exact partition key -- no table-wide Scan) ---"
for pipeline_id in "${CANONICAL_DYNAMODB_PARTITIONS[@]}"; do
  add_blocked "DynamoDbPartition" "$pipeline_id" "retain_canonical"
  echo "DynamoDB partition ${pipeline_id}: RETAIN (canonical) -- recorded in blocked, never queried for eligibility, never a candidate."
done

if [ "$HAVE_AWS" = "true" ]; then
  for pipeline_id in "${LEGACY_DYNAMODB_PARTITIONS[@]}"; do
    run_aws_json dynamodb query \
      --table-name "$DYNAMODB_TABLE" \
      --key-condition-expression "#p = :p" \
      --expression-attribute-names "{\"#p\":\"${DYNAMODB_HASH_KEY}\"}" \
      --expression-attribute-values "{\":p\":{\"S\":\"${pipeline_id}\"}}" \
      --region "$AWS_REGION_EXPECTED"

    if [ "$LAST_AWS_OK" != "true" ]; then
      add_permission_gap "DYNAMODB_QUERY_PERMISSION_MISSING"
      echo "DynamoDB partition ${pipeline_id}: could not query -- reporting as a permission gap and blocking."
      add_candidate CANDIDATES_DYNAMODB "DynamoDbPartition" "$pipeline_id" "blocked" "{}" "query_not_verified;"
      continue
    fi

    QUERY_JSON="$LAST_AWS_JSON"
    ITEM_COUNT="$(echo "$QUERY_JSON" | jq -r '.Count // 0')"
    SORT_KEYS="$(echo "$QUERY_JSON" | jq -c "[.Items[]?.${DYNAMODB_RANGE_KEY}.S]")"
    TTL_VALUES="$(echo "$QUERY_JSON" | jq -c '[.Items[]? | select(has("ttl")) | .ttl.N]')"
    LAST_UPDATE_FIELDS="$(echo "$QUERY_JSON" | jq -c '[.Items[]? | {recordType: (.recordType.S // ""), recordedAt: (.recordedAt.N // .updatedAt.N // "")}]')"

    echo "DynamoDB partition ${pipeline_id}: itemCount=${ITEM_COUNT} recordTypes=${SORT_KEYS} ttlValues=${TTL_VALUES} queryVerified=true"

    set +e
    RESULT="$(classify_dynamodb_partition "$pipeline_id" "true" "$ITEM_COUNT" "$CANONICAL_BASELINE_VERIFIED")"
    STATUS=$?
    set -e

    EVIDENCE_JSON="$(jq -nc \
      --arg table "$DYNAMODB_TABLE" --argjson itemCount "$ITEM_COUNT" --argjson queryVerified true \
      --argjson recordTypes "$SORT_KEYS" --argjson ttlValues "$TTL_VALUES" --argjson lastUpdateFields "$LAST_UPDATE_FIELDS" \
      '{table:$table, itemCount:$itemCount, queryVerified:$queryVerified, recordTypes:$recordTypes, ttlValues:$ttlValues, lastUpdateFields:$lastUpdateFields}')"

    if [ "$STATUS" -eq 0 ]; then
      add_candidate CANDIDATES_DYNAMODB "DynamoDbPartition" "$pipeline_id" "eligible" "$EVIDENCE_JSON" ""
      echo "  -> eligible cleanup candidate (${ITEM_COUNT} item(s))"
    else
      add_candidate CANDIDATES_DYNAMODB "DynamoDbPartition" "$pipeline_id" "blocked" "$EVIDENCE_JSON" "$RESULT"
      echo "  -> blocked: ${RESULT}"
    fi
  done
else
  add_permission_gap "DYNAMODB_QUERY_PERMISSION_MISSING"
  echo "aws CLI not available -- cannot query DynamoDB legacy partitions."
  for pipeline_id in "${LEGACY_DYNAMODB_PARTITIONS[@]}"; do
    add_candidate CANDIDATES_DYNAMODB "DynamoDbPartition" "$pipeline_id" "blocked" "{}" "aws_cli_unavailable;"
  done
fi
echo ""

# --- F. Observer ECR inventory ----------------------------------------------
echo "--- F. Observer ECR inventory ---"

# Cluster-wide workload reference sweep -- read exactly once (four list
# calls, one per kind), combined into one workloadReferenceCheckVerified
# flag: ALL four must succeed for the count to be trusted at all. A
# failure of any one of them is never converted into "zero references".
WORKLOAD_REFERENCE_CHECK_VERIFIED="false"
LIVE_WORKLOAD_REFERENCE_COUNT=0
if [ "$HAVE_KUBECTL" = "true" ]; then
  ALL_WORKLOAD_LISTS_OK="true"
  for kind in pods statefulsets deployments daemonsets; do
    run_kubectl_json get "$kind" -A
    if [ "$LAST_KUBECTL_OK" = "true" ]; then
      COUNT="$(echo "$LAST_KUBECTL_JSON" | jq -r --arg repo "$OBSERVER_ECR_REPOSITORY" \
        '[.items[] | .spec.template.spec.containers[]?, .spec.containers[]? | select(.image? | contains($repo))] | length')"
      LIVE_WORKLOAD_REFERENCE_COUNT=$((LIVE_WORKLOAD_REFERENCE_COUNT + ${COUNT:-0}))
    else
      add_permission_gap "KUBECTL_${kind^^}_LIST_PERMISSION_MISSING"
      ALL_WORKLOAD_LISTS_OK="false"
    fi
  done
  WORKLOAD_REFERENCE_CHECK_VERIFIED="$ALL_WORKLOAD_LISTS_OK"
else
  add_permission_gap "KUBECTL_UNAVAILABLE"
fi

# Argo CD Application reference inspection -- reuses the single Application
# list already fetched (and its verified flag) in Section A. Inspects both
# spec.source (single-source Applications) and spec.sources[] (multi-
# source Applications) Helm parameter values for an observer repository
# reference. A failed/unavailable Application list is never converted into
# "zero references".
ARGO_REFERENCE_CHECK_VERIFIED="$ARGOCD_APPS_VERIFIED"
if [ "$ARGOCD_APPS_VERIFIED" = "true" ]; then
  ARGO_REF_COUNT="$(echo "$ARGOCD_APPS_JSON" | jq -r --arg repo "$OBSERVER_ECR_REPOSITORY" '
    [.items[] | select(
      ((.spec.source.helm.parameters // [])[]?.value // "" | contains($repo))
      or ((.spec.sources // [])[]?.helm.parameters[]?.value // "" | contains($repo))
    )] | length')"
  LIVE_WORKLOAD_REFERENCE_COUNT=$((LIVE_WORKLOAD_REFERENCE_COUNT + ${ARGO_REF_COUNT:-0}))
else
  add_permission_gap "ARGOCD_APPLICATION_READ_PERMISSION_MISSING"
fi

echo "Observer ECR workload reference sweep: workloadReferenceCheckVerified=${WORKLOAD_REFERENCE_CHECK_VERIFIED} argoReferenceCheckVerified=${ARGO_REFERENCE_CHECK_VERIFIED} liveReferenceCount=${LIVE_WORKLOAD_REFERENCE_COUNT}"

if [ "$HAVE_AWS" = "true" ]; then
  run_aws_json ecr describe-repositories --repository-names "$OBSERVER_ECR_REPOSITORY" --registry-id "$ECR_ACCOUNT_ID_EXPECTED" --region "$AWS_REGION_EXPECTED"
  if [ "$LAST_AWS_NOTFOUND" = "true" ]; then
    echo "Repository ${OBSERVER_ECR_REPOSITORY}: confirmed not found."
    add_blocked "EcrRepository" "$OBSERVER_ECR_REPOSITORY" "not_found"
  elif [ "$LAST_AWS_OK" != "true" ]; then
    add_permission_gap "OBSERVER_ECR_PERMISSION_MISSING"
    echo "OBSERVER_ECR_PERMISSION_MISSING (describe-repositories)"
    add_candidate CANDIDATES_ECR_REPOSITORIES "EcrRepository" "$OBSERVER_ECR_REPOSITORY" "blocked" "{}" "describe_call_did_not_succeed;"
  else
    REPO_URI="$(echo "$LAST_AWS_JSON" | jq -r '.repositories[0].repositoryUri // ""')"
    REPO_CREATED="$(echo "$LAST_AWS_JSON" | jq -r '.repositories[0].createdAt // ""')"
    REPO_URI_MATCH="false"
    [ "$REPO_URI" = "$OBSERVER_ECR_REPOSITORY_URI_EXPECTED" ] && REPO_URI_MATCH="true"
    echo "Repository ${OBSERVER_ECR_REPOSITORY}: uri=${REPO_URI} expectedUriMatch=${REPO_URI_MATCH} created=${REPO_CREATED}"

    set +e
    RESULT="$(classify_ecr_repository "$REPO_URI" "$REPO_URI_MATCH" "$WORKLOAD_REFERENCE_CHECK_VERIFIED" "$ARGO_REFERENCE_CHECK_VERIFIED" "$LIVE_WORKLOAD_REFERENCE_COUNT" "$CANONICAL_BASELINE_VERIFIED")"
    STATUS=$?
    set -e

    REPO_EVIDENCE_JSON="$(jq -nc \
      --arg uri "$REPO_URI" --argjson expectedUriMatch "$([ "$REPO_URI_MATCH" = "true" ] && echo true || echo false)" \
      --arg created "$REPO_CREATED" \
      --argjson workloadReferenceCheckVerified "$([ "$WORKLOAD_REFERENCE_CHECK_VERIFIED" = "true" ] && echo true || echo false)" \
      --argjson argoReferenceCheckVerified "$([ "$ARGO_REFERENCE_CHECK_VERIFIED" = "true" ] && echo true || echo false)" \
      --argjson liveWorkloadReferences "$LIVE_WORKLOAD_REFERENCE_COUNT" \
      '{repositoryUri:$uri, expectedUriMatch:$expectedUriMatch, createdAt:$created, workloadReferenceCheckVerified:$workloadReferenceCheckVerified, argoReferenceCheckVerified:$argoReferenceCheckVerified, liveWorkloadReferences:$liveWorkloadReferences}')"

    if [ "$STATUS" -eq 0 ]; then
      add_candidate CANDIDATES_ECR_REPOSITORIES "EcrRepository" "$OBSERVER_ECR_REPOSITORY" "eligible" "$REPO_EVIDENCE_JSON" ""
      echo "  -> eligible"
    else
      add_candidate CANDIDATES_ECR_REPOSITORIES "EcrRepository" "$OBSERVER_ECR_REPOSITORY" "blocked" "$REPO_EVIDENCE_JSON" "$RESULT"
      echo "  -> blocked: ${RESULT}"
    fi

    run_aws_json ecr describe-images --repository-name "$OBSERVER_ECR_REPOSITORY" --registry-id "$ECR_ACCOUNT_ID_EXPECTED" --region "$AWS_REGION_EXPECTED"
    if [ "$LAST_AWS_OK" != "true" ]; then
      add_permission_gap "OBSERVER_ECR_PERMISSION_MISSING"
      echo "OBSERVER_ECR_PERMISSION_MISSING (describe-images)"
    else
      IMAGE_COUNT="$(echo "$LAST_AWS_JSON" | jq -r '.imageDetails | length')"
      echo "Images found in ${OBSERVER_ECR_REPOSITORY}: ${IMAGE_COUNT}"
      while IFS= read -r image_row; do
        [ -z "$image_row" ] && continue
        DIGEST="$(echo "$image_row" | jq -r '.imageDigest // ""')"
        TAGS="$(echo "$image_row" | jq -c '.imageTags // []')"
        PUSHED_AT="$(echo "$image_row" | jq -r '.imagePushedAt // ""')"

        set +e
        IMG_RESULT="$(classify_observer_image "$TAGS" "$WORKLOAD_REFERENCE_CHECK_VERIFIED" "$ARGO_REFERENCE_CHECK_VERIFIED" "$LIVE_WORKLOAD_REFERENCE_COUNT" "$CANONICAL_BASELINE_VERIFIED")"
        IMG_STATUS=$?
        set -e

        IMG_EVIDENCE_JSON="$(jq -nc \
          --arg digest "$DIGEST" --argjson tags "$TAGS" --arg pushedAt "$PUSHED_AT" \
          --argjson workloadReferenceCheckVerified "$([ "$WORKLOAD_REFERENCE_CHECK_VERIFIED" = "true" ] && echo true || echo false)" \
          --argjson argoReferenceCheckVerified "$([ "$ARGO_REFERENCE_CHECK_VERIFIED" = "true" ] && echo true || echo false)" \
          --argjson liveWorkloadReferences "$LIVE_WORKLOAD_REFERENCE_COUNT" \
          '{digest:$digest, tags:$tags, pushedAt:$pushedAt, workloadReferenceCheckVerified:$workloadReferenceCheckVerified, argoReferenceCheckVerified:$argoReferenceCheckVerified, liveWorkloadReferences:$liveWorkloadReferences}')"

        if [ "$IMG_STATUS" -eq 0 ]; then
          add_candidate CANDIDATES_ECR_IMAGES "EcrImage" "${DIGEST}" "eligible" "$IMG_EVIDENCE_JSON" ""
        else
          add_candidate CANDIDATES_ECR_IMAGES "EcrImage" "${DIGEST}" "blocked" "$IMG_EVIDENCE_JSON" "$IMG_RESULT"
        fi
      done < <(echo "$LAST_AWS_JSON" | jq -c '.imageDetails[]?')
    fi
  fi
else
  add_permission_gap "OBSERVER_ECR_PERMISSION_MISSING"
  echo "OBSERVER_ECR_PERMISSION_MISSING"
  add_candidate CANDIDATES_ECR_REPOSITORIES "EcrRepository" "$OBSERVER_ECR_REPOSITORY" "blocked" "{}" "aws_cli_unavailable;"
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

INVENTORY_COMPLETE="false"
[ "$(echo "$PERMISSION_GAPS" | jq 'length')" -eq 0 ] && INVENTORY_COMPLETE="true"

ELIGIBILITY_READY="false"
[ "$CANONICAL_BASELINE_VERIFIED" = "true" ] && [ "$INVENTORY_COMPLETE" = "true" ] && ELIGIBILITY_READY="true"

BASELINE_JSON="$(jq -nc \
  --argjson verified "$([ "$CANONICAL_BASELINE_VERIFIED" = "true" ] && echo true || echo false)" \
  --argjson environmentOk "$([ "$BASELINE_ENVIRONMENT_OK" = "true" ] && echo true || echo false)" \
  --argjson accountOk "$([ "$BASELINE_ACCOUNT_OK" = "true" ] && echo true || echo false)" \
  --argjson regionOk "$([ "$BASELINE_REGION_OK" = "true" ] && echo true || echo false)" \
  --argjson clusterOk "$([ "$BASELINE_CLUSTER_OK" = "true" ] && echo true || echo false)" \
  --argjson applicationsOk "$([ "$BASELINE_APPLICATIONS_OK" = "true" ] && echo true || echo false)" \
  --argjson legacyApplicationAbsent "$([ "$BASELINE_LEGACY_APPLICATION_ABSENT" = "true" ] && echo true || echo false)" \
  --argjson legacyNamespaceAbsent "$([ "$BASELINE_LEGACY_NAMESPACE_ABSENT" = "true" ] && echo true || echo false)" \
  --argjson statefulSetsReady "$([ "$BASELINE_STATEFULSETS_READY" = "true" ] && echo true || echo false)" \
  --argjson pvcsBound "$([ "$BASELINE_PVCS_BOUND" = "true" ] && echo true || echo false)" \
  --argjson canonicalPvInfoObtained "$([ "$BASELINE_CANONICAL_PV_INFO_OBTAINED" = "true" ] && echo true || echo false)" \
  --argjson monitorValidated "$([ "$BASELINE_MONITOR_VALIDATED" = "true" ] && echo true || echo false)" \
  '{
    verified: $verified,
    environmentOk: $environmentOk,
    accountOk: $accountOk,
    regionOk: $regionOk,
    clusterOk: $clusterOk,
    applicationsOk: $applicationsOk,
    legacyApplicationAbsent: $legacyApplicationAbsent,
    legacyNamespaceAbsent: $legacyNamespaceAbsent,
    statefulSetsReady: $statefulSetsReady,
    pvcsBound: $pvcsBound,
    canonicalPvInfoObtained: $canonicalPvInfoObtained,
    monitorValidated: $monitorValidated
  }')"

MANIFEST_JSON="$(jq -nc \
  --arg environment "$ENVIRONMENT" \
  --arg generatedAt "$GENERATED_AT" \
  --arg region "$AWS_REGION_EXPECTED" \
  --arg workloadAccountId "$WORKLOAD_ACCOUNT_ID_EXPECTED" \
  --arg ecrAccountId "$ECR_ACCOUNT_ID_EXPECTED" \
  --arg eksCluster "$EKS_CLUSTER_EXPECTED" \
  --argjson baseline "$BASELINE_JSON" \
  --argjson canonicalBaselineVerified "$([ "$CANONICAL_BASELINE_VERIFIED" = "true" ] && echo true || echo false)" \
  --argjson inventoryComplete "$([ "$INVENTORY_COMPLETE" = "true" ] && echo true || echo false)" \
  --argjson eligibilityReady "$([ "$ELIGIBILITY_READY" = "true" ] && echo true || echo false)" \
  --argjson canonicalDeployments "$(printf '%s\n' "${CANONICAL_DEPLOYMENT_IDS[@]}" | jq -R . | jq -sc .)" \
  --argjson canonicalApplications "$(printf '%s\n' "${CANONICAL_APPLICATIONS[@]}" | jq -R . | jq -sc .)" \
  --argjson canonicalPvIds "$(printf '%s\n' "${CANONICAL_PV_IDS[@]}" | jq -R . | jq -sc .)" \
  --argjson canonicalPvcNames "$(printf '%s\n' "${CANONICAL_PVC_NAMES[@]}" | jq -R . | jq -sc .)" \
  --argjson canonicalEfsAccessPointIds "$(printf '%s\n' "${CANONICAL_EFS_ACCESS_POINT_IDS[@]:-}" | jq -R . | jq -sc 'map(select(. != ""))')" \
  --arg efsFileSystemId "$EFS_FILESYSTEM_ID_EXPECTED" \
  --argjson canonicalStorageClasses "$(printf '%s\n' "${CANONICAL_STORAGE_CLASSES[@]}" | jq -R . | jq -sc .)" \
  --argjson canonicalDynamodbPartitions "$(printf '%s\n' "${CANONICAL_DYNAMODB_PARTITIONS[@]}" | jq -R . | jq -sc .)" \
  --arg dynamodbTable "$DYNAMODB_TABLE" \
  --argjson pvCandidates "$(echo "$CANDIDATES_PV" | jq -c 'sort_by(.identifier)')" \
  --argjson efsApCandidates "$(echo "$CANDIDATES_EFS_AP" | jq -c 'sort_by(.identifier)')" \
  --argjson storageClassCandidates "$(echo "$CANDIDATES_STORAGE_CLASS" | jq -c 'sort_by(.identifier)')" \
  --argjson dynamodbCandidates "$(echo "$CANDIDATES_DYNAMODB" | jq -c 'sort_by(.identifier)')" \
  --argjson ecrRepositoryCandidates "$(echo "$CANDIDATES_ECR_REPOSITORIES" | jq -c 'sort_by(.identifier)')" \
  --argjson ecrImageCandidates "$(echo "$CANDIDATES_ECR_IMAGES" | jq -c 'sort_by(.identifier)')" \
  --argjson blocked "$(echo "$BLOCKED_ITEMS" | jq -c 'sort_by(.identifier)')" \
  --argjson permissionGaps "$(echo "$PERMISSION_GAPS" | jq -c 'sort')" \
  '{
    environment: $environment,
    generatedAt: $generatedAt,
    region: $region,
    workloadAccountId: $workloadAccountId,
    ecrAccountId: $ecrAccountId,
    eksCluster: $eksCluster,
    baseline: $baseline,
    canonicalBaselineVerified: $canonicalBaselineVerified,
    inventoryComplete: $inventoryComplete,
    eligibilityReady: $eligibilityReady,
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

- Canonical safety baseline verified: ${CANONICAL_BASELINE_VERIFIED}
- Inventory complete (zero permission gaps): ${INVENTORY_COMPLETE}
- Eligibility ready (baseline verified AND inventory complete): ${ELIGIBILITY_READY}
- Eligible PersistentVolume candidates: ${ELIGIBLE_PV_COUNT} / ${#CANDIDATE_PV_IDS[@]}
- Eligible EFS access-point candidates: ${ELIGIBLE_AP_COUNT} / ${#CANDIDATE_EFS_ACCESS_POINT_IDS[@]}
- Eligible StorageClass candidates: ${ELIGIBLE_SC_COUNT}
- Eligible DynamoDB partition candidates: ${ELIGIBLE_DDB_COUNT}
- Eligible ECR repository candidates: ${ELIGIBLE_ECR_REPO_COUNT}
- Permission gaps encountered: ${GAP_COUNT}

Canonical deployments, PVs, PVCs, StorageClasses, and DynamoDB partitions are
deny-listed and can never appear as candidates. No candidate is ever eligible
unless eligibilityReady is true. Review the full JSON manifest above before
taking any manual cleanup action -- this workflow performs none."

echo "$SUMMARY"

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  echo "$SUMMARY" >> "$GITHUB_STEP_SUMMARY"
fi

# Only small scalar outputs go to GITHUB_OUTPUT -- the full (potentially
# large) manifest is never written there. It is printed to the job log and
# the job summary above, which is where it must be reviewed.
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "inventory_complete=${INVENTORY_COMPLETE}" >> "$GITHUB_OUTPUT"
  echo "eligibility_ready=${ELIGIBILITY_READY}" >> "$GITHUB_OUTPUT"
  echo "eligible_pv_count=${ELIGIBLE_PV_COUNT}" >> "$GITHUB_OUTPUT"
  echo "permission_gap_count=${GAP_COUNT}" >> "$GITHUB_OUTPUT"
fi

exit 0
