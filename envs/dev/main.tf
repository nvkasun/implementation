terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "5.91.0"
    }
  }

  backend "s3" {}

  required_version = ">= 1.5.0"
}

# Region derives from envs/dev/environment.yaml (see environment.tf) -- never a second, independently-maintained region literal.
provider "aws" {
  region = local.gg_env_region
}