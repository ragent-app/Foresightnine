#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${TARGET_ROOT:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"
RESTART_SERVICES="${RESTART_SERVICES:-1}"

for rel in \
  src/apps/web/src/App.tsx \
  src/apps/web/src/components/MarketDataConfigPanel.tsx \
  src/apps/web/src/components/MethodsPanel.tsx \
  src/apps/web/src/styles.css
do
  install -D -m 0644 "$SCRIPT_DIR/baseline/$rel" "$ROOT/$rel"
done

rm -f \
  "$ROOT/src/apps/web/src/methods-guide.ts" \
  "$ROOT/src/apps/web/src/methods-guide.test.ts"

if [[ "$RESTART_SERVICES" == "1" && "$ROOT" == "/home/lxx/Foresightnine" ]]; then
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
  corepack pnpm --dir "$ROOT/src" --filter web build
  systemctl --user restart foresightnine-web.service
fi

printf '%s\n' "ROLLBACK_RESULT=PASS ROOT=$ROOT"
