#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# MinIO <-> S3 backup/restore for the ephemeral EKS cluster
#   minio-sync.sh backup    every MinIO bucket -> s3://<backup>/minio/<bucket>/
#   minio-sync.sh restore   s3://<backup>/minio/<bucket>/ -> MinIO (creates buckets)
#
# MinIO lives on EBS and dies with the cluster at `make down`. This copies its
# objects through a local staging directory with the aws CLI only (no mc):
#   - MinIO side: kubectl port-forward to svc/minio-svc, credentials from the
#     in-cluster minio-secrets Secret, path-style addressing, and AWS_PROFILE
#     UNSET so another account's profile can never be mixed in.
#   - S3 side: the deploy profile (NBODY_AWS_PROFILE, default nbody).
# backup does NOT use --delete: the S3 copy is a superset of every session's
# MinIO. With --delete, a session whose restore partly failed would erase good
# objects from the backup at the next `make down`.
#
# Reads REGION / BACKUP_BUCKET from configs/aws/session/session.env and the
# kube context from KUBE_CONTEXT (default nbody-eks).
set -euo pipefail

# Never the shell's AWS_PROFILE (it may belong to another account):
# the deploy profile, also when this script is run outside the Makefile.
export AWS_PROFILE="${NBODY_AWS_PROFILE:-nbody}"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SESSION_ENV="$ROOT_DIR/configs/aws/session/session.env"

ACTION="${1:-}"
case "$ACTION" in backup|restore) ;; *) echo "usage: $0 backup|restore"; exit 2 ;; esac

[ -f "$SESSION_ENV" ] || { echo -e "${RED}$SESSION_ENV missing — run make configure${NC}"; exit 1; }
# shellcheck disable=SC1090
. "$SESSION_ENV"
[ "${MODE:-}" = real ] || { echo -e "${RED}session.env holds example values — run make configure${NC}"; exit 1; }

CTX="${KUBE_CONTEXT:-nbody-eks}"
NS=nbody
LOCAL_PORT="${MINIO_LOCAL_PORT:-19000}"
S3_PREFIX="s3://$BACKUP_BUCKET/minio"

STAGE="$(mktemp -d)"
MINIO_CFG="$(mktemp)"
PF_PID=""
cleanup() {
    [ -n "$PF_PID" ] && kill "$PF_PID" 2>/dev/null || true
    rm -rf "$STAGE" "$MINIO_CFG"
}
trap cleanup EXIT

# --- MinIO access (never the AWS profile) ---
MINIO_USER="$(kubectl --context "$CTX" -n "$NS" get secret minio-secrets -o jsonpath='{.data.MINIO_ROOT_USER}' | base64 --decode)"
MINIO_PASS="$(kubectl --context "$CTX" -n "$NS" get secret minio-secrets -o jsonpath='{.data.MINIO_ROOT_PASSWORD}' | base64 --decode)"
printf '[default]\nregion = us-east-1\ns3 =\n    addressing_style = path\n' > "$MINIO_CFG"

minio() {
    env -u AWS_PROFILE -u AWS_DEFAULT_PROFILE -u AWS_SESSION_TOKEN \
        AWS_CONFIG_FILE="$MINIO_CFG" AWS_SHARED_CREDENTIALS_FILE=/dev/null \
        AWS_ACCESS_KEY_ID="$MINIO_USER" AWS_SECRET_ACCESS_KEY="$MINIO_PASS" \
        aws --endpoint-url "http://127.0.0.1:$LOCAL_PORT" "$@"
}
s3() { aws --region "$REGION" "$@"; }

kubectl --context "$CTX" -n "$NS" port-forward svc/minio-svc "$LOCAL_PORT:9000" >/dev/null 2>&1 &
PF_PID=$!
for _ in $(seq 1 30); do
    minio s3api list-buckets >/dev/null 2>&1 && break
    sleep 1
done
minio s3api list-buckets >/dev/null || { echo -e "${RED}MinIO not reachable via port-forward${NC}"; exit 1; }

if [ "$ACTION" = backup ]; then
    BUCKETS="$(minio s3api list-buckets --query 'Buckets[].Name' --output text | tr '\t' '\n' | sed '/^None$/d;/^$/d')"
    if [ -z "$BUCKETS" ]; then
        echo -e "${YELLOW}MinIO has no buckets — nothing to back up.${NC}"
        exit 0
    fi
    while read -r B; do
        echo "  MinIO $B -> $S3_PREFIX/$B/"
        minio s3 sync "s3://$B" "$STAGE/$B" --only-show-errors
        s3 s3 sync "$STAGE/$B" "$S3_PREFIX/$B" --only-show-errors
        rm -rf "${STAGE:?}/$B"
    done <<< "$BUCKETS"
    echo -e "${GREEN}MinIO backed up to $S3_PREFIX/${NC}"
else
    # `s3 ls` exits 1 on an empty prefix (first session): not an error here.
    BUCKETS="$({ s3 s3 ls "$S3_PREFIX/" 2>/dev/null || true; } | awk '$1=="PRE"{sub("/$","",$2); print $2}')"
    if [ -z "$BUCKETS" ]; then
        echo -e "${YELLOW}No MinIO backup under $S3_PREFIX/ — nothing to restore.${NC}"
        exit 0
    fi
    while read -r B; do
        echo "  $S3_PREFIX/$B/ -> MinIO $B"
        minio s3api head-bucket --bucket "$B" >/dev/null 2>&1 || minio s3 mb "s3://$B" >/dev/null
        s3 s3 sync "$S3_PREFIX/$B" "$STAGE/$B" --only-show-errors
        minio s3 sync "$STAGE/$B" "s3://$B" --only-show-errors
        rm -rf "${STAGE:?}/$B"
    done <<< "$BUCKETS"
    echo -e "${GREEN}MinIO restored from $S3_PREFIX/${NC}"
fi
