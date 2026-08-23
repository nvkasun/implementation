# EFS CSI corporate-tag CreateAccessPoint authorization correction.
#
# ROOT CAUSE (CloudTrail-proven, live gg-poc-dev account): the EFS CSI controller IRSA role
# created by module "eks" above (enable_efs_csi = true) already carries the AWS-managed
# AmazonEFSCSIDriverPolicy, which authorizes elasticfilesystem:CreateAccessPoint only when the
# request's tag keys are constrained to exactly the CSI driver's own ownership tag
# (efs.csi.aws.com/cluster) via a ForAllValues:StringEquals aws:TagKeys condition. The CURRENT
# aws-tf-module-eks-cluster v2.1.6 invocation configures the EFS CSI controller's Helm
# controller.tags with the full set of Cloud Factory corporate tags (ApplicationName,
# BusinessCriticality, BusinessUnit, BusinessUnitOwner, CostCenter, DataClassification,
# Environment, ManagedBy, RequestReference, env, map-migrated), so every dynamic-provisioning
# CreateAccessPoint call also carries those tags -- and the AWS-managed policy's
# ForAllValues:StringEquals condition no longer matches (it requires the request's tag keys to
# be ONLY efs.csi.aws.com/cluster), so the call is denied. This is confirmed by CloudTrail
# (elasticfilesystem:CreateAccessPoint, errorCode=AccessDenied, caller=assumed-role/gg-poc-dev-efs-csi-irsa/...)
# and by the EFS CSI controller/csi-provisioner logs ("GRPC error: code = Unauthenticated Access
# Denied"). It is not a StorageClass, PVC, EFS filesystem, mount/security-group, OIDC, or STS
# problem -- all of those are independently verified healthy.
#
# MODULE-MECHANISM INSPECTION: this Terraform root cannot download the private
# git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-eks-cluster.git?ref=v2.1.6 module
# source locally -- corporate Git authentication is not available in this environment (the same
# "fatal: could not read Username for 'https://github.com'" limitation already documented
# elsewhere in this repository for every other private ADCB module, e.g. the RECONSTRUCTION GAP
# notes on module "rds_postgres_repltest" and module "ogg_security_group_efs"). Its
# outputs.tf/variables.tf could therefore not be inspected to confirm whether it exposes an
# official "extra EFS CSI IAM policy/statement" input or output. Per this task's explicit
# instruction never to guess an unverifiable module argument or output name, this correction is
# implemented instead as the smallest Infra-repo-owned supplementary IAM policy attachment
# against the EXISTING module-created role -- looked up read-only by name, never by an
# unverifiable module output. "${local.cluster_name}-efs-csi-irsa" is the deterministic naming
# contract already visible in eks.tf (module "eks" is invoked with cluster_name =
# local.cluster_name, i.e. "gg-poc-dev"), and matches the live role's confirmed actual name
# (gg-poc-dev-efs-csi-irsa). If the real corporate module is later confirmed to expose an
# official mechanism for this, prefer it and retire this file instead of layering both.
#
# OWNERSHIP: the role's existing out-of-band inline policy (approximately named
# efs-csi-tag-resource, already granting elasticfilesystem:TagResource/UntagResource with
# Resource="*") is NOT represented anywhere in this Terraform root and is deliberately left
# completely untouched here -- it is not imported, renamed, or replaced (this task performs no
# live Terraform mutation, and blindly importing/guessing its exact resource shape would risk
# duplicate/conflicting policy ownership). This new policy is additive, separately named, and
# scoped to exactly one action, so the two policies coexist safely; the role's net effective
# permissions become AmazonEFSCSIDriverPolicy (DescribeAccessPoints, DescribeFileSystems,
# DescribeMountTargets, DeleteAccessPoint, etc., all unchanged) + the existing out-of-band
# TagResource/UntagResource inline policy (unchanged) + this new CreateAccessPoint allowance.
#
# SECURITY SCOPE: this policy grants exactly one action (elasticfilesystem:CreateAccessPoint,
# never elasticfilesystem:* or a wildcard Action), is attached ONLY to the EFS CSI controller
# IRSA role identified above (never the node role, the GoldenGate runtime role, the GitHub
# Actions role, the SSM controller role, or any application ServiceAccount), and still REQUIRES
# the standard CSI ownership request tag (aws:RequestTag/efs.csi.aws.com/cluster) to be present
# on every CreateAccessPoint call -- it only removes the additional, overly-narrow
# ForAllValues:StringEquals restriction that rejected a request carrying any OTHER tag key too.
# It does not touch the role's OIDC trust policy, its IRSA subject
# (system:serviceaccount:kube-system:efs-csi-controller-sa), STS regional endpoint behavior, VPC
# endpoints, or any networking resource.
data "aws_iam_role" "efs_csi_irsa" {
  name = "${local.cluster_name}-efs-csi-irsa"

  depends_on = [module.eks]
}

data "aws_iam_policy_document" "efs_csi_create_access_point" {
  statement {
    sid       = "AllowCreateAccessPointWithCorporateTags"
    effect    = "Allow"
    actions   = ["elasticfilesystem:CreateAccessPoint"]
    resources = ["*"]

    # Mirrors the AWS-managed AmazonEFSCSIDriverPolicy's own requirement that the standard CSI
    # ownership request tag be present -- Null=false means the condition key MUST exist on the
    # request. Deliberately does NOT add a ForAllValues:StringEquals aws:TagKeys condition
    # restricting the request to ONLY that one tag key, which is exactly the restriction that
    # rejects a request additionally carrying the Cloud Factory corporate tags.
    condition {
      test     = "Null"
      variable = "aws:RequestTag/efs.csi.aws.com/cluster"
      values   = ["false"]
    }
  }
}

resource "aws_iam_policy" "efs_csi_create_access_point" {
  name        = "${local.cluster_name}-efs-csi-create-access-point"
  description = "Supplementary allowance for elasticfilesystem:CreateAccessPoint when the request carries Cloud Factory corporate tags in addition to the standard efs.csi.aws.com/cluster ownership tag -- the AWS-managed AmazonEFSCSIDriverPolicy's own CreateAccessPoint condition only permits that one tag key alone, which corporate-tagged dynamic provisioning requests never satisfy."
  policy      = data.aws_iam_policy_document.efs_csi_create_access_point.json

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "efs_csi_create_access_point" {
  role       = data.aws_iam_role.efs_csi_irsa.name
  policy_arn = aws_iam_policy.efs_csi_create_access_point.arn
}
