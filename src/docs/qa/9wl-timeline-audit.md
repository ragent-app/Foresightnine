# 9WL timeline and probability audit

Run date: 2026-08-24  
Command: `corepack pnpm audit:worldline-fixtures`

## What is checked

- All nine narrative files and all nine stage-news files still match the SHA-256 recorded by the regression fixture.
- Every fixture trigger occurs verbatim in the associated read-only narrative.
- The fixture reveal date equals the matching stage-two news-visible date.
- Every event is after the 2026-07-17 baseline and before the scenario horizon.
- `leak` rules have `reveal_date < event_date`; `observed` rules have `reveal_date >= event_date`.
- Every weak/main/strong branch retains its source event/reveal fields; conditional masses sum to one; scalar and interval joint probabilities recompute from the recorded premise probabilities.

## Result

The audit passes for WL1–WL9. It verifies provenance and internal temporal/probability semantics, including the explicit leak/observed distinction. It does **not** assert that any future event, price path, NAV or simulated allocation will happen.

## Portfolio/NAV interpretation

The product may use 9WL as a regression template only. Future price paths, simulated NAVs and scenario holdings must keep their `synthetic` / `unvalidated` labels, and current-executable allocations remain blocked by licensed current market-data and legal-release gates. The separate [WL1 real warm-up FM audit](wl-warmup-fm-audit.md) is the evidence that the shared client FM engine can consume a cutoff-safe historical panel without future leakage; it is not a return backtest claim.
