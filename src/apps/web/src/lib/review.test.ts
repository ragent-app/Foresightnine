import type { ResearchV2 } from "@foresightnine/contracts";
import { describe, expect, it } from "vitest";
import { buildVersionInput, diffIsEmpty, diffVersions, draftProblem, isDirty, pendingClaimEdits } from "./review";

const CITATION = { source_id: "s1", quotation: "Revenue 100.", location: { character_start: 0, character_end: 12 } };
const C1: ResearchV2.ResearchClaim = { claim_id: "C1", role: "support", text: "收入为 100。", turn_id: "t1", citations: [CITATION] };
const C2: ResearchV2.ResearchClaim = { claim_id: "C2", role: "counterargument", text: "利润率下降。", turn_id: "t1", citations: [CITATION] };
const FACT: ResearchV2.ResearchFact = {
  fact_id: "F1", turn_id: "t1", label: "总收入", value: "100", unit: "millions", currency: "USD", period: "FY2026", source_id: "s1",
  quotation: "Revenue 100.", character_start: 0, character_end: 12, location: { character_start: 0, character_end: 12 }, verification: "x",
};

function version(id: string, content: ResearchV2.ResearchContent, extra: Partial<ResearchV2.ResearchVersion> = {}): ResearchV2.ResearchVersion {
  return { version_id: id, created_at: "2026-11-19T00:00:00Z", review: "draft", note: "", content, source_ids: ["s1"], ...extra };
}

const V = version("v2", { summary: "", claims: [C1, C2], turns: [], facts: [FACT] });

describe("review draft", () => {
  it("is clean until a field actually differs from the version", () => {
    expect(isDirty(V, {})).toBe(false);
    expect(isDirty(V, { review: "draft", note: "", summary: "", claims: { C1: { text: " 收入为 100。 ", role: "support" } } })).toBe(false);
    expect(isDirty(V, { review: "confirmed" })).toBe(true);
    expect(isDirty(V, { claims: { C2: { text: "利润率下降。", role: "support" } } })).toBe(true);
  });

  it("ignores edits to claims that are no longer in the version", () => {
    expect(pendingClaimEdits(V, { claims: { gone: { text: "x", role: "support" } } })).toEqual([]);
  });

  it("requires a note for a reservation and non-empty claim text", () => {
    expect(draftProblem(V, { review: "reservation" })).toMatch(/保留意见/);
    expect(draftProblem(V, { review: "reservation", note: "第 2 条依据不足" })).toBeNull();
    expect(draftProblem(V, { claims: { C1: { text: "  ", role: "support" } } })).toBe("结论文字不能为空。");
  });

  it("builds the version body from the latest content, replacing only edited claims and the summary", () => {
    const body = buildVersionInput(V, { review: "confirmed", note: "看过", summary: "我的判断", claims: { C2: { text: " 利润率下降 2 个百分点。 ", role: "counterargument" } } });
    expect(body.parent_version_id).toBe("v2");
    expect(body.review).toBe("confirmed");
    expect(body.note).toBe("看过");
    expect(body.content.summary).toBe("我的判断");
    expect(body.content.claims![0]).toBe(C1);
    expect(body.content.claims![1]).toEqual({ ...C2, text: "利润率下降 2 个百分点。" });
    expect(body.content.facts).toBe(V.content.facts);
    expect(body.content.turns).toBe(V.content.turns);
  });
});

describe("diffVersions", () => {
  it("reports turns, sources, claims, facts and calculations added, removed or changed", () => {
    const calc = { calculation_id: "K1", formula_id: "growth_percent", value: "12.00", unit: "percent" } as ResearchV2.ResearchCalculation;
    const turn = { turn_id: "t2", index: 2, question: "Q2" } as ResearchV2.ResearchTurn;
    const next = version("v3", {
      summary: "新总结",
      turns: [turn],
      claims: [{ ...C1, text: "收入为 101。" }, { claim_id: "C3", role: "support", text: "新结论", citations: [CITATION] }],
      facts: [{ ...FACT, value: "101" }],
      calculations: [calc],
    }, { review: "confirmed", source_ids: ["s1", "s2"] });
    const diff = diffVersions(V, next);
    expect(diff.review).toEqual({ before: "draft", after: "confirmed" });
    expect(diff.summaryChanged).toBe(true);
    expect(diff.turnsAdded.map((item) => item.turn_id)).toEqual(["t2"]);
    expect(diff.sourcesAdded).toEqual(["s2"]);
    expect(diff.claimsAdded.map((item) => item.claimId)).toEqual(["C3"]);
    expect(diff.claimsChanged.map((item) => item.claimId)).toEqual(["C1"]);
    expect(diff.claimsRemoved.map((item) => item.claimId)).toEqual(["C2"]);
    expect(diff.factsChanged).toHaveLength(1);
    expect(diff.calculationsAdded).toEqual([calc]);
    expect(diffIsEmpty(diff)).toBe(false);
  });

  it("is empty for identical content", () => {
    expect(diffIsEmpty(diffVersions(V, version("v3", V.content)))).toBe(true);
  });
});
