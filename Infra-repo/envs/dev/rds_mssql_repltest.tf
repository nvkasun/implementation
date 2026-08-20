# DEV RDS SQL Server instance for the GoldenGate PostgreSQL -> MSSQL replication test (pipeline repltest-pg-to-mssql-001), paired with module.rds_postgres_repltest. Owned by aws-cloud-factory-infra under the current ownership model. Provisioning only: no database users/schemas/databases/tables/GoldenGate configuration here -- those are created manually once the instance is available. mssql_backup_bucket is deliberately omitted: this DEV replication-test instance does not require the MSSQL native backup-to-S3 integration, and no S3 bucket/module is created to satisfy it -- the real Cloud Factory Terraform plan must confirm the corporate module accepts this input being absent. Networking/security-group/master-credential/KMS behavior beyond what is set below is entirely the approved module's own default handling; there is a known, separately-tracked corporate RDS-module security-group lookup issue owned by the module team -- not solved here.
module "rds_mssql_repltest" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-rds-mssql?ref=v1.4.1"

  env        = local.environment
  identifier = "gg-repltest-mssql"

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

  storage            = 20
  custom_port        = 5002
  enable_autoscaling = true
  max_storage        = 100

  storage_type = "gp3"

  backup_retention_period = 0

  request_reference    = local.common_tags.RequestReference
  map_migrated         = local.common_tags["map-migrated"]
  application_name     = local.common_tags.ApplicationName
  data_classification  = local.common_tags.DataClassification
  business_criticality = local.common_tags.BusinessCriticality
  business_unit        = local.common_tags.BusinessUnit
  cost_center          = local.common_tags.CostCenter
  business_unit_owner  = "ganesh.harikrishnan"
}
