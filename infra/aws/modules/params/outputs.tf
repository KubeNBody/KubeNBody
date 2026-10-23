# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Params Module Outputs

output "names" {
  description = "Parameter names"
  value       = [for p in aws_ssm_parameter.secret : p.name]
}

output "arns" {
  description = "Parameter ARNs (cluster stack scopes the ESO IRSA policy to them)"
  value       = [for p in aws_ssm_parameter.secret : p.arn]
}

output "placeholder" {
  description = "Placeholder value (init-secrets.sh overwrites only parameters still holding it)"
  value       = var.placeholder
}
