import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  allocateCapital, computeFm, evaluateFactorAst, mountFmWorker, rawLongOnlyWeights, selectFactors, shouldRebalance,
} from "../dist/index.js";
const fixture = JSON.parse(readFileSync(new URL("../../test-fixtures/fm/v1/core-parity.json", import.meta.url), "utf8"));

const rows = [
  { asOf: "2026-07-13", fields: { alpha: [1, 2, 3], beta: [3, 2, 1], invalid: [null, null, null], close: [100, 200, 300] } },
  { asOf: "2026-07-14", fields: { alpha: [2, 3, 4], beta: [2, 3, 4], invalid: [null, null, null], close: [101, 198, 303] } },
  { asOf: "2026-07-15", fields: { alpha: [3, null, 5], beta: [1, 4, 2], invalid: [null, null, null], close: [102, 201, 306] } },
];
const forwardReturns = rows.map((row) => ({ asOf: row.asOf, values: [0.01, -0.01, 0.02] }));

test("Python stable factor-selection ties retain non-ID input order", () => {
  const selected = selectFactors([
    { id: 77, icMean: 0.2, icir: 1, admitted: true, signal: new Float64Array([1, 2]) },
    { id: 3, icMean: 0.2, icir: 1, admitted: true, signal: new Float64Array([1, 2]) },
  ]);
  assert.deepEqual(selected.map((factor) => factor.id), [77, 3]);
});

test("selection skips non-finite quality before maxFactors truncation", () => {
  const selected = selectFactors([
    { id: 999, icMean: Number.NaN, icir: 100, admitted: true, signal: new Float64Array([1, 2]) },
    { id: 998, icMean: 100, icir: Number.POSITIVE_INFINITY, admitted: true, signal: new Float64Array([1, 2]) },
    { id: 77, icMean: 0.2, icir: 1, admitted: true, signal: new Float64Array([1, 2]) },
  ], 1);
  assert.deepEqual(selected.map((factor) => factor.id), [77]);
  assert.deepEqual(Array.from(rawLongOnlyWeights(new Float64Array([Number.NaN, Number.NaN])).weights), [0.5, 0.5]);
});

test("golden fixture visibly covers the six mapped reference subsets", () => {
  assert.deepEqual(fixture.oracle, { mode: "copied_formula_reference", import_status: "not_persisted" });
  const cases = fixture.reference_cases;
  assert.deepEqual(cases["combination.py"].input_shape_t_by_n, [2, 5]);
  assert.equal(cases["combination.py"].ic_weighted_non_positive_fallback.fallback, "equal_weight");
  assert.deepEqual(cases["combination.py"].orthogonal.output_shape_t_by_n, [2, 5]);
  assert.equal(cases["portfolio.py"].transaction_cost_bps, 3);
  assert.deepEqual(cases["portfolio.py"].turnover_series, [0, 1]);
  assert.equal(cases["portfolio.py"].ls_net_series[1], 0.0394);
  assert.deepEqual(cases["backtest.py"].train_test_split.split, { train_start: 0, train_end: 7, test_start: 7, test_end: 10 });
  assert.equal(cases["backtest.py"].rolling_splits.splits.length, 2);
  for (const name of ["fm_walk_forward.py", "combination.py", "portfolio.py", "metrics.py", "backtest.py", "ASSETS.yaml"]) assert.match(fixture.reference_sha256[name], /^[0-9a-f]{64}$/);
});

test("finite Factor AST whitelist evaluates supported operators and rejects malformed nodes", () => {
  const result = evaluateFactorAst({ op: "rolling_mean", input: { op: "field", name: "alpha" }, window: 2 }, rows);
  assert.deepEqual(Array.from(result[2]), [2.5, 3, 4.5]);
  assert.throws(() => evaluateFactorAst({ op: "eval", source: "1+1" }, rows), /unknown factor AST operator/);
  assert.throws(() => evaluateFactorAst({ op: "constant", value: Number.NaN }, rows), /finite number/);
  assert.throws(() => evaluateFactorAst({ op: "field", name: "missing" }, rows), /unknown factor field/);
  const safeDivide = evaluateFactorAst({ op: "div", left: { op: "constant", value: 1 }, right: { op: "constant", value: 0 } }, rows);
  assert.ok(Number.isNaN(safeDivide[0][0]));
});

test("computeFm is cutoff-aware, returns intermediates, and keeps initial cost free", () => {
  const result = computeFm({
    cutoff: "2026-07-15",
    observations: rows,
    factors: [
      { id: 10, icMean: 0.2, icir: 1, admitted: true, expression: { op: "field", name: "alpha" } },
      { id: 20, icMean: -0.1, icir: 2, admitted: true, expression: { op: "abs", input: { op: "field", name: "beta" } } },
    ],
    forwardReturns,
    currentValues: new Float64Array([100, 100, 100]),
    initialAllocation: true,
  });
  assert.deepEqual(result.selectedFactorIds, [10, 20]);
  assert.equal(result.combination.combined.length, 3);
  assert.equal(result.rebalance?.cost, 0);
  assert.ok(result.metrics !== null);
  assert.ok(result.reasons.includes("initial_allocation_free"));
  assert.throws(() => computeFm({ ...({ cutoff: "2026-07-15", observations: [...rows, { asOf: "2026-07-16", fields: rows[0].fields }], factors: [] }) }), /future_data_rejected/);
  assert.throws(() => computeFm({ cutoff: "2026-07-15", observations: rows, factors: [], currentValues: new Float64Array([0, 0, 0]) }), /positive NAV/);
  assert.throws(() => computeFm({ cutoff: "2026-07-15", observations: rows, factors: [], forwardReturns: [...forwardReturns.slice(0, 2), { asOf: "2026-07-16", values: [0.01, -0.01, 0.02] }] }), /future_data_rejected/);
  assert.throws(() => computeFm({ cutoff: "2026-07-15", observations: rows, factors: [], forwardReturns: [...forwardReturns.slice(0, 2), { asOf: "2026-07-14", values: [0.01, -0.01, 0.02] }] }), /must match its observation asOf/);
});

test("forecasts use only factors that contributed and do not alter dead-band", () => {
  const common = { cutoff: "2026-07-15", observations: rows, currentValues: new Float64Array([100, 100, 100]), forwardReturns };
  const withUnusedSelected = computeFm({ ...common, factors: [
    { id: 2, icMean: 0.9, icir: 2, admitted: true, expression: { op: "field", name: "invalid" } },
    { id: 1, icMean: 0.2, icir: 1, admitted: true, expression: { op: "field", name: "alpha" } },
  ] });
  const onlyUsed = computeFm({ ...common, factors: [
    { id: 1, icMean: 0.2, icir: 1, admitted: true, expression: { op: "field", name: "alpha" } },
  ] });
  assert.deepEqual(withUnusedSelected.selectedFactorIds, [2, 1]);
  assert.deepEqual(withUnusedSelected.combination.usedFactorIds, [1]);
  assert.deepEqual(Array.from(withUnusedSelected.forecasts), Array.from(onlyUsed.forecasts));
  assert.deepEqual(withUnusedSelected.deadband, onlyUsed.deadband);
});

test("computeFm keeps width-shaped equal fallback when every factor is invalid", () => {
  const result = computeFm({
    cutoff: "2026-07-15",
    observations: rows,
    factors: [{ id: 999, icMean: Number.NaN, icir: Number.POSITIVE_INFINITY, admitted: true, expression: { op: "field", name: "alpha" } }],
    currentValues: new Float64Array([200, 100, 0]),
  });
  assert.deepEqual(result.selectedFactorIds, []);
  assert.equal(result.combination.combined.length, 3);
  assert.ok(Array.from(result.combination.combined).every((value) => Number.isNaN(value)));
  assert.equal(result.weights.weights.length, 3);
  assert.deepEqual(Array.from(result.weights.weights), [1 / 3, 1 / 3, 1 / 3]);
  assert.equal(result.weights.fallback, true);
  assert.equal(result.forecasts.length, 3);
  assert.ok(Array.from(result.forecasts).every((value) => Number.isNaN(value)));
  assert.ok(result.reasons.includes("no_valid_factors"));
  assert.ok(result.reasons.includes("equal_weight_fallback"));
  assert.equal(result.deadband?.execute, true);
  assert.ok(result.rebalance !== null);
  assert.ok((result.rebalance?.cost ?? 0) > 0);
});

test("dead-band is equality-closed and strictly-above", () => {
  const equal = shouldRebalance(new Float64Array([0.5, 0.5]), new Float64Array([0.4, 0.6]), new Float64Array([-0.00015, 0.00015]), 3);
  assert.equal(equal.execute, false);
  assert.equal(equal.reason, "below_cost_deadband");
  const above = shouldRebalance(new Float64Array([0.5, 0.5]), new Float64Array([0.4, 0.6]), new Float64Array([-0.00015, 0.000151]), 3);
  assert.equal(above.execute, true);
  assert.equal(above.reason, "clears_cost_deadband");
});

test("Worker message path returns typed results and sanitized errors", () => {
  const sent = [];
  const port = { onmessage: null, postMessage(message) { sent.push(message); } };
  const unmount = mountFmWorker(port);
  port.onmessage({ data: { type: "compute", requestId: "ok-1", input: {
    cutoff: "2026-07-15", observations: rows, factors: [{ id: 1, icMean: 0.1, icir: 1, admitted: true, expression: { op: "field", name: "alpha" } }],
  } } });
  assert.equal(sent[0].type, "result");
  port.onmessage({ data: { type: "compute", requestId: "bad-1", input: {
    cutoff: "2026-07-15", observations: rows, factors: [{ id: 1, icMean: 0.1, icir: 1, admitted: true, expression: { op: "eval", source: "process.env" } }],
  } } });
  assert.deepEqual(sent[1], { type: "error", requestId: "bad-1", error: { code: "compute_failed", message: "FM computation failed" } });
  port.onmessage({ data: { type: "compute", requestId: "future-1", input: { cutoff: "2026-07-15", observations: [{ ...rows[0], asOf: "2026-07-16" }], factors: [] } } });
  assert.equal(sent[2].error.message, "FM computation failed");
  unmount();
  assert.equal(port.onmessage, null);
});

test("shared entry executes an Android-sized 15-asset fixture", () => {
  const androidRows = Array.from({ length: 16 }, (_, time) => ({
    asOf: `2026-07-${String(time + 1).padStart(2, "0")}`,
    fields: { alpha: Array.from({ length: 15 }, (_, asset) => (asset + time) % 7), close: Array.from({ length: 15 }, (_, asset) => 100 + asset + time) },
  }));
  const result = computeFm({ cutoff: "2026-07-16", observations: androidRows, factors: [{ id: 1, icMean: 0.1, icir: 1, admitted: true, expression: { op: "rank", input: { op: "field", name: "alpha" } } }] });
  assert.equal(result.weights.weights.length, 15);
  assert.ok(Array.from(result.weights.weights).every((value) => Number.isFinite(value)));
});

test("all-invalid and single-factor inputs use explicit equal-weight fallback", () => {
  assert.deepEqual(Array.from(rawLongOnlyWeights(new Float64Array([NaN, NaN, NaN])).weights), [1 / 3, 1 / 3, 1 / 3]);
  assert.deepEqual(Array.from(rawLongOnlyWeights(new Float64Array([0.2, NaN, NaN])).weights), [1 / 3, 1 / 3, 1 / 3]);
});

test("allocateCapital rejects non-finite and non-positive numeric asset inputs", () => {
  const asset = { id: "A", weight: 1, price: 10, fxToBase: 1, lotSize: 1, fractionalSupported: true };
  for (const capital of [Number.NaN, Number.POSITIVE_INFINITY, -1]) assert.throws(() => allocateCapital(capital, [asset]), /capital must be non-negative/);
  for (const [field, value] of [["weight", Number.NaN], ["weight", Number.POSITIVE_INFINITY], ["weight", -1], ["price", Number.NaN], ["price", Number.POSITIVE_INFINITY], ["price", 0], ["price", -1], ["fxToBase", Number.NaN], ["fxToBase", Number.POSITIVE_INFINITY], ["fxToBase", 0], ["fxToBase", -1], ["lotSize", Number.NaN], ["lotSize", Number.POSITIVE_INFINITY], ["lotSize", 0], ["lotSize", -1]]) {
    const invalid = { ...asset, [field]: value };
    assert.throws(() => allocateCapital(100, [invalid]), /invalid asset A/);
  }
});
