# ForesightNine API

FastAPI adapter for the canonical v1 JSON Schema. Pydantic models are generated at runtime, all model and request objects reject unknown fields, and deterministic mock outputs are schema/semantic validated. No provider key, network search, live market claim, broker connection or server-side final portfolio calculation exists in Phase 1.

## Phase 3 provider boundary

`DeterministicMockLlmProvider` is the default and needs no key. It exercises the complete Challenge/evidence path offline. `OpenCodeDeepSeekFlashProvider` is opt-in only with the non-secret server setting `FORESIGHTNINE_LLM_PROVIDER=opencode`; it invokes `opencode run --format json -m opencode-go/deepseek-v4-flash` and does not read, print, persist, or inject any credential. A deployment's new OpenCode secret must be supplied only by its server-side secret manager/environment to the CLI.

The live OpenCode path is bounded to one concurrent call, fixed round/token/cost budgets, timeout/cancel handling, and exponential retry for 429/5xx. Its JSON is rejected unless it can be rebuilt into and validated against the canonical `ChallengeRecord`. Client evidence URLs are never fetched by the API and receive protocol/length/private-IP guards; an evidence bundle without a URL is grade D and explicitly not independently reviewable.

From the workspace root:

```powershell
uv sync --project apps/api --frozen
uv run --project apps/api pytest apps/api/tests
uv run --project apps/api python apps/api/scripts/export_openapi.py --check
uv run --project apps/api uvicorn app.main:app --app-dir apps/api
```
