# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# AWS Production — BASE stack outputs
# Consumed by the cluster stack (terraform_remote_state), the Makefile and the
# scripts (init-secrets.sh, mirror-images.sh, build-and-push.sh --aws).

output "account_id" {
  description = "AWS account ID"
  value       = var.account_id
}

output "region" {
  description = "AWS region"
  value       = var.region
}

output "azs" {
  description = "AZs with a public subnet"
  value       = local.azs
}

output "vpc_id" {
  description = "VPC ID"
  value       = module.networking.vpc_id
}

output "vpc_cidr" {
  description = "VPC CIDR"
  value       = module.networking.vpc_cidr
}

output "public_subnet_ids" {
  description = "Public subnet IDs (EKS cluster + system node group)"
  value       = module.networking.public_subnet_ids
}

output "subnet_ids_by_az" {
  description = "Public subnet per AZ (worker/GPU node groups use the worker_zone one)"
  value       = module.networking.subnet_ids_by_az
}

output "ecr_registry" {
  description = "ECR registry host — set as ECR_REGISTRY for build-and-push.sh --aws"
  value       = "${var.account_id}.dkr.ecr.${var.region}.amazonaws.com"
}

output "ecr_app_repository_urls" {
  description = "App repo name => URL"
  value       = module.ecr.app_repository_urls
}

output "ecr_mirror_repository_urls" {
  description = "Mirror repo name => URL"
  value       = module.ecr.mirror_repository_urls
}

output "backup_bucket" {
  description = "Backup bucket (pgbackrest/ + minio/ prefixes)"
  value       = module.storage.bucket_name
}

output "backup_bucket_arn" {
  description = "Backup bucket ARN"
  value       = module.storage.bucket_arn
}

output "secret_parameter_names" {
  description = "SSM parameter names (init-secrets.sh)"
  value       = module.params.names
}

output "secret_parameter_arns" {
  description = "SSM parameter ARNs (ESO IRSA policy scope)"
  value       = module.params.arns
}

output "secret_placeholder" {
  description = "Placeholder value init-secrets.sh replaces"
  value       = module.params.placeholder
}
