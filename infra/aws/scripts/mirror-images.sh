#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Mirror third-party images into ECR (run after `make base`; re-run any time)
# Copies every image in infra/aws/mirror-images.json into its ECR mirror repo
# (created by the base stack). Registry-to-registry copy with
# `docker buildx imagetools create` — no local pull, and a single source is
# copied as-is, so multi-arch indexes and digests are preserved.
#
# Why: anonymous Docker Hub pulls are rate-limited per egress IP, and Bitnami's
# 2025 catalogue change makes the pinned redis digests' long-term availability
# uncertain. Every `make up` then pulls from ECR.
#
# Prints source -> ECR digest for each image; the configs/aws overlay (step 4)
# references the mirrors by these digests. Idempotent: an image whose ECR digest
# already matches the source is skipped.
set -euo pipefail

# Never the shell's AWS_PROFILE (it may belong to another account):
# the deploy profile, also when this script is run outside the Makefile.
export AWS_PROFILE="${NBODY_AWS_PROFILE:-nbody}"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AWS_DIR="$SCRIPT_DIR/.."
ENV_DIR="$AWS_DIR/environments/prod/base"
LIST="$AWS_DIR/mirror-images.json"

tf_out() { (cd "$ENV_DIR" && terraform output -raw "$1" 2>/dev/null || true); }

for cmd in aws docker python3; do
    command -v "$cmd" >/dev/null || { echo -e "${RED}$cmd not found${NC}"; exit 1; }
done

ECR_REGISTRY="${ECR_REGISTRY:-$(tf_out ecr_registry)}"
REGION="${REGION:-$(tf_out region)}"
if [ -z "$ECR_REGISTRY" ] || [ -z "$REGION" ]; then
    echo -e "${RED}ecr_registry/region not found in terraform output — run after 'make base'.${NC}"
    exit 1
fi

echo -e "${GREEN}Logging in to $ECR_REGISTRY (profile: ${AWS_PROFILE:-<default>})${NC}"
aws ecr get-login-password --region "$REGION" \
    | docker login --username AWS --password-stdin "$ECR_REGISTRY" >/dev/null

# Digest of whatever a reference points at (index or manifest); empty if missing.
digest_of() {
    docker buildx imagetools inspect "$1" --format '{{json .Manifest}}' 2>/dev/null \
        | python3 -c 'import sys,json; print(json.load(sys.stdin).get("digest",""))' 2>/dev/null \
        || true
}

# "source|repo|tag" lines from the JSON list.
ENTRIES="$(python3 -c '
import json, sys
for m in json.load(open(sys.argv[1])):
    print("{}|{}|{}".format(m["source"], m["repo"], m["tag"]))
' "$LIST")"

FAILED=0
echo ""
while IFS='|' read -r SRC REPO TAG; do
    [ -z "$SRC" ] && continue
    DST="$ECR_REGISTRY/$REPO:$TAG"
    SRC_DIGEST="$(digest_of "$SRC")"
    if [ -z "$SRC_DIGEST" ]; then
        echo -e "${RED}  ✗ $SRC: cannot resolve source (gone upstream or rate-limited?)${NC}"
        FAILED=1
        continue
    fi
    if [ "$(digest_of "$DST")" = "$SRC_DIGEST" ]; then
        echo -e "${YELLOW}  = $REPO:$TAG already mirrored ($SRC_DIGEST)${NC}"
        continue
    fi
    echo "  > $SRC"
    if ! docker buildx imagetools create --tag "$DST" "$SRC" >/dev/null; then
        echo -e "${RED}  ✗ copy failed: $SRC -> $DST${NC}"
        FAILED=1
        continue
    fi
    DST_DIGEST="$(digest_of "$DST")"
    if [ "$DST_DIGEST" != "$SRC_DIGEST" ]; then
        # Not fatal (the image works), but the overlay must then pin DST_DIGEST.
        echo -e "${YELLOW}  ! $REPO:$TAG digest changed in copy: $SRC_DIGEST -> $DST_DIGEST${NC}"
    else
        echo -e "${GREEN}  ✓ $REPO:$TAG@$DST_DIGEST${NC}"
    fi
done <<< "$ENTRIES"

echo ""
if [ "$FAILED" -ne 0 ]; then
    echo -e "${RED}Some images failed to mirror (see above).${NC}"
    exit 1
fi
echo -e "${GREEN}All mirrors up to date in $ECR_REGISTRY.${NC}"
