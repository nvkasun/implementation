# Module labels were renamed to match the new naming pattern; do not `terraform apply` without first reconciling state (`terraform state mv`/`import`) or it will destroy/recreate secrets already live in AWS.

module "tls_certificate_secret_protected" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-secrets-manager.git?ref=v2.0.2"

  name                    = "dev/goldengate/tls-certificate"
  description             = "GoldenGate shared wildcard TLS certificate secret for dev"
  recovery_window_in_days = 0
  safety_mode             = "protected"

  # Mandatory tags
  map_migrated         = local.tags.map_migrated
  business_criticality = local.tags.business_criticality
  application_name     = local.tags.application_name
  cost_center          = local.tags.cost_center
  business_unit        = local.tags.business_unit
  data_classification  = local.tags.data_classification
  env                  = local.tags.env
}


module "source_admin_secret_protected" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-secrets-manager.git?ref=v2.0.2"

  name                    = "dev/goldengate/source/admin"
  description             = "GoldenGate source admin login secret for dev"
  recovery_window_in_days = 0
  safety_mode             = "protected"

  # Mandatory tags
  map_migrated         = local.tags.map_migrated
  business_criticality = local.tags.business_criticality
  application_name     = local.tags.application_name
  cost_center          = local.tags.cost_center
  business_unit        = local.tags.business_unit
  data_classification  = local.tags.data_classification
  env                  = local.tags.env
}

module "target_admin_secret_protected" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-secrets-manager.git?ref=v2.0.2"

  name                    = "dev/goldengate/target/admin"
  description             = "GoldenGate target admin login secret for dev"
  recovery_window_in_days = 0
  safety_mode             = "protected"

  # Mandatory tags
  map_migrated         = local.tags.map_migrated
  business_criticality = local.tags.business_criticality
  application_name     = local.tags.application_name
  cost_center          = local.tags.cost_center
  business_unit        = local.tags.business_unit
  data_classification  = local.tags.data_classification
  env                  = local.tags.env
}
