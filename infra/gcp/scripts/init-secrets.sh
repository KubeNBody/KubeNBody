#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Set the application secrets in GCP Secret Manager (run after `make apply`,
# which creates the secrets). A secret that already has a version is left
# alone. ESO then syncs them into the cluster (ClusterSecretStore
# gcp-secret-manager).
#
# PostgreSQL needs none: PGO generates its credentials
# (kubectl get secret nbody-db-pguser-nbody -n nbody).
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

command -v gcloud >/dev/null || { echo -e "${RED}gcloud CLI not found${NC}"; exit 1; }
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
[ -n "$PROJECT_ID" ] || { echo -e "${RED}PROJECT_ID not set and no gcloud project configured${NC}"; exit 1; }
echo -e "${GREEN}Secret Manager: project ${PROJECT_ID}${NC}"

set_secret() {
    local name="$1" value="$2"
    if [ -n "$(gcloud secrets versions list "$name" --project="$PROJECT_ID" --format='value(name)' 2>/dev/null | head -1)" ]; then
        echo -e "${YELLOW}  $name already set, skipping${NC}"
        return
    fi
    printf '%s' "$value" | gcloud secrets versions add "$name" --project="$PROJECT_ID" --data-file=- >/dev/null
    echo -e "${GREEN}  ✓ $name${NC}"
}

# shellcheck source=../../common/app-secrets.sh
. "$SCRIPT_DIR/../../common/app-secrets.sh"
app_secrets

echo ""
echo -e "${GREEN}Done. The dashboard password: gcloud secrets versions access latest --secret nbody-default-user-password --project $PROJECT_ID${NC}"
