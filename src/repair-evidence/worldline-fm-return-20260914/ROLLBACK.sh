#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
TARGET_ROOT=${1:-/home/lxx/Foresightnine/src}
install -D -m 0644 "$SCRIPT_DIR/baseline/state.ts" "$TARGET_ROOT/apps/web/src/state.ts"
install -D -m 0644 "$SCRIPT_DIR/baseline/app.test.ts" "$TARGET_ROOT/apps/web/src/app.test.ts"
install -D -m 0644 "$SCRIPT_DIR/baseline/FmPanel.tsx" "$TARGET_ROOT/apps/web/src/components/FmPanel.tsx"
install -D -m 0644 "$SCRIPT_DIR/baseline/i18n.tsx" "$TARGET_ROOT/apps/web/src/i18n.tsx"
echo "ROLLBACK_RESULT=restored"
echo "ROLLBACK_TARGET=$TARGET_ROOT"
