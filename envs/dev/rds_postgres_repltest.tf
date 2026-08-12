# DEV-only RDS PostgreSQL instance for the GoldenGate PostgreSQL -> MSSQL replication test 
module "rds_postgres_repltest" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-rds-postgres?ref=v2.1.0"

  identifier     = "gg-repltest-postgresql"
  instance_class = "db.t4g.micro"
  engine_version = "16.4"
  custom_port    = 4003

  enable_performance_insights           = true
  performance_insights_retention_period = 7
  enhanced_monitoring_enabled           = true
  monitoring_interval                   = 60
  enable_cw_logs                        = true
  multi_az                              = false
  storage                               = 32
  storage_type                          = "gp3"
  backup_retention_period               = 7
  backup_window                         = "19:00-21:00"
  maintenance_window                    = "sat:22:00-sat:23:30"
  recovery_window_in_days               = 0
  additional_security_group_ids         = []

  application_name     = "CloudFactory"
  data_classification  = "General"
  business_criticality = "Low"
  business_unit        = "TechnologyPlatform"
  cost_center          = "219"
  env                  = var.environment
}
