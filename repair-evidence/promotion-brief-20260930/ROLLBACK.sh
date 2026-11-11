#!/usr/bin/env bash
set -euo pipefail
target="${1:?usage: ROLLBACK.sh PATH_TO_COPY}"
rm -f -- "$target"
