module "goldengate_pipeline_state" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-dynamodb.git?ref=v1.2.0"

  name      = "gg-eks-pipeline"
  hash_key  = "pipeline"
  range_key = "recordType"

  attributes = [
    {
      name = "pipeline"
      type = "S"
    },
    {
      name = "recordType"
      type = "S"
    }
  ]

  billing_mode = "PAY_PER_REQUEST"
  safety_mode  = "on_demand"

  ttl_enabled        = true
  ttl_attribute_name = "ttl"

  global_secondary_indexes = []
  local_secondary_indexes  = []

  autoscaling_enabled = false

  custom_kms_key_arn = null

  map_migrated         = "comm5TZY31HX9S"
  business_criticality = "Low"
  application_name     = "CloudFactory"
  cost_center          = "219"
  business_unit        = "TechnologyPlatform"
  business_unit_owner  = "ganesh.harikrishnan"
  data_classification  = "General"
  env                  = "dev"
}

# CONFIG is Terraform-owned (monitor owns LEASE/STATE#*); seeded from goldengate-deployments.yaml; ignore_changes=[item] so later manual tuning survives apply.
moved {
  from = aws_dynamodb_table_item.gg_oracle_payments_01_config
  to   = aws_dynamodb_table_item.pipeline_config["gg-oracle-payments-01"]
}

moved {
  from = aws_dynamodb_table_item.gg_postgresql_payments_01_config
  to   = aws_dynamodb_table_item.pipeline_config["gg-postgresql-payments-01"]
}

resource "aws_dynamodb_table_item" "pipeline_config" {
  depends_on = [
    module.goldengate_pipeline_state
  ]

  for_each = {
    for id in local.goldengate_deployment_names :
    id => local.goldengate_enabled_deployments[id].runtime.deploymentType
  }

  table_name = "gg-eks-pipeline"
  hash_key   = "pipeline"
  range_key  = "recordType"

  item = jsonencode({
    pipeline       = { S = each.key }
    recordType     = { S = "CONFIG" }
    deploymentType = { S = each.value }

    alertsEnabled            = { BOOL = false }
    metricsEnabled           = { BOOL = false }
    credSyncEnabled          = { BOOL = false }
    tz                       = { S = "Asia/Dubai" }
    checkIntervalSeconds     = { N = "60" }
    startupGraceSeconds      = { N = "300" }
    autoStartEnabled         = { BOOL = false }
    autoRestartMaxRetries    = { N = "0" }
    autoRestartWindowMinutes = { N = "0" }
    trailRetentionHours      = { N = "48" }

    defaults = { M = {
      lagMode              = { S = "alert" }
      lagThresholdSeconds  = { N = "300" }
      maxConsecutiveAbends = { N = "3" }
      abendRecheckSeconds  = { N = "120" }
      alertEachAbend       = { BOOL = false }
      failoverEnabled      = { BOOL = false }
      distpathStallChecks  = { N = "3" }
    } }

    quietHours = { M = {} }
    overrides  = { M = {} }
  })

  lifecycle {
    ignore_changes = [item]
  }
}

module "goldengate_alerts" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-dynamodb.git?ref=v1.2.0"

  name     = "gg-alerts"
  hash_key = "alert_id"

  attributes = [
    {
      name = "alert_id"
      type = "S"
    }
  ]

  billing_mode = "PAY_PER_REQUEST"
  safety_mode  = "on_demand"

  ttl_enabled        = false
  ttl_attribute_name = null

  global_secondary_indexes = []
  local_secondary_indexes  = []

  autoscaling_enabled = false

  custom_kms_key_arn = null

  map_migrated         = "comm5TZY31HX9S"
  business_criticality = "Low"
  application_name     = "CloudFactory"
  cost_center          = "219"
  business_unit        = "TechnologyPlatform"
  business_unit_owner  = "ganesh.harikrishnan"
  data_classification  = "General"
  env                  = "dev"
}

# GLOBAL is the routing-policy singleton for gg-alerter (not yet implemented); disabled/empty until configured via the DynamoDB console.
resource "aws_dynamodb_table_item" "alerts_global" {
  depends_on = [
    module.goldengate_alerts
  ]

  table_name = "gg-alerts"
  hash_key   = "alert_id"

  item = jsonencode({
    alert_id            = { S = "GLOBAL" }
    enabled             = { BOOL = false }
    distribution_list   = { L = [] }
    maintenance_windows = { L = [] }
  })

  lifecycle {
    ignore_changes = [item]
  }
}

module "goldengate_metrics_history" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-dynamodb.git?ref=v1.2.0"

  name      = "gg-metrics-history"
  hash_key  = "deployment_name"
  range_key = "timestamp"

  attributes = [
    {
      name = "deployment_name"
      type = "S"
    },
    {
      name = "timestamp"
      type = "N"
    }
  ]

  billing_mode = "PAY_PER_REQUEST"
  safety_mode  = "on_demand"

  ttl_enabled        = true
  ttl_attribute_name = "ttl"

  global_secondary_indexes = []
  local_secondary_indexes  = []

  autoscaling_enabled = false

  custom_kms_key_arn = null

  map_migrated         = "comm5TZY31HX9S"
  business_criticality = "Low"
  application_name     = "CloudFactory"
  cost_center          = "219"
  business_unit        = "TechnologyPlatform"
  business_unit_owner  = "ganesh.harikrishnan"
  data_classification  = "General"
  env                  = "dev"
}

# No seed items -- populated only by a future gg-alerter/metrics-history writer (not implemented yet).
