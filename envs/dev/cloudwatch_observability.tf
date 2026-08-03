# Phase 6B2A: pre-created CloudWatch Logs destination for the future
# amazon-cloudwatch-observability CloudWatch Agent / OTel Container Insights
# collectors (the Kubernetes namespace, ServiceAccount, operator, and Argo CD
# Application that will write to this log group are NOT created in this
# phase -- IAM/Terraform prerequisites only; see envs/dev/iam.tf's
# goldengate_cloudwatch_metrics_role_dev module and
# envs/dev/policies/goldengate-cloudwatch-metrics-dev/).
#
# This group is pre-created deliberately, the same posture already used for
# the Fluent Bit log groups in envs/dev/cloudwatch_logs.tf:
# GoldenGateCloudWatchMetricsRole-dev is never granted logs:CreateLogGroup
# or logs:PutRetentionPolicy, so a log group that does not already exist
# here is a hard failure for the agent, never a silently-created,
# never-expiring, unbounded-cost group.
#
# Only the Container Insights "performance" event log group is created here.
# Ordinary application/container stdout/stderr logs remain OUT of scope for
# the future CloudWatch Observability deployment (containerLogs.enabled will
# stay false, per the Phase 6B1 validation values) -- gg-fluent-bit
# (helm/goldengate-platform) continues to be the sole owner of GoldenGate and
# gg-monitor stdout/stderr log delivery, to
# /adcb/goldengate/dev/runtime and /adcb/goldengate/dev/monitor
# (envs/dev/cloudwatch_logs.tf), which this file does not touch. This file
# deliberately does NOT create the application/dataplane/host Container
# Insights log groups, any Fluent Bit log group, any Prometheus log group,
# or any X-Ray/Application Signals resource.
#
# Encryption: no kms_key_id is set below, so this group relies on CloudWatch
# Logs' own default server-side encryption -- the same current encryption
# posture as the existing GoldenGate log groups in cloudwatch_logs.tf. This
# repository has no approved CloudWatch Logs customer-managed KMS key ARN
# yet; do not invent one here.
#
# Tags: reuses local.goldengate_log_group_tags, already declared in
# envs/dev/cloudwatch_logs.tf (Terraform locals are module-wide) -- never
# redefined here.

variable "goldengate_container_insights_retention_days" {
  description = "CloudWatch Logs retention (days) for the pre-created Container Insights performance-event log group (/aws/containerinsights/gg-poc-dev/performance)."
  type        = number
  default     = 30
}

resource "aws_cloudwatch_log_group" "goldengate_container_insights_performance" {
  name              = "/aws/containerinsights/gg-poc-dev/performance"
  retention_in_days = var.goldengate_container_insights_retention_days

  tags = merge(local.goldengate_log_group_tags, {
    Name = "/aws/containerinsights/gg-poc-dev/performance"
  })
}
