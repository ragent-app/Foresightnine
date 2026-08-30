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
  marketDataStatus,
  marketSnapshotBundleToFmInput,
  redactedMarketSnapshotBundleJson,
  resolveSnapshotAnchors,
  validateMarketSnapshotBundle,
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
  assert.deepEqual(resolveSnapshotAnchors("2026-08-10T00:30:00+08:00").latest_completed_session, "2026-08-10");
});

test("validates cutoff, timezone, complete anchor matrix, and deterministic integrity", () => {
  const validated = validateMarketSnapshotBundle(bundle(), request);
  assert.equal(validated.timezone, "UTC");
  assert.equal(validated.snapshots.length, request.assetIds.length * SNAPSHOT_ANCHORS.length);
  assert.equal(validated.integrityHash, computeMarketSnapshotIntegrityHash(validated));
  assert.equal(marketDataStatus(validated), "mock");
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
