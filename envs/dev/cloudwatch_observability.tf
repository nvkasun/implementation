# Pre-created CloudWatch Logs destination (Container Insights "performance" event group only) for the future CloudWatch Observability agent; IAM prerequisites only, see iam.tf's goldengate_cloudwatch_metrics_role_dev. IRSA has no logs:CreateLogGroup, so this must exist ahead of time; no kms_key_id (default encryption); reuses local.goldengate_log_group_tags from cloudwatch_logs.tf. Log-group name derives from envs/dev/environment.yaml's eks.clusterName (see environment.tf) -- never hardcoded, since Container Insights paths are keyed by cluster name and a recreated cluster gets a new one.

variable "goldengate_container_insights_retention_days" {
  description = "CloudWatch Logs retention (days) for the pre-created Container Insights performance-event log group."
  type        = number
  default     = 30
}

resource "aws_cloudwatch_log_group" "goldengate_container_insights_performance" {
  name              = local.gg_env_container_insights_log_group
  retention_in_days = var.goldengate_container_insights_retention_days

  tags = merge(local.goldengate_log_group_tags, {
    Name = local.gg_env_container_insights_log_group
  })
}
