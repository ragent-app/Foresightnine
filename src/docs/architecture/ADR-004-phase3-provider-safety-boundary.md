# ADR-004: Phase 3 provider and client-evidence safety boundary

- Status: accepted
- Date: 2026-08-15

## Decision

Challenge generation now depends on the business-level `LlmProvider` abstraction. `DeterministicMockLlmProvider` is the default and is the only adapter exercised end-to-end in this repository. `OpenCodeDeepSeekFlashProvider` is an opt-in server process adapter for OpenCode CLI 1.3.0's known command form:

```text
opencode run --format json -m opencode-go/deepseek-v4-flash
```

The adapter neither reads nor passes a credential. A deployment may select it using the non-secret `FORESIGHTNINE_LLM_PROVIDER=opencode` setting; any OpenCode secret remains solely in that server process's secret-managed environment. It has one concurrent execution slot, pre-submission cost reservation, bounded rounds/input/output/cost budgets, cancellation, timeout, and bounded exponential retry only for 429/5xx/timeout failures. Other non-zero CLI exits fail immediately.

Provider JSON is untrusted. It is parsed into a neutral draft, checked for the ten canonical rubric dimensions, attached-citation membership, D/E public-fact misuse, and prompt-control text, then rebuilt and validated as the existing canonical `ChallengeRecord`. Failed parsing or validation produces no challenge record.

Native search capability is detected at runtime from actual OpenCode JSON `capabilities`, `tools` and `search` envelope fields; a missing capability forces `native_search_used=false` and retains client fallback.

Provider work receives the actual `OperationManager` cancellation event registered before the call begins. A cancel command sets that event and produces the typed error event; no post-cancel draft is committed. Because the OpenCode CLI has no charge-status or provider idempotency receipt to query, resume preserves a cancelled/failed terminal checkpoint and never calls the provider again or fabricates a completion. The original idempotency key replays the same terminal result. A new billable attempt requires a user-explicit new command and idempotency key.

`SearchEvidenceProvider` validates the complete canonical-hashed client bundle: URL, title, publication/retrieval times, excerpt and user note. URL evidence requires a publication date and begins at grade C (reviewable, not independently verified). URL validation covers HTTP(S), length/port/user-info, decimal/abbreviated IPv4, IPv6 and IPv4-mapped private addresses. The service does not fetch client URLs. A reusable redirect-target validator resolves and rejects unsafe redirect destinations before any future fetcher follows them. No-URL evidence is explicitly grade D and independently unreviewable.

## Consequences

No canonical schema, generated Pydantic model, TypeScript type, or OpenAPI artifact changed: the current evidence and challenge contracts already express the needed fields. The existing confirmation gate remains authoritative; a normal `CONFIRM` cannot bypass missing evidence, unresolved issues, total score, five critical dimension thresholds, or an independently reviewable evidence item. Reservation confirmation remains available for unresolved/private evidence.

Live DeepSeek verification is blocked by the absence of an authorized server secret in this environment. This must not be presented as live-provider validation; deterministic mock fixtures cover the Phase 3 acceptance path.
