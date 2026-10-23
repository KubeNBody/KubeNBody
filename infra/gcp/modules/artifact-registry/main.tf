# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Artifact Registry Module
# Creates a Docker repository for container images.

resource "google_artifact_registry_repository" "docker" {
  location      = var.region
  project       = var.project_id
  repository_id = var.repository_id
  description   = var.description
  format        = "DOCKER"

  # Cleanup policy to manage storage costs
  cleanup_policies {
    id     = "keep-recent-versions"
    action = "KEEP"
    most_recent_versions {
      keep_count = var.keep_count
    }
  }

  labels = var.labels
}
