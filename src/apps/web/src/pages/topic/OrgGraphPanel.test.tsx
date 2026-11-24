// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ACTION_KIND_META, OrgGraphPanel } from "./OrgGraphPanel";
import type { OrgActionKind, OrgGraphEventInput } from "./orgGraphReducer";

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
    },
    slot: "deep_reasoner",
    timestamp: "2026-11-24T04:40:05Z",
  },
  {
    sequence: 2,
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
    sequence: 3,
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
    sequence: 4,
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
    sequence: 5,
    stage: "stage_2_workers",
    node_id: "worker_guidance",
    role: "worker_guidance",
    display_name: "Guidance & Transcript",
    runtime_state: "running",
    action_kind: "SEARCHING",
    current_activity: "Comparing Q4 guidance midpoint against actual revenue.",
    slot: "fast_extractor",
    parent_node_id: "lead_coordinator",
    timestamp: "2026-11-24T04:40:09Z",
  },
];

describe("OrgGraphPanel (Ragent_product Morandi orb visual system)", () => {
  it("renders circular gradient orbs, 45° frosted action badges, distinct review edges, and zero inline styles", () => {
    mockViewport({ tablet: true });
    const { container } = render(<OrgGraphPanel events={ACTIVE_SQUAD_EVENTS} />);

    const svg = screen.getByTestId("org-graph-svg");
    expect(svg.getAttribute("viewBox")).toBe("0 0 420 456");

    // All 6 canonical orbs rendered as circular nodes (not rectangular cards)
    for (const id of [
      "lead_coordinator",
      "worker_segments",
      "worker_forensics",
      "worker_guidance",
      "bear_reviewer",
      "dossier_writer",
    ]) {
      const node = container.querySelector(`[data-node-id="${id}"]`);
      expect(node).not.toBeNull();
      expect(node?.querySelector("circle")).not.toBeNull();
      expect(node?.querySelector("rect")).toBeNull();
    }

    // 45° frosted action badges present for active/blocked nodes
    expect(screen.getByTestId("action-badge-worker_segments")).toBeTruthy();
    expect(screen.getByTestId("action-badge-worker_forensics")).toBeTruthy();

    // Review edges distinguished by data-review="true" and .is-review class
    const reviewEdges = container.querySelectorAll('[data-review="true"]');
    expect(reviewEdges.length).toBeGreaterThanOrEqual(3);

    // Zero inline style attributes or <style> tags (strict CSP style-src 'self')
    expect(container.querySelectorAll("[style]").length).toBe(0);
    expect(container.querySelectorAll("style").length).toBe(0);
  });

  it("exposes semantic Chinese labels and icons for all 9 NodeActionKind values", () => {
    const kinds: OrgActionKind[] = [
      "READING",
      "SEARCHING",
      "INSPECTING",
      "DELEGATING",
      "WAITING_SUBORDINATES",
      "RESPONDING",
      "NEEDS_USER",
      "BLOCKED",
      "FAILED",
    ];
    for (const kind of kinds) {
      expect(ACTION_KIND_META[kind].label.length).toBeGreaterThan(0);
      expect(typeof ACTION_KIND_META[kind].icon).toBe("object");
    }
  });

  it("respects prefers-reduced-motion by disabling particles and rotating arcs", () => {
    mockViewport({ tablet: true, reducedMotion: true });
    const { container } = render(<OrgGraphPanel events={ACTIVE_SQUAD_EVENTS} />);
    expect(container.querySelectorAll("animateMotion").length).toBe(0);
    expect(container.querySelectorAll(".org-ragent-badge-arc").length).toBe(0);
  });

  it("opens concise process log drawer on orb click/keyboard and calls onSelectNode", () => {
    mockViewport({ tablet: true, inspector: true });
    const onSelectNode = vi.fn();
    const { container } = render(
      <OrgGraphPanel events={ACTIVE_SQUAD_EVENTS} onSelectNode={onSelectNode} />,
    );

    const forensicsOrb = container.querySelector('[data-node-id="worker_forensics"]') as Element;
    fireEvent.click(forensicsOrb);

    const drawer = screen.getByTestId("org-node-drawer");
    expect(within(drawer).getAllByText(/Blocked unauthorized tool call news_search/).length).toBeGreaterThanOrEqual(1);
    expect(within(drawer).getByRole("alert").textContent).toContain("outside worker_forensics");
    expect(onSelectNode).toHaveBeenCalledWith(expect.objectContaining({ nodeId: "worker_forensics" }));
  });

  it("auto-collapses into compact colored-dot summary bar on completion and re-expands on click", () => {
    mockViewport({ tablet: true });
    const completedEvents: OrgGraphEventInput[] = [
      ...ACTIVE_SQUAD_EVENTS,
      {
        sequence: 6,
        stage: "stage_3b_write",
        node_id: "dossier_writer",
        role: "dossier_writer",
        display_name: "Writer",
        runtime_state: "completed",
        action_kind: "RESPONDING",
        current_activity: "Memo rendered.",
        timestamp: "2026-11-24T04:41:00Z",
      },
    ];
    render(<OrgGraphPanel events={completedEvents} runStatus="completed" />);

    expect(screen.getByTestId("org-graph-summary-bar")).toBeTruthy();
    expect(screen.queryByTestId("org-graph-svg")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /展开组织图/ }));
    expect(screen.getByTestId("org-graph-svg")).toBeTruthy();
  });

  it("renders cleanly in mobile layout mode (<600px)", () => {
    mockViewport({ tablet: false });
    render(<OrgGraphPanel events={ACTIVE_SQUAD_EVENTS} />);
    expect(screen.getByTestId("org-graph-panel").getAttribute("data-layout-mode")).toBe("mobile");
    expect(screen.getByTestId("org-graph-svg").getAttribute("viewBox")).toBe("0 0 420 456");
  });
});
