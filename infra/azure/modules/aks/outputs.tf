# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# AKS Module Outputs

output "cluster_name" {
  description = "AKS cluster name"
  value       = azurerm_kubernetes_cluster.main.name
}

output "cluster_id" {
  description = "AKS cluster resource ID"
  value       = azurerm_kubernetes_cluster.main.id
}

output "node_resource_group" {
  description = "Auto-created MC_* resource group holding the node VMSS/NSG/LB"
  value       = azurerm_kubernetes_cluster.main.node_resource_group
}

output "oidc_issuer_url" {
  description = "OIDC issuer URL — the `issuer` for federated identity credentials"
  value       = azurerm_kubernetes_cluster.main.oidc_issuer_url
}

output "kubelet_identity_object_id" {
  description = "Kubelet managed identity objectId — assign AcrPull to THIS"
  value       = azurerm_kubernetes_cluster.main.kubelet_identity[0].object_id
}

output "kube_config_raw" {
  description = "Raw kubeconfig (admin)"
  value       = azurerm_kubernetes_cluster.main.kube_config_raw
  sensitive   = true
}
