# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# AKS Module — User Node Pools

# CPU Worker Pool (on-demand, single-zone, manually scaled)
# Mirrors the GKE worker-cpu pool. Fixed count (default 0), scale via
# `az aks nodepool scale`.
#
# TAINT GOTCHA: a Spot pool ALSO gets an automatic AKS taint
#   kubernetes.azure.com/scalesetpriority=spot:NoSchedule
# in addition to our `dedicated=worker` taint, so worker pods must tolerate
# BOTH — the configs/azure overlay adds the extra `scalesetpriority=spot`
# toleration (the GKE pods only tolerate `dedicated=worker`).
resource "azurerm_kubernetes_cluster_node_pool" "workercpu" {
  name                  = "workercpu" # <=12 chars, lowercase alnum
  kubernetes_cluster_id = azurerm_kubernetes_cluster.main.id
  vm_size               = var.worker_cpu_vm_size
  node_count            = var.worker_cpu_node_count
  zones                 = [var.worker_zone]
  vnet_subnet_id        = var.node_subnet_id
  os_sku                = "Ubuntu"
  mode                  = "User"

  # On-demand (Regular): Spot capacity can be unreliable in a region (in
  # polandcentral, D-v7 Spot allocations stalled), and preemptions interrupt
  # jobs. For Spot, set:
  #   priority = "Spot"; eviction_policy = "Delete"; spot_max_price = -1
  priority = "Regular"

  node_taints = ["dedicated=worker:NoSchedule"]

  lifecycle {
    ignore_changes = [node_count]
  }
}

# GPU Pool (on-demand, single-zone, manually scaled)
# NC4as_T4_v3 = 1x NVIDIA T4 (same Turing GPU as the GKE T4 — image works as-is).
# On-demand (priority defaults to Regular), matching GKE gpu_spot=false.
#
# DRIVER/PLUGIN: AKS auto-installs the CUDA DRIVER on this supported SKU, but
# does NOT advertise `nvidia.com/gpu` until a device plugin is present. The
# NVIDIA device-plugin DaemonSet is added in the configs/azure overlay with a
# toleration matching the taint below.
#
# Ubuntu os_sku on purpose: Azure Linux GPU pools don't get automatic security
# patches.
resource "azurerm_kubernetes_cluster_node_pool" "gpu" {
  name                  = "gpu" # <=12 chars, lowercase alnum
  kubernetes_cluster_id = azurerm_kubernetes_cluster.main.id
  vm_size               = var.gpu_vm_size
  node_count            = var.gpu_node_count
  zones                 = [var.worker_zone]
  vnet_subnet_id        = var.node_subnet_id
  os_sku                = "Ubuntu"
  mode                  = "User"

  node_taints = ["nvidia.com/gpu=present:NoSchedule"]

  # AKS sets this by default on GPU SKUs (auto-installs the NVIDIA/CUDA driver).
  # Pin it so the live "Install" value doesn't read as drift ("Install" -> null),
  # which would otherwise force a pool replacement on every plan (azurerm 4.x:
  # gpu_driver is ForceNew).
  gpu_driver = "Install"

  upgrade_settings {
    max_surge = "1"
  }

  lifecycle {
    ignore_changes = [node_count]
  }
}
