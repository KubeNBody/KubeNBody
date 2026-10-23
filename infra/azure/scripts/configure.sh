#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Generate configs/azure/session/, the deployment's values of the Azure overlay
# (infra/common/session.py writes it, git-ignored: no IDs are committed):
#   - the app images from ACR
#   - the ESO ServiceAccount's Workload Identity (client-id of the ESO managed
#     identity, tenant)
#   - the ClusterSecretStore azure-kv (Key Vault URL, tenant). The ESO identity
#     is federated with external-secrets/external-secrets, and ESO mints its
#     token through serviceAccountRef (the controller pod needs no
#     azure.workload.identity/use label in that mode).
#
#   configure.sh             real values (terraform outputs, az account)
#   configure.sh --example   dummy values, no Azure calls (offline render/checks)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AZURE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_DIR="$AZURE_DIR/environments/prod"

MODE=real
[ "${1:-}" = "--example" ] && MODE=example

if [ "$MODE" = example ]; then
    ACR=nbodyexample.azurecr.io
    ESO_CID=00000000-0000-0000-0000-000000000001
    TENANT=00000000-0000-0000-0000-000000000002
    KV_URI=https://nbody-kv-example.vault.azure.net
else
    tf_out() { (cd "$ENV_DIR" && terraform output -raw "$1" 2>/dev/null || true); }
    ACR="$(tf_out acr_login_server)"
    ESO_CID="$(tf_out eso_client_id)"
    KV_URI="$(tf_out key_vault_uri)"
    TENANT="$(az account show --query tenantId -o tsv 2>/dev/null || true)"
    for v in ACR ESO_CID KV_URI TENANT; do
        if [ -z "${!v}" ]; then
            echo "Error: $v is empty: run 'make init' (terraform outputs) and 'az login'." >&2
            exit 1
        fi
    done
fi

MODE="$MODE" ACR="$ACR" ESO_CID="$ESO_CID" TENANT="$TENANT" KV_URI="${KV_URI%/}" python3 - <<'PY' \
    | python3 "$AZURE_DIR/../common/session.py"
import json, os

e = os.environ
print(json.dumps({
    "cloud": "azure",
    "mode": e["MODE"],
    "script": "infra/azure/scripts/configure.sh",
    "images": [{"name": n, "newName": f"{e['ACR']}/{n}", "app": True} for n in ("kubenbody", "kubenbody-gpu", "kubenbody-dashboard")],
    "patches": [
        {"target": {"kind": "ServiceAccount", "name": "external-secrets", "namespace": "external-secrets"},
         "patch": {"apiVersion": "v1", "kind": "ServiceAccount",
                   "metadata": {"name": "external-secrets", "namespace": "external-secrets",
                                "annotations": {"$patch": "replace",
                                                "azure.workload.identity/client-id": e["ESO_CID"],
                                                "azure.workload.identity/tenant-id": e["TENANT"]}}}},
    ],
    "secret_store": f"""apiVersion: external-secrets.io/v1beta1
kind: ClusterSecretStore
metadata:
  name: azure-kv
spec:
  provider:
    azurekv:
      authType: WorkloadIdentity
      vaultUrl: "{e['KV_URI']}"
      tenantId: "{e['TENANT']}"
      serviceAccountRef:
        name: external-secrets
        namespace: external-secrets
""",
    "env": {"ACR_REGISTRY": e["ACR"], "KEY_VAULT_URI": e["KV_URI"]},
}))
PY
echo "Wrote configs/azure/session/ ($MODE): ACR $ACR, Key Vault $KV_URI"
