# NOTE: The module block labels and `name` attributes below were renamed to
# match the new AWS Secrets Manager naming pattern (dev/goldengate/<source|target>/admin,
# dev/goldengate/tls-certificate). Renaming a module block label changes its
# Terraform resource address, so `terraform apply` will treat this as
# destroy-the-old / create-the-new unless the state is migrated first with
# `terraform state mv`. If the new secrets already exist manually in AWS
# Secrets Manager (as they do for this rollout), do NOT run `terraform apply`
# on this file without first reconciling state (either `terraform state mv`
# from the old addresses, or `terraform import` against the existing secrets)
# -- otherwise Terraform will attempt to create secrets that already exist,
# or delete/recreate the old ones out from under running GoldenGate pods.

module "tls_certificate_secret_protected" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-secrets-manager.git?ref=v2.0.2"

  name                    = "dev/goldengate/tls-certificate"
  description             = "GoldenGate shared wildcard TLS certificate secret for dev"
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


module "source_admin_secret_protected" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-secrets-manager.git?ref=v2.0.2"

  name                    = "dev/goldengate/source/admin"
  description             = "GoldenGate source admin login secret for dev"
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

module "target_admin_secret_protected" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-secrets-manager.git?ref=v2.0.2"

  name                    = "dev/goldengate/target/admin"
  description             = "GoldenGate target admin login secret for dev"
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
