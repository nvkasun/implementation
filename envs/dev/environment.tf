# Terraform-side consumption of the canonical envs/dev/environment.yaml environment contract (see automation/goldengate-environment.py for the Python-side equivalent -- both derive the same values from the same file, never a second independent schema). Every other envs/dev/*.tf file must reference these locals instead of re-typing AWS region/account IDs/cluster identity/OIDC/DNS/role names literally. Some environment.yaml-derived values (for example the ECR registry, monitor/Argo CD hostnames, ALB group name, ACM certificate ARN, and several IAM role ARNs) have no live Terraform consumer in this root -- they are consumed exclusively through automation/goldengate-environment.py's github-env output by the GitHub Actions workflow/Helm layer, and are deliberately NOT re-declared here as unused Terraform locals (see Live Deploy Fix 6). This file only CONSUMES the live EKS cluster + its IAM OIDC provider (read-only data sources) -- it never creates/modifies EKS, the OIDC provider, VPC, subnets, or the load balancer; those remain aws-cloud-factory-infra's ownership.
locals {
  gg_env_config = yamldecode(file("${path.module}/environment.yaml"))

  gg_env_environment         = local.gg_env_config.environment
  gg_env_region              = local.gg_env_config.aws.region
  gg_env_workload_account_id = local.gg_env_config.aws.workloadAccountId

  gg_env_cluster_name = local.gg_env_config.eks.clusterName
  # Derived, never hardcoded -- the sole cluster ARN construction point for this root.
  gg_env_cluster_arn = "arn:aws:eks:${local.gg_env_region}:${local.gg_env_workload_account_id}:cluster/${local.gg_env_cluster_name}"

  gg_env_oidc_issuer       = local.gg_env_config.eks.oidcIssuer
  gg_env_oidc_hostpath     = trimprefix(local.gg_env_oidc_issuer, "https://")
  gg_env_oidc_provider_arn = "arn:aws:iam::${local.gg_env_workload_account_id}:oidc-provider/${local.gg_env_oidc_hostpath}"

  gg_env_namespaces = local.gg_env_config.namespaces

  gg_env_dns_domain = local.gg_env_config.network.dnsDomain

  gg_env_role_names = local.gg_env_config.iam.roles

  gg_env_efs_shared_security_group_description = local.gg_env_config.efs.sharedSecurityGroupDescription

  gg_env_tags = local.gg_env_config.tags

  gg_env_source_admin_secret_name = "${local.gg_env_environment}/goldengate/source/admin"
  gg_env_target_admin_secret_name = "${local.gg_env_environment}/goldengate/target/admin"
  gg_env_tls_secret_name          = "${local.gg_env_environment}/goldengate/tls-certificate"

  gg_env_runtime_log_group            = "/adcb/goldengate/${local.gg_env_environment}/runtime"
  gg_env_monitor_log_group            = "/adcb/goldengate/${local.gg_env_environment}/monitor"
  gg_env_container_insights_log_group = "/aws/containerinsights/${local.gg_env_cluster_name}/performance"
}

# Live, read-only discovery of the current Cloud Factory EKS cluster named in environment.yaml -- never a hardcoded cluster ARN/OIDC issuer. This root only reads the cluster; it never creates/modifies it.
data "aws_eks_cluster" "target" {
  name = local.gg_env_cluster_name
}

# Read-only verification that the IAM OIDC provider for the discovered cluster already exists (aws-cloud-factory-infra's ownership) -- this data source itself fails the plan if the provider is missing, so GOLDENGATE-EKS-APP never has to create it.
data "aws_iam_openid_connect_provider" "target" {
  arn = local.gg_env_oidc_provider_arn
}

# Fail-closed EKS/OIDC binding contract: every value this root derives from environment.yaml must agree with the live cluster before any other resource in this root may plan. This is the critical safety gate for cluster recreation -- a recreated EKS cluster keeps the SAME name but gets a DIFFERENT OIDC issuer, and every IRSA trust policy in envs/dev/policies/** is only valid for the issuer environment.yaml declares.
resource "terraform_data" "gg_environment_contract" {
  input = local.gg_env_environment

  lifecycle {
    precondition {
      condition     = local.gg_env_environment == var.environment
      error_message = "envs/${var.environment}/environment.yaml: environment (${local.gg_env_environment}) must equal var.environment (${var.environment})."
    }
    precondition {
      condition     = data.aws_eks_cluster.target.name == local.gg_env_cluster_name
      error_message = "envs/${var.environment}/environment.yaml: the live EKS cluster name does not match eks.clusterName -- refusing to proceed."
    }
    precondition {
      condition     = data.aws_eks_cluster.target.status == "ACTIVE"
      error_message = "envs/${var.environment}/environment.yaml: EKS cluster ${local.gg_env_cluster_name} is not ACTIVE (status=${data.aws_eks_cluster.target.status}) -- refusing to proceed."
    }
    precondition {
      condition     = data.aws_eks_cluster.target.arn == local.gg_env_cluster_arn
      error_message = "envs/${var.environment}/environment.yaml: the live EKS cluster ARN (${data.aws_eks_cluster.target.arn}) does not match the derived ARN (${local.gg_env_cluster_arn}) -- account/region/name mismatch."
    }
    precondition {
      condition     = can(regex("^https://oidc\\.eks\\.[a-z]{2}-[a-z]+-\\d\\.amazonaws\\.com/id/[0-9A-Fa-f]{32}$", local.gg_env_oidc_issuer))
      error_message = "envs/${var.environment}/environment.yaml: eks.oidcIssuer does not have the expected EKS OIDC HTTPS issuer form."
    }
    precondition {
      # The critical fresh-cluster safety gate: the recreated EKS keeps the same name but a different OIDC issuer per recreation. If this fails, every generated IRSA trust policy is stale.
      condition     = data.aws_eks_cluster.target.identity[0].oidc[0].issuer == local.gg_env_oidc_issuer
      error_message = "Configured EKS OIDC issuer does not match the live cluster. Update envs/${var.environment}/environment.yaml to the current EKS issuer and regenerate IAM policies before deployment."
    }
    precondition {
      condition     = data.aws_iam_openid_connect_provider.target.url == local.gg_env_oidc_hostpath
      error_message = "envs/${var.environment}/environment.yaml: the discovered IAM OIDC provider URL does not match eks.oidcIssuer's host/path -- refusing to proceed."
    }
  }
}
