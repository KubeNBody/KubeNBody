# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Azure Container Registry (ACR) Module
# Docker image repository. AKS pulls via an AcrPull role assignment on the
# cluster's KUBELET identity (created in environments/prod/main.tf).

resource "azurerm_container_registry" "main" {
  name                = var.registry_name
  resource_group_name = var.resource_group_name
  location            = var.location
  sku                 = var.sku

  # No admin user: pulls use the kubelet managed identity (AcrPull). Pushes use
  # your `az acr login` identity (AcrPush). Keeps static creds out of the cluster.
  admin_enabled = false

  tags = var.tags
}
