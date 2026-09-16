#!/usr/bin/env bash
set -euo pipefail
TARGET=/home/lxx/Foresightnine/src
if [[ "${1:-}" == "--local" ]]; then
  TARGET="${2:?usage: ROLLBACK.sh --local TARGET_ROOT}"
fi
TARGET="$TARGET" python3 - <<'PY'
import os
from pathlib import Path
path=Path(os.environ['TARGET'])/'scripts/release/sign-android-aab-windows.ps1'
if path.exists(): path.unlink()
print('RESTORED_scripts/release/sign-android-aab-windows.ps1=MISSING')
PY
printf 'ROLLBACK_STATUS=restored_pre_helper_absence\n'
printf 'ROLLBACK_EXIT=0\n'
