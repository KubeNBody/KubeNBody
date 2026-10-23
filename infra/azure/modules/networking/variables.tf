# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Networking Module Variables

variable "name_prefix" {
  description = "Prefix for network resource names"
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

variable "vnet_address_space" {
  description = "VNet CIDR (node IPs only; pod IPs are on the overlay CIDR)"
  type        = string
  default     = "10.10.0.0/16"
}

variable "node_subnet_prefix" {
  description = "Subnet CIDR for AKS node IPs"
  type        = string
  default     = "10.10.0.0/20"
}

variable "tags" {
  description = "Resource tags"
  type        = map(string)
  default     = {}
}
