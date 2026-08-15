# ADR-006: Client-only FM engine boundary

- Status: accepted for Phase 5
- Date: 2026-08-15

## Decision

The FM engine is a pure TypeScript package. It receives an immutable, caller-owned
market/factor snapshot, rejects observations whose `asOf` is after the computation
`cutoff`, and returns auditable intermediate arrays plus the raw client result. It
does not call FastAPI, read files, read credentials, or calculate a user portfolio
on the server.

The public orchestration entry point is `computeFm`. Factor expressions are a finite
discriminated-union AST: `field`, `constant`, `add`, `sub`, `mul`, `div`, `neg`,
`abs`, `rank`, `rolling_mean`, and `rolling_std`. Runtime validation rejects
unknown operators, extra properties, invalid windows, missing fields, and
non-finite constants. Evaluation is a switch visitor; `eval`, `Function`, dynamic
imports, and arbitrary code execution are not used. Division by zero produces the
engine's missing-value (`NaN`) result.

The browser Worker adapter accepts a typed compute message and posts a typed result
or a fixed, stack-free error. `mountFmWorker` only requires `postMessage` and
`onmessage`, so a `DedicatedWorkerGlobalScope` or deterministic fake port can mount
it. Core source has no Node or DOM imports and only uses ECMAScript built-ins,
`Float64Array`, and browser-compatible data structures; it is therefore importable
from React Native/Hermes. The Android proof is a 15-asset test of the same package
entry, not an Expo application.

## Formula and reference mapping

The six pinned read-only files are recorded in
`packages/test-fixtures/fm/v1/core-parity.json` with path-independent SHA-256
values and visible formula subsets:

| Reference | Client-bound formula or explicit scope |
| --- | --- |
| `scheduler/fm_walk_forward.py` | cutoff-visible expanding window, stable top-10 selection, rank-minus-0.5 signal, support-weighted composite, population z-score long-only weights, forecast scaling, fixed-point asset transfer, free initial allocation, 3 bps subsequent transfer, strict dead-band |
| `evaluation/combination.py` | equal-weight, positive-IC weighted, non-positive-IC equal fallback, and orthogonal shape behavior are captured as evaluation reference cases; `computeFm` uses the walk-forward rank/support subset |
| `evaluation/portfolio.py` | quintile, turnover, transaction-cost and IC/monotonicity cases are captured for audit; these are evaluation-only and are not exposed as user portfolio operations |
| `evaluation/metrics.py` | M-by-T Spearman IC, NaN omission, and sample ICIR minimum-valid-observation behavior |
| `evaluation/backtest.py` | train/test split, rolling split, and IC-stat cases are captured; split execution is a typed exclusion from `computeFm` because it is research evaluation, not client portfolio calculation |
| `ASSETS.yaml` | asset universe/FX/lot semantics are represented by the typed allocation boundary and fixture FX/lot cases; the full file remains read-only |

The engine preserves NaN as missing rather than converting missing signal support to
neutral zero. All-invalid and insufficient-support composites explicitly fall back
to equal weights and return reasons. Factor quality ties retain input/library order,
matching Python stable sorting rather than using factor ID as a tie-breaker.

## Oracle and verification

`scripts/fm/generate_oracle.py` hashes all six files and emits deterministic JSON
with null in place of non-finite values. In this checkout the full FactorMiner
package import is unavailable because the `uv` environment lacks `matplotlib` in
its dependency chain. The generator therefore reports the concrete import failure
to stderr and uses an explicitly labelled standard-library oracle whose formulas
were copied from the inspected references. The exception text is not persisted, so
`--check` is byte-identical across environments. `--require-reference-import`
remains the fail-closed command for an environment expected to provide the full
reference package.

## Consequences and limitations

- The fixture proves the declared formulas and source mapping; it is not a claim
  that every research-only helper in the six upstream files is a client API.
- The local oracle is verified against the pinned source hashes and the TypeScript
  parity cases available in this repository, but a full live reference import is
  still not possible in the minimal environment.
- The Worker adapter is an execution boundary, not a worker bundler. Applications
  must provide the worker module/build configuration in a later client phase.
- No Phase 6 UI or Phase 7 Expo surface is included.
