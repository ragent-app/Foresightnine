#!/usr/bin/env bash
set -euo pipefail
ROOT="${TARGET_ROOT:-/home/lxx/Foresightnine/src}"
EVID="${EVID_ROOT:-/home/lxx/Foresightnine/src/repair-evidence/app-link-certificates-20260831}"
restore() { install -m "$3" "$EVID/baseline/$1" "$2"; }
remove_path() {
  python3 - "$1" <<'PY'
import pathlib
import shutil
import sys

path = pathlib.Path(sys.argv[1])
if path.is_dir() and not path.is_symlink():
    shutil.rmtree(path)
elif path.exists() or path.is_symlink():
    path.unlink()
PY
}
restore apps/web/.env.production.local "$ROOT/apps/web/.env.production.local" 600
restore apps/web/vite.config.ts "$ROOT/apps/web/vite.config.ts" 664
restore infra/docker/staging.env "$ROOT/infra/docker/staging.env" 600
restore apps/mobile/app.config.cjs "$ROOT/apps/mobile/app.config.cjs" 664
restore apps/mobile/android/app/src/main/AndroidManifest.xml "$ROOT/apps/mobile/android/app/src/main/AndroidManifest.xml" 664
if [[ -f "$EVID/baseline/apps/mobile/.env.qa.local" ]]; then
  restore apps/mobile/.env.qa.local "$ROOT/apps/mobile/.env.qa.local" 600
else
  remove_path "$ROOT/apps/mobile/.env.qa.local"
fi
if [[ -f "$EVID/baseline/certificates.zip" ]]; then install -m 664 "$EVID/baseline/certificates.zip" "$ROOT/apps/mobile/certificates.zip"; fi
if [[ -d "$ROOT/apps/mobile/certificates" ]]; then
  remove_path "$ROOT/apps/mobile/certificates"
fi
remove_path "$ROOT/apps/mobile/tests/app-link-certificates.test.mjs"
if [[ "${SKIP_SERVICES:-0}" != 1 ]]; then
  (cd "$ROOT" && corepack pnpm --filter @foresightnine/web build)
  systemctl --user restart foresightnine-web.service
  systemctl --user restart foresightnine-cloudflared.service
fi
printf '%s\n' 'rollback restored baseline App Link/config files; service restart skipped or completed'
