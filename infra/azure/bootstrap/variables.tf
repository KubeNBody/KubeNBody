# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Azure Bootstrap Variables

variable "subscription_id" {
  description = "Azure Subscription ID"
  type        = string
}

variable "location" {
  description = "Azure region"
  type        = string
  default     = "polandcentral"
}

variable "state_resource_group_name" {
  description = "Resource Group to hold the Terraform state storage account"
  type        = string
  default     = "nbody-tfstate-rg"
}

variable "state_storage_account_name" {
  description = "Storage Account for TF state (3-24 chars, lowercase alphanumeric, globally unique)"
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]{3,24}$", var.state_storage_account_name))
    error_message = "Must be 3-24 lowercase alphanumeric characters (no dashes/underscores)."
  }
}
