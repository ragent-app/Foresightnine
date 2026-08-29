# GLM Coding Plan MCP search smoke

This is a server-only, opt-in smoke for the project-search adapter. The
credential is read from `FORESIGHTNINE_GLM_CODING_API_KEY` in the server
process environment and is never accepted as a CLI argument, written to the
repository, or included in output.

## Fixed protocol boundary

- Endpoint: `https://open.bigmodel.cn/api/mcp/web_search_prime/mcp`
- Protocol: MCP Streamable HTTP, JSON-RPC initialize → initialized notification
  → `tools/list` → `tools/call`
- Search tool: the adapter calls the exact advertised tool name. The current
  Coding Plan response advertises `web_search_prime`; the historical
  `webSearchPrime` spelling remains a strict fixture-compatibility alias.
- Output: candidates only. The adapter validates HTTPS URLs, bounds query and
  response sizes, and does not convert a search candidate into evidence or an
  LLM fact without the normal user review/submission path.

## Execution

From `D:\Foresightnine\src`, inject the secret through an approved server
secret manager and run:

```powershell
uv run --project apps/api python apps/api/scripts/glm_mcp_live_smoke.py
```

The smoke prints only protocol metadata: status, candidate count, HTTPS
validation, and the count of dated candidates. It never prints source titles,
URLs, excerpts, response bodies, session identifiers, or credentials. With no
secret it exits blocked without making a network request.

## Evidence and current host limitation

The credentialed run in the current Windows development host was attempted
without printing the secret. The default transport failed closed: the chat
smoke returned `provider_transport_error` and the MCP smoke returned
`search_provider_unavailable`. The host's DNS maps the official hostname to a
reserved `198.18/15` address; the production SSRF guard correctly rejects that
mapping. This is an environment limitation, not evidence that the provider
contract is valid for public deployment.

Separately, a one-off diagnostic transport (bypassing only that local DNS
mapping, without changing the production guard) delivered the real provider
responses to the existing adapters. The GLM response passed the structured
challenge parser with ten rubric items, three questions, one validated
citation, and filtered reasoning content. The real MCP exchange passed the
full handshake and search parser with three HTTPS candidates. Those diagnostic
results prove wire/parser compatibility only; an approved ordinary-DNS
deployment must pass the default smoke before live-provider enablement.
