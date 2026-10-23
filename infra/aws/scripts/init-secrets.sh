#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Initialize SSM Parameter Store secret values (run ONCE after `make base`)
# The app secrets of infra/common/app-secrets.sh. Terraform (modules/params)
# creates each SecureString with a placeholder; this script replaces the
# placeholder with a real value. A parameter that no longer holds the
# placeholder is left alone (never rotate live creds). ESO then syncs the
# values into the cluster (ClusterSecretStore aws-ps).
#
# No pgBackRest keys here: pgBackRest uses IRSA (web-id), not static creds.
#
# Config comes from the base stack's terraform outputs. Runs as NBODY_AWS_PROFILE
# (default nbody), never the inherited AWS_PROFILE.
set -euo pipefail

# Never the shell's AWS_PROFILE (it may belong to another account):
# the deploy profile, also when this script is run outside the Makefile.
export AWS_PROFILE="${NBODY_AWS_PROFILE:-nbody}"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_DIR="$SCRIPT_DIR/../environments/prod/base"

tf_out() { (cd "$ENV_DIR" && terraform output -raw "$1" 2>/dev/null || true); }

command -v aws >/dev/null || { echo -e "${RED}aws CLI not found${NC}"; exit 1; }

REGION="${REGION:-$(tf_out region)}"
PLACEHOLDER="${PLACEHOLDER:-$(tf_out secret_placeholder)}"

if [ -z "$REGION" ] || [ -z "$PLACEHOLDER" ]; then
    echo -e "${RED}region/secret_placeholder not found in terraform output.${NC}"
    echo "Run after 'make init && make base' (or set REGION and PLACEHOLDER)."
    exit 1
fi

echo -e "${GREEN}Parameter Store: ${REGION} (profile: ${AWS_PROFILE:-<default>})${NC}"
echo ""

# Set a parameter only while it still holds the Terraform placeholder.
set_secret() {
    local name="$1" value="$2" current
    if ! current="$(aws ssm get-parameter --region "$REGION" --name "$name" \
            --with-decryption --query Parameter.Value --output text 2>/dev/null)"; then
        echo -e "${RED}  ✗ $name: parameter not found — run 'make base' first.${NC}"
        return 1
    fi
    if [ "$current" != "$PLACEHOLDER" ]; then
        echo -e "${YELLOW}  $name already set — skipping${NC}"
        return
    fi
    aws ssm put-parameter --region "$REGION" --name "$name" \
        --type SecureString --value "$value" --overwrite >/dev/null
    echo -e "${GREEN}  ✓ $name${NC}"
}

echo "--- Application secrets ---"
# shellcheck source=../../common/app-secrets.sh
. "$SCRIPT_DIR/../../common/app-secrets.sh"
app_secrets

echo ""
echo -e "${GREEN}Done. ESO will sync these into the cluster (ClusterSecretStore aws-ps).${NC}"
echo "The dashboard password: aws ssm get-parameter --profile $AWS_PROFILE --region $REGION --name nbody-default-user-password --with-decryption --query Parameter.Value --output text"
