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


# IRSA role for GoldenGate runtime pods: canonical (gg-oracle-sa,
# gg-postgresql-sa) and the retired legacy deployment's ServiceAccount
# subject (ogg-oracle-sa), which still exists live and still needs Secrets
# Manager/KMS access for its CSI-mounted credentials until that legacy
# deployment is retired in a later phase. The shared monitor uses its OWN
# role (goldengate_monitor_read_role_dev below), not this one.
#
# This role's policy previously also granted DynamoDB (gg-eks-pipeline)
# and CloudWatch (GoldenGate/Pipelines PutMetricData) actions. Because this
# role is shared by every runtime pod's ServiceAccount, those permissions
# were available to canonical pods too, even though only the now-retired
# observer sidecars ever actually required or used them -- canonical
# GoldenGate application containers never called DynamoDB or CloudWatch.
# Shared gg-monitor now exclusively owns canonical LEASE/STATE# writes and
# manager-compatible metric publication through its own IRSA role
# (goldengate_monitor_read_role_dev below), so those two statements have
# been removed from this policy.
#
# IMPORTANT: the retained legacy deployment's observer pods are still live
# and still assume this same role. Do not apply this IAM reduction
# (terraform apply) while those legacy observer pods are still running --
# doing so removes their DynamoDB/CloudWatch access immediately. Apply this
# change only after the legacy observer pods have been retired.
module "goldengate_secrets_read_role_dev" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-iam-role.git?ref=v2.0.0"

  name          = "GoldenGateSecretsReadRole-dev"
  description   = "IRSA role for GoldenGate runtime pods (canonical and legacy): read Secrets Manager objects and decrypt via KMS. No DynamoDB or CloudWatch access -- canonical monitoring state and metrics are owned exclusively by the shared gg-monitor."
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


# IRSA role for the shared monitor (collector + portal). Trust: exactly
# system:serviceaccount:goldengate-monitoring:gg-monitor.
module "goldengate_monitor_read_role_dev" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-iam-role.git?ref=v2.0.0"

  name          = "GoldenGateMonitorReadRole-dev"
  description   = "IRSA role for the shared GoldenGate monitor (collector + portal): read Secrets Manager objects, read/write gg-eks-pipeline, publish GoldenGate/Pipelines metrics"
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


# Phase 6A: dedicated IRSA role for the platform-level Fluent Bit DaemonSet
# (helm/goldengate-platform, fluentBit.create=true). Trust: exactly
# system:serviceaccount:goldengate-dev:gg-fluent-bit -- see
# envs/dev/policies/goldengate-platform-logging-dev/assume_role_policy/sts.json.
# Deliberately its own role, never a reuse of GoldenGateSecretsReadRole-dev,
# GoldenGateMonitorReadRole-dev, or GoldenGateEKSDeployRole-dev: this is the
# only role in this environment that may write to the pre-created
# /adcb/goldengate/dev/runtime and /adcb/goldengate/dev/monitor CloudWatch
# Logs groups (see envs/dev/cloudwatch_logs.tf), and it must never carry any
# Secrets Manager, DynamoDB, EFS, or Kubernetes control permission.
module "goldengate_platform_logging_role_dev" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-iam-role.git?ref=v2.0.0"

  name          = "GoldenGatePlatformLoggingRole-dev"
  description   = "IRSA role for the platform Fluent Bit DaemonSet: write-only access to the pre-created GoldenGate runtime and monitor CloudWatch Logs groups"
  policy_folder = "goldengate-platform-logging-dev"

  managed_policy_arns = []

  map_migrated         = "comm5TZY31HX9S"
  business_criticality = "Low"
  application_name     = "CloudFactory"
  cost_center          = "219"
  business_unit        = "TechnologyPlatform"
  data_classification  = "General"
  env                  = "dev"
}
