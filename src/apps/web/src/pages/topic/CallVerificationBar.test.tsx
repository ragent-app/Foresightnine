import fs from "node:fs";
import path from "node:path";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CallAuditReportEnvelope, CallVerificationReport } from "../../api/callVerification";
import { jsonResponse, storeSession } from "../../test/fixtures";
import { CallVerificationBar } from "./CallVerificationBar";
import { TopicContext, type TopicView } from "./context";
import { ReaderPanel } from "./ReaderPanel";

function buildSampleReport(overrides: Partial<CallVerificationReport> = {}): CallVerificationReport {
  return {
    schema_version: "1",
    owner_id: "acct_1",
    entity_key: "0001045810",
    cik: "0001045810",
    symbol: "NVDA",
    company_name: "NVIDIA Corporation",
    fiscal_period_key: "FY2026-Q3",
    call_date: "2025-11-19",
    call_default_basis: "non_gaap",
    ledger_status: "same_period_10q_matched",
    awaiting_current_10q: false,
    overall_light: "red",
    headline: "FY2026-Q3 · 历史口头承诺违背 · gross_margin (FY2026-Q4) · Q&A 回避 1/2 轮",
    red_count: 1,
    yellow_count: 1,
    green_count: 1,
    evasion_red_yellow_count: 1,
    evasion_total_exchanges: 2,
    evasion_distribution: { direct: 1, intermediate: 0, fully_evasive: 1 },
    confidentiality_whitelisted_count: 1,
    collision_findings: [
      {
        finding_id: "f_1",
        track: "track_6_commitments",
        collision_kind: "direct_contradiction",
        severity: "red",
        title: "历史口头承诺违背 · gross_margin (FY2026-Q4)",
        management_statement: "管理层曾承诺 FY2026-Q4 GAAP 毛利率保持在 75% 以上。",
        ledger_evidence: "10-Q 首次披露 GAAP 毛利率为 68.00%，低于承诺 700 bps。",
        formula_note: "GrossProfit / Revenues = 68.00% (initial-filing basis)",
        basis_badge: "GAAP",
        period_badge: "FY2026-Q4",
        restated_later: true,
        restatement_note: "后续季报存在拆股/重述调整，已按首次归档口径碰撞。",
        skeptic_note: "需结合 Blackwell 产能爬坡初期良率影响解读。",
        quotes: [
          {
            source_id: "src_call_q3",
            quotation: "We expect GAAP gross margin to remain at or above 75% in Q4.",
            verified: true,
            raw_char_start: 120,
            raw_char_end: 182,
            slide_number: 4,
            timestamp_start: "14:22",
            match_space: "raw",
          },
        ],
      },
      {
        finding_id: "f_2",
        track: "track_2_earnings_cash",
        collision_kind: "basis_divergence_nongaap_gap",
        severity: "yellow",
        title: "Non-GAAP vs GAAP 调节差偏大 (500 bps) · gross_margin",
        management_statement: "Non-GAAP gross margin was 73.0%.",
        ledger_evidence: "GAAP gross margin 68.0%, 调节差 500 bps。",
        basis_badge: "Non-GAAP vs GAAP",
        quotes: [],
      },
    ],
    qa_evasions: [
      {
        finding_id: "ev_2",
        exchange_index: 2,
        analyst_name: "Stacy Rasgon",
        analyst_firm: "Bernstein",
        executive_name: "Colette Kress",
        executive_role: "cfo",
        evasion_level: "fully_evasive",
        behavior_subtype: "topic_pivot",
        display_severity: "red",
        legitimate_confidentiality: false,
        collapsed_by_default: false,
        sub_questions: ["Q4 Blackwell gross margin", "China export tariff exposure"],
        unanswered_sub_questions: ["Q4 Blackwell gross margin", "China export tariff exposure"],
        topic_tags: ["gross_margin", "china"],
        followup_count: 1,
        rationale: "高管转向谈论长期主权 AI 需求，未正面回答 Q4 毛利率与关税影响。",
        question_quote: {
          source_id: "src_call_q3",
          quotation: "Can you quantify Q4 Blackwell gross margin and China tariff exposure?",
          verified: true,
          raw_char_start: 510,
          raw_char_end: 579,
          page_number: 7,
        },
        answer_quote: {
          source_id: "src_call_q3",
          quotation: "Sovereign AI adoption across Europe and Asia is accelerating rapidly.",
          verified: true,
          raw_char_start: 590,
          raw_char_end: 659,
          page_number: 7,
        },
      },
      {
        finding_id: "ev_1",
        exchange_index: 1,
        analyst_name: "Vivek Arya",
        analyst_firm: "BofA",
        executive_name: "Jensen Huang",
        executive_role: "ceo",
        evasion_level: "direct",
        behavior_subtype: "omission",
        display_severity: "info",
        legitimate_confidentiality: true,
        collapsed_by_default: true,
        sub_questions: ["single-customer pricing with Microsoft"],
        unanswered_sub_questions: ["single-customer pricing with Microsoft"],
        topic_tags: ["pricing"],
        followup_count: 0,
        rationale: "涉及单客户定价机密，适用 D3 双条件合规保密豁免。",
        question_quote: {
          source_id: "src_call_q3",
          quotation: "What exact unit discount did you offer Microsoft on GB200 racks?",
          verified: true,
        },
        answer_quote: {
          source_id: "src_call_q3",
          quotation: "Per our customer NDA policy, we do not disclose single-customer pricing.",
          verified: true,
        },
      },
    ],
    commitments: [
      {
        commitment_id: "cmt_gm_q4",
        entity_key: "0001045810",
        source_id: "src_call_q3",
        source_fiscal_period: "FY2026-Q3",
        call_date: "2025-11-19",
        metric_canonical: "gross_margin",
        basis: "gaap",
        effective_basis: "gaap",
        period_scope: "single_quarter",
        target_period_key: "FY2026-Q4",
        operator: "ge",
        value_low: "75.0",
        unit: "%",
        verification_tier: "xbrl_auto",
        status: "missed",
        actual_value: "68.00%",
        delta_note: "低于承诺下限 700 bps",
        commitment_text: "FY2026-Q4 GAAP 毛利率保持 >= 75%",
        muted: false,
        quote: {
          source_id: "src_call_q3",
          quotation: "We expect GAAP gross margin to remain at or above 75% in Q4.",
          verified: true,
          raw_char_start: 120,
          raw_char_end: 182,
        },
      },
    ],
    speaker_baseline_shifts: [
      {
        executive_name: "Colette Kress",
        speaker_role: "cfo",
        quarters_observed: 3,
        current_hedge_density: 0.42,
        historical_avg_hedge_density: 0.18,
        hedge_density_delta: 0.24,
        current_evasion_rate_pct: 50,
        historical_avg_evasion_rate_pct: 15,
        evasion_rate_delta_pp: 35,
        severity: "yellow",
        summary: "相对近 3 季自身基线，防御措辞密度上升 +0.24，回避率上升 +35pp。",
      },
    ],
    expectation_benchmarks: [
      {
        tier: "v25_historical_credibility",
        authority_tier: "sec_public_prior",
        title: "V2.5 历史指引信用（只读先验）",
        summary: "过去 6 季指引兑现率为 100%（保守超预期型）。",
      },
      {
        tier: "sellside_third_party",
        authority_tier: "third_party_opinion",
        title: "Q3 FY26 Revenue Preview",
        summary: "Bernstein 预计 Q3 收入 $35.2B。",
        sellside_firm: "Bernstein Equity Research",
        sellside_timing: "pre_earnings_preview",
      },
    ],
    skeptic_calibration: ["高管回避可能受产能爬坡不确定性影响，建议交叉比对供应商月度营收。"],
    open_gaps: ["缺少电话会完整中文同传校对稿。"],
    source_ids: ["src_call_q3"],
    verified_quote_count: 5,
    total_quote_count: 5,
    rules_sha256: "abcdef1234567890",
    evaluation_rules_version: "1.0.0",
    context_budget_version: "1.0.0",
    text_only_evasion_disclaimer: "仅基于电话会文本语义与账本数据进行响应核验，不构成声学测谎或情绪断言。",
    model_id: "glm-5.3-flash",
    total_tokens: 6032,
    llm_call_count: 1,
    generated_at: "2026-11-25T11:00:00Z",
    ...overrides,
  };
}

function sseResponse(frames: string[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const f of frames) controller.enqueue(encoder.encode(f));
      controller.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": "text/event-stream; charset=utf-8" },
  });
}

describe("CallVerificationBar & CallVerificationPanel (V3a-3)", () => {
  beforeEach(() => {
    storeSession();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("enforces strict mount zero-outbound contract when not awaiting_current_10q", async () => {
    const envelope: CallAuditReportEnvelope = {
      available: true,
      awaiting_current_10q: false,
      last_filing_fingerprint_check_at: null,
      report: buildSampleReport(),
      markdown: "# NVDA Call Audit",
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse(envelope));

    render(
      <CallVerificationBar
        topicId="topic_nvda_1"
        sourceIds={["src_call_q3"]}
        symbol="NVDA"
        onAddSources={vi.fn()}
        onInspectQuote={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText(/历史口头承诺违背/)).toBeTruthy();
    });
    // Wait a tick to ensure no deferred check-filings fires
    await new Promise((r) => setTimeout(r, 25));

    const urls = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(urls.length).toBe(1);
    expect(urls[0]).toContain("/v2/research/call-audit/report?");
    expect(urls[0]).toContain("cache_only=true");
    expect(urls.some((u) => u.includes("/check-filings"))).toBe(false);
    expect(urls.some((u) => u.includes("/call-audit/run"))).toBe(false);
  });

  it("triggers /check-filings ONLY when awaiting_current_10q=true AND cooldown > 30m", async () => {
    const staleAt = new Date(Date.now() - 45 * 60 * 1000).toISOString();
    const freshAt = new Date(Date.now() - 5 * 60 * 1000).toISOString();

    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/call-audit/report")) {
        return jsonResponse({
          available: true,
          awaiting_current_10q: true,
          last_filing_fingerprint_check_at: staleAt,
          report: buildSampleReport({ awaiting_current_10q: true, ledger_status: "awaiting_current_10q_using_8k_and_prior_10q" }),
          markdown: "# NVDA",
        });
      }
      if (url.includes("/call-audit/check-filings")) {
        return jsonResponse({ checked_ciks: ["0001045810"], triggered_ciks: [], inbox_item_ids: [], cooldown_minutes: 30 });
      }
      return jsonResponse({});
    });

    const { unmount } = render(
      <CallVerificationBar
        topicId="topic_wait_stale"
        sourceIds={["src_call_q3"]}
        onAddSources={vi.fn()}
        onInspectQuote={vi.fn()}
      />,
    );

    await waitFor(() => {
      const urls = fetchMock.mock.calls.map((c) => String(c[0]));
      expect(urls.some((u) => u.includes("/call-audit/check-filings"))).toBe(true);
    });
    unmount();

    // Now test within 30m cooldown -> MUST NOT call /check-filings!
    fetchMock.mockClear();
    fetchMock.mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/call-audit/report")) {
        return jsonResponse({
          available: true,
          awaiting_current_10q: true,
          last_filing_fingerprint_check_at: freshAt,
          report: buildSampleReport({ awaiting_current_10q: true }),
          markdown: "# NVDA",
        });
      }
      return jsonResponse({});
    });

    render(
      <CallVerificationBar
        topicId="topic_wait_fresh"
        sourceIds={["src_call_q3"]}
        onAddSources={vi.fn()}
        onInspectQuote={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(fetchMock.mock.calls.length).toBe(1);
    });
    await new Promise((r) => setTimeout(r, 25));
    const urls2 = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(urls2.some((u) => u.includes("/call-audit/check-filings"))).toBe(false);
  });

  it("renders all 3 L2 tabs, supports quote inspect, D3 collapse, D7 mute, Markdown copy, and has ZERO inline styles (CSP)", async () => {
    const onInspectQuote = vi.fn();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });

    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.includes("/call-audit/report")) {
        return jsonResponse({
          available: true,
          awaiting_current_10q: false,
          report: buildSampleReport(),
          markdown: "# NVDA Call Audit Brief",
        });
      }
      if (url.includes("/call-audit/commitments/cmt_gm_q4") && init?.method === "PATCH") {
        return jsonResponse({ commitment_id: "cmt_gm_q4", entity_key: "0001045810", muted: true });
      }
      return jsonResponse({});
    });

    const { container } = render(
      <CallVerificationBar
        topicId="topic_nvda_tabs"
        sourceIds={["src_call_q3"]}
        initialExpanded
        onAddSources={vi.fn()}
        onInspectQuote={onInspectQuote}
      />,
    );

    // Tab 1: Talk vs. Walk
    await waitFor(() => {
      expect(screen.getByTestId("call-verification-panel")).toBeTruthy();
    });
    expect(container.querySelectorAll("[style]").length).toBe(0);

    expect(screen.getByText(/Slide 4 · 14:22/)).toBeTruthy();
    fireEvent.click(screen.getByText(/We expect GAAP gross margin/));
    expect(onInspectQuote).toHaveBeenCalledWith(
      expect.objectContaining({
        source_id: "src_call_q3",
        raw_char_start: 120,
        raw_char_end: 182,
        slide_number: 4,
      }),
    );

    // Copy Markdown
    fireEvent.click(screen.getByRole("button", { name: "复制 Markdown 审计简报" }));
    expect(writeText).toHaveBeenCalledWith("# NVDA Call Audit Brief");

    // Tab 2: Q&A Evasion Radar
    fireEvent.click(screen.getByRole("tab", { name: /Tab 2/ }));
    expect(screen.getByText(/未答：Q4 Blackwell gross margin/)).toBeTruthy();
    expect(screen.getByText(/跨季高管个人基线漂移/)).toBeTruthy();
    // D3 confidentiality item is collapsed by default
    expect(screen.queryByText(/single-customer pricing/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /展开直接回答与合规保密轮次/ }));
    expect(screen.getByText(/未答：single-customer pricing with Microsoft/)).toBeTruthy();
    expect(screen.getAllByText(/合规保密豁免/).length).toBeGreaterThanOrEqual(2);
    expect(container.querySelectorAll("[style]").length).toBe(0);

    // Tab 3: Commitments & 4-tier expectation isolation + D7 opt-out mute
    fireEvent.click(screen.getByRole("tab", { name: /Tab 3/ }));
    expect(screen.getByText("V2.5 历史指引信用 · SEC 公开先验")).toBeTruthy();
    expect(screen.getByText("卖方研报预期 · 第三方观点")).toBeTruthy();
    expect(screen.getByText("Bernstein Equity Research")).toBeTruthy();
    expect(container.querySelectorAll("[style]").length).toBe(0);

    fireEvent.click(screen.getByRole("button", { name: "忽略" }));
    await waitFor(() => {
      expect(fetchMock.mock.calls.some((c) => String(c[0]).includes("/call-audit/commitments/cmt_gm_q4"))).toBe(true);
    });

    // Static source check: zero inline `style=` across both V3a TSX files
    const barSrc = fs.readFileSync(path.resolve(__dirname, "CallVerificationBar.tsx"), "utf8");
    const panelSrc = fs.readFileSync(path.resolve(__dirname, "CallVerificationPanel.tsx"), "utf8");
    expect(/\bstyle\s*=/.test(barSrc)).toBe(false);
    expect(/\bstyle\s*=/.test(panelSrc)).toBe(false);
  });

  it("auto-sniffs attached sources via /preview, runs async SSE, and supports Cancel", async () => {
    const cancelSpy = vi.fn();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.includes("/call-audit/report")) {
        return jsonResponse({ available: false, awaiting_current_10q: false, report: null });
      }
      if (url.includes("/call-audit/preview")) {
        return jsonResponse({
          auto_trigger_eligible: true,
          detected_symbol: "NVDA",
          mgmt_source_ids: ["src_new_call"],
          sellside_source_ids: [],
          total_pending_ocr_pages: 0,
          call_default_basis: "non_gaap",
          sources: [],
        });
      }
      if (url.includes("/call-audit/run")) {
        return jsonResponse({ operation_id: "op_v3a_live", status: "running", entity_key: "0001045810" }, { status: 202 });
      }
      if (url.includes("/call-audit/operations/op_v3a_live/events")) {
        return sseResponse([
          'id: 1\nevent: layer1_ready\ndata: {"sequence":1,"stage":"layer1_ready"}\n\n',
        ]);
      }
      if (url.includes("/call-audit/operations/op_v3a_live/cancel") && init?.method === "POST") {
        cancelSpy();
        return jsonResponse({ operation_id: "op_v3a_live", status: "cancelled" });
      }
      if (url.includes("/call-audit/operations/op_v3a_live")) {
        return jsonResponse({
          operation_id: "op_v3a_live",
          status: "running",
          stage: "layer2_extracting",
          stages: [],
        });
      }
      return jsonResponse({});
    });

    render(
      <CallVerificationBar
        topicId="topic_auto_sniff"
        sourceIds={["src_new_call"]}
        attachedTrigger={{ sourceIds: ["src_new_call"], seq: 1 }}
        onAddSources={vi.fn()}
        onInspectQuote={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText(/已识别电话会材料/)).toBeTruthy();
    });

    const cancelButtons = screen.getAllByRole("button", { name: "取消" });
    fireEvent.click(cancelButtons[0]!);
    await waitFor(() => {
      expect(cancelSpy).toHaveBeenCalledTimes(1);
    });
  });

  it("defines EVERY CSS custom property referenced in index.css (0 undefined vars) and survives sourceIds churn during auto-sniff", async () => {
    // 1. Static CSS token integrity audit across styles/index.css
    const cssPath = path.resolve(__dirname, "../../styles/index.css");
    const cssText = fs.readFileSync(cssPath, "utf8");
    const defined = new Set<string>();
    for (const m of cssText.matchAll(/(--[a-zA-Z0-9_-]+)\s*:/g)) {
      if (m[1]) defined.add(m[1]);
    }
    const undefinedVars = new Set<string>();
    for (const m of cssText.matchAll(/var\(\s*(--[a-zA-Z0-9_-]+)/g)) {
      const name = m[1];
      if (!name || name.startsWith("--radix-")) continue;
      if (!defined.has(name)) undefinedVars.add(name);
    }
    expect([...undefinedVars]).toEqual([]);

    // 2. Auto-sniff MUST NOT abort /preview when Topic re-renders with a new sourceIds array reference
    let previewAborted = false;
    let resolvePreview!: (resp: Response) => void;
    const previewPending = new Promise<Response>((res) => {
      resolvePreview = res;
    });

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.includes("/call-audit/report")) {
        return jsonResponse({ available: false, awaiting_current_10q: false, report: null });
      }
      if (url.includes("/call-audit/preview")) {
        init?.signal?.addEventListener("abort", () => {
          previewAborted = true;
        });
        return previewPending;
      }
      if (url.includes("/call-audit/run")) {
        return jsonResponse({ operation_id: "op_live_skel", status: "running", entity_key: "0001045810" }, { status: 202 });
      }
      if (url.includes("/call-audit/operations/op_live_skel/events")) {
        return sseResponse([
          'id: 1\nevent: layer0_ready\ndata: {"sequence":1,"stage":"layer0_ready","payload":{"entity_key":"0001045810","fiscal_period_key":"FY2026-Q3","qa_exchange_count":6}}\n\n',
          'id: 2\nevent: layer1_ready\ndata: {"sequence":2,"stage":"layer1_ready","payload":{"ledger_status":"same_period_10q_matched"}}\n\n',
        ]);
      }
      if (url.includes("/call-audit/operations/op_live_skel")) {
        return jsonResponse({
          operation_id: "op_live_skel",
          status: "running",
          stage: "layer2_extracting",
          stages: [],
        });
      }
      return jsonResponse({});
    });

    const trig = { sourceIds: ["src_new_1"], seq: 1 };
    const { rerender } = render(
      <CallVerificationBar
        topicId="topic_churn"
        sourceIds={["src_old_1"]}
        attachedTrigger={trig}
        onAddSources={vi.fn()}
        onInspectQuote={vi.fn()}
      />,
    );
    // Simulate Topic re-render with brand-new sourceIds array reference while /preview is still in flight!
    rerender(
      <CallVerificationBar
        topicId="topic_churn"
        sourceIds={["src_old_1", "src_new_1"]}
        attachedTrigger={trig}
        onAddSources={vi.fn()}
        onInspectQuote={vi.fn()}
      />,
    );
    expect(previewAborted).toBe(false);

    // Resolve preview -> triggers SSE -> renders progressive Layer 0/1 live skeleton card!
    resolvePreview(
      jsonResponse({
        auto_trigger_eligible: true,
        entity_key: "0001045810",
        detected_symbol: "NVDA",
        management_source_ids: ["src_new_1"],
        sellside_source_ids: [],
        total_pending_ocr_pages: 0,
        call_default_basis: "non_gaap",
        sources: [],
      }),
    );

    await waitFor(() => {
      expect(screen.getByTestId("call-audit-live-skeleton")).toBeTruthy();
      expect(screen.getByText(/实体 0001045810/)).toBeTruthy();
      expect(screen.getByText(/已识别 Q&A 6 轮/)).toBeTruthy();
    });
  });

  it("renders ReaderPanel <mark class='on'> with strict priority over overlapping cited spans and shows Slide N for pptx_slides_v1", async () => {
    const rawText =
      "Intro paragraph before slide. Gross margin reached 75.0% in Q3 while Data Center revenue grew 94% YoY. Closing remarks.";
    const quoteTarget = "Gross margin reached 75.0% in Q3";
    const qStart = rawText.indexOf(quoteTarget);
    const qEnd = qStart + quoteTarget.length;

    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        source_id: "src_deck_pptx",
        title: "NVDA Q3 Deck.pptx",
        text: rawText,
        content_sha256: "a".repeat(64),
        retrieval_method: "user_pptx",
        permissions: { store: true, model_input: true, display: true, export: true },
        document_pages: {
          method: "pptx_slides_v1",
          pages: [
            [0, 20, "slide_ok"],
            [20, rawText.length, "slide_ok"],
          ],
        },
      }),
    );

    const mockView = {
      topicId: "topic_overlap",
      version: { version_id: "ver_1" },
      content: {
        claims: [
          {
            claim_id: "c_outer",
            // Enclosing broader citation [10..110] that previously swallowed inner V3a quote [30..62]!
            citations: [{ source_id: "src_deck_pptx", location: { character_start: 10, character_end: 110 } }],
          },
        ],
        facts: [],
      },
      sources: new Map(),
      sourceTitle: () => "NVDA Q3 Deck.pptx",
    } as unknown as TopicView;

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { container } = render(
      <QueryClientProvider client={qc}>
        <TopicContext.Provider value={mockView}>
          <ReaderPanel sourceId="src_deck_pptx" range={[qStart, qEnd]} onClose={vi.fn()} />
        </TopicContext.Provider>
      </QueryClientProvider>,
    );

    await waitFor(() => {
      const activeMark = document.querySelector("mark.on");
      expect(activeMark).toBeTruthy();
      expect(activeMark?.textContent).toBe(quoteTarget);
    });
    expect(screen.getByText("Slide 2")).toBeTruthy();
    expect(document.querySelectorAll("mark.cited").length).toBe(2);
  });
});
