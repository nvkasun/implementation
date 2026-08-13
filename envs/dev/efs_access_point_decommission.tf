# PHASE 1 of a two-apply controlled EFS access-point decommission: temporarily adopts the two retired CSI-created access points (no longer mounted by any runtime -- both GoldenGate Argo Applications, pods, PVCs, and Ingresses are already gone) into this Terraform state so the approved Terraform execution identity can delete them, since the operator lacks IAM permission to delete them manually. Import-only; see the child module at terraform/decommission/efs-access-point for the ignore_changes=all contract that prevents Terraform from mutating any CSI-created attribute during import. PHASE 2 (separate, later task, after the real VDR import apply succeeds) removes this file so the next plan proposes exactly these two access points for destruction -- neither EFS filesystem nor mount targets are touched by either phase.

module "goldengate_postgresql_repltest_efs_ap_decommission" {
  source = "../../terraform/decommission/efs-access-point"

  file_system_id = "fs-09bb3373f132d01b0"
}

module "goldengate_mssql_repltest_efs_ap_decommission" {
  source = "../../terraform/decommission/efs-access-point"

  file_system_id = "fs-03d4beaa58f19be78"
}

import {
  to = module.goldengate_postgresql_repltest_efs_ap_decommission.aws_efs_access_point.this
  id = "fsap-05b0995fdcd1cf498"
}

import {
  to = module.goldengate_mssql_repltest_efs_ap_decommission.aws_efs_access_point.this
  id = "fsap-07f0c6516b7c6c656"
}
