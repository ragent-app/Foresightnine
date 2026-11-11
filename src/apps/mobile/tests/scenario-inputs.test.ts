import { describe, expect, it } from "vitest";
import { computeFm } from "@foresightnine/fm-engine";
import { ASSETS, fmInput } from "../src/fixtures";
import { createPortfolioPlan } from "../src/portfolio";
import { parseScenarioReturn, scenarioDraftFor, scenarioNavFor, updateScenarioDraft, type ScenarioInputs } from "../src/scenario-inputs";
import type { AllocationRow } from "../src/types";

const plan = createPortfolioPlan(computeFm(fmInput()), 10000, "CNY");
const row = (id: string, amount: number, cash = false): AllocationRow => ({ ...plan.rows[0]!, asset: { ...ASSETS[0]!, id, assetClass: cash ? "cash" : "equity" }, rawAmount: amount, publicAmount: amount / 2, rawWeight: 0.9, publicWeight: 0.7 });
const input = (patch: Partial<ScenarioInputs> = {}): ScenarioInputs => ({ capital: 1000, mode: "raw", rows: [row("A", 500), row("B", 200), row("CASH", 100, true)], baselineDate: "2026-10-03", horizonEnd: "2027-10-03", returns: { A: "10", B: "-20" }, ...patch });
describe("Android scenario assumptions", () => {
  it("has no historical or predicted curve without explicit assumptions", () => {
    const result = scenarioNavFor("main"); expect(result.status).toBe("missing_inputs"); expect(result.points).toEqual([]); expect(result.scenarioReturn).toBeNull(); expect(result.maxDrawdown).toBeNull();
  });
  it("uses rounded amounts, not target weights, and leaves cash at zero return", () => {
    const result = scenarioNavFor("main", input());
    expect(result.status).toBe("ready"); expect(result.scenarioReturn).toBeCloseTo(0.01, 12); expect(result.residualCash).toBe(200);
    expect(result.inputs.find(item => item.assetId === "A")?.weight).toBe(0.5); expect(result.inputs.find(item => item.cash)?.returnPercent).toBe(0);
    expect(result.points).toHaveLength(25); expect(result.points[0]?.nav).toBe(100); expect(result.points.at(-1)?.nav).toBeCloseTo(101); expect(result.points.every(point => point.segment === "simulated")).toBe(true);
    expect(result.points[0]?.date).toBe("2026-10-03"); expect(result.points.at(-1)?.date).toBe("2027-10-03");
  });
  it("recalculates against selected Public Default amounts", () => { expect(scenarioNavFor("main", input({ mode: "public" })).scenarioReturn).toBeCloseTo(0.005, 12); });
  it("never makes strong positive or weak negative solely because of the label", () => {
    expect(scenarioNavFor("strong", input()).points).toEqual(scenarioNavFor("weak", input()).points);
  });
  it("requires every held non-cash asset instead of assuming missing returns are zero", () => {
    const result = scenarioNavFor("weak", input({ returns: { A: "10" } })); expect(result.status).toBe("missing_inputs"); expect(result.missingAssets).toEqual(["B"]); expect(result.points).toEqual([]);
  });
  it("does not require values for unheld positions and ignores stale extra keys", () => {
    expect(scenarioNavFor("main", input({ rows: [row("A", 500), row("B", 0)], returns: { A: "10", unused: "NaN" } })).scenarioReturn).toBe(0.05);
  });
  it("calculates drawdown only on the assumed path and permits total loss without negative NAV", () => {
    const result = scenarioNavFor("strong", input({ rows: [row("A", 1000)], returns: { A: "-100" } }));
    expect(result.scenarioReturn).toBe(-1); expect(result.maxDrawdown).toBe(-1); expect(result.points.at(-1)?.nav).toBe(0);
    expect(result.points.every(point => point.nav >= 0 && point.drawdown <= 0)).toBe(true);
  });
  it.each(["", "NaN", "Infinity", "1e2", "10%", "0x10", "-100.1", "1000.1"])("rejects invalid return text %j", text => { expect(parseScenarioReturn(text)).toBeNull(); });
  it("accepts explicit zero, signs, decimals and both endpoints", () => {
    expect(["0", "+10", "-.5", "-100", "1000"].map(parseScenarioReturn)).toEqual([0, 10, -0.5, -100, 1000]);
  });
  it.each([{ capital: 0 }, { capital: NaN }, { horizonEnd: "2025-01-01" }, { baselineDate: "bad" }, { rows: [row("A", -1)] }, { rows: [row("A", 1001)] }, { rows: [row("A", 10), row("A", 20)] }])("fails closed on invalid allocation or dates: %j", patch => {
    const result = scenarioNavFor("main", input(patch)); expect(result.status).toBe("invalid_inputs"); expect(result.points).toEqual([]); expect(result.scenarioReturn).toBeNull();
  });
  it("wires actual portfolio rows and chosen horizon into the calculation without mutating FM", () => {
    const fm = computeFm(fmInput()); const weights = [...fm.weights.weights];
    const returns = Object.fromEntries(ASSETS.map(asset => [asset.id, "10"]));
    const result = createPortfolioPlan(fm, 10000, "CNY", undefined, "en", "strong", undefined, { mode: "raw", baselineDate: "2026-10-03", horizonEnd: "2027-10-03", returns });
    expect(result.scenario.status).toBe("ready");
    expect(result.scenario.scenarioReturn).toBeCloseTo(result.rows.filter(item => item.asset.assetClass !== "cash").reduce((sum, item) => sum + item.rawAmount, 0) / 10000 * 0.1, 12);
    expect([...fm.weights.weights]).toEqual(weights);
  });
});
describe("scoped per-worldline drafts", () => {
  it("retains other branches and refuses to show another account or research's assumptions", () => {
    let saved = { scope: "a:study-1", values: {} };
    saved = updateScenarioDraft(saved, "a:study-1", "weak", "A", "-10");
    saved = updateScenarioDraft(saved, "a:study-1", "strong", "A", "20");
    expect(scenarioDraftFor(saved, "a:study-1")).toEqual({ weak: { A: "-10" }, strong: { A: "20" } });
    expect(scenarioDraftFor(saved, "b:study-1")).toEqual({}); expect(scenarioDraftFor(saved, "a:study-2")).toEqual({});
    const replaced = updateScenarioDraft(saved, "b:study-1", "main", "B", "3"); expect(replaced.values).toEqual({ main: { B: "3" } });
  });
  it("rejects prototype keys and oversized draft input", () => {
    const saved = { scope: "a", values: {} };
    expect(updateScenarioDraft(saved, "a", "main", "__proto__", "1")).toBe(saved);
    expect(updateScenarioDraft(saved, "a", "main", "A", "1".repeat(25))).toBe(saved);
  });
});
