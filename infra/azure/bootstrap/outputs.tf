# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Azure Bootstrap Outputs
# Feed these into `make init` (-backend-config) for the prod environment.

output "state_resource_group_name" {
  description = "Resource group holding the state storage account"
  value       = azurerm_resource_group.tfstate.name
}

output "state_storage_account_name" {
  description = "Storage account name for the azurerm backend"
  value       = azurerm_storage_account.tfstate.name
}

output "state_container_name" {
  description = "Blob container name for the azurerm backend"
  value       = azurerm_storage_container.tfstate.name
}

output "backend_config_hint" {
  description = "backend-config args for `terraform init` in environments/prod"
  value = join(" ", [
    "-backend-config=\"resource_group_name=${azurerm_resource_group.tfstate.name}\"",
    "-backend-config=\"storage_account_name=${azurerm_storage_account.tfstate.name}\"",
    "-backend-config=\"container_name=${azurerm_storage_container.tfstate.name}\"",
    "-backend-config=\"key=prod.tfstate\"",
  ])
}
