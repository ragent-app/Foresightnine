import {
  BookOpen,
  ChevronDown,
  ChevronUp,
  CircleAlert,
  CircleCheck,
  Clock,
  Eye,
  GitBranch,
  Layers,
  Search,
  ShieldAlert,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { formatDateTime, formatInteger } from "../../lib/format";
import { MEDIA, useMedia } from "../../ui/media";
import { Panel } from "../../ui/Panel";
import { wrapCausalLabel } from "./charts/causalGraphLayout";
import {
  createInitialOrgGraphState,
  reduceOrgGraphEvents,
  type OrgActionKind,
  type OrgGraphEdge,
  type OrgGraphEventInput,
  type OrgGraphNode,
  type OrgGraphState,
  type OrgModelSlot,
  type OrgRunStatus,
  type OrgRuntimeState,
} from "./orgGraphReducer";

export interface OrgGraphPanelProps {
  /** Sequenced events from SSE (`org_graph_event`) or dossier/checkpoint response. */
  events?: readonly OrgGraphEventInput[];
  /** Optional pre-reduced organization graph state (takes precedence over `events`). */
  state?: OrgGraphState;
  /** Authoritative run status when supplied by parent endpoint (`running` / `completed` / `failed` / `cancelled`). */
  runStatus?: OrgRunStatus;
  /** Accessible panel heading. */
  title?: string;
  /** Optional test/override flag for `prefers-reduced-motion`. */
  reducedMotion?: boolean;
  /** Optional initial expanded override. Defaults to expanded while active and collapsed when completed. */
  defaultExpanded?: boolean;
  /** Optional notification when the user selects or closes a node drawer. */
  onSelectNode?: (node: OrgGraphNode | null) => void;
}

interface ActionVisualSpec {
  readonly label: string;
  readonly color: string;
  readonly roleClass: "support" | "counter" | "review" | "error" | "neutral";
  readonly icon: LucideIcon;
}

export const ACTION_KIND_META: Readonly<Record<OrgActionKind, ActionVisualSpec>> = {
  READING: { label: "阅读资料", color: "var(--counter)", roleClass: "counter", icon: BookOpen },
  SEARCHING: { label: "检索线索", color: "var(--counter)", roleClass: "counter", icon: Search },
  INSPECTING: { label: "核验分析", color: "var(--chart-4)", roleClass: "counter", icon: Eye },
  DELEGATING: { label: "派发任务", color: "var(--counter)", roleClass: "counter", icon: GitBranch },
  WAITING_SUBORDINATES: {
    label: "等待并行节点",
    color: "var(--on-variant)",
    roleClass: "neutral",
    icon: Clock,
  },
  RESPONDING: { label: "生成交付", color: "var(--success)", roleClass: "support", icon: CircleCheck },
  NEEDS_USER: { label: "需补充信息", color: "var(--warning)", roleClass: "review", icon: CircleAlert },
  BLOCKED: { label: "已阻断", color: "var(--warning)", roleClass: "review", icon: ShieldAlert },
  FAILED: { label: "执行失败", color: "var(--error)", roleClass: "error", icon: XCircle },
};

interface RuntimeVisualSpec {
  readonly label: string;
  readonly stroke: string;
  readonly fill: string;
  readonly textColor: string;
  readonly accent: string;
  readonly roleClass: "support" | "counter" | "review" | "error" | "neutral";
  readonly icon: LucideIcon;
}

export const RUNTIME_STATE_META: Readonly<Record<OrgRuntimeState, RuntimeVisualSpec>> = {
  idle: {
    label: "待命",
    stroke: "var(--separator)",
    fill: "var(--s-low)",
    textColor: "var(--on-tertiary)",
    accent: "var(--on-tertiary)",
    roleClass: "neutral",
    icon: Clock,
  },
  running: {
    label: "运行中",
    stroke: "var(--running)",
    fill: "var(--surface)",
    textColor: "var(--running)",
    accent: "var(--running)",
    roleClass: "counter",
    icon: Layers,
  },
  waiting: {
    label: "等待中",
    stroke: "var(--on-tertiary)",
    fill: "var(--s-low)",
    textColor: "var(--on-variant)",
    accent: "var(--on-variant)",
    roleClass: "neutral",
    icon: Clock,
  },
  completed: {
    label: "已完成",
    stroke: "var(--done)",
    fill: "var(--surface)",
    textColor: "var(--done)",
    accent: "var(--done)",
    roleClass: "support",
    icon: CircleCheck,
  },
  failed: {
    label: "失败",
    stroke: "var(--failed)",
    fill: "var(--error-t)",
    textColor: "var(--failed)",
    accent: "var(--failed)",
    roleClass: "error",
    icon: XCircle,
  },
  blocked: {
    label: "受阻",
    stroke: "var(--waiting)",
    fill: "var(--warning-t)",
    textColor: "var(--waiting)",
    accent: "var(--waiting)",
    roleClass: "review",
    icon: ShieldAlert,
  },
  cancelled: {
    label: "已取消",
    stroke: "var(--on-tertiary)",
    fill: "var(--s-low)",
    textColor: "var(--on-tertiary)",
    accent: "var(--on-tertiary)",
    roleClass: "neutral",
    icon: CircleAlert,
  },
  skipped: {
    label: "已跳过",
    stroke: "var(--separator)",
    fill: "var(--s-low)",
    textColor: "var(--on-tertiary)",
    accent: "var(--on-tertiary)",
    roleClass: "neutral",
    icon: CircleAlert,
  },
};

const STAGE_LABEL: Readonly<Record<string, string>> = {
  stage_0_prefetch: "Stage 0 · 确定性预热",
  stage_1_plan: "Stage 1 · 规划与派发",
  stage_2_workers: "Stage 2 · 并行专题取证",
  steering: "Steering · 运行中重派",
  stage_2_5_review: "Stage 2.5 · 交叉复核补证",
  stage_3a_bear: "Stage 3A · 独立空头审查",
  stage_3b_gate_memo: "Stage 3B · 门控与底稿渲染",
  deep_report: "Deep Report · 深研报告渲染",
};

const SLOT_LABEL: Readonly<Record<OrgModelSlot, string>> = {
  deep_reasoner: "deep_reasoner（统筹推理）",
  fast_extractor: "fast_extractor（并行取证）",
  bear_reviewer: "bear_reviewer（独立空头）",
  writer: "writer（底稿撰稿）",
};

const RUN_STATUS_META: Readonly<Record<OrgRunStatus, { label: string; roleClass: string; icon: LucideIcon }>> = {
  idle: { label: "待启动", roleClass: "neutral", icon: Clock },
  running: { label: "协同执行中", roleClass: "counter", icon: Layers },
  completed: { label: "已完成", roleClass: "support", icon: CircleCheck },
  failed: { label: "运行中断", roleClass: "error", icon: XCircle },
  cancelled: { label: "已取消", roleClass: "neutral", icon: CircleAlert },
};

const FOCUS_NODE_IDS = [
  "lead_coordinator",
  "worker_segments",
  "worker_forensics",
  "worker_guidance",
  "bear_reviewer",
  "dossier_writer",
] as const;

type FocusNodeId = (typeof FOCUS_NODE_IDS)[number];

interface NodeBoxRect {
  readonly x: number;
  readonly y: number;
  readonly width: number;
  readonly height: number;
}

interface PositionedEdge {
  readonly key: string;
  readonly fromNodeId: string;
  readonly toNodeId: string;
  readonly path: string;
  readonly isReview: boolean;
  readonly edgeKind: "dispatch" | "report" | "review" | "steering" | "handoff";
  readonly label: string | null;
  readonly labelX: number;
  readonly labelY: number;
  readonly triggered: boolean;
  readonly transmitting: boolean;
  readonly isSteering: boolean;
}

const DESKTOP_RECTS: Readonly<Record<FocusNodeId, NodeBoxRect>> = {
  lead_coordinator: { x: 12, y: 122, width: 154, height: 84 },
  worker_segments: { x: 198, y: 38, width: 174, height: 76 },
  worker_forensics: { x: 198, y: 126, width: 174, height: 76 },
  worker_guidance: { x: 198, y: 214, width: 174, height: 76 },
  bear_reviewer: { x: 406, y: 122, width: 152, height: 84 },
  dossier_writer: { x: 588, y: 122, width: 144, height: 84 },
};

const MOBILE_RECTS: Readonly<Record<FocusNodeId, NodeBoxRect>> = {
  lead_coordinator: { x: 24, y: 14, width: 312, height: 72 },
  worker_segments: { x: 52, y: 110, width: 292, height: 70 },
  worker_forensics: { x: 52, y: 192, width: 292, height: 70 },
  worker_guidance: { x: 52, y: 274, width: 292, height: 70 },
  bear_reviewer: { x: 24, y: 368, width: 312, height: 72 },
  dossier_writer: { x: 24, y: 456, width: 312, height: 72 },
};

function stageLabel(stage: string | null | undefined): string {
  if (!stage) return "未启动";
  return STAGE_LABEL[stage] ?? stage;
}

function actionLabel(kind: OrgActionKind | null): string {
  if (!kind) return "待派发";
  return ACTION_KIND_META[kind].label;
}

function actionColor(kind: OrgActionKind | null, fallbackColor: string): string {
  if (!kind) return fallbackColor;
  return ACTION_KIND_META[kind].color;
}

function actionIcon(kind: OrgActionKind | null, fallbackIcon: LucideIcon): LucideIcon {
  if (!kind) return fallbackIcon;
  return ACTION_KIND_META[kind].icon;
}

function handleEnterOrSpace(event: KeyboardEvent, action: () => void): void {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    action();
  }
}

function truncateLine(raw: string, maxUnits: number): string {
  return wrapCausalLabel(raw, maxUnits, 1)[0] ?? raw;
}

function buildFocusEdges(
  nodeById: ReadonlyMap<string, OrgGraphNode>,
  stateEdges: readonly OrgGraphEdge[],
  isDesktop: boolean,
  runActive: boolean,
): PositionedEdge[] {
  const rects = isDesktop ? DESKTOP_RECTS : MOBILE_RECTS;
  const edgeMap = new Map(stateEdges.map((edge) => [edge.edgeId, edge]));
  const result: PositionedEdge[] = [];

  const isRunning = (nodeId: string): boolean => nodeById.get(nodeId)?.runtimeState === "running";
  const isCompleted = (nodeId: string): boolean => nodeById.get(nodeId)?.runtimeState === "completed";

  // 1. Lead Coordinator -> 3 Parallel Workers (delegation / steering / Stage 2.5 review).
  const workerIds: readonly FocusNodeId[] = ["worker_segments", "worker_forensics", "worker_guidance"];
  const leadRect = rects.lead_coordinator;

  for (const workerId of workerIds) {
    const wRect = rects[workerId];
    const flowEdge = edgeMap.get(`lead_coordinator->${workerId}:flow`);
    const reviewEdge = edgeMap.get(`lead_coordinator->${workerId}:review`);
    const useReview = Boolean(reviewEdge?.triggered && reviewEdge.lastSequence >= (flowEdge?.lastSequence ?? 0));
    const edgeData = useReview ? reviewEdge : flowEdge;

    const path = isDesktop
      ? (() => {
          const x1 = leadRect.x + leadRect.width;
          const y1 = leadRect.y + leadRect.height / 2;
          const x2 = wRect.x;
          const y2 = wRect.y + wRect.height / 2;
          const c1x = x1 + (x2 - x1) * 0.52;
          const c2x = x2 - (x2 - x1) * 0.52;
          return `M ${x1} ${y1} C ${c1x} ${y1}, ${c2x} ${y2}, ${x2} ${y2}`;
        })()
      : (() => {
          const x1 = leadRect.x + 14;
          const y1 = leadRect.y + leadRect.height;
          const x2 = wRect.x;
          const y2 = wRect.y + wRect.height / 2;
          return `M ${x1} ${y1} C ${x1} ${y2}, ${x1 + 6} ${y2}, ${x2} ${y2}`;
        })();

    const triggered = Boolean(edgeData?.triggered);
    const workerRunning = isRunning(workerId);
    const transmitting = runActive && (workerRunning || Boolean(edgeData?.active && isRunning("lead_coordinator")));

    result.push({
      key: `lead->${workerId}`,
      fromNodeId: "lead_coordinator",
      toNodeId: workerId,
      path,
      isReview: useReview,
      edgeKind: useReview ? "review" : (edgeData?.edgeKind ?? "dispatch"),
      label: edgeData?.label ?? null,
      labelX: 0,
      labelY: 0,
      triggered,
      transmitting,
      isSteering: edgeData?.edgeKind === "steering",
    });

    // Optional worker -> lead report-back review curve when triggered.
    const reportEdge = edgeMap.get(`${workerId}->lead_coordinator:review`);
    if (reportEdge?.triggered) {
      const reportPath = isDesktop
        ? (() => {
            const x1 = wRect.x;
            const y1 = wRect.y + wRect.height / 2 + 12;
            const x2 = leadRect.x + leadRect.width;
            const y2 = leadRect.y + leadRect.height / 2 + 14;
            return `M ${x1} ${y1} C ${x1 - 14} ${y1 + 14}, ${x2 + 14} ${y2 + 14}, ${x2} ${y2}`;
          })()
        : (() => {
            const x1 = wRect.x;
            const y1 = wRect.y + wRect.height / 2 + 10;
            const x2 = leadRect.x + 28;
            const y2 = leadRect.y + leadRect.height;
            return `M ${x1} ${y1} C ${x2 + 2} ${y1 + 6}, ${x2} ${y2 + 16}, ${x2} ${y2}`;
          })();
      result.push({
        key: `${workerId}->lead_report`,
        fromNodeId: workerId,
        toNodeId: "lead_coordinator",
        path: reportPath,
        isReview: true,
        edgeKind: "report",
        label: reportEdge.label,
        labelX: 0,
        labelY: 0,
        triggered: true,
        transmitting: false,
        isSteering: false,
      });
    }
  }

  // 2. Parallel Workers -> Bear Reviewer (convergence handoff into independent bear review).
  const bearRect = rects.bear_reviewer;
  const bearActive = isRunning("bear_reviewer");
  const bearDoneOrActive = bearActive || isCompleted("bear_reviewer") || nodeById.get("bear_reviewer")?.runtimeState === "failed";

  for (const workerId of workerIds) {
    const wRect = rects[workerId];
    const workerDone = isCompleted(workerId);
    const path = isDesktop
      ? (() => {
          const x1 = wRect.x + wRect.width;
          const y1 = wRect.y + wRect.height / 2;
          const x2 = bearRect.x;
          const y2 = bearRect.y + bearRect.height / 2;
          const c1x = x1 + (x2 - x1) * 0.52;
          const c2x = x2 - (x2 - x1) * 0.52;
          return `M ${x1} ${y1} C ${c1x} ${y1}, ${c2x} ${y2}, ${x2} ${y2}`;
        })()
      : (() => {
          const x1 = wRect.x + wRect.width;
          const y1 = wRect.y + wRect.height / 2;
          const x2 = bearRect.x + bearRect.width - 14;
          const y2 = bearRect.y;
          return `M ${x1} ${y1} C ${x1 + 14} ${y1}, ${x2 + 18} ${y2 - 14}, ${x2} ${y2}`;
        })();

    result.push({
      key: `${workerId}->bear`,
      fromNodeId: workerId,
      toNodeId: "bear_reviewer",
      path,
      isReview: true,
      edgeKind: "handoff",
      label: null,
      labelX: 0,
      labelY: 0,
      triggered: workerDone || bearDoneOrActive,
      transmitting: runActive && bearActive && workerDone,
      isSteering: false,
    });
  }

  // 3. Lead Coordinator -> Bear Reviewer (explicit isolated review arch, is_review: true).
  const leadToBear = edgeMap.get("lead_coordinator->bear_reviewer:review");
  const leadToBearPath = isDesktop
    ? (() => {
        const x1 = leadRect.x + leadRect.width * 0.52;
        const y1 = leadRect.y;
        const x2 = bearRect.x + bearRect.width * 0.5;
        const y2 = bearRect.y;
        return `M ${x1} ${y1} C ${x1 + 44} 8, ${x2 - 44} 8, ${x2} ${y2}`;
      })()
    : (() => {
        const x1 = leadRect.x;
        const y1 = leadRect.y + leadRect.height / 2;
        const x2 = bearRect.x;
        const y2 = bearRect.y + bearRect.height / 2;
        return `M ${x1} ${y1} C 4 140, 4 300, ${x2} ${y2}`;
      })();

  result.push({
    key: "lead->bear_review",
    fromNodeId: "lead_coordinator",
    toNodeId: "bear_reviewer",
    path: leadToBearPath,
    isReview: true,
    edgeKind: "review",
    label: leadToBear?.label ?? "独立空头审查",
    labelX: isDesktop ? 285 : 74,
    labelY: isDesktop ? 18 : 228,
    triggered: Boolean(leadToBear?.triggered || bearDoneOrActive),
    transmitting: runActive && bearActive,
    isSteering: false,
  });

  // 4. Bear Reviewer -> Dossier Writer (is_review: true anchored review handoff).
  const writerRect = rects.dossier_writer;
  const bearToWriter = edgeMap.get("bear_reviewer->dossier_writer:review");
  const writerRunning = isRunning("dossier_writer");
  const writerDone = isCompleted("dossier_writer");
  const bearToWriterPath = isDesktop
    ? (() => {
        const x1 = bearRect.x + bearRect.width;
        const y1 = bearRect.y + bearRect.height / 2;
        const x2 = writerRect.x;
        const y2 = writerRect.y + writerRect.height / 2;
        return `M ${x1} ${y1} C ${x1 + 12} ${y1}, ${x2 - 12} ${y2}, ${x2} ${y2}`;
      })()
    : (() => {
        const x1 = bearRect.x + bearRect.width / 2;
        const y1 = bearRect.y + bearRect.height;
        const x2 = writerRect.x + writerRect.width / 2;
        const y2 = writerRect.y;
        return `M ${x1} ${y1} C ${x1} ${y1 + 6}, ${x2} ${y2 - 6}, ${x2} ${y2}`;
      })();

  result.push({
    key: "bear->writer_review",
    fromNodeId: "bear_reviewer",
    toNodeId: "dossier_writer",
    path: bearToWriterPath,
    isReview: true,
    edgeKind: "review",
    label: bearToWriter?.label ?? "反证锚定交付",
    labelX: isDesktop ? (bearRect.x + bearRect.width + writerRect.x) / 2 : bearRect.x + bearRect.width / 2 + 54,
    labelY: isDesktop ? bearRect.y + bearRect.height / 2 - 12 : bearRect.y + bearRect.height + 11,
    triggered: Boolean(bearToWriter?.triggered || writerRunning || writerDone),
    transmitting: runActive && writerRunning,
    isSteering: false,
  });

  return result;
}

function edgeStrokeColor(edge: PositionedEdge): string {
  if (edge.isSteering && edge.triggered) return "var(--warning)";
  if (edge.isReview) {
    if (edge.transmitting) return "var(--chart-4)";
    return edge.triggered ? "var(--chart-4)" : "var(--on-tertiary)";
  }
  if (edge.transmitting) return "var(--running)";
  return edge.triggered ? "var(--done)" : "var(--separator)";
}

function NodeChipButton({ node, onClick }: { node: OrgGraphNode; onClick: (node: OrgGraphNode) => void }) {
  const sMeta = RUNTIME_STATE_META[node.runtimeState];
  const NIcon = actionIcon(node.actionKind, sMeta.icon);
  return (
    <button
      type="button"
      className="chart-tool-btn"
      onClick={() => onClick(node)}
      aria-label={`${node.displayName}：${sMeta.label}，查看过程日志`}
    >
      <NIcon size={12} color={sMeta.accent} aria-hidden="true" />
      <span>{node.displayName}</span>
      <span className={`role light ${sMeta.roleClass}`}>{sMeta.label}</span>
    </button>
  );
}

function NodeProcessDrawer({ node, onClose }: { node: OrgGraphNode; onClose: () => void }) {
  const stateMeta = RUNTIME_STATE_META[node.runtimeState];
  const actLabel = actionLabel(node.actionKind);
  const actColor = actionColor(node.actionKind, stateMeta.accent);
  const ActIcon = actionIcon(node.actionKind, stateMeta.icon);
  const actRoleClass = node.actionKind ? ACTION_KIND_META[node.actionKind].roleClass : stateMeta.roleClass;
  const logsReversed = useMemo(() => [...node.activityLog].reverse(), [node.activityLog]);

  return (
    <Panel title={node.displayName} onClose={onClose} detents bodyClassName="tight">
      <div className="item">
        <div className="line">
          <ActIcon size={16} color={actColor} aria-hidden="true" />
          <span className={`role ${actRoleClass}`}>{actLabel}</span>
          <span className={`role light ${stateMeta.roleClass}`}>{stateMeta.label}</span>
          {node.lastSequence > 0 ? <span className="meta mono">{`#${node.lastSequence}`}</span> : null}
        </div>
        <div className="chips">
          <span>{`阶段：${stageLabel(node.stage)}`}</span>
          {node.slot ? <span>{`槽位：${SLOT_LABEL[node.slot] ?? node.slot}`}</span> : null}
        </div>
      </div>

      <div>
        <div className="gtitle">当前任务摘要</div>
        <div className="item">
          <span>{node.currentTaskSummary || "暂无任务分配"}</span>
        </div>
      </div>

      <div>
        <div className="gtitle">实时动作</div>
        <div className="item">
          <span>{node.currentActivity || "待命中，等待上游节点完成。"}</span>
          {node.lastTimestamp ? (
            <span className="meta">{`最近更新：${formatDateTime(node.lastTimestamp) || node.lastTimestamp}`}</span>
          ) : null}
        </div>
      </div>

      {node.blockerText ? (
        <div className={`callout${node.runtimeState === "failed" ? " error" : ""}`} role="alert">
          <div className="h">
            <CircleAlert size={16} aria-hidden="true" />
            <span>阻断 / 异常说明</span>
          </div>
          <p>{node.blockerText}</p>
        </div>
      ) : null}

      <div>
        <div className="gtitle">{`简洁过程日志 · ${formatInteger(logsReversed.length)} 条`}</div>
        {logsReversed.length === 0 ? (
          <p className="empty-note">尚无过程日志事件。</p>
        ) : (
          <ol className="pt-list in-panel" aria-label={`${node.displayName} 过程日志`}>
            {logsReversed.map((entry) => {
              const entryAct = ACTION_KIND_META[entry.actionKind];
              const entryState = RUNTIME_STATE_META[entry.runtimeState];
              const EntryIcon = entryAct.icon;
              const formattedTime = entry.timestamp ? formatDateTime(entry.timestamp) : "";
              return (
                <li
                  key={entry.sequence}
                  className={`pt-row${entry.runtimeState === "running" ? " pt-current" : ""}`}
                >
                  <span className="pt-ic" aria-hidden="true">
                    <EntryIcon size={14} color={entryAct.color} />
                  </span>
                  <div className="pt-main">
                    <div className="pt-head">
                      <span className="pt-label">{entryAct.label}</span>
                      <span className={`role light ${entryState.roleClass}`}>{entryState.label}</span>
                    </div>
                    <div className="pt-why">{entry.text}</div>
                    <div className="meta">
                      {[stageLabel(entry.stage), formattedTime || null, `#${entry.sequence}`]
                        .filter(Boolean)
                        .join(" · ")}
                    </div>
                  </div>
                </li>
              );
            })}
          </ol>
        )}
      </div>
    </Panel>
  );
}

export function OrgGraphPanel({
  events,
  state: externalState,
  runStatus: runStatusProp,
  title = "买方投研小组协同拓扑",
  reducedMotion: reducedMotionProp,
  defaultExpanded,
  onSelectNode,
}: OrgGraphPanelProps) {
  const uid = useId().replace(/:/g, "");
  const isDesktop = useMedia(MEDIA.tablet);
  const systemReducedMotion = useMedia(MEDIA.reducedMotion);
  const reducedMotion = reducedMotionProp ?? systemReducedMotion;

  const derivedState = useMemo(() => {
    if (externalState) return externalState;
    return reduceOrgGraphEvents(createInitialOrgGraphState(), events ?? []);
  }, [externalState, events]);

  const effectiveRunStatus: OrgRunStatus = runStatusProp ?? derivedState.runStatus;
  const isCompleted = effectiveRunStatus === "completed";
  const runActive = effectiveRunStatus === "running";

  const [expanded, setExpanded] = useState<boolean>(() => defaultExpanded ?? !isCompleted);
  const prevCompletedRef = useRef<boolean>(isCompleted);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);

  useEffect(() => {
    if (prevCompletedRef.current !== isCompleted) {
      prevCompletedRef.current = isCompleted;
      setExpanded(!isCompleted);
    }
  }, [isCompleted]);

  const nodeById = useMemo(
    () => new Map(derivedState.nodes.map((node) => [node.nodeId, node])),
    [derivedState.nodes],
  );

  const selectedNode = selectedNodeId ? nodeById.get(selectedNodeId) ?? null : null;

  const selectNode = (node: OrgGraphNode | null) => {
    setSelectedNodeId(node ? node.nodeId : null);
    onSelectNode?.(node);
  };

  const focusNodes = useMemo(
    () =>
      FOCUS_NODE_IDS.map((id) => nodeById.get(id)).filter(
        (node): node is OrgGraphNode => node !== undefined,
      ),
    [nodeById],
  );
  const prefetchNode = nodeById.get("prefetch_engine") ?? null;
  const extraNodes = useMemo(
    () =>
      derivedState.nodes.filter(
        (node) =>
          node.nodeId !== "prefetch_engine" &&
          !(FOCUS_NODE_IDS as readonly string[]).includes(node.nodeId),
      ),
    [derivedState.nodes],
  );

  const summaryCounts = useMemo(() => {
    let completed = 0;
    let failed = 0;
    let blocked = 0;
    let skipped = 0;
    for (const node of focusNodes) {
      if (node.runtimeState === "completed") completed += 1;
      else if (node.runtimeState === "failed") failed += 1;
      else if (node.runtimeState === "blocked") blocked += 1;
      else if (node.runtimeState === "skipped" || node.runtimeState === "cancelled") skipped += 1;
    }
    const totalEvents = derivedState.nodes.reduce((sum, n) => sum + n.activityLog.length, 0);
    return { total: focusNodes.length, completed, failed, blocked, skipped, totalEvents };
  }, [focusNodes, derivedState.nodes]);

  const positionedEdges = useMemo(
    () => buildFocusEdges(nodeById, derivedState.edges, isDesktop, runActive),
    [nodeById, derivedState.edges, isDesktop, runActive],
  );

  const runMeta = RUN_STATUS_META[effectiveRunStatus];
  const RunIcon = runMeta.icon;
  const anyWorkerRunning = derivedState.nodes.some((n) => n.runtimeState === "running");

  const summaryParts = [`${summaryCounts.completed}/${summaryCounts.total} 节点完成`];
  if (summaryCounts.failed > 0) summaryParts.push(`${summaryCounts.failed} 失败`);
  if (summaryCounts.blocked > 0) summaryParts.push(`${summaryCounts.blocked} 受阻`);
  if (summaryCounts.skipped > 0) summaryParts.push(`${summaryCounts.skipped} 跳过`);
  summaryParts.push(`共 ${formatInteger(summaryCounts.totalEvents)} 条过程事件`);

  const rects = isDesktop ? DESKTOP_RECTS : MOBILE_RECTS;
  const viewBox = isDesktop ? "0 0 744 312" : "0 0 360 542";

  return (
    <section
      className="chart-card org-graph-panel"
      aria-label={expanded ? title : "投研小组执行摘要条"}
      data-reduced-motion={reducedMotion ? "true" : "false"}
      data-run-status={effectiveRunStatus}
    >
      {!expanded ? (
        <div className="org-summary-bar" data-testid="org-summary-bar">
          <div className="chart-head">
            <div className="chart-title-wrap">
              <div className="chart-title-row">
                <RunIcon
                  size={16}
                  color={summaryCounts.failed > 0 ? "var(--warning)" : "var(--success)"}
                  aria-hidden="true"
                />
                <h3 className="chart-title">投研小组执行摘要条</h3>
                <span className={`chart-badge ${summaryCounts.failed > 0 ? "is-sim" : "is-updated"}`}>
                  {summaryCounts.failed > 0 ? "已完成（含局部缺口）" : runMeta.label}
                </span>
              </div>
              <p className="chart-subtitle">{summaryParts.join(" · ")}</p>
            </div>
            <div className="chart-tools">
              <button
                type="button"
                className="btn plain"
                aria-expanded={false}
                onClick={() => setExpanded(true)}
              >
                <span>展开组织图</span>
                <ChevronDown size={15} aria-hidden="true" />
              </button>
            </div>
          </div>

          <div className="pt-chips" role="group" aria-label="各角色执行结果快捷查看">
            {[...(prefetchNode ? [prefetchNode] : []), ...focusNodes].map((node) => (
              <NodeChipButton key={node.nodeId} node={node} onClick={selectNode} />
            ))}
          </div>
        </div>
      ) : (
        <>
          <div className="chart-head">
            <div className="chart-title-wrap">
              <div className="chart-title-row">
                {runActive && anyWorkerRunning && !reducedMotion ? (
                  <span className="dot running" aria-hidden="true" />
                ) : (
                  <RunIcon size={16} color={runMeta.roleClass === "support" ? "var(--success)" : runMeta.roleClass === "error" ? "var(--error)" : "var(--on-variant)"} aria-hidden="true" />
                )}
                <h3 className="chart-title">{title}</h3>
                <span className={`role light ${runMeta.roleClass}`}>{runMeta.label}</span>
                {derivedState.currentStage ? (
                  <span className="chart-badge is-updated">{stageLabel(derivedState.currentStage)}</span>
                ) : null}
              </div>
              <p className="chart-subtitle">
                Lead Coordinator → 3 并行只读专题 Worker → 独立空头审查 → 底稿撰稿 (Writer)
              </p>
            </div>

            <div className="chart-tools">
              {prefetchNode ? (
                <button
                  type="button"
                  className="chart-tool-btn"
                  onClick={() => selectNode(prefetchNode)}
                >
                  <CircleCheck size={13} color={RUNTIME_STATE_META[prefetchNode.runtimeState].accent} aria-hidden="true" />
                  <span>{`${prefetchNode.displayName} · ${RUNTIME_STATE_META[prefetchNode.runtimeState].label}`}</span>
                </button>
              ) : null}
              <button
                type="button"
                className="btn plain"
                aria-expanded={true}
                onClick={() => setExpanded(false)}
              >
                <span>收起组织图</span>
                <ChevronUp size={15} aria-hidden="true" />
              </button>
            </div>
          </div>

          <div className="cg-legend" aria-label="组织图图例">
            <span className="cg-legend-item">
              <span className="cg-swatch is-pos" aria-hidden="true" />
              派发 / 协同连线
            </span>
            <span className="cg-legend-item">
              <span className="cg-swatch is-cond" aria-hidden="true" />
              独立审查 / 复核连线 (is_review)
            </span>
            <span className="cg-legend-item">
              <span className="dot done" aria-hidden="true" />
              已完成
            </span>
            <span className="cg-legend-item">
              <span className="dot partial" aria-hidden="true" />
              受阻 / 需补充
            </span>
            <span className="cg-legend-item">
              <span className="dot failed" aria-hidden="true" />
              失败
            </span>
          </div>

          <div className="cg-scroll">
            <svg
              className="org-graph-svg"
              width="100%"
              viewBox={viewBox}
              role="group"
              aria-label={`${title} 有向组织图`}
              data-layout-mode={isDesktop ? "desktop" : "mobile"}
            >
              <title>{title}</title>
              <defs>
                <marker
                  id={`${uid}-arrow-flow`}
                  viewBox="0 0 10 10"
                  refX="8.5"
                  refY="5"
                  markerWidth="6"
                  markerHeight="6"
                  orient="auto-start-reverse"
                >
                  <path d="M 0 1.5 L 9 5 L 0 8.5 z" fill="var(--separator)" />
                </marker>
                <marker
                  id={`${uid}-arrow-flow-active`}
                  viewBox="0 0 10 10"
                  refX="8.5"
                  refY="5"
                  markerWidth="6"
                  markerHeight="6"
                  orient="auto-start-reverse"
                >
                  <path d="M 0 1.5 L 9 5 L 0 8.5 z" fill="var(--running)" />
                </marker>
                <marker
                  id={`${uid}-arrow-flow-done`}
                  viewBox="0 0 10 10"
                  refX="8.5"
                  refY="5"
                  markerWidth="6"
                  markerHeight="6"
                  orient="auto-start-reverse"
                >
                  <path d="M 0 1.5 L 9 5 L 0 8.5 z" fill="var(--done)" />
                </marker>
                <marker
                  id={`${uid}-arrow-steering`}
                  viewBox="0 0 10 10"
                  refX="8.5"
                  refY="5"
                  markerWidth="6"
                  markerHeight="6"
                  orient="auto-start-reverse"
                >
                  <path d="M 0 1.5 L 9 5 L 0 8.5 z" fill="var(--warning)" />
                </marker>
                <marker
                  id={`${uid}-arrow-review`}
                  viewBox="0 0 10 10"
                  refX="8.5"
                  refY="5"
                  markerWidth="6.5"
                  markerHeight="6.5"
                  orient="auto-start-reverse"
                >
                  <path d="M 0 1.5 L 9 5 L 0 8.5 z" fill="var(--chart-4)" />
                </marker>
                <marker
                  id={`${uid}-arrow-review-idle`}
                  viewBox="0 0 10 10"
                  refX="8.5"
                  refY="5"
                  markerWidth="6"
                  markerHeight="6"
                  orient="auto-start-reverse"
                >
                  <path d="M 0 1.5 L 9 5 L 0 8.5 z" fill="var(--on-tertiary)" />
                </marker>
              </defs>

              <g className="org-edges">
                {positionedEdges.map((edge) => {
                  const stroke = edgeStrokeColor(edge);
                  const markerId = edge.isSteering && edge.triggered
                    ? `${uid}-arrow-steering`
                    : edge.isReview
                      ? edge.triggered || edge.transmitting
                        ? `${uid}-arrow-review`
                        : `${uid}-arrow-review-idle`
                      : edge.transmitting
                        ? `${uid}-arrow-flow-active`
                        : edge.triggered
                          ? `${uid}-arrow-flow-done`
                          : `${uid}-arrow-flow`;
                  const strokeWidth = edge.transmitting ? 2.1 : edge.triggered ? 1.7 : 1.2;
                  const opacity = edge.triggered || edge.transmitting ? 0.95 : 0.48;
                  const showReviewPill =
                    edge.isReview &&
                    (edge.edgeKind === "review") &&
                    edge.labelX > 0 &&
                    (edge.triggered || edge.fromNodeId === "bear_reviewer");

                  return (
                    <g
                      key={edge.key}
                      className={`org-edge${edge.isReview ? " is-review" : " is-delegation"}${edge.transmitting ? " is-transmitting" : ""}`}
                      data-edge-key={edge.key}
                      data-edge-review={edge.isReview ? "true" : "false"}
                    >
                      <path
                        d={edge.path}
                        fill="none"
                        stroke={stroke}
                        strokeWidth={strokeWidth}
                        strokeDasharray={edge.isReview ? "6 4" : undefined}
                        opacity={opacity}
                        markerEnd={`url(#${markerId})`}
                      />
                      {!reducedMotion && edge.transmitting ? (
                        <circle
                          r={3.4}
                          fill={edge.isReview ? "var(--chart-4)" : "var(--running)"}
                          className="org-edge-particle"
                          data-testid={`edge-particle-${edge.key}`}
                        >
                          <animateMotion dur="1.8s" repeatCount="indefinite" path={edge.path} />
                        </circle>
                      ) : null}
                      {showReviewPill ? (
                        <g className="org-edge-pill">
                          <rect
                            x={edge.labelX - 38}
                            y={edge.labelY - 10}
                            width={76}
                            height={18}
                            rx={9}
                            fill="var(--surface)"
                            stroke={edge.triggered ? "var(--chart-4)" : "var(--separator)"}
                            strokeWidth={0.9}
                          />
                          <text
                            x={edge.labelX}
                            y={edge.labelY + 2.5}
                            textAnchor="middle"
                            fontFamily="var(--font)"
                            fontSize={10}
                            fontWeight={600}
                            fill={edge.triggered ? "var(--chart-4)" : "var(--on-tertiary)"}
                          >
                            {edge.fromNodeId === "lead_coordinator" ? "独立空头审查" : "审查锚定"}
                          </text>
                        </g>
                      ) : null}
                    </g>
                  );
                })}
              </g>

              <g className="org-nodes">
                {focusNodes.map((node) => {
                  const rect = rects[node.nodeId as FocusNodeId];
                  const stateMeta = RUNTIME_STATE_META[node.runtimeState];
                  const actText = actionLabel(node.actionKind);
                  const ActIcon = actionIcon(node.actionKind, stateMeta.icon);
                  const accent =
                    node.runtimeState === "idle"
                      ? stateMeta.accent
                      : actionColor(node.actionKind, stateMeta.accent);
                  const isSelected = selectedNodeId === node.nodeId;
                  const isRunning = node.runtimeState === "running";
                  const isDashed = node.runtimeState === "skipped" || node.runtimeState === "cancelled";
                  const previewRaw =
                    node.blockerText ||
                    node.currentActivity ||
                    (node.slot ? `槽位 ${node.slot}` : "等待上游派发");
                  const previewLine = truncateLine(previewRaw, isDesktop ? 21 : 36);
                  const ariaLabel = `${node.displayName}，动作：${actText}，状态：${stateMeta.label}${node.currentActivity ? `，当前：${node.currentActivity}` : ""}`;

                  return (
                    <g
                      key={node.nodeId}
                      className={`org-node state-${node.runtimeState}${isSelected ? " is-selected" : ""}`}
                      role="button"
                      tabIndex={0}
                      aria-label={ariaLabel}
                      aria-pressed={isSelected}
                      data-node-id={node.nodeId}
                      data-runtime-state={node.runtimeState}
                      data-action-kind={node.actionKind ?? "NONE"}
                      onClick={() => selectNode(node)}
                      onKeyDown={(event) => handleEnterOrSpace(event, () => selectNode(node))}
                    >
                      <title>{ariaLabel}</title>
                      <rect
                        className="org-node-card"
                        x={rect.x}
                        y={rect.y}
                        width={rect.width}
                        height={rect.height}
                        rx={12}
                        fill={stateMeta.fill}
                        stroke={isSelected ? "var(--primary)" : stateMeta.stroke}
                        strokeWidth={isSelected ? 2.2 : isRunning ? 1.8 : 1.1}
                        strokeDasharray={isDashed ? "4 3" : undefined}
                        opacity={isDashed ? 0.68 : 1}
                      />
                      <rect
                        x={rect.x + 1}
                        y={rect.y + 8}
                        width={3.5}
                        height={rect.height - 16}
                        rx={2}
                        fill={accent}
                      />
                      <ActIcon
                        x={rect.x + 11}
                        y={rect.y + 10}
                        width={13}
                        height={13}
                        color={accent}
                        strokeWidth={1.8}
                        aria-hidden="true"
                      />
                      <text
                        x={rect.x + 28}
                        y={rect.y + 20}
                        fontFamily="var(--font)"
                        fontSize={10.5}
                        fontWeight={600}
                        fill={accent}
                      >
                        {actText}
                      </text>
                      {isRunning && !reducedMotion ? (
                        <circle cx={rect.x + rect.width - 48} cy={rect.y + 16.5} r={3} fill="var(--running)">
                          <animate
                            attributeName="opacity"
                            values="1;0.35;1"
                            dur="1.6s"
                            repeatCount="indefinite"
                          />
                        </circle>
                      ) : null}
                      <text
                        x={rect.x + rect.width - 10}
                        y={rect.y + 20}
                        textAnchor="end"
                        fontFamily="var(--font)"
                        fontSize={10.5}
                        fontWeight={600}
                        fill={stateMeta.textColor}
                      >
                        {stateMeta.label}
                      </text>
                      <text
                        x={rect.x + 11}
                        y={rect.y + 42}
                        fontFamily="var(--font)"
                        fontSize={12.5}
                        fontWeight={600}
                        fill="var(--on-surface)"
                      >
                        {node.displayName}
                      </text>
                      <text
                        x={rect.x + 11}
                        y={rect.y + (rect.height >= 80 ? 63 : 60)}
                        fontFamily="var(--font)"
                        fontSize={10.5}
                        fill={node.blockerText ? "var(--warning)" : "var(--on-tertiary)"}
                      >
                        {previewLine}
                      </text>
                    </g>
                  );
                })}
              </g>
            </svg>
          </div>

          {extraNodes.length > 0 ? (
            <div className="pt-chips" role="group" aria-label="其他协同节点">
              {extraNodes.map((node) => (
                <NodeChipButton key={node.nodeId} node={node} onClick={selectNode} />
              ))}
            </div>
          ) : null}

          <div className="chart-foot">
            <span className="meta">
              点击任意角色节点可查看该节点的任务摘要与简洁过程日志
            </span>
            {derivedState.lastSequence > 0 ? (
              <span className="meta chart-foot-hint mono">{`seq #${derivedState.lastSequence}`}</span>
            ) : null}
          </div>
        </>
      )}

      {selectedNode ? (
        <NodeProcessDrawer node={selectedNode} onClose={() => selectNode(null)} />
      ) : null}
    </section>
  );
}
