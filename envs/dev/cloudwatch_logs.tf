# Pre-created CloudWatch Logs destinations for the platform Fluent Bit DaemonSet; its IRSA role has no logs:CreateLogGroup, so these must exist ahead of time. No kms_key_id set (default AWS-owned encryption; no approved CMK yet).

variable "goldengate_log_retention_days" {
  description = "CloudWatch Logs retention (days) for the GoldenGate runtime and monitor container log groups."
  type        = number
  default     = 30
}

locals {
  goldengate_log_group_tags = {
    ApplicationName     = local.gg_env_tags.applicationName
    DataClassification  = local.gg_env_tags.dataClassification
    BusinessCriticality = local.gg_env_tags.businessCriticality
    BusinessUnit        = local.gg_env_tags.businessUnit
    CostCenter          = local.gg_env_tags.costCenter
    Environment         = local.gg_env_environment
    "map-migrated"      = local.gg_env_tags.mapMigrated
  }
}

# GoldenGate runtime container logs (namespace goldengate-dev, all deployment types).
resource "aws_cloudwatch_log_group" "goldengate_runtime" {
  name              = local.gg_env_runtime_log_group
  retention_in_days = var.goldengate_log_retention_days

  tags = merge(local.goldengate_log_group_tags, {
    Name = local.gg_env_runtime_log_group
  })
}

# Shared gg-monitor container logs (namespace goldengate-monitoring).
resource "aws_cloudwatch_log_group" "goldengate_monitor" {
  name              = local.gg_env_monitor_log_group
  retention_in_days = var.goldengate_log_retention_days

  tags = merge(local.goldengate_log_group_tags, {
    Name = local.gg_env_monitor_log_group
  })
}
