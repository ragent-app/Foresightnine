#!/usr/bin/env bash
set -euo pipefail

BASELINE_ROOT=/home/lxx/.config/foresightnine/rollback/play-app-link-production-signing-20260915
REPO_ROOT=/home/lxx/Foresightnine/src
MODE=live
TARGET_ROOT="$REPO_ROOT"

if [[ "${1:-}" == "--local" ]]; then
  MODE=local
  TARGET_ROOT="${2:?usage: ROLLBACK.sh --local TARGET_ROOT}"
fi

install -d -m 700 "$TARGET_ROOT/apps/web" "$TARGET_ROOT/infra/docker"
install -m 600 "$BASELINE_ROOT/apps/web/.env.production.local" "$TARGET_ROOT/apps/web/.env.production.local"
install -m 600 "$BASELINE_ROOT/infra/docker/staging.env" "$TARGET_ROOT/infra/docker/staging.env"

WEB_SHA256=$(sha256sum "$TARGET_ROOT/apps/web/.env.production.local" | awk '{print $1}')
API_SHA256=$(sha256sum "$TARGET_ROOT/infra/docker/staging.env" | awk '{print $1}')
printf 'RESTORED_WEB_SHA256=%s\n' "$WEB_SHA256"
printf 'RESTORED_API_SHA256=%s\n' "$API_SHA256"

if [[ "$MODE" == "live" ]]; then
  cd "$REPO_ROOT"
  FORESIGHTNINE_ENV=production corepack pnpm --filter @foresightnine/web build -- --mode production
  sudo -n docker compose --env-file infra/docker/staging.env -f infra/docker/compose.yaml up -d --build api
fi

printf 'ROLLBACK_STATUS=restored_original_configuration\n'
printf 'ROLLBACK_EXIT=0\n'
