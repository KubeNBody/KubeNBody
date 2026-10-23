# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
output "secret_ids" {
  description = "Map of secret names to their IDs"
  value = {
    # PostgreSQL credentials managed by CrunchyData PGO
    redis_password         = google_secret_manager_secret.redis_password.secret_id
    minio_root_user        = google_secret_manager_secret.minio_root_user.secret_id
    minio_root_password    = google_secret_manager_secret.minio_root_password.secret_id
    rabbitmq_user          = google_secret_manager_secret.rabbitmq_user.secret_id
    rabbitmq_password      = google_secret_manager_secret.rabbitmq_password.secret_id
    grafana_admin_password = google_secret_manager_secret.grafana_admin_password.secret_id
    jwt_secret_key         = google_secret_manager_secret.jwt_secret_key.secret_id
    default_user_password  = google_secret_manager_secret.default_user_password.secret_id
  }
}

output "secret_names" {
  description = "List of all secret names (for use in init script)"
  value = [
    google_secret_manager_secret.redis_password.secret_id,
    google_secret_manager_secret.minio_root_user.secret_id,
    google_secret_manager_secret.minio_root_password.secret_id,
    google_secret_manager_secret.rabbitmq_user.secret_id,
    google_secret_manager_secret.rabbitmq_password.secret_id,
    google_secret_manager_secret.grafana_admin_password.secret_id,
    google_secret_manager_secret.jwt_secret_key.secret_id,
    google_secret_manager_secret.default_user_password.secret_id,
  ]
}
