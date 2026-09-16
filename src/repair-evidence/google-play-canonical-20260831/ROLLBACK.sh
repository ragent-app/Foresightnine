#!/usr/bin/env bash
set -euo pipefail
TARGET=${1:?target copy required}
SOURCE=${2:?rollback source required}
cp -- "$SOURCE" "$TARGET"
printf 'ROLLBACK_RESULT=RESTORED\n'
