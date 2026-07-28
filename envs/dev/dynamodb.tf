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

# Phase 3 (refactored): manager-aligned canonical CONFIG inventory, seeded
# generically from pipelines/deployments.yaml -- mirrors the manager
# reference implementation's own pattern exactly:
#
#   terraform/platform/dynamodb.tf (manager repo, inspected read-only):
#     for_each = {
#       for d in yamldecode(file("${path.module}/../../pipelines/deployments.yaml")).deployments :
#       "gg-${d.name}" => d.type
#     }
#
# One central deployment inventory (pipelines/deployments.yaml), one shared
# table (gg-eks-pipeline, unchanged below), one generic
# aws_dynamodb_table_item resource with for_each -- never a per-runtime
# resource block. A future declared deployment (enabled or not) needs only
# a new pipelines/deployments.yaml entry, no Terraform code change.
#
# STATE OWNERSHIP SAFETY: Terraform owns ONLY recordType=CONFIG items in
# this table. recordType=LEASE, recordType=STATE#_deployment, and
# recordType=STATE#<process> are owned exclusively by the (not-yet-deployed)
# shared gg-monitor -- Terraform must never create, update, or delete any
# item with those record types. This keeps writers disjoint by sort key, so
# there is no write contention between Terraform and the future monitor.
# RESOLVED STATE KEY CONTRACT for that future phase (the manager reference
# code is internally inconsistent here: charts/gg-monitor/files/gg-monitor.py
# reads a legacy singleton recordType="STATE", while the actual writer,
# charts/gg-deployment/files/utility-sidecar.py, only ever writes
# recordType="STATE#_deployment" / "STATE#<process>" -- our canonical
# contract is the STATE#-prefixed form; the bare "STATE" singleton is not
# used and must not be created or read).
#
# Schema mirrors the manager reference implementation's own CONFIG item
# field names, types, and (mostly) default values -- see
# terraform/platform/dynamodb.tf and charts/gg-monitor/files/gg-monitor.py /
# charts/gg-deployment/files/utility-sidecar.py / gg_health.py in the manager
# reference repository (inspected read-only; not copied or modified). CONFIG
# in that schema is a monitoring/alerting-policy record only -- it
# deliberately carries no endpoint, namespace, serviceName, or
# secret-reference fields (those live in
# topologies/dev/payments-ora-to-pg-001.yaml instead, mirroring the
# manager's own separation between DynamoDB CONFIG and its ConfigMap-mounted
# topology data).
#
# CONFIRMED MANAGER DEFECT (field name): the manager's own Terraform seed
# writes `dispatchStallChecks` inside `defaults`, but every reader
# (gg_health.py DEFAULTS["defaults"]["distpathStallChecks"] and
# utility-sidecar.py's `rule["distpathStallChecks"]`) expects
# `distpathStallChecks`. We use the corrected `distpathStallChecks` name,
# never the seed's typo.
#
# PASSIVE-SAFE DEVIATION (intentional, approved architecture): our runtime
# pods have no manager utility sidecar, and the future shared gg-monitor
# must be passive (no restart/start/fence/failover). alertsEnabled=false,
# credSyncEnabled=false, autoStartEnabled=false, autoRestartMaxRetries=0,
# defaults.failoverEnabled=false, and defaults.alertEachAbend=false all
# deviate from the manager's own seed defaults (which assume its active
# sidecar) for exactly this reason. metricsEnabled and every timing/
# threshold field (tz, checkIntervalSeconds, startupGraceSeconds,
# trailRetentionHours, defaults.lagMode/lagThresholdSeconds/
# maxConsecutiveAbends/abendRecheckSeconds/distpathStallChecks) stay
# manager-compatible.
#
# ignore_changes = [item]: matches the manager pattern -- Terraform creates
# the initial CONFIG item once; operators or future management workflows may
# tune existing CONFIG values afterward (DynamoDB console / future tooling
# is the live source of truth from then on), so Terraform never resets them
# on a later apply. A new pipelines/deployments.yaml entry still creates a
# new CONFIG item normally (for_each diff), whether or not it is enabled.
#
# No ttl attribute: CONFIG records must not expire (only the table's TTL
# *feature* stays enabled at the table level, for LEASE/STATE items the
# monitor will own -- individual CONFIG items simply omit the ttl attribute
# entirely, which is sufficient for DynamoDB TTL to never act on them).
#
# TERRAFORM STATE MIGRATION SAFETY: the two explicit resources this replaces
# (aws_dynamodb_table_item.gg_oracle_payments_01_config and
# .gg_postgresql_payments_01_config) were present in this file's prior
# revision and may already exist in remote Terraform state. The moved
# blocks below let an already-applied item move to its new for_each address
# without a delete/recreate; if it was never applied, the moved block is a
# harmless no-op and the for_each resource is simply created normally.
#
# OPERATIONAL RULE -- read the plan before applying, every time this file
# changes:
#
#   Scenario A (plan shows CREATE for both pipeline_config[...] addresses):
#     the old explicit resources were never applied. The new records receive
#     the corrected passive CONFIG payload directly. Proceed after normal
#     review -- no special handling needed.
#
#   Scenario B (plan shows only MOVE for both addresses, no attribute diff):
#     the old explicit resources already exist in remote state.
#     lifecycle.ignore_changes = [item] means Terraform will NOT show or
#     apply any difference between the old remote payload and this file's
#     corrected passive payload -- a MOVE-only plan does not by itself prove
#     the live item already matches. Before applying:
#       1. Read both current DynamoDB CONFIG items (read-only
#          get-item/describe -- never write).
#       2. If they still contain the previous active payload
#          (autoStartEnabled=true, autoRestartMaxRetries=3,
#          autoRestartWindowMinutes=30, defaults.failoverEnabled=true) or the
#          misspelled dispatchStallChecks field, perform a controlled
#          one-time reconciliation: temporarily remove ignore_changes for
#          that migration apply, verify the resulting payload matches this
#          file exactly, then restore ignore_changes in a subsequent,
#          separately reviewed commit.
#       3. Do not remove ignore_changes from this steady-state file itself
#          unless that one-time migration has been confirmed necessary --
#          this local correction pass does not perform or assume that
#          migration; it only documents the rule for whoever applies next.
moved {
  from = aws_dynamodb_table_item.gg_oracle_payments_01_config
  to   = aws_dynamodb_table_item.pipeline_config["gg-oracle-payments-01"]
}

moved {
  from = aws_dynamodb_table_item.gg_postgresql_payments_01_config
  to   = aws_dynamodb_table_item.pipeline_config["gg-postgresql-payments-01"]
}

resource "aws_dynamodb_table_item" "pipeline_config" {
  # Table creation ordering: module.goldengate_pipeline_state (above) sources
  # from a private AbuDhabiCommercialBank GitHub repo this environment cannot
  # reach (no network credentials to that host), so its exact output names
  # (e.g. a hypothetical .name/.hash_key/.range_key) cannot be verified --
  # no local documentation, cached module metadata, or existing
  # module.X.Y-output usage exists anywhere in this repository either. Per
  # "do not guess module output names," table_name/hash_key/range_key below
  # stay the existing literal strings (matching this repo's established
  # convention of hardcoding names/ARNs rather than referencing unverified
  # module outputs), and this explicit depends_on takes over the ordering
  # guarantee a verified module-output reference would otherwise have
  # provided implicitly -- so a fresh environment cannot attempt CONFIG item
  # creation before the shared table exists.
  depends_on = [
    module.goldengate_pipeline_state
  ]

  # Exact manager for_each value pattern (terraform/platform/dynamodb.tf in
  # the manager reference repo): the map value is d.type itself (a bare
  # string), not the whole d object -- each.value below is therefore already
  # the deploymentType string.
  for_each = {
    for d in yamldecode(
      file("${path.module}/../../pipelines/deployments.yaml")
    ).deployments :
    "gg-${d.name}" => d.type
  }

  table_name = "gg-eks-pipeline"
  hash_key   = "pipeline"
  range_key  = "recordType"

  item = jsonencode({
    pipeline       = { S = each.key }
    recordType     = { S = "CONFIG" }
    deploymentType = { S = each.value }

    alertsEnabled            = { BOOL = false }
    metricsEnabled           = { BOOL = true }
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
