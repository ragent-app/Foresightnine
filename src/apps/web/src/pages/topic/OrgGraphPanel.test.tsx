// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { OrgGraphPanel } from "./OrgGraphPanel";
import type { OrgGraphEventInput } from "./orgGraphReducer";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function mockViewport({
  tablet = true,
  inspector = false,
  reducedMotion = false,
}: {
  tablet?: boolean;
  inspector?: boolean;
  reducedMotion?: boolean;
}) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      matches:
        query === "(min-width: 600px)"
          ? tablet
          : query === "(min-width: 1280px)"
            ? inspector
            : query === "(prefers-reduced-motion: reduce)"
              ? reducedMotion
              : false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  );
}

const ACTIVE_SQUAD_EVENTS: OrgGraphEventInput[] = [
  {
    sequence: 1,
    stage: "stage_0_prefetch",
    node_id: "prefetch_engine",
    role: "prefetch",
    display_name: "Stage 0 Prefetch",
    runtime_state: "completed",
    action_kind: "RESPONDING",
    current_activity: "Stage 0 complete: 2 flags, 0 model calls.",
    timestamp: "2026-11-24T04:40:00Z",
  },
  {
    sequence: 2,
    stage: "stage_1_plan",
    node_id: "lead_coordinator",
    role: "lead_coordinator",
    display_name: "Lead Coordinator",
    runtime_state: "running",
    action_kind: "DELEGATING",
    current_activity: "Dispatched worker_segments for Cloud KPI extraction.",
    edge_event: {
      from_node_id: "lead_coordinator",
      to_node_id: "worker_segments",
      is_review: false,
      edge_kind: "dispatch",
      label: "提取云业务与分部收入 KPI",
    },
    slot: "deep_reasoner",
    timestamp: "2026-11-24T04:40:05Z",
  },
  {
    sequence: 3,
    stage: "stage_2_workers",
    node_id: "lead_coordinator",
    role: "lead_coordinator",
    display_name: "Lead Coordinator",
    runtime_state: "waiting",
    action_kind: "WAITING_SUBORDINATES",
    current_activity: "Waiting for 3 parallel read-only worker(s).",
    slot: "deep_reasoner",
    timestamp: "2026-11-24T04:40:06Z",
  },
  {
    sequence: 4,
    stage: "stage_2_workers",
    node_id: "worker_segments",
    role: "worker_segments",
    display_name: "Segments & KPIs",
    runtime_state: "running",
    action_kind: "READING",
    current_activity: "Reading R14.htm Segment Information table.",
    slot: "fast_extractor",
    parent_node_id: "lead_coordinator",
    timestamp: "2026-11-24T04:40:07Z",
  },
  {
    sequence: 5,
    stage: "stage_2_workers",
    node_id: "worker_forensics",
    role: "worker_forensics",
    display_name: "Quality Forensics",
    runtime_state: "blocked",
    action_kind: "BLOCKED",
    current_activity: "Blocked unauthorized tool call news_search.",
    blocker_text: "Tool news_search is outside worker_forensics allowed subset.",
    slot: "fast_extractor",
    parent_node_id: "lead_coordinator",
    timestamp: "2026-11-24T04:40:08Z",
  },
  {
    sequence: 6,
    stage: "stage_2_workers",
    node_id: "worker_guidance",
    role: "worker_guidance",
    display_name: "Guidance & Transcript",
    runtime_state: "running",
    action_kind: "SEARCHING",
    current_activity: "Querying earnings_consensus and 8-K EX-99.1.",
    slot: "fast_extractor",
    parent_node_id: "lead_coordinator",
    timestamp: "2026-11-24T04:40:09Z",
  },
];

describe("OrgGraphPanel", () => {
  it("renders desktop Focus Tree with all 6 canonical roles, distinct review edges, and zero inline styles", () => {
    mockViewport({ tablet: true, inspector: true, reducedMotion: false });
    const { container } = render(<OrgGraphPanel events={ACTIVE_SQUAD_EVENTS} />);

    // Zero inline style attributes or <style> tags (CSP style-src 'self' compliance)
    expect(container.querySelectorAll("[style]").length).toBe(0);
    expect(container.querySelectorAll("style").length).toBe(0);

    const svg = screen.getByRole("group", { name: /有向组织图/ });
    expect(svg.getAttribute("data-layout-mode")).toBe("desktop");

    // All 6 core nodes present with Chinese/readable labels
    for (const label of [
      "Lead Coordinator",
      "分部与行业 KPI",
      "财务质量与风险法证",
      "指引与电话会预期差",
      "独立空头审查",
      "底稿撰稿 (Writer)",
    ]) {
      expect(screen.getByText(label)).toBeTruthy();
    }

    // Distinguishes is_review edges from delegation edges visually
    const reviewEdges = container.querySelectorAll('[data-edge-review="true"]');
    const delegationEdges = container.querySelectorAll('[data-edge-review="false"]');
    expect(reviewEdges.length).toBeGreaterThanOrEqual(2);
    expect(delegationEdges.length).toBe(3);

    for (const edge of reviewEdges) {
      const path = edge.querySelector("path");
      expect(path?.getAttribute("stroke-dasharray")).toBe("6 4");
    }
    for (const edge of delegationEdges) {
      const path = edge.querySelector("path");
      expect(path?.getAttribute("stroke-dasharray")).toBeNull();
    }
  });

  it("pairs semantic color, icon, and readable Chinese labels for all 9 NodeActionKind values", () => {
    mockViewport({ tablet: true, reducedMotion: true });
    const allActionEvents: OrgGraphEventInput[] = [
      {
        sequence: 1,
        stage: "stage_1_plan",
        node_id: "lead_coordinator",
        role: "lead_coordinator",
        display_name: "Lead Coordinator",
        runtime_state: "running",
        action_kind: "DELEGATING",
        current_activity: "Delegating.",
      },
      {
        sequence: 2,
        stage: "stage_2_workers",
        node_id: "worker_segments",
        role: "worker_segments",
        display_name: "Segments & KPIs",
        runtime_state: "running",
        action_kind: "READING",
        current_activity: "Reading 10-Q.",
      },
      {
        sequence: 3,
        stage: "stage_2_workers",
        node_id: "worker_forensics",
        role: "worker_forensics",
        display_name: "Quality Forensics",
        runtime_state: "running",
        action_kind: "SEARCHING",
        current_activity: "Searching EFTS.",
      },
      {
        sequence: 4,
        stage: "stage_2_workers",
        node_id: "worker_guidance",
        role: "worker_guidance",
        display_name: "Guidance & Transcript",
        runtime_state: "blocked",
        action_kind: "NEEDS_USER",
        current_activity: "Needs FMP BYOK key.",
      },
      {
        sequence: 5,
        stage: "stage_3a_bear",
        node_id: "bear_reviewer",
        role: "bear_reviewer",
        display_name: "Bear Reviewer",
        runtime_state: "running",
        action_kind: "INSPECTING",
        current_activity: "Inspecting flags.",
      },
      {
        sequence: 6,
        stage: "stage_3b_gate_memo",
        node_id: "dossier_writer",
        role: "writer",
        display_name: "Dossier Writer",
        runtime_state: "failed",
        action_kind: "FAILED",
        current_activity: "Writer failed.",
      },
    ];

    const { rerender } = render(<OrgGraphPanel events={allActionEvents} />);
    expect(screen.getByText("派发任务")).toBeTruthy();
    expect(screen.getByText("阅读资料")).toBeTruthy();
    expect(screen.getByText("检索线索")).toBeTruthy();
    expect(screen.getByText("需补充信息")).toBeTruthy();
    expect(screen.getByText("核验分析")).toBeTruthy();
    expect(screen.getByText("执行失败")).toBeTruthy();

    rerender(
      <OrgGraphPanel
        events={[
          ...allActionEvents,
          {
            sequence: 7,
            stage: "stage_2_workers",
            node_id: "lead_coordinator",
            role: "lead_coordinator",
            display_name: "Lead Coordinator",
            runtime_state: "waiting",
            action_kind: "WAITING_SUBORDINATES",
            current_activity: "Waiting.",
          },
          {
            sequence: 8,
            stage: "stage_2_workers",
            node_id: "worker_forensics",
            role: "worker_forensics",
            display_name: "Quality Forensics",
            runtime_state: "blocked",
            action_kind: "BLOCKED",
            current_activity: "Blocked.",
          },
          {
            sequence: 9,
            stage: "stage_3b_gate_memo",
            node_id: "dossier_writer",
            role: "writer",
            display_name: "Dossier Writer",
            runtime_state: "completed",
            action_kind: "RESPONDING",
            current_activity: "Done.",
          },
        ]}
      />,
    );
    // Run transitioned to completed -> auto-collapsed; re-expand to inspect the node action labels in the graph.
    fireEvent.click(screen.getByRole("button", { name: "展开组织图" }));
    expect(screen.getByText("等待并行节点")).toBeTruthy();
    expect(screen.getByText("已阻断")).toBeTruthy();
    expect(screen.getByText("生成交付")).toBeTruthy();
  });

  it("respects prefers-reduced-motion and renders zero infinite shimmer on waiting states", () => {
    // 1) Motion allowed: active worker edge gets restrained particle, waiting Lead node does NOT pulse.
    mockViewport({ tablet: true, reducedMotion: false });
    const { container, rerender } = render(<OrgGraphPanel events={ACTIVE_SQUAD_EVENTS} />);

    expect(container.querySelectorAll("animateMotion").length).toBeGreaterThan(0);
    const leadNode = container.querySelector('[data-node-id="lead_coordinator"]')!;
    expect(leadNode.getAttribute("data-runtime-state")).toBe("waiting");
    expect(leadNode.querySelectorAll("animate, animateMotion, .dot.running").length).toBe(0);

    // 2) Reduced motion enabled via media query: zero <animate>, zero <animateMotion>, zero .dot.running.
    mockViewport({ tablet: true, reducedMotion: true });
    rerender(<OrgGraphPanel events={ACTIVE_SQUAD_EVENTS} />);
    expect(container.querySelectorAll("animate, animateMotion").length).toBe(0);
    expect(container.querySelectorAll(".dot.running").length).toBe(0);
  });

  it("opens concise node process log drawer on click/keyboard without raw evidence cards and escapes untrusted HTML", () => {
    mockViewport({ tablet: true, inspector: true, reducedMotion: false });
    const eventsWithHtml: OrgGraphEventInput[] = [
      ...ACTIVE_SQUAD_EVENTS,
      {
        sequence: 7,
        stage: "stage_2_workers",
        node_id: "worker_forensics",
        role: "worker_forensics",
        display_name: "Quality Forensics",
        runtime_state: "failed",
        action_kind: "FAILED",
        current_activity: "<script>alert(1)</script>检查 Redline 失败",
        blocker_text: "<img src=x onerror=alert(2)>无法解析附注",
        slot: "fast_extractor",
      },
    ];
    render(<OrgGraphPanel events={eventsWithHtml} />);

    // Open worker_segments via click -> side inspector on >=1280px
    const segNode = screen.getByRole("button", { name: /分部与行业 KPI/ });
    fireEvent.click(segNode);
    const segDrawer = screen.getByRole("complementary", { name: "分部与行业 KPI" });
    expect(within(segDrawer).getByText("提取云业务与分部收入 KPI")).toBeTruthy();
    expect(within(segDrawer).getAllByText("Reading R14.htm Segment Information table.").length).toBeGreaterThanOrEqual(1);
    expect(within(segDrawer).getByText("简洁过程日志 · 1 条")).toBeTruthy();

    // Switch to worker_forensics via keyboard Enter -> displays blockerText & plain-text escaped payload
    const forenNode = screen.getByRole("button", { name: /财务质量与风险法证/ });
    fireEvent.keyDown(forenNode, { key: "Enter" });
    const forenDrawer = screen.getByRole("complementary", { name: "财务质量与风险法证" });
    expect(within(forenDrawer).getAllByText("<script>alert(1)</script>检查 Redline 失败").length).toBeGreaterThanOrEqual(1);
    expect(within(forenDrawer).getAllByText("<img src=x onerror=alert(2)>无法解析附注").length).toBeGreaterThanOrEqual(1);
    expect(within(forenDrawer).getByText("简洁过程日志 · 2 条")).toBeTruthy();
    expect(forenDrawer.querySelector("script, img")).toBeNull();

    // Close drawer
    fireEvent.click(within(forenDrawer).getByRole("button", { name: "关闭" }));
    expect(screen.queryByRole("complementary", { name: "财务质量与风险法证" })).toBeNull();
  });

  it("auto-collapses into expandable 投研小组执行摘要条 when the squad run completes", () => {
    mockViewport({ tablet: true, inspector: false, reducedMotion: true });
    const { rerender } = render(<OrgGraphPanel events={ACTIVE_SQUAD_EVENTS} />);

    // Expanded while active
    expect(screen.queryByTestId("org-summary-bar")).toBeNull();
    expect(screen.getByRole("group", { name: /有向组织图/ })).toBeTruthy();

    // Squad finishes Stage 3B -> auto-collapses
    const completedEvents: OrgGraphEventInput[] = [
      ...ACTIVE_SQUAD_EVENTS,
      {
        sequence: 7,
        stage: "stage_3a_bear",
        node_id: "bear_reviewer",
        role: "bear_reviewer",
        display_name: "Bear Reviewer",
        runtime_state: "completed",
        action_kind: "RESPONDING",
        current_activity: "Produced 2 anchored bear cases.",
        edge_event: {
          from_node_id: "bear_reviewer",
          to_node_id: "dossier_writer",
          is_review: true,
          edge_kind: "review",
          label: "2 anchored challenges",
        },
        slot: "bear_reviewer",
      },
      {
        sequence: 8,
        stage: "stage_3b_gate_memo",
        node_id: "dossier_writer",
        role: "writer",
        display_name: "Dossier Writer",
        runtime_state: "completed",
        action_kind: "RESPONDING",
        current_activity: "Locked EarningsDossier and rendered Memo.",
        slot: "writer",
      },
    ];

    act(() => {
      rerender(<OrgGraphPanel events={completedEvents} />);
    });

    const summaryBar = screen.getByTestId("org-summary-bar");
    expect(within(summaryBar).getByText("投研小组执行摘要条")).toBeTruthy();
    expect(screen.queryByRole("group", { name: /有向组织图/ })).toBeNull();

    // Clicking role chip directly in summary bar opens its concise process drawer
    fireEvent.click(within(summaryBar).getByRole("button", { name: /底稿撰稿 \(Writer\).*查看过程日志/ }));
    const writerDialog = screen.getByRole("dialog", { name: "底稿撰稿 (Writer)" });
    expect(within(writerDialog).getAllByText("Locked EarningsDossier and rendered Memo.").length).toBeGreaterThanOrEqual(1);
    fireEvent.click(within(writerDialog).getByRole("button", { name: "关闭" }));

    // Re-expand full Focus Tree anytime
    fireEvent.click(within(summaryBar).getByRole("button", { name: "展开组织图" }));
    expect(screen.getByRole("group", { name: /有向组织图/ })).toBeTruthy();
  });

  it("renders vertical mobile layout on <600px viewports and opens bottom sheet drawer", () => {
    mockViewport({ tablet: false, inspector: false, reducedMotion: true });
    render(<OrgGraphPanel events={ACTIVE_SQUAD_EVENTS} />);

    const svg = screen.getByRole("group", { name: /有向组织图/ });
    expect(svg.getAttribute("data-layout-mode")).toBe("mobile");
    expect(svg.getAttribute("viewBox")).toBe("0 0 360 542");

    fireEvent.click(screen.getByRole("button", { name: /分部与行业 KPI/ }));
    const sheet = screen.getByRole("dialog", { name: "分部与行业 KPI" });
    expect(sheet.getAttribute("data-panel")).toBe("sheet");
    expect(within(sheet).getByText("提取云业务与分部收入 KPI")).toBeTruthy();
  });
});
