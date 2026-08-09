# ADR-001: Versioned contracts are the integration boundary

- Status: accepted
- Date: 2026-08-10

## Decision

`packages/contracts/schemas/v1/contracts.schema.json` is the sole `v1` payload authority. Strict TypeScript transport types are generated from it. Pydantic models are compiled from it at runtime. FastAPI OpenAPI replaces framework-generated component schemas with the canonical definitions and is exported as a deterministic snapshot. API model/provider outputs pass the JSON Schema before return. Clients consume the versioned contract rather than provider-specific payloads.

## Consequences

Breaking changes require a new versioned schema path. Compatibility `research.schema.json` and `entities.schema.json` files contain references only, never duplicate definitions. `verify:generated-contracts` and OpenAPI resolver tests fail on source/artifact drift or dangling references.
