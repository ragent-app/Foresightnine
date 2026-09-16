#!/usr/bin/env bash
set -euo pipefail

BASELINE=/home/lxx/.config/foresightnine/rollback/problem-progress-20260915/problem.md
TARGET=/home/lxx/Foresightnine/src/docs/problem.md
if [[ "${1:-}" == "--local" ]]; then
  TARGET="${2:?usage: ROLLBACK.sh --local TARGET_FILE}"
fi
install -d -m 700 "$(dirname "$TARGET")"
install -m 600 "$BASELINE" "$TARGET"
printf 'RESTORED_SHA256=%s\n' "$(sha256sum "$TARGET" | awk '{print $1}')"
printf 'ROLLBACK_STATUS=restored_2026_09_10_authoritative_snapshot\n'
printf 'ROLLBACK_EXIT=0\n'
