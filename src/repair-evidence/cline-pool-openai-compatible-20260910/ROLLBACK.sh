#!/usr/bin/env bash
set -euo pipefail
ROOT=${1:?usage: ROLLBACK.sh COPY_ROOT}
EVIDENCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
restore() {
  local rel=$1 backup=$2 target="$ROOT/$1"
  mkdir -p "$(dirname -- "$target")"
  cp "$EVIDENCE_DIR/$backup" "$target"
}
restore src/apps/api/app/openai_compatible.py openai_compatible.py.orig
restore src/apps/api/tests/test_openai_compatible_provider.py test_openai_compatible_provider.py.orig
restore src/infra/docker/compose.yaml compose.yaml.orig
if [[ -f "$ROOT/staging.env" ]]; then
  cp "$EVIDENCE_DIR/staging.env.fixture.orig" "$ROOT/staging.env"
fi
printf '%s\n' 'ROLLBACK_RESULT=RESTORED_COPY'
