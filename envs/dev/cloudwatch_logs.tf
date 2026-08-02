# Phase 6A: pre-created CloudWatch Logs destinations for the platform-level
# Fluent Bit DaemonSet (helm/goldengate-platform, fluentBit.create=true).
# Pre-creating these here -- rather than letting Fluent Bit auto-create
# groups -- is deliberate: the DaemonSet's IRSA role
# (envs/dev/policies/goldengate-platform-logging-dev) is never granted
# logs:CreateLogGroup, so a log group that does not already exist here is a
# hard failure, not a silently-created, never-expiring, unbounded-cost
# group.
#
# Encryption: no kms_key_id is set on either log group below, so both rely
# on CloudWatch Logs' own default server-side encryption (AWS-owned key,
# not a customer-managed key this repository administers). This repository
# has no approved CloudWatch Logs customer-managed KMS key ARN yet -- do
# not set kms_key_id to a guessed alias (e.g. "alias/aws/logs" is not a
# valid kms_key_id value for this resource; it is not the account's actual
# default CloudWatch Logs encryption behavior) and do not create a new KMS
# CMK just to satisfy this field. This matches the same "no bespoke CMK"
# posture already used elsewhere in this environment (envs/dev/dynamodb.tf
# custom_kms_key_arn = null; envs/dev/secret.tf secrets have no custom key
# either), extended here one step further: no key reference of any kind
# until an approved CMK ARN is actually supplied.
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

  tags = merge(local.goldengate_log_group_tags, {
    Name = "/adcb/goldengate/dev/runtime"
  })
}

# Shared gg-monitor container logs (namespace goldengate-monitoring).
resource "aws_cloudwatch_log_group" "goldengate_monitor" {
  name              = "/adcb/goldengate/dev/monitor"
  retention_in_days = var.goldengate_log_retention_days

  tags = merge(local.goldengate_log_group_tags, {
    Name = "/adcb/goldengate/dev/monitor"
  })
}
