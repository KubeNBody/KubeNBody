# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Identity Module Outputs

output "client_ids" {
  description = "Map of consumer key => managed-identity client_id (for the SA annotation)"
  value       = { for k, v in azurerm_user_assigned_identity.consumer : k => v.client_id }
}

output "principal_ids" {
  description = "Map of consumer key => managed-identity principal_id (for role assignments)"
  value       = { for k, v in azurerm_user_assigned_identity.consumer : k => v.principal_id }
}

output "ids" {
  description = "Map of consumer key => managed-identity resource ID"
  value       = { for k, v in azurerm_user_assigned_identity.consumer : k => v.id }
}
