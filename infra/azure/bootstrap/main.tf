# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Azure Bootstrap Infrastructure
# Run ONCE before the main Azure environment to create the remote Terraform
# state backend (Resource Group + Storage Account + blob container).
# Uses LOCAL state itself (chicken-and-egg).
#
# GCP is unaffected — this is a wholly separate stack under infra/azure/.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
  # Local state on purpose — this stack *creates* the remote backend.
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

# Resource Group holding the Terraform state
resource "azurerm_resource_group" "tfstate" {
  name     = var.state_resource_group_name
  location = var.location

  tags = local.tags
}

# Storage Account for Terraform state
# NAME GOTCHA: 3-24 chars, lowercase alphanumeric only, GLOBALLY unique.
resource "azurerm_storage_account" "tfstate" {
  name                     = var.state_storage_account_name
  resource_group_name      = azurerm_resource_group.tfstate.name
  location                 = azurerm_resource_group.tfstate.location
  account_tier             = "Standard"
  account_kind             = "StorageV2"
  account_replication_type = "ZRS"

  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = true # backend "azurerm" uses the access key by default

  blob_properties {
    versioning_enabled = true
    delete_retention_policy {
      days = 30 # soft-delete window for state recovery
    }
  }

  tags = local.tags
}

# Blob container for the state files
resource "azurerm_storage_container" "tfstate" {
  name                  = "tfstate"
  storage_account_id    = azurerm_storage_account.tfstate.id
  container_access_type = "private"
}

locals {
  tags = {
    purpose    = "terraform-state"
    project    = "nbody"
    managed-by = "terraform"
  }
}
