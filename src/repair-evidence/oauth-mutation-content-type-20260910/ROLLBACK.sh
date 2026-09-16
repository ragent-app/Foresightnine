#!/usr/bin/env bash
set -euo pipefail

target_dir=${1:?usage: ROLLBACK.sh <separate-copy-directory>}
artifact_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

test -d "$target_dir"
cp "$artifact_dir/baseline-files/web-oauth-client.ts.orig" "$target_dir/web-oauth-client.ts"
cp "$artifact_dir/baseline-files/web-oauth-client.test.ts.orig" "$target_dir/web-oauth-client.test.ts"
cp "$artifact_dir/baseline-files/mobile-oauth-client.ts.orig" "$target_dir/mobile-oauth-client.ts"
cp "$artifact_dir/baseline-files/mobile-oauth-client.test.ts.orig" "$target_dir/mobile-oauth-client.test.ts"
for file in web-oauth-client.ts web-oauth-client.test.ts mobile-oauth-client.ts mobile-oauth-client.test.ts; do
  cmp -s "$target_dir/$file" "$artifact_dir/baseline-files/$file.orig"
done
printf '%s\n' 'ROLLBACK_RESULT=RESTORED_COPY'
printf '%s\n' 'ROLLBACK_COPY_VERIFY=PASS'
