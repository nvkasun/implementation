# RECONSTRUCTION GAP: the real VDR sg.tf is known to also carry an older, disabled/commented-out security-group block above the active module below (per the supplied VDR screenshot description), but its exact text was not provided to this reconstruction and is therefore intentionally NOT reproduced here rather than fabricated -- see the final report's reconstruction-gap list. Do not activate any such historical configuration when the real source is later inspected. MISSING SOURCE DEPENDENCY: this module also reads rules/ingress_rules.yaml and rules/egress_rules.yaml (relative to path.root) -- neither file exists in this local reconstruction and their content was not supplied, so they are NOT fabricated here; terraform validate/plan against the real corporate module will fail closed on these missing files until the real rules/*.yaml are restored from the actual VDR source.
module "ogg_security_group_efs" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-security-group.git?ref=v1.1.5"

  name        = "gg-${local.app_prefix}-${local.environment}-efs-sg"
  description = "Security group for EFS filesystem - NFS port 2049 from EKS nodes only"

  ingress_rules = yamldecode(file("${path.root}/rules/ingress_rules.yaml"))
  egress_rules  = yamldecode(file("${path.root}/rules/egress_rules.yaml"))

  application_name     = local.common_tags.ApplicationName
  environment          = local.environment
  data_classification  = local.common_tags.DataClassification
  business_criticality = local.common_tags.BusinessCriticality
  business_unit        = local.common_tags.BusinessUnit
  cost_center          = local.common_tags.CostCenter
}
