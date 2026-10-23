# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# ACR Module Variables

variable "registry_name" {
  description = "ACR name (5-50 chars, lowercase alphanumeric, globally unique)"
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]{5,50}$", var.registry_name))
    error_message = "Must be 5-50 lowercase alphanumeric characters (no dashes/underscores)."
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

variable "sku" {
  description = "ACR SKU (Basic/Standard/Premium)"
  type        = string
  default     = "Basic"
}

variable "tags" {
  description = "Resource tags"
  type        = map(string)
  default     = {}
}
