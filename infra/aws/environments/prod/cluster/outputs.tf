# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# AWS Production — CLUSTER stack outputs
# `make configure` fills the configs/aws placeholders from these (the role ARNs
# change with every `make up`).

output "cluster_name" {
  description = "EKS cluster name"
  value       = module.eks.cluster_name
}

output "cluster_oidc_issuer_url" {
  description = "OIDC issuer URL — unique per cluster (ties the ACTIVE pgBackRest generation to this cluster)"
  value       = module.eks.cluster_oidc_issuer_url
}

output "region" {
  description = "AWS region"
  value       = var.region
}

output "eso_role_arn" {
  description = "ESO IRSA role -> configs/aws/operators (external-secrets SA annotation)"
  value       = module.irsa.role_arns["eso"]
}

output "pgbackrest_role_arn" {
  description = "pgBackRest IRSA role -> configs/aws/infra (PostgresCluster + SA annotations)"
  value       = module.irsa.role_arns["pgbackrest"]
}

output "workload_role_arn" {
  description = "App workload IRSA role (nbody-workload SA, if used)"
  value       = module.irsa.role_arns["workload"]
}

output "backup_bucket" {
  description = "Backup bucket (from the base stack)"
  value       = local.base.backup_bucket
}

output "vpc_id" {
  description = "VPC ID (from the base stack; `make down` checks no NLBs remain in it)"
  value       = local.base.vpc_id
}

output "ecr_registry" {
  description = "ECR registry host (from the base stack)"
  value       = local.base.ecr_registry
}

output "connect_command" {
  description = "kubeconfig command (context alias = cluster name)"
  value       = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name} --alias ${module.eks.cluster_name} --profile <NBODY_AWS_PROFILE>"
}
