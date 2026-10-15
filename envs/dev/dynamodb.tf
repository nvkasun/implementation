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

# CONFIG is Terraform-owned (monitor owns LEASE/STATE#*); seeded from the folder-driven inventory in goldengate_inventory.tf; ignore_changes=[item] so later manual tuning survives apply.
moved {
  from = aws_dynamodb_table_item.gg_oracle_payments_01_config
  to   = aws_dynamodb_table_item.pipeline_config["gg-oracle-payments-01"]
}

moved {
  from = aws_dynamodb_table_item.gg_postgresql_payments_01_config
  to   = aws_dynamodb_table_item.pipeline_config["gg-postgresql-payments-01"]
}

# DynamoDB CONFIG Identity Migration Correction: the Pipeline-Aware Descriptor Hierarchy's four deployment-ID renames change not just this resource's for_each key but the PHYSICAL DynamoDB primary key embedded inside the single JSON-encoded `item` argument itself (pipeline=<deployment-id>). Proven via an isolated offline reproduction (real aws_dynamodb_table_item resource type, fabricated prior state, terraform plan -refresh=false, zero AWS calls): a `moved` block alone correctly avoids destroy+recreate (Plan: 0 to add, 0 to change, 0 to destroy), but ignore_changes=[item] then freezes the ENTIRE item body -- including the embedded pipeline key attribute -- forever at its OLD value; the physical item never actually relocates to the new key, so the new canonical deployment ID's own CONFIG lookup would find nothing. A `moved` block is therefore actively WRONG here (unlike envs/dev/efs.tf's managed EFS module, whose underlying AWS resource identity/content is genuinely unchanged by the rename) -- it would just freeze the stale content in place. Since a DynamoDB item's primary key cannot be changed in place, relocating it is unavoidably a copy-then-delete: the four bounded NEW ids below are intentionally left to naturally destroy their OLD for_each address (no longer present in local.goldengate_deployment_names) and create their NEW one, but that new item's initial content is seeded from a live, read-only lookup of the OLD item's REAL current content (below), never Terraform's hardcoded defaults -- so the eventual real terraform apply that performs this migration carries forward any existing operator-tuned CONFIG instead of silently resetting it. Genuinely new deployment IDs are never a member of this bounded map and are unaffected.
locals {
  goldengate_config_migration_source_ids = {
    "gg-postgresql-repltest-001" = "gg-postgresql-repltest-01"
    "gg-mssql-repltest-001"      = "gg-mssql-repltest-01"
    "gg-oracle-repltest-002"     = "gg-oracle-repltest-01"
    "gg-postgresql-repltest-002" = "gg-postgresql-repltest-02"
  }
}

# DynamoDB CONFIG Migration Disposition: per-mapped-id, explicit, auditable classification -- replaces the former coarse goldengate_config_migration_pending boolean. That boolean's own premise (all four bounded ids still have a legacy CONFIG item worth copying) was disproven live: a real environment-wide MAIN Deploy's Phase 2 Terraform PLAN read data.aws_dynamodb_table_item.legacy_pipeline_config for all four OLD keys against real AWS and got an authoritative empty result for every single one -- there is no legacy CONFIG payload to copy or preserve for any of the four current bounded migrations. COPY_REQUIRED names an id whose OLD item has been independently verified (via a real Terraform plan/apply against AWS, never assumed) to still exist and must be read-and-preserved; LEGACY_CONFIG_ABSENT names an id independently verified to have no OLD item, so its NEW CONFIG item is seeded from the exact same canonical default body every non-migrated/future deployment already uses below -- never a second, duplicated default. Membership here is the single source of truth -- never inferred from a boolean, never re-derived from a live read's own success/failure at plan time (that is precisely the assumption that just failed live). A future, separately authorized migration may move an id from LEGACY_CONFIG_ABSENT to COPY_REQUIRED (never the reverse, and never without first proving the OLD item has reappeared) -- this source-only correction performs no live data migration itself.
locals {
  goldengate_config_migration_copy_required_ids = toset([])

  goldengate_config_migration_legacy_absent_ids = toset([
    "gg-postgresql-repltest-001",
    "gg-mssql-repltest-001",
    "gg-oracle-repltest-002",
    "gg-postgresql-repltest-002",
  ])
}

# Read-only lookup of each bounded pair's OLD physical CONFIG item -- never a mutation, never a second write path. Exists ONLY for the bounded NEW ids classified COPY_REQUIRED above; a LEGACY_CONFIG_ABSENT id (proven to have no OLD item) and a genuinely new deployment ID (never a member of goldengate_config_migration_source_ids at all) never attempt this read. The postconditions below fail closed (before this value is ever used to construct the NEW item) unless the item actually read back carries EXACTLY the expected OLD identity -- self is available here because postcondition (unlike precondition) runs after a data resource's own read, per Terraform's own precondition/postcondition contract, verified against the real provider via `terraform validate`.
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

# Fail-closed guard: the migration map must never alias a NEW id to itself, never chain (an OLD id must never also be a NEW id / map key elsewhere), and must contain exactly these four known pairs -- never a general/future onboarding mechanism.
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

  # Bounded content source: an id classified COPY_REQUIRED carries forward its OLD item's real (possibly manually tuned) content; every other id -- LEGACY_CONFIG_ABSENT (proven to have no OLD item), or a genuinely new/future deployment never in goldengate_config_migration_source_ids at all -- uses the exact same plain Terraform-default CONFIG body below, never a second/duplicated copy of it. Unaffected either way once written, since ignore_changes=[item] below freezes it against future drift regardless of which branch produced it. The COPY_REQUIRED branch is NEVER a verbatim copy of the legacy item -- the OLD item's own embedded pipeline attribute value is the OLD deployment ID (that is the whole physical key DynamoDB itself would keep if copied as-is, since aws_dynamodb_table_item resolves hash_key/range_key VALUES from inside `item`, never from the for_each key/resource address alone). jsondecode()+merge()+jsonencode() copies the COMPLETE old body -- every attribute Terraform does not explicitly know about included -- and overrides ONLY the two top-level key attributes (pipeline -> this NEW canonical id; recordType forced back to "CONFIG" defensively) so the resulting item physically keys under the NEW pipeline value while every other attribute (manual tuning, unknown/future fields) survives untouched.
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
