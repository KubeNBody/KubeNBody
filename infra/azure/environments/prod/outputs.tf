# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Azure Production Environment Outputs

output "resource_group" {
  description = "Resource group name"
  value       = azurerm_resource_group.main.name
}

output "cluster_name" {
  description = "AKS cluster name"
  value       = module.aks.cluster_name
}

output "node_resource_group" {
  description = "Node resource group (MC_*)"
  value       = module.aks.node_resource_group
}

output "acr_login_server" {
  description = "ACR login server — set as ACR_REGISTRY for build-and-push.sh --azure"
  value       = module.acr.login_server
}

output "oidc_issuer_url" {
  description = "Cluster OIDC issuer URL (for Workload Identity)"
  value       = module.aks.oidc_issuer_url
}

output "kubelet_identity_object_id" {
  description = "Kubelet managed identity objectId (AcrPull target)"
  value       = module.aks.kubelet_identity_object_id
}

output "aks_connect_command" {
  description = "Command to get admin kubeconfig for the cluster"
  value       = "az aks get-credentials --resource-group ${azurerm_resource_group.main.name} --name ${module.aks.cluster_name}"
}

# Workload Identity / Key Vault (fill the configs/azure placeholders)
output "eso_client_id" {
  description = "ESO managed-identity client_id → operators/kustomization.yaml"
  value       = module.identity.client_ids["eso"]
}

output "workload_client_id" {
  description = "App workload managed-identity client_id (for nbody-workload SA, if used)"
  value       = module.identity.client_ids["workload"]
}

output "key_vault_uri" {
  description = "Key Vault URI (the ClusterSecretStore vaultUrl, written by make configure)"
  value       = module.keyvault.vault_uri
}

output "key_vault_name" {
  description = "Key Vault name (for scripts/init-secrets.sh)"
  value       = module.keyvault.vault_name
}

output "pgbackrest_storage_account" {
  description = "Storage account name for pgBackRest backups"
  value       = azurerm_storage_account.pgbackrest.name
}

output "pgbackrest_container" {
  description = "Blob container for pgBackRest backups"
  value       = azurerm_storage_container.pgbackrest.name
}
