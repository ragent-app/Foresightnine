# Local API-backed demo

This runbook starts the current MVP on loopback with the deterministic mock
provider. It is a reproducible development/demo path only: it does not use a
provider key, fetch live market data, or expose a public service.

## Start the API

From `D:\Foresightnine\src`, open a PowerShell window and run:

```powershell
$env:FORESIGHTNINE_CORS_ORIGINS = "http://127.0.0.1:5173"
uv run --project apps/api uvicorn app.main:app --app-dir apps/api --host 127.0.0.1 --port 8000
```

Keep this window running. In a second PowerShell window, verify:

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/health
```

The expected response identifies `deterministic_mock` and
`client-evidence-bundle`.

## Start the Web client against the API

In the second window, from the same directory, run:

```powershell
$env:VITE_FORESIGHTNINE_API_BASE_URL = "http://127.0.0.1:8000"
corepack pnpm --filter @foresightnine/web dev
```

Open <http://127.0.0.1:5173/>. The client now uses the local API for session,
challenge, evidence, confirmation and worldline requests. FM still executes in
the client Web Worker. If the API base variable is omitted, the Web client
uses its explicitly labelled offline/mock path instead.

## Boundaries

- Bindings are loopback-only; the demo is not reachable by other machines.
- The API uses deterministic mock research and the client evidence/search
  fallback. It must not be described as live DeepSeek/GLM or licensed market
  data.
- Do not put a provider key in a Vite variable, source file, Android bundle,
  or shell history. Production credentials belong in the server deployment
  secret manager and must pass `corepack pnpm verify:production-preflight`.
- Stop both processes with `Ctrl+C` when the demo is complete.

## Quick checks

```powershell
$web = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:5173/
$web.StatusCode
$web.Content -match 'id="root"'
```

The expected values are `200` and `True`.
