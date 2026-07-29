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
# =====================================================================
# KMS DEPLOYMENT GATE -- DO NOT DISMISS. gg-monitor-dev-role is NOT safe to
# apply/use until the items below are verified and this comment is updated.
# =====================================================================
# The manager reference implementation's own "sm_pod" role (this role's
# structural template) DOES include kms:Decrypt/kms:DescribeKey, and this repository's own
# already-successful, already-working runtime roles (GoldenGateSecretsReadRole-dev
# and friends) also grant kms:Decrypt/kms:DescribeKey. That precedent is real and is NOT being
# dismissed here -- it means this role is very likely MISSING a required
# permission right now, not that KMS access is unnecessary.
#
# kms:Decrypt/kms:DescribeKey is deliberately OMITTED below (not guessed, matching the
# precedent already set for the two runtime roles in this same file) because
# the exact encryption configuration has not been verified live for any of:
#   - DynamoDB table gg-eks-pipeline (envs/dev/dynamodb.tf's table module
#     sets custom_kms_key_arn = null, i.e. no customer-managed key configured
#     THERE, but the module's own internal default SSE type -- AWS owned key,
#     AWS managed key, or a CMK -- has not been confirmed live)
#   - dev/goldengate/source/admin
#   - dev/goldengate/target/admin
#   - dev/goldengate/tls-certificate
# This sandbox has no AWS CLI/credentials to run the verification commands
# (aws dynamodb describe-table / aws secretsmanager describe-secret
# --query KmsKeyId) -- guessing a key ARN or alias here would repeat the
# exact mistake already reverted once in this repository's IAM history.
#
# BEFORE this role can be safely deployed:
#   1. Run, read-only, against each of the 4 resources above:
#        aws dynamodb describe-table --table-name gg-eks-pipeline \
#          --query 'Table.SSEDescription'
#        aws secretsmanager describe-secret --secret-id <name> --query KmsKeyId
#   2. If every result shows an AWS-owned/no-CMK default: no kms:Decrypt/kms:DescribeKey
#      statement is required at all; update this comment to say so and
#      close this gate.
#   3. If any result is a customer-managed key ARN: add a least-privilege
#      kms:Decrypt/kms:DescribeKey statement scoped to that EXACT key ARN, with
#      kms:ViaService (dynamodb.eu-west-1.amazonaws.com or
#      secretsmanager.eu-west-1.amazonaws.com as applicable),
#      kms:CallerAccount = 668311715351, and the same encryption-context
#      condition pattern already proven elsewhere in this repository
#      (see envs/dev/policies/goldengate-monitor-read-dev/policies/policies_1.json
#      for the DynamoDB kms:ViaService pattern). Also confirm the CMK's own
#      key policy permits gg-monitor-dev-role (or account-level IAM
#      delegation) to call kms:Decrypt/kms:DescribeKey -- an IAM statement alone is not
#      sufficient if the key policy does not also allow it.
#   4. Do not apply this Terraform module against real AWS until step 2 or 3
#      has been completed for all 4 resources.
#
# =====================================================================
# KMS OPERATOR VERIFICATION REQUIREMENT (correction pass -- restates the
# above as an explicit pre-deployment checklist item, unchanged in
# substance). Before deploying the shared monitor IAM changes, an
# AWS-enabled operator must verify the live gg-eks-pipeline encryption
# configuration. The operator must compare:
#   - DynamoDB SSEDescription.KMSMasterKeyArn
#   - the existing IAM policy KMS resource, where present
# Possible outcomes:
#   1. AWS-owned/default DynamoDB encryption: no additional application
#      KMS permission should normally be required.
#   2. AWS-managed DynamoDB key: confirm whether any explicit
#      monitor-role KMS permission is required.
#   3. Customer-managed KMS key: confirm the monitor role and key policy
#      permit the required DynamoDB access.
# Do not decide which outcome applies without live AWS evidence -- this
# local pass does not and cannot make that determination.
# Read-only command for the final manual checklist (documented here, NOT
# run in this local environment):
#   aws dynamodb describe-table \
#     --table-name gg-eks-pipeline \
#     --region eu-west-1 \
#     --query 'Table.{TableName:TableName,SSE:SSEDescription}'
# This is a documentation-only restatement -- no KMS key, alias, key
# policy, grant, or DynamoDB encryption setting is added, removed, or
# modified by this correction pass, and no KMS ARN is guessed anywhere in
# this file.
# =====================================================================
#
# cloudwatch:PutMetricData uses Resource="*" because the CloudWatch API does
# not support resource-level ARNs for this action (same exception the
# manager's own sm_pod policy documents) -- scoped instead via the
# cloudwatch:namespace condition to GoldenGate/Pipelines only. This is not
# part of the KMS gate above; PutMetricData needs no KMS permission.
#
# CLOUDWATCH IS NOT REQUIRED FOR THE CURRENT PHASE. CONFIG.metricsEnabled
# defaults to false (envs/dev/dynamodb.tf) and gg_monitor_core.py gates
# every cloudwatch:PutMetricData call behind it -- the monitor starts,
# becomes Ready, polls, and writes LEASE/STATE with zero use of this
# statement in the current default configuration.
#
# HARD APPLICATION-LEVEL KILL SWITCH (correction pass): CONFIG.metricsEnabled
# alone is not a sufficient CloudWatch gate, because this table's CONFIG
# item is protected by lifecycle.ignore_changes = [item] (see
# envs/dev/dynamodb.tf) -- an already-applied CONFIG item can carry
# metricsEnabled=true forever, and Terraform will never correct it on a
# later apply. CLOUDWATCH_PUBLISH_ENABLED (env var on the gg-monitor
# Deployment, helm/gg-monitor, default "false", not set to true in any
# environment values file) is a SEPARATE, code-level gate CONFIG cannot
# override: gg_monitor_core.cloudwatch_enabled_for() requires BOTH
# CLOUDWATCH_PUBLISH_ENABLED=true AND CONFIG.metricsEnabled=true before any
# cloudwatch:PutMetricData call is even attempted. An already-applied
# CONFIG item with metricsEnabled=true therefore CANNOT by itself activate
# CloudWatch while CLOUDWATCH_PUBLISH_ENABLED remains false. The one-time
# CONFIG reconciliation described earlier in this file (Scenario B) is
# still required for DATA CONSISTENCY (so the live CONFIG item accurately
# reflects this repository's intended defaults), but is no longer the sole
# thing standing between an old CONFIG item and live CloudWatch calls.
# CloudWatch must not be enabled (CLOUDWATCH_PUBLISH_ENABLED=true) until a
# later, separately approved phase -- this correction pass does not enable
# it anywhere.
#
# It remains staged (not
# broadened) for a later, separately validated CloudWatch phase rather than
# removed and re-added, matching "prefer feature-gating over requiring" for
# an already-least-privilege-scoped statement.
#
# IAM POLICY BOUNDARY (documented, not silently assumed): the
# PipelineCoordinationDDB statement's Resource is scoped to the exact
# gg-eks-pipeline table ARN only (never table/*, never account-wide) and
# grants no dynamodb:DeleteTable/CreateTable/Scan/BatchWriteItem. It CANNOT,
# however, be further scoped by IAM alone to (a) reject writes to
# recordType=CONFIG while allowing recordType=LEASE/STATE# in the same
# table (DynamoDB IAM has no condition key over an item's own attribute
# VALUES for PutItem/UpdateItem), or (b) restrict to only the two canonical
# runtime partition keys via dynamodb:LeadingKeys (a real, viable
# tightening for a future pass, but NOT added here: it cannot be verified
# against live AWS in this sandbox, and an unverified DynamoDB IAM
# condition risks silently breaking the already-deployed collector with no
# way to test the change first). Both boundaries are therefore enforced as
# a CODE-LEVEL contract only today: gg_monitor_core.py never constructs a
# CONFIG item, and validate_secret_arn_coverage-style verification exists
# for secrets but not yet for DynamoDB partitions. This role is granted NO
# access to gg-alerts or gg-metrics-history (neither table ARN appears
# anywhere in this policy) -- the monitor does not need write access to
# either during this phase, and no gg-alerter IAM role is created here.
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