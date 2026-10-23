#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

# Build the N-body simulator images and push them to the clouds' registries
#
# Usage:
#   ./scripts/build-and-push.sh --azure                  # build + push to ACR, auto-generated tag
#   ./scripts/build-and-push.sh --gcp --aws 2026.03.24.v1  # explicit tag, two clouds
#   ./scripts/build-and-push.sh --build-only             # build only, no push
#   ./scripts/build-and-push.sh --push-only --azure 2026.03.24.v1  # push images built earlier
#
# Each cloud's push also sets the tag in configs/<cloud>/apps/kustomization.yaml.
# The registry is $GCP_REGISTRY / $ACR_REGISTRY / $ECR_REGISTRY, else the one in
# configs/<cloud>/session/session.env (written by `make configure` in infra/<cloud>).
#
# Builder: every image is built with `docker buildx build --load`, so it lands
# in the local image store (needed for `docker tag`/`docker push`). The builder
# is --builder NAME, else $BUILDX_BUILDER, else the builder of the current
# docker context (the `docker` driver: `orbstack`, `default`, `desktop-linux`),
# not the one selected with `docker buildx use`.
#
# Compatible with macOS /bin/bash 3.2 under `set -u`.

PLATFORM="linux/amd64"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
CODE_DIR="$ROOT_DIR/code"

# AWS login uses $NBODY_AWS_PROFILE (default nbody). Deliberately NOT
# $AWS_PROFILE, which may belong to another account.
NBODY_AWS_PROFILE="${NBODY_AWS_PROFILE:-nbody}"

IMAGES=(
    "kubenbody|$CODE_DIR|Dockerfile"
    "kubenbody-gpu|$CODE_DIR|Dockerfile.gpu"
    "kubenbody-dashboard|$CODE_DIR/web-dashboard|Dockerfile"
)

# --- Parse args ---
BUILD=true
PUSH=true
CLOUDS=()
BUILDER="${BUILDX_BUILDER:-}"
TAG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --build-only)  PUSH=false; shift ;;
        --push-only)   BUILD=false; shift ;;
        --gcp|--azure|--aws) CLOUDS+=("${1#--}"); shift ;;
        --builder)
            if [[ $# -lt 2 ]]; then
                echo "Error: --builder needs a builder name (see: docker buildx ls)"
                exit 1
            fi
            BUILDER="$2"; shift 2 ;;
        --builder=*)   BUILDER="${1#--builder=}"; shift ;;
        --help|-h)
            echo "Usage: $0 [--build-only|--push-only] [--gcp] [--azure] [--aws] [--builder NAME] [TAG]"
            echo ""
            echo "  TAG             Image tag (default: YYYY.MM.DD.vN auto-incremented)"
            echo "  --build-only    Build images locally, don't push"
            echo "  --push-only     Push already-built images (requires TAG)"
            echo "  --gcp           Push to Artifact Registry and set the tag in configs/gcp"
            echo "  --azure         Push to ACR and set the tag in configs/azure"
            echo "  --aws           Push to ECR and set the tag in configs/aws"
            echo "  --builder NAME  buildx builder (default: \$BUILDX_BUILDER, else the current"
            echo "                  docker context's builder, i.e. the 'docker' driver)"
            echo ""
            echo "Images built:"
            for img in "${IMAGES[@]}"; do
                IFS='|' read -r name _ _ <<< "$img"
                echo "  - $name"
            done
            exit 0
            ;;
        -*)  echo "Error: unknown option $1 (see --help)"; exit 1 ;;
        *)   TAG="$1"; shift ;;
    esac
done

# The registry of a cloud: its environment variable, else its session.env.
registry_of() {
    local var key
    case "$1" in
        gcp)   var=GCP_REGISTRY; key=REGISTRY ;;
        azure) var=ACR_REGISTRY; key=ACR_REGISTRY ;;
        aws)   var=ECR_REGISTRY; key=ECR_REGISTRY ;;
    esac
    local env_file="$ROOT_DIR/configs/$1/session/session.env"
    if [[ -n "${!var:-}" ]]; then
        echo "${!var}"
    elif [[ "$(sed -n 's/^MODE=//p' "$env_file" 2>/dev/null)" == real ]]; then
        sed -n "s/^$key=//p" "$env_file"
    fi                  # an example session (make render) names no real registry
}
kustomization_of() { echo "$ROOT_DIR/configs/$1/apps/kustomization.yaml"; }

# --- Validate flags ---
if [[ "$BUILD" == false && -z "$TAG" ]]; then
    echo "Error: --push-only requires an explicit TAG (the images already built)"
    exit 1
fi
if [[ "$PUSH" == true && ${#CLOUDS[@]} -eq 0 ]]; then
    echo "Error: nothing to push: add --gcp, --azure and/or --aws (or --build-only)"
    exit 1
fi
REGISTRIES=()
if [[ "$PUSH" == true ]]; then
    for cloud in "${CLOUDS[@]}"; do
        reg="$(registry_of "$cloud")"
        if [[ -z "$reg" ]]; then
            echo "Error: no registry for $cloud: run 'make configure' in infra/$cloud, or set its"
            echo "       registry variable (GCP_REGISTRY, ACR_REGISTRY or ECR_REGISTRY)"
            exit 1
        fi
        override="$(sed -n 's/^IMAGE_REGISTRY=//p' "$ROOT_DIR/configs/$cloud/session/session.env" 2>/dev/null || true)"
        if [[ -n "$override" ]]; then
            echo "Error: configs/$cloud pulls the app images from $override (NBODY_IMAGE_REGISTRY), not from"
            echo "       $reg: run 'make configure NBODY_IMAGE_REGISTRY=none' in infra/$cloud first"
            exit 1
        fi
        REGISTRIES+=("$reg")
    done
fi

# --- Auto-generate tag if not provided ---
# The latest tag is read from the first cloud's kustomization.
if [[ -z "$TAG" ]]; then
    DATE_PREFIX=$(date +%Y.%m.%d)
    EXISTING=""
    if [[ ${#CLOUDS[@]} -gt 0 ]]; then
        EXISTING=$(grep 'newTag:' "$(kustomization_of "${CLOUDS[0]}")" | head -1 | sed 's/.*newTag: *"\(.*\)"/\1/')
    fi
    if [[ "$EXISTING" == "$DATE_PREFIX"* ]]; then
        NEXT_V=$(( ${EXISTING#"$DATE_PREFIX".v} + 1 ))
    else
        NEXT_V=1
    fi
    TAG="${DATE_PREFIX}.v${NEXT_V}"
fi

# --- AWS: fail before the (slow) build if the profile isn't the registry's account ---
for i in ${CLOUDS[@]+"${!CLOUDS[@]}"}; do
    [[ "${CLOUDS[$i]}" == aws && "$PUSH" == true ]] || continue
    ECR="${REGISTRIES[$i]}"
    if [[ ! "$ECR" =~ ^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com$ ]]; then
        echo "Error: ECR registry '$ECR' is not <account>.dkr.ecr.<region>.amazonaws.com"
        exit 1
    fi
    ACTUAL_ACCOUNT=$(aws sts get-caller-identity --profile "$NBODY_AWS_PROFILE" \
        --query Account --output text 2>/dev/null || true)
    if [[ "$ACTUAL_ACCOUNT" != "${ECR%%.*}" ]]; then
        echo "Error: profile '$NBODY_AWS_PROFILE' is account '${ACTUAL_ACCOUNT:-<no credentials>}', registry is ${ECR%%.*}. Refusing."
        exit 1
    fi
done

# --- Resolve the buildx builder ---
if [[ "$BUILD" == true ]]; then
    if [[ -z "$BUILDER" ]]; then
        # The builder named after the docker context uses the `docker` driver.
        BUILDER=$(docker context show 2>/dev/null || echo default)
    fi
    BUILDER_DRIVER=$(docker buildx inspect "$BUILDER" 2>/dev/null | sed -n 's/^Driver: *//p' | head -1 || true)
    if [[ -z "$BUILDER_DRIVER" ]]; then
        echo "Error: buildx builder '$BUILDER' not found or not reachable (see: docker buildx ls)"
        exit 1
    fi
fi

echo "============================================="
echo "  Tag:      $TAG"
echo "  Platform: $PLATFORM"
echo "  Build:    $BUILD"
if [[ "$BUILD" == true ]]; then
    echo "  Builder:  $BUILDER ($BUILDER_DRIVER driver)"
fi
echo "  Push:     $PUSH"
for i in ${REGISTRIES[@]+"${!REGISTRIES[@]}"}; do
    echo "            ${CLOUDS[$i]}: ${REGISTRIES[$i]}"
done
echo "============================================="
echo ""

# --- Build: <name>:local and <name>:<tag> in the local image store ---
if [[ "$BUILD" == true ]]; then
    for img in "${IMAGES[@]}"; do
        IFS='|' read -r NAME CONTEXT DOCKERFILE <<< "$img"
        echo ">>> Building $NAME:$TAG ($CONTEXT/$DOCKERFILE)"
        # --load: put the result in the local image store even when the builder
        # is not the daemon's own (docker-container driver).
        docker buildx build \
            --builder "$BUILDER" \
            --load \
            --platform "$PLATFORM" \
            -t "$NAME:local" \
            -t "$NAME:$TAG" \
            -f "$CONTEXT/$DOCKERFILE" \
            "$CONTEXT"
        echo ""
    done
    echo "All images built."
fi

# --- Push: the same local image to every registry (identical digest) ---
login() {
    case "$1" in
        gcp)   gcloud auth configure-docker "${2%%/*}" --quiet ;;
        azure) az acr login --name "${2%%.*}" ;;
        aws)   aws ecr get-login-password --profile "$NBODY_AWS_PROFILE" --region "$(echo "$2" | cut -d. -f4)" \
                   | docker login --username AWS --password-stdin "$2" ;;
    esac
}

if [[ "$PUSH" == true ]]; then
    for i in "${!CLOUDS[@]}"; do
        cloud="${CLOUDS[$i]}"; reg="${REGISTRIES[$i]}"
        echo ""
        echo ">>> Pushing to $reg ($cloud)"
        login "$cloud" "$reg"
        for img in "${IMAGES[@]}"; do
            IFS='|' read -r NAME _ _ <<< "$img"
            docker tag "$NAME:$TAG" "$reg/$NAME:$TAG"
            docker push "$reg/$NAME:$TAG"
        done
        K="$(kustomization_of "$cloud")"
        sed -i.bak "s/newTag: \".*\"/newTag: \"$TAG\"/" "$K"
        rm -f "$K.bak"
        if [[ "$cloud" == aws ]]; then step="make up"; else step="make deploy-k8s"; fi
        echo "    Tag set in $K. Deploy: (cd infra/$cloud && $step)"
    done
fi

echo ""
echo "Done. Tag: $TAG"
