#!/usr/bin/env bash
set -euo pipefail
TARGET_FILE="${TARGET_FILE:-/home/lxx/.config/google-play/service-account.json}"
python3 - "$TARGET_FILE" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
if p.exists() or p.is_symlink():
    p.unlink()
print(f'removed={p}')
PY
