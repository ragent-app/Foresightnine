#!/usr/bin/env bash
set -o pipefail
E=/home/lxx/Foresightnine/src/repair-evidence/android-billing-release-v2-20260915
cd /home/lxx/Foresightnine/src/apps/mobile/android
APKSIGNER=$(find /home/lxx/Android/Sdk/build-tools -name apksigner -type f | sort -V | tail -1)
RAW_FP=$($APKSIGNER verify --print-certs /tmp/foresightnine-play-v1-universal.apk | sed -n 's/^Signer #1 certificate SHA-256 digest: //p')
FP=$(printf '%s' "$RAW_FP" | sed 's/../&:/g;s/:$//' | tr '[:lower:]' '[:upper:]')
export ANDROID_HOME=/home/lxx/Android/Sdk
export ANDROID_SDK_ROOT=/home/lxx/Android/Sdk
export NODE_ENV=production
export FORESIGHTNINE_ENV=production
export EXPO_PUBLIC_FORESIGHTNINE_API_BASE_URL=https://api-staging.foresightnine.com
export EXPO_PUBLIC_FORESIGHTNINE_MARKET_DATA_MODE=public
export EXPO_PUBLIC_FORESIGHTNINE_MARKET_DATA_BASE_URL=https://api-staging.foresightnine.com
export EXPO_PUBLIC_FORESIGHTNINE_OAUTH_RETURN_URI=https://app.foresightnine.com/oauth/complete
export EXPO_PUBLIC_FORESIGHTNINE_OAUTH_APP_LINK_HOST=app.foresightnine.com
export EXPO_PUBLIC_FORESIGHTNINE_OAUTH_APP_LINK_PACKAGE=com.foresightnine.mobile
export EXPO_PUBLIC_FORESIGHTNINE_OAUTH_APP_LINK_SHA256_CERT_FINGERPRINT="$FP"
export EXPO_PUBLIC_FORESIGHTNINE_OAUTH_APP_LINK_VERIFIED=true
./gradlew --no-daemon :app:bundleRelease > "$E/bundle-release-detached.output.txt" 2>&1
RC=$?
printf '%s\n' "$RC" > "$E/bundle-release-detached.exit.txt"
exit "$RC"
