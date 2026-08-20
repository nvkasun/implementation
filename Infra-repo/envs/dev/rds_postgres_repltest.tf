# DEV RDS PostgreSQL instance for the GoldenGate PostgreSQL -> MSSQL replication test. Owned by aws-cloud-factory-infra under the current ownership model (RDS/VPC/EKS/platform infra is infra-side; GOLDENGATE-EKS-APP owns only GoldenGate application/orchestration Terraform). POST-PROVISION ACTION REQUIRED, not expressed below because the approved v2.1.0 module does not expose these parameters: rds.logical_replication, wal_level, max_replication_slots, and max_wal_senders must be applied manually via the DB parameter group after this instance is created, to support GoldenGate logical replication.
module "rds_postgres_repltest" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-rds-postgres?ref=v2.1.0"

  identifier     = "gg-repltest-postgresql"
  instance_class = "db.t4g.micro"
  engine_version = "16.14"
  custom_port    = 4009

  enable_performance_insights           = true
  performance_insights_retention_period = 7
  enhanced_monitoring_enabled           = true
  monitoring_interval                   = 60
  enable_cw_logs                        = true
  multi_az                              = false
  storage                               = 32
  storage_type                          = "gp3"
  backup_retention_period               = 0
  backup_window                         = "19:00-21:00"
  maintenance_window                    = "sat:22:00-sat:23:30"
  recovery_window_in_days               = 0
  additional_security_group_ids         = []

  application_name     = local.common_tags.ApplicationName
  data_classification  = local.common_tags.DataClassification
  business_criticality = local.common_tags.BusinessCriticality
  business_unit        = local.common_tags.BusinessUnit
  business_unit_owner  = "ganesh.harikrishnan"
  cost_center          = local.common_tags.CostCenter
  map_migrated         = local.common_tags["map-migrated"]
  request_reference    = local.common_tags.RequestReference
  env                  = local.environment
}
