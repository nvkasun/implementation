# Module labels were renamed to match the new naming pattern; do not `terraform apply` without first reconciling state (`terraform state mv`/`import`) or it will destroy/recreate secrets already live in AWS.

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
