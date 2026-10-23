# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Key Vault Module Variables

variable "vault_name" {
  description = "Key Vault name (3-24 chars, alphanumeric + dashes, globally unique)"
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z0-9-]{3,24}$", var.vault_name))
    error_message = "Must be 3-24 chars: alphanumeric and dashes only."
  }
}

variable "resource_group_name" {
  description = "Resource group name"
  type        = string
}

variable "location" {
  description = "Azure region"
  type        = string
}

variable "eso_principal_id" {
  description = "Principal ID of the ESO managed identity (granted Secrets User)"
  type        = string
}

variable "tags" {
  description = "Resource tags"
  type        = map(string)
  default     = {}
}
