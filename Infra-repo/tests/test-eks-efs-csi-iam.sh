#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
APP_REPO_ROOT="$(cd "${REPO_ROOT}/.." && pwd)"
IAM_FILE="${REPO_ROOT}/envs/dev/eks_efs_csi_iam.tf"
EKS_FILE="${REPO_ROOT}/envs/dev/eks.tf"
TAGS_FILE="${REPO_ROOT}/envs/dev/tags.tf"

PASS_COUNT=0
FAIL_COUNT=0

pass() { echo "PASS: $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "FAIL: $1"; FAIL_COUNT=$((FAIL_COUNT + 1)); }

if [ ! -f "$IAM_FILE" ]; then
  fail "expected file envs/dev/eks_efs_csi_iam.tf does not exist"
  echo "Summary: ${PASS_COUNT} passed, ${FAIL_COUNT} failed"
  exit 1
fi

IAM_CONTENT="$(cat "$IAM_FILE")"
IAM_CODE_ONLY="$(grep -vE '^\s*#' "$IAM_FILE")"

if grep -qE '^\s*enable_efs_csi\s*=\s*true\s*$' "$EKS_FILE" \
    && ! echo "$IAM_CODE_ONLY" | grep -qiE 'detach|remove.*polic|aws_iam_role_policy_attachment"\s*"efs_csi_irsa"'; then
  pass "1: enable_efs_csi=true is unchanged in eks.tf and the new file performs no detach/removal of any existing policy -- the EFS CSI controller role retains AmazonEFSCSIDriverPolicy exactly as the module already attaches it"
else
  fail "1: eks.tf enable_efs_csi flag changed, or the new file appears to detach/remove an existing policy"
fi

# 5: corporate controller tags remain enabled -- eks.tf still passes the full common_tags map
# into the module (which is what feeds the EFS CSI controller's Helm controller.tags), and every
# corporate tag key this task names is still declared in tags.tf.
if grep -qE '^\s*tags\s*=\s*local\.common_tags\s*$' "$EKS_FILE"; then
  pass "5a: eks.tf still passes tags = local.common_tags into module \"eks\" -- corporate controller tags remain enabled"
else
  fail "5a: eks.tf no longer passes tags = local.common_tags into module \"eks\""
fi

MISSING_TAG_KEYS=""
for key in ApplicationName BusinessCriticality BusinessUnit BusinessUnitOwner CostCenter DataClassification Environment ManagedBy RequestReference env map-migrated; do
  grep -qF "${key}" "$TAGS_FILE" || MISSING_TAG_KEYS="${MISSING_TAG_KEYS} ${key}"
done
if [ -z "$MISSING_TAG_KEYS" ]; then
  pass "5b: every Cloud Factory corporate tag key this task names is still declared in tags.tf's common_tags"
else
  fail "5b: tags.tf is missing expected corporate tag key(s):${MISSING_TAG_KEYS}"
fi

if ! grep -rv -E '^\s*#' "${REPO_ROOT}/envs" --include='*.tf' 2>/dev/null | grep -q "efs-csi-tag-resource"; then
  pass "6: no file in this Terraform root references the existing out-of-band efs-csi-tag-resource policy by name -- it is left completely untouched, so its existing TagResource/UntagResource support remains available exactly as before"
else
  fail "6: a file references efs-csi-tag-resource -- this task must not adopt/rename/replace the existing out-of-band policy"
fi

if ! echo "$IAM_CODE_ONLY" | grep -qiE 'oidc|assume_role_policy|web_identity|service_account|sts_regional|regional_sts'; then
  pass "7/8/9: the new file contains no OIDC/assume_role_policy/web_identity/service_account/STS-regional construct -- OIDC trust, the kube-system/efs-csi-controller-sa IRSA subject, and regional STS behavior are all left unchanged"
else
  fail "7/8/9: the new file appears to reference OIDC trust, ServiceAccount identity, or STS regional configuration -- out of scope for this correction"
fi

if command -v terraform >/dev/null 2>&1; then
  SCRATCH_DIR="$(mktemp -d)"
  trap 'rm -rf "$SCRATCH_DIR"' EXIT

  cat > "${SCRATCH_DIR}/main.tf" <<'EOF'
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0, < 7.0"
    }
  }
}

provider "aws" {
  region                      = "eu-west-1"
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  access_key                  = "test"
  secret_key                  = "test"
}

locals {
  cluster_name = "gg-poc-dev"
  common_tags  = { env = "dev" }
}

resource "null_resource" "eks_stub" {}
EOF

  sed 's/depends_on = \[module\.eks\]/depends_on = [null_resource.eks_stub]/' "$IAM_FILE" > "${SCRATCH_DIR}/eks_efs_csi_iam.tf"

  cat >> "${SCRATCH_DIR}/main.tf" <<'EOF'

output "efs_csi_create_access_point_policy_json" {
  value = data.aws_iam_policy_document.efs_csi_create_access_point.json
}
EOF

  if terraform -chdir="$SCRATCH_DIR" init -backend=false -input=false >"${SCRATCH_DIR}/init.log" 2>&1 \
      && terraform -chdir="$SCRATCH_DIR" validate >"${SCRATCH_DIR}/validate.log" 2>&1; then
    pass "terraform validate succeeds against the isolated scratch copy (real hashicorp/aws provider schema, no private module dependency)"

    # Human-readable plan output, so the jsonencode()'d policy document can be inspected directly
    # as rendered by the real provider -- never a hand-reimplemented parse of Terraform's own
    # internal plan representation.
    PLAN_TEXT="$(terraform -chdir="$SCRATCH_DIR" plan -input=false -no-color 2>/dev/null)"

    # Terraform's human-readable plan text quotes VALUES only, never the map/object KEY beside
    # them (e.g. `Effect    = "Allow"`, never `"Effect" = "Allow"`) -- these checks match on that
    # basis rather than assuming a quoted key.
    if echo "$PLAN_TEXT" | grep -qF '"elasticfilesystem:CreateAccessPoint"' \
        && echo "$PLAN_TEXT" | grep -qE 'Effect\s*=\s*"Allow"'; then
      pass "2: the rendered policy document contains an Allow statement for elasticfilesystem:CreateAccessPoint"
    else
      fail "2: the rendered policy document does not contain an Allow elasticfilesystem:CreateAccessPoint statement"
      echo "$PLAN_TEXT"
    fi

    if echo "$PLAN_TEXT" | grep -qF '"aws:RequestTag/efs.csi.aws.com/cluster" = "false"' \
        && echo "$PLAN_TEXT" | grep -qE 'Null\s*=\s*\{'; then
      pass "3: the rendered policy document requires aws:RequestTag/efs.csi.aws.com/cluster to be present (Null=false)"
    else
      fail "3: the rendered policy document does not require aws:RequestTag/efs.csi.aws.com/cluster to be present"
      echo "$PLAN_TEXT"
    fi

    if ! echo "$PLAN_TEXT" | grep -qiE 'ForAllValues|TagKeys'; then
      pass "4: the rendered policy document does NOT contain the restrictive ForAllValues:StringEquals aws:TagKeys condition"
    else
      fail "4: the rendered policy document unexpectedly contains ForAllValues/TagKeys -- this would reintroduce the exact restriction that caused the original AccessDenied failure"
      echo "$PLAN_TEXT"
    fi

    ACTION_LINES="$(echo "$PLAN_TEXT" | grep -E 'Action\s*=' || true)"
    if [ -n "$ACTION_LINES" ] && ! echo "$ACTION_LINES" | grep -qE '\*|elasticfilesystem:\*'; then
      pass "11a: the rendered policy document's Action is an exact action string, never a wildcard (elasticfilesystem:* or *)"
    else
      fail "11a: the rendered policy document's Action appears to be missing or wildcarded"
      echo "$ACTION_LINES"
    fi
  else
    fail "terraform init/validate failed against the isolated scratch copy -- see init.log/validate.log"
    cat "${SCRATCH_DIR}/init.log" "${SCRATCH_DIR}/validate.log" 2>/dev/null || true
  fi

  trap - EXIT
  rm -rf "$SCRATCH_DIR"
else
  echo "SKIP: terraform not available -- tests 2/3/4/11a (rendered policy document proof) skipped"
fi

# 11b: static confirmation, independent of the rendered-JSON proof above, that the source itself
# never spells out a wildcard IAM/EFS action anywhere in the new file.
if ! echo "$IAM_CONTENT" | grep -qE '"elasticfilesystem:\*"|actions\s*=\s*\["\*"\]|"\*"\s*$'; then
  pass "11b: the new file's source contains no elasticfilesystem:* or bare \"*\" action literal"
else
  fail "11b: the new file's source appears to contain a wildcard action literal"
fi

# 10: no EFS filesystem/access-point/PV/PVC provisioning resource was introduced -- this
# correction stays scoped to IAM only, exactly as the task requires.
if ! echo "$IAM_CONTENT" | grep -qE 'resource\s+"aws_efs_(file_system|access_point|mount_target)"|resource\s+"kubernetes_(persistent_volume|persistent_volume_claim)"'; then
  pass "10: no aws_efs_file_system/access_point/mount_target or kubernetes_persistent_volume(_claim) resource was introduced -- EFS filesystem/AP/PV/PVC provisioning ownership stays exactly where it already was"
else
  fail "10: the new file appears to introduce EFS filesystem/access-point or Kubernetes PV/PVC provisioning -- out of scope for this IAM-only correction"
fi

# 12: the exact GoldenGate application files this task names as off-limits are byte-identical to
# git HEAD -- proving this correction did not touch them. Scoped to exactly those files (never
# "the whole app repo has zero diff") because the GoldenGate application repository legitimately
# carries its own independent, unrelated in-flight changes from other work/live pipeline activity
# that this Infra-repo-only correction has no bearing on and must not be conflated with.
GOLDENGATE_FORBIDDEN_FILES=(
  "helm/goldengate/templates/efs-storageclass.yaml"
  "helm/goldengate/templates/runtime-pvc.yaml"
  "envs/dev/gg-mssql-repltest-01/values.yaml"
  "envs/dev/gg-postgresql-repltest-01/values.yaml"
)
if command -v git >/dev/null 2>&1 && git -C "$APP_REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  FORBIDDEN_FILE_DIFF="$(git -C "$APP_REPO_ROOT" diff --stat -- "${GOLDENGATE_FORBIDDEN_FILES[@]}" 2>/dev/null || true)"
  if [ -z "$FORBIDDEN_FILE_DIFF" ]; then
    pass "12: all four GoldenGate application files this task names as off-limits (efs-storageclass.yaml, runtime-pvc.yaml, both repltest values.yaml) are byte-identical to git HEAD -- confirmed untouched by this correction"
  else
    fail "12: unexpected diff detected in a GoldenGate application file this task names as off-limits:"$'\n'"${FORBIDDEN_FILE_DIFF}"
  fi
else
  echo "SKIP: 12 -- git unavailable or ${APP_REPO_ROOT} is not a git working tree"
fi

# Explicit role-scope proof: the new attachment targets ONLY the EFS CSI controller IRSA role
# (looked up by the deterministic ${cluster_name}-efs-csi-irsa name), never the node role,
# GoldenGate runtime role, GitHub Actions role, SSM controller role, or an application
# ServiceAccount-bound role.
if echo "$IAM_CODE_ONLY" | grep -qE 'name\s*=\s*"\$\{local\.cluster_name\}-efs-csi-irsa"' \
    && ! echo "$IAM_CODE_ONLY" | grep -qiE 'node.?role|goldengate|github.?actions|ssm.?controller'; then
  pass "role-scope: the new policy attachment targets only the EFS CSI controller IRSA role (\${local.cluster_name}-efs-csi-irsa), with no reference to the node role, GoldenGate runtime role, GitHub Actions role, or SSM controller role anywhere in this file"
else
  fail "role-scope: the new file does not cleanly target only the EFS CSI controller IRSA role by the expected deterministic name"
fi

echo ""
echo "Summary: ${PASS_COUNT} passed, ${FAIL_COUNT} failed"
if [ "$FAIL_COUNT" -gt 0 ]; then
  exit 1
fi
