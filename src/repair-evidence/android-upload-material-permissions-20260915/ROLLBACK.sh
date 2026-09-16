#!/usr/bin/env bash
set -euo pipefail

TARGET_ROOT=/home/lxx/ForesightNine-secrets/android-upload
if [[ "${1:-}" == "--local" ]]; then
  TARGET_ROOT="${2:?usage: ROLLBACK.sh --local TARGET_ROOT}"
fi

FILES=(
  "$TARGET_ROOT/foresightnine-upload.jks"
  "$TARGET_ROOT/foresightnine-upload-2026-08-31.jks"
  "$TARGET_ROOT/foresightnine-upload-2026-08-31.keystore-password.dpapi"
  "$TARGET_ROOT/foresightnine-upload-2026-08-31.key-password.dpapi"
)
chmod 664 "${FILES[@]}"
for file in "${FILES[@]}"; do
  printf '%s mode=%s\n' "$(basename "$file")" "$(stat -c %a "$file")"
done
printf 'ROLLBACK_STATUS=restored_original_permissions\n'
printf 'ROLLBACK_EXIT=0\n'
