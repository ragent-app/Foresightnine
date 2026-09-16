#!/usr/bin/env bash
set -euo pipefail
ROOT=${1:?rollback target root required}
cp "$ROOT/src/repair-evidence/play-data-safety-account-deletion-20260916/baseline/privacy.html" "$ROOT/src/apps/web/public/privacy.html"
if [ -e "$ROOT/src/apps/web/public/delete-account.html" ]; then mv "$ROOT/src/apps/web/public/delete-account.html" "$ROOT/src/apps/web/public/delete-account.html.rolled-back"; fi
