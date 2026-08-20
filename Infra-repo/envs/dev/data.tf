data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

# data "aws_ssm_parameter" "vpc_id" {
#   name = "/ADCB/Network/VPC/ID"
# }

# data "aws_ssm_parameter" "vpc_cidr" {
#   name = "/ADCB/Network/VPC/CIDR"
# }

# data "aws_ssm_parameter" "app_subnet_ids" {
#   name = "/ADCB/Network/Subnets/App/IDs"
# }
