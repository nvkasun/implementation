module "secret_protected" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-secrets-manager.git?ref=v2.0.2"

  name                    = "dev/goldengate/certificate"
  description             = "Goldengate security certificate key"
  recovery_window_in_days = 0
  safety_mode             = "protected"

  # Mandatory tags
  map_migrated         = "comm5TZY31HX9S"
  business_criticality = "Low"
  application_name     = "CloudFactory"
  cost_center          = "219"
  business_unit        = "TechnologyPlatform"
  data_classification  = "General"
  env                  = "dev"
}