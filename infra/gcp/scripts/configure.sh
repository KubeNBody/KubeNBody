#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Generate configs/gcp/session/, the deployment's values of the GKE overlay
# (infra/common/session.py writes it, git-ignored: no project IDs are
# committed):
#   - the app images from Artifact Registry
#   - Workload Identity of the ESO and pgBackRest ServiceAccounts
#   - the pgBackRest GCS bucket
#   - the ClusterSecretStore gcp-secret-manager (the project's Secret Manager)
#   - Grafana's LoadBalancer open to your public IP only (ADMIN_CIDR overrides it)
#
#   configure.sh             real values (terraform outputs)
#   configure.sh --example   dummy values, no GCP calls (offline render/checks)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GCP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_DIR="$GCP_DIR/environments/prod"

MODE=real
[ "${1:-}" = "--example" ] && MODE=example

if [ "$MODE" = example ]; then
    PROJECT=nbody-example
    REGISTRY=europe-west1-docker.pkg.dev/nbody-example/nbody-cluster-images
    ESO_SA=nbody-cluster-eso@nbody-example.iam.gserviceaccount.com
    PGBR_SA=nbody-cluster-pgbackrest@nbody-example.iam.gserviceaccount.com
    BUCKET=nbody-example-pgbackrest
    ADMIN_CIDR="${ADMIN_CIDR:-203.0.113.10/32}"   # TEST-NET-3, never routable
else
    tf_out() { (cd "$ENV_DIR" && terraform output -raw "$1" 2>/dev/null || true); }
    PROJECT="$(tf_out project_id)"
    REGISTRY="$(tf_out registry_url)"
    ESO_SA="$(tf_out eso_service_account)"
    PGBR_SA="$(tf_out pgbackrest_service_account)"
    BUCKET="$(tf_out pgbackrest_bucket)"
    for v in PROJECT REGISTRY ESO_SA PGBR_SA BUCKET; do
        if [ -z "${!v}" ]; then
            echo "Error: terraform output for $v is empty: run 'make init' and 'make apply'." >&2
            exit 1
        fi
    done
    if [ -z "${ADMIN_CIDR:-}" ]; then
        IP="$(curl -fsS --max-time 10 https://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]' || true)"
        [[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Error: could not determine your public IPv4 (set ADMIN_CIDR)" >&2; exit 1; }
        ADMIN_CIDR="$IP/32"
    fi
    [[ "$ADMIN_CIDR" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+$ ]] || { echo "Error: ADMIN_CIDR=$ADMIN_CIDR is not an IPv4 CIDR such as 203.0.113.10/32" >&2; exit 1; }
fi

MODE="$MODE" PROJECT="$PROJECT" REGISTRY="$REGISTRY" ESO_SA="$ESO_SA" PGBR_SA="$PGBR_SA" BUCKET="$BUCKET" \
ADMIN_CIDR="$ADMIN_CIDR" python3 - <<'PY' | python3 "$GCP_DIR/../common/session.py"
import json, os

e = os.environ


def workload_identity(name, namespace, gsa):
    return {"apiVersion": "v1", "kind": "ServiceAccount",
            "metadata": {"name": name, "namespace": namespace,
                         "annotations": {"iam.gke.io/gcp-service-account": gsa}}}


print(json.dumps({
    "cloud": "gcp",
    "mode": e["MODE"],
    "script": "infra/gcp/scripts/configure.sh",
    "images": [{"name": n, "newName": f"{e['REGISTRY']}/{n}", "app": True} for n in ("kubenbody", "kubenbody-gpu", "kubenbody-dashboard")],
    "patches": [
        {"target": {"kind": "ServiceAccount", "name": "external-secrets", "namespace": "external-secrets"},
         "patch": workload_identity("external-secrets", "external-secrets", e["ESO_SA"])},
        {"target": {"kind": "ServiceAccount", "name": "nbody-db-pgbackrest"},
         "patch": workload_identity("nbody-db-pgbackrest", "nbody", e["PGBR_SA"])},
        {"target": {"kind": "PostgresCluster", "name": "nbody-db"},
         "patch": [{"op": "replace", "path": "/spec/backups/pgbackrest/repos/0/gcs/bucket", "value": e["BUCKET"]}]},
        {"target": {"kind": "Service", "name": "lgtm"},
         "patch": [{"op": "add", "path": "/spec/loadBalancerSourceRanges", "value": [e["ADMIN_CIDR"]]}]},
    ],
    "secret_store": f"""apiVersion: external-secrets.io/v1beta1
kind: ClusterSecretStore
metadata:
  name: gcp-secret-manager
spec:
  provider:
    gcpsm:
      projectID: {e['PROJECT']}
""",
    "env": {"PROJECT_ID": e["PROJECT"], "REGISTRY": e["REGISTRY"], "PGBACKREST_BUCKET": e["BUCKET"],
            "ADMIN_CIDR": e["ADMIN_CIDR"]},
}))
PY
echo "Wrote configs/gcp/session/ ($MODE): project $PROJECT, registry $REGISTRY, Grafana open to $ADMIN_CIDR"
