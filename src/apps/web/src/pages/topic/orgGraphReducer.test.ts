import { describe, expect, it } from "vitest";
import {
  createInitialOrgGraphState,
  orgGraphReducer,
  parseOrgGraphEvent,
  reduceOrgGraphEvent,
  reduceOrgGraphEvents,
  type OrgGraphEventInput,
} from "./orgGraphReducer";

function makeEvent(overrides: Partial<OrgGraphEventInput> & { sequence: number }): OrgGraphEventInput {
  return {
    stage: "stage_2_workers",
    node_id: "worker_segments",
    role: "worker_segments",
    display_name: "Segments & KPIs",
    runtime_state: "running",
    action_kind: "READING",
    current_activity: "Reading R4.htm segment table.",
    blocker_text: null,
    edge_event: null,
    slot: "fast_extractor",
    parent_node_id: "lead_coordinator",
    timestamp: "2026-11-24T04:50:00Z",
    ...overrides,
  };
}

describe("orgGraphReducer", () => {
  it("seeds canonical 6-node focus tree skeleton in stable order with baseline delegation and review edges", () => {
    const initial = createInitialOrgGraphState();
    expect(initial.lastSequence).toBe(0);
    expect(initial.runStatus).toBe("idle");
    expect(initial.nodes.map((n) => n.nodeId)).toEqual([
      "lead_coordinator",
      "worker_segments",
      "worker_forensics",
      "worker_guidance",
      "bear_reviewer",
      "dossier_writer",
    ]);
    expect(initial.nodes.map((n) => n.displayName)).toEqual([
      "Lead Coordinator",
      "分部与行业 KPI",
      "财务质量与风险法证",
      "指引与电话会预期差",
      "独立空头审查",
      "底稿撰稿 (Writer)",
    ]);
    expect(initial.nodes.every((n) => n.runtimeState === "idle" && n.activityLog.length === 0)).toBe(true);

    const delegationEdges = initial.edges.filter((e) => !e.isReview);
    const reviewEdges = initial.edges.filter((e) => e.isReview);
    expect(delegationEdges).toHaveLength(3);
    expect(reviewEdges).toHaveLength(2);
    expect(reviewEdges.map((e) => `${e.fromNodeId}->${e.toNodeId}`)).toEqual([
      "lead_coordinator->bear_reviewer",
      "bear_reviewer->dossier_writer",
    ]);
  });

  it("enforces strict monotonic sequence deduplication and deep immutability", () => {
    const s0 = createInitialOrgGraphState();
    Object.freeze(s0);
    Object.freeze(s0.nodes);
    Object.freeze(s0.edges);

    const ev1 = makeEvent({
      sequence: 1,
      stage: "stage_1_plan",
      node_id: "lead_coordinator",
      role: "lead_coordinator",
      display_name: "Lead Coordinator",
      action_kind: "INSPECTING",
      runtime_state: "running",
      current_activity: "Inspecting Stage 0 snapshot.",
      slot: "deep_reasoner",
      parent_node_id: null,
    });
    const ev2 = makeEvent({
      sequence: 2,
      node_id: "worker_segments",
      action_kind: "READING",
      runtime_state: "running",
      current_activity: "Reading segment table R12.htm.",
    });

    const s1 = reduceOrgGraphEvent(s0, ev1);
    expect(s1).not.toBe(s0);
    expect(s1.lastSequence).toBe(1);
    expect(s0.lastSequence).toBe(0); // prior state unchanged

    const s2 = reduceOrgGraphEvent(s1, ev2);
    expect(s2.lastSequence).toBe(2);

    // Duplicate sequence (== 2) and stale sequence (< 2) are ignored and preserve reference identity.
    const dup = reduceOrgGraphEvent(s2, ev2);
    expect(dup).toBe(s2);

    const stale = reduceOrgGraphEvent(
      s2,
      makeEvent({
        sequence: 1,
        node_id: "worker_segments",
        runtime_state: "failed",
        action_kind: "FAILED",
        current_activity: "Stale failure should never regress seq 2.",
      }),
    );
    expect(stale).toBe(s2);
    expect(stale.nodes.find((n) => n.nodeId === "worker_segments")?.runtimeState).toBe("running");
    expect(stale.nodes.find((n) => n.nodeId === "worker_segments")?.activityLog).toHaveLength(1);
  });

  it("rejects malformed or non-positive sequence events safely", () => {
    expect(parseOrgGraphEvent(null)).toBeNull();
    expect(parseOrgGraphEvent({ sequence: 0 })).toBeNull();
    expect(parseOrgGraphEvent({ sequence: 1.5 })).toBeNull();
    expect(
      parseOrgGraphEvent({
        sequence: 1,
        stage: "stage_2",
        node_id: "worker_segments",
        role: "worker_segments",
        display_name: "Segments",
        runtime_state: "running",
        action_kind: "UNKNOWN_KIND",
        current_activity: "bad action",
      }),
    ).toBeNull();

    const s0 = createInitialOrgGraphState();
    const same = reduceOrgGraphEvent(s0, { sequence: 0 } as unknown as OrgGraphEventInput);
    expect(same).toBe(s0);
  });

  it("keeps stable node ordering regardless of parallel worker event arrival order and handles prefetch + custom nodes", () => {
    const events: OrgGraphEventInput[] = [
      makeEvent({
        sequence: 1,
        stage: "stage_2_workers",
        node_id: "worker_guidance",
        role: "worker_guidance",
        display_name: "Guidance & Transcript",
        action_kind: "SEARCHING",
        current_activity: "Fetching consensus.",
      }),
      makeEvent({
        sequence: 2,
        stage: "stage_0_prefetch",
        node_id: "prefetch_engine",
        role: "prefetch",
        display_name: "Stage 0 Prefetch",
        action_kind: "RESPONDING",
        runtime_state: "completed",
        current_activity: "Stage 0 complete.",
        slot: null,
        parent_node_id: null,
      }),
      makeEvent({
        sequence: 3,
        stage: "stage_2_workers",
        node_id: "worker_segments",
        role: "worker_segments",
        display_name: "Segments & KPIs",
        action_kind: "READING",
        current_activity: "Reading EX-99.1.",
      }),
      makeEvent({
        sequence: 4,
        stage: "stage_2_workers",
        node_id: "custom_helper",
        role: "helper",
        display_name: "自定义核查节点",
        action_kind: "INSPECTING",
        current_activity: "Checking footnote 9.",
      }),
    ];

    const state = reduceOrgGraphEvents(createInitialOrgGraphState(), events);
    expect(state.nodes.map((n) => n.nodeId)).toEqual([
      "prefetch_engine",
      "lead_coordinator",
      "worker_segments",
      "worker_forensics",
      "worker_guidance",
      "bear_reviewer",
      "dossier_writer",
      "custom_helper",
    ]);
    expect(state.nodes.find((n) => n.nodeId === "prefetch_engine")?.displayName).toBe("Stage 0 确定性预热");
    expect(state.nodes.find((n) => n.nodeId === "worker_guidance")?.displayName).toBe("指引与电话会预期差");
    expect(state.nodes.find((n) => n.nodeId === "custom_helper")?.displayName).toBe("自定义核查节点");
  });

  it("distinguishes is_review: true review edges from delegation edges even between the same nodes", () => {
    const events: OrgGraphEventInput[] = [
      makeEvent({
        sequence: 1,
        stage: "stage_1_plan",
        node_id: "lead_coordinator",
        role: "lead_coordinator",
        display_name: "Lead Coordinator",
        action_kind: "DELEGATING",
        runtime_state: "running",
        current_activity: "Dispatched worker_forensics.",
        edge_event: {
          from_node_id: "lead_coordinator",
          to_node_id: "worker_forensics",
          is_review: false,
          edge_kind: "dispatch",
          label: "Investigate AR/DSO yellow flag",
        },
        slot: "deep_reasoner",
        parent_node_id: null,
      }),
      makeEvent({
        sequence: 2,
        stage: "stage_2_workers",
        node_id: "worker_forensics",
        role: "worker_forensics",
        display_name: "Quality Forensics",
        action_kind: "RESPONDING",
        runtime_state: "completed",
        current_activity: "Emitted 3 cards.",
        edge_event: {
          from_node_id: "worker_forensics",
          to_node_id: "lead_coordinator",
          is_review: true,
          edge_kind: "report",
          label: "3 cards",
        },
      }),
      makeEvent({
        sequence: 3,
        stage: "stage_2_5_review",
        node_id: "lead_coordinator",
        role: "lead_coordinator",
        display_name: "Lead Coordinator",
        action_kind: "DELEGATING",
        runtime_state: "running",
        current_activity: "Stage 2.5 targeted follow-up to worker_forensics.",
        edge_event: {
          from_node_id: "lead_coordinator",
          to_node_id: "worker_forensics",
          is_review: true,
          edge_kind: "review",
          label: "Stage 2.5 follow-up: Verify inventory reserve",
        },
        slot: "deep_reasoner",
        parent_node_id: null,
      }),
    ];

    const state = reduceOrgGraphEvents(createInitialOrgGraphState(), events);
    const dispatchEdge = state.edges.find((e) => e.edgeId === "lead_coordinator->worker_forensics:flow");
    const reviewEdge = state.edges.find((e) => e.edgeId === "lead_coordinator->worker_forensics:review");
    const reportEdge = state.edges.find((e) => e.edgeId === "worker_forensics->lead_coordinator:review");

    expect(dispatchEdge).toBeDefined();
    expect(dispatchEdge?.isReview).toBe(false);
    expect(dispatchEdge?.edgeKind).toBe("dispatch");
    expect(dispatchEdge?.triggered).toBe(true);

    expect(reviewEdge).toBeDefined();
    expect(reviewEdge?.isReview).toBe(true);
    expect(reviewEdge?.edgeKind).toBe("review");
    expect(reviewEdge?.label).toBe("Stage 2.5 follow-up: Verify inventory reserve");

    expect(reportEdge).toBeDefined();
    expect(reportEdge?.isReview).toBe(true);
    expect(reportEdge?.edgeKind).toBe("report");

    expect(state.nodes.find((n) => n.nodeId === "worker_forensics")?.currentTaskSummary).toBe(
      "Stage 2.5 follow-up: Verify inventory reserve",
    );
  });

  it("never infers node state from free-text assistant prose", () => {
    const s1 = reduceOrgGraphEvent(
      createInitialOrgGraphState(),
      makeEvent({
        sequence: 1,
        node_id: "worker_forensics",
        role: "worker_forensics",
        display_name: "Quality Forensics",
        runtime_state: "running",
        action_kind: "READING",
        current_activity: "Analysis completed? No — failed blocked cancelled skipped error crashed!",
      }),
    );

    const node = s1.nodes.find((n) => n.nodeId === "worker_forensics");
    expect(node?.runtimeState).toBe("running");
    expect(node?.actionKind).toBe("READING");
    expect(s1.runStatus).toBe("running");
  });

  it("tracks concise per-node activityLog, blockerText lifecycle, and terminal completion settling", () => {
    const events: OrgGraphEventInput[] = [
      makeEvent({
        sequence: 1,
        stage: "stage_2_workers",
        node_id: "lead_coordinator",
        role: "lead_coordinator",
        display_name: "Lead Coordinator",
        runtime_state: "waiting",
        action_kind: "WAITING_SUBORDINATES",
        current_activity: "Waiting for 3 parallel workers.",
        slot: "deep_reasoner",
        parent_node_id: null,
      }),
      makeEvent({
        sequence: 2,
        stage: "stage_2_workers",
        node_id: "worker_guidance",
        role: "worker_guidance",
        display_name: "Guidance & Transcript",
        runtime_state: "blocked",
        action_kind: "NEEDS_USER",
        current_activity: "worker_guidance requires FMP BYOK credential.",
        blocker_text: "Missing FMP BYOK key; falling back to 8-K Ex 99.1.",
      }),
      makeEvent({
        sequence: 3,
        stage: "steering",
        node_id: "worker_guidance",
        role: "worker_guidance",
        display_name: "Guidance & Transcript",
        runtime_state: "running",
        action_kind: "READING",
        current_activity: "Reading 8-K Exhibit 99.1 fallback.",
        blocker_text: null,
      }),
      makeEvent({
        sequence: 4,
        stage: "stage_3b_gate_memo",
        node_id: "dossier_writer",
        role: "writer",
        display_name: "Dossier Writer",
        runtime_state: "completed",
        action_kind: "RESPONDING",
        current_activity: "Locked EarningsDossier and rendered Memo.",
        slot: "writer",
      }),
    ];

    const state = reduceOrgGraphEvents(createInitialOrgGraphState(), events);
    const guidance = state.nodes.find((n) => n.nodeId === "worker_guidance")!;
    expect(guidance.activityLog).toHaveLength(2);
    expect(guidance.activityLog.map((e) => e.sequence)).toEqual([2, 3]);
    expect(guidance.blockerText).toBeNull(); // cleared upon active recovery

    expect(state.runStatus).toBe("completed");
    // Lead coordinator transitions cleanly from waiting -> completed without fabricating prose logs.
    const lead = state.nodes.find((n) => n.nodeId === "lead_coordinator")!;
    expect(lead.runtimeState).toBe("completed");
    expect(lead.activityLog).toHaveLength(1);

    const reset = orgGraphReducer(state, { type: "reset" });
    expect(reset.lastSequence).toBe(0);
    expect(reset.runStatus).toBe("idle");
  });
});
