#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
TARGET_ROOT=${1:-/home/lxx/Foresightnine/src}
install -D -m 0644 "$SCRIPT_DIR/baseline/App.tsx" "$TARGET_ROOT/apps/web/src/App.tsx"
echo "ROLLBACK_RESULT=restored"
echo "ROLLBACK_TARGET=$TARGET_ROOT"
