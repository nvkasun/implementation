# Pre-created CloudWatch Logs destinations for the platform Fluent Bit DaemonSet; its IRSA role has no logs:CreateLogGroup, so these must exist ahead of time. No kms_key_id set (default AWS-owned encryption; no approved CMK yet).

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

# GoldenGate runtime container logs: gg-oracle-payments-01, gg-postgresql-payments-01 (namespace goldengate-dev).
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
