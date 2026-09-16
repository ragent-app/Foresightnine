import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { DEFAULT_FRESHNESS_POLICY } from "@foresightnine/domain";
import { computeMarketSnapshotIntegrityHash, marketDataStatus } from "@foresightnine/market-data";
import { ASSET_FIXTURES, MARKET_SNAPSHOT_BUNDLE, RUNTIME_AS_OF, assetsForMarketBundle, freshnessFor, fmInput, branches } from "./fixtures";
import { allocationRows, publicWeights } from "./state";
import { PortfolioPanel } from "./components/PortfolioPanel";

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

describe("Web freshness policy integration", () => {
  it("fails closed by default for offline fixture data", () => {
    expect(freshnessFor(ASSET_FIXTURES[0]!).allowCurrentPublicDefault).toBe(false);
    expect(publicWeights([0.5, 0.2, 0.1, 0.1, 0.05, 0.05])).toEqual([0, 0, 0, 0, 0, 0]);
  });

  it("does not treat fixture FX as current data for Public Default", () => {
    const weights = publicWeights([0.5, 0.2, 0.1, 0.1, 0.05, 0.05], ASSET_FIXTURES, { runtimeAsOf: "2026-08-10T00:00:00Z", providerStatus: "fresh" });
    expect(weights).toEqual([0, 0, 0, 0, 0, 0]);
  });

  it("uses revised provider FX values when the FX as-of date is unchanged", () => {
    const result = ({ weights: { weights: Float64Array.from([0.5, 0.2, 0.1, 0.1, 0.05, 0.05]) } } as never);
    const common = { runtimeAsOf: "2026-08-10T00:00:00Z", providerStatus: "fresh" as const, fxProviderStatus: "fresh" as const, fxAsOf: "2026-08-10", fxCnyPerUnit: { CNY: 1, USD: 7.18, EUR: 7.82 } };
    const revised = { ...common, fxCnyPerUnit: { CNY: 1, USD: 8.18, EUR: 7.82 } };
    const first = allocationRows(result, 100000, "CNY", "public", common);
    const second = allocationRows(result, 100000, "CNY", "public", revised);
    expect(second.find((row) => row.id === "GRID")?.publicAmount).not.toBe(first.find((row) => row.id === "GRID")?.publicAmount);
  });

  it("keeps fresh and exact-threshold assets eligible but excludes stale and historical-only assets", () => {
    const exact = freshnessFor(ASSET_FIXTURES[0]!, { runtimeAsOf: "2026-08-10T00:00:00Z", providerStatus: "fresh", policy: DEFAULT_FRESHNESS_POLICY });
    expect(exact.status).toBe("fresh");
    expect(exact.allowCurrentPublicDefault).toBe(true);
    expect(freshnessFor(ASSET_FIXTURES[4]!, { runtimeAsOf: RUNTIME_AS_OF, providerStatus: "fresh" }).status).toBe("historical_only");

    const stale = { runtimeAsOf: RUNTIME_AS_OF, providerStatus: "fresh" as const, fxProviderStatus: "fresh" as const, fxAsOf: RUNTIME_AS_OF.slice(0, 10), fxCnyPerUnit: { CNY: 1, USD: 7.18, EUR: 7.82 } };
    const staleAssets = ASSET_FIXTURES.map((asset, index) => index === 0 ? { ...asset, asOf: "2026-08-07" } : asset);
    const weights = publicWeights([0.5, 0.2, 0.1, 0.1, 0.05, 0.05], staleAssets, stale);
    expect(weights[0]).toBe(0);
    expect(weights.reduce((sum, weight) => sum + weight, 0)).toBeCloseTo(1);
  });

  it("fails closed for provider stale/unavailable and invalid dates while retaining historical rows", () => {
    const result = allocationRows(({ weights: { weights: Float64Array.from([0.2, 0.2, 0.2, 0.2, 0.1, 0.1]) } } as never), 100000, "CNY", "public", { runtimeAsOf: RUNTIME_AS_OF, providerStatus: "unavailable" });
    expect(result.every((row) => row.publicAmount === 0 && row.publicUnits === 0)).toBe(true);
    expect(result.some((row) => row.rawAmount > 0 && row.freshnessLabel === "historical_only")).toBe(true);

    const invalid = freshnessFor({ ...ASSET_FIXTURES[0]!, asOf: "not-a-date" }, { runtimeAsOf: RUNTIME_AS_OF, providerStatus: "fresh" });
    expect(invalid.status).toBe("invalid_date");
    expect(invalid.allowCurrentPublicDefault).toBe(false);
  });

  it("rejects missing prices but retains stale provider prices as explicitly stale Raw inputs", () => {
    const missing = bundleWithExplicitRowStatus("missing");
    expect(marketDataStatus(missing)).toBe("limited_data");
    expect(() => assetsForMarketBundle(missing)).toThrow("No usable latest completed-session price for GRID.");

    const stale = bundleWithExplicitRowStatus("stale");
    expect(marketDataStatus(stale)).toBe("limited_data");
    expect(assetsForMarketBundle(stale).find((asset) => asset.assetId === "GRID")?.freshness).toBe("stale");
  });

  it("never enables Public Default for a missing row or stale provider result while retaining Raw", () => {
    const result = ({ weights: { weights: Float64Array.from([0.5, 0.2, 0.1, 0.1, 0.05, 0.05]) } } as never);
    const missingAssets = ASSET_FIXTURES.map((asset, index) => index === 0 ? { ...asset, asOf: "" } : asset);
    const missingRows = allocationRows(result, 100000, "CNY", "public", { runtimeAsOf: "2026-08-10T00:00:00Z", providerStatus: "fresh" }, missingAssets);
    expect(missingRows[0]).toMatchObject({ publicWeight: 0, publicAmount: 0, publicUnits: 0, publicExecutionAllowed: false });
    expect(missingRows[0]?.weight).toBeGreaterThan(0);
    for (const providerStatus of ["stale", "unavailable"] as const) {
      const rows = allocationRows(result, 100000, "CNY", "public", { runtimeAsOf: "2026-08-10T00:00:00Z", providerStatus });
      expect(rows.every((row) => row.publicAmount === 0 && row.publicUnits === 0 && !row.publicExecutionAllowed)).toBe(true);
      expect(rows.some((row) => row.rawAmount > 0)).toBe(true);
    }
  });

  it("uses the same historical/simulated wording in the portfolio surface", () => {
    const result = allocationRows(null, 100000, "CNY", "public");
    const markup = renderToStaticMarkup(createElement(PortfolioPanel, { mode: "public", currency: "CNY", rows: result, capital: 100000, selectedBranch: "main", branches, stress: "main", onMode: () => undefined, onStress: () => undefined, onCompute: () => undefined, onNav: () => undefined, onExport: () => undefined, fmReady: false }));
    expect(markup).not.toContain("Historical/simulated display only");

    const staleRows = allocationRows(({ weights: { weights: Float64Array.from([0.2, 0.2, 0.2, 0.2, 0.1, 0.1]) } } as never), 100000, "CNY", "public", { runtimeAsOf: "2026-08-11T00:00:00Z", providerStatus: "fresh", fxProviderStatus: "fresh", fxAsOf: "2026-08-11", fxCnyPerUnit: { CNY: 1, USD: 7.18, EUR: 7.82 } });
    const staleMarkup = renderToStaticMarkup(createElement(PortfolioPanel, { mode: "public", currency: "CNY", rows: staleRows, capital: 100000, selectedBranch: "main", branches, stress: "main", onMode: () => undefined, onStress: () => undefined, onCompute: () => undefined, onNav: () => undefined, onExport: () => undefined, fmReady: true }));
    expect(staleMarkup).toContain("历史/模拟展示；过期或无效数据下，当前 Public Default 金额已停用。");
  });

  it("visibly disables Public Default when any portfolio-wide readiness gate has no eligible row", () => {
    const result = ({ weights: { weights: Float64Array.from([0.5, 0.2, 0.1, 0.1, 0.05, 0.05]) } } as never);
    const render = (options: Parameters<typeof allocationRows>[4], assets = ASSET_FIXTURES) => renderToStaticMarkup(createElement(PortfolioPanel, {
      mode: "public", currency: "CNY", rows: allocationRows(result, 100000, "CNY", "public", options, assets), capital: 100000,
      selectedBranch: "main", branches, stress: "main", onMode: () => undefined, onStress: () => undefined,
      onCompute: () => undefined, onNav: () => undefined, onExport: () => undefined, fmReady: true,
    }));

    const cases = [
      { runtimeAsOf: "2026-08-10T00:00:00Z", providerStatus: "fresh" as const },
      { runtimeAsOf: "2026-08-10T00:00:00Z", providerStatus: "unavailable" as const, fxProviderStatus: "fresh" as const, fxAsOf: "2026-08-10", fxCnyPerUnit: { CNY: 1, USD: 7.18, EUR: 7.82 } },
      { runtimeAsOf: "2026-08-10T00:00:00Z", providerStatus: "fresh" as const, fxProviderStatus: "stale" as const, fxAsOf: "2026-08-07", fxCnyPerUnit: { CNY: 1, USD: 7.18, EUR: 7.82 } },
    ];
    for (const options of cases) {
      const markup = render(options);
      expect(markup).toContain('disabled=""');
      expect(markup).toContain("Public Default 当前停用");
    }

    const staleAssets = ASSET_FIXTURES.map((asset) => ({ ...asset, asOf: "2026-08-07" }));
    const staleMarkup = render({ runtimeAsOf: "2026-08-10T00:00:00Z", providerStatus: "fresh" }, staleAssets);
    expect(staleMarkup).toContain('disabled=""');
    expect(staleMarkup).toContain("Public Default 当前停用");
    const missingAssets = ASSET_FIXTURES.map((asset) => ({ ...asset, asOf: "" }));
    const missingMarkup = render({ runtimeAsOf: "2026-08-10T00:00:00Z", providerStatus: "fresh" }, missingAssets);
    expect(missingMarkup).toContain('disabled=""');
    expect(missingMarkup).toContain("Public Default 当前停用");
  });
});
