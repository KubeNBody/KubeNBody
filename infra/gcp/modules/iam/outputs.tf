# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
output "gke_node_service_account_email" {
  description = "Email of the GKE nodes service account"
  value       = google_service_account.gke_nodes.email
}

output "gke_node_service_account_id" {
  description = "ID of the GKE nodes service account"
  value       = google_service_account.gke_nodes.id
}

output "workload_service_account_email" {
  description = "Email of the workload service account"
  value       = google_service_account.workload.email
}

output "workload_service_account_id" {
  description = "ID of the workload service account"
  value       = google_service_account.workload.id
}

output "eso_service_account_email" {
  description = "Email of the External Secrets Operator service account"
  value       = google_service_account.external_secrets.email
}

output "eso_service_account_id" {
  description = "ID of the External Secrets Operator service account"
  value       = google_service_account.external_secrets.id
}
