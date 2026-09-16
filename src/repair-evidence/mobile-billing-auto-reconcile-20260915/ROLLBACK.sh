#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TARGET_ROOT=${1:-/home/lxx/Foresightnine/src}
restore_files=(
  apps/mobile/app/console.tsx
  docs/problem.md
  apps/mobile/android/app/build/outputs/bundle/release/app-release.aab
)
new_files=(
  apps/mobile/src/billing-reconciliation.ts
  apps/mobile/tests/billing-reconciliation.test.ts
)
for file in "${restore_files[@]}"; do
  source_file="$SCRIPT_DIR/baseline/$file"
  target_file="$TARGET_ROOT/$file"
  test -f "$source_file"
  mkdir -p "$(dirname "$target_file")"
  cp --preserve=mode,timestamps "$source_file" "$target_file"
  printf 'RESTORED %s SHA256=%s\n' "$file" "$(sha256sum "$target_file" | awk '{print $1}')"
done
for file in "${new_files[@]}"; do
  target_file="$TARGET_ROOT/$file"
  if [ -e "$target_file" ]; then unlink "$target_file"; fi
  test ! -e "$target_file"
  printf 'RESTORED %s=MISSING\n' "$file"
done
printf 'ROLLBACK_STATUS=restored_pre_change_state\n'
printf 'ROLLBACK_EXIT=0\n'
