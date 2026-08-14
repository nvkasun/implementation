# PHASE RDS-1 of a two-apply controlled old-VPC RDS decommission: temporarily adopts the two physical DEV replication-test DB instances (gg-repltest-postgresql, gg-repltest-mssql) into this Terraform state so the approved Terraform execution identity can delete them -- they were previously detached from envs/dev management (see envs/dev/rds_repltest_detach.tf) and still exist only as unmanaged AWS resources. Raw aws_db_instance resources ONLY, never the corporate rds-postgres/rds-mssql modules preserved at database-reference/dev/*.tf -- restoring those modules here would re-attach their full original configuration (including supporting resources) instead of adopting the bare instances for deletion. lifecycle.ignore_changes=all means this file never reconciles any live setting (including the manually-applied PostgreSQL logical-replication parameter-group settings called out in rds_repltest_detach.tf) -- import-only. PHASE RDS-2 (separate, later task, after this import apply succeeds) removes this file so the next plan proposes exactly these two DB instances for destruction; no other resource is touched by either phase.

resource "aws_db_instance" "rds_postgres_repltest_decommission" {
  identifier        = "gg-repltest-postgresql"
  instance_class    = "db.t4g.micro"
  engine            = "postgres"
  engine_version    = "16.14"
  allocated_storage = 32
  port              = 4009

  skip_final_snapshot      = true
  delete_automated_backups = true

  lifecycle {
    ignore_changes = all
  }
}

resource "aws_db_instance" "rds_mssql_repltest_decommission" {
  identifier        = "gg-repltest-mssql"
  instance_class    = "db.m5.large"
  engine            = "sqlserver-se"
  engine_version    = "15.00.4316.3.v1"
  allocated_storage = 20
  port              = 5002
  license_model     = "license-included"

  skip_final_snapshot      = true
  delete_automated_backups = true

  lifecycle {
    ignore_changes = all
  }
}

import {
  to = aws_db_instance.rds_postgres_repltest_decommission
  id = "gg-repltest-postgresql"
}

import {
  to = aws_db_instance.rds_mssql_repltest_decommission
  id = "gg-repltest-mssql"
}
