# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# IRSA Module Outputs

output "role_arns" {
  description = "Consumer key => role ARN (annotate the ServiceAccount with eks.amazonaws.com/role-arn)"
  value       = { for k, r in aws_iam_role.consumer : k => r.arn }
}
