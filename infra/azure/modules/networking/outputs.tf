# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Networking Module Outputs

output "vnet_id" {
  description = "Virtual network ID"
  value       = azurerm_virtual_network.main.id
}

output "node_subnet_id" {
  description = "Subnet ID for AKS nodes (pass to the AKS module)"
  value       = azurerm_subnet.nodes.id
}
