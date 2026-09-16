#!/usr/bin/env bash
set -euo pipefail
TARGET_ROOT="${1:-/home/lxx/Foresightnine/src}"
TARGET_ENV="${2:-$TARGET_ROOT/infra/docker/staging.env}"
EVIDENCE_ROOT="/home/lxx/Foresightnine/src/repair-evidence/challenge-structure-ux-20260914"
SECRET_BASELINE="/home/lxx/.config/foresightnine/rollback/challenge-structure-ux-20260914/staging.env"
restore() {
  local baseline="$1" rel="$2" expected="$3" actual
  install -D -m 0644 "$EVIDENCE_ROOT/baseline/$baseline" "$TARGET_ROOT/$rel"
  actual="$(sha256sum "$TARGET_ROOT/$rel" | awk '{print $1}')"
  [[ "$actual" == "$expected" ]] || { printf 'ROLLBACK_HASH_MISMATCH path=%s\n' "$rel" >&2; return 1; }
  printf 'RESTORED %s SHA256=%s\n' "$rel" "$actual"
}
restore types.ts apps/web/src/types.ts 42ee30b4ea1242368f330ed09db99404b0334f8a817470285367c1c88a5e9334
restore api-runtime.ts apps/web/src/api-runtime.ts af89c1e6f3128d342d07536aa69d9dd3bb1c1ab52a4229b41ca703b53132a273
restore ChallengePanel.tsx apps/web/src/components/ChallengePanel.tsx 93b5db00442e53c7d32772b2bfd79bd5bffa9bdb92802b07fd4a29db851ef732
restore styles.css apps/web/src/styles.css 32eb9fda77e5cdc3b9be843b257a2786b04c3d49cf309ddcfdd395f7d270b86e
restore api-runtime.error.test.ts apps/web/src/api-runtime.error.test.ts 7c88637e63e398cd1a241a6232826b8a3a7762c936c5cbdb7c1478cf7d4a264a
restore challenge-panel.interaction.test.ts apps/web/src/challenge-panel.interaction.test.ts ec22e3a73df6409a74476423a47fecbedf65cf953f36fdba4f886fcea8722f00
restore openai_compatible.py apps/api/app/openai_compatible.py 76f5542e2233abb59ad1e8bf81f8e0e6b6a2d734defdc117318b9c21cb1c7823
restore phase3.py apps/api/app/phase3.py bef0aa336dce7eb70ae9e98f8bb75f9639c4f1ab1c25a711b809570b03f652c4
restore test_openai_compatible_provider.py apps/api/tests/test_openai_compatible_provider.py 9282491f26a6383c8b21ca7441007290d4cb243d9538e1c4c627161bee9c6677
restore test_phase3_providers.py apps/api/tests/test_phase3_providers.py fa2af2c24bccaee735831e0251e5a5db4c26e993a0108e7442991d5b75442342
install -D -m 0600 "$SECRET_BASELINE" "$TARGET_ENV"
env_hash="$(sha256sum "$TARGET_ENV" | awk '{print $1}')"
[[ "$env_hash" == "f1521b5b1a6548f39179b6a03b5dd3c4a1e47522fe19aa8276a7abd19b034af2" ]] || { printf 'ROLLBACK_HASH_MISMATCH path=infra/docker/staging.env\n' >&2; exit 1; }
printf 'RESTORED infra/docker/staging.env SHA256=%s MODE=%s\n' "$env_hash" "$(stat -c %a "$TARGET_ENV")"
printf 'ROLLBACK_STATUS=restored_original_files_and_provider_selector\n'
