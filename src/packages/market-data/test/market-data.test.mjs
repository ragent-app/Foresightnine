import assert from "node:assert/strict";
import test from "node:test";
import {
  MARKET_DATA_LIMITS,
  SNAPSHOT_ANCHORS,
  MarketDataValidationError,
  computeMarketSnapshotIntegrityHash,
  createByokMarketDataSource,
  createMockMarketSnapshotBundle,
  createPublicMarketDataSource,
  createMarketRuntimeController,
  applyLatestCompletedSessionPrices,
  marketDataStatus,
  marketSnapshotBundleToFmInput,
  redactedMarketSnapshotBundleJson,
  resolveSnapshotAnchors,
  validateMarketSnapshotBundle,
  computeAnchorFactorMetrics,
} from "../dist/index.js";

const request = {
  assetIds: ["AAA", "BBB"],
  cutoff: "2026-08-09",
  timezone: "UTC",
  requestedAt: "2026-08-09T12:00:00Z",
};

function bundle(overrides = {}) {
  const base = createMockMarketSnapshotBundle(request);
  const next = { ...base, ...overrides };
  return { ...next, integrityHash: computeMarketSnapshotIntegrityHash(next) };
}

function expectCode(callback, code) {
  assert.throws(callback, (error) => error instanceof MarketDataValidationError && error.code === code);
}

test("resolves all three anchors with weekend-aware calendar semantics", () => {
  assert.deepEqual(resolveSnapshotAnchors("2022-01-02"), {
    year_start: "2021-12-31",
    latest_month_start: "2021-12-31",
    latest_completed_session: "2021-12-31",
  });
  assert.deepEqual(resolveSnapshotAnchors("2026-08-09"), {
    year_start: "2026-01-01",
    latest_month_start: "2026-07-31",
    latest_completed_session: "2026-08-07",
  });
  assert.deepEqual(resolveSnapshotAnchors("2026-08-10T00:30:00+08:00").latest_completed_session, "2026-08-07");
});

test("validates cutoff, timezone, complete anchor matrix, and deterministic integrity", () => {
  const validated = validateMarketSnapshotBundle(bundle(), request);
  assert.equal(validated.timezone, "UTC");
  assert.equal(validated.snapshots.length, request.assetIds.length * SNAPSHOT_ANCHORS.length);
  assert.equal(validated.integrityHash, computeMarketSnapshotIntegrityHash(validated));
  assert.equal(marketDataStatus(validated), "mock");
});

test("validates point-in-time FX independently and includes it in the integrity hash", () => {
  const fx = { asOf: "2026-08-07", retrievedAt: "2026-08-09T00:00:00Z", provider: "licensed-or-byok", cnyPerUnit: { CNY: 1, USD: 7.18, EUR: 7.82 } };
  const validated = validateMarketSnapshotBundle(bundle({ fx }), request);
  assert.deepEqual(validated.fx, fx);
  assert.equal(validated.integrityHash, computeMarketSnapshotIntegrityHash(validated));
  expectCode(() => validateMarketSnapshotBundle(bundle({ fx: { ...fx, cnyPerUnit: { USD: 7.18, EUR: 7.82 } } }), request), "invalid_bundle");
});

test("rejects a same-calendar-date observation after an intraday cutoff", () => {
  const intradayRequest = { assetIds: ["AAA"], cutoff: "2026-08-10T00:30:00+08:00", timezone: "UTC", requestedAt: "2026-08-09T00:00:00Z" };
  const base = createMockMarketSnapshotBundle(intradayRequest);
  const snapshots = base.snapshots.map((snapshot, index) => index === 0 ? { ...snapshot, asOf: "2026-08-10" } : snapshot);
  const altered = { ...base, snapshots, integrityHash: computeMarketSnapshotIntegrityHash({ ...base, snapshots }) };
  expectCode(() => validateMarketSnapshotBundle(altered, intradayRequest), "future_data");
});

test("public provider sends bounded JSON without an Authorization header", async () => {
  let captured;
  const source = createPublicMarketDataSource({
    baseUrl: "https://market.example.test/api",
    providerId: "approved-public",
    fetcher: async (_url, init) => {
      captured = init;
      return new Response(JSON.stringify({ snapshots: createMockMarketSnapshotBundle(request).snapshots }), { status: 200, headers: { "content-type": "application/json" } });
    },
  });
  const result = await source.fetchSnapshots(request);
  assert.equal(new Headers(captured.headers).has("Authorization"), false);
  assert.equal(result.provider.mode, "public");
});

test("BYOK authorization is request-only and absent from bundle, hash, and redacted output", async () => {
  const secret = ["unit-test-only-", "secret-not-data"].join("");
  let captured;
  let networkCalls = 0;
  const source = createByokMarketDataSource({
    baseUrl: "https://market.example.test/api",
    providerId: "user-byok",
    apiKey: secret,
    fetcher: async (_url, init) => {
      networkCalls += 1;
      captured = init;
      return new Response(JSON.stringify({ snapshots: createMockMarketSnapshotBundle(request).snapshots }), { status: 200 });
    },
  });
  const result = await source.fetchSnapshots(request);
  assert.equal(new Headers(captured.headers).get("Authorization"), `Bearer ${secret}`);
  const serialized = `${JSON.stringify(result)}${redactedMarketSnapshotBundleJson(result)}${result.integrityHash}`;
  assert.equal(serialized.includes(secret), false);
  assert.equal(JSON.stringify(source).includes(secret), false);
  assert.equal(networkCalls, 1);
  await assert.rejects(source.fetchSnapshots(request), (error) => error instanceof MarketDataValidationError && error.code === "byok_reused" && error.message === "BYOK market data source is single-use.");
  assert.equal(networkCalls, 1);
});

test("rejects unknown assets, duplicate rows, malformed/future dates, bad prices, and unsafe URLs", () => {
  const base = createMockMarketSnapshotBundle(request);
  expectCode(() => validateMarketSnapshotBundle(bundle({ snapshots: [{ ...base.snapshots[0], assetId: "UNKNOWN" }, ...base.snapshots.slice(1)] }), request), "unknown_asset");
  expectCode(() => validateMarketSnapshotBundle(bundle({ snapshots: [...base.snapshots, base.snapshots[0]] }), request), "duplicate_snapshot");
  expectCode(() => validateMarketSnapshotBundle(bundle({ snapshots: [{ ...base.snapshots[0], asOf: "2026-02-30" }, ...base.snapshots.slice(1)] }), request), "malformed_date");
  expectCode(() => validateMarketSnapshotBundle(bundle({ snapshots: [{ ...base.snapshots[0], asOf: "2026-08-10" }, ...base.snapshots.slice(1)] }), request), "future_data");
  expectCode(() => validateMarketSnapshotBundle(bundle({ snapshots: [{ ...base.snapshots[0], price: -1 }, ...base.snapshots.slice(1)] }), request), "invalid_price");
  expectCode(() => validateMarketSnapshotBundle({ ...base, snapshots: [{ ...base.snapshots[0], close: Number.NaN }, ...base.snapshots.slice(1)], integrityHash: "invalid" }, request), "invalid_price");
  assert.equal(createPublicMarketDataSource instanceof Function, true);
  expectCode(() => createPublicMarketDataSource({ baseUrl: "http://market.example.test", providerId: "unsafe" }), "invalid_provider_url");
});

test("accepts explicit stale/missing rows but returns limited-data status without fixture substitution", () => {
  const base = createMockMarketSnapshotBundle(request);
  const snapshots = base.snapshots.map((snapshot, index) => index === 0
    ? { ...snapshot, status: "stale" }
    : index === 1
      ? { ...snapshot, status: "missing", asOf: null, price: null, close: null, features: undefined }
      : snapshot);
  const partial = bundle({ snapshots });
  const result = marketSnapshotBundleToFmInput(partial, [{ id: 1, icMean: 0.1, icir: 0.5, admitted: true, expression: { op: "field", name: "momentum" } }]);
  assert.equal(result.status, "limited_data");
  assert.equal(result.dataStatus, "limited_data");
  assert.equal(result.input.observations.some((observation) => observation.fields.momentum.some((value) => value === null)), true);
  assert.equal(result.input.forwardReturns, undefined);
});

test("rejects retrievedAt after the cutoff for both valued and missing rows", () => {
  const base = createMockMarketSnapshotBundle(request);
  const future = "2026-08-10T00:00:00Z";
  expectCode(() => validateMarketSnapshotBundle(bundle({ snapshots: [{ ...base.snapshots[0], retrievedAt: future }, ...base.snapshots.slice(1)] }), request), "future_data");
  const missingSnapshots = base.snapshots.map((snapshot, index) => index === 0
    ? { ...snapshot, status: "missing", asOf: null, price: null, close: null, features: undefined, retrievedAt: future }
    : snapshot);
  expectCode(() => validateMarketSnapshotBundle(bundle({ snapshots: missingSnapshots }), request), "future_data");
});

test("converts a valid bundle to the existing local FM shape with latest currentValues and no future observations", () => {
  const result = marketSnapshotBundleToFmInput(bundle(), [
    { id: 10, icMean: 0.072, icir: 0.64, admitted: true, expression: { op: "field", name: "momentum" } },
    { id: 20, icMean: 0.051, icir: 0.58, admitted: true, expression: { op: "field", name: "quality" } },
  ], { currentValues: Float64Array.from([120, 80]), initialAllocation: true });
  assert.equal(result.status, "ready");
  assert.deepEqual([...result.input.currentValues], [120, 80]);
  assert.equal(result.input.observations.length, 3);
  assert.equal(result.input.observations.at(-1).asOf, "2026-08-07");
  assert.equal(result.input.forwardReturns, undefined);
  assert.equal(result.warnings.some((warning) => warning.includes("IC/ICIR")), true);
  assert.equal(result.input.observations.every((observation) => observation.asOf <= request.cutoff), true);
});

test("rejects oversized provider responses before making an unbounded bundle", async () => {
  const source = createPublicMarketDataSource({
    baseUrl: "https://market.example.test",
    providerId: "approved-public",
    fetcher: async () => new Response("x".repeat(MARKET_DATA_LIMITS.maxResponseBytes + 1), { status: 200 }),
  });
  await assert.rejects(source.fetchSnapshots(request), (error) => error instanceof MarketDataValidationError && error.code === "oversized_payload");
});

test("runtime controller starts with explicit mock and records a ready bundle", async () => {
  const controller = createMarketRuntimeController();
  controller.configure({ mode: "mock", providerId: "runtime-mock" });
  const bundle = await controller.refresh(request);
  assert.equal(bundle.provider.id, "runtime-mock");
  assert.equal(controller.getState().status, "ready");
  assert.equal(controller.getState().mode, "mock");
});

test("selected public provider failure does not silently fall back to mock", async () => {
  const controller = createMarketRuntimeController({ fetcher: async () => new Response("unavailable", { status: 503 }) });
  controller.configure({ mode: "public", providerId: "public-test", publicBaseUrl: "https://market.example.test" });
  await assert.rejects(controller.refresh(request), /HTTP 503/);
  assert.equal(controller.getState().status, "error");
  assert.equal(controller.getState().mode, "public");
  assert.equal(controller.getState().bundle, null);
});

test("selected public provider without a URL fails closed", async () => {
  const controller = createMarketRuntimeController();
  controller.configure({ mode: "public", providerId: "public-missing-url" });
  await assert.rejects(controller.refresh(request), (error) => error instanceof MarketDataValidationError && error.code === "invalid_provider_url");
  assert.equal(controller.getState().status, "error");
  assert.equal(controller.getState().mode, "public");
  assert.equal(controller.getState().bundle, null);
});

test("runtime BYOK is request-only and cannot be reused after success", async () => {
  let calls = 0;
  const controller = createMarketRuntimeController({
    fetcher: async (_url, init) => {
      calls += 1;
      assert.equal(new Headers(init.headers).get("Authorization"), "Bearer runtime-secret");
      return new Response(JSON.stringify({ snapshots: createMockMarketSnapshotBundle(request).snapshots }), { status: 200 });
    },
  });
  controller.configure({ mode: "byok", providerId: "runtime-byok", byokBaseUrl: "https://market.example.test", byokApiKey: "runtime-secret" });
  const first = await controller.refresh(request);
  assert.equal(first.provider.mode, "byok");
  assert.equal(controller.getState().status, "ready");
  await assert.rejects(controller.refresh(request), (error) => error instanceof MarketDataValidationError && error.code === "invalid_request");
  assert.equal(controller.getState().status, "error");
  assert.equal(calls, 1);
  assert.equal(JSON.stringify(controller.getState()).includes("runtime-secret"), false);
});

test("latest completed-session prices map for both Web and Android asset key shapes", () => {
  const snapshotBundle = createMockMarketSnapshotBundle(request);
  const webAssets = [{ assetId: "AAA", price: 1, quoteCurrency: "USD", asOf: "2020-01-01", freshness: "historical_only" }];
  const androidAssets = [{ id: "AAA", price: 1, quoteCurrency: "USD", asOf: "2020-01-01", freshness: "historical_only" }];
  assert.equal(applyLatestCompletedSessionPrices(webAssets, snapshotBundle)[0].price, 102);
  assert.equal(applyLatestCompletedSessionPrices(androidAssets, snapshotBundle)[0].price, 102);
});

test("computeAnchorFactorMetrics derives IC/ICIR from the three anchors", () => {
  const sixAssetRequest = { ...request, assetIds: ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"] };
  const liveBundle = createMockMarketSnapshotBundle(sixAssetRequest);
  const factors = [
    { id: 10, icMean: 0, icir: 0, admitted: true, expression: { op: "field", name: "momentum" } },
    { id: 20, icMean: 0, icir: 0, admitted: true, expression: { op: "field", name: "quality" } },
    { id: 30, icMean: 0, icir: 0, admitted: true, expression: { op: "rank", input: { op: "field", name: "momentum" } } },
    { id: 40, icMean: 0, icir: 0, admitted: true, expression: { op: "field", name: "vendor_score" } },
  ];
  const metrics = computeAnchorFactorMetrics(liveBundle, factors);
  assert.equal(metrics.length, 4);
  for (const metric of metrics.slice(0, 3)) {
    assert.equal(metric.sufficient, true, `factor ${metric.factorId} must be sufficient`);
    assert.equal(metric.observations.length, 2, "three anchors yield exactly two IC points");
    assert.ok(metric.icMean >= -1 && metric.icMean <= 1);
    assert.ok(Number.isFinite(metric.icir));
    assert.ok(metric.observations.every((observation) => observation.assets === 6));
  }
  const unknownField = metrics[3];
  assert.equal(unknownField.sufficient, false);
  assert.equal(unknownField.error, "unknown_field");
});

test("computeAnchorFactorMetrics fails closed below five assets", () => {
  const twoAssetBundle = createMockMarketSnapshotBundle(request);
  const factors = [{ id: 10, icMean: 0, icir: 0, admitted: true, expression: { op: "field", name: "momentum" } }];
  const metrics = computeAnchorFactorMetrics(twoAssetBundle, factors);
  assert.equal(metrics[0].sufficient, false);
  assert.equal(metrics[0].error, "insufficient_assets");
});
