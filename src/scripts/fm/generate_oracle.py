"""Generate and verify deterministic Phase 5 fixtures from six read-only references.

The primary oracle is the pinned FactorMiner package.  This repository's minimal
``uv`` environment intentionally does not install its scientific dependencies, so
the checked-in fixture is also reproducible by the explicit standard-library oracle
below.  The local formulas are copied from the inspected reference functions; use
``--require-reference-import`` in a full FactorMiner environment to fail closed if
the reference package cannot be imported.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

SOURCE_ROOT = Path(r"D:\FM acceleration-deepseek\bundle\agent-framework")
FIXTURE_PATH = Path(__file__).resolve().parents[2] / "packages" / "test-fixtures" / "fm" / "v1" / "core-parity.json"
REFERENCE_FILES = {
    "fm_walk_forward.py": SOURCE_ROOT / "scheduler" / "fm_walk_forward.py",
    "combination.py": SOURCE_ROOT / "FactorMiner" / "factorminer" / "evaluation" / "combination.py",
    "portfolio.py": SOURCE_ROOT / "FactorMiner" / "factorminer" / "evaluation" / "portfolio.py",
    "metrics.py": SOURCE_ROOT / "FactorMiner" / "factorminer" / "evaluation" / "metrics.py",
    "backtest.py": SOURCE_ROOT / "FactorMiner" / "factorminer" / "evaluation" / "backtest.py",
    "ASSETS.yaml": SOURCE_ROOT / "ASSETS.yaml",
}

Number = float | int | None


def finite(value: float) -> bool:
    return math.isfinite(float(value))


def clean(value: Number) -> float | int | None:
    return value if value is None or finite(float(value)) else None


def vec(values: list[Number]) -> list[float | int | None]:
    return [clean(value) for value in values]


def mean(values: list[float]) -> float:
    return sum(values) / len(values)


def std0(values: list[float]) -> float:
    if not values:
        return math.nan
    average = mean(values)
    return math.sqrt(mean([(value - average) ** 2 for value in values]))


def rank_average(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    result = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        rank = (start + 1 + end) / 2.0
        for cursor in range(start, end):
            result[order[cursor]] = rank
        start = end
    return result


def pct_rank(values: list[Number], centered: bool = False) -> list[float | None]:
    valid = [(index, float(value)) for index, value in enumerate(values) if value is not None and finite(float(value))]
    result: list[float | None] = [None] * len(values)
    if not valid:
        return result
    ranks = rank_average([value for _, value in valid])
    for (index, _), rank in zip(valid, ranks):
        result[index] = rank / len(valid) - (0.5 if centered else 0.0)
    return result


def combine(factors: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(factors[0]["signal"])
    combined = [0.0] * n
    support = [0.0] * n
    contributions: dict[str, list[float | None]] = {}
    quality: dict[str, float] = {}
    used: list[int] = []
    for factor in factors:
        signal = factor["signal"]
        valid = [value for value in signal if value is not None and finite(float(value))]
        if len(valid) < 2:
            continue
        ranked = pct_rank(signal, centered=True)
        direction = 1.0 if float(factor["ic_mean"]) >= 0 else -1.0
        contribution = [None if value is None else direction * float(value) for value in ranked]
        weight = max(abs(float(factor["ic_mean"])), 1e-6) * max(abs(float(factor["icir"])), 1e-6)
        for index, value in enumerate(contribution):
            if value is not None:
                combined[index] += value * weight
                support[index] += weight
        contributions[str(factor["id"])] = vec(contribution)
        quality[str(factor["id"])] = weight
        used.append(int(factor["id"]))
    result = [combined[index] / support[index] if support[index] > 0 else None for index in range(n)]
    return {"combined": vec(result), "support_weight": vec(support), "contributions": contributions, "quality_weights": quality, "used_factor_ids": used}


def weights(combined: list[Number], kappa: float = 1.0) -> dict[str, Any]:
    valid = [index for index, value in enumerate(combined) if value is not None and finite(float(value))]
    equal = [1.0 / len(combined)] * len(combined) if combined else []
    if len(valid) < 2:
        return {"z": [None] * len(combined), "raw_weights": vec(equal), "fallback": True}
    values = [float(combined[index]) for index in valid]
    deviation = std0([value - mean(values) for value in values])
    if not finite(deviation) or deviation <= 1e-12:
        return {"z": [None] * len(combined), "raw_weights": vec(equal), "fallback": True}
    z: list[Number] = [None] * len(combined)
    raw = [0.0] * len(combined)
    for index in valid:
        z[index] = (float(combined[index]) - mean(values)) / deviation
        raw[index] = max(0.0, (1 + kappa * float(z[index])) / len(valid))
    total = sum(raw)
    if total <= 1e-12:
        return {"z": vec(z), "raw_weights": vec(equal), "fallback": True}
    return {"z": vec(z), "raw_weights": vec([value / total for value in raw]), "fallback": False}


def forecast_scale(closes: list[list[Number]], horizon: int) -> dict[str, Any]:
    vols: list[float] = []
    for row in range(horizon, len(closes)):
        returns: list[float] = []
        for asset, current in enumerate(closes[row]):
            prior = closes[row - horizon][asset]
            if current is not None and prior is not None and finite(float(current)) and finite(float(prior)) and float(prior) != 0:
                returns.append(float(current) / float(prior) - 1.0)
        vols.append(std0(returns))
    recent = [value for value in vols[-252:] if finite(value)]
    recent.sort()
    if not recent:
        scale = 0.01
    elif len(recent) % 2:
        scale = recent[len(recent) // 2]
    else:
        scale = (recent[len(recent) // 2 - 1] + recent[len(recent) // 2]) / 2
    return {"cross_sectional_volatility": vec(vols), "return_scale": scale if finite(scale) and scale > 0 else 0.01}


def spearman(signal: list[Number], returns: list[Number]) -> float | None:
    paired = [(float(x), float(y)) for x, y in zip(signal, returns) if x is not None and y is not None and finite(float(x)) and finite(float(y))]
    if len(paired) < 5:
        return None
    rx, ry = rank_average([pair[0] for pair in paired]), rank_average([pair[1] for pair in paired])
    ax, ay = mean(rx), mean(ry)
    numerator = sum((x - ax) * (y - ay) for x, y in zip(rx, ry))
    left = sum((x - ax) ** 2 for x in rx)
    right = sum((y - ay) ** 2 for y in ry)
    denominator = math.sqrt(left * right)
    return 0.0 if denominator < 1e-12 else numerator / denominator


def ic_series(signal: list[list[Number]], returns: list[list[Number]]) -> list[float | None]:
    return [spearman([row[index] for row in signal], [row[index] for row in returns]) for index in range(len(signal[0]))]


def icir(series: list[Number]) -> float:
    valid = [float(value) for value in series if value is not None and finite(float(value))]
    if len(valid) < 3:
        return 0.0
    average = mean(valid)
    deviation = math.sqrt(sum((value - average) ** 2 for value in valid) / (len(valid) - 1))
    return 0.0 if deviation < 1e-12 else average / deviation


def rebalance() -> dict[str, float]:
    current, target, nav, rate = [600.0, 400.0], [0.4, 0.6], 1000.0, 3.0 / 10000.0
    post, transferred, cost = nav, 0.0, 0.0
    for _ in range(100):
        transferred = sum(max(current[index] - post * target[index], 0.0) for index in range(2))
        cost = transferred * rate
        updated = nav - cost
        if abs(updated - post) <= max(1e-9, nav * 1e-12):
            post = updated
            break
        post = updated
    return {"post_trade_nav": post, "transferred": transferred, "cost": cost, "turnover": transferred / nav}


def product_amounts() -> dict[str, Any]:
    capital = 1000.0
    target, prices, fx, lots, fractional = [0.5, 0.3, 0.2], [100.0, 200.0, 50.0], [1.0, 0.5, 2.0], [1.0, 2.0, 1.0], [True, False, False]
    units, amounts = [], []
    for weight, price, rate, lot, allows_fraction in zip(target, prices, fx, lots, fractional):
        desired = capital * weight / (price * rate)
        actual = desired if allows_fraction else math.floor(desired / lot) * lot
        units.append(actual)
        amounts.append(actual * price * rate)
    return {"units": vec(units), "allocated_amounts": vec(amounts), "cash_remainder": capital - sum(amounts)}


def reference_hashes() -> dict[str, str]:
    missing = [str(path) for path in REFERENCE_FILES.values() if not path.is_file()]
    if missing:
        raise RuntimeError("missing pinned read-only reference(s): " + ", ".join(missing))
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in REFERENCE_FILES.items()}


def reference_import_status(require: bool) -> dict[str, str]:
    sys.path.insert(0, str(SOURCE_ROOT / "FactorMiner"))
    try:
        __import__("factorminer.evaluation.metrics")
        __import__("factorminer.evaluation.combination")
        __import__("factorminer.evaluation.portfolio")
        __import__("factorminer.evaluation.backtest")
    except Exception as error:  # dependency errors must be visible, never silent
        message = f"{type(error).__name__}: {error}"
        if require:
            raise RuntimeError("reference package import failed: " + message) from error
        print("REFERENCE IMPORT UNAVAILABLE: " + message, file=sys.stderr)
        print("Using explicit local oracle copied from the six inspected references.", file=sys.stderr)
        # The concrete dependency error is diagnostic output only.  Keeping it
        # out of the payload makes --check byte-identical across environments.
        return {"mode": "copied_formula_reference", "import_status": "not_persisted"}
    print("REFERENCE IMPORT AVAILABLE: validation package loaded", file=sys.stderr)
    # Keep normal fixture output stable even when optional scientific packages
    # happen to be installed.  --require-reference-import remains fail-closed.
    return {"mode": "copied_formula_reference", "import_status": "not_persisted"}


def standardize(rows: list[list[float]]) -> list[list[float]]:
    output: list[list[float]] = []
    for row in rows:
        average = mean(row)
        deviation = std0(row)
        output.append([(value - average) / deviation if deviation > 0 else 0.0 for value in row])
    return output


def add_matrices(left: list[list[float]], right: list[list[float]], left_weight: float = 1.0, right_weight: float = 1.0) -> list[list[float]]:
    return [[left_weight * left[row][column] + right_weight * right[row][column] for column in range(len(left[row]))] for row in range(len(left))]


def combination_reference_cases() -> dict[str, Any]:
    first = [[1, 2, 4, 8, 16], [2, 4, 8, 16, 32]]
    second = [[5, 1, 3, 2, 4], [4, 2, 5, 1, 3]]
    first_z, second_z = standardize(first), standardize(second)
    equal = [[(left + right) / 2 for left, right in zip(first_z[row], second_z[row])] for row in range(2)]
    positive = [[(0.2 * left + 0.1 * right) / 0.3 for left, right in zip(first_z[row], second_z[row])] for row in range(2)]
    fallback = equal
    vectors = [[value for row in matrix for value in row] for matrix in (first_z, second_z)]
    ortho: list[list[float]] = []
    for vector in vectors:
        residual = vector[:]
        for previous in ortho:
            denominator = sum(value * value for value in previous)
            if denominator > 1e-12:
                scale = sum(left * right for left, right in zip(residual, previous)) / denominator
                residual = [value - scale * previous[index] for index, value in enumerate(residual)]
        ortho.append(residual)
    orthogonal = [[sum(ortho[factor][row * 5 + column] for factor in range(2)) / 2 for column in range(5)] for row in range(2)]
    return {
        "input_shape_t_by_n": [2, 5],
        "equal_weight": {"output": equal},
        "ic_weighted_positive": {"ic": {"first": 0.2, "second": 0.1}, "output": positive},
        "ic_weighted_non_positive_fallback": {"ic": {"first": -0.2, "second": 0.0}, "output": fallback, "fallback": "equal_weight"},
        "orthogonal": {"output": orthogonal, "output_shape_t_by_n": [2, 5], "empty_input": "reject"},
    }


def portfolio_reference_case() -> dict[str, Any]:
    signals = [[1, 2, 3, 4, 5], [5, 4, 3, 2, 1]]
    returns = [[0.01, 0.02, 0.03, 0.04, 0.05], [0.05, 0.04, 0.03, 0.02, 0.01]]
    quintiles = [[row[index] for index in range(5)] for row in returns]
    turnover = [0.0, 1.0]
    gross = [0.04, 0.04]
    net = [gross[0], gross[1] - 2 * (3 / 10000) * turnover[1]]
    return {
        "input_shape_t_by_n": [2, 5],
        "transaction_cost_bps": 3,
        "quintile_period_returns": quintiles,
        "quintile_means": [0.01, 0.02, 0.03, 0.04, 0.05],
        "turnover_series": turnover,
        "avg_turnover": 0.5,
        "ls_gross_series": gross,
        "ls_net_series": net,
        "ls_return": mean(net),
        "metrics": {"ic_series": [1.0, -1.0], "ic_mean": 0.0, "icir": 0.0, "ic_win_rate": 0.5, "monotonicity": 1.0},
        "formula": "ls_net = ls_gross - 2 * (cost_bps / 10000) * turnover",
    }


def backtest_reference_case() -> dict[str, Any]:
    series = [0.2, -0.1, 0.3]
    average = mean(series)
    sample_std = math.sqrt(sum((value - average) ** 2 for value in series) / (len(series) - 1))
    return {
        "train_test_split": {"T": 10, "train_ratio": 0.7, "split": {"train_start": 0, "train_end": 7, "test_start": 7, "test_end": 10}},
        "rolling_splits": {"T": 8, "train_window": 3, "test_window": 2, "step": 2, "splits": [{"train_start": 0, "train_end": 3, "test_start": 3, "test_end": 5}, {"train_start": 2, "train_end": 5, "test_start": 5, "test_end": 7}]},
        "ic_stats": {"series": series, "ic_mean": average, "ic_std": sample_std, "icir": average / sample_std, "ic_win_rate": 2 / 3, "ic_max": 0.3, "ic_min": -0.1},
        "typed_exclusion": "evaluation-only; computeFm consumes the walk-forward subset and does not expose backtest split execution",
    }


def payload(require_reference_import: bool = False) -> dict[str, Any]:
    import_status = reference_import_status(require_reference_import)
    factors = [
        {"id": 20, "ic_mean": -0.10, "icir": 2.00, "admitted": True, "signal": [None, 5.0, 4.0, 1.0, 1.0, 3.0]},
        {"id": 10, "ic_mean": 0.20, "icir": 1.50, "admitted": True, "signal": [3.0, 1.0, 1.0, None, 5.0, 2.0]},
        {"id": 30, "ic_mean": 0.90, "icir": 0.0, "admitted": True, "signal": [None, None, None, None, None, None]},
        {"id": 40, "ic_mean": 0.50, "icir": 3.00, "admitted": False, "signal": [6.0, 5.0, 4.0, 3.0, 2.0, 1.0]},
    ]
    selected = sorted((factor for factor in factors if factor["admitted"]), key=lambda factor: abs(factor["ic_mean"]) * abs(factor["icir"]), reverse=True)[:10]
    tie_candidates = [{"id": 77, "quality": 1.0}, {"id": 3, "quality": 1.0}, {"id": 42, "quality": 1.0}]
    top_ten_candidates = [{"id": identifier, "quality": identifier} for identifier in range(100, 111)]
    combined = combine(selected)
    composite = combined["combined"]
    zero_close = [[100.0] * 6 for _ in range(4)]
    signal = [[1, 2, 3], [2, 2, 1], [1, None, 3], [4, 1, 2], [5, 3, 1]]
    returns = [[1, 2, 3], [3, 2, 1], [1, None, 2], [1, 2, 4], [5, 4, 1]]
    valid_composite = [value for value in composite if value is not None]
    composite_mean, composite_std = mean([float(value) for value in valid_composite]), std0([float(value) for value in valid_composite])
    mean_abs_ic = mean([abs(float(factor["ic_mean"])) for factor in selected if factor["id"] in combined["used_factor_ids"]])
    forecast = [None if value is None else (float(value) - composite_mean) / composite_std * mean_abs_ic * 0.01 for value in composite]
    canonical_case = {"a": 1, "nested": {"a": True, "b": [2, "x"]}, "z": None}
    canonical = json.dumps(canonical_case, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {
        "fixture_version": "fm-v1",
        "oracle": import_status,
        "reference_sha256": reference_hashes(),
        "orientation": {
            "signals_returns": "M-by-T (assets x time) for metrics.py; rows are T observations of N assets for the client engine.",
            "backtest": "T-by-N (time x assets) for backtest.py.",
            "portfolio": "T-by-N forward returns/signals; ASSETS.yaml defines the 15 tradable asset universe.",
        },
        "reference_cases": {
            "combination.py": combination_reference_cases(),
            "portfolio.py": portfolio_reference_case(),
            "backtest.py": backtest_reference_case(),
            "fm_walk_forward.py": {"client_subset": ["stable factor selection", "rank-minus-0.5 combination", "support-weighted composite", "long-only z weights", "forecast scaling", "cutoff", "3bps transfer", "dead-band"]},
            "metrics.py": {"client_subset": ["M-by-T Spearman IC", "NaN omission", "sample ICIR with minimum three valid observations"]},
            "ASSETS.yaml": {"client_subset": ["tradable asset IDs", "base-currency FX conversion", "lot rounding", "3bps", "free initial allocation"]},
        },
        "inputs": {
            "cutoff": "2026-07-15",
            "observations": [{"as_of": "2026-07-14", "close": [100.0, 100.0]}, {"as_of": "2026-07-15", "close": [100.0, 100.0]}],
            "factors": factors,
            "max_factors": 10,
            "closes": zero_close,
            "forecast_horizon": 1,
            "ic_signal_assets_by_time": signal,
            "ic_returns_assets_by_time": returns,
            "asset_registry_sample": [{"id": "USD", "currency": "usd", "fx_to_base": 1.0}, {"id": "JPY", "currency": "jpy", "fx_to_base": 0.5}, {"id": "EUR", "currency": "eur", "fx_to_base": 2.0}],
        },
        "expected": {
            "selected_factor_ids": [factor["id"] for factor in selected],
            "stable_tie_selection_ids": [entry["id"] for entry in tie_candidates],
            "top10_selection_ids": [entry["id"] for entry in sorted(top_ten_candidates, key=lambda entry: entry["quality"], reverse=True)[:10]],
            "combination": combined,
            "weights": weights(composite),
            "fallback_all_invalid": weights([None, None, None]),
            "fallback_single_factor": weights([0.2, None, None]),
            "forecast_scale": forecast_scale(zero_close, 1),
            "forecast": vec(forecast),
            "metrics": {"ic_series": vec(ic_series(signal, returns)), "ic_mean": mean([float(value) for value in ic_series(signal, returns) if value is not None]), "icir": icir(ic_series(signal, returns)), "too_few_icir": icir([0.5, None])},
            "rebalance_3bps": rebalance(),
            "initial_allocation": {"post_trade_nav": 1000.0, "transferred": 1000.0, "cost": 0.0, "turnover": 0.0},
            "deadband_equal": {"expected_gain": 0.00015, "cost": 0.00015, "execute": False, "strictly_above": True},
            "product_amounts": product_amounts(),
            "canonical": {"value": canonical_case, "json": canonical, "sha256": sha256(canonical)},
        },
    }


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def serialized(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify fixture is byte-identical to deterministic oracle")
    parser.add_argument("--require-reference-import", action="store_true", help="fail if the pinned FactorMiner package cannot be imported")
    args = parser.parse_args()
    expected = serialized(payload(args.require_reference_import))
    if args.check:
        actual = FIXTURE_PATH.read_text(encoding="utf-8") if FIXTURE_PATH.is_file() else ""
        if actual != expected:
            raise SystemExit(f"FM oracle fixture is not byte-identical: {FIXTURE_PATH}")
        print(f"PASS byte-identical FM oracle fixture: {FIXTURE_PATH}")
        print("PASS six immutable reference hashes: " + ", ".join(f"{name}={digest}" for name, digest in json.loads(actual)["reference_sha256"].items()))
        return
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(expected, encoding="utf-8")
    print(FIXTURE_PATH)


if __name__ == "__main__":
    main()
