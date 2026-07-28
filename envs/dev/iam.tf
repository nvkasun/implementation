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


# Phase 4: dedicated IRSA role for the shared gg-monitor Deployment
# (goldengate-monitoring-dev/gg-monitor). Modeled on the manager reference
# implementation's "sm_pod" per-pod Service Manager role (terraform/platform/
# iam.tf in the manager reference repository, inspected read-only, not
# modified) -- NOT the manager's own narrower "gg_monitor" role, which is
# read-only and never polls/writes because that responsibility lives entirely
# in the manager's per-pod utility-sidecar. Our shared monitor takes over
# exactly that writer/poller role externally (no utility sidecar in runtime
# pods), so it needs the sm_pod-equivalent write permissions instead.
#
# Trust is scoped to exactly system:serviceaccount:goldengate-monitoring-dev:gg-monitor
# -- never GoldenGateSecretsReadRole-dev, gg-oracle-dev-runtime-role,
# gg-postgresql-dev-runtime-role, or EKSControllerSSM-gg-poc-dev-eu.
#
# Deliberately OMITTED pending live verification (do not guess, matching the
# precedent set for the runtime roles): kms:Decrypt for either DynamoDB or
# Secrets Manager. envs/dev/dynamodb.tf's table module sets
# custom_kms_key_arn = null (no customer-managed key configured there), and
# no Secrets Manager KmsKeyId has been verified for the 3 secrets below (see
# the prior KMS verification task) -- this sandbox has no AWS CLI/credentials
# to confirm either. If a customer-managed key is later confirmed for either,
# add a scoped kms:Decrypt statement then; do not add one on a guess now.
#
# cloudwatch:PutMetricData uses Resource="*" because the CloudWatch API does
# not support resource-level ARNs for this action (same exception the
# manager's own sm_pod policy documents) -- scoped instead via the
# cloudwatch:namespace condition to GoldenGate/Pipelines only.
module "gg_monitor_dev_role" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-iam-role.git?ref=v2.0.0"

  name          = "gg-monitor-dev-role"
  description   = "IRSA role for the shared gg-monitor Deployment: read/write gg-eks-pipeline (CONFIG read, LEASE/STATE write), read runtime admin + TLS secrets, publish GoldenGate/Pipelines CloudWatch metrics"
  policy_folder = "gg-monitor-dev-role"

  managed_policy_arns = []

  map_migrated         = "comm5TZY31HX9S"
  business_criticality = "Low"
  application_name     = "CloudFactory"
  cost_center          = "219"
  business_unit        = "TechnologyPlatform"
  data_classification  = "General"
  env                  = "dev"
}