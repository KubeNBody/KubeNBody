# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# ECR Module Outputs

output "app_repository_urls" {
  description = "App repo name => URL"
  value       = { for k, r in aws_ecr_repository.app : k => r.repository_url }
}

output "mirror_repository_urls" {
  description = "Mirror repo name => URL"
  value       = { for k, r in aws_ecr_repository.mirror : k => r.repository_url }
}

output "app_repository_arns" {
  description = "App repo ARNs"
  value       = [for r in aws_ecr_repository.app : r.arn]
}
