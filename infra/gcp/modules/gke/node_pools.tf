# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# GKE Node Pools

# CPU Node Pool
# For general workloads: PostgreSQL, Redis, MinIO, RabbitMQ, API, Dashboard, etc.
resource "google_container_node_pool" "cpu" {
  name     = "cpu-pool"
  project  = var.project_id
  location = var.region
  cluster  = google_container_cluster.main.name

  # Fixed node count (no autoscaling)
  node_count = var.cpu_node_count

  # Node configuration
  node_config {
    machine_type = var.cpu_machine_type
    disk_size_gb = var.cpu_disk_size_gb
    disk_type    = "pd-balanced"

    # Service account for nodes
    service_account = var.node_service_account
    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform"
    ]

    # Workload Identity
    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    # Labels
    labels = merge(var.labels, {
      workload = "cpu"
    })

    # Tags for firewall rules
    tags = ["gke-${var.cluster_name}", "cpu-pool"]
  }

  # Management
  management {
    auto_repair  = true
    auto_upgrade = true
  }

  # Upgrade settings
  upgrade_settings {
    max_surge       = 1
    max_unavailable = 0
  }
}

# CPU Worker Node Pool
# Dedicated Spot pool for compute-bound CPU workers. Single zone, tainted, and
# manually scaled (fixed node_count, default 0 — resize via `make scale-worker-cpu`).
resource "google_container_node_pool" "worker_cpu" {
  name     = "worker-cpu"
  project  = var.project_id
  location = var.region
  cluster  = google_container_cluster.main.name

  # Single-zone pool inside the regional cluster
  node_locations = [var.worker_zone]

  # Fixed count, manually scaled (no autoscaling)
  node_count = var.worker_cpu_node_count

  node_config {
    machine_type = var.worker_cpu_machine_type
    disk_size_gb = var.cpu_disk_size_gb
    disk_type    = "pd-balanced"
    spot         = true

    service_account = var.node_service_account
    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform"
    ]

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    labels = merge(var.labels, {
      workload = "worker-cpu"
    })

    # Only workloads that tolerate this taint schedule here, keeping base
    # services (no toleration) on the default cpu-pool.
    taint {
      key    = "dedicated"
      value  = "worker"
      effect = "NO_SCHEDULE"
    }

    tags = ["gke-${var.cluster_name}", "worker-cpu-pool"]
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  upgrade_settings {
    max_surge       = 1
    max_unavailable = 0
  }
}

# GPU Node Pool
# For GPU workers running physics simulations with NVIDIA T4
resource "google_container_node_pool" "gpu" {
  name     = "gpu-pool"
  project  = var.project_id
  location = var.region
  cluster  = google_container_cluster.main.name

  # Single-zone pool inside the regional cluster. Without this, the pool spans
  # all 3 region zones and node_count is per-zone — so node_count=1 spins up 3
  # T4 VMs (3x cost) and scaling only moves in bumps of 3. Pinning to one zone
  # makes node_count the true total.
  node_locations = [var.worker_zone]

  # Fixed node count (no autoscaling)
  node_count = var.gpu_node_count

  # Node configuration
  node_config {
    machine_type = var.gpu_machine_type # Must be N1 series for T4
    disk_size_gb = var.gpu_disk_size_gb
    disk_type    = "pd-balanced"
    # On-demand by default (gpu_spot=false): Spot T4 in this region is
    # preempted too often to keep GPU workers alive. Set gpu_spot=true to
    # trade stability for cost (preempted jobs resume from snapshot via watchdog).
    # NOTE: `spot` is immutable — flipping it recreates the pool.
    spot = var.gpu_spot

    # GPU configuration
    guest_accelerator {
      type  = "nvidia-tesla-t4"
      count = 1
      gpu_driver_installation_config {
        gpu_driver_version = "LATEST"
      }
    }

    # Service account for nodes
    service_account = var.node_service_account
    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform"
    ]

    # Workload Identity
    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    # Labels
    labels = merge(var.labels, {
      workload                           = "gpu"
      "cloud.google.com/gke-accelerator" = "nvidia-tesla-t4"
    })

    # Taint GPU nodes so only GPU workloads schedule on them
    taint {
      key    = "nvidia.com/gpu"
      value  = "present"
      effect = "NO_SCHEDULE"
    }

    # Tags for firewall rules
    tags = ["gke-${var.cluster_name}", "gpu-pool"]
  }

  # Management
  management {
    auto_repair  = true
    auto_upgrade = true
  }

  # Upgrade settings
  upgrade_settings {
    max_surge       = 1
    max_unavailable = 0
  }
}
