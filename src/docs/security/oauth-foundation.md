# OAuth foundation

The API contains a pure server-side foundation for Google and GitHub OAuth. It
does not make provider requests and does not expose OAuth endpoints, callback
handlers, token exchange, user-token storage, or authenticated sessions yet.

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

`OAuthFoundation.create_transaction` creates a short-lived, one-use PKCE S256
transaction. Cryptographic state, nonce, and code verifier values are stored in
the server-side in-memory transaction store. The public authorization request
contains the state and code challenge needed by a future provider redirect, but
does not contain the nonce or verifier. `validate_and_consume` atomically
validates state and provider, rejects expiry or mismatch, and returns the
server-only values for a future callback/token-exchange implementation.

The in-memory store is intentionally only a foundation. A production deployment
will need an appropriate shared, expiring server-side store before running more
than one API process.

