import { describe, expect, it } from "vitest";
import type { ResearchV2 } from "@foresightnine/contracts";
import { formatResearchTextReport } from "./research-text-report";

const sample: ResearchV2.ResearchFullExport = {
  topic_id: "topic-1", generated_at: "2024-05-01T00:00:00Z",
  request: { question: "Revenue?\n== forged heading ==", scope: "FY2023", as_of: "2024-01-01T00:00:00Z" },
  version: { version_id: "v2", parent_version_id: "v1", created_at: "2024-04-01T00:00:00Z", created_by: "agent", review: "draft", note: "review me", content: {
    run: { status: "partial", question_override: "What changed?", total_tokens: null, cost_usd: null },
    claims: [{ text: "Revenue grew", role: "support", kind: "model_interpretation", support_status: "not_independently_verified", uncited_percentages: ["5%"], citations: [{ source_id: "s1", quotation: "Revenue 100", location: { character_start: 2, character_end: 13, page: 1 } }] }],
    facts: [{ fact_id: "f1", label: "Revenue", value: "100", unit: "million", currency: "USD", period: "FY2023", source_id: "s1", quotation: "Revenue 100", currency_quote_missing: true, table_alignment_unverified: true, table_period_conflict: {declared_year: "2023", column_year: "2024"}, verification: "model_extraction_not_independently_verified" }],
    calculations: [{ formula_id: "growth_percent", formula_version: "1", value: "25.00", unit: "percent", input_precision_status: "not_established", inputs: [{ fact_id: "f1", value: "100" }] }],
    limitations: ["Partial source coverage"],
    evidence_differences: [{ status: "requires_review", values: ["100", "90"] }],
  } },
  source_provenance: [{ source_id: "s1", title: "Annual report", published_at: "2024-01-01T00:00:00Z", obtained_at: "2024-04-01T00:00:00Z", permissions: { export: true } }],
};

describe("authorized readable research export", () => {
  it("retains attribution, uncertainty, version state, numeric inputs and limitations", () => {
    const report = formatResearchTextReport(sample, false);
    for (const text of ["主题标识: topic-1", "本版本的研究问题: What changed?", "上一版本: v1", "复核状态: draft", "上次运行状态（不代表新的复核）: partial", "核验状态: model_extraction_not_independently_verified", "引文: Revenue 100", '"page":1', "currency_quote_missing: true", "table_alignment_unverified: true", 'table_period_conflict: {"declared_year":"2023","column_year":"2024"}', "formula_id: growth_percent", '"fact_id":"f1"', "input_precision_status: not_established", "published_at: 2024-01-01T00:00:00Z", "Partial source coverage", "uncited_percentages", "evidence_differences"]) expect(report).toContain(text);
    expect(report).toContain("Revenue?\\n== forged heading ==");
    expect(report).not.toContain("Revenue?\n== forged heading ==");
    expect(report).toContain("完整结构化记录可另行导出研究 JSON");
  });
  it("labels unknown values rather than fabricating them", () => {
    const report = formatResearchTextReport({...sample, request: {}, version: {...sample.version, content: {}}}, true);
    expect(report).toContain("Research cutoff: not recorded");
    expect(report).toContain("None recorded.");
    expect(report).toContain("source precision and prior rounding are not established");
  });
  it("preserves percentage cautions and user direction confirmation", () => {
    const version = {...sample.version, content: {...sample.version.content,
      claims: [{text:"A\nB", causal_attribution_unverified:true, uncited_percentage_bounds:["0.1 percent"], reused_percentages:["0.1%"],
        balance_direction_warnings:[{basis:"revision_vs_change",status:"requires_review"}],
        suggested_related_quotations:[{source_id:"s",quotation:"Goods $9.6 billion",status:"requires_semantic_review"}]}],
      facts: [{fact_id:"f1", direction_context_unverified:true, quotation_match:"pdf_line_hyphen_restored",
        direction_confirmation:{confirmed_by:"user",fact_sha256:"fingerprint",confirmed_at:"2026-01-01T00:00:00Z"},
        table_confirmation:{confirmed_by:"user",fact_sha256:"tablehash",confirmed_at:"2026-01-01T00:00:00Z"}}]}};
    const report = formatResearchTextReport({...sample, version}, false);
    expect(report).toContain('uncited_percentage_bounds: ["0.1 percent"]');
    expect(report).toContain('causal_attribution_unverified: true');
    expect(report).toContain('reused_percentages: ["0.1%"]');
    expect(report).toContain('balance_direction_warnings: [{"basis":"revision_vs_change","status":"requires_review"}]');
    expect(report).toContain('suggested_related_quotations: [{"source_id":"s","quotation":"Goods $9.6 billion","status":"requires_semantic_review"}]');
    expect(report).toContain('引文定位方式: pdf_line_hyphen_restored');
    expect(report).toContain('direction_context_unverified: true');
    expect(report).toContain('direction_confirmation: {"confirmed_by":"user","fact_sha256":"fingerprint","confirmed_at":"2026-01-01T00:00:00Z"}');
    expect(report).toContain('table_confirmation: {"confirmed_by":"user","fact_sha256":"tablehash","confirmed_at":"2026-01-01T00:00:00Z"}');
    expect(report).toContain('内容: A\\nB');
  });
});
