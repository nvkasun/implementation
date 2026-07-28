module "goldengate_eks_deploy_role_dev" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-iam-role?ref=v2.0.0"

  name          = "GoldenGateEKSDeployRole-dev"
  description   = "Cross-account IAM role for GoldenGate GitHub Actions CodeBuild runner to deploy Helm releases to gg-poc-dev EKS cluster"
  policy_folder = "goldengate-eks-deploy-dev"

  managed_policy_arns = []

  map_migrated        = "comm5TZY31HX9S"
  business_criticality = "Low"
  application_name     = "CloudFactory"
  cost_center          = "219"
  business_unit        = "TechnologyPlatform"
  data_classification  = "General"
  env                  = "dev"
}


module "goldengate_secrets_read_role_dev" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-iam-role.git?ref=v2.0.0"

  name          = "GoldenGateSecretsReadRole-dev"
  description   = "IAM role used by GoldenGate pods to read AWS Secrets Manager secrets through Secrets Store CSI Driver"
  policy_folder = "goldengate-secrets-read-dev"

  managed_policy_arns = []

  map_migrated         = "comm5TZY31HX9S"
  business_criticality = "Low"
  application_name     = "CloudFactory"
  cost_center          = "219"
  business_unit        = "TechnologyPlatform"
  data_classification  = "General"
  env                  = "dev"
}


module "goldengate_monitor_read_role_dev" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-iam-role.git?ref=v2.0.0"

  name          = "GoldenGateMonitorReadRole-dev"
  description   = "Read-only IRSA role for the shared GoldenGate monitoring portal to query GoldenGate deployment state from DynamoDB"
  policy_folder = "goldengate-monitor-read-dev"

  managed_policy_arns = []

  map_migrated         = "comm5TZY31HX9S"
  business_criticality = "Low"
  application_name     = "CloudFactory"
  cost_center          = "219"
  business_unit        = "TechnologyPlatform"
  data_classification  = "General"
  env                  = "dev"
}


module "goldengate_argocd_ecr_read_role_dev" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-iam-role.git?ref=v2.0.0"

  name          = "GoldenGateArgocdECRRead-dev"
  description   = "IRSA role used by the Argo CD ECR token sync CronJob to refresh private GoldenGate Helm OCI repository credentials"
  policy_folder = "argocd-ecr-oci-read-dev"

  managed_policy_arns = []

  map_migrated         = "comm5TZY31HX9S"
  business_criticality = "Low"
  application_name     = "CloudFactory"
  cost_center          = "219"
  business_unit        = "TechnologyPlatform"
  data_classification  = "General"
  env                  = "dev"
}


# Shared, engine-level runtime IRSA roles used by the platform bootstrap
# ServiceAccounts (gg-oracle-sa, gg-postgresql-sa) in the goldengate-dev
# namespace. Trust is scoped exactly to each ServiceAccount via
# system:serviceaccount:goldengate-dev:<sa-name> -- never a wildcard
# namespace/name pattern. Least privilege: each role may read only the
# Secrets Manager objects its own GoldenGate engine candidate needs plus the
# shared TLS certificate. No DynamoDB, CloudWatch, or Kubernetes API
# permissions are granted here; those belong to the shared monitor role
# (goldengate_monitor_read_role_dev above).
module "gg_oracle_dev_runtime_role" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-iam-role.git?ref=v2.0.0"

  name          = "gg-oracle-dev-runtime-role"
  description   = "Least-privilege IRSA role for the shared gg-oracle-sa runtime ServiceAccount to read the Oracle candidate's Secrets Manager objects and the shared TLS certificate"
  policy_folder = "gg-oracle-dev-runtime-role"

  managed_policy_arns = []

  map_migrated         = "comm5TZY31HX9S"
  business_criticality = "Low"
  application_name     = "CloudFactory"
  cost_center          = "219"
  business_unit        = "TechnologyPlatform"
  data_classification  = "General"
  env                  = "dev"
}


module "gg_postgresql_dev_runtime_role" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-iam-role.git?ref=v2.0.0"

  name          = "gg-postgresql-dev-runtime-role"
  description   = "Least-privilege IRSA role for the shared gg-postgresql-sa runtime ServiceAccount to read the PostgreSQL candidate's Secrets Manager objects and the shared TLS certificate"
  policy_folder = "gg-postgresql-dev-runtime-role"

  managed_policy_arns = []

  map_migrated         = "comm5TZY31HX9S"
  business_criticality = "Low"
  application_name     = "CloudFactory"
  cost_center          = "219"
  business_unit        = "TechnologyPlatform"
  data_classification  = "General"
  env                  = "dev"
}