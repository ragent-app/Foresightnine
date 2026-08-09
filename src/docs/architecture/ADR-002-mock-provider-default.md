# ADR-002: Default to a deterministic mock provider

- Status: accepted
- Date: 2026-08-10

## Decision

Phase 0/1 exposes provider interfaces for Challenge, Worldline generation, and Probability assessment, but wires only `MockResearchProvider`. It performs no network call and needs no key.

## Consequences

The API remains testable without secrets. Mock probability outputs use intervals and explicitly declare `model_estimate_unvalidated`; they are not forecasts or investment advice. A real provider may only be introduced behind the same validated interfaces and server-side secret injection.
