"""Generate deterministic Phase 5 fixtures from the read-only FactorMiner reference."""
from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

SOURCE_ROOT = Path(r"D:\FM acceleration-deepseek\bundle\agent-framework")
FIXTURE_PATH = Path(__file__).resolve().parents[2] / "packages" / "test-fixtures" / "fm" / "v1" / "core-parity.json"
sys.path.insert(0, str(SOURCE_ROOT / "FactorMiner"))
from factorminer.evaluation.metrics import compute_ic, compute_icir, compute_ic_mean  # noqa: E402


def finite_or_none(value: float) -> float | None:
    return float(value) if math.isfinite(float(value)) else None


def array(values: np.ndarray) -> list[float | None]:
    return [finite_or_none(value) for value in values.tolist()]


def matrix(values: np.ndarray) -> list[list[float | None]]:
    return [array(row) for row in values]


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def pandas_pct_rank(values: np.ndarray) -> np.ndarray:
    result = np.full(values.shape, np.nan, dtype=np.float64)
    valid = np.isfinite(values)
    result[valid] = pd.Series(values[valid]).rank(pct=True).to_numpy() - 0.5
    return result


def combine(factors: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(factors[0]["signal"])
    combined = np.zeros(n, dtype=np.float64)
    support = np.zeros(n, dtype=np.float64)
    contributions: dict[str, list[float | None]] = {}
    quality: dict[str, float] = {}
    used: list[int] = []
    for factor in factors:
        signal = np.asarray(factor["signal"], dtype=np.float64)
        valid = np.isfinite(signal)
        if valid.sum() < 2:
            continue
        ranked = pandas_pct_rank(signal)
        direction = 1.0 if factor["ic_mean"] >= 0 else -1.0
        contribution = np.full(n, np.nan, dtype=np.float64)
        contribution[valid] = direction * ranked[valid]
        weight = max(abs(factor["ic_mean"]), 1e-6) * max(abs(factor["icir"]), 1e-6)
        combined[valid] += contribution[valid] * weight
        support[valid] += weight
        contributions[str(factor["id"])] = array(contribution)
        quality[str(factor["id"])] = float(weight)
        used.append(factor["id"])
    eligible = support > 0
    combined[eligible] /= support[eligible]
    combined[~eligible] = np.nan
    return {"combined": array(combined), "support_weight": array(support), "contributions": contributions, "quality_weights": quality, "used_factor_ids": used}


def weights(combined: np.ndarray) -> dict[str, Any]:
    finite = np.flatnonzero(np.isfinite(combined))
    if len(finite) < 2:
        return {"z": array(np.full(combined.shape, np.nan)), "raw_weights": [1 / len(combined)] * len(combined), "fallback": True}
    centered = combined[finite] - np.mean(combined[finite])
    std = float(np.std(centered, ddof=0))
    if not math.isfinite(std) or std <= 1e-12:
        return {"z": array(np.full(combined.shape, np.nan)), "raw_weights": [1 / len(combined)] * len(combined), "fallback": True}
    z = np.full(combined.shape, np.nan)
    z[finite] = centered / std
    raw = np.zeros(combined.shape)
    raw[finite] = np.maximum(0.0, (1 + z[finite]) / len(finite))
    total = raw.sum()
    if total <= 1e-12:
        raw[:] = 1 / len(combined)
        return {"z": array(z), "raw_weights": array(raw), "fallback": True}
    raw /= total
    return {"z": array(z), "raw_weights": array(raw), "fallback": False}


def forecast_scale(closes: np.ndarray, horizon: int) -> dict[str, Any]:
    returns = closes[horizon:] / closes[:-horizon] - 1.0
    vols = []
    for row in returns:
        finite = row[np.isfinite(row)]
        vols.append(float(np.std(finite, ddof=0)) if len(finite) else math.nan)
    recent = np.asarray([value for value in vols[-252:] if math.isfinite(value)])
    scale = float(np.median(recent)) if len(recent) else 0.01
    if not math.isfinite(scale) or scale <= 0:
        scale = 0.01
    return {"cross_sectional_volatility": array(np.asarray(vols)), "return_scale": scale}


def rebalance() -> dict[str, float]:
    current = np.asarray([600.0, 400.0])
    target = np.asarray([0.4, 0.6])
    nav = 1000.0
    rate = 3.0 / 10_000.0
    post = nav
    for _ in range(100):
        transferred = float(np.maximum(current - post * target, 0.0).sum())
        cost = transferred * rate
        updated = nav - cost
        if abs(updated - post) <= max(1e-9, nav * 1e-12):
            post = updated
            break
        post = updated
    return {"post_trade_nav": post, "transferred": transferred, "cost": cost, "turnover": transferred / nav}


def product_amounts() -> dict[str, Any]:
    capital = 1000.0
    target = [0.5, 0.3, 0.2]
    prices = [100.0, 200.0, 50.0]
    fx = [1.0, 0.5, 2.0]
    lots = [1.0, 2.0, 1.0]
    fractional = [True, False, False]
    units, amounts = [], []
    for weight, price, rate, lot, allows_fraction in zip(target, prices, fx, lots, fractional):
        desired = capital * weight / (price * rate)
        actual = desired if allows_fraction else math.floor(desired / lot) * lot
        units.append(actual)
        amounts.append(actual * price * rate)
    return {"units": units, "allocated_amounts": amounts, "cash_remainder": capital - sum(amounts)}


def main() -> None:
    factors = [
        {"id": 10, "ic_mean": 0.20, "icir": 1.50, "admitted": True, "signal": [3.0, 1.0, 1.0, None, 5.0, 2.0]},
        {"id": 20, "ic_mean": -0.10, "icir": 2.00, "admitted": True, "signal": [None, 5.0, 4.0, 1.0, 1.0, 3.0]},
        {"id": 30, "ic_mean": 0.90, "icir": 0.0, "admitted": True, "signal": [None, None, None, None, None, None]},
        {"id": 40, "ic_mean": 0.50, "icir": 3.00, "admitted": False, "signal": [6.0, 5.0, 4.0, 3.0, 2.0, 1.0]},
    ]
    selected = sorted((factor for factor in factors if factor["admitted"]), key=lambda factor: abs(factor["ic_mean"]) * abs(factor["icir"]), reverse=True)[:10]
    top_ten_candidates = [{"id": identifier, "quality": identifier} for identifier in range(100, 111)]
    combined = combine(selected)
    composite = np.asarray([math.nan if value is None else value for value in combined["combined"]], dtype=np.float64)
    signal = np.asarray([[1, 2, 3], [2, 2, 1], [1, math.nan, 3], [4, 1, 2], [5, 3, 1]], dtype=np.float64)
    returns = np.asarray([[1, 2, 3], [3, 2, 1], [1, math.nan, 2], [1, 2, 4], [5, 4, 1]], dtype=np.float64)
    zero_close = np.full((4, 6), 100.0)
    canonical_case = {"a": 1, "nested": {"a": True, "b": [2, "x"]}, "z": None}
    payload = {
        "fixture_version": "fm-v1",
        "reference_sha256": {path.name: sha256(path.read_text(encoding="utf-8")) for path in [SOURCE_ROOT / "scheduler" / "fm_walk_forward.py", SOURCE_ROOT / "FactorMiner" / "factorminer" / "evaluation" / "metrics.py", SOURCE_ROOT / "FactorMiner" / "factorminer" / "evaluation" / "portfolio.py"]},
        "inputs": {"factors": factors, "max_factors": 10, "closes": matrix(zero_close), "forecast_horizon": 1, "ic_signal_assets_by_time": matrix(signal), "ic_returns_assets_by_time": matrix(returns)},
        "expected": {
            "selected_factor_ids": [factor["id"] for factor in selected],
            "top10_selection_ids": [entry["id"] for entry in sorted(top_ten_candidates, key=lambda entry: entry["quality"], reverse=True)[:10]],
            "combination": combined,
            "weights": weights(composite),
            "forecast_scale": forecast_scale(zero_close, 1),
            "forecast": array(((composite - np.nanmean(composite)) / np.nanstd(composite) * np.mean([abs(f["ic_mean"]) for f in selected if f["id"] in combined["used_factor_ids"]]) * 0.01)),
            "metrics": {"ic_series": array(compute_ic(signal, returns)), "ic_mean": compute_ic_mean(compute_ic(signal, returns)), "icir": compute_icir(compute_ic(signal, returns)), "too_few_icir": compute_icir(np.asarray([0.5, math.nan]))},
            "rebalance_3bps": rebalance(),
            "deadband_equal": {"expected_gain": 0.00015, "cost": 0.00015, "execute": False},
            "product_amounts": product_amounts(),
            "canonical": {"value": canonical_case, "json": canonical_json(canonical_case), "sha256": sha256(canonical_json(canonical_case))},
        },
    }
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(FIXTURE_PATH)


if __name__ == "__main__":
    main()
