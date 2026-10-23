# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# ACR Module Outputs

output "id" {
  description = "ACR resource ID (used for the AcrPull role assignment scope)"
  value       = azurerm_container_registry.main.id
}

output "login_server" {
  description = "ACR login server (e.g. nbodyacr.azurecr.io) — use as the image registry host"
  value       = azurerm_container_registry.main.login_server
}

output "name" {
  description = "ACR name"
  value       = azurerm_container_registry.main.name
}
