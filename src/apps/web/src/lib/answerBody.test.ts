import { describe, expect, it } from "vitest";
import type { ResearchV2 } from "@foresightnine/contracts";
import { answerBody, cellText, conclusionText, currentCalculations, keyCalculations, readableNumber, shortPeriod, tableLayout } from "./answerBody";
import { latestFinds, liveStages, sourceHosts } from "./liveStages";
import type { ProgressEvent } from "../api/types";

const calc = (id: string, value: string, inputs: [string, string]): ResearchV2.ResearchCalculation => ({
  calculation_id: id, formula_id: "growth_percent", expression: "(first / second - 1) * 100", value, unit: "percent", currency: null,
  inputs: inputs.map((fact_id, i) => ({ fact_id, value: String(i), unit: "USD per share", period: i ? "2026-11-20 completed NASDAQ session" : "2026-11-23 completed NASDAQ session", currency: "USD" })),
} as unknown as ResearchV2.ResearchCalculation);

describe("answer body", () => {
  it("prefers the reviewed report and falls back to the labelled draft", () => {
    expect(answerBody({ report: " A ", draft_report: "B" } as ResearchV2.ResearchTurn)).toEqual({ text: "A", kind: "report" });
    expect(answerBody({ report: "", draft_report: "B" } as ResearchV2.ResearchTurn)).toEqual({ text: "B", kind: "draft" });
    expect(answerBody({ report: "" } as ResearchV2.ResearchTurn)).toBeNull();
  });

  it("drops a calculation superseded by a later one over the same inputs", () => {
    const wrong = calc("c1", "-23.94", ["f_low", "f_high"]);
    const fixed = calc("c2", "31.48", ["f_high", "f_low"]);
    const other = calc("c3", "6.97", ["q1", "q2"]);
    expect(currentCalculations([wrong, other, fixed]).map((c) => c.calculation_id)).toEqual(["c3", "c2"]);
  });

  it("shows only the numbers the answer states, in its order", () => {
    const rise = calc("c2", "31.48", ["a", "b"]);
    const qqq = calc("c3", "6.97", ["q1", "q2"]);
    const fall = calc("c4", "-39.27", ["d", "e"]);
    const body = "上涨 31.48%。此前跌幅 39.27%。QQQ 上涨 6.97%。另有 131.485 不是匹配。";
    expect(keyCalculations([qqq, fall, rise], body).map((c) => c.calculation_id)).toEqual(["c2", "c4", "c3"]);
    expect(keyCalculations([qqq], "没有提到数字")).toEqual([]);
    expect(keyCalculations([qqq, fall, rise], null)).toHaveLength(3);
  });

  it("shortens dated periods and provider float noise", () => {
    expect(shortPeriod("2026-11-20 completed NASDAQ session")).toBe("2026-11-20");
    expect(shortPeriod("FY2025")).toBe("FY2025");
    expect(readableNumber("165.050003")).toBe("165.05");
    expect(readableNumber("125.53")).toBe("125.53");
    expect(cellText("206.69 USD per share", "USD per share")).toBe("206.69");
    expect(cellText("165.050003 USD per share", null)).toBe("165.05 美元/股");
    expect(cellText("776,225,888 shares", null)).toBe("776,225,888 股");
    expect(cellText("1234.567891", null)).toBe("1,234.57");
    expect(cellText(null, null)).toBe("—");
  });

  it("drops a first column that repeats the row labels", () => {
    const table = {
      columns: [{ key: "d", label: "交易日", unit: null }, { key: "p", label: "收盘", unit: "USD per share" }],
      rows: [{ label: "2026-11-20", cells: { d: { display_value: "2026-11-20" }, p: { display_value: "125.53 USD per share" } } }],
    } as unknown as ResearchV2.ResearchTable;
    const layout = tableLayout(table);
    expect(layout.rowLabels).toBe(true);
    expect(layout.columns.map((c) => c.key)).toEqual(["p"]);
  });

  it("takes the marked conclusion for the topic summary", () => {
    const body = "## 标题\n\n**时间锚点：** 收于 165.05。[[claim:c1]]\n\n**核心判断：** CEO 复职是最直接的催化剂。\n\n[[table:t1]]";
    expect(conclusionText(body)).toBe("CEO 复职是最直接的催化剂。");
    expect(conclusionText("第一段。[[claim:c1]]\n\n第二段。")).toBe("第一段。");
  });
});

describe("live stages", () => {
  const tool = (index: number, name: string, summary: Record<string, unknown> = {}, status = "completed"): ProgressEvent =>
    ({ index, kind: "tool", name, status, reason: null, rationale: null, denied_count: null, summary } as unknown as ProgressEvent);

  it("folds tool calls into five stages", () => {
    const events = [
      tool(0, "plan_research_requirements", {}, "saved"),
      tool(1, "news_search"),
      tool(2, "acquire_sources", { kind: "acquire_sources", acquired_count: 2, items: [{ title: "Board reinstates CEO", status: "acquired" }, { title: "Blocked", status: "failed" }] }),
      tool(3, "read_sources", { kind: "read_sources", read_count: 3, items: [] }),
    ];
    const stages = liveStages(events, { kind: "tool", name: "record_facts" });
    expect(stages.map((s) => [s.key, s.state])).toEqual([["plan", "done"], ["find", "done"], ["read", "done"], ["compute", "active"], ["write", "pending"]]);
    expect(stages.every((stage) => stage.detail === "")).toBe(true);
    expect(latestFinds(events)).toEqual(["Board reinstates CEO"]);
  });

  it("returns to an earlier stage when the agent goes back for more material", () => {
    const events = [tool(0, "submit_research", {}, "needs_attention"), tool(1, "submit_research", {}, "needs_attention")];
    const stages = liveStages(events, { kind: "tool", name: "acquire_sources" });
    expect(stages.find((s) => s.key === "write")!.detail).toBe("第 2 稿");
    expect(stages.find((s) => s.key === "find")!.state).toBe("active");
    expect(stages.find((s) => s.key === "write")!.state).toBe("done");
  });
});

describe("source hosts", () => {
  it("lists the sites documents were obtained from, once each, in order", () => {
    const acquire = (index: number, items: Record<string, unknown>[]): ProgressEvent =>
      ({ index, kind: "tool", name: "acquire_sources", status: "completed", reason: null, rationale: null, denied_count: null,
         summary: { kind: "acquire_sources", items } } as unknown as ProgressEvent);
    const events = [
      acquire(0, [{ host: "WWW.CNBC.com", status: "acquired" }, { host: "apnews.com", status: "failed" }]),
      acquire(1, [{ host: "www.cnbc.com", status: "acquired" }, { host: "nvidianews.nvidia.com", status: "retrieved" }]),
    ];
    expect(sourceHosts(events)).toEqual(["www.cnbc.com", "nvidianews.nvidia.com"]);
  });
});
