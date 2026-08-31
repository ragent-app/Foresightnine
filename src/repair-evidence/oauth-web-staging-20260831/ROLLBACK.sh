#!/usr/bin/env bash
set -euo pipefail
ROOT="${TARGET_ROOT:-/home/lxx/Foresightnine/src}"
EVID="${EVID_ROOT:-/home/lxx/Foresightnine/src/repair-evidence/oauth-web-staging-20260831}"
CLOUDFLARED_CONFIG="${TARGET_CLOUDFLARED:-/home/lxx/.cloudflared/foresightnine-api-staging.yml}"
restore() {
  local src="$EVID/baseline/$1" dst="$2"
  install -m "$3" "$src" "$dst"
}
restore apps/web/.env.production.local "$ROOT/apps/web/.env.production.local" 600
restore apps/web/vite.config.ts "$ROOT/apps/web/vite.config.ts" 664
restore apps/api/app/oauth_identity.py "$ROOT/apps/api/app/oauth_identity.py" 664
restore cloudflared/foresightnine-api-staging.yml "$CLOUDFLARED_CONFIG" 600
rm -f "$ROOT/apps/api/tests/test_oauth_persistence.py"
if [[ "${SKIP_BUILD:-0}" != 1 ]]; then (cd "$ROOT" && corepack pnpm --filter @foresightnine/web build); fi
if [[ "${SKIP_RESTART:-0}" != 1 ]]; then
  systemctl --user restart foresightnine-web.service
  systemctl --user restart foresightnine-cloudflared.service
  printf '%s\n' 'rollback restored baseline files and restarted web/cloudflared'
else
  printf '%s\n' 'rollback restored baseline files (service restart skipped)'
fi
