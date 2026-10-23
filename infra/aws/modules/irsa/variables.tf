# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# IRSA Module Variables

variable "name_prefix" {
  description = "Role name prefix (the cluster name)"
  type        = string
}

variable "oidc_provider_arn" {
  description = "Cluster OIDC provider ARN"
  type        = string
}

variable "oidc_provider" {
  description = "OIDC issuer host/path without scheme (condition key prefix)"
  type        = string
}

variable "consumers" {
  description = "Role key => ServiceAccount (wildcards allowed) and inline policy JSON (null = no permissions)"
  type = map(object({
    namespace       = string
    service_account = string
    policy_json     = optional(string)
  }))
}

variable "tags" {
  description = "Resource tags"
  type        = map(string)
  default     = {}
}
