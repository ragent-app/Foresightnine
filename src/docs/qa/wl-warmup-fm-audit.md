# WL1 real warm-up FM audit

Run date: 2026-08-24  
Command: `corepack pnpm --filter @foresightnine/fm-engine typecheck && node scripts/fm/audit_wl_warmup.mjs`

## Scope and data boundary

- Read-only source: `D:\WL-data-final\panels\WL1_full.parquet`
- Source SHA-256: `81f3315805d677b382935463dfa8b13985da3c0ccd56182be9607c5d71648d50`
- Decision cutoff: 2026-07-15
- Assets: 20 cross-sectional assets (`000300.SH`, `000688.SH`, BTC, CN10Y, COPPER, DXY, ETH, EURUSD, HSI, N225, NDX, SOX, SPX, SX5E, US10Y, USDCNY, USDJPY, VIX, WTI, XAU)
- The extractor selects only complete observations at or before the cutoff. It explicitly records `synthetic_online_segment_used: false`; it does not load the online worldline segment beginning 2026-07-17.

## Factor construction

The audit creates a cutoff-safe snapshot with two transparent price-derived fields:

1. 20-observation momentum: `close[t] / close[t-20] - 1`.
2. Five-observation reversal: `-(close[t] / close[t-5] - 1)`.

For each historical observation, the forward return is only the next complete cross-sectional date when that date is no later than the cutoff. The final date receives missing forward returns, preventing any use of post-cutoff prices. The TypeScript engine then computes the historical Spearman IC series, IC mean and ICIR used as factor metadata.

## Result

- Selected factors: 101 (momentum_20), 102 (reversal_5)
- Momentum IC mean / ICIR: `0.08190243049484172` / `0.24031534212180794`
- Reversal IC mean / ICIR: `-0.03243453461239308` / `-0.08622033319168466`
- Raw client weights sum to 1 within `1e-12`; no leverage or server-side portfolio computation occurred.
- Initial allocation cost: `0` (the separate 3 bps rebalance path remains covered by golden tests).
- Deterministic client result hash: `8ec7cccc74bea8c8e79869f5ddbeb4d37bf6a61893fab179b5323dceca22e6b1`

This is a reproducible development audit, not a current investable recommendation or a backtest performance claim. The asset registry, licences, trading eligibility, currency conversion and public-product constraint layer must still be applied before showing an investable public default.

## Re-run requirements

- The read-only source data path must be present.
- `uv` will obtain the temporary `pyarrow` reader package; it is not added to the product runtime.
- The shared FM TypeScript package must be built first through its normal `typecheck` command.
