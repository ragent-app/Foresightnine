# GLM Coding live smoke

This is a server-only, opt-in smoke for the existing OpenAI-compatible GLM
Coding adapter. It sends one synthetic challenge only when the server process
has `FORESIGHTNINE_GLM_CODING_API_KEY`; it never falls back to the mock provider.

## Fixed provider profile

- Official base endpoint: `https://open.bigmodel.cn/api/coding/paas/v4`
- Chat-completions request: `POST https://open.bigmodel.cn/api/coding/paas/v4/chat/completions`
- Model: `glm-5.3-flash`
- Server-owned generation controls remain in the existing adapter. The smoke
  has no CLI flags for endpoint, model, temperature, or `top_p` overrides.

## Execution

From `D:\Foresightnine\src`, inject the credential into the server process
environment (for example through the deployment secret manager), never into a
command line, prompt, source file, or test fixture:

```powershell
uv run --project apps/api python apps/api/scripts/glm_coding_live_smoke.py
```

The output is one structured, redacted JSON object. It contains only status,
the fixed model, an optional upstream request id, and validation metadata. It
does not print `Authorization`, the API key, response text, claims, reasoning
content, or evidence excerpts.

With no server secret, the known safe output is:

```json
{"model":"glm-5.3-flash","no_network_attempted":true,"status":"blocked","validation_summary":{"challenge_draft":false,"result":"secret_missing"}}
```

The command exits non-zero when blocked or when the provider fails. A live
success exits zero only after the adapter and smoke boundary validate a
`ChallengeDraft`, exactly ten rubric criteria, and the citation fields.

## Safety boundary

The live challenge is synthetic and declares no private evidence. Run it only
in an external security-approved environment with the server secret injected
by the environment's secret manager. A successful smoke proves one bounded
provider exchange and schema validation; it is not production authorization,
privacy approval, current market-data validation, or a substitute for the
external security review.
