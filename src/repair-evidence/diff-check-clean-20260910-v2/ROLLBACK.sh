#!/usr/bin/env bash
set -euo pipefail
target_dir=${1:?usage: ROLLBACK.sh <separate-copy-directory>}
artifact_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
test -d "$target_dir"
cp "$artifact_dir/baseline-files/dataprovider.md.orig" "$target_dir/dataprovider指导.md"
cmp -s "$target_dir/dataprovider指导.md" "$artifact_dir/baseline-files/dataprovider.md.orig"
printf '%s\n' 'ROLLBACK_RESULT=RESTORED_COPY'
printf '%s\n' 'ROLLBACK_COPY_VERIFY=PASS'
