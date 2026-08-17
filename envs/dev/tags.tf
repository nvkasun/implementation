# local.tags is the stable interface every approved module below already references -- its values are mapped from the canonical envs/dev/environment.yaml tags (see environment.tf) rather than restated here, so this file has exactly one thing to change if corporate tag values ever change. Resource-specific identifiers are never centralized here -- only common metadata.
locals {
  tags = {
    env                  = local.gg_env_environment
    application_name     = local.gg_env_tags.applicationName
    business_criticality = local.gg_env_tags.businessCriticality
    business_unit        = local.gg_env_tags.businessUnit
    business_unit_owner  = local.gg_env_tags.businessUnitOwner
    cost_center          = local.gg_env_tags.costCenter
    map_migrated         = local.gg_env_tags.mapMigrated
    request_reference    = local.gg_env_tags.requestReference
    data_classification  = local.gg_env_tags.dataClassification
  }
}
