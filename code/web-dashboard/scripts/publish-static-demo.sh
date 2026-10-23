#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Build the static demo and publish it into a local clone of the GitHub Pages repo.
#
# Usage: scripts/publish-static-demo.sh <path-to-pages-repo-clone> [--push]
#   DEMO_BASE_PATH  base path of the Pages site (default: /<clone directory name>,
#                   i.e. a project site at https://<user>.github.io/<repo>/)
#
# Commits in the clone and stops before pushing unless --push is given.
set -euo pipefail

usage() { echo "usage: $0 <path-to-pages-repo-clone> [--push]" >&2; exit 2; }
[[ $# -ge 1 ]] || usage
clone=$(cd "$1" && pwd) || usage
push=false
[[ "${2:-}" == "--push" ]] && push=true

app_dir=$(cd "$(dirname "$0")/.." && pwd)
base_path=${DEMO_BASE_PATH-/$(basename "$clone")}

[[ -d "$clone/.git" ]] || { echo "✗ $clone is not a git clone" >&2; exit 1; }
# Every publish should map to a source commit
if [[ -n "$(git -C "$app_dir" status --porcelain -- .)" ]]; then
  echo "✗ uncommitted changes under $app_dir - commit them first" >&2
  git -C "$app_dir" status --short -- . >&2
  exit 1
fi

source_sha=$(git -C "$app_dir" rev-parse --short HEAD)
recorded_at=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["recorded_at"])' "$app_dir/demo-data/manifest.json")

# TZ=UTC: dates prerendered at build time shouldn't follow this machine's time zone
(cd "$app_dir" && TZ=UTC NEXT_PUBLIC_BASE_PATH="$base_path" node scripts/build-static-demo.mjs)

# Mirror the export, keeping the clone's git metadata and a custom-domain CNAME if any
rsync -a --delete --exclude .git --exclude CNAME "$app_dir/out/" "$clone/"

git -C "$clone" add -A
if git -C "$clone" diff --cached --quiet; then
  echo "✓ nothing changed in $clone"
  exit 0
fi
git -C "$clone" commit -q -m "Demo build from $source_sha (fixtures recorded $recorded_at)"
git -C "$clone" show --stat --oneline HEAD | tail -5

if $push; then
  git -C "$clone" push
  echo "✓ pushed - GitHub Pages redeploys in about a minute"
else
  echo "✓ committed in $clone - review, then: git -C $clone push"
fi
