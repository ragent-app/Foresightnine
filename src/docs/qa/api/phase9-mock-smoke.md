# Phase 9 API mock smoke evidence

Captured on 2026-08-29 against the local API at `http://127.0.0.1:8765` from the current Phase 8 implementation checkout. This is a local deterministic-mock smoke test, not a live provider, production, or licensed market-data test.

## Observed responses

| Request | Result | Observation |
| --- | --- | --- |
| `GET /health` | HTTP 200 | `status=ok`, `provider=deterministic_mock`, `search_provider=client-evidence-bundle`, `project_search_available=false`, `schema_version=v1` |
| `GET /openapi.json` | HTTP 200 | 76,927-byte OpenAPI response served by FastAPI |
| `POST /v1/sessions` with `client_locale=zh-CN` and an idempotency key | HTTP 201 | A typed anonymous session was created; the returned identifier is intentionally omitted from this report |
| `POST /v1/challenges` with a valid session, thesis, capital, currency and privacy flag | HTTP 201 | A typed `CHALLENGING` challenge was created; the returned identifier is intentionally omitted from this report |
| `POST /v1/sessions` with unsupported `capital` / `base_currency` fields | HTTP 422 | Strict validation returned `error_code=validation_error`; no permissive extra fields were accepted |

The mock health clock is intentionally fixed for deterministic provider fixtures. Runtime session/operation retention uses a separate lifecycle clock, as covered by the Phase 8 security tests. No Authorization header, provider secret, user API key, raw prompt, or private evidence was used or written.

## Reproduction

From `D:\Foresightnine\src`:

```powershell
uv run --project apps/api uvicorn app.main:app --app-dir apps/api --host 127.0.0.1 --port 8765
```

In a second shell, issue the requests described above with a fresh idempotency key. Then run:

```powershell
uv run --project apps/api pytest apps/api/tests
```

The API does not perform the final portfolio calculation. FM remains a client-side computation in the Web Worker/shared TypeScript engine and Android Hermes path.

## Release boundary

This evidence proves local contract and mock-service reachability only. It does not prove GLM5.3 success, native search availability, PostgreSQL/Redis deployment, CAPTCHA operation, current licensed market data, production HTTPS/CSP, legal approval, or public traffic readiness.
