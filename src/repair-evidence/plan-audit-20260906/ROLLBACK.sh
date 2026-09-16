#!/bin/sh
set -eu
target=${1:?target path required}
baseline=${2:?baseline path required}
cp -- "$baseline" "$target"
if cmp -s "$baseline" "$target"; then
  printf '%s\n' 'ROLLBACK_RESULT=RESTORED'
  printf '%s\n' 'ROLLBACK_SCRIPT_EXIT_STATUS=0'
  printf '%s\n' 'ROLLBACK_RESTORED_BEHAVIOR=BASELINE_MATCH'
else
  printf '%s\n' 'ROLLBACK_RESULT=FAILED'
  exit 1
fi
