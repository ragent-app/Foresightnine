#!/usr/bin/env bash
set -euo pipefail

BASELINE_ROOT=/home/lxx/.config/foresightnine/rollback/android-billing-release-v2-20260915
REPO_ROOT=/home/lxx/Foresightnine/src
TARGET_ROOT="$REPO_ROOT"
if [[ "${1:-}" == "--local" ]]; then
  TARGET_ROOT="${2:?usage: ROLLBACK.sh --local TARGET_ROOT}"
fi

APP_ROOT="$TARGET_ROOT/apps/mobile/android/app"
install -d -m 700 "$APP_ROOT/src/main"
install -m 600 "$BASELINE_ROOT/apps/mobile/android/app/build.gradle" "$APP_ROOT/build.gradle"
install -m 600 "$BASELINE_ROOT/apps/mobile/android/app/AndroidManifest.xml" "$APP_ROOT/src/main/AndroidManifest.xml"

printf 'RESTORED_BUILD_GRADLE_SHA256=%s\n' "$(sha256sum "$APP_ROOT/build.gradle" | awk '{print $1}')"
printf 'RESTORED_ANDROID_MANIFEST_SHA256=%s\n' "$(sha256sum "$APP_ROOT/src/main/AndroidManifest.xml" | awk '{print $1}')"
printf 'ROLLBACK_STATUS=restored_version_1_source_and_prior_app_link_configuration\n'
printf 'ROLLBACK_EXIT=0\n'
