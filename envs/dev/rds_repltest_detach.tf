# Detaches the two DEV RDS replication-test module instances from envs/dev Terraform management without destroying the live AWS resources -- their configuration (git::.../aws-tf-module-rds-postgres?ref=v2.1.0 / aws-tf-module-rds-mssql?ref=v1.4.1) is preserved for reference at database-reference/dev/rds_postgres_repltest.tf and database-reference/dev/rds_mssql_repltest.tf. Required because gg-repltest-postgresql now has manually-applied parameter-group settings for GoldenGate logical replication that the approved corporate module does not expose, so it must no longer be reconciled by this root's apply.
removed {
  from = module.rds_postgres_repltest

  lifecycle {
    destroy = false
  }
}

removed {
  from = module.rds_mssql_repltest

  lifecycle {
    destroy = false
  }
}
