# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Key Vault Module
# Key Vault in RBAC-authorization mode (current best practice). Secret
# VALUES are set out-of-band (scripts/init-secrets.sh), as on GCP.
#
# Role assignments (data-plane):
#  - ESO's managed identity  -> Key Vault Secrets User    (read)
#  - the Terraform/deployer   -> Key Vault Secrets Officer (so init-secrets can write)
#
# NOTE: role assignments can take ~minutes to propagate — `az keyvault
# secret set` right after apply may 403 until it settles.

data "azurerm_client_config" "current" {}

resource "azurerm_key_vault" "main" {
  name                = var.vault_name
  resource_group_name = var.resource_group_name
  location            = var.location
  tenant_id           = data.azurerm_client_config.current.tenant_id
  sku_name            = "standard"

  rbac_authorization_enabled = true
  purge_protection_enabled   = false
  soft_delete_retention_days = 7

  tags = var.tags
}

# ESO managed identity: read secrets
resource "azurerm_role_assignment" "eso_secrets_user" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = var.eso_principal_id
}

# Deployer (current az login / SP): manage secret values via init-secrets
resource "azurerm_role_assignment" "deployer_secrets_officer" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}
