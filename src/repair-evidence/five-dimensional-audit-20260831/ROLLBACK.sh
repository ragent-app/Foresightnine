#!/usr/bin/env bash
set -euo pipefail
TARGET_FILE="${TARGET_FILE:?TARGET_FILE is required}"
BASELINE_FILE="${BASELINE_FILE:?BASELINE_FILE is required}"
install -m 664 "$BASELINE_FILE" "$TARGET_FILE"
printf 'rollback_restored=1 target=%s baseline_bytes=%s target_bytes=%s\n' \
  "$TARGET_FILE" "$(stat -c '%s' "$BASELINE_FILE")" "$(stat -c '%s' "$TARGET_FILE")"
