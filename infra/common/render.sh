#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Offline check of a configs/<cloud> overlay: generate its session with example
# values, render every deploy stage into OUT_DIR (a new temporary directory by
# default) and validate the result with kubeconform when it is installed.
# Refuses to overwrite a session with real values unless FORCE=1.
#
#   render.sh OVERLAY_DIR CONFIGURE_SCRIPT [OUT_DIR]
set -euo pipefail

OVERLAY="${1:?usage: render.sh OVERLAY_DIR CONFIGURE_SCRIPT [OUT_DIR]}"
CONFIGURE="${2:?usage: render.sh OVERLAY_DIR CONFIGURE_SCRIPT [OUT_DIR]}"
OUT="${3:-$(mktemp -d)}"

if [ "$(sed -n 's/^MODE=//p' "$OVERLAY/session/session.env" 2>/dev/null)" = real ] && [ "${FORCE:-}" != 1 ]; then
    echo "Error: $OVERLAY/session holds real values (a live deployment). FORCE=1 to overwrite with examples." >&2
    exit 1
fi
"$CONFIGURE" --example

mkdir -p "$OUT"
kustomize build --enable-helm "$OVERLAY/base" > "$OUT/base.yaml"
kustomize build --enable-helm "$OVERLAY/stages/operators" > "$OUT/operators.yaml"
kustomize build --enable-helm "$OVERLAY" > "$OUT/all.yaml"
echo "rendered: $(grep -c '^kind:' "$OUT/all.yaml") resources ($OUT)"

if command -v kubeconform >/dev/null; then
    kubeconform -kubernetes-version "${K8S_VERSION:-1.35.0}" -strict -summary \
        -skip CustomResourceDefinition \
        -schema-location default \
        -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
        "$OUT/all.yaml" "$OVERLAY/session/cluster-secret-store.yml"
fi
