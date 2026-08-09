# ForesightNine API

FastAPI adapter for the canonical v1 JSON Schema. Pydantic models are generated at runtime, all model and request objects reject unknown fields, and deterministic mock outputs are schema/semantic validated. No provider key, network search, live market claim, broker connection or server-side final portfolio calculation exists in Phase 1.

From the workspace root:

```powershell
uv sync --project apps/api --frozen
uv run --project apps/api pytest apps/api/tests
uv run --project apps/api python apps/api/scripts/export_openapi.py --check
uv run --project apps/api uvicorn app.main:app --app-dir apps/api
```
