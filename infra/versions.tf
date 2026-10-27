terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0, < 7.0"
    }
  }
  # Supply a private encrypted bucket using -backend-config=backend.hcl.
  # This stack is separate from the registry platform state.
  backend "s3" {}
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.aws_account_id]
  default_tags {
    tags = {
      Application = var.project_name
      ManagedBy   = "box-agent-sdlc-infra"
    }
  }
}
