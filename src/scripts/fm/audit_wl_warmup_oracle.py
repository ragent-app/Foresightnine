"""Independent Python oracle for the cutoff-safe WL1 warm-up audit.

This file intentionally does not import the TypeScript FM package.  It reads
the JSON emitted by ``extract_wl_warmup.py`` and re-computes the bounded FM
surface used by the client: stable selection, average-tie percentile ranks,
support-weighted signals, IC/ICIR, volatility scale, long-only weights,
forecast scaling, and the free initial allocation.  The companion Node audit
compares its result with the real TypeScript engine.
"""
from __future__ import annotations

from datetime import date
import json
import math
import sys
from typing import Any


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def mean(values: list[float]) -> float:
    return sum(values) / len(values)


def std0(values: list[float]) -> float:
    if not values:
        return math.nan
    average = mean(values)
    return math.sqrt(mean([(value - average) ** 2 for value in values]))


def rank_average(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    output = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        rank = (start + 1 + end) / 2.0
        for index in order[start:end]:
            output[index] = rank
        start = end
    return output


def percent_rank(values: list[Any], centered: bool = False) -> list[float | None]:
    valid = [(index, float(value)) for index, value in enumerate(values) if finite(value)]
    output: list[float | None] = [None] * len(values)
    if not valid:
        return output
    ranks = rank_average([value for _, value in valid])
    for (index, _), rank in zip(valid, ranks):
        output[index] = rank / len(valid) - (0.5 if centered else 0.0)
    return output


def spearman(signal: list[Any], returns: list[Any]) -> float | None:
    paired = [(float(left), float(right)) for left, right in zip(signal, returns, strict=True) if finite(left) and finite(right)]
    if len(paired) < 5:
        return None
    left = rank_average([pair[0] for pair in paired])
    right = rank_average([pair[1] for pair in paired])
    left_mean, right_mean = mean(left), mean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right, strict=True))
    left_sum = sum((x - left_mean) ** 2 for x in left)
    right_sum = sum((y - right_mean) ** 2 for y in right)
    denominator = math.sqrt(left_sum * right_sum)
    return 0.0 if denominator < 1e-12 else numerator / denominator


def ic_series(signal: list[list[Any]], returns: list[list[Any]]) -> list[float | None]:
    # The warm-up JSON is T-by-N (time x assets), while the reference metrics
    # API consumes M-by-T.  Computing one cross-section at a time here keeps
    # the orientation explicit and mirrors the TypeScript transpose boundary.
    return [spearman(signal[time], returns[time]) for time in range(len(signal))]


def ic_mean(series: list[float | None]) -> float:
    valid = [float(value) for value in series if finite(value)]
    return mean(valid) if valid else 0.0


def icir(series: list[float | None]) -> float:
    valid = [float(value) for value in series if finite(value)]
    if len(valid) < 3:
        return 0.0
    average = mean(valid)
    sample_std = math.sqrt(sum((value - average) ** 2 for value in valid) / (len(valid) - 1))
    return 0.0 if sample_std < 1e-12 else average / sample_std


def nullable(values: list[Any]) -> list[float | None]:
    return [float(value) if finite(value) else None for value in values]


def combine(factors: list[dict[str, Any]], width: int) -> dict[str, Any]:
    if not factors:
        return {"combined": [], "support_weight": [], "quality_weights": {}, "used_factor_ids": []}
    combined = [0.0] * width
    support = [0.0] * width
    quality_weights: dict[str, float] = {}
    used_factor_ids: list[int] = []
    for factor in factors:
        signal = factor["signal"]
        if sum(finite(value) for value in signal) < 2:
            continue
        ranked = percent_rank(signal, centered=True)
        direction = 1.0 if factor["icMean"] >= 0 else -1.0
        quality = max(abs(float(factor["icMean"])), 1e-6) * max(abs(float(factor["icir"])), 1e-6)
        for index, value in enumerate(ranked):
            if value is not None:
                combined[index] += direction * value * quality
                support[index] += quality
        quality_weights[str(factor["id"])] = quality
        used_factor_ids.append(int(factor["id"]))
    return {
        "combined": nullable([combined[index] / support[index] if support[index] > 0 else math.nan for index in range(width)]),
        "support_weight": nullable(support),
        "quality_weights": quality_weights,
        "used_factor_ids": used_factor_ids,
    }


def raw_weights(combined: list[float | None]) -> dict[str, Any]:
    valid = [index for index, value in enumerate(combined) if finite(value)]
    equal = [1.0 / len(combined)] * len(combined) if combined else []
    if len(valid) < 2:
        return {"z": [None] * len(combined), "weights": equal, "fallback": True}
    values = [float(combined[index]) for index in valid]
    average = mean(values)
    deviation = std0([value - average for value in values])
    if not finite(deviation) or deviation <= 1e-12:
        return {"z": [None] * len(combined), "weights": equal, "fallback": True}
    z: list[float | None] = [None] * len(combined)
    weights = [0.0] * len(combined)
    for index in valid:
        z[index] = (float(combined[index]) - average) / deviation
        weights[index] = max(0.0, (1.0 + float(z[index])) / len(valid))
    total = sum(weights)
    if total <= 1e-12:
        return {"z": z, "weights": equal, "fallback": True}
    return {"z": z, "weights": [weight / total for weight in weights], "fallback": False}


def forecast_scale(closes: list[list[Any]], horizon: int = 1) -> dict[str, Any]:
    volatility: list[float | None] = []
    for row in range(horizon, len(closes)):
        returns = []
        for asset, current in enumerate(closes[row]):
            prior = closes[row - horizon][asset]
            if finite(prior) and finite(current) and float(prior) != 0:
                returns.append(float(current) / float(prior) - 1.0)
        value = std0(returns)
        volatility.append(value if finite(value) else None)
    recent = sorted(float(value) for value in volatility[-252:] if finite(value))
    if not recent:
        scale = 0.01
    elif len(recent) % 2:
        scale = recent[len(recent) // 2]
    else:
        scale = (recent[len(recent) // 2 - 1] + recent[len(recent) // 2]) / 2.0
    return {"cross_sectional_volatility": volatility, "return_scale": scale if finite(scale) and scale > 0 else 0.01}


def forecasts(combined: list[float | None], factors: list[dict[str, Any]], scale: float) -> list[float | None]:
    valid = [float(value) for value in combined if finite(value)]
    if len(valid) < 2 or not finite(scale):
        return [None] * len(combined)
    average = mean(valid)
    deviation = std0([value - average for value in valid])
    if not finite(deviation) or deviation <= 1e-12:
        return [None] * len(combined)
    mean_abs_ic = mean([abs(float(factor["icMean"])) for factor in factors]) if factors else 0.0
    return [None if not finite(value) else (float(value) - average) / deviation * mean_abs_ic * scale for value in combined]


def run(payload: dict[str, Any]) -> dict[str, Any]:
    source = payload["source"]
    cutoff = str(source["cutoff"])
    cutoff_date = date.fromisoformat(cutoff)
    observations = sorted(payload["observations"], key=lambda row: row["asOf"])
    forward_returns = sorted(payload["forwardReturns"], key=lambda row: row["asOf"])
    if len(observations) != len(forward_returns):
        raise ValueError("warm-up observations and forward returns are misaligned")
    if any(date.fromisoformat(row["asOf"]) > cutoff_date for row in [*observations, *forward_returns]):
        raise ValueError("future data is present in the oracle input")
    asset_ids = list(payload["asset_ids"])
    width = len(asset_ids)
    closes = [row["fields"]["close"] for row in observations]
    returns = [row["values"] for row in forward_returns]

    factor_specs = [(101, "momentum_20"), (102, "reversal_5")]
    factors: list[dict[str, Any]] = []
    metadata: dict[str, dict[str, Any]] = {}
    for factor_id, field in factor_specs:
        signals = [row["fields"][field] for row in observations]
        series = ic_series(signals, returns)
        details = {"icMean": ic_mean(series), "icir": icir(series), "valid_ic_observations": sum(finite(value) for value in series)}
        metadata[field] = details
        factors.append({"id": factor_id, "icMean": details["icMean"], "icir": details["icir"], "signal": signals[-1]})
    selected = sorted(factors, key=lambda factor: abs(factor["icMean"]) * abs(factor["icir"]), reverse=True)
    combination = combine(selected, width)
    weights = raw_weights(combination["combined"])
    scale = forecast_scale(closes)
    used = [factor for factor in selected if factor["id"] in combination["used_factor_ids"]]
    forecast = forecasts(combination["combined"], used, scale["return_scale"])
    signal_by_id = {factor_id: [row["fields"][field] for row in observations] for factor_id, field in factor_specs}
    combined_series = [combine([
        {"id": factor["id"], "icMean": factor["icMean"], "icir": factor["icir"], "signal": signal_by_id[factor["id"]][time]}
        for factor in selected
    ], width)["combined"] for time in range(len(observations))]
    metrics_series = ic_series(combined_series, returns)
    nav = float(width * 100.0)
    return {
        "oracle": "independent_python",
        "cutoff": cutoff,
        "asset_ids": asset_ids,
        "selected_factor_ids": [factor["id"] for factor in selected],
        "factor_metadata": metadata,
        "combination": combination,
        "weights": weights,
        "forecast_scale": scale,
        "forecasts": forecast,
        "metrics": {"icSeries": metrics_series, "icMean": ic_mean(metrics_series), "icir": icir(metrics_series)},
        "rebalance": {"postTradeNav": nav, "transferred": nav, "cost": 0.0, "turnover": 0.0},
        "cutoff_safe": True,
        "synthetic_online_segment_used": bool(source.get("synthetic_online_segment_used")),
    }


def main() -> None:
    payload = json.load(sys.stdin)
    json.dump(run(payload), sys.stdout, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
