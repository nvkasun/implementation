module "goldengate_pipeline_state" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-dynamodb.git?ref=v1.2.0"

  name      = "gg-eks-pipeline"
  hash_key  = "pipeline"
  range_key = "recordType"

  attributes = [
    {
      name = "pipeline"
      type = "S"
    },
    {
      name = "recordType"
      type = "S"
    }
  ]

  billing_mode = "PAY_PER_REQUEST"
  safety_mode  = "on_demand"

  ttl_enabled        = true
  ttl_attribute_name = "ttl"

  global_secondary_indexes = []
  local_secondary_indexes  = []

  autoscaling_enabled = false

  custom_kms_key_arn = null

  map_migrated         = "comm5TZY31HX9S"
  business_criticality = "Low"
  application_name     = "CloudFactory"
  cost_center          = "219"
  business_unit        = "TechnologyPlatform"
  business_unit_owner  = "ganesh.harikrishnan"
  data_classification  = "General"
  env                  = "dev"
}
