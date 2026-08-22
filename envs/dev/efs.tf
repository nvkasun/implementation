# Managed-mode GoldenGate runtime EFS filesystems: one dedicated module instance per managed runtime deployment, keyed by deployment ID, created through the approved corporate Terraform workflow (this file lives in the normal envs/dev root processed by .github/workflows/10-sub-iam-secrets.yaml -> AbuDhabiCommercialBank/adcb-reusable-workflows/aws-terraform-apply.yaml@main) -- one Terraform state does not mean one EFS: each for_each key below is its own dedicated aws_efs_file_system inside the approved module. Existing-mode deployments get no module instance here since their filesystem already exists outside Terraform. Scope boundary: this file owns the EFS filesystem + mount targets only, via the approved ADCB module below -- it does NOT create EFS access points, which remain owned by the EFS CSI driver's dynamic provisioning (helm/goldengate/templates/efs-storageclass.yaml -> StorageClass -> PVC), exactly as today.

# Explicit managed-EFS decommission control -- NEVER derived from deployment.enabled (or the retired lifecycle.state). deployment.enabled=false by itself always retains managed EFS (see local.goldengate_managed_efs_deployments's own comment); an ID may be added here ONLY after its workload/PVC/access-point cleanup has been independently verified. Removing an ID later makes its managed EFS desired again, so Terraform recreates it in the current environment without reconstructing the runtime descriptor. GoldenGate Runtime Desired-State Simplification: this hold is an intentionally SEPARATE authorization from runtime desired presence -- deployment.enabled=true (activating runtime compute) never by itself clears this hold or authorizes managed-EFS creation for an ID listed here; only an explicit, independently-verified edit to this list does.
locals {
  goldengate_managed_efs_decommission_ids = toset([
    "gg-postgresql-repltest-01",
    "gg-mssql-repltest-01",
  ])

  goldengate_managed_efs_desired_deployments = {
    for id, v in local.goldengate_managed_efs_deployments : id => v
    if !contains(local.goldengate_managed_efs_decommission_ids, id)
  }
}

# Gated on desired (post-decommission) managed EFS deployments, NOT the canonical inventory: the canonical map deliberately keeps a decommissioned deployment's identity even after its EFS is destroyed (see the canonical map's own comment), but the shared EFS SG itself is owned and lifecycled by the separate aws-cloud-factory-infra repo -- once every desired EFS module instance that needed it is gone, that repo is free to delete the SG, and this data lookup must stop resolving it or a later plan/apply here would fail trying to read an intentionally deleted security group. This file never creates, deletes, or otherwise manages the SG resource itself -- read-only lookup only.
data "aws_security_group" "goldengate_efs_shared" {
  count = length(local.goldengate_managed_efs_desired_deployments) > 0 ? 1 : 0

  filter {
    name   = "description"
    values = [local.gg_env_efs_shared_security_group_description]
  }
}

# Fail-closed guard for the decommission set above: an ID that isn't a real managed-EFS deployment is very likely a typo about to silently no-op instead of decommissioning the intended filesystem. GoldenGate Runtime Desired-State Simplification: the previous consistency precondition requiring each decommissioned ID's descriptor to independently declare itself inactive (lifecycle.state=absent) is retired along with that field -- deployment.enabled is now the sole runtime-presence control and is deliberately NOT substituted here, since this list is (per its own header comment) an explicit, out-of-band storage-destruction authorization, never derived from -- or coupled to -- any single descriptor field. A deployment.enabled=true runtime (desired compute presence) can legitimately still have its managed-EFS creation held back by this list; that is exactly the "runtime desired presence" vs "persistent storage destruction authorization" separation this task requires.
resource "terraform_data" "goldengate_managed_efs_decommission_contract" {
  input = local.goldengate_managed_efs_decommission_ids

  lifecycle {
    precondition {
      condition     = length(setsubtract(local.goldengate_managed_efs_decommission_ids, keys(local.goldengate_managed_efs_deployments))) == 0
      error_message = "envs/dev/efs.tf: goldengate_managed_efs_decommission_ids contains a deployment ID that is not a current managed-EFS deployment -- refusing to silently no-op an intended EFS decommission."
    }
    precondition {
      condition = alltrue([
        for id in local.goldengate_managed_efs_decommission_ids :
        try(local.goldengate_runtime_documents[id].replication.enabled, true) == false
      ])
      error_message = "envs/dev/efs.tf: every deployment ID in goldengate_managed_efs_decommission_ids must have replication.enabled=false -- refusing to decommission managed EFS while replication is declared enabled."
    }
  }
}

# One approved-module instance per managed-mode runtime deployment EXCLUDING the explicit decommission set above, keyed by deployment ID -- module.goldengate_runtime_efs["gg-a"] and module.goldengate_runtime_efs["gg-b"] are two dedicated filesystems even though both live in this one Terraform state. `name` is the deterministic creation token; the approved module's v1.0.0 source has been manually verified to set `creation_token = var.name`, so this is an exact, verified contract, not an assumption. THROUGHPUT CONTRACT WARNING: v1.0.0 does NOT pass throughput_mode straight through to the AWS API -- its verified resource code is `throughput_mode = (var.throughput_mode == "enhanced" ? "elastic" : "bursting")`, so the module INPUT "enhanced" is what produces the AWS EFS API value "elastic" (any other input, including the raw AWS value "elastic" itself, falls through to "bursting"); this exact input/output pair is a verified module-source contract, not a historical AWS filesystem observation. Do NOT replace "enhanced" below with the raw AWS value "elastic" without re-reading the module's actual resource code first -- v1.0.0 also has no provisioned-throughput branch, so "provisioned" is not a valid input either.
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

  # Deterministic, non-secret ownership tags merged into the EFS resource via the approved module's verified var.custom_tags input -- GoldenGateDeploymentId is the sole mechanism the read-only managed_efs_inventory_guard uses to map one AWS EFS filesystem back to exactly one runtime deployment; never credentials, secret ARNs, or database details.
  custom_tags = {
    ManagedBy              = "goldengate-eks-app"
    GoldenGateDeploymentId = each.key
    GoldenGateStorage      = "u02"
    GoldenGateEnvironment  = var.environment
  }
}
