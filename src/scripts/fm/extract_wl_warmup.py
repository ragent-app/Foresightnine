"""Extract one auditable, cutoff-safe FM input from a read-only WL panel.

This development audit intentionally reads only complete cross-sectional dates at
or before the cutoff.  The WL panel's later online path is synthetic and is not
loaded into the emitted client input.

Run via:
  uv run --with pyarrow python scripts/fm/extract_wl_warmup.py --source D:\\WL-data-final\\panels\\WL1_full.parquet
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pyarrow.parquet as pq


def source_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_ratio(current: float, prior: float) -> float | None:
    if current <= 0 or prior <= 0:
        return None
    return current / prior - 1.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--cutoff", default="2026-07-15")
    parser.add_argument("--dates", type=int, default=64)
    arguments = parser.parse_args()
    if arguments.dates < 32:
        raise SystemExit("--dates must be at least 32 for the two factor lookbacks")
    if not arguments.source.is_file():
        raise SystemExit("source parquet is not available")

    rows = pq.read_table(arguments.source, columns=["asset_id", "date", "close"]).to_pydict()
    by_date: dict[str, dict[str, float]] = {}
    asset_ids: set[str] = set()
    for asset_id, date, close in zip(rows["asset_id"], rows["date"], rows["close"], strict=True):
        if date > arguments.cutoff or close is None or close <= 0:
            continue
        by_date.setdefault(date, {})[asset_id] = float(close)
        asset_ids.add(asset_id)

    ordered_assets = sorted(asset_ids)
    complete_dates = [date for date in sorted(by_date) if len(by_date[date]) == len(ordered_assets)]
    selected_dates = complete_dates[-arguments.dates:]
    if len(selected_dates) != arguments.dates:
        raise SystemExit("not enough complete cutoff-safe dates")
    if selected_dates[-1] != arguments.cutoff:
        raise SystemExit("cutoff is not a complete cross-sectional observation")

    observations: list[dict[str, object]] = []
    forward_returns: list[dict[str, object]] = []
    for index, date in enumerate(selected_dates):
        closes = [by_date[date][asset] for asset in ordered_assets]
        momentum = [
            safe_ratio(closes[asset_index], by_date[selected_dates[index - 20]][asset]) if index >= 20 else None
            for asset_index, asset in enumerate(ordered_assets)
        ]
        reversal = [
            -safe_ratio(closes[asset_index], by_date[selected_dates[index - 5]][asset]) if index >= 5 else None
            for asset_index, asset in enumerate(ordered_assets)
        ]
        observations.append({"asOf": date, "fields": {"close": closes, "momentum_20": momentum, "reversal_5": reversal}})
        next_date = selected_dates[index + 1] if index + 1 < len(selected_dates) else None
        forward_returns.append({
            "asOf": date,
            "values": [safe_ratio(by_date[next_date][asset], closes[asset_index]) if next_date else None for asset_index, asset in enumerate(ordered_assets)],
        })

    print(json.dumps({
        "schema_version": "wl-warmup-audit-v1",
        "source": {"path": str(arguments.source), "sha256": source_hash(arguments.source), "cutoff": arguments.cutoff, "synthetic_online_segment_used": False},
        "asset_ids": ordered_assets,
        "observations": observations,
        "forwardReturns": forward_returns,
    }, separators=(",", ":"), allow_nan=False))


if __name__ == "__main__":
    main()
