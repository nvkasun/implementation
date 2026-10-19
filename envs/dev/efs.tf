# Managed-mode EFS: one dedicated module instance per managed deployment. Existing-mode deployments have no instance here. Owns the filesystem + mount targets only -- never EFS access points (CSI-provisioned).

# Explicit managed-EFS decommission list -- never derived from deployment.enabled. Add an id only after verified cleanup; this hold is a separate authorization from runtime desired presence.
locals {
  # Empty unless a verified EFS decommission target exists.
  goldengate_managed_efs_decommission_ids = toset([])

  goldengate_managed_efs_desired_deployments = {
    for id, v in local.goldengate_managed_efs_deployments : id => v
    if !contains(local.goldengate_managed_efs_decommission_ids, id)
  }
}

# Gated on desired (post-decommission) deployments. The shared EFS security group is infra-owned -- read-only lookup, never managed here.
data "aws_security_group" "goldengate_efs_shared" {
  count = length(local.goldengate_managed_efs_desired_deployments) > 0 ? 1 : 0

  filter {
    name   = "description"
    values = [local.gg_env_efs_shared_security_group_description]
  }
}

# Guards against a typo silently no-opping a decommission. deployment.enabled never substitutes for this explicit storage-destruction authorization.
resource "terraform_data" "goldengate_managed_efs_decommission_contract" {
  input = local.goldengate_managed_efs_decommission_ids

  lifecycle {
    precondition {
      condition     = length(setsubtract(local.goldengate_managed_efs_decommission_ids, keys(local.goldengate_managed_efs_deployments))) == 0
      error_message = "envs/dev/efs.tf: goldengate_managed_efs_decommission_ids contains a deployment ID that is not a current managed-EFS deployment -- refusing to silently no-op an intended EFS decommission."
    }
    precondition {
      # Decommission list must stay empty -- manual GoldenGate/replication state is opaque to Git and can't prove a decommission is safe.
      condition     = length(local.goldengate_managed_efs_decommission_ids) == 0
      error_message = "envs/dev/efs.tf: goldengate_managed_efs_decommission_ids must be empty -- manual GoldenGate replication/process state is opaque to Git and cannot be used to prove a managed-EFS decommission is safe. Adding an ID here requires a future, separately-approved teardown safety mechanism that this task does not implement."
    }
  }
}

# moved blocks preserve Terraform state identity across the four id renames -- for_each does NOT correlate addresses by matching creation_token, so without this Terraform would destroy/recreate the EFS. Equal creation_token is necessary but not sufficient; complementary to LEGACY_MANAGED_EFS_CREATION_TOKENS. Never remove one without the other, never add a new one for a genuinely new runtime.
moved {
  from = module.goldengate_runtime_efs["gg-postgresql-repltest-01"]
  to   = module.goldengate_runtime_efs["gg-postgresql-repltest-001"]
}

moved {
  from = module.goldengate_runtime_efs["gg-mssql-repltest-01"]
  to   = module.goldengate_runtime_efs["gg-mssql-repltest-001"]
}

moved {
  from = module.goldengate_runtime_efs["gg-oracle-repltest-01"]
  to   = module.goldengate_runtime_efs["gg-oracle-repltest-002"]
}

moved {
  from = module.goldengate_runtime_efs["gg-postgresql-repltest-02"]
  to   = module.goldengate_runtime_efs["gg-postgresql-repltest-002"]
}

# One module instance per desired managed deployment; `name` is the deterministic creation token. v1.0.0's verified resource code: `throughput_mode = (var.throughput_mode == "enhanced" ? "elastic" : "bursting")` -- do not pass "elastic"/"provisioned" directly.
module "goldengate_runtime_efs" {
  for_each = local.goldengate_managed_efs_desired_deployments
  source   = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-efs?ref=v1.0.0"

  name             = each.value.creation_token
  env              = var.environment
  performance_mode = "generalPurpose"
  throughput_mode  = "enhanced"

  existing_security_group_ids = [data.aws_security_group.goldengate_efs_shared[0].id]

  application_name     = "CloudFactory"
  data_classification  = "General"
  business_criticality = "Low"
  business_unit        = "TechnologyPlatform"
  cost_center          = "219"

  # GoldenGateDeploymentId is how managed_efs_inventory_guard maps an EFS filesystem back to its deployment -- never secrets here.
  custom_tags = {
    ManagedBy              = "goldengate-eks-app"
    GoldenGateDeploymentId = each.key
    GoldenGateStorage      = "u02"
    GoldenGateEnvironment  = var.environment
  }
}
