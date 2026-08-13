# TEMPORARY DECOMMISSION-ONLY MODULE.
# Never use this module for normal GoldenGate EFS provisioning.
# Normal access points remain owned by the EFS CSI driver.
# This module exists only to temporarily adopt retired CSI-created APs so the
# approved Terraform execution identity can delete them during controlled
# environment decommission.

resource "aws_efs_access_point" "this" {
  file_system_id = var.file_system_id

  lifecycle {
    ignore_changes = all
  }
}
