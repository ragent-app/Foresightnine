#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
TARGET="${ROLLBACK_TARGET:-/home/lxx/.cloudflared/foresightnine-api-staging.yml}"
SOURCE="${ROLLBACK_SOURCE:-$ROOT/BASELINE_FILE}"
cp "$SOURCE" "$TARGET"
chmod 600 "$TARGET"
if cmp -s "$SOURCE" "$TARGET"; then
  printf '%s\n' 'ROLLBACK_RESULT=RESTORED'
  printf '%s\n' 'ROLLBACK_COMPARE_EXIT_STATUS=0'
else
  printf '%s\n' 'ROLLBACK_RESULT=FAILED'
  printf '%s\n' 'ROLLBACK_COMPARE_EXIT_STATUS=1'
  exit 1
fi
