terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0, < 7.0"
    }
  }

  backend "s3" {}

  required_version = ">= 1.6"
}

provider "aws" {
  region = var.region

  default_tags {
    tags = local.common_tags
  }
}

# RECONSTRUCTION GAP: the real VDR main.tf is known to contain a further historical/commented-out KMS resource + alias block below this point (per the supplied VDR screenshot description), but its exact text was not provided to this reconstruction and is therefore intentionally NOT reproduced here rather than fabricated. Do not add a new KMS resource to fill this gap -- see the final report's reconstruction-gap list.
