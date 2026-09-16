#!/usr/bin/env bash
set -euo pipefail
TARGET=/home/lxx/Foresightnine/src
if [[ "${1:-}" == "--local" ]]; then
  TARGET="${2:?usage: ROLLBACK.sh --local TARGET_ROOT}"
fi
TARGET="$TARGET" python3 - <<'PY'
import os
from pathlib import Path
root=Path(os.environ['TARGET'])
for relative in ('apps/api/scripts/google_play_internal_release.py','apps/api/tests/test_google_play_internal_release.py'):
    path=root/relative
    if path.exists():
        path.unlink()
    print(f'RESTORED_{relative}=MISSING')
PY
printf 'ROLLBACK_STATUS=restored_pre_tool_absence\n'
printf 'ROLLBACK_EXIT=0\n'
