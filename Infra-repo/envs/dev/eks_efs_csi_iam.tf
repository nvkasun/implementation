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
