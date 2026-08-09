# Mirrors envs/dev/main.tf's exact provider/version pin -- this root is a separate Terraform state, not a separate provider contract.
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "5.91.0"
    }
  }

  required_version = ">= 1.5.0"
}

provider "aws" {
}
