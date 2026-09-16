#!/usr/bin/env bash
set -euo pipefail
ROOT=${1:?usage: ROLLBACK.sh COPY_ROOT}
EVIDENCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
mkdir -p "$ROOT/src/apps/api/app"
cp "$EVIDENCE_DIR/openai_compatible.py.orig" "$ROOT/src/apps/api/app/openai_compatible.py"
if [ -f "$ROOT/src/infra/docker/staging.env" ]; then
  sed -i '/^FORESIGHTNINE_LLM_TIMEOUT_SECONDS=/d' "$ROOT/src/infra/docker/staging.env"
fi
printf '%s\n' 'ROLLBACK_RESULT=RESTORED_COPY'
