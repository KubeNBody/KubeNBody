# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# AWS Production — BASE stack (permanent)
# Everything that must survive `make down`: network, image registry, backup
# bucket, secret parameters. Costs ~$1-3/month while the cluster is down
# (ECR + S3 storage; the VPC, S3 gateway endpoint and standard SSM parameters
# are free).
#
# The EPHEMERAL cluster stack (environments/prod/cluster) reads these outputs
# via terraform_remote_state.

terraform {
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  backend "s3" {
    # bucket / region / key are supplied via -backend-config during `make init`
    # (from bootstrap outputs). S3-native locking — no DynamoDB table.
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id] # never another account

  default_tags {
    tags = local.tags
  }
}

data "aws_availability_zones" "available" {
  state = "available"

  # Standard AZs only: an opted-in Local Zone (eu-central-1-ham-1a) would sort
  # before eu-central-1a and be picked by the slice below.
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  # First N AZs unless pinned explicitly (preflight picks worker_zone among them).
  azs = length(var.azs) > 0 ? var.azs : slice(sort(data.aws_availability_zones.available.names), 0, 3)

  mirror_images = jsondecode(file("${path.module}/../../../mirror-images.json"))

  tags = {
    environment = "prod"
    project     = "nbody-simulator"
    managed-by  = "terraform"
    cloud       = "aws"
    stack       = "base"
  }
}

# Networking — public subnets only, no NAT
module "networking" {
  source = "../../../modules/networking"

  name_prefix = var.name_prefix
  region      = var.region
  vpc_cidr    = var.vpc_cidr
  azs         = local.azs
}

# Container registry — app repos + third-party mirrors
module "ecr" {
  source = "../../../modules/ecr"

  app_repositories    = var.app_repositories
  mirror_repositories = [for m in local.mirror_images : m.repo]
  keep_images         = var.ecr_keep_images
  force_delete        = var.allow_destroy
}

# Backup bucket — pgBackRest repo + MinIO mirror
module "storage" {
  source = "../../../modules/storage"

  bucket_name   = var.backup_bucket_name
  force_destroy = var.allow_destroy
}

# Secret parameters (values set by scripts/init-secrets.sh)
module "params" {
  source = "../../../modules/params"

  names = var.secret_names
}
