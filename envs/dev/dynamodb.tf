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

  map_migrated         = local.tags.map_migrated
  business_criticality = local.tags.business_criticality
  application_name     = local.tags.application_name
  cost_center          = local.tags.cost_center
  business_unit        = local.tags.business_unit
  business_unit_owner  = local.tags.business_unit_owner
  data_classification  = local.tags.data_classification
  env                  = local.tags.env
}

# CONFIG is Terraform-owned (monitor owns LEASE/STATE#). ignore_changes=[item] preserves manual tuning.
moved {
  from = aws_dynamodb_table_item.gg_oracle_payments_01_config
  to   = aws_dynamodb_table_item.pipeline_config["gg-oracle-payments-01"]
}

moved {
  from = aws_dynamodb_table_item.gg_postgresql_payments_01_config
  to   = aws_dynamodb_table_item.pipeline_config["gg-postgresql-payments-01"]
}

# Renaming a deployment id moves this item's physical DynamoDB key, not just its for_each key. A moved block would wrongly freeze the OLD key under ignore_changes=[item] -- these four bounded ids instead destroy/recreate, seeded from the OLD item's real content (never Terraform defaults), so operator tuning survives.
locals {
  goldengate_config_migration_source_ids = {
    "gg-postgresql-repltest-001" = "gg-postgresql-repltest-01"
    "gg-mssql-repltest-001"      = "gg-mssql-repltest-01"
    "gg-oracle-repltest-002"     = "gg-oracle-repltest-01"
    "gg-postgresql-repltest-002" = "gg-postgresql-repltest-02"
  }
}

# Per-id migration disposition, replacing the old coarse pending boolean. COPY_REQUIRED ids have a verified OLD item to preserve; LEGACY_CONFIG_ABSENT ids (live-proven to have none) use the default CONFIG body. legacy_absent_ids is derived below, never a second hand-maintained id list.
locals {
  goldengate_config_migration_copy_required_ids = toset([])

  goldengate_config_migration_legacy_absent_ids = setsubtract(
    toset(keys(local.goldengate_config_migration_source_ids)),
    local.goldengate_config_migration_copy_required_ids
  )
}

# Read-only lookup of each COPY_REQUIRED id's OLD CONFIG item. LEGACY_CONFIG_ABSENT and new ids never attempt this read. Postconditions fail closed unless the read-back item's identity matches exactly.
data "aws_dynamodb_table_item" "legacy_pipeline_config" {
  for_each = {
    for new_id, old_id in local.goldengate_config_migration_source_ids : new_id => old_id
    if contains(local.goldengate_config_migration_copy_required_ids, new_id)
  }

  table_name = "gg-eks-pipeline"

  key = jsonencode({
    pipeline   = { S = each.value }
    recordType = { S = "CONFIG" }
  })

  depends_on = [module.goldengate_pipeline_state]

  lifecycle {
    postcondition {
      condition     = try(jsondecode(self.item).pipeline.S, null) == each.value
      error_message = "envs/dev/dynamodb.tf: the legacy CONFIG item read for migration target ${each.key} does not have pipeline.S == ${each.value} (the exact mapped OLD id) -- refusing to migrate a missing, malformed, or foreign-identity item."
    }
    postcondition {
      condition     = try(jsondecode(self.item).recordType.S, null) == "CONFIG"
      error_message = "envs/dev/dynamodb.tf: the legacy CONFIG item read for migration target ${each.key} does not have recordType.S == \"CONFIG\" -- refusing to migrate a missing or malformed record."
    }
  }
}

# Fail-closed guard: migration map must never self-alias or chain ids, and must contain exactly these four pairs.
resource "terraform_data" "goldengate_config_migration_contract" {
  input = local.goldengate_config_migration_source_ids

  lifecycle {
    precondition {
      condition     = alltrue([for new_id, old_id in local.goldengate_config_migration_source_ids : new_id != old_id])
      error_message = "envs/dev/dynamodb.tf: goldengate_config_migration_source_ids maps a NEW deployment ID to itself -- every entry must name a genuinely different OLD id."
    }
    precondition {
      condition     = length(setintersection(toset(keys(local.goldengate_config_migration_source_ids)), toset(values(local.goldengate_config_migration_source_ids)))) == 0
      error_message = "envs/dev/dynamodb.tf: goldengate_config_migration_source_ids chains an OLD id into also being a NEW id (or vice versa) -- this map must be a single, non-chaining one-time bridge, never a multi-hop alias chain."
    }
    precondition {
      condition     = length(local.goldengate_config_migration_source_ids) == 4
      error_message = "envs/dev/dynamodb.tf: goldengate_config_migration_source_ids must contain exactly the four known bounded pairs from the pipeline-aware hierarchy migration -- never a general onboarding mechanism."
    }
    precondition {
      condition     = length(setintersection(local.goldengate_config_migration_copy_required_ids, local.goldengate_config_migration_legacy_absent_ids)) == 0
      error_message = "envs/dev/dynamodb.tf: goldengate_config_migration_copy_required_ids and goldengate_config_migration_legacy_absent_ids overlap -- every bounded id must carry exactly one migration disposition, never both."
    }
    precondition {
      condition     = setunion(local.goldengate_config_migration_copy_required_ids, local.goldengate_config_migration_legacy_absent_ids) == toset(keys(local.goldengate_config_migration_source_ids))
      error_message = "envs/dev/dynamodb.tf: the union of goldengate_config_migration_copy_required_ids and goldengate_config_migration_legacy_absent_ids must equal exactly the four bounded ids in goldengate_config_migration_source_ids -- every bounded id must carry a disposition, and neither set may name an id outside the bounded map."
    }
    precondition {
      # Every NEW migration id must exist in the full folder-driven inventory (not just enabled deployments), so a disabled descriptor never false-trips this.
      condition     = length(setsubtract(toset(keys(local.goldengate_config_migration_source_ids)), toset(keys(local.goldengate_runtime_documents)))) == 0
      error_message = "envs/dev/dynamodb.tf: goldengate_config_migration_source_ids names a NEW deployment ID with no matching canonical runtime descriptor under pipelines/*/*/values.yaml (checked against the full folder-driven inventory, including temporarily disabled descriptors) -- refusing to carry forward a historical migration bridge for a runtime that no longer exists in Git."
    }
  }
}

resource "aws_dynamodb_table_item" "pipeline_config" {
  depends_on = [
    module.goldengate_pipeline_state,
    terraform_data.goldengate_config_migration_contract
  ]

  for_each = {
    for id in local.goldengate_deployment_names :
    id => try(local.goldengate_enabled_deployments[id].runtime.deploymentType, "")
  }

  table_name = "gg-eks-pipeline"
  hash_key   = "pipeline"
  range_key  = "recordType"

  # COPY_REQUIRED ids carry forward the OLD item's content (decode/merge/re-encode, overriding only pipeline+recordType); every other id uses the default body below.
  item = (
    contains(local.goldengate_config_migration_copy_required_ids, each.key)
    ? jsonencode(merge(
      jsondecode(data.aws_dynamodb_table_item.legacy_pipeline_config[each.key].item),
      {
        pipeline   = { S = each.key }
        recordType = { S = "CONFIG" }
      }
    ))
    : jsonencode({
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
  )

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

  map_migrated         = local.tags.map_migrated
  business_criticality = local.tags.business_criticality
  application_name     = local.tags.application_name
  cost_center          = local.tags.cost_center
  business_unit        = local.tags.business_unit
  business_unit_owner  = local.tags.business_unit_owner
  data_classification  = local.tags.data_classification
  env                  = local.tags.env
}

# GLOBAL is the gg-alerter routing-policy singleton (not yet implemented) -- disabled/empty until configured via console.
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

  map_migrated         = local.tags.map_migrated
  business_criticality = local.tags.business_criticality
  application_name     = local.tags.application_name
  cost_center          = local.tags.cost_center
  business_unit        = local.tags.business_unit
  business_unit_owner  = local.tags.business_unit_owner
  data_classification  = local.tags.data_classification
  env                  = local.tags.env
}

# No seed items -- populated only by a future gg-alerter/metrics-history writer (not implemented yet).
