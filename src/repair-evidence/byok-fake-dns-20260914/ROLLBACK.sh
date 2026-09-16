#!/usr/bin/env bash
set -euo pipefail
TARGET_ROOT="${1:-/home/lxx/Foresightnine/src}"
EVIDENCE_ROOT="/home/lxx/Foresightnine/src/repair-evidence/byok-fake-dns-20260914"
restore() {
  local baseline="$1" rel="$2" expected="$3" actual
  install -D -m 0644 "$EVIDENCE_ROOT/baseline/$baseline" "$TARGET_ROOT/$rel"
  actual="$(sha256sum "$TARGET_ROOT/$rel" | awk '{print $1}')"
  if [[ "$actual" != "$expected" ]]; then
    printf 'ROLLBACK_HASH_MISMATCH path=%s expected=%s actual=%s\n' "$rel" "$expected" "$actual" >&2
    return 1
  fi
  printf 'RESTORED %s SHA256=%s\n' "$rel" "$actual"
}
restore openai_compatible.py apps/api/app/openai_compatible.py 73ebfa2a8d215d4c3815251a4c100b14e7fc702a8e5d181841d30d04efb8f744
restore model_credentials.py apps/api/app/model_credentials.py c97972a25b1e78d9387b2731c7f88c5515258581acc992c85d113338b3fdd537
restore test_openai_compatible_provider.py apps/api/tests/test_openai_compatible_provider.py 221a526dd58d5bb4f95f27335ef79ed95a3cbf24cc62fe59ecfcccb9bbc4d0e1
restore test_model_credentials.py apps/api/tests/test_model_credentials.py 4aeab9affcbecb01971baff811e76881e577dbd97a30f9735e742cf8f93321c2
restore compose.yaml infra/docker/compose.yaml 87492c208dfd4ab75d3bd4f46d27fdf8b28258832d52dc0d2fffe4c7ab2db1da
printf 'ROLLBACK_STATUS=restored_original_files\n'
