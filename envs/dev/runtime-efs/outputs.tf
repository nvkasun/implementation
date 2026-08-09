# Safe, non-sensitive output: this runtime's resolved EFS filesystem ID. Consumed via `terraform output -raw efs_id` (or, once this root's remote-state backend is wired, `terraform_remote_state`) by the deploy workflow to populate RESOLVED_EFS_ID for Helm and Argo CD -- never resolved by scanning AWS.
output "efs_id" {
  description = "The AWS-generated EFS filesystem ID for this GoldenGate runtime deployment."
  value       = module.goldengate_runtime_efs.efs_id
}
