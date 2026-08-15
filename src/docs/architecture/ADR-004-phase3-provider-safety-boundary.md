# ADR-004: Phase 3 provider and client-evidence safety boundary

- Status: accepted
- Date: 2026-08-15

## Decision

Challenge generation now depends on the business-level `LlmProvider` abstraction. `DeterministicMockLlmProvider` is the default and is the only adapter exercised end-to-end in this repository. `OpenCodeDeepSeekFlashProvider` is an opt-in server process adapter for OpenCode CLI 1.3.0's known command form:

```text
opencode run --format json -m opencode-go/deepseek-v4-flash
```

The adapter neither reads nor passes a credential. A deployment may select it using the non-secret `FORESIGHTNINE_LLM_PROVIDER=opencode` setting; any OpenCode secret remains solely in that server process's secret-managed environment. It has one concurrent execution slot, pre-submission cost reservation, bounded rounds/input/output/cost budgets, cancellation, timeout, and bounded exponential retry only for 429/5xx/timeout failures. Other non-zero CLI exits fail immediately.

OpenCode 1.3.0's `--format json` output is treated as raw JSONL rather than a single JSON envelope. The adapter accepts the legacy single-envelope fixture shape only for compatibility, parses every JSONL event, accumulates the final strict structured text/payload and final usage record, and derives native-search provenance from actual `tool_use.part.tool` events (`search`/`websearch`). `no`, `unavailable` and `error` tool statuses never establish availability.

Provider JSON is untrusted. It is parsed into a neutral draft, checked for the ten canonical rubric dimensions, attached-citation membership, D/E public-fact misuse, and prompt-control text (including untrusted attached-evidence text), then rebuilt and validated as a canonical `ChallengeRecord`. `citation_ids`, `public_fact_citation_ids`, `native_search_used` and server-derived `provenance` are required versioned fields in that record. Failed parsing or validation produces no challenge record.

Native search capability is detected at runtime from actual OpenCode JSON `capabilities`, `tools` and `search` envelope fields; a missing capability forces `native_search_used=false` and retains client fallback.

Provider work receives the actual `OperationManager` cancellation event registered before the call begins. The opt-in `POST /v1/challenges/{challenge_id}/messages/async` endpoint returns a typed running operation immediately; its events endpoint is a `StreamingResponse` that replays typed events, sends SSE keepalive heartbeats while work remains running, and ends on typed complete/error. A cancel command sets the same runner Event and produces the typed error event; no post-cancel draft is committed. Synchronous endpoints retain their existing contracts.

`OperationManager.execute` uses an in-memory atomic single-flight key of endpoint plus idempotency key. Concurrent same-body calls wait for and replay the first terminal response; a different body remains a 409 conflict. This prevents duplicate provider submission and state mutation within one API process. Because the OpenCode CLI has no charge-status or provider idempotency receipt to query, resume preserves a cancelled/failed terminal checkpoint and never calls the provider again or fabricates a completion. A new billable attempt requires a user-explicit new command and idempotency key.

`SearchEvidenceProvider` validates the complete canonical-hashed client bundle: URL, title, publication/retrieval times, excerpt and user note. URL evidence requires a publication date and begins at grade C (reviewable, not independently verified). URL validation covers HTTP(S), length/port/user-info, decimal/abbreviated IPv4, IPv6 and IPv4-mapped private addresses. The service does not fetch client URLs. A reusable redirect-target validator resolves and rejects unsafe redirect destinations before any future fetcher follows them. No-URL evidence is explicitly grade D and independently unreviewable.

## Consequences

The canonical schema, generated Pydantic model, TypeScript type and OpenAPI snapshot change together when audit fields or the asynchronous endpoint change. The existing confirmation gate remains authoritative; a normal `CONFIRM` cannot bypass missing evidence, unresolved issues, total score, five critical dimension thresholds, or an independently reviewable evidence item. Once an evidence-backed subsequent message draft has neither missing evidence nor unresolved issues, it returns to `CHALLENGING`, so ordinary confirmation is not unconditionally blocked. Reservation confirmation remains available for unresolved/private evidence.

Live DeepSeek verification is blocked by the absence of an authorized server secret in this environment. This must not be presented as live-provider validation; deterministic mock fixtures cover the Phase 3 acceptance path.
