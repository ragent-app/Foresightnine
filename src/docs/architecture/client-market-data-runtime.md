# Client market-data runtime

This document records the implemented data boundary for the current product
slice. It is deliberately smaller than a backtest data warehouse.

## Request sequence

```text
user selects viewpoint + capital
        |
        v
client FM action
        |
        +--> request selected asset IDs + cutoff + UTC
        |
        +--> resolve year_start / latest_month_start /
             latest_completed_session
        |
        +--> mock | approved HTTPS public | single-use BYOK provider
        |
        +--> validate bundle: identity, dates, cutoff, prices,
             completeness, payload size and integrity hash
        |
        +--> map latest completed-session prices into local assets
        |
        +--> Web Worker / Android shared FM engine
```

The request contains at most 64 assets and exactly three rows per asset. A
provider must return explicit `missing` or `stale` rows when a value is not
available; the client does not manufacture a value from mock data. A limited
bundle remains visible as limited data and cannot be represented as a clean
live-data result.

## Provider modes

- `mock`: deterministic offline data for local QA and tests. It is the initial
  default when no build-time market provider is configured.
- `public`: an approved HTTPS endpoint, without an authorization header. An
  explicit public configuration with a missing/invalid URL fails closed; it
  never silently changes to mock.
- `byok`: a user-supplied HTTPS endpoint and key held in memory for one request.
  The key is placed only in that request's bearer header, never in the bundle,
  integrity hash, export, archive or runtime state. After success or failure,
  the source and key are cleared and the source cannot be reused.

Build-time public configuration:

```text
Web:     VITE_FORESIGHTNINE_MARKET_DATA_MODE=public
         VITE_FORESIGHTNINE_MARKET_DATA_BASE_URL=https://approved.example
Android: EXPO_PUBLIC_FORESIGHTNINE_MARKET_DATA_MODE=public
         EXPO_PUBLIC_FORESIGHTNINE_MARKET_DATA_BASE_URL=https://approved.example
```

No market-data key belongs in either client environment. BYOK UI and secure
credential-handling remain a separately approved client feature; the shared
adapter is already request-only so this feature can be added without moving
FM to Linux.

## Linux responsibility

Linux only needs to deploy the API, Cloudflare ingress, OAuth/account service,
PostgreSQL, Redis and—if separately approved—the HTTPS provider contract. It
does not need the Windows research fixture path, a full historical market-data
warehouse, or server-side FM. The server may provide a bounded response and
method/data metadata; final factor selection, signal, weights, costs, amounts,
shares and `Public Default` remain on the device.

The three anchors are not a backtest. They are a small current-context sample
for a user's chosen assets. A later full-history research audit may package a
separate cutoff-labelled fixture, but it must not become a production runtime
dependency or be silently substituted for current data.

## Implemented verification

The shared tests cover URL and bundle validation, future/missing/stale rows,
integrity, public failure without mock fallback, BYOK redaction and one-shot
use, runtime states, and Web/Android asset-key mapping. Web and Android both
invoke the shared runtime before local FM; Web retains the Worker boundary.
