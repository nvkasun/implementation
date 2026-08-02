# Phase 6A: pre-created CloudWatch Logs destinations for the platform-level
# Fluent Bit DaemonSet (helm/goldengate-platform, fluentBit.create=true).
# Pre-creating these here -- rather than letting Fluent Bit auto-create
# groups -- is deliberate: the DaemonSet's IRSA role
# (envs/dev/policies/goldengate-platform-logging-dev) is never granted
# logs:CreateLogGroup, so a log group that does not already exist here is a
# hard failure, not a silently-created, never-expiring, unbounded-cost
# group.
#
# No new KMS CMK: encrypted with the account's existing AWS-managed
# alias/aws/logs key, the same "no bespoke CMK" pattern already used
# elsewhere in this environment (envs/dev/dynamodb.tf custom_kms_key_arn =
# null; envs/dev/secret.tf secrets have no custom key either -- both rely
# on the relevant service's AWS-managed default key).
#
# Naming matches the goldengate.adcb/ label prefix already used throughout
# this repository (e.g. helm/goldengate-platform, helm/goldengate,
# helm/goldengate-monitor).

variable "goldengate_log_retention_days" {
  description = "CloudWatch Logs retention (days) for the GoldenGate runtime and monitor container log groups."
  type        = number
  default     = 30
}

locals {
  goldengate_log_group_tags = {
    ApplicationName     = "CloudFactory"
    DataClassification  = "General"
    BusinessCriticality = "Low"
    BusinessUnit        = "TechnologyPlatform"
    CostCenter          = "219"
    Environment         = "dev"
    "map-migrated"      = "comm5TZY31HX9S"
  }
}

# GoldenGate runtime container logs: gg-oracle-payments-01, gg-postgresql-payments-01
# (namespace goldengate-dev).
resource "aws_cloudwatch_log_group" "goldengate_runtime" {
  name              = "/adcb/goldengate/dev/runtime"
  retention_in_days = var.goldengate_log_retention_days
  kms_key_id        = "alias/aws/logs"

  tags = merge(local.goldengate_log_group_tags, {
    Name = "/adcb/goldengate/dev/runtime"
  })
}

# Shared gg-monitor container logs (namespace goldengate-monitoring).
resource "aws_cloudwatch_log_group" "goldengate_monitor" {
  name              = "/adcb/goldengate/dev/monitor"
  retention_in_days = var.goldengate_log_retention_days
  kms_key_id        = "alias/aws/logs"

  tags = merge(local.goldengate_log_group_tags, {
    Name = "/adcb/goldengate/dev/monitor"
  })
}
