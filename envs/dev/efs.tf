# Managed-mode GoldenGate runtime EFS filesystems: one dedicated module instance per managed runtime deployment, keyed by deployment ID, created through the approved corporate Terraform workflow (this file lives in the normal envs/dev root processed by .github/workflows/gg-iam-secrets-deployment.yaml -> AbuDhabiCommercialBank/adcb-reusable-workflows/aws-terraform-apply.yaml@main) -- one Terraform state does not mean one EFS: each for_each key below is its own dedicated aws_efs_file_system inside the approved module. Existing-mode deployments get no module instance here since their filesystem already exists outside Terraform. Scope boundary: this file owns the EFS filesystem + mount targets only, via the approved ADCB module below -- it does NOT create EFS access points, which remain owned by the EFS CSI driver's dynamic provisioning (helm/goldengate/templates/efs-storageclass.yaml -> StorageClass -> PVC), exactly as today.

# Single environment-level configuration point for the shared EFS security group (never a per-deployment values.yaml setting); count is conditional on at least one managed deployment existing, so an existing-only environment (today: both live descriptors) never needs to resolve it. The aws_security_group data source itself fails closed if the filter matches zero or more than one security group.
variable "goldengate_efs_shared_security_group_description" {
  description = "Description of the single pre-existing shared security group (NFS/2049 from EKS nodes only) that every GoldenGate runtime EFS filesystem attaches to."
  type        = string
  default     = "Security group for EFS filesystem - NFS port 2049 from EKS nodes only"
}

data "aws_security_group" "goldengate_efs_shared" {
  count = length(local.goldengate_managed_efs_deployments) > 0 ? 1 : 0

  filter {
    name   = "description"
    values = [var.goldengate_efs_shared_security_group_description]
  }
}

# One approved-module instance per managed-mode runtime deployment, keyed by deployment ID -- module.goldengate_runtime_efs["gg-a"] and module.goldengate_runtime_efs["gg-b"] are two dedicated filesystems even though both live in this one Terraform state. `name` is the deterministic creation token; the approved module's v1.0.0 source has been manually verified to set `creation_token = var.name`, so this is an exact, verified contract, not an assumption. THROUGHPUT CONTRACT WARNING: v1.0.0 does NOT pass throughput_mode straight through to the AWS API -- its verified resource code is `throughput_mode = (var.throughput_mode == "enhanced" ? "elastic" : "bursting")`, so the module INPUT "enhanced" is what produces the AWS EFS API value "elastic" (any other input, including the raw AWS value "elastic" itself, falls through to "bursting"); this exact input/output pair is proven by the real existing filesystem fs-05cadf3570f23cd39 ("gg-poc-dev-efs", AWS-reported Performance mode=General Purpose / Throughput mode=Elastic), created by this same module with performance_mode="generalPurpose"/throughput_mode="enhanced". Do NOT replace "enhanced" below with the raw AWS value "elastic" without re-reading the module's actual resource code first -- v1.0.0 also has no provisioned-throughput branch, so "provisioned" is not a valid input either.
module "goldengate_runtime_efs" {
  for_each = local.goldengate_managed_efs_deployments
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
