# Public market provider recheck

## Result

No currently tested source is safe to configure as the public production
provider for the ForesightNine product. The closest technical source is a
CORS-enabled OHLC endpoint, but it still does not provide the approved FM
feature fields and its public-use terms must be reviewed before redistribution.

The application therefore keeps the stable `market-snapshot-bundle/v1`
contract, displays price evidence only when a source is explicitly enabled, and
keeps FM/Public Default limited when factor cells, FX, freshness or rights are
missing. No key, proxy, cache or provider URL was added to runtime defaults.

## Windows probe and official-documentation evidence

All requests below were made from the Windows development environment with an
`Origin: https://staging.foresightnine.com` request header.

| Source | Observation | Decision |
| --- | --- | --- |
| Yahoo Finance chart | The earlier probe returned HTTP 200 daily prices for PAVE, SOXX, XLU, GLD and VT, but no CORS header, no project `POST /snapshots` protocol, and no factor features. | Do not wire directly. A licensed server adapter could normalize price rows only. |
| Stooq CSV | HTTP 200 was an anti-automation verification HTML page, not CSV. | Treat as unavailable; do not parse or bypass the challenge. |
| Market Data API | `https://api.marketdata.app/v1/stocks/candles/D/AAPL?countback=10` returned HTTP 203 with `Access-Control-Allow-Origin: *`; target ETF coverage needs authorization. Its official CORS policy says self-service browser use is personal/non-public and public redistribution requires a commercial license. | Candidate for user-BYOK or licensed adapter only. |
| Alpha Vantage | The official daily endpoint with demo credentials returned HTTP 200 and CORS `*`. The documented interface requires an API key for normal use; free access is rate/coverage constrained and its technical indicators are not automatically the ForesightNine FM feature contract. | Candidate for user-BYOK or server-secret commercial review only. |
| Nasdaq chart API | SPY chart returned HTTP 200 JSON but no CORS header. | Server-side research candidate only, subject to rights and protocol review. |
| Twelve Data | Official ETF material advertises broad historical ETF coverage, but the API quickstart requires an API key. Its terms/support material defines providing data to third parties as redistribution and says redistribution requires a separate agreement. This is an official-documentation review, not a Windows HTTP/CORS probe. | Licensing candidate based on documented ETF coverage; not a public/free default and not an anonymous direct client source. |
| Finnhub | Official API documentation requires a token; stock candles are marked Premium and ETF profile/holdings endpoints are also Premium. The response shape is OHLC/profile data, not the approved factor/FX feature contract. This is an official-documentation review, not a Windows HTTP/CORS probe. | Candidate for a licensed or explicit user-BYOK adapter only; no production wiring. |

Official references: [Alpha Vantage API documentation](https://www.alphavantage.co/documentation/), [Alpha Vantage market-data policy](https://www.alphavantage.co/realtime_data_policy/), [Market Data API CORS policy](https://www.marketdata.app/docs/api/cors/), [Market Data API overview](https://www.marketdata.app/docs/api/), [Twelve Data ETF coverage](https://twelvedata.com/etf), [Twelve Data API documentation](https://twelvedata.com/docs/etfs/etfs-list), [Twelve Data redistribution guidance](https://support.twelvedata.com/en/articles/5332349-commercial-and-personal-usage), [Finnhub API documentation](https://finnhub.io/docs/api/quote).

## Closest safe adaptation

Do not make the vendor response the application contract. Instead implement a
thin adapter at the unstable boundary:

```text
vendor GET/JSON/CSV
  -> allowlisted symbol and exchange manifest
  -> bounded session/date normalizer
  -> three explicit anchors per requested asset
  -> MarketSnapshotBundle v1
  -> client validator and integrity hash
  -> local FM only when exact feature cells are present
```

For price-only OHLC, the adapter may emit `price`, `close`, `asOf`, currency,
retrieval time, source warnings and `status`. It must not rename RSI, SMA,
technical momentum or vendor quality fields to the experiment's `momentum` or
`quality` fields. It must emit `features` as absent/null and return
`limited_data` until an independently licensed feed supplies the exact factor
fields, units, revision semantics and as-of dates.

The minimal request remains the three anchors already used by the client:

```json
{
  "assetIds": ["ETF:SPY", "ETF:SOXX", "ETF:GLD"],
  "anchors": {
    "year_start": "2026-01-01",
    "latest_month_start": "2026-07-31",
    "latest_completed_session": "2026-08-28"
  },
  "cutoff": "2026-08-31T00:00:00Z",
  "timezone": "UTC"
}
```

An Alpha Vantage or Market Data BYOK adapter can fetch a bounded OHLC window
to identify the latest session on or before each anchor, but it must not claim
that this window recreates IC/ICIR or the existing historical FM experiment.
Those metrics require the approved factor/return data and remain offline
metadata in the current product.

The same boundary applies to Twelve Data and Finnhub. Their documented ETF
coverage makes them reasonable candidates for a paid, server-side licensing
track, but an API key or a Premium label is not evidence of redistribution
rights. Until written rights and exact feature semantics are confirmed, their
responses may be used only in an isolated adapter test or an explicitly
user-supplied/BYOK path, never as the public default.

## Acceptance gate

Before any source is enabled for public traffic, obtain written rights for the
intended free/paid product, verify every target asset and currency, validate
weekends and exchange holidays, enforce timeout/rate limits, reject future and
duplicate rows, redact logs, and have Sol review the evidence. If any gate is
missing, use mock/offline data or explicit limited-data mode. A technically
reachable CORS endpoint is not by itself permission to redistribute market
data.
