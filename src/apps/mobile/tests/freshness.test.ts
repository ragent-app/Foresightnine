import { describe, expect, it } from "vitest";
import { DEFAULT_FRESHNESS_POLICY } from "@foresightnine/domain";
import { ASSETS, RUNTIME_AS_OF, fmInput, freshnessFor } from "../src/fixtures";
import { computeFm } from "@foresightnine/fm-engine";
import { createPortfolioPlan, publicWeights } from "../src/portfolio";

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
});
