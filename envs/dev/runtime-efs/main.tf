# One EFS filesystem for exactly one GoldenGate runtime deployment (var.deployment_id) -- a single fixed instance below, never a multi-instance construct; a source+target replication setup normally means running this root twice, once per runtime, each with its own isolated state. Scope boundary: this root owns the EFS filesystem + mount targets only, via the approved ADCB module below -- it does NOT create EFS access points, which remain owned by the EFS CSI driver's dynamic provisioning (helm/goldengate/templates/efs-storageclass.yaml -> StorageClass -> PVC), exactly as today. The shared EFS security-group lookup lives here (not in the shared envs/dev root) so an environment with zero managed runtimes never needs to resolve it at all -- this root simply is never invoked in that case.

data "aws_security_group" "goldengate_efs_shared" {
  filter {
    name   = "description"
    values = [var.shared_security_group_description]
  }
}

# ASSUMPTION (unverifiable locally, flagged for VDR): this presumes the module's `name` input becomes -- or exactly equals -- the underlying aws_efs_file_system creation_token, so it is later resolvable via `aws efs describe-file-systems --creation-token` or `terraform output efs_id` from this same state; the module source itself was not fetched or inspected, per the "never reimplement or modify the approved module" constraint.
module "goldengate_runtime_efs" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-efs?ref=v1.0.0"

  name             = var.efs_creation_name
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
