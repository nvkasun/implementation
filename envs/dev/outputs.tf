output "goldengate_monitoring_table_name" {
  description = "Name of the shared GoldenGate monitoring state table"
  value       = module.goldengate_pipeline_state.name
}

output "goldengate_monitoring_table_arn" {
  description = "ARN of the shared GoldenGate monitoring state table"
  value       = module.goldengate_pipeline_state.arn
}
