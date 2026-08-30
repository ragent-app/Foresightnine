# Public market-provider assessment and adaptation plan

**Date:** 2026-08-30  
**Status:** owner-confirmed provider set configured for staged use; production traffic remains gated
**Scope:** the product runtime's three point-in-time anchors, not a historical
backtest warehouse

## Decision

Before the owner authorization recorded below, no currently verified source
satisfied all of the following at the same time:

1. no user or server key;
2. browser-compatible access for a public Web app;
3. coverage of the product's equity, ETF, commodity and cash proxies;
4. stable timestamps, currency and session semantics;
5. permission to redistribute the data to public users; and
6. the approved FM factor-feature contract.

The owner has now confirmed permission to use Stooq, Kenneth French, AQR QMJ,
and Frankfurter for this deployment. The Linux runtime therefore uses Stooq as
the sole configured price source and records the other three sources for their
distinct factor/FX contracts. Yahoo is not configured or used. The adapter
translates protocol and enforces the bundle contract; it does not calculate FM,
store a historical warehouse, or manufacture factor values.

## Owner authorization update — 2026-08-31

The deployment owner authorized the following source order:

| Canonical field | Primary | Fallback | Runtime boundary |
| --- | --- | --- | --- |
| price | Stooq | none currently approved | Thin server-side adapter, price-only three-anchor bundle |
| momentum | Kenneth French MOM | none | Source configured; no feature emitted until the exact client feature contract is wired |
| quality | AQR QMJ | Kenneth French RMW | RMW is a documented fallback proxy, not a renamed QMJ value |
| FX reference | Frankfurter, ECB preference | none | Reference rate only; no bid/ask or execution semantics |

For the same canonical field, the checked-in manifest resolves a conflict by
provider priority, then majority among equal-priority observations; an
unresolved tie is returned as missing. A source from another category is never
used as a substitute. The owner's confirmation is recorded as deployment input,
while production admission still requires the external release, security,
privacy, operations, TLS/HA, and OAuth gates.

## Read-only provider evidence

| Candidate | What was observed | Blocking issue | Decision |
| --- | --- | --- | --- |
| Yahoo Finance chart | PAVE, SOXX, XLU, GLD and VT returned HTTP 200 with daily rows in the Windows probe | No `Access-Control-Allow-Origin`; vendor GET protocol; prices only; no approved factor fields or redistribution approval | Closest price-only candidate behind a licensed adapter only |
| Alpha Vantage | Official docs expose daily equity/ETF history and technical-indicator endpoints | API key is required; free daily access is compact; vendor indicator names are not automatically the FM factor contract; public redistribution and exchange terms need approval | Server-secret alternative after license/limits review |
| Market Data | AAPL demo endpoint returned data without a token; PAVE, SOXX, XLU, GLD and VT returned HTTP 401 in the Windows probe | Official CORS policy limits self-service use to personal/non-public use and prohibits redistribution; target symbols are not unlocked | Not suitable for public production |
| biquote | Docs claim keyless/open CORS and OHLC; live health and symbol catalogue were reachable | Actual response had no CORS header; PAVE, SOXX, XLU, GLD and VT returned empty daily bars; usable examples are mainly FX/CFD | Not suitable for current ETF universe |
| Xoomar | Keyless CORS macro and ETF-flow endpoints returned JSON | No target ETF OHLC/close endpoint; ETF-flow data is crypto ETF-focused and cannot replace prices | Optional macro/evidence supplement only |
| Fear & Greed Chart | Keyless CORS JSON returned VIX, SPY, sector ETF and sentiment series, including XLU | Fixed curated universe; no PAVE/SOXX/GLD/VT coverage and no FM feature provenance | Optional risk-context supplement only |
| HF Market Data | Public page claims keyless stock/ETF daily bars | Windows request timed out at 10 seconds and legal/operational terms were not independently verified | Keep as an unapproved candidate for a later supervised probe |

The probe did not add any provider URL, key, proxy, cache or unlicensed data to
the application.

## Preserve the current client contract

Do **not** change `market-snapshot-bundle/v1` merely to match a vendor. The
vendor is the unstable boundary; the bundle is the stable client boundary.
Web and Android should continue to send a bounded request equivalent to:

```json
{
  "assetIds": ["ETF:SPY", "ETF:SOXX", "ETF:GLD"],
  "cutoff": "2026-08-30T00:00:00Z",
  "timezone": "UTC",
  "requestedAt": "2026-08-30T00:00:00Z"
}
```

The edge adapter returns exactly one row for every requested asset and each of
`year_start`, `latest_month_start` and `latest_completed_session`. When an
upstream row is unavailable it returns an explicit `missing` or `stale` row;
it must not omit the row or substitute a fixture.

## Thin adapter transformation

The production data path, if approved, is:

```text
client POST /snapshots
  -> allowlisted edge/server adapter
  -> vendor GET request(s), bounded to the requested anchors
  -> symbol/session/date normalizer
  -> canonical MarketSnapshotBundle v1
  -> client validator + integrity check
  -> Web Worker / Android local FM
```

The adapter should use a signed, versioned symbol manifest rather than infer
symbols from user text. The manifest must distinguish an economic exposure
from its tradable proxy, for example:

| Product asset ID | Yahoo-style vendor symbol | Notes |
| --- | --- | --- |
| `ETF:SPY` | `SPY` | tradable USD ETF proxy |
| `ETF:SOXX` | `SOXX` | semiconductor ETF proxy |
| `ETF:GLD` | `GLD` | gold ETF proxy, not spot gold |
| `ETF:QQQ` | `QQQ` | technology ETF proxy |
| `ETF:IEF` | `IEF` | Treasury ETF proxy, not a yield series |
| `CASH:USD` | no vendor symbol | preserve as client residual cash unless a cash/NAV source is approved |

The full mapping must be generated from the existing asset registry and must
never silently map an index, spot commodity or yield exposure to an unrelated
ETF. Each mapping records provider, exchange, currency, adjustment choice,
license scope and manifest version.

### Vendor field mapping

For a price-only daily bar:

| Canonical field | Source / rule |
| --- | --- |
| `assetId` | reverse lookup from the allowlisted provider symbol manifest |
| `anchor` | requested anchor whose selected session is the latest provider session on or before the anchor date |
| `asOf` | provider's actual exchange/session date, never the requested calendar date when no session occurred |
| `retrievedAt` | adapter retrieval time; reject if it is after the request cutoff |
| `timezone` | request timezone, after validating the vendor session timezone |
| `price`, `close` | the explicitly approved close variant, normally adjusted close for an ETF proxy |
| `currency` | vendor quote currency, checked against the manifest |
| `status` | `ok` only after all checks pass; otherwise `stale` or `missing` |
| `warnings` | source, adjustment, delayed/freshness and session-calendar caveats |
| `features` | absent or explicit nulls unless an independently approved feature source exists |

The adapter must reject malformed timestamps, duplicate asset/anchor rows,
future rows, negative/non-finite prices, unknown symbols, wrong currency and
payloads above the existing client limits. It must request only the three
anchor windows; it must not fill a long price history for production runtime.

## FM feature policy

Price history is not equivalent to the existing FM factor features. In
particular, a vendor's `RSI`, `SMA`, `quality`, `momentum` or similarly named
field must not be renamed into the FM fields without a method-level review.

There are only two acceptable paths:

1. **Approved feature feed:** a licensed source supplies the exact fields,
   units, as-of dates, revisions, missing-value semantics and method version.
   The adapter copies those values into `snapshot.features` with provenance
   warnings and tests the exact field names used by the FM AST.
2. **No approved feature feed:** return explicit missing feature cells. The
   client reports `limited_data`, keeps FM Raw unavailable for the affected
   computation, and disables Public Default. It may still display the price
   evidence and the reason for the limitation.

Do not derive momentum, quality, IC or ICIR from the three product anchors.
Three anchors are intentionally insufficient for the research metrics and
would change the meaning of the experiment. The existing offline factor
metadata remains explanatory research data, not a live feature feed.

## FX, cash and amount calculation

Price snapshots in mixed currencies need a separately approved FX contract.
Until that exists:

- keep the quote currency visible;
- do not pretend that USD, CNY, HKD, EUR, JPY and BTC prices are comparable;
- preserve cash as a client residual rather than inventing a cash NAV;
- disable executable-looking Public Default amounts when required FX or
  tradable-proxy data is missing; and
- keep share/lot calculation in the client after FM has produced a valid local
  result.

## Acceptance gates before any provider is enabled

The Linux agent may implement a staging adapter only after the owner supplies
the provider's written terms or confirms that the source permits the intended
public use. The gate requires:

1. symbol and exchange coverage for the enabled asset subset;
2. exact close/adjustment and currency semantics;
3. three-anchor and cutoff tests, including weekends and exchange holidays;
4. CORS or a controlled edge route without exposing a server secret;
5. rate-limit, timeout, retry and abuse-control behavior;
6. no persistent raw-data cache unless the license permits it;
7. feature and FX provenance, or explicit limited-data behavior;
8. redacted logs and no credentials in bundles, source maps, fixtures or
   client builds; and
9. Sol review of the evidence before a production hostname is enabled.

Until these gates pass, the correct runtime state is mock/offline or explicit
limited data. That is safer and more honest than presenting a price-only feed
as an FM-backed portfolio recommendation.

## Sources checked

- [Alpha Vantage API documentation](https://www.alphavantage.co/documentation/)
- [Nasdaq Data Link getting started](https://docs.data.nasdaq.com/v1.0/docs/getting-started)
- [Market Data CORS policy](https://www.marketdata.app/docs/api/cors/)
- [Market Data free-account limits](https://www.marketdata.app/docs/account/free-accounts/)
- [biquote API reference](https://biquote.io/docs/)
- [Xoomar developer page](https://xoomar.com/developers)
- [Xoomar market API catalogue](https://xoomar.com/markets/api)
- [Fear & Greed Chart API docs](https://feargreedchart.com/api-docs)
- [HF Market Data public API page](https://www.hfmarketdata.io/)
