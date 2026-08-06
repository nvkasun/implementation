# Fleet-overview CloudWatch dashboard; widgets are generated from the canonical registry, never hand-listed per deployment.

locals {
  # Sourced from the folder-driven inventory in goldengate_inventory.tf, never a handwritten registry file.
  gg_dashboard_enabled_deployments = {
    for id in local.goldengate_deployment_names : id => {
      name     = id
      type     = local.goldengate_enabled_deployments[id].runtime.deploymentType
      pipeline = local.goldengate_enabled_deployments[id].deployment.pipeline
      role     = local.goldengate_enabled_deployments[id].deployment.role
    }
  }

  gg_dashboard_deployment_names = local.goldengate_deployment_names

  gg_dashboard_pipeline_names = sort(distinct([
    for name in local.gg_dashboard_deployment_names : local.gg_dashboard_enabled_deployments[name].pipeline
  ]))

  gg_dashboard_source_count = length([
    for name in local.gg_dashboard_deployment_names : name
    if local.gg_dashboard_enabled_deployments[name].role == "source"
  ])
  gg_dashboard_target_count = length([
    for name in local.gg_dashboard_deployment_names : name
    if local.gg_dashboard_enabled_deployments[name].role == "target"
  ])

  gg_dashboard_critical_services = ["adminsrvr", "distsrvr", "recvsrvr"]
  gg_dashboard_namespace         = "GoldenGate/Pipelines"
  gg_dashboard_region            = "eu-west-1"
  gg_dashboard_eks_cluster       = "gg-poc-dev"
  gg_dashboard_monitor_host      = "monitor.${local.goldengate_shared_environment.dnsDomain}"

  gg_dashboard_deployment_metric_names = ["DeploymentDown", "LagBreached", "AbendFailure", "HeartbeatAgeSeconds"]

  # One metric-array entry per enabled deployment, keyed by metric name so each widget picks its own slice.
  gg_dashboard_deployment_metrics = {
    for metric_name in local.gg_dashboard_deployment_metric_names :
    metric_name => [
      for name in local.gg_dashboard_deployment_names : [
        local.gg_dashboard_namespace,
        metric_name,
        "Deployment", name,
        "DeploymentType", local.gg_dashboard_enabled_deployments[name].type,
        { label = "${name} (${local.gg_dashboard_enabled_deployments[name].type})" },
      ]
    ]
  }

  # setproduct (not nested-for + flatten) keeps each 8-element metric array intact -- flatten() would collapse it too.
  gg_dashboard_critical_service_metrics = [
    for pair in setproduct(local.gg_dashboard_deployment_names, local.gg_dashboard_critical_services) : [
      local.gg_dashboard_namespace,
      "CriticalServiceDown",
      "Deployment", pair[0],
      "DeploymentType", local.gg_dashboard_enabled_deployments[pair[0]].type,
      "Service", pair[1],
      { label = "${pair[0]} / ${pair[1]}" },
    ]
  ]

  gg_dashboard_deployment_inventory_lines = [
    for name in local.gg_dashboard_deployment_names :
    "- **${name}** (${local.gg_dashboard_enabled_deployments[name].type}, ${local.gg_dashboard_enabled_deployments[name].role}) -- pipeline `${local.gg_dashboard_enabled_deployments[name].pipeline}`"
  ]

  gg_dashboard_header_markdown = join("\n", [
    "# GoldenGate DEV Fleet Overview",
    "",
    "- **Environment:** ${local.goldengate_shared_environment.environment}",
    "- **AWS region:** ${local.gg_dashboard_region}",
    "- **EKS cluster:** ${local.gg_dashboard_eks_cluster}",
    "- **Runtime namespace:** ${local.goldengate_shared_environment.runtimeNamespace}",
    "- **Monitoring namespace:** ${local.goldengate_shared_environment.monitoringNamespace}",
    "- **Enabled deployments:** ${length(local.gg_dashboard_deployment_names)} (source: ${local.gg_dashboard_source_count}, target: ${local.gg_dashboard_target_count})",
    "- **Logical pipelines:** ${join(", ", local.gg_dashboard_pipeline_names)}",
    "- **Monitoring portal:** ${local.gg_dashboard_monitor_host}",
    "",
    "### Enabled deployments",
    "",
    join("\n", local.gg_dashboard_deployment_inventory_lines),
    "",
    "Deployment and critical-service health below reflect the shared gg-monitor collector only; replication is not claimed healthy here. Process-level Extract/Replicat visibility becomes available only after real GoldenGate processes are configured (see *Replication process visibility* below).",
  ])

  gg_dashboard_deployment_health_note_markdown = join("\n", [
    "**Deployment health values**",
    "",
    "- `0` = normal",
    "- `1` = condition detected",
    "",
    "Values shown are the Maximum over the selected period. Missing data is not automatically healthy.",
  ])

  gg_dashboard_critical_service_note_markdown = join("\n", [
    "**Critical service values**",
    "",
    "- `0` = reachable",
    "- `1` = unreachable",
    "",
    "Missing data is not automatically healthy -- it means the service was not probed this period.",
  ])

  gg_dashboard_container_resources_markdown = join("\n", [
    "## Container resource metrics",
    "",
    "OTel Container Insights metrics (for example `container_cpu_usage_seconds_total`) are live and available today in CloudWatch Query Studio / Container Insights.",
    "",
    "Terraform-managed PromQL charts for CPU, memory, and restart metrics will be added only after the exact console-generated query source and label contract are captured and validated. No CPU percentage, memory percentage, restart count, readiness, or node-health query is fabricated here.",
  ])

  gg_dashboard_process_visibility_markdown = join("\n", [
    "## Replication process visibility",
    "",
    "Real Extract and Replicat processes are not configured yet, so no canonical `STATE#<process>` rows exist. Process-level widgets intentionally show no fabricated health.",
    "",
    "`ExtractLagSeconds`, `ReplicatLagSeconds`, `AbendState`, and `AbendEvent` widgets will be added once real GoldenGate processes are validated and publishing.",
  ])

  gg_dashboard_runtime_log_query = join(" | ", [
    "SOURCE '${aws_cloudwatch_log_group.goldengate_runtime.name}'",
    "fields @timestamp, kubernetes.pod_name, kubernetes.container_name, environment, @message",
    "filter @message like /(?i)(error|failed|fatal|exception|abend|unreachable)/",
    "sort @timestamp desc",
    "limit 100",
  ])

  gg_dashboard_monitor_log_query = join(" | ", [
    "SOURCE '${aws_cloudwatch_log_group.goldengate_monitor.name}'",
    "fields @timestamp, kubernetes.pod_name, environment, @message",
    "filter @message like /(?i)(error|exception|failed|accessdenied|authorization|putmetricdata|cloudwatch|tick failed)/",
    "sort @timestamp desc",
    "limit 100",
  ])

  gg_dashboard_widgets = [
    {
      type   = "text"
      x      = 0
      y      = 0
      width  = 24
      height = 8
      properties = {
        markdown = local.gg_dashboard_header_markdown
      }
    },
    {
      type   = "metric"
      x      = 0
      y      = 8
      width  = 6
      height = 6
      properties = {
        title     = "Deployment availability"
        view      = "singleValue"
        region    = local.gg_dashboard_region
        stat      = "Maximum"
        period    = 60
        sparkline = true
        liveData  = true
        metrics   = local.gg_dashboard_deployment_metrics["DeploymentDown"]
      }
    },
    {
      type   = "metric"
      x      = 6
      y      = 8
      width  = 6
      height = 6
      properties = {
        title     = "Lag-rule breaches"
        view      = "singleValue"
        region    = local.gg_dashboard_region
        stat      = "Maximum"
        period    = 60
        sparkline = true
        liveData  = true
        metrics   = local.gg_dashboard_deployment_metrics["LagBreached"]
      }
    },
    {
      type   = "metric"
      x      = 12
      y      = 8
      width  = 6
      height = 6
      properties = {
        title     = "ABEND failure threshold"
        view      = "singleValue"
        region    = local.gg_dashboard_region
        stat      = "Maximum"
        period    = 60
        sparkline = true
        liveData  = true
        metrics   = local.gg_dashboard_deployment_metrics["AbendFailure"]
      }
    },
    {
      type   = "text"
      x      = 18
      y      = 8
      width  = 6
      height = 6
      properties = {
        markdown = local.gg_dashboard_deployment_health_note_markdown
      }
    },
    {
      type   = "metric"
      x      = 0
      y      = 14
      width  = 24
      height = 6
      properties = {
        title    = "Monitor heartbeat age"
        view     = "timeSeries"
        region   = local.gg_dashboard_region
        stat     = "Maximum"
        period   = 60
        liveData = true
        stacked  = false
        legend   = { position = "bottom" }
        yAxis    = { left = { min = 0 } }
        metrics  = local.gg_dashboard_deployment_metrics["HeartbeatAgeSeconds"]
      }
    },
    {
      type   = "metric"
      x      = 0
      y      = 20
      width  = 18
      height = 6
      properties = {
        title    = "Critical service reachability"
        view     = "table"
        region   = local.gg_dashboard_region
        stat     = "Maximum"
        period   = 60
        liveData = true
        metrics  = local.gg_dashboard_critical_service_metrics
      }
    },
    {
      type   = "text"
      x      = 18
      y      = 20
      width  = 6
      height = 6
      properties = {
        markdown = local.gg_dashboard_critical_service_note_markdown
      }
    },
    {
      type   = "log"
      x      = 0
      y      = 26
      width  = 24
      height = 6
      properties = {
        title  = "GoldenGate runtime warnings and errors"
        region = local.gg_dashboard_region
        view   = "table"
        query  = local.gg_dashboard_runtime_log_query
      }
    },
    {
      type   = "log"
      x      = 0
      y      = 32
      width  = 24
      height = 6
      properties = {
        title  = "gg-monitor warnings and publication failures"
        region = local.gg_dashboard_region
        view   = "table"
        query  = local.gg_dashboard_monitor_log_query
      }
    },
    {
      type   = "text"
      x      = 0
      y      = 38
      width  = 24
      height = 5
      properties = {
        markdown = local.gg_dashboard_container_resources_markdown
      }
    },
    {
      type   = "text"
      x      = 0
      y      = 43
      width  = 24
      height = 5
      properties = {
        markdown = local.gg_dashboard_process_visibility_markdown
      }
    },
  ]

  gg_dashboard_body = {
    start          = "-PT3H"
    periodOverride = "inherit"
    widgets        = local.gg_dashboard_widgets
  }
}

resource "aws_cloudwatch_dashboard" "goldengate_fleet" {
  dashboard_name = "gg-dev-fleet-overview"
  dashboard_body = jsonencode(local.gg_dashboard_body)
}
