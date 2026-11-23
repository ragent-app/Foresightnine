import type { ResearchV2 } from "@foresightnine/contracts";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TopicContext, type TopicView } from "../context";
import { computeCausalGraphLayout } from "./causalGraphLayout";
import { CausalGraphView } from "./CausalGraphView";
import { ChartCard } from "./ChartCard";

const SAMPLE_GRAPH: ResearchV2.CausalGraphSpec = {
  nodes: [
    { id: "m1", label: "美联储降息周期", category: "macro", status: "confirmed", detail: "利率下行缓解估值压力" },
    { id: "c1", label: "Northwind 收入加速", category: "company", status: "confirmed", detail: "云业务 FY26 +17.79%" },
    { id: "c2", label: "数据中心营业费用上升", category: "company", status: "confirmed" },
    { id: "a1", label: "营业利润率承压", category: "asset", status: "confirmed" },
    { id: "s1", label: "若明年资本开支未见回报", category: "scenario", status: "assumption", detail: "失效条件：利用率超 85%" },
    { id: "s2", label: "旧产能出清假设", category: "scenario", status: "invalidated" },
  ],
  edges: [
    { from_id: "m1", to_id: "c1", relation: "宏观流动性支撑云支出", direction: "positive", claim_ids: ["claim-1"], unverified_assumption: false },
    { from_id: "c1", to_id: "c2", relation: "扩容带动营业开支同步抬升", direction: "conditional", claim_ids: ["claim-2", "claim-3"], unverified_assumption: false },
    { from_id: "c2", to_id: "a1", relation: "费用增加压制营业利润率", direction: "negative", claim_ids: ["claim-2"], unverified_assumption: false },
    { from_id: "s1", to_id: "a1", relation: "效率低迷或扩大回撤风险", direction: "uncertain", claim_ids: [], unverified_assumption: true },
    { from_id: "a1", to_id: "m1", relation: "反馈检验", direction: "uncertain", claim_ids: [], unverified_assumption: true },
  ],
};

const CAUSAL_CHART: ResearchV2.ResearchChart = {
  chart_id: "chart_causal_1",
  turn_id: "turn_2",
  chart_type: "causal_graph",
  title: "Northwind 盈利传导链与风险情景",
  subtitle: "逻辑传导图 · 含未核实假设",
  x_label: "",
  axes: [],
  series: [],
  causal_graph: SAMPLE_GRAPH,
  is_simulated: false,
  has_projected_points: true,
  replaces_chart_id: null,
  source_ids: ["src_annual"],
};

function makeTopicView(gotoClaim = vi.fn()): TopicView {
  return {
    topicId: "t1",
    topic: {
      topic_id: "t1",
      owner_id: "acc",
      created_at: "2026-08-01T00:00:00Z",
      updated_at: "2026-08-01T00:00:00Z",
      request: { question: "q" },
      versions: [],
    },
    version: {
      version_id: "v1",
      created_at: "2026-08-01T00:00:00Z",
      created_by: "agent",
      review: "draft",
      content: {
        claims: [
          { claim_id: "claim-1", role: "support", text: "总收入同比增长 17.79%。" },
          { claim_id: "claim-2", role: "counterargument", text: "营业费用率上升压制利润率。" },
          { claim_id: "claim-3", role: "counterargument", text: "数据中心资本开支持续高位。" },
        ],
      },
    },
    content: {
      claims: [
        { claim_id: "claim-1", role: "support", text: "总收入同比增长 17.79%。" },
        { claim_id: "claim-2", role: "counterargument", text: "营业费用率上升压制利润率。" },
        { claim_id: "claim-3", role: "counterargument", text: "数据中心资本开支持续高位。" },
      ],
    },
    turns: [],
    citations: { numberFor: () => 0, entries: [] },
    sources: new Map(),
    live: null,
    locked: false,
    turnNumber: () => 1,
    sourceTitle: (id) => (id === "src_annual" ? "Northwind 2026 年报" : id),
    openPanel: vi.fn(),
    deepDive: vi.fn(),
    gotoClaim,
    reextractPdf: vi.fn(async () => undefined),
    draft: {},
    setDraft: vi.fn(),
    dirty: false,
    saveDraft: vi.fn(async () => "v2"),
    saving: false,
    notify: vi.fn(),
    evidence: new Map(),
    setEvidence: vi.fn(),
  };
}

describe("CausalGraphView & causalGraphLayout", () => {
  it("produces deterministic layered coordinates ordered by macro -> industry -> company -> asset -> scenario", () => {
    const first = computeCausalGraphLayout(SAMPLE_GRAPH);
    const second = computeCausalGraphLayout(SAMPLE_GRAPH);
    expect(first).toEqual(second);

    const m1 = first.nodeById.get("m1")!;
    const c1 = first.nodeById.get("c1")!;
    const c2 = first.nodeById.get("c2")!;
    const a1 = first.nodeById.get("a1")!;
    const s1 = first.nodeById.get("s1")!;

    expect(m1.col).toBeLessThan(c1.col);
    expect(c1.col).toBeLessThan(c2.col); // intra-category topological depth separation
    expect(c2.col).toBeLessThan(a1.col);
    expect(a1.col).toBeLessThan(s1.col);
    expect(first.edges).toHaveLength(5);
    expect(first.edges.every((e) => e.path.startsWith("M "))).toBe(true);
  });

  it("distinguishes confirmed vs assumption vs invalidated nodes, verified vs unverified edges, and navigates to claims via click or keyboard", () => {
    const onSelectClaim = vi.fn();
    const { container } = render(
      <CausalGraphView
        graph={SAMPLE_GRAPH}
        title="传导测试"
        onSelectClaim={onSelectClaim}
        claimText={(id) => (id === "claim-1" ? "总收入同比增长 17.79%。" : undefined)}
      />,
    );

    expect(container.querySelectorAll(".cg-node.status-confirmed").length).toBe(4);
    expect(container.querySelectorAll(".cg-node.status-assumption").length).toBe(1);
    expect(container.querySelectorAll(".cg-node.status-invalidated").length).toBe(1);
    expect(container.querySelectorAll(".cg-edge.is-verified").length).toBe(3);
    expect(container.querySelectorAll(".cg-edge.is-unverified").length).toBe(2);
    expect(container.querySelectorAll("[style]").length).toBe(0);

    // Single-claim verified edge directly triggers onSelectClaim on click
    const singleEdge = screen.getByRole("button", { name: /宏观流动性支撑云支出/ });
    fireEvent.click(singleEdge);
    expect(onSelectClaim).toHaveBeenCalledWith("claim-1");

    // Multi-claim edge shows detail strip with jump buttons on keyboard Enter
    const multiEdge = screen.getByRole("button", { name: /扩容带动营业开支同步抬升/ });
    fireEvent.keyDown(multiEdge, { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: /定位结论 #2 \(claim-3\)/ }));
    expect(onSelectClaim).toHaveBeenLastCalledWith("claim-3");

    // Node selection displays node detail / invalidation condition and claim links
    const scenarioNode = screen.getByRole("button", { name: /情景 · 若明年资本开支效率不及预期|情景 · 若明年资本开支未见回报/ });
    fireEvent.keyDown(scenarioNode, { key: " " });
    expect(screen.getByText("失效条件：利用率超 85%")).toBeTruthy();
  });

  it("integrates into ChartCard with table toggle, gotoClaim navigation, zoom modal, SVG export, and CSV export", async () => {
    const gotoClaim = vi.fn();
    const view = makeTopicView(gotoClaim);
    const capturedBlobs: Blob[] = [];
    const createObjectUrl = vi.fn((blob: Blob) => {
      capturedBlobs.push(blob);
      return "blob:causal";
    });
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    vi.stubGlobal("URL", { ...URL, createObjectURL: createObjectUrl, revokeObjectURL: vi.fn() });

    render(
      <TopicContext.Provider value={view}>
        <ChartCard chart={CAUSAL_CHART} />
      </TopicContext.Provider>,
    );

    const figure = screen.getByRole("figure", { name: "Northwind 盈利传导链与风险情景" });
    expect(within(figure).getByText("含假设分支")).toBeTruthy();
    expect(figure.querySelector('[data-chart-engine="causal-svg"]')).toBeTruthy();

    // SVG export & CSV export
    fireEvent.click(within(figure).getByRole("button", { name: "导出 SVG" }));
    fireEvent.click(within(figure).getByRole("button", { name: "导出 CSV" }));
    expect(createObjectUrl).toHaveBeenCalledTimes(2);
    expect(capturedBlobs[0]!.type).toContain("image/svg+xml");

    const csvText = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result ?? ""));
      reader.onerror = () => reject(reader.error);
      reader.readAsText(capturedBlobs[1]!);
    });
    expect(csvText).toContain("起点,方向,终点,传导关系,状态,关联结论ID");
    expect(csvText).toContain("未核实假设");

    // Switch to table mode and click a verified claim jump button
    fireEvent.click(within(figure).getByRole("button", { name: "数据表" }));
    const table = within(figure).getByRole("table", { name: "Northwind 盈利传导链与风险情景 数据表" });
    expect(within(table).getAllByText("未核实假设").length).toBeGreaterThanOrEqual(1);
    fireEvent.click(within(table).getAllByRole("button", { name: /总收入同比增长/ })[0]!);
    expect(gotoClaim).toHaveBeenCalledWith("claim-1");

    // Switch back to chart and open zoom modal
    fireEvent.click(within(figure).getByRole("button", { name: "图表" }));
    fireEvent.click(within(figure).getByRole("button", { name: "放大查看" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog.querySelector('[data-chart-engine="causal-svg"].is-modal')).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "关闭放大视图" }));
    expect(screen.queryByRole("dialog")).toBeNull();

    anchorClick.mockRestore();
  });
});
