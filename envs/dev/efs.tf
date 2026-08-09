# Managed-mode GoldenGate runtime EFS filesystems: one dedicated filesystem per runtime deployment (never per pipeline, never shared between an Extract and a Replicat runtime), driven entirely by envs/dev/goldengate_inventory.tf's folder-derived locals; existing-mode deployments get no module instance here since their filesystem already exists outside Terraform. Scope boundary: this file owns the EFS filesystem + mount targets only, via the approved ADCB module below -- it does NOT create EFS access points, which remain owned by the EFS CSI driver's dynamic provisioning (helm/goldengate/templates/efs-storageclass.yaml -> StorageClass -> PVC), exactly as today.

# Single environment-level configuration point for the shared EFS security group (never a per-deployment values.yaml setting); the description string is the only locally-provable stable attribute available in this repo, and the aws_security_group data source itself fails closed if the filter matches zero or more than one security group.
variable "goldengate_efs_shared_security_group_description" {
  description = "Description of the single pre-existing shared security group (NFS/2049 from EKS nodes only) that every GoldenGate runtime EFS filesystem attaches to."
  type        = string
  default     = "Security group for EFS filesystem - NFS port 2049 from EKS nodes only"
}

data "aws_security_group" "goldengate_efs_shared" {
  filter {
    name   = "description"
    values = [var.goldengate_efs_shared_security_group_description]
  }
}

# One approved-module instance per managed-mode runtime deployment, keyed by deployment ID so a rerun always resolves the same module address/filesystem; `name` is the deterministic creation token, later resolvable via `aws efs describe-file-systems --creation-token`. ASSUMPTION flagged for VDR: this presumes the module's `name` input becomes (or equals) the underlying creation_token -- the module source itself was not fetched/inspected, per the "never reimplement or modify the approved module" constraint.
module "goldengate_runtime_efs" {
  for_each = local.goldengate_managed_efs_deployments
  source   = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-efs?ref=v1.0.0"

  name             = each.value.creation_token
  env              = var.environment
  performance_mode = "generalPurpose"
  throughput_mode  = "enhanced"

  existing_security_group_ids = [data.aws_security_group.goldengate_efs_shared.id]

  application_name     = "CloudFactory"
  data_classification  = "General"
  business_criticality = "Low"
  business_unit        = "TechnologyPlatform"
  cost_center          = "219"
}

# Safe, non-sensitive output (deployment ID -> AWS-generated EFS filesystem ID, managed-mode only); existing-mode runtimes are excluded since their fileSystemId is already the committed Git value, never Terraform-resolved.
output "goldengate_runtime_efs_filesystem_ids" {
  description = "Deployment ID -> resolved EFS filesystem ID, for managed-mode GoldenGate runtime deployments only."
  value = {
    for id, mod in module.goldengate_runtime_efs : id => mod.efs_id
  }
}
