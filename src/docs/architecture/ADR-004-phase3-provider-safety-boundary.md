# ADR-004: Phase 3 provider and client-evidence safety boundary

- Status: accepted
- Date: 2026-08-15

## Decision

Challenge generation now depends on the business-level `LlmProvider` abstraction. `DeterministicMockLlmProvider` is the default and is the only adapter exercised end-to-end in this repository. `OpenCodeDeepSeekFlashProvider` is an opt-in server process adapter for OpenCode CLI 1.3.0's known command form:

```text
opencode run --format json -m opencode-go/deepseek-v4-flash
```

The adapter neither reads nor passes a credential. A deployment may select it using the non-secret `FORESIGHTNINE_LLM_PROVIDER=opencode` setting; any OpenCode secret remains solely in that server process's secret-managed environment. It has one concurrent execution slot, bounded rounds/input/output/cost budgets, cancellation, timeout, and bounded exponential retry for 429/5xx failures.

Provider JSON is untrusted. It is parsed into a neutral draft, checked for the ten canonical rubric dimensions, attached-citation membership, D/E public-fact misuse, and prompt-control text, then rebuilt and validated as the existing canonical `ChallengeRecord`. Failed parsing or validation produces no challenge record.

Provider failures are recorded as typed `operation.error` events. The original idempotency key replays that failure rather than re-submitting the provider call; an explicit valid resume reuses the same operation checkpoint and, once successful, makes the original command replay the validated result.

`SearchEvidenceProvider` currently implements the client bundle fallback. URL-bearing evidence is HTTP(S)-only, length/port/user-info/private-IP guarded; the service does not fetch client URLs. A reusable redirect-target validator resolves and rejects unsafe redirect destinations before any future fetcher follows them. No-URL evidence is explicitly grade D and independently unreviewable.

## Consequences

No canonical schema, generated Pydantic model, TypeScript type, or OpenAPI artifact changed: the current evidence and challenge contracts already express the needed fields. The existing confirmation gate remains authoritative; a normal `CONFIRM` cannot bypass missing evidence, unresolved issues, total score, or five critical dimension thresholds.

Live DeepSeek verification is blocked by the absence of an authorized server secret in this environment. This must not be presented as live-provider validation; deterministic mock fixtures cover the Phase 3 acceptance path.
