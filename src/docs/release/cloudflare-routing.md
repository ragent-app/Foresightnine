# Cloudflare staging route contract

The current Tunnel is `foresightnine-api-staging`. The host topology is
deliberately split so that a Web catch-all cannot make an API or metrics probe
look successful.

| Host | Route | Destination/contract |
| --- | --- | --- |
| `app.foresightnine.com` | `/api/*`, `/oauth/*`, `/v1/*`, `/health`, `/readyz`, `/docs`, `/openapi.json` | API on loopback port 8000 |
| `app.foresightnine.com` | `/oauth/complete` and Web catch-all | Web on loopback port 4173 |
| `app.foresightnine.com` | `/metrics` | 404; metrics remain local-only |
| `api-staging.foresightnine.com` | all except `/metrics` | API on loopback port 8000 |
| `api-staging.foresightnine.com` | `/metrics` | 404; metrics remain local-only |
| `staging.foresightnine.com` | `/oauth/mobile-callback` and Web routes | Web on loopback port 4173 |
| `staging.foresightnine.com` | `/api/*`, `/v1/*`, `/health`, `/readyz`, `/metrics` | 404; use `api-staging` for API calls |

The current factor library API uses the explicit versioned paths:

```text
GET /v1/factor-libraries/v1.0.0/manifest
GET /v1/factor-libraries/v1.0.0/shards?shard_id=<manifest shard id>
```

`/v1/factor-libraries/manifest` is intentionally not an alias and returns
404.

HTTP-to-HTTPS is an edge concern, separate from Tunnel ingress. The current
policy is Zone-global `always_use_https=on`; Cloudflare returns HTTP 301 for
`app.foresightnine.com`, `staging.foresightnine.com`, and
`api-staging.foresightnine.com` before the request reaches Tunnel ingress,
preserving host, path, and query string. The supplied host key was used to
enable it. No DNS, billing, or paid resource change was made.
