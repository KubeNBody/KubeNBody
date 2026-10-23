# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Production Environment Outputs

# GKE Cluster
output "cluster_name" {
  description = "GKE cluster name"
  value       = module.gke.cluster_name
}

output "cluster_endpoint" {
  description = "GKE cluster endpoint"
  value       = module.gke.cluster_endpoint
  sensitive   = true
}

output "cluster_location" {
  description = "GKE cluster location"
  value       = module.gke.cluster_location
}

# Artifact Registry
output "registry_url" {
  description = "Artifact Registry URL for pushing/pulling images"
  value       = module.artifact_registry.repository_url
}

# Network
output "vpc_id" {
  description = "VPC network ID"
  value       = module.networking.vpc_id
}

output "subnet_id" {
  description = "Subnet ID"
  value       = module.networking.subnet_id
}

# Secrets
output "secret_ids" {
  description = "GCP Secret Manager secret IDs"
  value       = module.secrets.secret_ids
}

# pgBackRest (PGO Backups)
output "pgbackrest_bucket" {
  description = "GCS bucket for PostgreSQL backups"
  value       = google_storage_bucket.pgbackrest.name
}

output "eso_service_account" {
  description = "Service account email of the External Secrets Operator (Workload Identity)"
  value       = module.iam.eso_service_account_email
}

output "project_id" {
  description = "GCP project ID (the ClusterSecretStore's Secret Manager project)"
  value       = var.project_id
}

output "pgbackrest_service_account" {
  description = "Service account email for pgBackRest"
  value       = google_service_account.pgbackrest.email
}

# Connection Command
output "gke_connect_command" {
  description = "Command to connect to the GKE cluster"
  value       = "gcloud container clusters get-credentials ${module.gke.cluster_name} --region ${module.gke.cluster_location} --project ${var.project_id}"
}
