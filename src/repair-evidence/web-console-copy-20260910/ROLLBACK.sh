#!/usr/bin/env bash
set -euo pipefail

target_dir=${1:?usage: ROLLBACK.sh <separate-copy-directory>}
artifact_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

test -d "$target_dir"
cp "$artifact_dir/baseline-files/i18n.tsx.orig" "$target_dir/i18n.tsx"
cp "$artifact_dir/baseline-files/MethodsPanel.tsx.orig" "$target_dir/MethodsPanel.tsx"
cmp -s "$target_dir/i18n.tsx" "$artifact_dir/baseline-files/i18n.tsx.orig"
cmp -s "$target_dir/MethodsPanel.tsx" "$artifact_dir/baseline-files/MethodsPanel.tsx.orig"
printf '%s\n' 'ROLLBACK_RESULT=RESTORED_COPY'
printf '%s\n' 'ROLLBACK_COPY_VERIFY=PASS'
