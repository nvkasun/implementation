locals {
  # ---- Account / region (from data sources)
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region

  # ---- Cluster naming
  cluster_name      = "gg-${local.app_prefix}-${local.environment}"
  bastion_role_name = "EKSControllerSSM-${local.cluster_name}-eu"
  bastion_role_arn  = "arn:aws:iam::${local.account_id}:role/${local.bastion_role_name}"

  # ---- Networking (from SSM)
  # vpc_id         = nonsensitive(data.aws_ssm_parameter.vpc_id.value)
  # vpc_cidr       = nonsensitive(data.aws_ssm_parameter.vpc_cidr.value)
  # app_subnet_ids = split(",", nonsensitive(data.aws_ssm_parameter.app_subnet_ids.value))

  # ---- AMIs
  # AL2023 from SSM for self-managed EC2.
  # ec2_ami_id = nonsensitive(data.aws_ssm_parameter.al2023_ami.value)

  # ADCB digi-kubectl golden AMI for the bastion
  # bastion_ami_id = "ami-05b1c44652716d8e3"

  # ---- Self-managed EC2 distribution across AZs
  # self_managed_ec2_azs = {
  #   az1 = 0
  #   az2 = 1
  #   az3 = 2
  # }
}
