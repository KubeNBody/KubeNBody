#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Initialize Azure Key Vault secret values (run ONCE after `make apply`)
# The app secrets of infra/common/app-secrets.sh, plus
# the pgBackRest storage-account account/key (read from the backup storage
# account). ESO then syncs them into the cluster.
#
# Config comes from terraform outputs (preferred) or env overrides:
#   KEY_VAULT_NAME, PGBACKREST_STORAGE_ACCOUNT, RESOURCE_GROUP
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_DIR="$SCRIPT_DIR/../environments/prod"

tf_out() { (cd "$ENV_DIR" && terraform output -raw "$1" 2>/dev/null || true); }

command -v az >/dev/null || { echo -e "${RED}az CLI not found${NC}"; exit 1; }

KEY_VAULT_NAME="${KEY_VAULT_NAME:-$(tf_out key_vault_name)}"
PGBACKREST_STORAGE_ACCOUNT="${PGBACKREST_STORAGE_ACCOUNT:-$(tf_out pgbackrest_storage_account)}"
RESOURCE_GROUP="${RESOURCE_GROUP:-$(tf_out resource_group)}"

if [ -z "$KEY_VAULT_NAME" ]; then
    echo -e "${RED}KEY_VAULT_NAME not set and not found in terraform output.${NC}"
    echo "Set KEY_VAULT_NAME (and PGBACKREST_STORAGE_ACCOUNT, RESOURCE_GROUP) or run after 'make apply'."
    exit 1
fi

echo -e "${GREEN}Key Vault: ${KEY_VAULT_NAME}${NC}"
echo ""

# Set a KV secret only if it doesn't already exist (avoid rotating live creds).
# Retries on failure to ride out RBAC role propagation after `terraform apply`
# (the "Secrets Officer" assignment can take a few minutes).
set_secret() {
    local name="$1" value="$2"
    if az keyvault secret show --vault-name "$KEY_VAULT_NAME" --name "$name" >/dev/null 2>&1; then
        echo -e "${YELLOW}  $name already set — skipping${NC}"
        return
    fi
    local attempt
    for attempt in 1 2 3 4 5 6; do
        if az keyvault secret set --vault-name "$KEY_VAULT_NAME" --name "$name" --value "$value" >/dev/null 2>&1; then
            echo -e "${GREEN}  ✓ $name${NC}"
            return
        fi
        echo -e "${YELLOW}  $name: write failed (attempt $attempt) — likely RBAC propagation, retrying in 20s...${NC}"
        sleep 20
    done
    echo -e "${RED}  ✗ $name: gave up after retries. Check you have 'Key Vault Secrets Officer' on ${KEY_VAULT_NAME}.${NC}"
    return 1
}

echo "--- Application secrets ---"
# shellcheck source=../../common/app-secrets.sh
. "$SCRIPT_DIR/../../common/app-secrets.sh"
app_secrets

echo "--- pgBackRest (Azure Blob shared key) ---"
if [ -n "$PGBACKREST_STORAGE_ACCOUNT" ] && [ -n "$RESOURCE_GROUP" ]; then
    PG_KEY="$(az storage account keys list \
        --account-name "$PGBACKREST_STORAGE_ACCOUNT" \
        --resource-group "$RESOURCE_GROUP" \
        --query "[0].value" -o tsv)"
    set_secret "nbody-pgbackrest-account" "$PGBACKREST_STORAGE_ACCOUNT"
    set_secret "nbody-pgbackrest-key"     "$PG_KEY"
else
    echo -e "${YELLOW}  PGBACKREST_STORAGE_ACCOUNT/RESOURCE_GROUP unset — skipping pgBackRest creds${NC}"
fi

echo ""
echo -e "${GREEN}Done. ESO will sync these into the cluster (ClusterSecretStore azure-kv).${NC}"
echo "The dashboard password: az keyvault secret show --vault-name $KEY_VAULT_NAME --name nbody-default-user-password --query value -o tsv"
