#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  printf 'usage: %s <git-worktree-root> <cloudflared-config> [cloudflare-key]\n' "$0" >&2
  exit 2
fi

TARGET_ROOT=$(realpath "$1")
TARGET_CONFIG=$(realpath -m "$2")
SNAPSHOT_DIR=$(cd -- "$(dirname -- "$0")" && pwd)
BASE_SHA=$(cat "$SNAPSHOT_DIR/base-revision.txt" | sed -n 's/^BASE_SHA=//p')

[[ -d "$TARGET_ROOT/.git" || -f "$TARGET_ROOT/.git" ]] || { printf 'target is not a Git worktree\n' >&2; exit 3; }
git -C "$TARGET_ROOT" cat-file -e "$BASE_SHA^{commit}"

git -C "$TARGET_ROOT" restore --source "$BASE_SHA" -- \
  src/apps/api/tests/test_api.py \
  src/docs/api/README.md \
  src/docs/problem.md \
  src/infra/deployment/cloudflared/foresightnine-api-staging.yml.example \
  src/scripts/verify/boundaries.mjs \
  src/scripts/verify/release-candidate.mjs
rm -f "$TARGET_ROOT/src/docs/release/cloudflare-routing.md" "$TARGET_ROOT/src/scripts/verify/boundaries.test.mjs"

if [[ -f "$SNAPSHOT_DIR/cloudflared-before.yml" ]]; then
  install -m 600 "$SNAPSHOT_DIR/cloudflared-before.yml" "$TARGET_CONFIG"
fi
if [[ $# -eq 3 && -f "$SNAPSHOT_DIR/key-mode-before.txt" ]]; then
  chmod "$(cat "$SNAPSHOT_DIR/key-mode-before.txt")" "$3"
fi

printf 'rollback restored base revision %s\n' "$BASE_SHA"
printf 'repo=%s\nconfig=%s\n' "$TARGET_ROOT" "$TARGET_CONFIG"
