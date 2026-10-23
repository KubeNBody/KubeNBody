# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Identity Module Variables

variable "name_prefix" {
  description = "Prefix for managed identity names"
  type        = string
}

variable "resource_group_name" {
  description = "Resource group name"
  type        = string
}

variable "location" {
  description = "Azure region"
  type        = string
}

variable "oidc_issuer_url" {
  description = "AKS cluster OIDC issuer URL (federation issuer)"
  type        = string
}

variable "consumers" {
  description = "Map of consumer key => the K8s ServiceAccount it federates with"
  type = map(object({
    namespace       = string
    service_account = string
  }))
}

variable "tags" {
  description = "Resource tags"
  type        = map(string)
  default     = {}
}
