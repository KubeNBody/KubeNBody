# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# GKE Module
# Creates the GKE cluster. Node pools are defined in node_pools.tf

resource "google_container_cluster" "main" {
  name     = var.cluster_name
  project  = var.project_id
  location = var.region # Regional cluster for HA

  # `make destroy` deletes the cluster (the provider's default refuses to)
  deletion_protection = false

  # We manage node pools separately
  remove_default_node_pool = true
  initial_node_count       = 1

  # Network configuration
  network    = var.vpc_id
  subnetwork = var.subnet_id

  # VPC-native cluster (required for most features)
  ip_allocation_policy {
    cluster_secondary_range_name  = var.pods_range_name
    services_secondary_range_name = var.services_range_name
  }

  # PUBLIC nodes (no private cluster config = nodes get public IPs)
  # This saves ~$32/mo by not needing Cloud NAT

  # Workload Identity (secure way for pods to access GCP services)
  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  # Release channel for automatic upgrades
  release_channel {
    channel = "REGULAR"
  }

  # Cluster addons
  addons_config {
    http_load_balancing {
      disabled = false
    }

    horizontal_pod_autoscaling {
      disabled = false
    }

    # Required for persistent volumes
    gce_persistent_disk_csi_driver_config {
      enabled = true
    }

    # Network policy (Calico)
    network_policy_config {
      disabled = false
    }
  }

  # Enable network policy
  network_policy {
    enabled  = true
    provider = "CALICO"
  }

  # Logging and monitoring.
  # SYSTEM_COMPONENTS only: keep low-volume cluster/node diagnostics in GCP,
  # but drop the expensive WORKLOADS firehose (container stdout/stderr) — the
  # self-hosted LGTM stack + Alloy already collect app logs into Loki, so
  # Cloud Logging of workloads would be redundant double-spend.
  # To re-enable app logs in GCP: add "WORKLOADS" to logging enable_components.
  # To disable entirely (max savings, no GCP-native diagnostics): set [].
  logging_config {
    enable_components = ["SYSTEM_COMPONENTS"]
  }
  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS"]
  }

  # Resource labels
  resource_labels = var.labels

  # Ignore changes to node config (we manage node pools separately)
  lifecycle {
    ignore_changes = [
      node_config,
      initial_node_count,
    ]
  }
}
