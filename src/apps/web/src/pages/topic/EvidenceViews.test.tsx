import type { ResearchV2 } from "@foresightnine/contracts";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { parseCoverageMap, parseReconciliation } from "../../lib/research";
import { toolIcon } from "../../ui/icons";
import { EvidenceViews, stalenessLabel } from "./EvidenceViews";
import { ProcessTimeline } from "./ProcessTimeline";

const reconciliation = {
  version: 1,
  counts: { differs: 1, consistent: 1 },
  rows: [
    {
      row_id: "r1", title: { zh: "ANT 收盘价 · 2026-11-19", en: "ANT Close" }, status: "differs", severity: "material", badge: "differs_material",
      independent_source_count: 2, reference: { value: "115.10", source_class: "market_data" },
      cells: [
        { fact_id: "f1", source_id: "s1", source_class: "market_data", host: null, display_value: "115.10 USD", published_at: null, as_of: "2026-11-19", is_reference: true, low_ocr_confidence: false },
        { fact_id: "f2", source_id: "s2", source_class: "news", host: "cbsnews.com", display_value: "114.99 USD", published_at: null, as_of: null, is_reference: false, low_ocr_confidence: false },
      ],
    },
    {
      row_id: "r2", title: { zh: "ANT 开盘价 · 2026-11-19", en: "ANT Open" }, status: "rounding_only", severity: null, badge: "rounding_only",
      independent_source_count: 2, reference: null, cells: [],
    },
  ],
};

const coverage = {
  version: 1, run: { status: "incomplete" }, research_clock: { utc_date: "2026-11-20" },
  rows: [
    { category: "market_prices", label: { zh: "行情价格", en: "Market prices" }, status: "obtained", status_label: { zh: "已取得", en: "Obtained" }, data_as_of: "2026-11-17", is_stale: true, trading_days_behind: 2, days_behind_clock: 3, not_established: ["没有交易所行情：ANT"], sources_obtained: 1 },
    { category: "news_web", label: { zh: "新闻与网页", en: "News and web" }, status: "not_found", status_label: { zh: "未找到", en: "Not found" }, data_as_of: null, is_stale: null, trading_days_behind: null, days_behind_clock: null, not_established: [], sources_obtained: 0 },
  ],
  documents: [
    { source_id: "d1", category: "filings", title: "年报", origin: "regulatory_filing", ingestion_kind: "official", retrieval_method: "sec_filing_html", permissions: { store: true, model_input: true, display: true, export: false }, chars_total: 1000, read_fraction: 0.2, pages: { total: 10, by_status: { text: 8, needs_ocr: 2 }, read: 3, unread_text_pages: 5, unsearched_count: 2, ocr_low_confidence_pages: [4] } },
  ],
};

function turn(extra: Record<string, unknown>): ResearchV2.ResearchTurn {
  return { turn_id: "t1", ...extra } as unknown as ResearchV2.ResearchTurn;
}

describe("EvidenceViews", () => {
  it("renders data-as-of with staleness, the reconciliation table, the coverage map and the GDELT attribution", () => {
    render(<EvidenceViews turn={turn({ reconciliation, coverage_map: coverage, attributions: [{ id: "gdelt", text: "Source: The GDELT Project", url: "https://www.gdeltproject.org/" }] })} />);
    const asOf = screen.getByLabelText("数据截至");
    expect(within(asOf).getByText("2026-11-17")).toBeTruthy();
    expect(within(asOf).getByTestId("staleness").textContent).toBe("已落后 2 个交易日");
    const table = screen.getByRole("table");
    expect(within(table).getByText("差异较大")).toBeTruthy();
    expect(within(table).getByText("仅四舍五入差异")).toBeTruthy();
    expect(within(table).getByText("115.10 USD")).toBeTruthy();
    expect(within(table).getByText("114.99 USD")).toBeTruthy();
    expect(within(table).getByText(/新闻与网页 · cbsnews.com/)).toBeTruthy();
    expect(screen.getByText("没有交易所行情：ANT")).toBeTruthy();
    expect(screen.getByText("未找到")).toBeTruthy();
    expect(screen.getByLabelText("来源身份与覆盖").textContent).toContain("2 页待 OCR");
    expect(screen.getByLabelText("来源身份与覆盖").textContent).toContain("5 页文字未读");
    expect(screen.getByLabelText("来源身份与覆盖").textContent).toContain("监管披露");
    expect(screen.getByLabelText("来源身份与覆盖").textContent).toContain("模型使用");
    expect(screen.getByLabelText("来源身份与覆盖").textContent).not.toContain("导出");
    const link = screen.getByRole("link", { name: "Source: The GDELT Project" });
    expect(link.getAttribute("href")).toBe("https://www.gdeltproject.org/");
    expect(link.getAttribute("rel")).toContain("noopener");
  });

  it("distinguishes search attempts, sourced findings, and a single uncorroborated observation", () => {
    const single = { version: 1, rows: [{ row_id: "single", title: { zh: "收盘价", en: "Close" }, status: "single_source", badge: "single_source", independent_source_count: 1, cells: [{ fact_id: "f", source_id: "s", source_class: "market_data", display_value: "97.17 USD", quotation_excerpt: "Close 97.17" }] }] };
    render(<EvidenceViews turn={turn({ reconciliation: single, counter_evidence: { attempts: [{ scope: "external", target: "估值", hypothesis: "需求转弱", status: "completed" }, { scope: "document", target: "估值", hypothesis: "风险", status: "completed", source_id: "s" }], linked_findings: [{ target_claim_id: "c1", source_id: "s2", quotation: "Demand weakened" }] } })} />);
    expect(screen.getByText("外部搜索 1 次、文内搜索 1 次，找到 1 条可能的反证。")).toBeTruthy();
    expect(screen.getByText("Demand weakened")).toBeTruthy();
    expect(screen.getByText("来源独立性未核实")).toBeTruthy();
    expect(screen.getByText("仅单一来源，未交叉核实")).toBeTruthy();
    expect(screen.getByText("Close 97.17")).toBeTruthy();
  });

  it("renders nothing when a turn has no views and ignores malformed ones", () => {
    const { container } = render(<EvidenceViews turn={turn({ reconciliation: { rows: "x" }, coverage_map: 3, attributions: [] })} />);
    expect(container.textContent).toBe("");
    expect(parseReconciliation({ rows: [{ row_id: "x", status: "bogus" }] })).toBeNull();
    expect(parseCoverageMap({ rows: [{ category: 1 }] })?.rows).toEqual([]);
  });

  it("staleness label falls back to distance from the research clock", () => {
    const base = parseCoverageMap(coverage)!.rows[0]!;
    expect(stalenessLabel({ ...base, is_stale: false, days_behind_clock: 4 })?.text).toBe("距研究时点 4 天");
    expect(stalenessLabel({ ...base, is_stale: false, days_behind_clock: 0 })).toBeNull();
  });

  it("shows tool icons and the search_library and news_search summaries", () => {
    expect(toolIcon("search_library")).toBe("library");
    expect(toolIcon("news_search")).toBe("news");
    expect(toolIcon("issuer_documents")).toBe("globe");
    expect(toolIcon("extract_statements")).toBe("table");
    render(
      <ProcessTimeline
        events={[
          { index: 0, kind: "tool", name: "search_library", status: "completed", reason: null, rationale: "找营收", denied_count: null,
            summary: { kind: "search_library", query: "revenue", hit_count: 3, source_count: 2, unsearched_pages: 4, complete: false, hits_preview: [{ title: "年报", page: 7 }] } },
          { index: 1, kind: "tool", name: "news_search", status: "completed", reason: null, rationale: "找新闻", denied_count: 0,
            summary: { kind: "news_search", found_count: 5, eligible_count: 4, denied_count: 1, cluster_count: 3, attributions: ["gdelt"], leads_preview: [] } },
        ]}
      />,
    );
    expect(screen.getByText("检索资料库")).toBeTruthy();
    expect(screen.getByText(/命中 3 处 · 覆盖 2 份资料 · 4 页尚未提取文字，未检索/)).toBeTruthy();
    expect(screen.getByText(/归并为 3 组报道/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "The GDELT Project" })).toBeTruthy();
  });
});
