# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Key Vault Module Outputs

output "vault_uri" {
  description = "Key Vault URI (vaultUrl for the ESO ClusterSecretStore)"
  value       = azurerm_key_vault.main.vault_uri
}

output "vault_name" {
  description = "Key Vault name"
  value       = azurerm_key_vault.main.name
}

output "vault_id" {
  description = "Key Vault resource ID"
  value       = azurerm_key_vault.main.id
}
