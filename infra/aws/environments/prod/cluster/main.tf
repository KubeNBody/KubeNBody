# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# AWS Production — CLUSTER stack (ephemeral: `make up` / `make down`)
# EKS + node groups + add-ons + IRSA roles. Created per session and destroyed
# afterwards: EKS bills the control plane while the cluster exists, even with
# zero nodes. Everything permanent lives in the base stack, read here via
# terraform_remote_state.
#
# The IRSA roles live here, not in base: each new cluster has a new OIDC issuer
# and the trust policies point at it.

terraform {
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  backend "s3" {
    # bucket / region / key via -backend-config during `make init`.
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

data "terraform_remote_state" "base" {
  backend = "s3"

  config = {
    bucket = var.state_bucket
    key    = "prod/base.tfstate"
    region = var.region
  }
}

locals {
  base = data.terraform_remote_state.base.outputs

  tags = {
    environment = "prod"
    project     = "nbody-simulator"
    managed-by  = "terraform"
    cloud       = "aws"
    stack       = "cluster"
  }
}

# EKS cluster + node groups + add-ons (+ EBS CSI IRSA role)
module "eks" {
  source = "../../../modules/eks"

  cluster_name       = var.cluster_name
  kubernetes_version = var.kubernetes_version

  vpc_id           = local.base.vpc_id
  subnet_ids       = local.base.public_subnet_ids
  worker_subnet_id = local.base.subnet_ids_by_az[var.worker_zone]

  endpoint_public_access_cidrs = var.endpoint_public_access_cidrs

  system_instance_type = var.system_instance_type
  system_node_count    = var.system_node_count
  worker_instance_type = var.worker_instance_type
  worker_max_nodes     = var.worker_max_nodes
  gpu_instance_type    = var.gpu_instance_type
  gpu_max_nodes        = var.gpu_max_nodes

  tags = local.tags
}

# IRSA roles for in-cluster consumers
data "aws_iam_policy_document" "eso" {
  # Read the app secret parameters. No KMS statement: the AWS-managed aws/ssm
  # key's policy already allows decrypt via SSM for principals in the account.
  statement {
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:GetParametersByPath",
    ]
    resources = local.base.secret_parameter_arns
  }
  statement {
    actions   = ["ssm:DescribeParameters"]
    resources = ["*"] # DescribeParameters has no resource-level scoping
  }
}

data "aws_iam_policy_document" "pgbackrest" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = [local.base.backup_bucket_arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["pgbackrest", "pgbackrest/*"]
    }
  }
  statement {
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = ["${local.base.backup_bucket_arn}/pgbackrest/*"]
  }
}

module "irsa" {
  source = "../../../modules/irsa"

  name_prefix       = var.cluster_name
  oidc_provider_arn = module.eks.oidc_provider_arn
  oidc_provider     = module.eks.oidc_provider

  consumers = {
    eso = {
      namespace       = "external-secrets"
      service_account = "external-secrets"
      policy_json     = data.aws_iam_policy_document.eso.json
    }
    # WAL archive-push runs in the INSTANCE pod (nbody-db-instance), backups in
    # the backup job (nbody-db-pgbackrest), restores in a PGO job — one wildcard
    # covers them all.
    pgbackrest = {
      namespace       = "nbody"
      service_account = "nbody-db-*"
      policy_json     = data.aws_iam_policy_document.pgbackrest.json
    }
    # Parity with Azure; no permissions yet.
    workload = {
      namespace       = "nbody"
      service_account = "nbody-workload"
    }
  }

  tags = local.tags
}
