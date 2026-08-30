# Linux agent handoff: production data and deployment gates

This is guidance for the Linux deployment agent. It is not permission to alter
the approved plan or to enable an unverified market provider.

## What Windows has completed

- Web and Android keep the stable `market-snapshot-bundle/v1` contract.
- The client requests only three point-in-time anchors and performs FM locally.
- Missing/stale prices, missing factor cells and missing FX keep Public Default
  disabled while preserving transparent FM Raw/limited-data state.
- Yahoo, Stooq, Market Data, Alpha Vantage and Nasdaq were rechecked. No tested
  source satisfies all of: target coverage, browser/mobile access, exact FM
  features, stable session semantics and rights to redistribute in a public
  free-plus-subscription product.
- Android OAuth is wired to the server redirect/ticket flow. Production mobile
  builds target an HTTPS App Link; custom scheme is local-only.

## Linux execution order

1. **Persistent runtime first.** Deploy FastAPI behind Cloudflare/TLS with
   PostgreSQL and the approved persistent operation/session/identity stores.
   Keep Redis or the approved abuse-control store durable enough for rate-limit
   semantics. Run health/readiness, migration, backup/restore and restart
   tests before opening public traffic.
2. **OAuth configuration second.** Inject provider client IDs/secrets,
   callback base URL and exact return-URI allowlist only through the server
   environment/secret manager. Configure Google and GitHub provider consoles to
   the same callback URL. Never copy credentials into Git, images, logs,
   OpenAPI, frontend bundles or source maps.
3. **Android App Link third.** Serve
   `/.well-known/assetlinks.json` from the public domain with the actual release
   package and release certificate SHA-256. The URI must match the mobile build,
   API allowlist and provider console exactly. Verify the link after installing
   the signed APK; do not accept a custom-scheme-only production flow.
4. **Market data rights before wiring.** Obtain written permission for the
   intended public display and redistribution. If no permission exists, expose
   only the mock/limited-data state. If a source is approved, add a thin
   allowlisted adapter that normalizes vendor responses into `v1`, emits all
   three rows per asset, rejects future/duplicate/malformed rows, limits rate
   and payload size, and records source/adjustment/currency/freshness metadata.
5. **Factor/FX gate after prices.** Do not fill `momentum`, `quality`, IC, ICIR
   or FX by inference from three prices. Only emit factor features when an
   approved source supplies the exact field semantics and rights. Otherwise
   keep the client in `limited_data` and Public Default off.
6. **Public release gates last.** Run secret scan, dependency audit, SSRF and
   input validation checks, privacy/retention review, Cloudflare WAF/rate-limit
   checks, TLS checks, OAuth replay/logout/deletion tests, and a clean restart
   with an empty cache. The public first version may be free, while a future
   subscription can be added only through a reviewed billing module; neither
   state authorizes market-data redistribution or brokerage execution.

## Do not do

- Do not replace the client contract with Yahoo/Alpha Vantage/Market Data JSON.
- Do not proxy or cache data whose terms do not allow redistribution.
- Do not move FM calculation to the server.
- Do not enable Public Default merely because an HTTP 200 or CORS header exists.
- Do not add server-side billing, broker connectivity, order execution or funds
  custody in this deployment slice.

## Required handoff evidence

Return the deployment commit, environment variable names (never values), public
health/readiness response, migration and backup evidence, OAuth callback and
App Link evidence, provider rights decision, and a list of remaining gates.
Ask Windows to rerun client/browser/Android validation only after the public API
base and exact return URI are available.
