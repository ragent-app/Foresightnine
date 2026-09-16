#!/usr/bin/env bash
set -euo pipefail
ROOT=${1:?usage: ROLLBACK.sh COPY_ROOT}
EVIDENCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
mkdir -p "$ROOT/src/apps/api/app"
cp "$EVIDENCE_DIR/billing.py.orig" "$ROOT/src/apps/api/app/billing.py"
printf '%s\n' 'ROLLBACK_RESULT=RESTORED_COPY'
