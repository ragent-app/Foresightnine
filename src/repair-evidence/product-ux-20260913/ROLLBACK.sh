#!/usr/bin/env bash
set -euo pipefail

TARGET_ROOT="${1:-/home/lxx/Foresightnine/src}"
EVIDENCE_ROOT="/home/lxx/Foresightnine/src/repair-evidence/product-ux-20260913"

restore() {
  local source="$1" destination="$2"
  mkdir -p "$(dirname "$TARGET_ROOT/$destination")"
  rm -f "$TARGET_ROOT/$destination"
  install -m 0644 "$EVIDENCE_ROOT/$source" "$TARGET_ROOT/$destination"
}

restore baseline/apps/web/src/App.tsx apps/web/src/App.tsx
restore baseline/src/apps/web/src/components/ChallengePanel.tsx apps/web/src/components/ChallengePanel.tsx
restore baseline/src/apps/web/src/styles.css apps/web/src/styles.css
restore baseline/src/apps/web/src/types.ts apps/web/src/types.ts
restore baseline/src/apps/web/src/state.ts apps/web/src/state.ts
restore baseline/src/apps/web/src/app.test.ts apps/web/src/app.test.ts
restore baseline/apps/web/src/fixtures.ts apps/web/src/fixtures.ts
restore baseline/apps/web/src/exports.ts apps/web/src/exports.ts
restore baseline/apps/web/src/api-runtime.ts apps/web/src/api-runtime.ts
restore baseline/apps/web/src/market-config-client.ts apps/web/src/market-config-client.ts
restore baseline/apps/web/src/market-config-client.test.ts apps/web/src/market-config-client.test.ts
restore baseline/apps/web/src/MarketDataConfigPanel.tsx apps/web/src/components/MarketDataConfigPanel.tsx
restore baseline/apps/web/src/methods-guide.ts apps/web/src/methods-guide.ts
restore baseline/apps/web/src/methods-guide.test.ts apps/web/src/methods-guide.test.ts
restore baseline/apps/web/src/components/MethodsPanel.tsx apps/web/src/components/MethodsPanel.tsx
restore baseline/apps/api/app/main.py apps/api/app/main.py
restore baseline/apps/api/app/operations.py apps/api/app/operations.py
restore baseline/apps/api/app/operation_store.py apps/api/app/operation_store.py
restore baseline/apps/api/app/billing.py apps/api/app/billing.py
restore baseline/apps/api/app/openai_compatible.py apps/api/app/openai_compatible.py
restore baseline/apps/api/app/market_provider.py apps/api/app/market_provider.py
restore baseline/apps/api/app/phase3.py apps/api/app/phase3.py
restore baseline/apps/api/app/market_credentials.py apps/api/app/market_credentials.py
restore baseline/apps/api/tests/test_billing.py apps/api/tests/test_billing.py
restore baseline/apps/api/tests/test_api.py apps/api/tests/test_api.py
restore baseline/apps/api/tests/test_market_credentials.py apps/api/tests/test_market_credentials.py
restore baseline/apps/api/tests/test_market_provider.py apps/api/tests/test_market_provider.py
restore baseline/apps/api/tests/test_openai_compatible_provider.py apps/api/tests/test_openai_compatible_provider.py
restore baseline/packages/api-client/src/client.ts packages/api-client/src/client.ts
restore baseline/infra/docker/compose.yaml infra/docker/compose.yaml
restore baseline/infra/docker/staging.env.example infra/docker/staging.env.example
restore baseline/apps/api/app/storage.py apps/api/app/storage.py
restore baseline/apps/api/tests/test_operations.py apps/api/tests/test_operations.py
restore baseline/apps/api/tests/test_storage.py apps/api/tests/test_storage.py
restore baseline/apps/api/tests/test_phase3_providers.py apps/api/tests/test_phase3_providers.py

rm -f \
  "$TARGET_ROOT/apps/web/src/challenge-panel.interaction.test.ts" \
  "$TARGET_ROOT/apps/web/src/api-runtime.error.test.ts" \
  "$TARGET_ROOT/apps/web/src/model-config-client.ts" \
  "$TARGET_ROOT/apps/web/src/model-config-client.test.ts" \
  "$TARGET_ROOT/apps/web/src/components/ModelConfigPanel.tsx" \
  "$TARGET_ROOT/apps/api/app/model_credentials.py" \
  "$TARGET_ROOT/apps/api/tests/test_model_credentials.py" \
  "$TARGET_ROOT/apps/api/tests/test_provider_failure_http.py"

verify_pair() {
  local source="$1" destination="$2"
  test "$(sha256sum "$EVIDENCE_ROOT/$source" | cut -d' ' -f1)" = "$(sha256sum "$TARGET_ROOT/$destination" | cut -d' ' -f1)"
}

verify_pair baseline/apps/web/src/App.tsx apps/web/src/App.tsx
verify_pair baseline/src/apps/web/src/components/ChallengePanel.tsx apps/web/src/components/ChallengePanel.tsx
verify_pair baseline/src/apps/web/src/styles.css apps/web/src/styles.css
verify_pair baseline/src/apps/web/src/types.ts apps/web/src/types.ts
verify_pair baseline/src/apps/web/src/state.ts apps/web/src/state.ts
verify_pair baseline/src/apps/web/src/app.test.ts apps/web/src/app.test.ts
verify_pair baseline/apps/web/src/fixtures.ts apps/web/src/fixtures.ts
verify_pair baseline/apps/web/src/exports.ts apps/web/src/exports.ts
verify_pair baseline/apps/web/src/api-runtime.ts apps/web/src/api-runtime.ts
verify_pair baseline/apps/web/src/market-config-client.ts apps/web/src/market-config-client.ts
verify_pair baseline/apps/web/src/market-config-client.test.ts apps/web/src/market-config-client.test.ts
verify_pair baseline/apps/web/src/MarketDataConfigPanel.tsx apps/web/src/components/MarketDataConfigPanel.tsx
verify_pair baseline/apps/web/src/methods-guide.ts apps/web/src/methods-guide.ts
verify_pair baseline/apps/web/src/methods-guide.test.ts apps/web/src/methods-guide.test.ts
verify_pair baseline/apps/web/src/components/MethodsPanel.tsx apps/web/src/components/MethodsPanel.tsx
verify_pair baseline/apps/api/app/main.py apps/api/app/main.py
verify_pair baseline/apps/api/app/operations.py apps/api/app/operations.py
verify_pair baseline/apps/api/app/operation_store.py apps/api/app/operation_store.py
verify_pair baseline/apps/api/app/billing.py apps/api/app/billing.py
verify_pair baseline/apps/api/app/openai_compatible.py apps/api/app/openai_compatible.py
verify_pair baseline/apps/api/app/market_provider.py apps/api/app/market_provider.py
verify_pair baseline/apps/api/app/phase3.py apps/api/app/phase3.py
verify_pair baseline/apps/api/app/market_credentials.py apps/api/app/market_credentials.py
verify_pair baseline/apps/api/tests/test_billing.py apps/api/tests/test_billing.py
verify_pair baseline/apps/api/tests/test_api.py apps/api/tests/test_api.py
verify_pair baseline/apps/api/tests/test_market_credentials.py apps/api/tests/test_market_credentials.py
verify_pair baseline/apps/api/tests/test_market_provider.py apps/api/tests/test_market_provider.py
verify_pair baseline/apps/api/tests/test_openai_compatible_provider.py apps/api/tests/test_openai_compatible_provider.py
verify_pair baseline/packages/api-client/src/client.ts packages/api-client/src/client.ts
verify_pair baseline/infra/docker/compose.yaml infra/docker/compose.yaml
verify_pair baseline/infra/docker/staging.env.example infra/docker/staging.env.example
verify_pair baseline/apps/api/app/storage.py apps/api/app/storage.py
verify_pair baseline/apps/api/tests/test_operations.py apps/api/tests/test_operations.py
verify_pair baseline/apps/api/tests/test_storage.py apps/api/tests/test_storage.py
verify_pair baseline/apps/api/tests/test_phase3_providers.py apps/api/tests/test_phase3_providers.py

for removed in \
  apps/web/src/challenge-panel.interaction.test.ts \
  apps/web/src/api-runtime.error.test.ts \
  apps/web/src/model-config-client.ts \
  apps/web/src/model-config-client.test.ts \
  apps/web/src/components/ModelConfigPanel.tsx \
  apps/api/app/model_credentials.py \
  apps/api/tests/test_model_credentials.py \
  apps/api/tests/test_provider_failure_http.py; do
  test ! -e "$TARGET_ROOT/$removed"
done

printf 'ROLLBACK_RESULT=PASS\nROLLBACK_HASH_MATCH=PASS restored=35 removed=8\nTARGET_ROOT=%s\nRESTORED_BEHAVIOR=pre-product-UX challenge, evidence-hash/export, provider-network, provider-fallback, market-config, model-config, guide, ownership/account-switch, challenge-mutation concurrency, operation-transaction, and billing-reservation behavior\n' "$TARGET_ROOT"
