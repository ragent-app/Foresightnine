#!/usr/bin/env bash
set -euo pipefail
BASE=/home/lxx/Foresightnine/src/repair-evidence/fm-live-limited-data-20260915/baseline
TARGET=/home/lxx/Foresightnine/src
if [[ "${1:-}" == "--local" ]]; then
  TARGET="${2:?usage: ROLLBACK.sh --local TARGET_ROOT}"
fi
install -d -m 700 "$TARGET/apps/web/src"
for f in fixtures.ts app.test.ts freshness.test.ts; do
  install -m 600 "$BASE/apps/web/src/$f" "$TARGET/apps/web/src/$f"
done
printf 'RESTORED_FIXTURES_SHA256=%s\n' "$(sha256sum "$TARGET/apps/web/src/fixtures.ts" | awk '{print $1}')"
printf 'RESTORED_APP_TEST_SHA256=%s\n' "$(sha256sum "$TARGET/apps/web/src/app.test.ts" | awk '{print $1}')"
printf 'RESTORED_FRESHNESS_TEST_SHA256=%s\n' "$(sha256sum "$TARGET/apps/web/src/freshness.test.ts" | awk '{print $1}')"
printf 'ROLLBACK_STATUS=restored_pre_fix_source_and_tests\n'
printf 'ROLLBACK_EXIT=0\n'
