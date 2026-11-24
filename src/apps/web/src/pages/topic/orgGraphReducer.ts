import type { ResearchV2 } from "@foresightnine/contracts";

export type OrgActionKind = ResearchV2.OrgGraphEvent["action_kind"];
export type OrgRuntimeState = ResearchV2.OrgGraphEvent["runtime_state"];
export type OrgEdgeKind = NonNullable<ResearchV2.OrgEdgeEvent["edge_kind"]>;
export type OrgModelSlot = NonNullable<ResearchV2.OrgGraphEvent["slot"]>;

export type OrgGraphEventInput = ResearchV2.OrgGraphEvent & {
  readonly operation_id?: string | null;
};

export type OrgRunStatus = "idle" | "running" | "completed" | "failed" | "cancelled";

export interface OrgActivityLogEntry {
  readonly sequence: number;
  readonly stage: string;
  readonly actionKind: OrgActionKind;
  readonly runtimeState: OrgRuntimeState;
  readonly text: string;
  readonly timestamp: string | null;
}

export interface OrgGraphNode {
  readonly nodeId: string;
  readonly role: string;
  readonly displayName: string;
  readonly parentNodeId: string | null;
  readonly slot: OrgModelSlot | null;
  readonly order: number;
  /** Focus tree tier: 0=prefetch, 1=lead, 2=workers, 3=bear_reviewer, 4=writer. */
  readonly tier: 0 | 1 | 2 | 3 | 4;
  readonly stage: string | null;
  readonly runtimeState: OrgRuntimeState;
  readonly actionKind: OrgActionKind | null;
  readonly currentTaskSummary: string | null;
  readonly currentActivity: string | null;
  readonly blockerText: string | null;
  readonly lastSequence: number;
  readonly lastTimestamp: string | null;
  readonly activityLog: readonly OrgActivityLogEntry[];
}

export interface OrgGraphEdge {
  readonly edgeId: string;
  readonly fromNodeId: string;
  readonly toNodeId: string;
  readonly isReview: boolean;
  readonly edgeKind: OrgEdgeKind;
  readonly label: string | null;
  readonly active: boolean;
  readonly triggered: boolean;
  readonly lastSequence: number;
  readonly eventCount: number;
}

export interface OrgGraphState {
  readonly lastSequence: number;
  readonly runStatus: OrgRunStatus;
  readonly currentStage: string | null;
  readonly nodes: readonly OrgGraphNode[];
  readonly edges: readonly OrgGraphEdge[];
}

export type OrgGraphAction =
  | { readonly type: "event"; readonly event: OrgGraphEventInput }
  | { readonly type: "batch"; readonly events: readonly OrgGraphEventInput[] }
  | { readonly type: "run_terminal"; readonly status: "completed" | "failed" | "cancelled" }
  | { readonly type: "reset" };

export const ORG_ACTION_KINDS: ReadonlySet<OrgActionKind> = new Set<OrgActionKind>([
  "READING",
  "SEARCHING",
  "INSPECTING",
  "DELEGATING",
  "WAITING_SUBORDINATES",
  "RESPONDING",
  "NEEDS_USER",
  "BLOCKED",
  "FAILED",
]);

export const ORG_RUNTIME_STATES: ReadonlySet<OrgRuntimeState> = new Set<OrgRuntimeState>([
  "idle",
  "running",
  "waiting",
  "completed",
  "failed",
  "blocked",
  "cancelled",
  "skipped",
]);

const ORG_EDGE_KINDS: ReadonlySet<OrgEdgeKind> = new Set<OrgEdgeKind>([
  "dispatch",
  "report",
  "review",
  "steering",
]);

const MAX_NODE_LOG_ENTRIES = 50;

interface CanonicalNodeSpec {
  readonly nodeId: string;
  readonly role: string;
  readonly displayName: string;
  readonly backendDefaults: readonly string[];
  readonly parentNodeId: string | null;
  readonly slot: OrgModelSlot | null;
  readonly order: number;
  readonly tier: 0 | 1 | 2 | 3 | 4;
  readonly defaultTaskSummary: string;
}

const PREFETCH_NODE_SPEC: CanonicalNodeSpec = {
  nodeId: "prefetch_engine",
  role: "prefetch",
  displayName: "Stage 0 确定性预热",
  backendDefaults: ["Stage 0 Prefetch", "prefetch_engine"],
  parentNodeId: null,
  slot: null,
  order: 0,
  tier: 0,
  defaultTaskSummary: "并行拉取 SEC 披露、一致预期与确定性财务诊断（0 模型调用）",
};

const CANONICAL_SQUAD_SPECS: readonly CanonicalNodeSpec[] = [
  {
    nodeId: "lead_coordinator",
    role: "lead_coordinator",
    displayName: "Lead Coordinator",
    backendDefaults: ["Lead Coordinator", "lead_coordinator"],
    parentNodeId: null,
    slot: "deep_reasoner",
    order: 10,
    tier: 1,
    defaultTaskSummary: "统筹 Stage 0 快照、匹配 L2 行业方法包并调度并行取证与交叉补证",
  },
  {
    nodeId: "worker_segments",
    role: "worker_segments",
    displayName: "分部与行业 KPI",
    backendDefaults: ["Segments & KPIs", "worker_segments"],
    parentNodeId: "lead_coordinator",
    slot: "fast_extractor",
    order: 20,
    tier: 2,
    defaultTaskSummary: "拆解分部收入、地区/产品结构与 L2 行业关键指标",
  },
  {
    nodeId: "worker_forensics",
    role: "worker_forensics",
    displayName: "财务质量与风险法证",
    backendDefaults: ["Quality Forensics", "worker_forensics"],
    parentNodeId: "lead_coordinator",
    slot: "fast_extractor",
    order: 30,
    tier: 2,
    defaultTaskSummary: "核查红黄绿灯异常、附注、跨期 Redline 变动与 EFTS 线索",
  },
  {
    nodeId: "worker_guidance",
    role: "worker_guidance",
    displayName: "指引与电话会预期差",
    backendDefaults: ["Guidance & Transcript", "worker_guidance"],
    parentNodeId: "lead_coordinator",
    slot: "fast_extractor",
    order: 40,
    tier: 2,
    defaultTaskSummary: "对比双期 8-K 指引、一致预期与管理层电话会问答",
  },
  {
    nodeId: "bear_reviewer",
    role: "bear_reviewer",
    displayName: "独立空头审查",
    backendDefaults: ["Bear Reviewer", "bear_reviewer"],
    parentNodeId: "lead_coordinator",
    slot: "bear_reviewer",
    order: 50,
    tier: 3,
    defaultTaskSummary: "隔离多头叙事，输出证据锚定的空头反证与下季证伪指标",
  },
  {
    nodeId: "dossier_writer",
    role: "writer",
    displayName: "底稿撰稿 (Writer)",
    backendDefaults: ["Dossier Writer", "dossier_writer", "writer"],
    parentNodeId: "lead_coordinator",
    slot: "writer",
    order: 60,
    tier: 4,
    defaultTaskSummary: "执行服务端引文与数值门控，固化底稿并渲染 Memo / 深研报告",
  },
];

const CANONICAL_SPEC_MAP: ReadonlyMap<string, CanonicalNodeSpec> = new Map(
  [PREFETCH_NODE_SPEC, ...CANONICAL_SQUAD_SPECS].map((spec) => [spec.nodeId, spec]),
);

const BASELINE_EDGES: readonly OrgGraphEdge[] = [
  {
    edgeId: "lead_coordinator->worker_segments:flow",
    fromNodeId: "lead_coordinator",
    toNodeId: "worker_segments",
    isReview: false,
    edgeKind: "dispatch",
    label: null,
    active: false,
    triggered: false,
    lastSequence: 0,
    eventCount: 0,
  },
  {
    edgeId: "lead_coordinator->worker_forensics:flow",
    fromNodeId: "lead_coordinator",
    toNodeId: "worker_forensics",
    isReview: false,
    edgeKind: "dispatch",
    label: null,
    active: false,
    triggered: false,
    lastSequence: 0,
    eventCount: 0,
  },
  {
    edgeId: "lead_coordinator->worker_guidance:flow",
    fromNodeId: "lead_coordinator",
    toNodeId: "worker_guidance",
    isReview: false,
    edgeKind: "dispatch",
    label: null,
    active: false,
    triggered: false,
    lastSequence: 0,
    eventCount: 0,
  },
  {
    edgeId: "lead_coordinator->bear_reviewer:review",
    fromNodeId: "lead_coordinator",
    toNodeId: "bear_reviewer",
    isReview: true,
    edgeKind: "review",
    label: null,
    active: false,
    triggered: false,
    lastSequence: 0,
    eventCount: 0,
  },
  {
    edgeId: "bear_reviewer->dossier_writer:review",
    fromNodeId: "bear_reviewer",
    toNodeId: "dossier_writer",
    isReview: true,
    edgeKind: "review",
    label: null,
    active: false,
    triggered: false,
    lastSequence: 0,
    eventCount: 0,
  },
];

function makeInitialNode(spec: CanonicalNodeSpec): OrgGraphNode {
  return {
    nodeId: spec.nodeId,
    role: spec.role,
    displayName: spec.displayName,
    parentNodeId: spec.parentNodeId,
    slot: spec.slot,
    order: spec.order,
    tier: spec.tier,
    stage: null,
    runtimeState: "idle",
    actionKind: null,
    currentTaskSummary: spec.defaultTaskSummary,
    currentActivity: null,
    blockerText: null,
    lastSequence: 0,
    lastTimestamp: null,
    activityLog: [],
  };
}

export function makeEdgeId(fromNodeId: string, toNodeId: string, isReview: boolean): string {
  return `${fromNodeId}->${toNodeId}:${isReview ? "review" : "flow"}`;
}

export function createInitialOrgGraphState(): OrgGraphState {
  return {
    lastSequence: 0,
    runStatus: "idle",
    currentStage: null,
    nodes: CANONICAL_SQUAD_SPECS.map(makeInitialNode),
    edges: BASELINE_EDGES,
  };
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function resolveDisplayName(nodeId: string, incomingDisplayName: string, previousDisplayName: string | null): string {
  const cleaned = incomingDisplayName.trim();
  const spec = CANONICAL_SPEC_MAP.get(nodeId);
  if (!spec) {
    return cleaned || previousDisplayName || nodeId;
  }
  if (!cleaned || spec.backendDefaults.includes(cleaned)) {
    return previousDisplayName || spec.displayName;
  }
  return cleaned;
}

function isTerminalNodeState(state: OrgRuntimeState): boolean {
  return state === "completed" || state === "failed" || state === "cancelled" || state === "skipped";
}

function inferTierForUnknownNode(role: string, parentNodeId: string | null): 0 | 1 | 2 | 3 | 4 {
  if (role === "prefetch") return 0;
  if (role === "lead_coordinator") return 1;
  if (role === "bear_reviewer") return 3;
  if (role === "writer" || role === "dossier_writer") return 4;
  if (parentNodeId === "lead_coordinator") return 2;
  return 2;
}

/**
 * Validates an untrusted JSON payload (e.g. SSE `org_graph_event` frame data) into a typed
 * `OrgGraphEventInput` or returns `null` when required structural fields are invalid.
 */
export function parseOrgGraphEvent(raw: unknown): OrgGraphEventInput | null {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return null;
  const obj = raw as Record<string, unknown>;
  const sequence = obj["sequence"];
  if (typeof sequence !== "number" || !Number.isInteger(sequence) || sequence < 1) return null;

  const stage = obj["stage"];
  const nodeId = obj["node_id"];
  const role = obj["role"];
  const displayName = obj["display_name"];
  const runtimeState = obj["runtime_state"];
  const actionKind = obj["action_kind"];
  const currentActivity = obj["current_activity"];

  if (
    !isNonEmptyString(stage) ||
    !isNonEmptyString(nodeId) ||
    !isNonEmptyString(role) ||
    !isNonEmptyString(displayName) ||
    !isNonEmptyString(runtimeState) ||
    !isNonEmptyString(actionKind) ||
    !isNonEmptyString(currentActivity)
  ) {
    return null;
  }

  if (!ORG_RUNTIME_STATES.has(runtimeState as OrgRuntimeState)) return null;
  if (!ORG_ACTION_KINDS.has(actionKind as OrgActionKind)) return null;

  const rawEdge = obj["edge_event"];
  let edgeEvent: ResearchV2.OrgEdgeEvent | null = null;
  if (rawEdge !== undefined && rawEdge !== null) {
    if (typeof rawEdge !== "object" || Array.isArray(rawEdge)) return null;
    const edgeObj = rawEdge as Record<string, unknown>;
    const fromId = edgeObj["from_node_id"];
    const toId = edgeObj["to_node_id"];
    if (!isNonEmptyString(fromId) || !isNonEmptyString(toId)) return null;
    const rawKind = edgeObj["edge_kind"];
    const edgeKind: OrgEdgeKind =
      typeof rawKind === "string" && ORG_EDGE_KINDS.has(rawKind as OrgEdgeKind)
        ? (rawKind as OrgEdgeKind)
        : Boolean(edgeObj["is_review"])
          ? "review"
          : "dispatch";
    edgeEvent = {
      from_node_id: fromId.trim(),
      to_node_id: toId.trim(),
      is_review: Boolean(edgeObj["is_review"]),
      edge_kind: edgeKind,
      label: typeof edgeObj["label"] === "string" ? edgeObj["label"] : null,
    };
  }

  const slotRaw = obj["slot"];
  const slot: OrgModelSlot | null =
    slotRaw === "fast_extractor" ||
    slotRaw === "deep_reasoner" ||
    slotRaw === "bear_reviewer" ||
    slotRaw === "writer"
      ? slotRaw
      : null;

  return {
    sequence,
    stage: stage.trim(),
    node_id: nodeId.trim(),
    role: role.trim(),
    display_name: displayName.trim(),
    runtime_state: runtimeState as OrgRuntimeState,
    action_kind: actionKind as OrgActionKind,
    current_activity: currentActivity.trim(),
    blocker_text: typeof obj["blocker_text"] === "string" ? obj["blocker_text"] : null,
    edge_event: edgeEvent,
    slot,
    parent_node_id: typeof obj["parent_node_id"] === "string" ? obj["parent_node_id"] : null,
    ...(typeof obj["timestamp"] === "string" ? { timestamp: obj["timestamp"] } : {}),
    ...(typeof obj["operation_id"] === "string" ? { operation_id: obj["operation_id"] } : {}),
  };
}

function sortNodesStable(nodes: readonly OrgGraphNode[]): readonly OrgGraphNode[] {
  return [...nodes].sort((a, b) => a.order - b.order || a.nodeId.localeCompare(b.nodeId));
}

function nextCustomOrder(nodes: readonly OrgGraphNode[]): number {
  let maxOrder = 99;
  for (const node of nodes) {
    if (node.order > maxOrder) maxOrder = node.order;
  }
  return maxOrder + 1;
}

function createDynamicNode(nodeId: string, role: string, displayName: string, parentNodeId: string | null, slot: OrgModelSlot | null, order: number): OrgGraphNode {
  const spec = CANONICAL_SPEC_MAP.get(nodeId);
  if (spec) return makeInitialNode(spec);
  return {
    nodeId,
    role,
    displayName: displayName.trim() || nodeId,
    parentNodeId,
    slot,
    order,
    tier: inferTierForUnknownNode(role, parentNodeId),
    stage: null,
    runtimeState: "idle",
    actionKind: null,
    currentTaskSummary: null,
    currentActivity: null,
    blockerText: null,
    lastSequence: 0,
    lastTimestamp: null,
    activityLog: [],
  };
}

function settleRunNodes(
  nodes: readonly OrgGraphNode[],
  edges: readonly OrgGraphEdge[],
  terminalStatus: "completed" | "failed" | "cancelled",
): { readonly nodes: readonly OrgGraphNode[]; readonly edges: readonly OrgGraphEdge[] } {
  const nextNodes = nodes.map((node) => {
    if (terminalStatus === "completed" && node.nodeId === "lead_coordinator" && (node.runtimeState === "running" || node.runtimeState === "waiting")) {
      return {
        ...node,
        runtimeState: "completed" as const,
      };
    }
    if (terminalStatus !== "completed" && (node.runtimeState === "running" || node.runtimeState === "waiting")) {
      return {
        ...node,
        runtimeState: terminalStatus === "cancelled" ? ("cancelled" as const) : ("failed" as const),
      };
    }
    return node;
  });

  const nextEdges = edges.map((edge) => (edge.active ? { ...edge, active: false } : edge));
  return { nodes: nextNodes, edges: nextEdges };
}

/**
 * Pure immutable reducer for a single `OrgGraphEvent`.
 *
 * Enforces strict monotonic `sequence` deduplication (`event.sequence <= state.lastSequence` is a no-op),
 * stable node ordering, distinction between `is_review: true` review edges and delegation edges,
 * and never infers `runtimeState` or `actionKind` from free-text `current_activity` prose.
 */
export function reduceOrgGraphEvent(state: OrgGraphState, rawEvent: OrgGraphEventInput): OrgGraphState {
  const event = parseOrgGraphEvent(rawEvent);
  if (!event) return state;
  if (event.sequence <= state.lastSequence) return state;

  const nodeId = event.node_id;
  const existingIdx = state.nodes.findIndex((n) => n.nodeId === nodeId);
  let workingNodes = [...state.nodes];
  let customOrder = nextCustomOrder(workingNodes);

  let targetNode: OrgGraphNode;
  if (existingIdx >= 0) {
    targetNode = workingNodes[existingIdx]!;
  } else {
    targetNode = createDynamicNode(
      nodeId,
      event.role,
      event.display_name,
      event.parent_node_id ?? null,
      event.slot ?? null,
      customOrder,
    );
    customOrder += 1;
  }

  const trimmedBlocker =
    typeof event.blocker_text === "string" && event.blocker_text.trim().length > 0
      ? event.blocker_text.trim()
      : null;

  const nextBlockerText =
    trimmedBlocker !== null
      ? trimmedBlocker
      : event.runtime_state === "running"
        ? null
        : targetNode.blockerText;

  const logEntry: OrgActivityLogEntry = {
    sequence: event.sequence,
    stage: event.stage,
    actionKind: event.action_kind,
    runtimeState: event.runtime_state,
    text: event.current_activity,
    timestamp: event.timestamp ?? null,
  };

  const nextLog =
    targetNode.activityLog.length >= MAX_NODE_LOG_ENTRIES
      ? [...targetNode.activityLog.slice(-(MAX_NODE_LOG_ENTRIES - 1)), logEntry]
      : [...targetNode.activityLog, logEntry];

  const updatedNode: OrgGraphNode = {
    ...targetNode,
    role: event.role,
    displayName: resolveDisplayName(nodeId, event.display_name, existingIdx >= 0 ? targetNode.displayName : null),
    parentNodeId: event.parent_node_id !== undefined ? event.parent_node_id : targetNode.parentNodeId,
    slot: event.slot ?? targetNode.slot,
    stage: event.stage,
    runtimeState: event.runtime_state,
    actionKind: event.action_kind,
    currentActivity: event.current_activity,
    blockerText: nextBlockerText,
    lastSequence: event.sequence,
    lastTimestamp: event.timestamp ?? targetNode.lastTimestamp,
    activityLog: nextLog,
  };

  if (existingIdx >= 0) {
    workingNodes[existingIdx] = updatedNode;
  } else {
    workingNodes.push(updatedNode);
    workingNodes = [...sortNodesStable(workingNodes)];
  }

  // Process optional edge event and keep edges immutable.
  let workingEdges = state.edges.map((edge) => {
    // Deactivate edges pointing into a node once that node finishes or stops running.
    if (edge.active && edge.toNodeId === nodeId && isTerminalNodeState(event.runtime_state)) {
      return { ...edge, active: false };
    }
    return edge;
  });

  if (event.edge_event) {
    const edgeEv = event.edge_event;
    const isReview = Boolean(edgeEv.is_review);
    const edgeKind: OrgEdgeKind = edgeEv.edge_kind ?? (isReview ? "review" : "dispatch");
    const edgeId = makeEdgeId(edgeEv.from_node_id, edgeEv.to_node_id, isReview);
    const edgeLabel = typeof edgeEv.label === "string" && edgeEv.label.trim().length > 0 ? edgeEv.label.trim() : null;
    const edgeIdx = workingEdges.findIndex((e) => e.edgeId === edgeId);
    const isEdgeActive = !isTerminalNodeState(event.runtime_state);

    if (edgeIdx >= 0) {
      const prev = workingEdges[edgeIdx]!;
      workingEdges[edgeIdx] = {
        ...prev,
        isReview,
        edgeKind,
        label: edgeLabel ?? prev.label,
        active: isEdgeActive,
        triggered: true,
        lastSequence: event.sequence,
        eventCount: prev.eventCount + 1,
      };
    } else {
      workingEdges = [
        ...workingEdges,
        {
          edgeId,
          fromNodeId: edgeEv.from_node_id,
          toNodeId: edgeEv.to_node_id,
          isReview,
          edgeKind,
          label: edgeLabel,
          active: isEdgeActive,
          triggered: true,
          lastSequence: event.sequence,
          eventCount: 1,
        },
      ];
    }

    // Update target node's concise currentTaskSummary strictly from structured coordinator task assignments.
    const updatesTaskSummary =
      edgeLabel !== null &&
      (edgeKind === "dispatch" ||
        edgeKind === "steering" ||
        (edgeKind === "review" &&
          edgeEv.from_node_id === "lead_coordinator" &&
          edgeEv.to_node_id !== "bear_reviewer"));
    if (updatesTaskSummary) {
      const toIdx = workingNodes.findIndex((n) => n.nodeId === edgeEv.to_node_id);
      if (toIdx >= 0) {
        const dest = workingNodes[toIdx]!;
        workingNodes[toIdx] = {
          ...dest,
          currentTaskSummary: edgeLabel,
        };
      }
    }
  }

  // Derive runStatus strictly from structured node_id + stage + runtime_state fields.
  let nextRunStatus: OrgRunStatus =
    event.runtime_state === "running" || state.runStatus === "idle" ? "running" : state.runStatus;
  if (
    (nodeId === "dossier_writer" || event.role === "writer") &&
    (event.stage === "stage_3b_gate_memo" || event.stage === "deep_report") &&
    event.runtime_state === "completed"
  ) {
    nextRunStatus = "completed";
    const settled = settleRunNodes(workingNodes, workingEdges, "completed");
    workingNodes = [...settled.nodes];
    workingEdges = [...settled.edges];
  } else if (
    (nodeId === "lead_coordinator" || nodeId === "bear_reviewer" || nodeId === "dossier_writer") &&
    event.runtime_state === "failed"
  ) {
    nextRunStatus = "failed";
    const settled = settleRunNodes(workingNodes, workingEdges, "failed");
    workingNodes = [...settled.nodes];
    workingEdges = [...settled.edges];
  }

  return {
    lastSequence: event.sequence,
    runStatus: nextRunStatus,
    currentStage: event.stage,
    nodes: workingNodes,
    edges: workingEdges,
  };
}

export function reduceOrgGraphEvents(
  initialState: OrgGraphState,
  events: readonly OrgGraphEventInput[],
): OrgGraphState {
  let state = initialState;
  for (const event of events) {
    state = reduceOrgGraphEvent(state, event);
  }
  return state;
}

export function orgGraphReducer(state: OrgGraphState, action: OrgGraphAction): OrgGraphState {
  switch (action.type) {
    case "event":
      return reduceOrgGraphEvent(state, action.event);
    case "batch":
      return reduceOrgGraphEvents(state, action.events);
    case "run_terminal": {
      const settled = settleRunNodes(state.nodes, state.edges, action.status);
      return {
        ...state,
        runStatus: action.status,
        nodes: settled.nodes,
        edges: settled.edges,
      };
    }
    case "reset":
      return createInitialOrgGraphState();
  }
}
