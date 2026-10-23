# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# AKS Module — Cluster
# Regional cluster (system pool spread across zones, like the GKE cpu-pool).
# Worker + GPU pools are single-zone and defined in node_pools.tf.
#
# Baked-in decisions:
#  - CNI Overlay (pod IPs off-VNet), Standard LB, NO network-policy engine.
#  - Workload Identity: oidc_issuer_enabled + workload_identity_enabled (webhook
#    is auto-installed by AKS — do NOT Helm-install it).
#  - Local admin accounts (no Entra/kubelogin).
#  - System pool is NOT tainted (only_critical_addons_enabled = false) so base
#    services (Postgres/Redis/MinIO/RabbitMQ) schedule on it, like GKE cpu-pool.

resource "azurerm_kubernetes_cluster" "main" {
  name                = var.cluster_name
  location            = var.location
  resource_group_name = var.resource_group_name
  dns_prefix          = var.cluster_name
  kubernetes_version  = var.kubernetes_version # null => AKS default

  # v4 argument names (renamed from v3's automatic_channel_upgrade / node_os_channel_upgrade).
  # No automatic upgrades: a node drain interrupts long jobs and their timing.
  # Upgrade the cluster and the node images by hand, between jobs.
  automatic_upgrade_channel = null # none
  node_os_upgrade_channel   = "None"

  # Local admin kubeconfig (via `az aks get-credentials`). No Entra RBAC.
  local_account_disabled = false

  oidc_issuer_enabled       = true
  workload_identity_enabled = true

  identity {
    type = "SystemAssigned"
  }

  default_node_pool {
    name                 = "system" # AKS Linux pool names: <=12 chars, lowercase alnum
    vm_size              = var.system_vm_size
    node_count           = var.system_node_count
    zones                = var.cluster_zones # regional: [1,2,3]
    vnet_subnet_id       = var.node_subnet_id
    os_sku               = "Ubuntu"
    orchestrator_version = var.kubernetes_version

    # Allow ForceNew field changes (e.g. vm_size) to rotate in-place instead of
    # recreating the whole cluster.
    temporary_name_for_rotation = "systmp"

    upgrade_settings {
      max_surge = "1"
    }
  }

  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    load_balancer_sku   = "standard"
    pod_cidr            = var.pod_cidr
    service_cidr        = var.service_cidr
    dns_service_ip      = var.dns_service_ip
    # network_policy intentionally omitted — no policy engine.
  }

  tags = var.tags

  lifecycle {
    # Node count is scaled manually (`az aks nodepool scale`). Don't let TF fight it.
    ignore_changes = [default_node_pool[0].node_count]
  }
}
