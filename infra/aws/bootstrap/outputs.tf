# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# AWS Bootstrap Outputs
# Feed these into `make init` (-backend-config) for the prod stacks.

output "state_bucket_name" {
  description = "S3 bucket for the s3 backend"
  value       = aws_s3_bucket.tfstate.bucket
}

output "region" {
  description = "Region of the state bucket"
  value       = var.region
}

output "backend_config_hint" {
  description = "backend-config args for `terraform init` in environments/prod/base"
  value = join(" ", [
    "-backend-config=\"bucket=${aws_s3_bucket.tfstate.bucket}\"",
    "-backend-config=\"region=${var.region}\"",
    "-backend-config=\"key=prod/base.tfstate\"",
  ])
}
