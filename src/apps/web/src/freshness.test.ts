import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { DEFAULT_FRESHNESS_POLICY } from "@foresightnine/domain";
import { ASSET_FIXTURES, RUNTIME_AS_OF, freshnessFor, fmInput, branches } from "./fixtures";
import { allocationRows, publicWeights } from "./state";
import { PortfolioPanel } from "./components/PortfolioPanel";

describe("Web freshness policy integration", () => {
  it("fails closed by default for offline fixture data", () => {
    expect(freshnessFor(ASSET_FIXTURES[0]!).allowCurrentPublicDefault).toBe(false);
    expect(publicWeights([0.5, 0.2, 0.1, 0.1, 0.05, 0.05])).toEqual([0, 0, 0, 0, 0, 0]);
  });

  it("keeps fresh and exact-threshold assets eligible but excludes stale and historical-only assets", () => {
    const exact = freshnessFor(ASSET_FIXTURES[0]!, { runtimeAsOf: "2026-08-10T00:00:00Z", providerStatus: "fresh", policy: DEFAULT_FRESHNESS_POLICY });
    expect(exact.status).toBe("fresh");
    expect(exact.allowCurrentPublicDefault).toBe(true);
    expect(freshnessFor(ASSET_FIXTURES[4]!, { runtimeAsOf: RUNTIME_AS_OF, providerStatus: "fresh" }).status).toBe("historical_only");

    const stale = { runtimeAsOf: RUNTIME_AS_OF, providerStatus: "fresh" as const };
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

  it("uses the same historical/simulated wording in the portfolio surface", () => {
    const result = allocationRows(null, 100000, "CNY", "public");
    const markup = renderToStaticMarkup(createElement(PortfolioPanel, { mode: "public", currency: "CNY", rows: result, capital: 100000, selectedBranch: "main", branches, stress: "main", onMode: () => undefined, onStress: () => undefined, onCompute: () => undefined, onNav: () => undefined, onExport: () => undefined, fmReady: false }));
    expect(markup).not.toContain("Historical/simulated display only");

    const staleRows = allocationRows(({ weights: { weights: Float64Array.from([0.2, 0.2, 0.2, 0.2, 0.1, 0.1]) } } as never), 100000, "CNY", "public", { runtimeAsOf: "2026-08-11T00:00:00Z", providerStatus: "fresh" });
    const staleMarkup = renderToStaticMarkup(createElement(PortfolioPanel, { mode: "public", currency: "CNY", rows: staleRows, capital: 100000, selectedBranch: "main", branches, stress: "main", onMode: () => undefined, onStress: () => undefined, onCompute: () => undefined, onNav: () => undefined, onExport: () => undefined, fmReady: true }));
    expect(staleMarkup).toContain("历史/模拟展示；过期或无效数据下，当前 Public Default 金额已停用。");
  });
});
