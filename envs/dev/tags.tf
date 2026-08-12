# Centralized DEV corporate/business tag values -- every approved module below that accepts these fields references local.tags.* instead of repeating the literal value; every value here is unchanged from what this repo already used, except the newly-established request_reference. Resource-specific identifiers (e.g. gg-repltest-postgresql, gg-repltest-mssql) are never centralized here -- only common metadata.
locals {
  tags = {
    env                  = "dev"
    application_name     = "CloudFactory"
    business_criticality = "Low"
    business_unit        = "TechnologyPlatform"
    business_unit_owner  = "ganesh.harikrishnan"
    cost_center          = "219"
    map_migrated         = "comm5TZY31HX9S"
    request_reference    = "P032080"
    data_classification  = "General"
  }
}
