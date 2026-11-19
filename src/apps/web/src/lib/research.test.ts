import { describe, expect, it } from "vitest";
import type { ResearchV2 } from "@foresightnine/contracts";
import type { ProgressEvent } from "../api/types";
import {
  ROLE_LABEL, answerSummary, buildCitationIndex, claimRole, parseTurnEvents, partialSummary, readCoverage, stepRows,
  suggestionsFor, toolStepCount, topicTitle, turnContent, turnDurationMs,
} from "./research";

const cite = (source_id: string, start: number, end: number): ResearchV2.Citation => ({
  source_id, quotation: "q", location: { character_start: start, character_end: end },
});
const claim = (claim_id: string, extra: Partial<ResearchV2.ResearchClaim> = {}): ResearchV2.ResearchClaim => ({
  claim_id, role: "support", text: claim_id, ...extra,
});
const turn = (turn_id: string, extra: Partial<ResearchV2.ResearchTurn> = {}): ResearchV2.ResearchTurn => ({
  turn_id, index: 1, question: "问题", question_origin: "user", status: "completed", reason: "submitted", finished_at: "2026-11-19T10:02:41Z",
  started_at: "2026-11-19T10:00:00Z", official_fetch_status: "x", search_status: "x", operation_ids: [], parent_version_id: "v0",
  usage: { model_calls: 1, search_calls: 0, total_tokens: 10 }, ...extra,
});

describe("parseTurnEvents", () => {
  it("whitelists fields, drops unknown kinds and keeps raw positions", () => {
    const events = parseTurnEvents([
      { kind: "model", finish_reason: "stop", usage: 120, usage_detail: { input_tokens: 100, output_tokens: 20 }, settlement: "ok", secret: "x" },
      { kind: "secret_kind", payload: "x" },
      "garbage",
      null,
      { kind: "tool", name: "read_sources", status: "read", reason: null, rationale: "r".repeat(400), denied_count: 2, arguments: { a: 1 } },
      { kind: "model_failure", code: "c".repeat(150) },
      { kind: "submission", status: "completed", extra: 1 },
    ]);
    expect(events.map((e) => e.index)).toEqual([0, 4, 5, 6]);
    expect(events[0]).toEqual({ index: 0, kind: "model", finish_reason: "stop", usage: 120, input_tokens: 100, output_tokens: 20, settlement: "ok" });
    const tool = events[1];
    expect(tool?.kind === "tool" && tool.rationale?.length).toBe(300);
    expect(tool).not.toHaveProperty("arguments");
    expect(tool?.kind === "tool" && tool.denied_count).toBe(2);
    const failure = events[2];
    expect(failure?.kind === "model_failure" && failure.code?.length).toBe(100);
    expect(events[3]).toEqual({ index: 6, kind: "submission", status: "completed" });
  });
  it("keeps only integer numbers and non-string text as null", () => {
    const [model, tool] = parseTurnEvents([
      { kind: "model", usage: 1.5, usage_detail: { input_tokens: "3", output_tokens: 4 }, finish_reason: 5 },
      { kind: "tool", name: 7, denied_count: true },
    ]);
    expect(model).toEqual({ index: 0, kind: "model", finish_reason: null, usage: null, input_tokens: null, output_tokens: 4, settlement: null });
    expect(tool).toEqual({ index: 1, kind: "tool", name: null, status: null, reason: null, rationale: null, denied_count: null });
  });
  it("handles undefined and missing usage_detail", () => {
    expect(parseTurnEvents(undefined)).toEqual([]);
    expect(parseTurnEvents([{ kind: "model", usage: 5 }])[0]).toMatchObject({ usage: 5, input_tokens: null });
  });
});

describe("stepRows", () => {
  const events: ProgressEvent[] = [
    { index: 0, kind: "model", finish_reason: null, usage: 1234, input_tokens: 1000, output_tokens: 234, settlement: null },
    { index: 1, kind: "model", finish_reason: null, usage: 1234, input_tokens: null, output_tokens: 5, settlement: null },
    { index: 2, kind: "model", finish_reason: null, usage: null, input_tokens: null, output_tokens: null, settlement: null },
    { index: 3, kind: "model_failure", code: "timeout" },
    { index: 4, kind: "model_failure", code: null },
    { index: 5, kind: "tool", name: "discover_sources", status: "unavailable", reason: "search_not_authorized_or_unavailable", rationale: "找线索", denied_count: null },
    { index: 6, kind: "tool", name: null, status: "weird", reason: null, rationale: null, denied_count: null },
    { index: 7, kind: "submission", status: "completed" },
  ];
  const rows = stepRows(events);
  it("describes model rows", () => {
    expect(rows[0]).toEqual({ kind: "model", index: 0, text: "模型调用 · 1,000 输入 / 234 输出 Token" });
    expect(rows[1]).toEqual({ kind: "model", index: 1, text: "模型调用 · 1,234 Token" });
    expect(rows[2]).toEqual({ kind: "model", index: 2, text: "模型调用" });
    expect(rows[3]).toEqual({ kind: "model", index: 3, text: "模型调用失败 · 请求没有返回可用结果（timeout）" });
    expect(rows[4]).toEqual({ kind: "model", index: 4, text: "模型调用失败 · 请求没有返回可用结果" });
  });
  it("names timeouts and dropped connections", () => {
    const failures = stepRows([
      { index: 0, kind: "model_failure", code: "provider_transport_error" },
      { index: 1, kind: "model_failure", code: "provider_timeout" },
    ]);
    expect(failures.map((row) => row.kind === "model" && row.text)).toEqual(["模型调用失败 · 连接中断", "模型调用失败 · 请求超时"]);
  });
  it("describes tool and submission rows with tones", () => {
    expect(rows[5]).toEqual({
      kind: "tool", index: 5, tool: "discover_sources", label: "搜索线索", status: "不可用", tone: "review", rationale: "找线索", reason: "搜索没有开启或不可用",
    });
    expect(rows[6]).toMatchObject({ tool: null, label: "工具", status: "weird", tone: "neutral", reason: null });
    expect(rows[7]).toEqual({ kind: "tool", index: 7, tool: "submit_research", label: "提交结论", status: "完成", tone: "support", rationale: null, reason: null });
  });
  it("maps every status to a tone", () => {
    const tone = (status: string): string | undefined => {
      const row = stepRows([{ index: 0, kind: "tool", name: "x", status, reason: null, rationale: null, denied_count: null }])[0];
      return row?.kind === "tool" ? row.tone : undefined;
    };
    for (const s of ["completed", "read", "acquired"]) expect(tone(s)).toBe("support");
    for (const s of ["invalid", "unavailable", "denied_or_unavailable"]) expect(tone(s)).toBe("review");
    for (const s of ["failed", "unknown"]) expect(tone(s)).toBe("error");
  });
  it("counts tool steps", () => {
    expect(toolStepCount(events)).toBe(3);
  });
});

describe("claimRole", () => {
  it("classifies claims", () => {
    expect(claimRole(claim("a"))).toBe("support");
    expect(claimRole(claim("a", { role: "counterargument" }))).toBe("counter");
    expect(claimRole(claim("a", { role: "counterargument", challenges_claim_ids: ["b"] }))).toBe("review");
    expect(claimRole(claim("a", { challenges_claim_ids: [] }))).toBe("support");
    expect(ROLE_LABEL).toEqual({ support: "支持", counter: "反方", review: "待复核" });
  });
});

describe("buildCitationIndex", () => {
  it("numbers in first-appearance order and dedupes across claims", () => {
    const claims = [
      claim("c1", { citations: [cite("s1", 0, 10), cite("s2", 5, 9)] }),
      claim("c2", { citations: [cite("s2", 5, 9), cite("s1", 10, 20), cite("s1", 0, 10)] }),
      claim("c3"),
    ];
    const index = buildCitationIndex(claims);
    expect(index.entries.map((e) => [e.n, e.claimId, e.citationIndex])).toEqual([[1, "c1", 0], [2, "c1", 1], [3, "c2", 1]]);
    expect(index.numberFor(cite("s2", 5, 9))).toBe(2);
    expect(index.numberFor(cite("s1", 0, 10))).toBe(1);
    expect(index.numberFor(cite("s1", 10, 20))).toBe(3);
    expect(index.numberFor(cite("s9", 0, 1))).toBe(0);
  });
  it("different quotation but same location reuses the number", () => {
    const index = buildCitationIndex([claim("c1", { citations: [{ ...cite("s1", 1, 2), quotation: "a" }, { ...cite("s1", 1, 2), quotation: "b" }] })]);
    expect(index.entries).toHaveLength(1);
  });
});

describe("turnContent", () => {
  const content: ResearchV2.ResearchContent = {
    claims: [claim("c1", { turn_id: "t1" }), claim("c2", { turn_id: "t2" }), claim("c3", { turn_id: "t1" })],
    facts: [{ fact_id: "f1", turn_id: "t2" } as ResearchV2.ResearchFact, { fact_id: "f2", turn_id: "t1" } as ResearchV2.ResearchFact],
    calculations: [{ calculation_id: "k1", turn_id: "t1" } as ResearchV2.ResearchCalculation],
  };
  it("selects by id lists preserving content order", () => {
    const result = turnContent(content, turn("t1", { claim_ids: ["c3", "c1"], fact_ids: ["f1"], calculation_ids: [] }));
    expect(result.claims.map((c) => c.claim_id)).toEqual(["c1", "c3"]);
    expect(result.facts.map((f) => f.fact_id)).toEqual(["f1"]);
    expect(result.calculations.map((c) => c.calculation_id)).toEqual(["k1"]);
  });
  it("falls back to turn_id", () => {
    const result = turnContent(content, turn("t2"));
    expect(result.claims.map((c) => c.claim_id)).toEqual(["c2"]);
    expect(result.facts.map((f) => f.fact_id)).toEqual(["f1"]);
    expect(result.calculations).toEqual([]);
  });
  it("tolerates missing arrays", () => {
    expect(turnContent({}, turn("t1"))).toEqual({ claims: [], facts: [], calculations: [] });
  });
});

describe("partialSummary", () => {
  it("composes the sentence", () => {
    expect(partialSummary({ facts: 3, calculations: 2, claims: 4 })).toBe("已记录 3 个数字，完成 2 项计算，形成 4 条结论。");
    expect(partialSummary({ facts: 3, calculations: 0, claims: 0 })).toBe("已记录 3 个数字，还没有形成可引用的结论。");
    expect(partialSummary({ facts: 0, calculations: 1, claims: 0 })).toBe("完成 1 项计算，还没有形成可引用的结论。");
    expect(partialSummary({ facts: 0, calculations: 0, claims: 2 })).toBe("形成 2 条结论。");
    expect(partialSummary({ facts: 0, calculations: 0, claims: 0 })).toBe("这一轮没有取得可用成果。");
  });
});

describe("answerSummary", () => {
  const content: ResearchV2.ResearchContent = {
    claims: [claim("c1", { turn_id: "t1" }), claim("c2", { turn_id: "t1" })],
    facts: [{ fact_id: "f1", turn_id: "t1" } as ResearchV2.ResearchFact],
    calculations: [{ calculation_id: "k1", turn_id: "t1" } as ResearchV2.ResearchCalculation, { calculation_id: "k2", turn_id: "t1" } as ResearchV2.ResearchCalculation],
  };
  it("lists all four chips in order", () => {
    const t = turn("t1", {
      read_ranges: [
        { source_id: "s1", character_start: 0, character_end: 5 },
        { source_id: "s1", character_start: 5, character_end: 9 },
        { source_id: "s2", character_start: 0, character_end: 5 },
      ],
      events: [{ kind: "tool", name: "read_sources" }, { kind: "model" }, { kind: "submission", status: "completed" }],
    });
    expect(answerSummary(t, content)).toEqual([
      { label: "2 条结论", tab: "claims" },
      { label: "1 个数字 · 2 项计算", tab: "numbers" },
      { label: "2 份资料", tab: "sources" },
      { label: "过程 2 步", tab: "steps" },
    ]);
  });
  it("omits empty chips", () => {
    expect(answerSummary(turn("t9"), content)).toEqual([]);
    expect(answerSummary(turn("t1", { fact_ids: ["f1"], calculation_ids: ["none"], claim_ids: ["none"] }), content)).toEqual([{ label: "1 个数字", tab: "numbers" }]);
  });
});

describe("turnDurationMs", () => {
  it("computes the difference and guards against bad dates", () => {
    expect(turnDurationMs(turn("t"))).toBe(161_000);
    expect(turnDurationMs(turn("t", { finished_at: "nope" }))).toBeNull();
  });
});

describe("readCoverage", () => {
  const content: ResearchV2.ResearchContent = {
    turns: [
      turn("t1", { read_ranges: [{ source_id: "s1", character_start: 100, character_end: 200 }, { source_id: "s2", character_start: 0, character_end: 50 }, { source_id: "s1", character_start: 0, character_end: 40 }] }),
      turn("t2", { read_ranges: [{ source_id: "s1", character_start: 150, character_end: 300 }, { source_id: "s1", character_start: 40, character_end: 60 }] }),
      turn("t3"),
    ],
    sources: [
      { source_id: "s1", initial_prompt_coverage: { text_characters: 1000 } },
      { source_id: "s2", initial_prompt_coverage: { text_characters: -1 } },
    ],
  };
  it("merges ranges across turns", () => {
    expect(readCoverage(content, "s1")).toEqual({ ranges: [[0, 60], [100, 300]], readCharacters: 260, totalCharacters: 1000 });
  });
  it("returns null totals when absent or invalid", () => {
    expect(readCoverage(content, "s2")).toEqual({ ranges: [[0, 50]], readCharacters: 50, totalCharacters: null });
    expect(readCoverage(content, "s3")).toEqual({ ranges: [], readCharacters: 0, totalCharacters: null });
    expect(readCoverage({}, "s1")).toEqual({ ranges: [], readCharacters: 0, totalCharacters: null });
  });
});

describe("topicTitle", () => {
  const topic = (question: string | undefined, turns: ResearchV2.ResearchTurn[]): ResearchV2.ResearchTopic => ({
    created_at: "", owner_id: "o", topic_id: "t", updated_at: "", request: question === undefined ? {} : { question },
    versions: [{ version_id: "v", created_at: "", review: "draft", content: { turns } }],
  });
  it("prefers the request question, then the first turn, then the default", () => {
    expect(topicTitle(topic("  苹果的毛利率  ", [turn("t1", { question: "别的" })]))).toBe("苹果的毛利率");
    expect(topicTitle(topic("   ", [turn("t1", { question: " 第一轮问题 " }), turn("t2", { question: "第二轮" })]))).toBe("第一轮问题");
    expect(topicTitle(topic(undefined, []))).toBe("文档概览");
  });
});

describe("suggestionsFor", () => {
  it("suggests gap follow-up from the last turn only", () => {
    const content: ResearchV2.ResearchContent = {
      turns: [turn("t1", { gaps: [{ code: "cancelled", message: "x" }] }), turn("t2", { gaps: [{ code: "no_progress", message: "y" }, { code: "cancelled", message: "z" }] })],
    };
    expect(suggestionsFor(content)).toEqual([
      { key: "gap", label: "补充上一轮的缺口", prefill: "请继续补充上一轮的缺口：模型重复相同操作，证据没有变化，已停止。" },
    ]);
    expect(suggestionsFor({ turns: [turn("t1", { gaps: [{ code: "cancelled", message: "x" }] }), turn("t2")] })).toEqual([]);
  });
  it("suggests counter evidence when a support claim exists", () => {
    const content: ResearchV2.ResearchContent = { claims: [claim("c1"), claim("c2", { role: "counterargument" })], turns: [turn("t1")] };
    expect(suggestionsFor(content)).toEqual([{ key: "counter", label: "寻找反方证据", prefill: "请寻找与当前主要结论相反的证据，并说明它们的强度。" }]);
    expect(suggestionsFor({ claims: [claim("c2", { role: "counterargument" })] })).toEqual([]);
  });
  it("returns both in order", () => {
    const content: ResearchV2.ResearchContent = { claims: [claim("c1")], turns: [turn("t1", { gaps: [{ code: "x", message: "m" }] })] };
    expect(suggestionsFor(content).map((s) => s.key)).toEqual(["gap", "counter"]);
  });
});
