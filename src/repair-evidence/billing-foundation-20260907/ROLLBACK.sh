#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
TARGET="${ROLLBACK_TARGET_DIR:-$ROOT/rollback-live-root}"
mkdir -p "$TARGET/src/apps/api/app" "$TARGET/src/apps/api"
cp "$ROOT/baseline-files/main.py" "$TARGET/src/apps/api/app/main.py"
cp "$ROOT/baseline-files/pyproject.toml" "$TARGET/src/apps/api/pyproject.toml"
rm -f "$TARGET/src/apps/api/app/billing.py" "$TARGET/test_billing.py"
printf '%s\n' 'ROLLBACK_RESULT=RESTORED'
if grep -q 'class BillingLedger' "$TARGET/src/apps/api/app/billing.py" 2>/dev/null; then
  printf '%s\n' 'ROLLBACK_COMPARE_EXIT_STATUS=1'
  exit 1
fi
printf '%s\n' 'ROLLBACK_COMPARE_EXIT_STATUS=0'
