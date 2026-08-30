import { describe, expect, it } from "vitest";
import { DEFAULT_FRESHNESS_POLICY } from "@foresightnine/domain";
import { computeMarketSnapshotIntegrityHash, marketDataStatus } from "@foresightnine/market-data";
import { ASSETS, MARKET_SNAPSHOT_BUNDLE, RUNTIME_AS_OF, assetsForMarketBundle, fmInput, freshnessFor } from "../src/fixtures";
import { computeFm } from "@foresightnine/fm-engine";
import { createPortfolioPlan, publicWeights } from "../src/portfolio";

function bundleWithExplicitRowStatus(status: "missing" | "stale") {
  const snapshots = MARKET_SNAPSHOT_BUNDLE.snapshots.map((snapshot) => {
    if (snapshot.assetId !== "GRID" || snapshot.anchor !== "latest_completed_session") return snapshot;
    return status === "missing"
      ? { ...snapshot, status, asOf: null, price: null, close: null }
      : { ...snapshot, status };
  });
  const base = { ...MARKET_SNAPSHOT_BUNDLE, snapshots };
  return { ...base, integrityHash: computeMarketSnapshotIntegrityHash(base) };
}

describe("Android freshness policy integration", () => {
  it("fails closed by default for offline fixture data", () => {
    expect(freshnessFor(ASSETS[0]!).allowCurrentPublicDefault).toBe(false);
    expect(Array.from(publicWeights(Float64Array.from([0.5, 0.2, 0.1, 0.1, 0.05, 0.05])))).toEqual([0, 0, 0, 0, 0, 0]);
  });

  it("keeps fresh and exact-threshold data current and excludes stale or historical-only data", () => {
    const exact = freshnessFor(ASSETS[0]!, { runtimeAsOf: "2026-08-10T00:00:00Z", providerStatus: "fresh", policy: DEFAULT_FRESHNESS_POLICY });
    expect(exact.status).toBe("fresh");
    expect(exact.allowCurrentPublicDefault).toBe(true);
    expect(freshnessFor(ASSETS[4]!, { runtimeAsOf: RUNTIME_AS_OF, providerStatus: "fresh" }).status).toBe("stale");
    const staleAssets = ASSETS.map((asset, index) => index === 0 ? { ...asset, asOf: "2026-08-07" } : asset);
    const weights = publicWeights(Float64Array.from([0.5, 0.2, 0.1, 0.1, 0.05, 0.05]), staleAssets, { runtimeAsOf: RUNTIME_AS_OF, providerStatus: "fresh" });
    expect(weights[0]).toBe(0);
    expect(weights.reduce((sum, weight) => sum + weight, 0)).toBeCloseTo(1);
  });

  it("removes all current Public Default amounts for provider failure and preserves historical metadata", () => {
    const plan = createPortfolioPlan(computeFm(fmInput()), 100000, "CNY", ASSETS, "zh", "main", { runtimeAsOf: RUNTIME_AS_OF, providerStatus: "unavailable" });
    expect(plan.rows.every((row) => row.publicAmount === 0 && row.publicUnits === 0)).toBe(true);
    expect(plan.rows.some((row) => row.rawAmount > 0 && row.freshnessLabel === "historical_only")).toBe(true);
    expect(plan.publicImplementable).toBe(false);
    expect(freshnessFor({ ...ASSETS[0]!, asOf: "not-a-date" }, { runtimeAsOf: RUNTIME_AS_OF, providerStatus: "fresh" }).allowCurrentPublicDefault).toBe(false);
  });

  it("rejects explicit missing/stale market rows instead of silently substituting mock prices", () => {
    for (const status of ["missing", "stale"] as const) {
      const bundle = bundleWithExplicitRowStatus(status);
      expect(marketDataStatus(bundle)).toBe("limited_data");
      expect(() => assetsForMarketBundle(bundle)).toThrow("No valid latest completed-session price for GRID.");
    }
  });

  it("never enables Public Default for a missing row or stale provider result while retaining Raw", () => {
    const result = computeFm(fmInput());
    const missingAssets = ASSETS.map((asset, index) => index === 0 ? { ...asset, asOf: "" } : asset);
    const missingPlan = createPortfolioPlan(result, 100000, "CNY", missingAssets, "zh", "main", { runtimeAsOf: "2026-08-10T00:00:00Z", providerStatus: "fresh" });
    expect(missingPlan.rows[0]).toMatchObject({ publicWeight: 0, publicAmount: 0, publicUnits: 0, publicExecutionAllowed: false });
    expect(missingPlan.rows[0]?.rawWeight).toBeGreaterThan(0);
    for (const providerStatus of ["stale", "unavailable"] as const) {
      const plan = createPortfolioPlan(result, 100000, "CNY", ASSETS, "zh", "main", { runtimeAsOf: "2026-08-10T00:00:00Z", providerStatus });
      expect(plan.rows.every((row) => row.publicAmount === 0 && row.publicUnits === 0 && !row.publicExecutionAllowed)).toBe(true);
      expect(plan.rows.some((row) => row.rawAmount > 0)).toBe(true);
    }
  });
});
