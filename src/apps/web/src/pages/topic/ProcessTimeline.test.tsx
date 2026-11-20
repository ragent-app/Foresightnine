import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { CurrentActivity, ProgressEvent } from "../../api/types";
import { ProcessTimeline } from "./ProcessTimeline";

function mockTabletMatch(matches: boolean) {
  vi.stubGlobal("matchMedia", vi.fn((query: string) => ({
    matches: query === "(min-width: 600px)" ? matches : false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })));
}

const RICH_EVENTS: ProgressEvent[] = [
  {
    index: 0,
    kind: "model",
    finish_reason: "tool_calls",
    usage: 1200,
    input_tokens: 900,
    output_tokens: 300,
    settlement: null,
    note: "先搜索公开披露并获取 SEC 数据。",
  },
  {
    index: 1,
    kind: "tool",
    name: "discover_sources",
    status: "completed",
    reason: null,
    rationale: "检索财报线索",
    denied_count: 1,
    summary: {
      kind: "discover_sources",
      found_count: 2,
      eligible_count: 1,
      denied_count: 1,
      leads_preview: [
        { title: "Northwind 10-K", host: "www.sec.gov", published_at: "2026-08-01T00:00:00Z" },
        { title: "Market Blog", host: "blog.example.com", published_at: null },
      ],
    },
  },
  {
    index: 2,
    kind: "tool",
    name: "acquire_sources",
    status: "completed",
    reason: null,
    rationale: "抓取官方披露",
    denied_count: null,
    summary: {
      kind: "acquire_sources",
      acquired_count: 1,
      failed_count: 1,
      items: [
        { title: "Northwind 10-K", host: "www.sec.gov", status: "acquired", reason: null, total_characters: 18500 },
        { title: "Blocked IR", host: "ir.example.com", status: "failed", reason: "http_forbidden_or_challenge", total_characters: null },
      ],
    },
  },
  {
    index: 3,
    kind: "tool",
    name: "sec_company_concept",
    status: "acquired",
    reason: null,
    rationale: "查询 XBRL 收入",
    denied_count: null,
    summary: {
      kind: "sec_company_concept",
      entity: "NWEX",
      taxonomy: "us-gaap",
      tag: "Revenues",
      unit: "USD",
      status: "acquired",
      title: "SEC Revenues",
      total_characters: 4200,
    },
  },
  {
    index: 4,
    kind: "tool",
    name: "historical_prices",
    status: "acquired",
    reason: null,
    rationale: "获取历史收盘价",
    denied_count: null,
    summary: {
      kind: "historical_prices",
      instrument_id: "NWEX",
      start_date: "2025-01-01",
      end_date: "2026-06-30",
      status: "acquired",
      title: "NWEX 日线",
    },
  },
  {
    index: 5,
    kind: "tool",
    name: "read_sources",
    status: "read",
    reason: null,
    rationale: "阅读收入段落",
    denied_count: null,
    summary: {
      kind: "read_sources",
      read_count: 1,
      items: [{ source_id: "s1", title: "Northwind 10-K", start: 100, end: 900, total_characters: 18500 }],
    },
  },
  {
    index: 6,
    kind: "tool",
    name: "record_facts",
    status: "completed",
    reason: null,
    rationale: "记录收入数值",
    denied_count: null,
    summary: {
      kind: "record_facts",
      recorded_count: 1,
      facts_preview: [{ label: "总收入", value: "48612", unit: "millions", currency: "USD", period: "FY2026" }],
    },
  },
  {
    index: 7,
    kind: "tool",
    name: "calculate_financials",
    status: "completed",
    reason: null,
    rationale: "计算收入增长率",
    denied_count: null,
    summary: {
      kind: "calculate_financials",
      calculated_count: 1,
      calcs_preview: [{ formula_id: "growth_percent", value: "17.79", unit: "percent", currency: null, expression: "(48612 / 41270 - 1) * 100" }],
    },
  },
];

describe("ProcessTimeline", () => {
  it("renders model notes, all 7 structured tool summaries, staged tags when live, and active current step", () => {
    mockTabletMatch(true);
    const current: CurrentActivity = {
      kind: "tool",
      name: "submit_research",
      rationale: "汇总已核验结论",
      target: "正在核验引文位置与研究结论",
      started_at: "2026-11-19T12:00:00Z",
    };
    render(<ProcessTimeline events={RICH_EVENTS} current={current} live />);

    expect(screen.getByText("思路 · 过程说明（非正式结论）")).toBeTruthy();
    expect(screen.getByText("先搜索公开披露并获取 SEC 数据。")).toBeTruthy();

    // discover_sources + expandable leads preview
    expect(screen.getByText("发现 2 条线索（可用 1 · 受限 1）")).toBeTruthy();
    expect(screen.queryByText("Market Blog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "展开线索 (2)" }));
    expect(screen.getByText("Market Blog")).toBeTruthy();
    expect(screen.getByText("www.sec.gov · 2026-08-01")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "收起线索" }));
    expect(screen.queryByText("Market Blog")).toBeNull();

    // acquire_sources
    expect(screen.getByText("已获取 1 份 · 未获取 1 份")).toBeTruthy();
    expect(screen.getByText("Blocked IR")).toBeTruthy();
    expect(screen.getByText(/目标网站拒绝访问/)).toBeTruthy();

    // sec_company_concept & historical_prices
    expect(screen.getByText("NWEX · us-gaap:Revenues (USD)")).toBeTruthy();
    expect(screen.getByText("NWEX (2025-01-01 ~ 2026-06-30)")).toBeTruthy();

    // read_sources
    expect(screen.getByText("第 100–900 字符 / 共 18,500 字符")).toBeTruthy();

    // record_facts & calculate_financials with live staged tags
    expect(screen.getByText("48,612 百万 · USD")).toBeTruthy();
    expect(screen.getByText("17.79%")).toBeTruthy();
    expect(screen.getAllByText("运行中暂存")).toHaveLength(2);

    // active current row
    expect(screen.getByText("提交结论")).toBeTruthy();
    expect(screen.getByText("汇总已核验结论")).toBeTruthy();
    expect(screen.getByText("正在核验引文位置与研究结论")).toBeTruthy();
  });

  it("hides 运行中暂存 when not live and formats model retry phases", () => {
    mockTabletMatch(true);
    const { container } = render(
      <ProcessTimeline
        events={RICH_EVENTS.slice(6)}
        current={{ kind: "model", phase: "retrying", retry_attempt: 2, started_at: null }}
        live={false}
      />,
    );
    expect(screen.queryByText("运行中暂存")).toBeNull();
    expect(screen.getByText("重试模型请求（第 2 次）")).toBeTruthy();
    expect(container.querySelector(".dot.running")).toBeTruthy();
  });

  it("collapses earlier steps on <600px viewports when >4 steps exist and expands on click", () => {
    mockTabletMatch(false);
    render(
      <ProcessTimeline
        events={RICH_EVENTS}
        current={{ kind: "model", phase: "thinking", retry_attempt: null, started_at: null }}
        live
      />,
    );
    // 8 completed rows + 1 current = 9 items -> keeps 3 completed + 1 current = 4 visible, collapses first 5
    const expandButton = screen.getByRole("button", { name: "展开前 5 步" });
    expect(screen.queryByText("先搜索公开披露并获取 SEC 数据。")).toBeNull();
    expect(screen.getByText("计算收入增长率")).toBeTruthy();
    expect(screen.getByText("模型思考中")).toBeTruthy();

    fireEvent.click(expandButton);
    expect(screen.getByText("先搜索公开披露并获取 SEC 数据。")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /展开前/ })).toBeNull();
  });

  it("renders untrusted model/tool text strictly as plain text with zero HTML injection", () => {
    mockTabletMatch(true);
    const xssEvents: ProgressEvent[] = [
      {
        index: 0,
        kind: "model",
        finish_reason: "tool_calls",
        usage: 100,
        input_tokens: 60,
        output_tokens: 40,
        settlement: null,
        note: "<script>window.__xss=1</script><b>说明</b>",
      },
      {
        index: 1,
        kind: "tool",
        name: "discover_sources",
        status: "completed",
        reason: null,
        rationale: "<img src=x onerror=alert(1)>",
        denied_count: null,
        summary: {
          kind: "discover_sources",
          found_count: 1,
          eligible_count: 1,
          denied_count: 0,
          leads_preview: [{ title: "<em>Lead</em>", host: "evil.example", published_at: null }],
        },
      },
    ];
    const { container } = render(
      <ProcessTimeline
        events={xssEvents}
        current={{ kind: "tool", name: "read_sources", rationale: "<svg onload=alert(1)>", target: "<iframe>bad</iframe>" }}
        live
      />,
    );
    expect(screen.getByText("<script>window.__xss=1</script><b>说明</b>")).toBeTruthy();
    expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeTruthy();
    expect(screen.getByText("<svg onload=alert(1)>")).toBeTruthy();
    expect(screen.getByText("<iframe>bad</iframe>")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "展开线索 (1)" }));
    expect(screen.getByText("<em>Lead</em>")).toBeTruthy();
    expect(container.querySelector("script, img, b, em, iframe")).toBeNull();
  });

  it("renders create_chart tool summaries with staged indicator when live and calm placeholder while assembling", () => {
    mockTabletMatch(true);
    const chartEvents: ProgressEvent[] = [
      {
        index: 0,
        kind: "tool",
        name: "create_chart",
        status: "completed",
        reason: null,
        rationale: "可视化收入与利润率",
        denied_count: null,
        summary: {
          kind: "create_chart",
          chart_id: "chart-1",
          chart_type: "combo_bar_line",
          title: "Northwind 收入与利润率对比",
          series_count: 2,
          point_count: 4,
        },
      },
    ];

    const { rerender } = render(
      <ProcessTimeline
        events={chartEvents}
        current={{ kind: "tool", name: "create_chart", rationale: null, target: null }}
        live
      />,
    );

    expect(screen.getAllByText("生成图表")).toHaveLength(2);
    expect(screen.getByText("Northwind 收入与利润率对比")).toBeTruthy();
    expect(screen.getByText("柱线组合图 · 2 条序列 · 4 个数据点")).toBeTruthy();
    expect(screen.getByText("运行中暂存")).toBeTruthy();
    expect(screen.getByText("正在组装并校验图表数据与单位口径…")).toBeTruthy();

    rerender(<ProcessTimeline events={chartEvents} live={false} />);
    expect(screen.queryByText("运行中暂存")).toBeNull();

    const causalEvent: ProgressEvent[] = [
      {
        index: 1,
        kind: "tool",
        name: "create_chart",
        status: "completed",
        reason: null,
        rationale: "生成逻辑传导图",
        denied_count: null,
        summary: {
          kind: "create_chart",
          chart_id: "chart-cg",
          chart_type: "causal_graph",
          title: "Northwind 传导路径",
          series_count: 6,
          point_count: 5,
        },
      },
    ];
    rerender(<ProcessTimeline events={causalEvent} live={false} />);
    expect(screen.getByText("逻辑传导图 · 6 个节点 · 5 条传导关系")).toBeTruthy();
  });
});
