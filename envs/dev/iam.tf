module "goldengate_eks_deploy_role_dev" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-iam-role?ref=v2.0.0"

  name          = "GoldenGateEKSDeployRole-dev"
  description   = "Cross-account IAM role for GoldenGate GitHub Actions CodeBuild runner to deploy Helm releases to gg-poc-dev EKS cluster"
  policy_folder = "goldengate-eks-deploy-dev"

  managed_policy_arns = []

  map_migrated         = "comm5TZY31HX9S"
  business_criticality = "Low"
  application_name     = "CloudFactory"
  cost_center          = "219"
  business_unit        = "TechnologyPlatform"
  data_classification  = "General"
  env                  = "dev"
}


# Single shared IAM role for every GoldenGate pod (runtime and shared
# monitor). Trust subjects: gg-oracle-sa, gg-postgresql-sa, gg-monitor
# (see policy_folder/assume_role_policy). Grants Secrets Manager read, KMS
# decrypt, and the DynamoDB/CloudWatch access the shared monitor needs to
# read CONFIG and own LEASE/STATE#.
module "goldengate_secrets_read_role_dev" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-iam-role.git?ref=v2.0.0"

  name          = "GoldenGateSecretsReadRole-dev"
  description   = "Shared IRSA role for GoldenGate runtime pods and the shared monitor: read Secrets Manager objects, read/write gg-eks-pipeline, publish GoldenGate/Pipelines metrics"
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
