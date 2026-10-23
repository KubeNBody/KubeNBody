# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Secrets Module
# Creates GCP Secret Manager secrets for the application.
# Secret VALUES are NOT managed by Terraform - only the secret resources.
# Values must be set manually using the init script or gcloud CLI.

# PostgreSQL credentials are managed by CrunchyData PGO
# No GCP secrets needed - PGO auto-generates and manages credentials

# Redis Secrets
resource "google_secret_manager_secret" "redis_password" {
  project   = var.project_id
  secret_id = "${var.secret_prefix}-redis-password"

  replication {
    auto {}
  }

  labels = var.labels
}

# MinIO Secrets
resource "google_secret_manager_secret" "minio_root_user" {
  project   = var.project_id
  secret_id = "${var.secret_prefix}-minio-root-user"

  replication {
    auto {}
  }

  labels = var.labels
}

resource "google_secret_manager_secret" "minio_root_password" {
  project   = var.project_id
  secret_id = "${var.secret_prefix}-minio-root-password"

  replication {
    auto {}
  }

  labels = var.labels
}

# RabbitMQ Secrets
resource "google_secret_manager_secret" "rabbitmq_user" {
  project   = var.project_id
  secret_id = "${var.secret_prefix}-rabbitmq-user"

  replication {
    auto {}
  }

  labels = var.labels
}

resource "google_secret_manager_secret" "rabbitmq_password" {
  project   = var.project_id
  secret_id = "${var.secret_prefix}-rabbitmq-password"

  replication {
    auto {}
  }

  labels = var.labels
}

# Grafana Secrets (Monitoring)
resource "google_secret_manager_secret" "grafana_admin_password" {
  project   = var.project_id
  secret_id = "${var.secret_prefix}-grafana-admin-password"

  replication {
    auto {}
  }

  labels = var.labels
}

# API Secrets: the JWT signing key and the dashboard user password (the API
# refuses to start in a cluster without them)
resource "google_secret_manager_secret" "jwt_secret_key" {
  project   = var.project_id
  secret_id = "${var.secret_prefix}-jwt-secret-key"

  replication {
    auto {}
  }

  labels = var.labels
}

resource "google_secret_manager_secret" "default_user_password" {
  project   = var.project_id
  secret_id = "${var.secret_prefix}-default-user-password"

  replication {
    auto {}
  }

  labels = var.labels
}

# IAM Bindings - Allow ESO service account to access secrets
resource "google_secret_manager_secret_iam_member" "eso_access" {
  for_each = toset([
    google_secret_manager_secret.redis_password.secret_id,
    google_secret_manager_secret.minio_root_user.secret_id,
    google_secret_manager_secret.minio_root_password.secret_id,
    google_secret_manager_secret.rabbitmq_user.secret_id,
    google_secret_manager_secret.rabbitmq_password.secret_id,
    google_secret_manager_secret.grafana_admin_password.secret_id,
    google_secret_manager_secret.jwt_secret_key.secret_id,
    google_secret_manager_secret.default_user_password.secret_id,
  ])

  project   = var.project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.eso_service_account_email}"
}
