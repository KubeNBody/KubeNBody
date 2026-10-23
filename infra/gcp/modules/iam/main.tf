# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# IAM Module
# Creates service accounts and IAM bindings for:
# 1. GKE nodes (minimal permissions)
# 2. Application workloads (Workload Identity)
# 3. External Secrets Operator (Secret Manager access)

# GKE Node Service Account
resource "google_service_account" "gke_nodes" {
  account_id   = "${var.name_prefix}-gke-nodes"
  project      = var.project_id
  display_name = "GKE Nodes Service Account"
  description  = "Service account for GKE nodes with minimal permissions"
}

# Node SA: Write logs
resource "google_project_iam_member" "nodes_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

# Node SA: Write metrics
resource "google_project_iam_member" "nodes_metric_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

# Node SA: View monitoring
resource "google_project_iam_member" "nodes_monitoring_viewer" {
  project = var.project_id
  role    = "roles/monitoring.viewer"
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

# Node SA: Write resource metadata
resource "google_project_iam_member" "nodes_resource_metadata" {
  project = var.project_id
  role    = "roles/stackdriver.resourceMetadata.writer"
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

# Node SA: Pull images from Artifact Registry
resource "google_project_iam_member" "nodes_artifact_registry" {
  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

# Application Workload Service Account (for Workload Identity)
resource "google_service_account" "workload" {
  account_id   = "${var.name_prefix}-workload"
  project      = var.project_id
  display_name = "N-Body Workload Service Account"
  description  = "Service account for application workloads via Workload Identity"
}

# Its Workload Identity binding is in environments/prod/main.tf, which can
# depend on the GKE cluster.

# External Secrets Operator Service Account
resource "google_service_account" "external_secrets" {
  account_id   = "${var.name_prefix}-eso"
  project      = var.project_id
  display_name = "External Secrets Operator Service Account"
  description  = "Service account for ESO to access Secret Manager"
}

# ESO SA: Access secrets from Secret Manager
resource "google_project_iam_member" "eso_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.external_secrets.email}"
}

# Its Workload Identity binding is in environments/prod/main.tf, which can
# depend on the GKE cluster.
