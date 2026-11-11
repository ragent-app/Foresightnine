import { describe, expect, it, vi } from "vitest";
import { isValidElement, type ReactNode } from "react";
import { computeFm } from "@foresightnine/fm-engine";
vi.mock("react-native", () => ({ Text: "Text", TextInput: "TextInput", View: "View" }));
vi.mock("../src/ui", () => ({ Card: "Card", Status: "Status", styles: {} }));
import { ScenarioCard } from "../src/scenario-card";
import { scenarioNavFor } from "../src/scenario-inputs";
import { createPortfolioPlan } from "../src/portfolio";
import { fmInput } from "../src/fixtures";
type Props = { children?: ReactNode; accessibilityLabel?: string; value?: string; onChangeText?: (value: string) => void };
function nodes(node: ReactNode): Props[] { if (Array.isArray(node)) return node.flatMap(nodes); return isValidElement<Props>(node) ? [node.props, ...nodes(node.props.children)] : []; }
function content(node: ReactNode): string { if (Array.isArray(node)) return node.map(content).join(" "); return isValidElement<Props>(node) ? content(node.props.children) : typeof node === "string" || typeof node === "number" ? String(node) : ""; }
const rows = createPortfolioPlan(computeFm(fmInput()), 10000, "CNY").rows;
const input = { rows, capital: 10000, mode: "raw" as const, baselineDate: "2026-10-03", horizonEnd: "2027-10-03", returns: {} };
describe("scenario card (element tree, not native renderer)", () => {
  it("shows missing-input feedback, not a made-up history/return", () => {
    const tree = ScenarioCard({ scenario: scenarioNavFor("main", input), currency: "CNY", language: "zh", returns: {}, setReturn: vi.fn() });
    expect(content(tree)).toContain("填写所有持仓资产的收益假设后计算"); expect(content(tree)).not.toContain("假设期末收益");
    expect(nodes(tree).some(props => props.accessibilityLabel?.includes("路径，不含历史行情"))).toBe(false);
  });
  it("wires each asset input to the draft setter without prefilled forecast values", () => {
    const setReturn = vi.fn(); const scenario = scenarioNavFor("main", input);
    const tree = ScenarioCard({ scenario, currency: "CNY", language: "zh", returns: {}, setReturn });
    const first = scenario.inputs.find(item => !item.cash)!;
    const field = nodes(tree).find(props => props.accessibilityLabel === `${first.assetId} 假设收益率（%）`)!;
    expect(field.value).toBe(""); field.onChangeText?.("-8"); expect(setReturn).toHaveBeenCalledWith(first.assetId, "-8");
  });
  it("labels a completed path as user assumptions and states cash and interpolation limits", () => {
    const returns = Object.fromEntries(rows.map(row => [row.asset.id, "10"])); const scenario = scenarioNavFor("strong", { ...input, returns });
    const tree = ScenarioCard({ scenario, currency: "CNY", language: "en", returns, setReturn: vi.fn() });
    expect(content(tree)).toContain("Assumed terminal return"); expect(content(tree)).toContain("Cash earns 0%"); expect(content(tree)).toContain("Linear interpolation does not estimate intra-period risk");
    expect(nodes(tree).some(props => props.accessibilityLabel === "Hypothetical path from your inputs, no historical prices")).toBe(true);
  });
});
