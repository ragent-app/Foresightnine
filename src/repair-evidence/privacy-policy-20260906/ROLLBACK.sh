#!/bin/sh
set -eu
root=${1:?target root required}
baseline=${2:?baseline artifact directory required}
mkdir -p "$root/src/apps/web/src/components" "$root/src/apps/web/src" "$root/src/apps/web/public" "$root/src/apps/mobile/app" "$root/src/apps/mobile/src"
cp -- "$baseline/BASELINE_AppShell.tsx" "$root/src/apps/web/src/components/AppShell.tsx"
cp -- "$baseline/BASELINE_i18n.tsx" "$root/src/apps/web/src/i18n.tsx"
cp -- "$baseline/BASELINE_web-styles.css" "$root/src/apps/web/src/styles.css"
cp -- "$baseline/BASELINE_mobile-console.tsx" "$root/src/apps/mobile/app/console.tsx"
cp -- "$baseline/BASELINE_mobile-i18n.ts" "$root/src/apps/mobile/src/i18n.ts"
cp -- "$baseline/BASELINE_mobile-ui.tsx" "$root/src/apps/mobile/src/ui.tsx"
rm -f "$root/src/apps/web/public/privacy.html"
printf '%s\n' 'ROLLBACK_RESULT=RESTORED'
printf '%s\n' 'ROLLBACK_SCRIPT_EXIT_STATUS=0'
printf '%s\n' 'ROLLBACK_RESTORED_BEHAVIOR=PRIVACY_PAGE_ABSENT_AND_APP_LINKS_BASELINE'
