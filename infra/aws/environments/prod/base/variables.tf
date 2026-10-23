# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# AWS Production — BASE stack variables

variable "account_id" {
  description = "AWS account ID to deploy into (the provider refuses any other)"
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Must be a 12-digit AWS account ID."
  }
}

variable "region" {
  description = "AWS region"
  type        = string
  default     = "eu-central-1"
}

variable "name_prefix" {
  description = "Prefix for resource names (matches the cluster name)"
  type        = string
  default     = "nbody-eks"
}

# Networking
variable "vpc_cidr" {
  description = "VPC CIDR (VPC CNI pods take IPs from it too)"
  type        = string
  default     = "10.20.0.0/16"
}

variable "azs" {
  description = "AZs for the public subnets; empty = first 3 available. Must include the worker_zone chosen in preflight"
  type        = list(string)
  default     = []
}

# Registry
variable "app_repositories" {
  description = "ECR repos for images built by scripts/build-and-push.sh"
  type        = list(string)
  default     = ["nbody", "nbody-gpu", "nbody-dashboard"]
}

variable "ecr_keep_images" {
  description = "Images kept per app repo"
  type        = number
  default     = 5
}

# Backups & secrets
variable "backup_bucket_name" {
  description = "S3 bucket for pgBackRest + MinIO backups (globally unique)"
  type        = string
}

variable "secret_names" {
  description = "SSM parameter names = ExternalSecret remoteRef keys in configs/base/secrets"
  type        = list(string)
  default = [
    "nbody-redis-password",
    "nbody-minio-root-user",
    "nbody-minio-root-password",
    "nbody-rabbitmq-user",
    "nbody-rabbitmq-password",
    "nbody-grafana-admin-password",
    "nbody-jwt-secret-key",
    "nbody-default-user-password",
  ]
}

variable "allow_destroy" {
  description = "Let `terraform destroy` remove ECR repos with images and the backup bucket with backups (full teardown only)"
  type        = bool
  default     = false
}
