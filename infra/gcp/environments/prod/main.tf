# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Production Environment
# Main configuration that ties together all modules for the prod environment

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }

  backend "gcs" {
    # Bucket name is set via -backend-config during init
    prefix = "terraform/prod"
  }
}

# Providers
provider "google" {
  project = var.project_id
  region  = var.region
}


# Networking Module
module "networking" {
  source = "../../modules/networking"

  project_id  = var.project_id
  region      = var.region
  name_prefix = var.cluster_name

  # IP ranges
  subnet_cidr   = var.subnet_cidr
  pods_cidr     = var.pods_cidr
  services_cidr = var.services_cidr
}

# IAM Module
module "iam" {
  source = "../../modules/iam"

  project_id    = var.project_id
  name_prefix   = var.cluster_name
  k8s_namespace = var.app_namespace
}

# Artifact Registry Module
module "artifact_registry" {
  source = "../../modules/artifact-registry"

  project_id    = var.project_id
  region        = var.region
  repository_id = "${var.cluster_name}-images"

  labels = local.labels
}

# GKE Module
module "gke" {
  source = "../../modules/gke"

  project_id   = var.project_id
  region       = var.region
  cluster_name = var.cluster_name

  # Network configuration
  vpc_id              = module.networking.vpc_id
  subnet_id           = module.networking.subnet_id
  pods_range_name     = module.networking.pods_range_name
  services_range_name = module.networking.services_range_name

  # Node configuration
  node_service_account = module.iam.gke_node_service_account_email

  # CPU node pool
  cpu_machine_type = var.cpu_machine_type
  cpu_node_count   = var.cpu_node_count
  cpu_disk_size_gb = var.cpu_disk_size_gb

  # CPU worker node pool (Spot, manually scaled)
  worker_zone             = coalesce(var.worker_zone, "${var.region}-b")
  worker_cpu_machine_type = var.worker_cpu_machine_type
  worker_cpu_node_count   = var.worker_cpu_node_count

  # GPU node pool
  gpu_machine_type = var.gpu_machine_type
  gpu_node_count   = var.gpu_node_count
  gpu_disk_size_gb = var.gpu_disk_size_gb
  gpu_spot         = var.gpu_spot

  labels = local.labels

  depends_on = [module.networking, module.iam]
}

# Secrets Module
module "secrets" {
  source = "../../modules/secrets"

  project_id    = var.project_id
  secret_prefix = var.secret_prefix

  eso_service_account_email = module.iam.eso_service_account_email

  labels = local.labels

  depends_on = [module.iam]
}

# pgBackRest Backup Bucket (for PGO)
resource "google_storage_bucket" "pgbackrest" {
  name     = "${var.project_id}-pgbackrest"
  location = var.region
  project  = var.project_id

  uniform_bucket_level_access = true

  # No lifecycle rule: pgBackRest expires its own backups (repo1-retention-full),
  # and an age-based delete would eventually remove files it writes once and
  # never rewrites (archive.info), which breaks WAL archiving.

  labels = local.labels
}

# pgBackRest Service Account (for GCS backup access via Workload Identity)
resource "google_service_account" "pgbackrest" {
  account_id   = "${var.cluster_name}-pgbackrest"
  display_name = "pgBackRest Backup Service Account"
  description  = "Service account for PGO pgBackRest to access GCS bucket"
  project      = var.project_id
}

# pgBackRest SA: Access to backup bucket
resource "google_storage_bucket_iam_member" "pgbackrest_bucket_access" {
  bucket = google_storage_bucket.pgbackrest.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.pgbackrest.email}"
}

# Workload Identity binding for pgBackRest
# PGO creates a SA named {cluster}-pgbackrest in the same namespace
# NOTE: Requires GKE cluster to exist (creates the Workload Identity pool)
resource "google_service_account_iam_member" "pgbackrest_workload_identity" {
  service_account_id = google_service_account.pgbackrest.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.app_namespace}/nbody-db-pgbackrest]"

  depends_on = [module.gke]
}

# Workload Identity binding for application workloads
# NOTE: Requires GKE cluster to exist (creates the Workload Identity pool)
resource "google_service_account_iam_member" "workload_identity_binding" {
  service_account_id = module.iam.workload_service_account_id
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.app_namespace}/nbody-workload]"

  depends_on = [module.gke]
}

# Workload Identity binding for External Secrets Operator
# NOTE: Requires GKE cluster to exist (creates the Workload Identity pool)
resource "google_service_account_iam_member" "eso_workload_identity_binding" {
  service_account_id = module.iam.eso_service_account_id
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[external-secrets/external-secrets]"

  depends_on = [module.gke]
}

# Local Values
locals {
  labels = {
    environment = "prod"
    project     = "nbody-simulator"
    managed-by  = "terraform"
  }
}
