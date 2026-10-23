#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Deploy a configs/<cloud> overlay, in the stages the operators need:
#   1. base (namespaces, storage classes)
#   2. operators (External Secrets, PGO), then wait for their CRDs and ESO
#   3. the session's ClusterSecretStore (needs the ESO CRDs)
#   4. everything
# Every call goes to CONTEXT, never the current kubectl context. The manifests
# win over changes made by hand: what was scaled with kubectl scale (workers,
# Grafana) goes back to the manifests' replicas.
#
#   deploy-k8s.sh OVERLAY_DIR CONTEXT
set -euo pipefail

OVERLAY="${1:?usage: deploy-k8s.sh OVERLAY_DIR CONTEXT}"
CONTEXT="${2:?usage: deploy-k8s.sh OVERLAY_DIR CONTEXT}"
SESSION="$OVERLAY/session"
KUBECTL=(kubectl --context "$CONTEXT")

if [ "$(sed -n 's/^MODE=//p' "$SESSION/session.env" 2>/dev/null)" != real ]; then
    echo "Error: $SESSION is missing or holds example values: run 'make configure'." >&2
    exit 1
fi
# FORMAT in infra/common/session.py: a session from older configure scripts lacks values the deploy needs
if [ "$(sed -n 's/^FORMAT=//p' "$SESSION/session.env")" != "$(sed -n 's/^FORMAT = //p' "$(dirname "$0")/session.py")" ]; then
    echo "Error: $SESSION was written by an older 'make configure': run it again." >&2
    exit 1
fi

apply() { kustomize build --enable-helm "$1" | "${KUBECTL[@]}" apply --server-side --force-conflicts -f -; }

echo "== Step 1: base (namespaces, storage classes) =="
apply "$OVERLAY/base"

echo "== Step 2: operators (ESO, PGO) =="
apply "$OVERLAY/stages/operators"
echo "Waiting for the CRDs and the ESO deployments..."
for crd in clustersecretstores.external-secrets.io externalsecrets.external-secrets.io \
           postgresclusters.postgres-operator.crunchydata.com; do
    "${KUBECTL[@]}" wait --for=condition=Established "crd/$crd" --timeout=180s
done
for d in external-secrets external-secrets-webhook external-secrets-cert-controller; do
    "${KUBECTL[@]}" wait --for=condition=Available "deployment/$d" -n external-secrets --timeout=300s
done

echo "== Step 3: ClusterSecretStore =="
"${KUBECTL[@]}" apply --server-side --force-conflicts -f "$SESSION/cluster-secret-store.yml"

echo "== Step 4: everything =="
apply "$OVERLAY"
# Earlier deployments had the Redis chart's own NetworkPolicy, which admitted
# every pod on the Redis ports. Apply does not remove what the manifests dropped.
"${KUBECTL[@]}" -n nbody delete networkpolicy redis-ha --ignore-not-found
