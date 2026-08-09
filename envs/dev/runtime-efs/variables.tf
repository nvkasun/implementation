# Reusable, generic inputs only -- this root never reads envs/dev's folder-driven inventory itself; the calling workflow resolves environment/deployment_id/efs_creation_name from `hack/goldengate-deployment-model.py describe <id>` (the single source of truth) and passes them in as plain Terraform variables, so this root stays a dumb, repo-agnostic "one EFS per runtime" template.

variable "environment" {
  description = "GoldenGate environment identifier (e.g. dev); must match the calling deployment's global.environment."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.environment))
    error_message = "environment must be a safe lowercase token."
  }
}

variable "deployment_id" {
  description = "The GoldenGate runtime deployment ID this EFS filesystem belongs to (envs/dev/<deployment_id>/values.yaml); the sole identity boundary for this state."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.deployment_id)) && length(var.deployment_id) <= 64
    error_message = "deployment_id must be a safe lowercase token."
  }
}

variable "efs_creation_name" {
  description = "Deterministic EFS creation identity for this runtime, as derived by hack/goldengate-deployment-model.py's derive_efs_creation_token() (<environment>-<deployment_id>-efs); passed in already-validated, never recomputed here."
  type        = string
  validation {
    condition     = length(var.efs_creation_name) > 0 && length(var.efs_creation_name) <= 64
    error_message = "efs_creation_name must be non-empty and at most 64 characters (the AWS EFS creation-token limit)."
  }
}

variable "shared_security_group_description" {
  description = "Description of the single pre-existing shared security group (NFS/2049 from EKS nodes only) that every GoldenGate runtime EFS filesystem attaches to -- never a per-deployment value."
  type        = string
  default     = "Security group for EFS filesystem - NFS port 2049 from EKS nodes only"
}
