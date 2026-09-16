#!/usr/bin/env bash
set -euo pipefail

target_dir=${1:?usage: ROLLBACK.sh <separate-copy-directory>}
artifact_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

test -d "$target_dir"
cp "$artifact_dir/baseline-files/secret-scan.mjs.orig" "$target_dir/secret-scan.mjs"
cp "$artifact_dir/baseline-files/billing-api.test.ts.orig" "$target_dir/billing-api.test.ts"
cmp -s "$target_dir/secret-scan.mjs" "$artifact_dir/baseline-files/secret-scan.mjs.orig"
cmp -s "$target_dir/billing-api.test.ts" "$artifact_dir/baseline-files/billing-api.test.ts.orig"
printf '%s\n' 'ROLLBACK_RESULT=RESTORED_COPY'
printf '%s\n' 'ROLLBACK_COPY_VERIFY=PASS'
