# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Azure Production Environment Variables

variable "subscription_id" {
  description = "Azure Subscription ID"
  type        = string
}

variable "location" {
  description = "Azure region"
  type        = string
  default     = "polandcentral"
}

variable "resource_group_name" {
  description = "Resource group for all Azure app infrastructure"
  type        = string
  default     = "nbody-prod-rg"
}

variable "cluster_name" {
  description = "AKS cluster name (also used as network name prefix)"
  type        = string
  default     = "nbody-aks"
}

variable "kubernetes_version" {
  description = "Kubernetes version (null = AKS default stable)"
  type        = string
  default     = null
}

# Registry
variable "acr_name" {
  description = "ACR name (5-50 chars, lowercase alphanumeric, GLOBALLY unique)"
  type        = string
}

variable "acr_sku" {
  description = "ACR SKU"
  type        = string
  default     = "Basic"
}

# Key Vault & backups
variable "key_vault_name" {
  description = "Key Vault name (3-24 chars, alphanumeric + dashes, GLOBALLY unique)"
  type        = string
}

variable "pgbackrest_storage_account_name" {
  description = "Storage account for pgBackRest backups (3-24 chars, lowercase alphanumeric, GLOBALLY unique)"
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]{3,24}$", var.pgbackrest_storage_account_name))
    error_message = "Must be 3-24 lowercase alphanumeric characters (no dashes/underscores)."
  }
}

# Networking
variable "vnet_address_space" {
  description = "VNet CIDR (node IPs only)"
  type        = string
  default     = "10.10.0.0/16"
}

variable "node_subnet_prefix" {
  description = "Subnet CIDR for AKS node IPs"
  type        = string
  default     = "10.10.0.0/20"
}

# Zones (regional cluster, single-zone worker/GPU — mirrors GKE)
variable "cluster_zones" {
  description = "Zones for the regional system pool"
  type        = list(string)
  default     = ["1", "2", "3"]
}

variable "worker_zone" {
  description = "Single zone for worker-cpu and GPU pools"
  type        = string
  default     = "1"
}

# Node pools
variable "system_vm_size" {
  description = "System pool VM size"
  type        = string
  default     = "Standard_D4s_v5"
}

variable "system_node_count" {
  description = "System pool node count"
  type        = number
  default     = 2
}

variable "worker_cpu_vm_size" {
  # 2 vCPU / 4 GiB like the GKE worker pool (c2d-highcpu-2). D2als_v7 (AMD
  # EPYC) is closer, but not offered in every zone: in polandcentral it exists
  # only in zone 1 and does not allocate. Not burstable: the workers run at
  # 100% CPU, and B-series would throttle them.
  description = "CPU worker VM size"
  type        = string
  default     = "Standard_D2ls_v5"
}

variable "worker_cpu_node_count" {
  description = "CPU worker node count (manually scaled)"
  type        = number
  default     = 0
}

variable "gpu_vm_size" {
  description = "GPU VM size (1x T4)"
  type        = string
  default     = "Standard_NC4as_T4_v3"
}

variable "gpu_node_count" {
  description = "GPU node count (manually scaled)"
  type        = number
  default     = 0
}
