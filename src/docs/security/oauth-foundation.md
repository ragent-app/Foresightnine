# OAuth foundation

The API contains a server-side OAuth boundary for Google and GitHub. Provider
requests remain server-only; the browser/mobile callback receives only a
short-lived opaque exchange ticket, and `POST /v1/auth/oauth/exchange` consumes
that ticket before issuing the local session. Bearer tokens and provider tokens
are never placed in redirect URLs.

Configuration is read from server environment variables only:

- `FORESIGHTNINE_OAUTH_CALLBACK_BASE_URL` — an HTTPS callback URL without a
  query or fragment.
- `FORESIGHTNINE_OAUTH_RETURN_URI_ALLOWLIST` — a comma-separated list or JSON
  array of exact `https://` web return URIs and/or `foresightnine://` deep links.
- `FORESIGHTNINE_OAUTH_GOOGLE_CLIENT_ID` and
  `FORESIGHTNINE_OAUTH_GOOGLE_CLIENT_SECRET`.
- `FORESIGHTNINE_OAUTH_GITHUB_CLIENT_ID` and
  `FORESIGHTNINE_OAUTH_GITHUB_CLIENT_SECRET`.

Each provider may be configured independently. Partial provider configuration,
missing configuration, non-HTTPS callbacks, malformed URIs, and unregistered
return URIs fail closed. Return URIs are compared as exact registered strings;
the foundation does not normalize them or implement an open redirect.

`OAuthFoundation.create_transaction` creates a bounded-TTL PKCE S256
transaction. State is keyed by a digest, while nonce and code verifier remain
server-side. When the existing storage configuration selects PostgreSQL, the
transaction and exchange-ticket records use the `oauth_foundation` collection,
database expiry metadata, and version-checked writes. State and tickets are
consumed by an atomic expected-version update, so a replay or a concurrent
second consumer fails closed across processes. Stored records contain no OAuth
client secret, authorization code, provider token or raw client secret.

Outside production/public hardening, the existing in-memory repository remains
the deterministic development/test fallback. If persistent storage is selected
but is missing, unavailable, or lacks the required repository operations, OAuth
configuration fails closed; the API does not silently fall back to memory.
