# Regional AWS STS Interface VPC Endpoint, requested through the approved corporate aws-tf-module-vpc-endpoints module (never a hand-written aws_vpc_endpoint resource) -- STS is required for private EKS workloads to complete IRSA's AssumeRoleWithWebIdentity call without transiting the public internet. VPC/subnet discovery is performed entirely inside the module itself via the standard corporate SSM parameters; no VPC ID, subnet ID, or other network identifier is passed or hardcoded here. This change deliberately provisions STS only -- no other private AWS-service dependency (ecr.api, ecr.dkr, logs, secretsmanager, kms, ssm, s3, etc.) is requested; those are addressed separately only once justified by further live deployment testing.
module "vpc_endpoints" {
  source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-vpc-endpoints.git?ref=v1.0.3"

  interface_endpoints = [
    "sts",
  ]

  gateway_endpoints = []

  application_name     = local.common_tags.ApplicationName
  environment          = local.environment
  data_classification  = local.common_tags.DataClassification
  business_criticality = local.common_tags.BusinessCriticality
  business_unit        = local.common_tags.BusinessUnit
  business_unit_owner  = local.common_tags.BusinessUnitOwner
  cost_center          = local.common_tags.CostCenter
  map_migrated         = local.common_tags["map-migrated"]
  request_reference    = local.common_tags.RequestReference
}
