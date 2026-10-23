# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Azure Production Environment
# Ties the Azure modules together. Separate from GCP (environments/prod under
# infra/) — its own azurerm backend, its own resource group.
#
# Resource group, networking, ACR (+ AcrPull), AKS, Workload Identity, Key
# Vault and the pgBackRest backup storage.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }

  backend "azurerm" {
    # resource_group_name / storage_account_name / container_name / key
    # are supplied via -backend-config during `make init` (from bootstrap outputs).
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

# Resource Group (all Azure app infra lives here)
resource "azurerm_resource_group" "main" {
  name     = var.resource_group_name
  location = var.location

  tags = local.tags
}

# Networking
module "networking" {
  source = "../../modules/networking"

  name_prefix         = var.cluster_name
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location

  vnet_address_space = var.vnet_address_space
  node_subnet_prefix = var.node_subnet_prefix

  tags = local.tags
}

# Container Registry
module "acr" {
  source = "../../modules/acr"

  registry_name       = var.acr_name
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = var.acr_sku

  tags = local.tags
}

# AKS Cluster
module "aks" {
  source = "../../modules/aks"

  cluster_name        = var.cluster_name
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  kubernetes_version  = var.kubernetes_version

  node_subnet_id = module.networking.node_subnet_id

  cluster_zones = var.cluster_zones
  worker_zone   = var.worker_zone

  system_vm_size    = var.system_vm_size
  system_node_count = var.system_node_count

  worker_cpu_vm_size    = var.worker_cpu_vm_size
  worker_cpu_node_count = var.worker_cpu_node_count

  gpu_vm_size    = var.gpu_vm_size
  gpu_node_count = var.gpu_node_count

  tags = local.tags
}

# Workload Identity (ESO + app workload)
# pgBackRest is intentionally NOT a consumer (shared key, not WI).
module "identity" {
  source = "../../modules/identity"

  name_prefix         = var.cluster_name
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  oidc_issuer_url     = module.aks.oidc_issuer_url

  consumers = {
    eso = {
      namespace       = "external-secrets"
      service_account = "external-secrets"
    }
    workload = {
      namespace       = "nbody"
      service_account = "nbody-workload"
    }
  }

  tags = local.tags
}

# Key Vault (+ ESO Secrets User, deployer Secrets Officer)
module "keyvault" {
  source = "../../modules/keyvault"

  vault_name          = var.key_vault_name
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  eso_principal_id    = module.identity.principal_ids["eso"]

  tags = local.tags
}

# pgBackRest backup storage (Azure Blob)
# Shared-key auth: pgBackRest reads account+key from a K8s secret that ESO
# materializes from Key Vault. Retention is enforced by pgBackRest itself, with no
# lifecycle policy: an age-based delete would eventually remove files pgBackRest
# writes once and never rewrites (archive.info), which breaks WAL archiving.
resource "azurerm_storage_account" "pgbackrest" {
  name                     = var.pgbackrest_storage_account_name
  resource_group_name      = azurerm_resource_group.main.name
  location                 = azurerm_resource_group.main.location
  account_tier             = "Standard"
  account_kind             = "StorageV2"
  account_replication_type = "ZRS"

  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = true # pgBackRest uses the shared account key

  tags = local.tags
}

resource "azurerm_storage_container" "pgbackrest" {
  name                  = "pgbackrest"
  storage_account_id    = azurerm_storage_account.pgbackrest.id
  container_access_type = "private"
}

# AcrPull: cluster KUBELET identity may pull from ACR
# NOTE: role assignments can take ~minutes to propagate — a pull right after
# apply may transiently 403 until it settles. depends_on ensures ordering.
resource "azurerm_role_assignment" "aks_acr_pull" {
  scope                = module.acr.id
  role_definition_name = "AcrPull"
  principal_id         = module.aks.kubelet_identity_object_id

  depends_on = [module.aks, module.acr]
}

locals {
  tags = {
    environment = "prod"
    project     = "nbody-simulator"
    managed-by  = "terraform"
    cloud       = "azure"
  }
}
