# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# The application secrets every cloud's secret store holds, under the names the
# ExternalSecrets of configs/base/secrets read. Sourced by
# infra/<cloud>/scripts/init-secrets.sh, which defines set_secret NAME VALUE
# (write the value unless the secret already has one: live credentials are
# never rotated by a re-run).
#
# Every value is generated. NBODY_DEFAULT_USER_PASSWORD sets the dashboard
# user's password instead (at least 8 characters).

rand() { openssl rand -base64 24 | tr -d '/+=' | head -c 24; }

app_secrets() {
    set_secret nbody-redis-password          "$(rand)"
    set_secret nbody-minio-root-user         minioadmin
    set_secret nbody-minio-root-password     "$(rand)"
    set_secret nbody-rabbitmq-user           rabbitmq
    set_secret nbody-rabbitmq-password       "$(rand)"
    set_secret nbody-grafana-admin-password  "$(rand)"
    # the API's JWT signing key (the code's default is public, for local
    # development only) and the dashboard user's password
    set_secret nbody-jwt-secret-key          "$(openssl rand -base64 48 | tr -d '\n')"
    set_secret nbody-default-user-password   "${NBODY_DEFAULT_USER_PASSWORD:-$(rand)}"
}
