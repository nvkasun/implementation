# DEV-only RDS SQL Server instance for the GoldenGate PostgreSQL -> MSSQL replication test (pipeline repltest-pg-to-mssql-001), paired with module.rds_postgres_repltest. Provisioning only: no database users/schemas/databases/tables/GoldenGate configuration here -- those are created manually once the instance is available. mssql_backup_bucket is deliberately omitted: this DEV replication-test instance does not require the MSSQL native backup-to-S3 integration, and no S3 bucket/module is created to satisfy it -- the real VDR Terraform plan must confirm the corporate module accepts this input being absent. Networking/security-group/master-credential/KMS behavior beyond what is set below is entirely the approved module's own default handling.
module "rds_mssql_repltest" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-rds-mssql?ref=v1.3.0"

  environment = local.tags.env
  identifier  = "gg-repltest-mssql"

  instance_class = "db.m5.large"

  engine         = "sqlserver-se"
  engine_version = "15.00.4316.3.v1"

  master_username = "dbadmin"
  license_model   = "license-included"

  enable_performance_insights           = true
  performance_insights_retention_period = 7

  enhanced_monitoring_enabled = true
  monitoring_interval         = 60

  enable_cw_logs          = true
  cloudwatch_logs_exports = ["agent", "error"]

  multi_az = false

  storage = 20

  enable_autoscaling = true
  max_storage        = 100

  storage_type = "gp3"

  backup_retention_period = 7

  request_reference    = local.tags.request_reference
  map_migrated         = local.tags.map_migrated
  application_name     = local.tags.application_name
  data_classification  = local.tags.data_classification
  business_criticality = local.tags.business_criticality
  business_unit        = local.tags.business_unit
  cost_center          = local.tags.cost_center
  business_unit_owner  = local.tags.business_unit_owner
}
