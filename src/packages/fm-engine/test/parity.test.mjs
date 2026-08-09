import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  allocateCapital, assetTransferRebalance, canonicalJson, combineLastSignals, forecasts,
  forecastScale, icir, icMean, informationCoefficient, pandasPercentRank, rawLongOnlyWeights,
  selectFactors, sha256Hex, shouldRebalance, toFloat64
} from "../dist/index.js";

const fixture = JSON.parse(readFileSync(new URL("../../test-fixtures/fm/v1/core-parity.json", import.meta.url), "utf8"));
const epsilon = 1e-10;
const close = (actual, expected, message = "") => assert.ok(Math.abs(actual - expected) <= epsilon, `${message}: ${actual} != ${expected}`);
const arrayClose = (actual, expected, message = "") => {
  assert.equal(actual.length, expected.length, `${message} length`);
  actual.forEach((value, index) => expected[index] === null ? assert.ok(Number.isNaN(value), `${message}[${index}] should be NaN`) : close(value, expected[index], `${message}[${index}]`));
};

test("Python oracle fixture: selection, pandas ties/NaN, support, z and forecasts", () => {
  const factors = fixture.inputs.factors.map((factor) => ({ id: factor.id, icMean: factor.ic_mean, icir: factor.icir, admitted: factor.admitted, signal: toFloat64(factor.signal) }));
  const selected = selectFactors(factors, fixture.inputs.max_factors);
  assert.deepEqual(selected.map((factor) => factor.id), fixture.expected.selected_factor_ids);
  const topTen = selectFactors(Array.from({ length: 11 }, (_, index) => ({ id: 100 + index, icMean: index + 1, icir: 1, admitted: true, signal: toFloat64([1, 2]) })), 10);
  assert.deepEqual(topTen.map((factor) => factor.id), fixture.expected.top10_selection_ids);
  const result = combineLastSignals(selected);
  assert.deepEqual(result.usedFactorIds, fixture.expected.combination.used_factor_ids);
  arrayClose(result.combined, fixture.expected.combination.combined, "combined");
  arrayClose(result.supportWeight, fixture.expected.combination.support_weight, "support");
  for (const [id, expected] of Object.entries(fixture.expected.combination.contributions)) arrayClose(result.contributions.get(Number(id)), expected, `contribution ${id}`);
  for (const [id, expected] of Object.entries(fixture.expected.combination.quality_weights)) close(result.qualityWeights.get(Number(id)), expected, `quality ${id}`);
  arrayClose(pandasPercentRank(toFloat64([3, 1, 1, null, 5, 2])), [0.8, 0.3, 0.3, null, 1, 0.6], "pandas rank pct");
  const weight = rawLongOnlyWeights(result.combined);
  arrayClose(weight.z, fixture.expected.weights.z, "z ddof=0");
  arrayClose(weight.weights, fixture.expected.weights.raw_weights, "raw long-only weights");
  assert.equal(weight.fallback, false);
  close(Array.from(weight.weights).reduce((sum, value) => sum + value, 0), 1, "fully invested");
  const scale = forecastScale(fixture.inputs.closes.map(toFloat64), fixture.inputs.forecast_horizon);
  arrayClose(scale.crossSectionalVolatility, fixture.expected.forecast_scale.cross_sectional_volatility, "cross sectional volatility");
  close(scale.returnScale, fixture.expected.forecast_scale.return_scale, "forecast scale fallback");
  const used = selected.filter((factor) => result.usedFactorIds.includes(factor.id));
  arrayClose(forecasts(result.combined, used, scale.returnScale), fixture.expected.forecast, "forecast");
});

test("Python oracle fixture: IC shape, tie ranks and sample boundaries", () => {
  const series = informationCoefficient(fixture.inputs.ic_signal_assets_by_time.map(toFloat64), fixture.inputs.ic_returns_assets_by_time.map(toFloat64));
  arrayClose(series, fixture.expected.metrics.ic_series, "IC series");
  close(icMean(series), fixture.expected.metrics.ic_mean, "IC mean");
  close(icir(series), fixture.expected.metrics.icir, "ICIR");
  assert.equal(icir(toFloat64([0.5, null])), fixture.expected.metrics.too_few_icir);
  assert.throws(() => informationCoefficient([toFloat64([1, 2])], [toFloat64([1])]), /shape/);
});

test("Python oracle fixture: 3bps transfer, dead-band equality, empty and missing forecasts", () => {
  const rebalance = assetTransferRebalance(toFloat64([600, 400]), toFloat64([0.4, 0.6]), 1000, 3, false);
  close(rebalance.postTradeNav, fixture.expected.rebalance_3bps.post_trade_nav, "post trade NAV");
  close(rebalance.transferred, fixture.expected.rebalance_3bps.transferred, "transferred");
  close(rebalance.cost, fixture.expected.rebalance_3bps.cost, "cost");
  close(rebalance.turnover, fixture.expected.rebalance_3bps.turnover, "turnover");
  const equal = shouldRebalance(toFloat64([0.5, 0.5]), toFloat64([0, 1]), toFloat64([0, 0.0003]), 3);
  assert.equal(equal.execute, fixture.expected.deadband_equal.execute);
  close(equal.expectedGain, fixture.expected.deadband_equal.expected_gain, "deadband gain");
  close(equal.cost, fixture.expected.deadband_equal.cost, "deadband cost");
  assert.equal(shouldRebalance(toFloat64([0.5, 0.5]), toFloat64([0, 1]), toFloat64([0, 0]), 3).execute, true, "zero forecast executes as reference");
  assert.equal(shouldRebalance(toFloat64([0.5, 0.5]), toFloat64([0, 1]), toFloat64([null, null]), 3).execute, true, "missing forecast executes as reference");
  assert.equal(shouldRebalance(toFloat64([0.5, 0.5]), toFloat64([0, 1]), new Float64Array(), 3).execute, true, "empty forecast executes as reference");
  assert.equal(shouldRebalance(toFloat64([0.5, 0.5]), toFloat64([0, 1]), undefined, 3).execute, true, "empty forecast executes as reference");
  const fallback = rawLongOnlyWeights(toFloat64([null, null, null]));
  assert.equal(fallback.fallback, true);
  arrayClose(fallback.weights, [1 / 3, 1 / 3, 1 / 3], "equal fallback");
});

test("Python oracle fixture: FX / lot amounts and canonical SHA-256", () => {
  const result = allocateCapital(1000, [
    { id: "USD", weight: 0.5, price: 100, fxToBase: 1, lotSize: 1, fractionalSupported: true },
    { id: "HALF", weight: 0.3, price: 200, fxToBase: 0.5, lotSize: 2, fractionalSupported: false },
    { id: "TWO", weight: 0.2, price: 50, fxToBase: 2, lotSize: 1, fractionalSupported: false }
  ]);
  arrayClose(result.allocations.map((item) => item.units), fixture.expected.product_amounts.units, "units");
  arrayClose(result.allocations.map((item) => item.allocatedAmount), fixture.expected.product_amounts.allocated_amounts, "amounts");
  close(result.cashRemainder, fixture.expected.product_amounts.cash_remainder, "cash remainder");
  assert.equal(canonicalJson(fixture.expected.canonical.value), fixture.expected.canonical.json);
  assert.equal(sha256Hex(fixture.expected.canonical.json), fixture.expected.canonical.sha256);
});
