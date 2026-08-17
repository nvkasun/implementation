module "goldengate_eks_deploy_role_dev" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-iam-role?ref=v2.0.0"

  name          = local.gg_env_role_names.eksDeploy
  description   = "Cross-account IAM role for GoldenGate GitHub Actions CodeBuild runner to deploy Helm releases to the ${local.gg_env_cluster_name} EKS cluster"
  policy_folder = "goldengate-eks-deploy-dev"

  managed_policy_arns = []

  map_migrated         = local.tags.map_migrated
  business_criticality = local.tags.business_criticality
  application_name     = local.tags.application_name
  cost_center          = local.tags.cost_center
  business_unit        = local.tags.business_unit
  data_classification  = local.tags.data_classification
  env                  = local.tags.env
}


# ONE common IRSA role for every GoldenGate runtime pod (PostgreSQL, MSSQL, and future engines), all sharing the single gg-runtime-sa identity; DynamoDB/CloudWatch access lives solely on goldengate_monitor_read_role_dev below.
module "goldengate_secrets_read_role_dev" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-iam-role.git?ref=v2.0.0"

  name          = local.gg_env_role_names.runtime
  description   = "IRSA role for the one common GoldenGate runtime ServiceAccount (gg-runtime-sa), shared by every GoldenGate engine: read Secrets Manager objects and decrypt via KMS. No DynamoDB or CloudWatch access -- canonical monitoring state and metrics are owned exclusively by the shared gg-monitor."
  policy_folder = "goldengate-secrets-read-dev"

  managed_policy_arns = []

  map_migrated         = local.tags.map_migrated
  business_criticality = local.tags.business_criticality
  application_name     = local.tags.application_name
  cost_center          = local.tags.cost_center
  business_unit        = local.tags.business_unit
  data_classification  = local.tags.data_classification
  env                  = local.tags.env
}


# IRSA role for the shared monitor (collector + portal); trust: exactly system:serviceaccount:goldengate-monitoring:gg-monitor.
module "goldengate_monitor_read_role_dev" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-iam-role.git?ref=v2.0.0"

  name          = local.gg_env_role_names.monitor
  description   = "IRSA role for the shared GoldenGate monitor (collector + portal): read Secrets Manager objects, read/write gg-eks-pipeline, publish GoldenGate/Pipelines metrics"
  policy_folder = "goldengate-monitor-read-dev"

  managed_policy_arns = []

  map_migrated         = local.tags.map_migrated
  business_criticality = local.tags.business_criticality
  application_name     = local.tags.application_name
  cost_center          = local.tags.cost_center
  business_unit        = local.tags.business_unit
  data_classification  = local.tags.data_classification
  env                  = local.tags.env
}


module "goldengate_argocd_ecr_read_role_dev" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-iam-role.git?ref=v2.0.0"

  name          = local.gg_env_role_names.argocdEcrRead
  description   = "IRSA role used by the Argo CD ECR token sync CronJob to refresh private GoldenGate Helm OCI repository credentials"
  policy_folder = "argocd-ecr-oci-read-dev"

  managed_policy_arns = []

  map_migrated         = local.tags.map_migrated
  business_criticality = local.tags.business_criticality
  application_name     = local.tags.application_name
  cost_center          = local.tags.cost_center
  business_unit        = local.tags.business_unit
  data_classification  = local.tags.data_classification
  env                  = local.tags.env
}


# Dedicated IRSA role for the platform Fluent Bit DaemonSet; trust: exactly system:serviceaccount:goldengate-dev:gg-fluent-bit. Never reused by other roles; the only role permitted to write the /adcb/goldengate/dev/* log groups, and must never carry Secrets Manager/DynamoDB/EFS/K8s-control permissions.
module "goldengate_platform_logging_role_dev" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-iam-role.git?ref=v2.0.0"

  name          = local.gg_env_role_names.platformLogging
  description   = "IRSA role for the platform Fluent Bit DaemonSet: write-only access to the pre-created GoldenGate runtime and monitor CloudWatch Logs groups"
  policy_folder = "goldengate-platform-logging-dev"

  managed_policy_arns = []

  map_migrated         = local.tags.map_migrated
  business_criticality = local.tags.business_criticality
  application_name     = local.tags.application_name
  cost_center          = local.tags.cost_center
  business_unit        = local.tags.business_unit
  data_classification  = local.tags.data_classification
  env                  = local.tags.env
}


# Dedicated IRSA role for the future CloudWatch Observability agent (IAM/Terraform prerequisites only); trust: exactly system:serviceaccount:amazon-cloudwatch:cloudwatch-agent. Publishes ContainerInsights metrics and writes only the pre-created performance log group; never shared with other roles and never writes /adcb/goldengate/dev/* logs.
module "goldengate_cloudwatch_metrics_role_dev" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-iam-role.git?ref=v2.0.0"

  name          = local.gg_env_role_names.cloudwatchMetrics
  description   = "IRSA role for the CloudWatch Agent / OTel Container Insights collectors: publish EKS cluster/node/pod/container metrics and write Container Insights performance events to the pre-created log group"
  policy_folder = "goldengate-cloudwatch-metrics-dev"

  managed_policy_arns = []

  map_migrated         = local.tags.map_migrated
  business_criticality = local.tags.business_criticality
  application_name     = local.tags.application_name
  cost_center          = local.tags.cost_center
  business_unit        = local.tags.business_unit
  data_classification  = local.tags.data_classification
  env                  = local.tags.env
}
