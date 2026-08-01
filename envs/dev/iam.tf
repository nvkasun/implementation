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
# Phase 5B1: the DynamoDB/CloudWatch monitoring actions previously granted
# here existed only for the retired observer sidecar (canonical runtime pods
# never had them; the observer wrote its own legacy STATE records and
# published metrics directly). They have been removed -- shared gg-monitor
# now exclusively owns canonical LEASE/STATE# writes and manager-compatible
# metric publication through its own IRSA role. Note: while the legacy
# deployment's observer sidecar remains live (pre-Phase-5B2 retirement), it
# shares this same role, so this reduction also removes its DynamoDB/
# CloudWatch access -- see the Phase 5B2 cleanup runbook.
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
