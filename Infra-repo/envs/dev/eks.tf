# RECONSTRUCTION GAP: the supplied VDR screenshot for this module block is known to also carry a real acm_certificate_arn = "..." line, but the exact ARN value was not provided to this reconstruction. It is intentionally OMITTED below rather than fabricated or guessed -- see the final report's reconstruction-gap list. If the module requires this argument, terraform validate/plan against the real corporate module will surface it as a missing required input, which must be filled in manually from the real VDR source before apply.
module "eks" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-eks-cluster.git?ref=v2.1.6"

  account_id   = local.account_id
  cluster_name = local.cluster_name
  env          = local.environment
  region       = local.region

  iam_role_use_name_prefix = true

  eks_version                           = "1.35"
  compute_mode                          = "node"
  default_ingress                       = true
  enable_nginx                          = false
  scaling_engine                        = "cluster-autoscaler"
  enable_secrets_store_csi              = true
  enable_secrets_store_csi_provider_aws = true
  enable_secrets_store_csi_sync_secret  = true
  enable_cert_manager                   = true

  enable_efs_csi     = true
  enable_bastionhost = true

  control_ec2_instance_ami = "ami-05b1c44652716d8e3"

  enable_opencost             = false
  auto_scaler_tags            = false
  control_ingress_cidr_blocks = ["10.0.0.0/8"]

  eks_managed_node_groups = {
    cloud-factory = {
      instance_types      = ["t2.large"]
      ami_release_version = "1.35.2-20260304"
      min_size            = 1
      max_size            = 3
      desired_size        = 1
    }
  }

  tags = local.common_tags
}

resource "aws_eks_access_entry" "bastion" {
  cluster_name  = module.eks.cluster_name
  principal_arn = local.bastion_role_arn
  type          = "STANDARD"

  tags = merge(local.common_tags, {
    Name = "bastion-access-entry-${local.environment}"
  })

  depends_on = [module.eks]
}

resource "aws_eks_access_policy_association" "bastion" {
  cluster_name  = module.eks.cluster_name
  principal_arn = local.bastion_role_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_access_entry.bastion]
}

resource "aws_eks_access_entry" "goldengate_deploy" {
  cluster_name  = module.eks.cluster_name
  principal_arn = "arn:aws:iam::668311715351:role/GoldenGateEKSDeployRole-dev"
  type          = "STANDARD"

  tags = merge(local.common_tags, {
    Name = "goldengate-deploy-access-entry-${local.environment}"
  })
}

resource "aws_eks_access_policy_association" "goldengate_deploy" {
  cluster_name  = module.eks.cluster_name
  principal_arn = "arn:aws:iam::668311715351:role/GoldenGateEKSDeployRole-dev"
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_access_entry.goldengate_deploy]
}
