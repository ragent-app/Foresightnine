#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TARGET_ROOT=${1:-/home/lxx/Foresightnine/src}
files=(apps/api/app/openai_compatible.py apps/api/tests/test_openai_compatible_provider.py)
for file in "${files[@]}"; do
  source_file="$SCRIPT_DIR/baseline/$file"
  target_file="$TARGET_ROOT/$file"
  test -f "$source_file"
  mkdir -p "$(dirname "$target_file")"
  cp --preserve=mode,timestamps "$source_file" "$target_file"
  printf 'RESTORED %s SHA256=%s\n' "$file" "$(sha256sum "$target_file" | awk '{print $1}')"
done
printf 'ROLLBACK_STATUS=restored_original_files\nROLLBACK_EXIT=0\n'
