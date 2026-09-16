# Cline pool as the staging OpenAI-compatible provider

## Current deployment

The staging API selects the generic `openai_compatible` adapter and connects
to the host-local Cline key-pool gateway through Docker's
`host.docker.internal` bridge name. The gateway exposes standard
`/v1/chat/completions` and `/v1/models` routes, so the application does not
depend on a Cline-specific SDK or client protocol.

The current staging model is the pool-advertised
`deepseek/deepseek-v4-flash`. `glm-5.3-flash` remains the billing/model
fallback when no deployment model override exists; it is not claimed as the
active model while the Cline pool is selected.

## Server-only settings

The real API key is loaded into the ignored staging environment file from the
gateway's server-side access-token setting. It is never committed, returned by
the API, included in a client bundle, or written into this document.

```text
FORESIGHTNINE_LLM_PROVIDER=openai_compatible
FORESIGHTNINE_LLM_BASE_URL=http://host.docker.internal:10910/v1
FORESIGHTNINE_LLM_MODEL=deepseek/deepseek-v4-flash
FORESIGHTNINE_LLM_ALLOWED_HOSTS=host.docker.internal:10910
FORESIGHTNINE_LLM_ALLOW_PRIVATE_NETWORK=true
FORESIGHTNINE_LLM_API_KEY=<deployment secret>
```

`FORESIGHTNINE_LLM_ALLOW_PRIVATE_NETWORK=true` is an explicit staging-only
exception. The adapter accepts HTTP and private DNS only for the two Docker
host gateway names; public provider configurations still require HTTPS,
public DNS, and an explicit host allowlist.

## Verification

1. Keep `cline-gateway.service` active and verify its local health endpoint and
   advertised model list without printing credentials.
2. Rebuild and restart only the staging API after changing the provider
   settings.
3. Verify API container health and readiness.
4. Submit a bounded structured challenge through the adapter and check that
   the response is schema-validated and includes provider usage.
5. Check the account summary reports the configured Cline model, while quota
   reservation and settlement continue to use server-reported total tokens.

Production must use a managed HTTPS provider endpoint or a separately
reviewed private service network; this local bridge configuration is for the
current staging/test phase.
