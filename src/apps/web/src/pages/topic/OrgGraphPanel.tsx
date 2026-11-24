import {
  BarChart3,
  BookOpen,
  ChevronDown,
  ChevronUp,
  CircleAlert,
  CircleCheck,
  Clock,
  Compass,
  Eye,
  FileSearch,
  GitBranch,
  Layers,
  PenLine,
  Scale,
  Search,
  ShieldAlert,
  ShieldCheck,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { formatDateTime } from "../../lib/format";
import { MEDIA, useMedia } from "../../ui/media";
import { Panel } from "../../ui/Panel";
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
  readonly activeRing: boolean;
}

export const ACTION_KIND_META: Readonly<Record<OrgActionKind, ActionVisualSpec>> = {
  READING: { label: "阅读资料", color: "var(--on-surface)", roleClass: "counter", icon: BookOpen, activeRing: true },
  SEARCHING: { label: "检索线索", color: "var(--on-surface)", roleClass: "counter", icon: Search, activeRing: true },
  INSPECTING: { label: "核验分析", color: "var(--on-surface)", roleClass: "counter", icon: Eye, activeRing: true },
  DELEGATING: { label: "派发任务", color: "var(--on-surface)", roleClass: "counter", icon: GitBranch, activeRing: true },
  WAITING_SUBORDINATES: {
    label: "等待并行节点",
    color: "var(--on-variant)",
    roleClass: "neutral",
    icon: Clock,
    activeRing: false,
  },
  RESPONDING: { label: "生成交付", color: "var(--success)", roleClass: "support", icon: CircleCheck, activeRing: true },
  NEEDS_USER: { label: "需补充信息", color: "var(--warning)", roleClass: "review", icon: CircleAlert, activeRing: false },
  BLOCKED: { label: "已阻断", color: "var(--error)", roleClass: "error", icon: ShieldAlert, activeRing: false },
  FAILED: { label: "执行失败", color: "var(--error)", roleClass: "error", icon: XCircle, activeRing: false },
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
  blocked: {
    label: "受阻",
    stroke: "var(--warning)",
    fill: "var(--surface)",
    textColor: "var(--warning)",
    accent: "var(--warning)",
    roleClass: "review",
    icon: ShieldAlert,
  },
  failed: {
    label: "失败",
    stroke: "var(--error)",
    fill: "var(--surface)",
    textColor: "var(--error)",
    accent: "var(--error)",
    roleClass: "error",
    icon: XCircle,
  },
  skipped: {
    label: "已跳过",
    stroke: "var(--separator)",
    fill: "var(--s-low)",
    textColor: "var(--on-tertiary)",
    accent: "var(--on-tertiary)",
    roleClass: "neutral",
    icon: Clock,
  },
  cancelled: {
    label: "已取消",
    stroke: "var(--separator)",
    fill: "var(--s-low)",
    textColor: "var(--on-tertiary)",
    accent: "var(--on-tertiary)",
    roleClass: "neutral",
    icon: XCircle,
  },
};

type MorandiFamily = "purple" | "blue" | "green" | "grey" | "warm";

/**
 * Exact 3-stop sRGB Morandi linear-gradient palettes from
 * `ragent-app/Ragent_product` (`OrganizationNode.kt:205-212`).
 */
const MORANDI_STOPS: Readonly<Record<MorandiFamily, readonly [string, string, string]>> = {
  purple: ["#657AA5", "#6E76A6", "#7B73A1"],
  blue: ["#6487A8", "#6E84A6", "#7A80A1"],
  green: ["#64887F", "#6E887C", "#7A8A78"],
  grey: ["#6F8997", "#798696", "#838293"],
  warm: ["#958263", "#9B846C", "#A08776"],
};

interface OrbRoleVisual {
  readonly family: MorandiFamily;
  readonly icon: LucideIcon;
  readonly shortLabel: string;
  readonly radius: number;
  readonly iconSize: number;
}

const ORB_ROLE_VISUALS: Readonly<Record<string, OrbRoleVisual>> = {
  lead_coordinator: { family: "purple", icon: Compass, shortLabel: "Lead 协调", radius: 34, iconSize: 26 },
  worker_segments: { family: "blue", icon: BarChart3, shortLabel: "分部与 KPI", radius: 28, iconSize: 21 },
  worker_forensics: { family: "green", icon: ShieldCheck, shortLabel: "财务法证", radius: 28, iconSize: 21 },
  worker_guidance: { family: "grey", icon: FileSearch, shortLabel: "指引预期差", radius: 28, iconSize: 21 },
  bear_reviewer: { family: "warm", icon: Scale, shortLabel: "空头审查", radius: 28, iconSize: 21 },
  dossier_writer: { family: "blue", icon: PenLine, shortLabel: "底稿撰稿", radius: 28, iconSize: 21 },
};

const FALLBACK_ORB_VISUAL: OrbRoleVisual = {
  family: "grey",
  icon: Layers,
  shortLabel: "协作节点",
  radius: 28,
  iconSize: 21,
};

const SLOT_LABELS: Readonly<Record<OrgModelSlot, string>> = {
  fast_extractor: "fast_extractor（并行取证）",
  deep_reasoner: "deep_reasoner（规划调度）",
  bear_reviewer: "bear_reviewer（独立空头）",
  writer: "writer（底稿撰稿）",
};

const STAGE_LABELS: Readonly<Record<string, string>> = {
  stage_0_prefetch: "预热取证",
  stage_1_plan: "任务派发",
  stage_2_workers: "并行深挖",
  stage_2_5_followup: "交叉补证",
  stage_3a_bear: "空头对抗",
  stage_3b_write: "底稿生成",
  steer: "中场调度",
  complete: "已完成",
};

function orbVisualFor(node: OrgGraphNode): OrbRoleVisual {
  return ORB_ROLE_VISUALS[node.nodeId] ?? ORB_ROLE_VISUALS[node.role] ?? {
    ...FALLBACK_ORB_VISUAL,
    shortLabel: node.displayName.slice(0, 8),
  };
}

/**
 * Top-to-bottom FocusTree coordinates (`FocusTreeLayout.kt` geometry)
 * inside a clean 420x456 viewBox that fits both 390px phones and 1440px desktop.
 */
interface OrbPoint {
  readonly cx: number;
  readonly cy: number;
  readonly r: number;
}

const CANONICAL_NODE_IDS = [
  "lead_coordinator",
  "worker_segments",
  "worker_forensics",
  "worker_guidance",
  "bear_reviewer",
  "dossier_writer",
] as const;

type CanonicalNodeId = (typeof CANONICAL_NODE_IDS)[number];

const FOCUS_TREE_POINTS: Readonly<Record<CanonicalNodeId, OrbPoint>> = {
  lead_coordinator: { cx: 210, cy: 52, r: 34 },
  worker_segments: { cx: 88, cy: 170, r: 28 },
  worker_forensics: { cx: 210, cy: 170, r: 28 },
  worker_guidance: { cx: 332, cy: 170, r: 28 },
  bear_reviewer: { cx: 210, cy: 288, r: 28 },
  dossier_writer: { cx: 210, cy: 400, r: 28 },
};

const SKELETON_EDGES: ReadonlyArray<{
  readonly id: string;
  readonly from: CanonicalNodeId;
  readonly to: CanonicalNodeId;
  readonly isReview: boolean;
}> = [
  { id: "skel:lead->segments", from: "lead_coordinator", to: "worker_segments", isReview: false },
  { id: "skel:lead->forensics", from: "lead_coordinator", to: "worker_forensics", isReview: false },
  { id: "skel:lead->guidance", from: "lead_coordinator", to: "worker_guidance", isReview: false },
  { id: "skel:segments->bear", from: "worker_segments", to: "bear_reviewer", isReview: true },
  { id: "skel:forensics->bear", from: "worker_forensics", to: "bear_reviewer", isReview: true },
  { id: "skel:guidance->bear", from: "worker_guidance", to: "bear_reviewer", isReview: true },
  { id: "skel:bear->writer", from: "bear_reviewer", to: "dossier_writer", isReview: false },
];

function cubicEdgePath(from: OrbPoint, to: OrbPoint): string {
  const sx = from.cx;
  const sy = from.cy + from.r + 24;
  const ex = to.cx;
  const ey = to.cy - to.r - 4;
  const midY = (sy + ey) / 2;
  return `M ${sx} ${sy} C ${sx} ${midY}, ${ex} ${midY}, ${ex} ${ey}`;
}

function edgeParticleKind(edge: { from: string; to: string; isReview: boolean }): "task" | "result" | "review" {
  if (edge.isReview) return "review";
  if (edge.from === "lead_coordinator") return "task";
  return "result";
}

function badgeOffset(r: number): { dx: number; dy: number; badgeR: number } {
  const badgeR = 11;
  const distance = r - badgeR / 3;
  const rad = Math.PI / 4; // 45 degrees (`OrganizationNode.kt:177`)
  return {
    dx: Math.round(distance * Math.cos(rad) * 100) / 100,
    dy: Math.round(distance * Math.sin(rad) * 100) / 100,
    badgeR,
  };
}

function usePrefersReducedMotion(override?: boolean): boolean {
  const [systemReduced, setSystemReduced] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const sync = () => setSystemReduced(Boolean(mq.matches));
    sync();
    if (typeof mq.addEventListener === "function") {
      mq.addEventListener("change", sync);
      return () => mq.removeEventListener("change", sync);
    }
    return undefined;
  }, []);
  return override ?? systemReduced;
}

export function OrgGraphPanel({
  events,
  state: externalState,
  runStatus,
  title = "投研小组组织图",
  reducedMotion: reducedMotionOverride,
  defaultExpanded,
  onSelectNode,
}: OrgGraphPanelProps) {
  const uid = useId().replace(/:/g, "");
  const isTabletUp = useMedia(MEDIA.tablet);
  const reducedMotion = usePrefersReducedMotion(reducedMotionOverride);

  const graphState = useMemo<OrgGraphState>(() => {
    if (externalState) return externalState;
    if (!events || events.length === 0) return createInitialOrgGraphState();
    return reduceOrgGraphEvents(createInitialOrgGraphState(), events);
  }, [externalState, events]);

  const effectiveRunStatus: OrgRunStatus = runStatus ?? graphState.runStatus;
  const isCompleted = effectiveRunStatus === "completed";

  const [expanded, setExpanded] = useState<boolean>(() => {
    if (defaultExpanded !== undefined) return defaultExpanded;
    return !isCompleted;
  });
  const prevCompletedRef = useRef(isCompleted);
  useEffect(() => {
    if (!prevCompletedRef.current && isCompleted && defaultExpanded === undefined) {
      setExpanded(false);
    } else if (prevCompletedRef.current && !isCompleted && defaultExpanded === undefined) {
      setExpanded(true);
    }
    prevCompletedRef.current = isCompleted;
  }, [isCompleted, defaultExpanded]);

  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const nodeById = useMemo(() => {
    const map = new Map<string, OrgGraphNode>();
    for (const node of graphState.nodes) {
      map.set(node.nodeId, node);
    }
    return map;
  }, [graphState.nodes]);

  const selectedNode = useMemo<OrgGraphNode | null>(
    () => (selectedNodeId ? nodeById.get(selectedNodeId) ?? null : null),
    [nodeById, selectedNodeId],
  );

  const selectNode = (node: OrgGraphNode | null) => {
    setSelectedNodeId(node ? node.nodeId : null);
    onSelectNode?.(node);
  };

  const displayNodes = useMemo(() => {
    return CANONICAL_NODE_IDS.map((id) => nodeById.get(id)).filter((n): n is OrgGraphNode => Boolean(n));
  }, [nodeById]);

  const renderedEdges = useMemo(() => {
    const dynamicByPair = new Map<string, OrgGraphEdge>();
    for (const edge of graphState.edges) {
      dynamicByPair.set(`${edge.fromNodeId}->${edge.toNodeId}:${edge.isReview ? "r" : "d"}`, edge);
    }
    return SKELETON_EDGES.map((skel) => {
      const dyn =
        dynamicByPair.get(`${skel.from}->${skel.to}:${skel.isReview ? "r" : "d"}`) ??
        dynamicByPair.get(`${skel.from}->${skel.to}:d`) ??
        dynamicByPair.get(`${skel.from}->${skel.to}:r`);
      const fromNode = nodeById.get(skel.from);
      const toNode = nodeById.get(skel.to);
      const active = Boolean(
        dyn?.active ||
          fromNode?.runtimeState === "running" ||
          toNode?.runtimeState === "running",
      );
      return {
        id: dyn?.edgeId ?? skel.id,
        from: skel.from,
        to: skel.to,
        isReview: dyn?.isReview ?? skel.isReview,
        active,
        path: cubicEdgePath(FOCUS_TREE_POINTS[skel.from], FOCUS_TREE_POINTS[skel.to]),
      };
    });
  }, [graphState.edges, nodeById]);

  const completedCount = displayNodes.filter((n) => n.runtimeState === "completed").length;
  const activeStageLabel =
    (graphState.currentStage && STAGE_LABELS[graphState.currentStage]) ||
    (isCompleted ? "已完成" : effectiveRunStatus === "running" ? "协同执行中" : "待命");

  const handleOrbKeyDown = (e: KeyboardEvent<SVGGElement>, node: OrgGraphNode) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      selectNode(selectedNodeId === node.nodeId ? null : node);
    }
  };

  return (
    <section
      className={expanded ? "org-graph-panel" : "org-graph-panel org-ragent-collapsed"}
      aria-label={title}
      data-testid="org-graph-panel"
      data-layout-mode={isTabletUp ? "desktop" : "mobile"}
    >
      {!expanded ? (
        <div className="org-ragent-summary-bar" data-testid="org-graph-summary-bar">
          <div className="org-ragent-summary-left">
            <span className="org-ragent-dot-strip" aria-hidden="true">
              {displayNodes.map((node) => {
                const vis = orbVisualFor(node);
                const muted = node.runtimeState === "idle" || node.runtimeState === "skipped";
                return (
                  <span
                    key={node.nodeId}
                    className={`org-ragent-orb-dot is-${vis.family}${muted ? " is-muted" : ""}`}
                  />
                );
              })}
            </span>
            <span className="org-ragent-summary-title">投研小组执行摘要条</span>
            <span className="org-ragent-summary-meta">
              {completedCount}/{displayNodes.length} 已完成
            </span>
          </div>
          <button
            type="button"
            className="org-ragent-toggle-btn"
            onClick={() => setExpanded(true)}
            aria-expanded="false"
          >
            <span>展开组织图</span>
            <ChevronDown size={14} aria-hidden="true" />
          </button>
        </div>
      ) : (
        <>
          <header className="org-ragent-head">
            <div className="org-ragent-head-left">
              <h3 className="org-ragent-title">{title}</h3>
              <span className="org-ragent-stage-pill">{activeStageLabel}</span>
            </div>
            <button
              type="button"
              className="org-ragent-toggle-btn"
              onClick={() => setExpanded(false)}
              aria-expanded="true"
            >
              <span>收起</span>
              <ChevronUp size={14} aria-hidden="true" />
            </button>
          </header>

          <div className="org-ragent-canvas-wrap">
            <svg
              className="org-ragent-svg"
              viewBox="0 0 420 456"
              role="img"
              aria-label={`${title}（${activeStageLabel}）`}
              data-testid="org-graph-svg"
            >
              <defs>
                {(Object.keys(MORANDI_STOPS) as MorandiFamily[]).map((fam) => {
                  const [c0, c1, c2] = MORANDI_STOPS[fam];
                  return (
                    <linearGradient
                      key={fam}
                      id={`orb-${fam}-${uid}`}
                      x1="12%"
                      y1="8%"
                      x2="88%"
                      y2="92%"
                    >
                      <stop offset="0%" stopColor={c0} />
                      <stop offset="55%" stopColor={c1} />
                      <stop offset="100%" stopColor={c2} />
                    </linearGradient>
                  );
                })}
                <filter id={`badge-shadow-${uid}`} x="-35%" y="-35%" width="170%" height="170%">
                  <feDropShadow dx="0" dy="2" stdDeviation="2.5" floodColor="#0F172A" floodOpacity="0.14" />
                </filter>
              </defs>

              {/* Ultra-thin cubic Bézier edges + semantic glowing particles (`OrganizationCanvas.kt`) */}
              <g className="org-ragent-edges">
                {renderedEdges.map((edge) => {
                  const pKind = edgeParticleKind(edge);
                  const pathId = `edge-${edge.id.replace(/[^a-zA-Z0-9_-]/g, "_")}-${uid}`;
                  return (
                    <g key={edge.id} data-edge-id={edge.id} data-review={edge.isReview ? "true" : "false"}>
                      <path
                        id={pathId}
                        d={edge.path}
                        className={`org-ragent-edge${edge.isReview ? " is-review" : ""}${edge.active ? " is-active" : ""}`}
                      />
                      {edge.active && !reducedMotion && (
                        <g data-particle-kind={pKind} aria-hidden="true">
                          {pKind === "task" && (
                            <>
                              <circle r="6.5" fill="#3B82F6" fillOpacity="0.30">
                                <animateMotion dur="2.1s" repeatCount="indefinite">
                                  <mpath href={`#${pathId}`} />
                                </animateMotion>
                              </circle>
                              <circle r="3.2" fill="#3B82F6">
                                <animateMotion dur="2.1s" repeatCount="indefinite">
                                  <mpath href={`#${pathId}`} />
                                </animateMotion>
                              </circle>
                            </>
                          )}
                          {pKind === "result" && (
                            <>
                              <circle r="7" fill="#10B981" fillOpacity="0.28">
                                <animateMotion dur="2.3s" repeatCount="indefinite">
                                  <mpath href={`#${pathId}`} />
                                </animateMotion>
                              </circle>
                              <polygon points="0,-4.5 4.5,0 0,4.5 -4.5,0" fill="#10B981">
                                <animateMotion dur="2.3s" repeatCount="indefinite">
                                  <mpath href={`#${pathId}`} />
                                </animateMotion>
                              </polygon>
                            </>
                          )}
                          {pKind === "review" && (
                            <>
                              <circle r="7" fill="#B47B38" fillOpacity="0.28">
                                <animateMotion dur="2.4s" repeatCount="indefinite">
                                  <mpath href={`#${pathId}`} />
                                </animateMotion>
                              </circle>
                              <polygon points="0,-4.5 4.5,0 0,4.5 -4.5,0" fill="#B47B38">
                                <animateMotion dur="2.4s" repeatCount="indefinite">
                                  <mpath href={`#${pathId}`} />
                                </animateMotion>
                              </polygon>
                            </>
                          )}
                        </g>
                      )}
                    </g>
                  );
                })}
              </g>

              {/* Circular Morandi Gradient Orbs + 45° Frosted Action Badges (`OrganizationNode.kt`) */}
              <g className="org-ragent-nodes">
                {displayNodes.map((node) => {
                  const pt = FOCUS_TREE_POINTS[node.nodeId as CanonicalNodeId];
                  if (!pt) return null;
                  const vis = orbVisualFor(node);
                  const OrbIcon = vis.icon;
                  const runtimeMeta = RUNTIME_STATE_META[node.runtimeState];
                  const actionMeta = node.actionKind ? ACTION_KIND_META[node.actionKind] : null;
                  const ActionIcon = actionMeta?.icon ?? null;
                  const { dx, dy, badgeR } = badgeOffset(pt.r);
                  const bx = pt.cx + dx;
                  const by = pt.cy + dy;
                  const isSelected = selectedNodeId === node.nodeId;
                  const showArc = Boolean(
                    actionMeta?.activeRing && node.runtimeState === "running" && !reducedMotion,
                  );
                  const stateClass =
                    node.runtimeState === "idle"
                      ? " is-idle"
                      : node.runtimeState === "skipped"
                        ? " is-skipped"
                        : "";
                  const a11yTitle = [
                    node.displayName,
                    actionMeta ? `动作：${actionMeta.label}` : null,
                    `状态：${runtimeMeta.label}`,
                    node.currentActivity ? `当前：${node.currentActivity}` : null,
                  ]
                    .filter(Boolean)
                    .join("，");

                  return (
                    <g
                      key={node.nodeId}
                      className={`org-ragent-node-group${stateClass}${isSelected ? " is-selected" : ""}`}
                      role="button"
                      tabIndex={0}
                      aria-label={a11yTitle}
                      aria-pressed={isSelected}
                      data-node-id={node.nodeId}
                      data-runtime-state={node.runtimeState}
                      data-action-kind={node.actionKind ?? ""}
                      onClick={() => selectNode(isSelected ? null : node)}
                      onKeyDown={(e) => handleOrbKeyDown(e, node)}
                    >
                      <title>{a11yTitle}</title>
                      <circle cx={pt.cx} cy={pt.cy} r={pt.r + 5} className="org-ragent-focus-ring" />
                      <circle cx={pt.cx} cy={pt.cy} r={pt.r} fill={`url(#orb-${vis.family}-${uid})`} />
                      <g
                        transform={`translate(${pt.cx - vis.iconSize / 2}, ${pt.cy - vis.iconSize / 2})`}
                        aria-hidden="true"
                      >
                        <OrbIcon size={vis.iconSize} color="#FFFFFF" strokeWidth={1.85} />
                      </g>

                      {actionMeta && ActionIcon && (
                        <g
                          transform={`translate(${bx}, ${by})`}
                          filter={`url(#badge-shadow-${uid})`}
                          data-testid={`action-badge-${node.nodeId}`}
                          aria-hidden="true"
                        >
                          <circle cx={0} cy={0} r={badgeR} className="org-ragent-badge-bg" />
                          {showArc && (
                            <path
                              d={`M 0 ${-(badgeR - 1.5)} A ${badgeR - 1.5} ${badgeR - 1.5} 0 0 1 ${badgeR - 1.5} 0`}
                              className="org-ragent-badge-arc"
                            />
                          )}
                          <g transform="translate(-6, -6)">
                            <ActionIcon size={12} color={actionMeta.color} strokeWidth={2} />
                          </g>
                        </g>
                      )}

                      <text x={pt.cx} y={pt.cy + pt.r + 18} className="org-ragent-node-label">
                        {vis.shortLabel}
                      </text>
                    </g>
                  );
                })}
              </g>
            </svg>
          </div>

          <footer className="org-ragent-foot">
            <span>点击圆球查看节点过程日志</span>
            {graphState.lastSequence > 0 && <span>seq #{graphState.lastSequence}</span>}
          </footer>
        </>
      )}

      {selectedNode && (
        <Panel
          title={selectedNode.displayName}
          onClose={() => selectNode(null)}
        >
          <div className="org-ragent-drawer-body" data-testid="org-node-drawer">
            <div className="org-ragent-drawer-meta">
              <span>{RUNTIME_STATE_META[selectedNode.runtimeState].label}</span>
              {selectedNode.actionKind && <span>· {ACTION_KIND_META[selectedNode.actionKind].label}</span>}
              {selectedNode.slot && <span>· {SLOT_LABELS[selectedNode.slot]}</span>}
            </div>

            {selectedNode.currentTaskSummary && (
              <div className="org-ragent-drawer-section">
                <span className="org-ragent-drawer-kicker">当前任务摘要</span>
                <p className="org-ragent-drawer-text">{selectedNode.currentTaskSummary}</p>
              </div>
            )}

            {selectedNode.currentActivity && (
              <div className="org-ragent-drawer-section">
                <span className="org-ragent-drawer-kicker">实时动作</span>
                <p className="org-ragent-drawer-text">{selectedNode.currentActivity}</p>
              </div>
            )}

            {selectedNode.blockerText && (
              <div className="org-ragent-drawer-blocker" role="alert">
                {selectedNode.blockerText}
              </div>
            )}

            <div className="org-ragent-drawer-section">
              <span className="org-ragent-drawer-kicker">
                简洁过程日志（{selectedNode.activityLog.length} 条）
              </span>
              {selectedNode.activityLog.length === 0 ? (
                <p className="org-ragent-drawer-text">暂无过程日志。</p>
              ) : (
                <ol className="org-ragent-log-list">
                  {[...selectedNode.activityLog].reverse().map((entry) => (
                    <li key={entry.sequence} className="org-ragent-log-item">
                      <div className="org-ragent-log-head">
                        <span>
                          {entry.actionKind ? ACTION_KIND_META[entry.actionKind].label : "状态更新"}
                        </span>
                        <span className="org-ragent-log-time">
                          {entry.timestamp ? formatDateTime(entry.timestamp) : `#${entry.sequence}`}
                        </span>
                      </div>
                      <p className="org-ragent-drawer-text">{entry.text}</p>
                    </li>
                  ))}
                </ol>
              )}
            </div>
          </div>
        </Panel>
      )}
    </section>
  );
}
