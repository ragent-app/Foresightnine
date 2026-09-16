#!/usr/bin/env bash
set -euo pipefail
ROOT=${1:?usage: ROLLBACK.sh COPY_ROOT}
EVIDENCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
mkdir -p "$ROOT/src/apps/api/app" "$ROOT/src/apps/api/tests"
cp "$EVIDENCE_DIR/main.py.orig" "$ROOT/src/apps/api/app/main.py"
cp "$EVIDENCE_DIR/test_billing.py.orig" "$ROOT/src/apps/api/tests/test_billing.py"
printf '%s\n' 'ROLLBACK_RESULT=RESTORED_COPY'
