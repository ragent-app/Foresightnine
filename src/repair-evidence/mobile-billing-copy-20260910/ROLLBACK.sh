#!/usr/bin/env bash
set -euo pipefail

target_dir=${1:?usage: ROLLBACK.sh <separate-copy-directory>}
artifact_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

test -d "$target_dir"
cp "$artifact_dir/baseline-files/i18n.ts.orig" "$target_dir/i18n.ts"
cp "$artifact_dir/baseline-files/console.tsx.orig" "$target_dir/console.tsx"
cmp -s "$target_dir/i18n.ts" "$artifact_dir/baseline-files/i18n.ts.orig"
cmp -s "$target_dir/console.tsx" "$artifact_dir/baseline-files/console.tsx.orig"
printf '%s\n' 'ROLLBACK_RESULT=RESTORED_COPY'
printf '%s\n' 'ROLLBACK_COPY_VERIFY=PASS'
