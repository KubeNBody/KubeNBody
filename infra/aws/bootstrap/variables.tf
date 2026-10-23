# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# AWS Bootstrap Variables

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

variable "state_bucket_name" {
  description = "S3 bucket for TF state (3-63 chars, lowercase, globally unique)"
  type        = string

  validation {
    condition = can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.state_bucket_name))
    # No dots: dotted bucket names break virtual-hosted-style TLS (pgBackRest, S3 backend).
    error_message = "Must be 3-63 chars: lowercase letters, digits and dashes (no dots)."
  }
}
