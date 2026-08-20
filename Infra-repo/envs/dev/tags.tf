locals {
  environment = "dev"
  app_prefix  = "poc"

  common_tags = {
    env                 = "dev"
    ApplicationName     = "CloudFactory"
    BusinessCriticality = "Low"
    BusinessUnit        = "TechnologyPlatform"
    CostCenter          = "219"
    DataClassification  = "General"
    Environment         = local.environment
    RequestReference    = "PO32080"
    ManagedBy           = "Terraform"
    map-migrated        = "comm5TZY31HX9S"
    BusinessUnitOwner   = "Ganesh.Harikrishnan"
  }

  # vpc_id             = nonsensitive(data.aws_ssm_parameter.vpc_id.value)
  # default_subnet_ids = split(",", nonsensitive(data.aws_ssm_parameter.app_subnet_ids.value))
}
