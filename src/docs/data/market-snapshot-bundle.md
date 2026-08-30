# Client market snapshot data plane

## Two data planes

The Research Data Plane contains the offline factor library, factor expressions,
factor IC/ICIR metadata, research references, and historical material used to
explain a method. It is not a product-runtime market feed.

The Product Runtime Data Plane is the bounded client-side
`MarketSnapshotBundle` in `@foresightnine/market-data`. It carries only user-selected
asset IDs and three point-in-time anchors. Web and Android use the same validator,
canonical serialization, integrity hash, and FM-input conversion. The final FM
calculation remains local in the existing Web Worker/Android client path; the
server is not used for that calculation.

## Anchors and limits

Every requested asset has one explicit row for each anchor:

- `year_start`: January 1 of the cutoff year, rolled back to Friday when it is a
  weekend;
- `latest_month_start`: the first day of the cutoff month, with the same weekend
  handling;
- `latest_completed_session`: the cutoff calendar date when it is a weekday, or
  the preceding Friday on a weekend.

These are calendar anchors. The adapter does not claim exchange-holiday certainty;
the provider owns the returned canonical trading/session date in `asOf`.
Returned observations after `cutoff`, malformed dates, unknown assets, duplicate
asset/anchor rows, invalid prices, unsafe provider URLs, and oversized payloads are
rejected. No rolling 60/120/252-day window is requested by default.

Rows may explicitly be `stale` or `missing`. They remain visible for auditability,
but conversion returns `limited_data` and never substitutes fixture values. A
limited bundle is not sufficient for a normal FM run when required prices or factor
features are absent.

## Provider policy

Mode A (`public`) uses a configured approved HTTPS JSON endpoint and sends no
authorization header. Mode B (`byok`) accepts a user key in memory for one request,
constructs the authorization header inside that request, and keeps the key out of
the bundle, integrity input, logs, and redacted serialization. The package does not
guess a vendor protocol: both modes use the documented generic bounded JSON adapter.

If a future source requires a server secret or forbids client distribution, it must
be configured out of this adapter rather than placing a secret in a client build.
With no configured provider, the product remains in explicit deterministic
mock/offline mode.

## Metrics limitation

The bundle is a point-in-time product slice, not a backtest system. It does not
contain forward returns. The local FM input therefore preserves the supplied offline
factor metadata and leaves FM metrics (`IC`/`ICIR`) null; it does not recompute them
from three snapshots.
