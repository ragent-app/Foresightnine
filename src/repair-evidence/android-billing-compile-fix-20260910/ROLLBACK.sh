#!/usr/bin/env bash
set -euo pipefail
ROOT=${1:?usage: ROLLBACK.sh COPY_ROOT}
EVIDENCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
mkdir -p "$ROOT/src/apps/mobile/android/app/src/main/java/com/foresightnine/mobile"
cp "$EVIDENCE_DIR/build.gradle.orig" "$ROOT/src/apps/mobile/android/app/build.gradle"
cp "$EVIDENCE_DIR/ForesightNineBillingModule.kt.orig" "$ROOT/src/apps/mobile/android/app/src/main/java/com/foresightnine/mobile/ForesightNineBillingModule.kt"
printf '%s\n' 'ROLLBACK_RESULT=RESTORED_COPY'
