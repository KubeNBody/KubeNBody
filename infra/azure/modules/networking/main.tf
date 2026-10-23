# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Networking Module (Azure)
# VNet + a single node subnet for AKS.
#
# With Azure CNI Overlay (set on the cluster), POD IPs come from an overlay
# CIDR that is independent of the VNet — so this subnet only needs to
# hold NODE IPs. No GKE-style secondary ranges for pods/services.
#
# No custom NSG here on purpose: AKS provisions and manages an NSG in its node
# resource group (MC_*), which already allows the AzureLoadBalancer service tag
# for LB health probes (168.63.129.16). Adding a custom deny NSG on this
# subnet is the classic way to break probe/intra-cluster traffic, so we don't.

resource "azurerm_virtual_network" "main" {
  name                = "${var.name_prefix}-vnet"
  resource_group_name = var.resource_group_name
  location            = var.location
  address_space       = [var.vnet_address_space]

  tags = var.tags
}

resource "azurerm_subnet" "nodes" {
  name                 = "${var.name_prefix}-nodes"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [var.node_subnet_prefix]
}
