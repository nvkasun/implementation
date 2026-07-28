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

# Phase 3: manager-aligned canonical CONFIG inventory records for the two
# live single-runtime GoldenGate deployments.
#
# STATE OWNERSHIP SAFETY: Terraform owns ONLY recordType=CONFIG items in
# this table. recordType=LEASE, recordType=STATE#_deployment, and
# recordType=STATE#<process> are owned exclusively by the (not-yet-deployed)
# shared gg-monitor -- Terraform must never create, update, or delete any
# item with those record types. This keeps writers disjoint by sort key, so
# there is no write contention between Terraform and the future monitor.
#
# Schema mirrors the manager reference implementation's own CONFIG item
# exactly (field names, types, and default values) -- see
# terraform/platform/dynamodb.tf and charts/gg-monitor/files/gg-monitor.py /
# charts/gg-deployment/files/utility-sidecar.py in the manager reference
# repository (inspected read-only; not copied or modified). CONFIG in that
# schema is a monitoring/alerting-policy record only -- it deliberately
# carries no endpoint, namespace, serviceName, or secret-reference fields
# (those live in topologies/dev/payments-ora-to-pg-001.yaml instead, mirroring
# the manager's own separation between DynamoDB CONFIG and its
# ConfigMap-mounted topology data).
#
# ignore_changes = [item]: matches the manager pattern -- Terraform seeds
# each CONFIG item once; DynamoDB console/future tooling is the live source
# of truth for operator-tuned values after that, so Terraform never fights
# an operator's later edit.
#
# No ttl attribute: CONFIG records must not expire (only the table's TTL
# *feature* stays enabled at the table level, for LEASE/STATE items the
# monitor will own -- individual CONFIG items simply omit the ttl attribute
# entirely, which is sufficient for DynamoDB TTL to never act on them).
resource "aws_dynamodb_table_item" "gg_oracle_payments_01_config" {
  table_name = "gg-eks-pipeline"
  hash_key   = "pipeline"
  range_key  = "recordType"

  item = jsonencode({
    pipeline       = { S = "gg-oracle-payments-01" }
    recordType     = { S = "CONFIG" }
    deploymentType = { S = "oracle" }

    alertsEnabled            = { BOOL = false }
    metricsEnabled           = { BOOL = true }
    credSyncEnabled          = { BOOL = false }
    tz                       = { S = "Asia/Dubai" }
    checkIntervalSeconds     = { N = "60" }
    startupGraceSeconds      = { N = "300" }
    autoStartEnabled         = { BOOL = true }
    autoRestartMaxRetries    = { N = "3" }
    autoRestartWindowMinutes = { N = "30" }
    trailRetentionHours      = { N = "48" }

    defaults = { M = {
      lagMode              = { S = "alert" }
      lagThresholdSeconds  = { N = "300" }
      maxConsecutiveAbends = { N = "3" }
      abendRecheckSeconds  = { N = "120" }
      alertEachAbend       = { BOOL = false }
      failoverEnabled      = { BOOL = true }
      dispatchStallChecks  = { N = "3" }
    } }

    quietHours = { M = {} }
    overrides  = { M = {} }
  })

  lifecycle {
    ignore_changes = [item]
  }
}

resource "aws_dynamodb_table_item" "gg_postgresql_payments_01_config" {
  table_name = "gg-eks-pipeline"
  hash_key   = "pipeline"
  range_key  = "recordType"

  item = jsonencode({
    pipeline       = { S = "gg-postgresql-payments-01" }
    recordType     = { S = "CONFIG" }
    deploymentType = { S = "postgresql" }

    alertsEnabled            = { BOOL = false }
    metricsEnabled           = { BOOL = true }
    credSyncEnabled          = { BOOL = false }
    tz                       = { S = "Asia/Dubai" }
    checkIntervalSeconds     = { N = "60" }
    startupGraceSeconds      = { N = "300" }
    autoStartEnabled         = { BOOL = true }
    autoRestartMaxRetries    = { N = "3" }
    autoRestartWindowMinutes = { N = "30" }
    trailRetentionHours      = { N = "48" }

    defaults = { M = {
      lagMode              = { S = "alert" }
      lagThresholdSeconds  = { N = "300" }
      maxConsecutiveAbends = { N = "3" }
      abendRecheckSeconds  = { N = "120" }
      alertEachAbend       = { BOOL = false }
      failoverEnabled      = { BOOL = true }
      dispatchStallChecks  = { N = "3" }
    } }

    quietHours = { M = {} }
    overrides  = { M = {} }
  })

  lifecycle {
    ignore_changes = [item]
  }
}
