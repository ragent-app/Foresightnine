#!/usr/bin/env bash
set -euo pipefail
target_dir=${1:?usage: ROLLBACK.sh <separate-copy-directory>}
artifact_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
test -d "$target_dir"
cp "$artifact_dir/baseline-files/problem.md.orig" "$target_dir/problem.md"
cp "$artifact_dir/baseline-files/plan.md.orig" "$target_dir/plan.md"
cp "$artifact_dir/baseline-files/governance-baseline.json.orig" "$target_dir/governance-baseline.json"
cmp -s "$target_dir/problem.md" "$artifact_dir/baseline-files/problem.md.orig"
cmp -s "$target_dir/plan.md" "$artifact_dir/baseline-files/plan.md.orig"
cmp -s "$target_dir/governance-baseline.json" "$artifact_dir/baseline-files/governance-baseline.json.orig"
printf '%s\n' 'ROLLBACK_RESULT=RESTORED_COPY'
printf '%s\n' 'ROLLBACK_COPY_VERIFY=PASS'
