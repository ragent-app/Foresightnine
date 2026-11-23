import type { ResearchV2 } from "@foresightnine/contracts";
import { describe, expect, it } from "vitest";
import type { AvailableVersionSource } from "../api/types";
import { answerMarkdown, markdownToPlain, tableMarkdown } from "./answerCopy";
import { buildCitationIndex } from "./research";

const NOW = "2026-11-23T00:00:00Z";

function source(id: string, title: string, exportable: boolean, simulated = false): AvailableVersionSource {
  return {
    status: "available", source_id: id, title, kind: "user_supplied", is_simulated: simulated, published_at: null, event_at: null, revised_at: null,
    obtained_at: NOW, content_sha256: "a".repeat(64), retrieval_method: "user_supplied", pdf_text_order: null, derived_from_source_id: null,
    permissions: { store: true, model_input: true, display: true, export: exportable }, verification: "user_declared_not_verified",
  } as AvailableVersionSource;
}

const CLAIMS: ResearchV2.ResearchClaim[] = [
  { claim_id: "C1", role: "support", text: "收入 100。", turn_id: "t1", citations: [{ source_id: "s1", quotation: "Revenue was 100.", location: { character_start: 0, character_end: 16 } }] },
  { claim_id: "C2", role: "support", text: "利润 20。", turn_id: "t1", citations: [{ source_id: "s2", quotation: "Profit was 20.", location: { character_start: 5, character_end: 19 } }] },
  { claim_id: "C3", role: "counterargument", text: "增速放缓。", turn_id: "t1", citations: [{ source_id: "s1", quotation: "Growth slowed.", location: { character_start: 20, character_end: 34 } }] },
];

const TABLE: ResearchV2.ResearchTable = {
  table_id: "tb1", title: "关键数字", columns: [{ key: "fy", label: "2026", unit: "USD" }],
  rows: [{ label: "收入 | 合计", cells: { fy: { kind: "fact", display_value: "100" } } }],
} as unknown as ResearchV2.ResearchTable;

function turn(overrides: Partial<ResearchV2.ResearchTurn> = {}): ResearchV2.ResearchTurn {
  return {
    turn_id: "t1", index: 1, question: "收入多少？", question_origin: "user", status: "completed", reason: "submitted",
    started_at: NOW, finished_at: NOW, parent_version_id: "v1", operation_ids: ["op"], official_fetch_status: "not_requested",
    search_status: "not_requested", usage: { model_calls: 1, search_calls: 0, total_tokens: 1 },
    report: "## 结论\n\n收入为 100。[[claim:C1]] 利润为 20。[[claim:C2]]\n\n[[table:tb1]]\n\n```\n[[claim:C1]]\n```",
    claim_ids: ["C1", "C2", "C3"], fact_ids: [], calculation_ids: [], gaps: [], events: [], ...overrides,
  } as ResearchV2.ResearchTurn;
}

const context = {
  claims: CLAIMS, turnClaims: CLAIMS, tables: [TABLE], charts: [],
  citations: buildCitationIndex(CLAIMS),
  sources: new Map([["s1", source("s1", "年报", true)], ["s2", source("s2", "内部纪要", false, true)]]),
  sourceTitle: (id: string) => id,
};

describe("answerMarkdown", () => {
  it("turns claim markers into citation numbers and lists sources by permission", () => {
    const md = answerMarkdown(turn(), context);
    expect(md).toContain("收入为 100。[1] 利润为 20。[2]");
    // Fenced code keeps its literal marker.
    expect(md).toContain("```\n[[claim:C1]]\n```");
    // Inline table anchor becomes a Markdown table, with pipes escaped.
    expect(md).toContain("| 收入 \\| 合计 | 100 |");
    // The counter claim is not in the report, so it is listed with its own citation.
    expect(md).toContain("## 反方与待复核\n- 反方：增速放缓。[3]");
    expect(md).toContain("[1] 《年报》：“Revenue was 100.”（字符 0–16）");
    // Not exportable: title only, and simulated data is labelled.
    expect(md).toContain("[2] 《内部纪要》（模拟数据）（该资料不允许导出原文）");
    expect(md).not.toContain("Profit was 20.");
  });

  it("falls back to the supporting claims when there is no report", () => {
    const md = answerMarkdown(turn({ report: "" }), context);
    expect(md.startsWith("- 收入 100。[1]\n- 利润 20。[2]")).toBe(true);
  });

  it("adds trailing tables that the report does not place inline", () => {
    const md = answerMarkdown(turn({ report: "只有正文。", table_ids: ["tb1"] }), { ...context, turnClaims: [] });
    expect(md).toBe(`只有正文。\n\n${tableMarkdown(TABLE)}`);
  });
});

describe("markdownToPlain", () => {
  it("removes Markdown syntax and turns tables into tab-separated rows", () => {
    const plain = markdownToPlain("## 结论\n\n**收入** 为 `100`，见 [年报](https://example.com/a)。\n\n| 指标 | 值 |\n| --- | ---: |\n| 收入 \\| 合计 | 100 |\n\n> 注意");
    expect(plain).toBe("结论\n\n收入 为 100，见 年报（https://example.com/a）。\n\n指标\t值\n收入 | 合计\t100\n\n注意");
  });
});
