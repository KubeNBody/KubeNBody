# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# EKS Module Outputs

output "cluster_name" {
  description = "EKS cluster name"
  value       = module.eks.cluster_name
}

output "cluster_endpoint" {
  description = "API server endpoint"
  value       = module.eks.cluster_endpoint
}

output "oidc_provider_arn" {
  description = "IRSA OIDC provider ARN (trust principal)"
  value       = module.eks.oidc_provider_arn
}

output "oidc_provider" {
  description = "OIDC issuer host/path, no scheme (trust condition key prefix)"
  value       = module.eks.oidc_provider
}

output "cluster_oidc_issuer_url" {
  description = "OIDC issuer URL — unique per cluster (configure.sh ties the ACTIVE pgBackRest generation to it)"
  value       = module.eks.cluster_oidc_issuer_url
}

output "node_security_group_id" {
  description = "Node security group"
  value       = module.eks.node_security_group_id
}

output "ebs_csi_role_arn" {
  description = "IRSA role of the EBS CSI controller"
  value       = aws_iam_role.ebs_csi.arn
}
