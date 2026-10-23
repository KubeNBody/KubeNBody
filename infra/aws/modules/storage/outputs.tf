# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Storage Module Outputs

output "bucket_name" {
  description = "Backup bucket name"
  value       = aws_s3_bucket.backups.bucket
}

output "bucket_arn" {
  description = "Backup bucket ARN (cluster stack scopes the pgBackRest IRSA policy to it)"
  value       = aws_s3_bucket.backups.arn
}
