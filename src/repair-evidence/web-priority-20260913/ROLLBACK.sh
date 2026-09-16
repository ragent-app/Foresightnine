#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${TARGET_ROOT:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"
LIVE_HOME="${TARGET_HOME:-/home/lxx}"
RESTART_SERVICES="${RESTART_SERVICES:-1}"

restore_from_baseline() {
  local rel="$1"
  install -D -m 0644 "$SCRIPT_DIR/baseline/$rel" "$ROOT/$rel"
}

for rel in \
  src/apps/api/app/main.py \
  src/apps/api/app/market_provider.py \
  src/apps/web/index.html \
  src/apps/web/src/App.tsx \
  src/apps/web/src/components/MethodsPanel.tsx \
  src/apps/web/src/components/SocialLoginPanel.tsx \
  src/apps/web/src/runtime-config.ts \
  src/apps/web/src/search-client.test.ts \
  src/apps/web/src/styles.css
do
  restore_from_baseline "$rel"
done

rm -f \
  "$ROOT/src/apps/api/app/market_credentials.py" \
  "$ROOT/src/apps/api/tests/test_market_credentials.py" \
  "$ROOT/src/apps/web/src/market-config-client.ts" \
  "$ROOT/src/apps/web/src/market-config-client.test.ts" \
  "$ROOT/src/apps/web/src/components/MarketDataConfigPanel.tsx" \
  "$ROOT/src/apps/web/public/robots.txt" \
  "$ROOT/src/apps/web/public/sitemap.xml"

python3 - "$ROOT" "$LIVE_HOME" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1])
home = Path(sys.argv[2])

def replace(path: Path, old: str, new: str) -> None:
    if not path.exists():
        return
    text = path.read_text()
    if old in text:
        path.write_text(text.replace(old, new))

def remove_lines(path: Path, needles: tuple[str, ...]) -> None:
    if not path.exists():
        return
    lines = path.read_text().splitlines(keepends=True)
    path.write_text("".join(line for line in lines if not any(n in line for n in needles)))

remove_lines(
    root / "src/infra/docker/compose.yaml",
    (
        "market-credential-encryption.key:/run/secrets/foresightnine-market-credential-encryption.key",
        "FORESIGHTNINE_MARKET_CREDENTIAL_ENCRYPTION_KEY_FILE:",
    ),
)
remove_lines(
    root / "src/infra/docker/staging.env.example",
    (
        "# Mounted file path only; the key material stays outside the repository.",
        "FORESIGHTNINE_MARKET_CREDENTIAL_ENCRYPTION_KEY_FILE=",
    ),
)

for path in (
    root / "src/infra/deployment/cloudflared/foresightnine-api-staging.yml.example",
    home / ".cloudflared/foresightnine-api-staging.yml",
):
    if not path.exists():
        continue
    text = path.read_text()
    blocks = (
        "  # The public apex is a canonical redirect entry handled by the API host middleware.\n"
        "  - hostname: foresightnine.com\n"
        "    service: http://127.0.0.1:8000\n"
        "  - hostname: www.foresightnine.com\n"
        "    service: http://127.0.0.1:8000\n",
        "  # Public apex entries redirect to the canonical app host in API middleware.\n"
        "  - hostname: foresightnine.com\n"
        "    service: http://127.0.0.1:8000\n"
        "  - hostname: www.foresightnine.com\n"
        "    service: http://127.0.0.1:8000\n",
    )
    for block in blocks:
        text = text.replace(block, "")
    text = text.replace("^/(api|oauth|v1|snapshots)(/.*)?$", "^/(api|oauth|v1)(/.*)?$")
    path.write_text(text)

vite = root / "src/apps/web/vite.config.ts"
replace(vite, "connect-src 'self';", "connect-src 'self' https://api-staging.foresightnine.com;")
replace(
    vite,
    'allowedHosts: ["app.foresightnine.com", "foresightnine.com", "www.foresightnine.com", "staging.foresightnine.com"]',
    'allowedHosts: ["staging.foresightnine.com"]',
)

prod_env = root / "src/apps/web/.env.production.local"
replace(prod_env, "VITE_FORESIGHTNINE_API_BASE_URL=https://app.foresightnine.com", "VITE_FORESIGHTNINE_API_BASE_URL=https://api-staging.foresightnine.com")
replace(prod_env, "VITE_FORESIGHTNINE_MARKET_DATA_BASE_URL=https://app.foresightnine.com", "VITE_FORESIGHTNINE_MARKET_DATA_BASE_URL=https://api-staging.foresightnine.com")

progress = root / "src/docs/progress.md"
if progress.exists():
    text = progress.read_text()
    marker = "## Current implementation correction status — 2026-08-31"
    if marker in text:
        progress.write_text("# Progress\n\n" + marker + text.split(marker, 1)[1])
PY

if [[ "$RESTART_SERVICES" == "1" && "$ROOT" == "/home/lxx/Foresightnine" ]]; then
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
  docker compose -f "$ROOT/src/infra/docker/compose.yaml" up -d --build api
  corepack pnpm --dir "$ROOT/src" --filter web build
  systemctl --user restart foresightnine-web.service
  systemctl --user restart cloudflared-foresightnine.service
fi

printf '%s\n' "ROLLBACK_RESULT=PASS ROOT=$ROOT"
