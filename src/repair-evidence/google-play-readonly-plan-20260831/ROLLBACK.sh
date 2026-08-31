#!/usr/bin/env bash
set -euo pipefail
TARGET_FILE="${TARGET_FILE:-/home/lxx/Foresightnine/src/docs/release/google-play-developer-api-readonly-test-plan.md}"
python3 - "$TARGET_FILE" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1])
if p.exists() or p.is_symlink():
    p.unlink()
print(f'removed={p}')
PY
