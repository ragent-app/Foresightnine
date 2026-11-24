import type { ResearchV2 } from "@foresightnine/contracts";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { Link, useBlocker, useNavigate, useParams } from "react-router-dom";
import {
  getEarningsDossier,
  renderDeepReport,
  runEarningsSquad,
  steerEarningsSquad,
  subscribeEarningsSquadEvents,
} from "../api/earningsSquad";
import { ApiError } from "../api/http";
import { readSession } from "../auth/session";
import { useActiveRun, useAttachSources, useCancelRun, useContinueTurn, useRecoverTurn, useRecoveryStatus, useReextractPdf, useRunProgress, useSaveVersion, useStartTurn, useTopic, useTopicActions, useTurnStatuses, useVersionSources } from "../api/queries";
import { acceptFindings, exportFull, exportMetadata, isActiveStatus, latestVersion, restoreVersion } from "../api/research";
import type { AvailableVersionSource, CreatedSource, ResearchInstruction } from "../api/types";
import { gapText } from "../lib/copy";
import { saveJson } from "../lib/download";
import { displayTurnNumber, formatDateTimeWithOffset } from "../lib/format";
import { buildCitationIndex, claimRole, reviewEditsAfter, suggestionsFor, topicTitle, versionWithTurn, type AnswerTab } from "../lib/research";
import { buildVersionInput, draftProblem, isDirty, type ReviewDraft } from "../lib/review";
import { ineligibility, INELIGIBLE_TEXT } from "../lib/sources";
import { useLayout } from "../ui/AppLayout";
import { Composer, type ComposerHandle, type ComposerToken } from "../ui/Composer";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { Icon } from "../ui/icons";
import { MEDIA, prefersReducedMotion, useMedia, useScrolledPast } from "../ui/media";
import { Menu } from "../ui/Menu";
import { NetworkBanner, Toolbar } from "../ui/Toolbar";
import { AddSourcesPanel } from "./research/AddSourcesPanel";
import { describeError } from "./research/errors";
import { DEFAULT_OPTIONS, optionsValid, runRequestFields, type RunOptions } from "./research/options";
import { optionTokens } from "./research/optionTokens";
import { ResearchSettingsPanel } from "./research/ResearchSettingsPanel";
import { CitationSheet } from "./topic/Citation";
import { TopicContext, type FocusCitation, type LiveRun, type PanelState, type TopicView } from "./topic/context";
import { InterruptedTurn, LiveTurn, TopicConclusion, TurnView, type AnswerTools, type TurnActions } from "./topic/Conversation";
import { useQueryClient } from "@tanstack/react-query";
import { queryKeys, useTopics } from "../api/queries";
import { appendInstruction, continueTurn as continueWaitingTurn, stopResearchTurn } from "../api/writes";
import { showToast } from "../ui/toast";
import { LiveCapsule } from "./topic/LiveBar";
import { EarningsDossierView, type DossierReportMode } from "./topic/EarningsDossierView";
import { OrgGraphPanel } from "./topic/OrgGraphPanel";
import { createInitialOrgGraphState, orgGraphReducer, type OrgRunStatus } from "./topic/orgGraphReducer";
import { ReaderPanel } from "./topic/ReaderPanel";
import { RecordPanel } from "./topic/RecordPanel";
import { ReviewPanel, saveErrorText } from "./topic/ReviewPanel";

/** The large title edited in place: Enter or leaving saves, Esc cancels; clearing restores the question. */
function TitleEditor({ initial, onDone }: { initial: string; onDone: (title: string | null) => void }) {
  const [value, setValue] = useState(initial);
  const done = useRef(false);
  const finish = (title: string | null) => {
    if (done.current) return;
    done.current = true;
    onDone(title);
  };
  return (
    <input
      className="title-edit"
      aria-label="研究名称"
      value={value}
      maxLength={200}
      autoFocus
      onFocus={(event) => event.currentTarget.select()}
      onChange={(event) => setValue(event.target.value)}
      onKeyDown={(event) => {
        if (event.key === "Enter" && !event.nativeEvent.isComposing) { event.preventDefault(); finish(value); }
        if (event.key === "Escape") { event.preventDefault(); finish(null); }
      }}
      onBlur={() => finish(value)}
    />
  );
}

interface PendingAsk {
  question: string;
  at: string;
  turnsAt: number;
  operationId: string;
}

interface PendingContinue {
  mode: "continue" | "deliver" | "regenerate";
  targetTurnId: string | null;
  operationId: string | null;
  question: string;
}

function flash(element: Element): void {
  element.classList.add("flash");
  setTimeout(() => element.classList.remove("flash"), 900);
}

const SQUAD_STATUS_META: Readonly<Record<OrgRunStatus, { readonly label: string; readonly tone: "neutral" | "counter" | "support" | "error" }>> = {
  idle: { label: "未启动", tone: "neutral" },
  running: { label: "运行中", tone: "counter" },
  completed: { label: "已完成", tone: "support" },
  failed: { label: "执行失败", tone: "error" },
  cancelled: { label: "已取消", tone: "neutral" },
};

function createSquadIdempotencyKey(prefix: string): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `${prefix}:${crypto.randomUUID()}`;
  }
  return `${prefix}:${Date.now()}:${Math.random().toString(36).slice(2, 10)}`;
}

function parseInspectLocation(location?: string | null): readonly [number, number] | null {
  if (!location) return null;
  const match = /^(?:chars?:?\s*)?(\d+)\s*[-–:]\s*(\d+)$/i.exec(location.trim());
  if (!match) return null;
  const start = Number(match[1]);
  const end = Number(match[2]);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null;
  return [start, end];
}

export function TopicPage() {
  const { topicId = "" } = useParams();
  const topicIdRef = useRef(topicId);
  topicIdRef.current = topicId;
  const draftKey = `research-draft:${readSession()?.account.account_id ?? "anonymous"}:${topicId}`;
  const topicQuery = useTopic(topicId);
  const runs = useActiveRun(topicId);
  const turnStatuses = useTurnStatuses();
  const inspector = useMedia(MEDIA.inspector);
  const { setPanelOpen } = useLayout();
  const scrollRef = useRef<HTMLDivElement>(null);
  const liveTurnRef = useRef<HTMLElement>(null);
  const nearBottomRef = useRef(true);
  const lastScrollTopRef = useRef(0);
  const suppressScrollUntilRef = useRef(0);
  const composerRef = useRef<ComposerHandle>(null);
  const scrolled = useScrolledPast(scrollRef);

  const [panel, setPanel] = useState<PanelState>(null);
  const [text, setText] = useState("");
  const [instructionBusy, setInstructionBusy] = useState(false);
  const [instructionError, setInstructionError] = useState<string | null>(null);
  const [receipts, setReceipts] = useState<ResearchInstruction[]>([]);
  const [stoppingTurn, setStoppingTurn] = useState(false);
  const [closedWaitingTurn, setClosedWaitingTurn] = useState<string | null>(null);
  const [focus, setFocus] = useState<FocusCitation | null>(null);
  const [options, setOptions] = useState<RunOptions>(DEFAULT_OPTIONS);
  const [pendingAsk, setPendingAsk] = useState<PendingAsk | null>(null);
  const [askedOp, setAskedOp] = useState<{ operationId: string; question: string } | null>(null);
  const [pendingContinue, setPendingContinue] = useState<PendingContinue | null>(null);
  const [awayFromLive, setAwayFromLive] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [editingTurnId, setEditingTurnId] = useState<string | null>(null);
  const [regenPrompt, setRegenPrompt] = useState<{ turn: ResearchV2.ResearchTurn; question: string | null } | null>(null);
  const [restoring, setRestoring] = useState(false);
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const topicList = useTopics();
  const topicActions = useTopicActions();
  const [evidence, setEvidenceState] = useState<ReadonlyMap<string, boolean>>(new Map());
  const setEvidence = useCallback((turnId: string, open: boolean) => {
    setEvidenceState((previous) => new Map(previous).set(turnId, open));
  }, []);
  const [notice, setNotice] = useState<{ text: string; tone: "info" | "error" } | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const [draft, setDraftState] = useState<ReviewDraft>({});
  // Asked before sending while review changes are unsaved (spec §4.3).
  const [sendPrompt, setSendPrompt] = useState<{ error: string | null } | null>(null);

  const [squadStatus, setSquadStatus] = useState<OrgRunStatus>("idle");
  const [squadRunId, setSquadRunId] = useState<string | null>(null);
  const squadRunIdRef = useRef<string | null>(null);
  squadRunIdRef.current = squadRunId;
  const [squadDossier, setSquadDossier] = useState<ResearchV2.EarningsDossier | null>(null);
  const [squadSlotRouting, setSquadSlotRouting] = useState<Readonly<Record<string, unknown>> | null>(null);
  const [squadSteeringHistory, setSquadSteeringHistory] = useState<readonly ResearchV2.UserSteeringInstruction[]>([]);
  const [squadError, setSquadError] = useState<string | null>(null);
  const [squadLaunching, setSquadLaunching] = useState(false);
  const [squadSymbol, setSquadSymbol] = useState("");
  const [steerInput, setSteerInput] = useState("");
  const [steerBusy, setSteerBusy] = useState(false);
  const [steerError, setSteerError] = useState<string | null>(null);
  const [reportMode, setReportMode] = useState<DossierReportMode>("memo");
  const [deepReportLoading, setDeepReportLoading] = useState(false);
  const [deepReportError, setDeepReportError] = useState<string | null>(null);
  const [orgGraphState, dispatchOrgGraph] = useReducer(orgGraphReducer, undefined, createInitialOrgGraphState);
  const [orgDrawerOpen, setOrgDrawerOpen] = useState(false);
  const orgGraphLastSeqRef = useRef(0);

  const startTurn = useStartTurn(topicId);
  const cancel = useCancelRun(topicId);
  const continueTurn = useContinueTurn(topicId);
  const recoverTurn = useRecoverTurn(topicId);
  const attach = useAttachSources(topicId);
  const reextract = useReextractPdf();
  const saveVersion = useSaveVersion(topicId);

  const topic = topicQuery.data;
  const version = topic ? latestVersion(topic) : null;
  const content: ResearchV2.ResearchContent = version?.content ?? {};
  const turns = content.turns ?? [];
  const lastTurn = turns[turns.length - 1] ?? null;
  const latestRun = runs.latestRun;
  const progress = useRunProgress(topicId, runs.activeRun?.operation_id ?? latestRun?.operation_id ?? null);
  const batchStatus = turnStatuses.data?.find((item) => item.topic_id === topicId);
  const awaitingUser = progress.progress?.logical_status === "awaiting_user" && closedWaitingTurn !== progress.progress.operation_id;
  const logicalRunning = progress.progress?.logical_status === "running" || (!progress.progress && batchStatus?.logical_status === "running");
  const terminalLogical = progress.progress?.logical_status && progress.progress.logical_status !== "running";
  const activeRun = terminalLogical ? null : runs.activeRun ?? (logicalRunning ? latestRun : null);
  const orphanRun =
    latestRun && ((!isActiveStatus(latestRun.status) && (latestRun.status === "failed" || latestRun.status === "cancelled")) || progress.progress?.logical_status === "failed" || progress.progress?.logical_status === "stopped")
      && topic && !turns.some((turn) => turn.operation_ids.includes(latestRun.operation_id))
      ? latestRun
      : null;
  const progressMatches = (operationId: string | undefined) => progress.progress?.operation_id === operationId;

  // The first turn names the topic a few seconds after it starts: pick the title up as soon as the
  // progress reports it, and otherwise look again every few seconds while that turn runs.
  const awaitingTitle = Boolean(topic && !topic.title?.trim() && activeRun && turns.length === 0);
  const reportedTitle = progress.progress?.topic_title?.trim() || null;
  useEffect(() => {
    if (!awaitingTitle) return undefined;
    const refresh = () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.topic(topicId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.topics });
    };
    if (reportedTitle) { refresh(); return undefined; }
    const timer = window.setInterval(refresh, 5000);
    return () => window.clearInterval(timer);
  }, [awaitingTitle, reportedTitle, queryClient, topicId]);
  const recoveryOperation = activeRun
    ? null
    : orphanRun?.operation_id ?? (lastTurn?.status === "partial" ? lastTurn.operation_ids[lastTurn.operation_ids.length - 1] ?? null : null);
  const recovery = useRecoveryStatus(topicId, recoveryOperation);
  const sourcesQuery = useVersionSources(topicId, version?.version_id ?? null);

  useEffect(() => {
    setPanelOpen((panel !== null && panel.kind !== "cite") || orgDrawerOpen);
    return () => setPanelOpen(false);
  }, [panel, orgDrawerOpen, setPanelOpen]);

  // Reset per-topic state when navigating between topics.
  useEffect(() => {
    setPanel(null); setOrgDrawerOpen(false); setText((() => { try { return sessionStorage.getItem(draftKey) ?? ""; } catch { return ""; } })()); setReceipts([]); setInstructionBusy(false); setInstructionError(null); setClosedWaitingTurn(null); setFocus(null); setPendingAsk(null); setAskedOp(null); setPendingContinue(null); setAwayFromLive(false);
    nearBottomRef.current = true; lastScrollTopRef.current = 0; suppressScrollUntilRef.current = 0;
    setNotice(null); setOptions(DEFAULT_OPTIONS); setDraftState({}); setSendPrompt(null);
    squadRunIdRef.current = null;
    orgGraphLastSeqRef.current = 0;
    dispatchOrgGraph({ type: "reset" });
    setSquadStatus("idle");
    setSquadRunId(null);
    setSquadDossier(null);
    setSquadSlotRouting(null);
    setSquadSteeringHistory([]);
    setSquadError(null);
    setSquadLaunching(false);
    setSquadSymbol("");
    setSteerInput("");
    setSteerBusy(false);
    setSteerError(null);
    setReportMode("memo");
    setDeepReportLoading(false);
    setDeepReportError(null);
  }, [topicId, draftKey]);

  const applySquadResponse = useCallback(
    (response: ResearchV2.EarningsSquadDossierResponse | ResearchV2.EarningsSquadRunResponse) => {
      if (topicIdRef.current !== response.topic_id) return;
      const previousRunId = squadRunIdRef.current;
      const switchedRun = Boolean(previousRunId && previousRunId !== response.run_id);
      if (switchedRun) {
        orgGraphLastSeqRef.current = 0;
        dispatchOrgGraph({ type: "reset" });
        setOrgDrawerOpen(false);
        setSquadSteeringHistory([]);
        if (!response.dossier) setSquadDossier(null);
      }
      squadRunIdRef.current = response.run_id;
      setSquadRunId(response.run_id);

      const events = response.org_events ?? [];
      if (events.length > 0) {
        let maxSeq = orgGraphLastSeqRef.current;
        for (const ev of events) {
          if (ev.sequence > maxSeq) maxSeq = ev.sequence;
        }
        orgGraphLastSeqRef.current = maxSeq;
        dispatchOrgGraph({ type: "batch", events });
      }
      if (response.status === "completed" || response.status === "failed" || response.status === "cancelled") {
        dispatchOrgGraph({ type: "run_terminal", status: response.status });
      }

      setSquadStatus(response.status);
      if (response.dossier !== undefined) {
        setSquadDossier(response.dossier ?? null);
      }
      if (response.slot_routing_summary !== undefined) {
        setSquadSlotRouting(response.slot_routing_summary ?? null);
      }
      if ("steering_instructions" in response && response.steering_instructions !== undefined) {
        setSquadSteeringHistory(response.steering_instructions);
      }
      setSquadError(response.error ?? null);
    },
    [],
  );

  // Hydrate existing earnings squad run/dossier for the topic (404 means none exists yet).
  useEffect(() => {
    if (!topicId) return undefined;
    const controller = new AbortController();
    void getEarningsDossier(topicId, controller.signal)
      .then((response) => {
        if (controller.signal.aborted || topicIdRef.current !== topicId) return;
        applySquadResponse(response);
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || topicIdRef.current !== topicId) return;
        if (error instanceof ApiError && error.status === 404) return;
        setSquadError(describeError(error));
      });
    return () => controller.abort();
  }, [topicId, applySquadResponse]);

  // Subscribe to live org_graph_event SSE stream while squad run is active, falling back to polling on error/EOF.
  useEffect(() => {
    if (!topicId || squadStatus !== "running") return undefined;
    let cancelled = false;
    let pollTimer: number | null = null;
    const controller = new AbortController();

    const clearPoll = () => {
      if (pollTimer !== null) {
        window.clearTimeout(pollTimer);
        pollTimer = null;
      }
    };

    const syncDossierState = async () => {
      try {
        const snapshot = await getEarningsDossier(topicId, controller.signal);
        if (cancelled || controller.signal.aborted || topicIdRef.current !== topicId) return;
        applySquadResponse(snapshot);
        if (snapshot.status === "running") {
          clearPoll();
          pollTimer = window.setTimeout(() => void syncDossierState(), 2000);
        }
      } catch (error: unknown) {
        if (cancelled || controller.signal.aborted || topicIdRef.current !== topicId) return;
        if (error instanceof ApiError && error.status === 404) {
          clearPoll();
          pollTimer = window.setTimeout(() => void syncDossierState(), 2000);
          return;
        }
        setSquadError(describeError(error));
      }
    };

    const startPolling = (delayMs = 250) => {
      if (cancelled || pollTimer !== null) return;
      pollTimer = window.setTimeout(() => {
        pollTimer = null;
        void syncDossierState();
      }, delayMs);
    };

    const unsubscribe = subscribeEarningsSquadEvents({
      topicId,
      after: orgGraphLastSeqRef.current,
      signal: controller.signal,
      onEvent: (event) => {
        if (cancelled || topicIdRef.current !== topicId) return;
        if (event.sequence > orgGraphLastSeqRef.current) {
          orgGraphLastSeqRef.current = event.sequence;
        }
        dispatchOrgGraph({ type: "event", event });
      },
      onTerminal: (terminal) => {
        if (cancelled || topicIdRef.current !== topicId) return;
        dispatchOrgGraph({ type: "run_terminal", status: terminal.operation_status });
        void syncDossierState();
      },
      onError: () => {
        if (cancelled || topicIdRef.current !== topicId) return;
        unsubscribe();
        startPolling(250);
      },
      onClose: () => {
        if (cancelled || topicIdRef.current !== topicId) return;
        startPolling(250);
      },
    });

    return () => {
      cancelled = true;
      clearPoll();
      unsubscribe();
      controller.abort();
    };
  }, [topicId, squadStatus, squadRunId, applySquadResponse]);

  const handleLaunchSquad = useCallback(async () => {
    if (!topicId || squadLaunching || squadStatus === "running") return;
    setSquadLaunching(true);
    setSquadError(null);
    setSteerError(null);
    setDeepReportError(null);
    setReportMode("memo");
    const trimmedSymbol = squadSymbol.trim().toUpperCase();
    const topicQuestion = topicQuery.data?.request.question?.trim() ?? "";
    try {
      const response = await runEarningsSquad(
        topicId,
        {
          execution_mode: "async",
          ...(trimmedSymbol ? { symbol: trimmedSymbol } : {}),
          ...(topicQuestion ? { question: topicQuestion } : {}),
        },
        { idempotencyKey: createSquadIdempotencyKey("squad-run") },
      );
      if (topicIdRef.current !== topicId) return;
      applySquadResponse(response);
    } catch (error: unknown) {
      if (topicIdRef.current !== topicId) return;
      setSquadError(describeError(error));
    } finally {
      if (topicIdRef.current === topicId) setSquadLaunching(false);
    }
  }, [topicId, squadLaunching, squadStatus, squadSymbol, topicQuery.data?.request.question, applySquadResponse]);

  const handleSteerSquad = useCallback(async () => {
    const trimmed = steerInput.trim();
    if (!topicId || !trimmed || steerBusy) return;
    setSteerBusy(true);
    setSteerError(null);
    try {
      const response = await steerEarningsSquad(
        topicId,
        trimmed,
        { idempotencyKey: createSquadIdempotencyKey("squad-steer") },
      );
      if (topicIdRef.current !== topicId) return;
      setSquadSteeringHistory((previous) => [
        ...previous.filter((item) => item.sequence !== response.instruction.sequence),
        response.instruction,
      ]);
      setSteerInput("");
    } catch (error: unknown) {
      if (topicIdRef.current !== topicId) return;
      setSteerError(describeError(error));
    } finally {
      if (topicIdRef.current === topicId) setSteerBusy(false);
    }
  }, [topicId, steerInput, steerBusy]);

  const handleRequestDeepReport = useCallback(async () => {
    if (!topicId) return null;
    setDeepReportLoading(true);
    setDeepReportError(null);
    try {
      const response = await renderDeepReport(
        topicId,
        { force: false },
        { idempotencyKey: createSquadIdempotencyKey("squad-deep") },
      );
      if (topicIdRef.current !== topicId) return null;
      setSquadDossier(response.dossier);
      return response;
    } catch (error: unknown) {
      if (topicIdRef.current !== topicId) return null;
      setDeepReportError(describeError(error));
      return null;
    } finally {
      if (topicIdRef.current === topicId) setDeepReportLoading(false);
    }
  }, [topicId]);

  const handleInspectSource = useCallback((sourceId: string, location?: string | null) => {
    if (!sourceId) return;
    const range = parseInspectLocation(location);
    setPanel({ kind: "reader", sourceId, range });
  }, []);

  const updateText = (value: string) => {
    setText(value);
    try { if (value) sessionStorage.setItem(draftKey, value); else sessionStorage.removeItem(draftKey); } catch { /* storage may be unavailable */ }
  };

  const dirty = version ? isDirty(version, draft) : false;
  const setDraft = useCallback((update: (current: ReviewDraft) => ReviewDraft) => setDraftState(update), []);
  const notify = useCallback((text: string, tone: "info" | "error" = "info") => setNotice({ text, tone }), []);

  /** Applies the draft to the latest version and saves it as a new one; resolves with its id. */
  const saveDraft = useCallback(async (): Promise<string> => {
    const latest = topicQuery.data ? latestVersion(topicQuery.data) : null;
    if (!latest) throw new Error("topic_not_loaded");
    const problem = draftProblem(latest, draft);
    if (problem) throw new Error(problem);
    const saved = await saveVersion.mutateAsync(buildVersionInput(latest, draft));
    setDraftState({});
    return latestVersion(saved).version_id;
  }, [topicQuery.data, draft, saveVersion]);

  // Leaving with unsaved review changes asks first (spec §7); closing the tab uses the browser prompt.
  const blocker = useBlocker(({ currentLocation, nextLocation }) => dirty && currentLocation.pathname !== nextLocation.pathname);
  const [leaveError, setLeaveError] = useState<string | null>(null);
  useEffect(() => {
    if (!dirty) return undefined;
    const handler = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);

  // Pending question bubble: hide once the started operation becomes tracked, stops, or commits.
  useEffect(() => {
    if (!pendingAsk) return;
    if (
      activeRun?.operation_id === pendingAsk.operationId
      || orphanRun?.operation_id === pendingAsk.operationId
      || turns.some((turn) => turn.operation_ids.includes(pendingAsk.operationId))
      || turns.length > pendingAsk.turnsAt
    ) {
      setPendingAsk(null);
    }
  }, [pendingAsk, activeRun, orphanRun, turns]);

  useEffect(() => {
    if (!pendingContinue || pendingContinue.operationId === null) return;
    const opId = pendingContinue.operationId;
    if (
      turns.some((turn) => turn.operation_ids.includes(opId))
      || orphanRun?.operation_id === opId
      || (activeRun !== null && activeRun.operation_id !== opId)
      || (!activeRun && latestRun?.operation_id === opId && !isActiveStatus(latestRun.status))
    ) {
      setPendingContinue(null);
    }
  }, [pendingContinue, activeRun, latestRun, orphanRun, turns]);

  useEffect(() => {
    if (!askedOp) return;
    const opId = askedOp.operationId;
    if (
      turns.some((turn) => turn.operation_ids.includes(opId))
      || orphanRun?.operation_id === opId
      || (activeRun !== null && activeRun.operation_id !== opId)
      || (!activeRun && latestRun?.operation_id === opId && !isActiveStatus(latestRun.status))
    ) {
      setAskedOp(null);
    }
  }, [askedOp, activeRun, latestRun, orphanRun, turns]);

  // Screen readers hear state changes only (spec §4.3).
  const previousActive = useRef<string | null>(null);
  useEffect(() => {
    const id = activeRun?.operation_id ?? null;
    if (id && id !== previousActive.current) setAnnouncement("研究开始");
    if (!id && previousActive.current) setAnnouncement(orphanRun ? "研究中断" : "本轮研究已结束");
    previousActive.current = id;
  }, [activeRun, orphanRun]);

  useEffect(() => {
    if (!notice) return undefined;
    const timer = setTimeout(() => setNotice(null), 6000);
    return () => clearTimeout(timer);
  }, [notice]);

  // Open at the latest turn when a topic with history loads.
  const initialScrollDone = useRef<string | null>(null);
  useEffect(() => {
    if (!topic || initialScrollDone.current === topicId) return;
    initialScrollDone.current = topicId;
    if (turns.length > 1) {
      requestAnimationFrame(() => {
        const all = scrollRef.current?.querySelectorAll(".turn");
        all?.[all.length - 1]?.scrollIntoView?.({ block: "start" });
      });
    }
  }, [topic, topicId, turns.length]);

  const sources = useMemo(() => {
    const map = new Map<string, AvailableVersionSource>();
    for (const row of sourcesQuery.data ?? []) if (row.status === "available") map.set(row.source_id, row);
    return map;
  }, [sourcesQuery.data]);
  const citations = useMemo(() => buildCitationIndex(content.claims ?? []), [content.claims]);
  const turnNumbers = useMemo(() => new Map(turns.map((turn) => [turn.turn_id, displayTurnNumber(turn)])), [turns]);

  const locked = activeRun !== null || startTurn.isPending;

  /**
   * Adds a created source to the topic as a new version. A narrowed source selection keeps
   * including newly added sources, since adding one signals the user wants it read.
   */
  const addToTopic = useCallback(async (source: CreatedSource | AvailableVersionSource, label: string) => {
    const latest = topicQuery.data ? latestVersion(topicQuery.data) : null;
    if (!latest) throw new Error("topic_not_loaded");
    await attach.mutateAsync({ parentVersionId: latest.version_id, sourceIds: [source.source_id] });
    const reason = ineligibility(source, topicQuery.data?.request.as_of ?? null);
    if (reason === null) setOptions((current) => (current.selectedSourceIds === null || current.selectedSourceIds.includes(source.source_id)
      ? current : { ...current, selectedSourceIds: [...current.selectedSourceIds, source.source_id] }));
    setNotice({ tone: "info", text: reason === null ? `${label}「${source.title}」。下一轮研究会读取它。` : `${label}「${source.title}」，但下一轮不会读取：${INELIGIBLE_TEXT[reason]}。` });
  }, [attach, topicQuery.data]);

  const prefill = useCallback((value: string) => {
    setPanel(null);
    setText(value);
    requestAnimationFrame(() => composerRef.current?.focus());
  }, []);

  const view: TopicView | null = useMemo(() => {
    if (!topic || !version) return null;
    const snapshot = progress.progress;
    const mergedInstructions = new Map<string, ResearchInstruction>();
    for (const receipt of [...receipts, ...(snapshot?.instructions ?? [])]) if (receipt.turn_id === snapshot?.turn.turn_id) mergedInstructions.set(receipt.instruction_id, receipt);
    const live: LiveRun | null = activeRun
      ? {
          operation: activeRun,
          progress: progressMatches(activeRun.operation_id) && snapshot ? { ...snapshot, instructions: [...mergedInstructions.values()] } : null,
          events: progressMatches(activeRun.operation_id) ? progress.events : [],
          stream: { getSnapshot: progress.getStreamSnapshot, subscribe: progress.subscribeStream },
        }
      : null;
    return {
      topicId,
      topic,
      version,
      content,
      turns,
      citations,
      sources,
      live,
      locked,
      draft,
      setDraft,
      dirty,
      saveDraft,
      saving: saveVersion.isPending,
      notify,
      turnNumber: (turnId) => (turnId ? turnNumbers.get(turnId) ?? null : null),
      sourceTitle: (sourceId) => {
        const known = sources.get(sourceId)?.title;
        if (known) return known;
        const row = (content.sources ?? []).find((item) => item.source_id === sourceId);
        if (typeof row?.title === "string") return row.title;
        const prefetched = squadDossier?.snapshot.prefetched_sources?.find((item) => item.source_id === sourceId);
        return prefetched?.title ?? "资料";
      },
      openPanel: setPanel,
      deepDive: (next) => {
        setFocus(next);
        setPanel(null);
        requestAnimationFrame(() => composerRef.current?.focus());
      },
      reextractPdf: async (source) => {
        const textOrder = source.pdf_text_order === "reading" ? "layout" : "reading";
        const created = await reextract.mutateAsync({ sourceId: source.source_id, textOrder });
        await addToTopic(created, "已重新提取并添加");
      },
      gotoClaim: (claimId) => {
        if (!inspector) setPanel(null);
        // Supporting claims sit in the collapsed evidence under their answer; open it first.
        // Counter and challenging claims are always shown, so their evidence stays as it is.
        const claim = content.claims?.find((item) => item.claim_id === claimId);
        const owner = turns.find((turn) => (turn.claim_ids ?? []).includes(claimId));
        if (owner && claim && claimRole(claim) === "support") setEvidence(owner.turn_id, true);
        requestAnimationFrame(() => requestAnimationFrame(() => {
          const element = document.getElementById(`claim-${claimId}`);
          if (!element) return;
          element.scrollIntoView?.({ block: "center", behavior: prefersReducedMotion() ? "auto" : "smooth" });
          flash(element);
        }));
      },
      evidence,
      setEvidence,
    };
    // progressMatches reads progress.progress, listed below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [topic, version, content, turns, citations, sources, activeRun, progress.progress, progress.events, progress.getStreamSnapshot, progress.subscribeStream, receipts, turnNumbers, topicId, inspector, locked, addToTopic, draft, setDraft, dirty, saveDraft, saveVersion.isPending, notify, evidence, setEvidence, squadDossier]);
  const cancelling = cancel.isPending || (cancel.isSuccess && cancel.variables?.operation_id === activeRun?.operation_id);

  const send = () => {
    const question = text.trim();
    if (awaitingUser && progress.progress?.turn.turn_id && latestRun && question && !instructionBusy) {
      void answerClarification(question);
      return;
    }
    if (activeRun) {
      const turnId = progress.progress?.turn.turn_id;
      if (!question || instructionBusy) return;
      if (!turnId) { setInstructionError("正在取得研究轮次，请稍后发送；草稿已保留。"); return; }
      if ([...question].length > 4000) { setInstructionError("追加指令不能超过 4000 个字符。"); return; }
      setInstructionBusy(true); setInstructionError(null);
      void appendInstruction(topicId, turnId, question).then((receipt) => {
        if (topicIdRef.current !== topicId) {
          try { if (sessionStorage.getItem(draftKey)?.trim() === question) sessionStorage.removeItem(draftKey); } catch {}
          return;
        }
        setReceipts((previous) => [...previous.filter((item) => item.instruction_id !== receipt.instruction_id), receipt]);
        setText((current) => { if (current.trim() !== question) return current; try { sessionStorage.removeItem(draftKey); } catch {} return ""; });
        followLiveOutput();
      }).catch((error) => {
        if (topicIdRef.current !== topicId) return;
        setInstructionError(error instanceof ApiError && error.status === 409 ? "本轮已结束或指令冲突，内容已保留，可作为下一轮追问。" : `发送未确认，请重试。${describeError(error)}`);
        void queryClient.invalidateQueries({ queryKey: queryKeys.runs(topicId) });
      }).finally(() => { if (topicIdRef.current === topicId) setInstructionBusy(false); });
      return;
    }
    if (!version || !question || locked || !optionsValid(options)) return;
    if (dirty) { setSendPrompt({ error: null }); return; }
    sendWith(version.version_id);
  };

  const answerClarification = async (answer: string) => {
    const snapshot = progress.progress;
    if (!snapshot?.turn.turn_id || !latestRun || instructionBusy) return;
    setInstructionBusy(true); setInstructionError(null);
    try {
      const receipt = await appendInstruction(topicId, snapshot.turn.turn_id, answer);
      setReceipts((previous) => [...previous.filter((item) => item.instruction_id !== receipt.instruction_id), receipt]);
      await continueWaitingTurn(topicId, latestRun.operation_id);
      setClosedWaitingTurn(snapshot.operation_id);
      setText((current) => current.trim() === answer ? "" : current);
      try { sessionStorage.removeItem(draftKey); } catch {}
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.runs(topicId) }),
        queryClient.invalidateQueries({ queryKey: ["research-turn-statuses"] }),
      ]);
    } catch (error) {
      setInstructionError(`补充已保存后可重试继续研究：${describeError(error)}`);
    } finally { setInstructionBusy(false); }
  };

  const followLiveOutput = useCallback(() => {
    nearBottomRef.current = true;
    setAwayFromLive(false);
    suppressScrollUntilRef.current = Date.now() + 550;
    requestAnimationFrame(() => {
      const element = scrollRef.current;
      element?.scrollTo?.({ top: element.scrollHeight, behavior: prefersReducedMotion() ? "auto" : "smooth" });
    });
  }, []);

  const sendWith = (parentVersionId: string) => {
    const question = text.trim();
    if (!question) return;
    const body: ResearchV2.ResearchRunRequest = {
      parent_version_id: parentVersionId,
      question_override: question,
      ...runRequestFields(options),
      ...(focus ? { focus_citation: { claim_id: focus.claimId, citation_index: focus.citationIndex } } : {}),
    };
    startTurn.mutate(body, {
      onSuccess: (response) => {
        setPendingContinue(null);
        setAskedOp({ operationId: response.operation.operation_id, question });
        setPendingAsk({ question, at: new Date().toISOString(), turnsAt: turns.length, operationId: response.operation.operation_id });
        updateText("");
        setFocus(null);
        followLiveOutput();
      },
    });
  };

  const startFirstTurn = () => {
    if (!version || locked || !optionsValid(options)) return;
    startTurn.mutate(
      { parent_version_id: version.version_id, ...runRequestFields(options) },
      {
        onSuccess: () => {
          setPendingContinue(null);
          setAskedOp(null);
          followLiveOutput();
        },
      },
    );
  };

  /**
   * Regenerate the latest answer, or resend it with an edited question. The run starts from the
   * version before that turn; the replaced answer stays in history and stays visible until the new
   * one is saved. Review edits made after the turn would not carry over, so they need confirmation.
   */
  const regenerate = (turn: ResearchV2.ResearchTurn, question: string | null, discardConfirmed = false) => {
    if (!version || !topic || locked || !optionsValid(options)) return;
    if (dirty) { setNotice({ tone: "error", text: "先保存或放弃复核修改，再重新生成。" }); return; }
    const edits = reviewEditsAfter(topic.versions, turn.parent_version_id);
    if (edits && !discardConfirmed) { setRegenPrompt({ turn, question }); return; }
    const shown = question ?? turn.question;
    setAskedOp(null);
    setPendingContinue({ mode: "regenerate", targetTurnId: turn.turn_id, operationId: null, question: shown });
    startTurn.mutate({
      parent_version_id: version.version_id,
      replace_turn_id: turn.turn_id,
      ...(question !== null ? { question_override: question } : {}),
      ...(edits ? { discard_review_edits: true } : {}),
      ...runRequestFields(options),
    }, {
      onSuccess: (response) => {
        setRegenPrompt(null);
        setPendingContinue({ mode: "regenerate", targetTurnId: turn.turn_id, operationId: response.operation.operation_id, question: shown });
        followLiveOutput();
      },
      onError: (error) => {
        setPendingContinue(null);
        // The server's own check found edits the client did not: ask, as above.
        if (!edits && error instanceof ApiError && error.status === 409 && /confirm/.test(error.message)) setRegenPrompt({ turn, question });
      },
    });
  };

  /** A new research holding this version up to the given turn; it opens right away. */
  const forkFrom = (turn: ResearchV2.ResearchTurn) => {
    if (!version) return;
    topicActions.fork.mutate({ topicId, fork: { version_id: version.version_id, turn_id: turn.turn_id } }, {
      onSuccess: (created) => { navigate(`/research/${encodeURIComponent(created.topic_id)}`); showToast("已创建分支"); },
      onError: (error) => setNotice({ tone: "error", text: `分叉失败：${describeError(error)}` }),
    });
  };
  const forkEntry = (turn: ResearchV2.ResearchTurn) => ({
    key: "fork", label: topicActions.fork.isPending ? "正在创建分支…" : "从这里分叉", icon: "fork" as const,
    disabled: topicActions.fork.isPending, onSelect: () => forkFrom(turn),
  });

  const restorePrevious = (turn: ResearchV2.ResearchTurn) => {
    if (!version || !topic || !turn.replaces_turn_id || locked) return;
    const previous = versionWithTurn(topic.versions, turn.replaces_turn_id);
    if (!previous) return;
    setRestoring(true);
    restoreVersion(topicId, previous.version_id, version.version_id)
      .then((updated) => {
        queryClient.setQueryData(queryKeys.topic(topicId), updated);
        showToast("已恢复上一次的回答");
      })
      .catch((error) => setNotice({ tone: "error", text: `恢复失败：${describeError(error)}` }))
      .finally(() => setRestoring(false));
  };

  const exportVersion = async (kind: "full" | "metadata") => {
    if (!version) return;
    try {
      const data = kind === "full" ? await exportFull(topicId, version.version_id) : await exportMetadata(topicId, version.version_id);
      saveJson(data, `foresightnine-${topicId}-${version.version_id}${kind === "metadata" ? "-metadata" : ""}.json`);
    } catch (error) {
      setNotice({ tone: "error", text: error instanceof ApiError && error.status === 403 ? "有资料不允许导出，无法导出完整记录。可以只导出元数据。" : describeError(error) });
    }
  };

  if (topicQuery.isError && topicQuery.error instanceof ApiError && topicQuery.error.status === 404) {
    return (
      <>
        <Toolbar scrolled={false} />
        <div className="scroll" data-page="TopicPage">
          <div className="center-page">
            <div><h1>找不到这个研究</h1><p className="meta">它可能已被删除，或者不属于当前账户。</p><Link className="btn" to="/research">新研究</Link></div>
          </div>
        </div>
      </>
    );
  }

  const title = topic ? topicTitle(topic) : "";
  // The origin may have been deleted since; then it is named generically.
  const forkOrigin = topic?.forked_from ? topicList.data?.find((item) => item.topic_id === topic.forked_from!.topic_id) : undefined;
  const forkOriginTitle = forkOrigin ? topicTitle(forkOrigin) : "原研究";
  const lastGap = lastTurn?.gaps?.[0];
  const turnActions = (question: string, deliverable: boolean, targetTurnId: string | null): TurnActions => ({
    recovery: recovery.data,
    checking: recovery.isLoading,
    busy: continueTurn.isPending || recoverTurn.isPending,
    locked,
    error: continueTurn.error ? describeError(continueTurn.error) : recoverTurn.error ? describeError(recoverTurn.error) : null,
    onContinue: () => {
      if (!recoveryOperation) return;
      setAskedOp(null);
      setPendingContinue({ mode: "continue", targetTurnId, operationId: null, question });
      continueTurn.mutate(recoveryOperation, {
        onSuccess: (response) => {
          setPendingContinue({ mode: "continue", targetTurnId, operationId: response.operation.operation_id, question });
          followLiveOutput();
        },
        onError: () => setPendingContinue(null),
      });
    },
    onDeliver: () => {
      if (!recoveryOperation) return;
      setAskedOp(null);
      setPendingContinue({ mode: "deliver", targetTurnId, operationId: null, question });
      recoverTurn.mutate(recoveryOperation, {
        onSuccess: (response) => {
          setPendingContinue({ mode: "deliver", targetTurnId, operationId: response.operation.operation_id, question });
          followLiveOutput();
        },
        onError: () => setPendingContinue(null),
      });
    },
    onAskAgain: () => prefill(question),
    onFillGap: lastGap ? () => prefill(`请继续补充上一轮的缺口：${gapText(lastGap)}`) : null,
    onAddSources: () => setPanel({ kind: "add" }),
    deliverable,
  });

  const live = view?.live ?? null;
  const canModifyLast = Boolean(view) && !locked && !live && !orphanRun && !pendingAsk && !startTurn.isPending;
  const latestTurnControls = (turn: ResearchV2.ResearchTurn) => {
    const tools: AnswerTools = {
      primary: (
        <button type="button" className="icon-btn tool" aria-label="重新生成" title="重新生成这一轮的回答" onClick={() => regenerate(turn, null)}>
          <Icon name="regenerate" size={15} />
        </button>
      ),
      menu: [
        { key: "edit", label: "编辑问题并重发", icon: "pen", onSelect: () => setEditingTurnId(turn.turn_id) },
        ...(turn.replaces_turn_id ? [{ key: "restore", label: restoring ? "正在恢复…" : "恢复上一次的回答", icon: "undo" as const, disabled: restoring, onSelect: () => restorePrevious(turn) }] : []),
        forkEntry(turn),
      ],
    };
    return {
      tools,
      askTools: (
        <button type="button" className="icon-btn tool" aria-label="编辑问题" title="编辑问题并重发" onClick={() => setEditingTurnId(turn.turn_id)}>
          <Icon name="pen" size={15} />
        </button>
      ),
      editing: editingTurnId === turn.turn_id,
      onEditDone: (question: string | null) => {
        setEditingTurnId(null);
        if (question !== null) regenerate(turn, question === turn.question ? null : question);
      },
    };
  };
  const liveIndex = live?.progress?.turn.index ?? 0;
  const liveCommitted = live !== null && turns.some((turn) => turn.operation_ids.includes(live.operation.operation_id));
  const liveTerminal = live?.progress != null && (live.progress.logical_status ? live.progress.logical_status !== "running" : !isActiveStatus(live.progress.operation_status));
  const showLiveTurn = live !== null && !liveCommitted && !liveTerminal;
  const showPendingTurn = showLiveTurn || (!live && pendingAsk !== null);
  const isContinuingLastTurn =
    showLiveTurn
    && lastTurn !== null
    && (live.progress
      ? live.progress.turn.index === lastTurn.index
      : pendingContinue !== null
        ? pendingContinue.targetTurnId === lastTurn.turn_id
        : lastTurn.status === "partial" && pendingAsk === null && askedOp?.operationId !== live.operation.operation_id);
  const continuingMode = isContinuingLastTurn ? (pendingContinue?.mode ?? "continue") : null;
  const liveNumber = liveIndex > 0 ? liveIndex : isContinuingLastTurn && lastTurn ? displayTurnNumber(lastTurn) : turns.length + 1;
  const liveQuestion = (
    live?.progress?.turn.question
    || pendingAsk?.question
    || (askedOp && askedOp.operationId === live?.operation.operation_id ? askedOp.question : "")
    || (!isContinuingLastTurn ? pendingContinue?.question : "")
    || (turns.length === 0 ? topic?.request.question : "")
  ) ?? "";

  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !showPendingTurn) {
      nearBottomRef.current = true;
      suppressScrollUntilRef.current = 0;
      setAwayFromLive(false);
      return undefined;
    }
    lastScrollTopRef.current = el.scrollTop;
    const update = () => {
      const top = el.scrollTop;
      const scrolledUp = top < lastScrollTopRef.current - 4;
      lastScrollTopRef.current = top;
      const distance = el.scrollHeight - top - el.clientHeight;
      if (scrolledUp) suppressScrollUntilRef.current = 0;
      if (Date.now() < suppressScrollUntilRef.current) {
        if (distance <= 80) {
          suppressScrollUntilRef.current = 0;
          nearBottomRef.current = true;
          setAwayFromLive(false);
        }
        return;
      }
      const near = distance <= 80;
      nearBottomRef.current = near;
      setAwayFromLive(!near);
    };
    update();
    el.addEventListener("scroll", update, { passive: true });
    return () => el.removeEventListener("scroll", update);
  }, [showPendingTurn]);

  const liveEventCount = live?.events.length ?? 0;
  const liveCurrent = live?.progress?.current;
  const liveCurrentKey = liveCurrent
    ? liveCurrent.kind === "tool"
      ? `tool:${liveCurrent.name}:${liveCurrent.rationale ?? ""}:${liveCurrent.target ?? ""}`
      : `model:${liveCurrent.phase ?? ""}:${liveCurrent.retry_attempt ?? ""}`
    : "";

  useEffect(() => {
    if (!showPendingTurn || !nearBottomRef.current) return undefined;
    const frame = requestAnimationFrame(() => {
      const el = scrollRef.current;
      if (!el) return;
      const reduced = prefersReducedMotion();
      if (!reduced && el.scrollHeight > el.clientHeight) {
        suppressScrollUntilRef.current = Date.now() + 450;
      }
      el.scrollTo?.({ top: el.scrollHeight, behavior: reduced ? "auto" : "smooth" });
    });
    return () => cancelAnimationFrame(frame);
  }, [showPendingTurn, liveEventCount, liveCurrentKey]);

  const onDraftGrow = useCallback(() => {
    if (!nearBottomRef.current) return;
    requestAnimationFrame(() => {
      const el = scrollRef.current;
      if (!el || !nearBottomRef.current) return;
      const reduced = prefersReducedMotion();
      if (!reduced && el.scrollHeight > el.clientHeight) {
        suppressScrollUntilRef.current = Date.now() + 450;
      }
      el.scrollTo?.({ top: el.scrollHeight, behavior: reduced ? "auto" : "smooth" });
    });
  }, []);

  const scrollToLive = useCallback(() => {
    nearBottomRef.current = true;
    setAwayFromLive(false);
    const target = liveTurnRef.current;
    const scroller = scrollRef.current;
    const reduced = prefersReducedMotion();
    if (!reduced && scroller && scroller.scrollHeight > scroller.clientHeight) {
      suppressScrollUntilRef.current = Date.now() + 450;
    }
    if (target?.scrollIntoView && scroller && scroller.clientHeight > 0) {
      target.scrollIntoView({ block: "end", behavior: reduced ? "auto" : "smooth" });
    } else if (scroller) {
      scroller.scrollTo?.({ top: scroller.scrollHeight, behavior: reduced ? "auto" : "smooth" });
    }
  }, []);

  const tokens: ComposerToken[] = [
    ...(focus ? [{ key: "focus", label: `深挖引用 ${focus.n}`, removeLabel: "移除深挖", onRemove: () => setFocus(null) }] : []),
    ...optionTokens(options, setOptions),
  ];
  const suggestions = view && !locked ? suggestionsFor(content) : [];

  return (
    <>
      <Toolbar title={title} scrolled={scrolled}>
        <button
          type="button"
          className="icon-btn"
          aria-label="研究记录"
          title="研究记录"
          aria-pressed={panel?.kind === "record"}
          disabled={!view}
          onClick={() => setPanel(panel?.kind === "record" ? null : { kind: "record", tab: "claims" })}
        >
          <Icon name="inspector" />
        </button>
        <Menu
          label="更多操作"
          trigger={<Icon name="more" />}
          triggerClassName="icon-btn"
          items={[
            { key: "rename", label: "重命名", icon: "pen", onSelect: () => setRenaming(true), disabled: !view },
            { key: "review", label: "复核与版本", icon: "check", onSelect: () => setPanel({ kind: "review" }) },
            { key: "settings", label: "研究设置", icon: "tune", onSelect: () => setPanel({ kind: "settings" }) },
            { key: "add", label: "添加资料", icon: "plus", onSelect: () => setPanel({ kind: "add" }), disabled: locked || !version },
            "separator",
            { key: "export", label: "导出完整记录（JSON）", icon: "download", onSelect: () => void exportVersion("full"), disabled: !version },
            { key: "metadata", label: "仅导出元数据", icon: "download", onSelect: () => void exportVersion("metadata"), disabled: !version },
          ]}
        />
      </Toolbar>
      <NetworkBanner />
      {notice ? <div className={`net-banner ${notice.tone === "error" ? "notice" : "info"}`} role={notice.tone === "error" ? "alert" : "status"}>{notice.text}</div> : null}
      <div className="sr-only" aria-live="polite">{announcement}</div>
      <div className="scroll" ref={scrollRef} data-page="TopicPage">
        <div className="col">
          {!view ? (
            <div aria-busy="true">
              <div className="skeleton title" />
              <div className="skeleton line" /><div className="skeleton line" /><div className="skeleton line short" />
              {topicQuery.isError ? <p className="inline-error">{describeError(topicQuery.error)}</p> : null}
            </div>
          ) : (
            <TopicContext.Provider value={view}>
              <header className="large-title">
                {renaming && topic ? (
                  <TitleEditor
                    initial={title}
                    onDone={(next) => {
                      setRenaming(false);
                      const value = next?.trim();
                      if (next === null || value === title || (!value && !topic.title)) return;
                      topicActions.update.mutate({ topicId, update: { title: value ?? "" } }, {
                        onError: (error) => setNotice({ tone: "error", text: `重命名失败：${describeError(error)}` }),
                      });
                    }}
                  />
                ) : (
                  <h1 onDoubleClick={() => setRenaming(true)}>{title}</h1>
                )}
                {/* Version and turn count live in 复核与版本; the header only names what needs attention. */}
                {topic!.request.as_of || dirty || topic!.forked_from ? (
                  <div className="meta">
                    {topic!.forked_from ? (
                      <Link className="link meta-link" to={`/research/${encodeURIComponent(topic!.forked_from.topic_id)}`}>
                        分叉自《{forkOriginTitle}》
                        {topic!.forked_from.turn_index ? `第 ${topic!.forked_from.turn_index} 轮` : ""}
                      </Link>
                    ) : null}
                    {topic!.request.as_of ? <span>数据截止 {formatDateTimeWithOffset(topic!.request.as_of)}</span> : null}
                    {dirty ? <button type="button" className="link meta-link unsaved" onClick={() => setPanel({ kind: "review" })}>有未保存的修改</button> : null}
                  </div>
                ) : null}
              </header>
              <TopicConclusion turns={turns} />
              {turns.map((turn, index) => (
                <TurnView
                  key={turn.turn_id}
                  turn={turn}
                  waiting={awaitingUser && turn.turn_id === progress.progress?.turn.turn_id}
                  first={index === 0}
                  {...(index === turns.length - 1 && canModifyLast ? latestTurnControls(turn) : view ? { tools: { menu: [forkEntry(turn)] } } : {})}
                  actions={index === turns.length - 1 && turn.status === "partial" && !orphanRun && !activeRun && !awaitingUser ? turnActions(turn.question, turn.reason !== "submitted", turn.turn_id) : null}
                />
              ))}
              {awaitingUser && progress.progress?.clarification ? (
                <div className="turn"><div className="callout" role="status">
                  <span className="h">需要你的补充</span>
                  <p>{progress.progress.clarification.question}</p>
                  <p>{progress.progress.clarification.why_it_matters}</p>
                  <button type="button" className="btn" disabled={instructionBusy} onClick={() => {
                    const turnId = progress.progress?.turn.turn_id;
                    if (!turnId) return;
                    setInstructionBusy(true); setInstructionError(null);
                    void acceptFindings(topicId, turnId).then(() => { setClosedWaitingTurn(progress.progress?.operation_id ?? null); return Promise.all([
                      queryClient.invalidateQueries({ queryKey: queryKeys.runs(topicId) }),
                      queryClient.invalidateQueries({ queryKey: ["research-turn-statuses"] }),
                    ]); }).catch((error) => setInstructionError(describeError(error))).finally(() => setInstructionBusy(false));
                  }}>接受现有分析</button>
                  <button type="button" className="btn" disabled={stoppingTurn} onClick={() => {
                    const turnId = progress.progress?.turn.turn_id;
                    if (!turnId) return;
                    setStoppingTurn(true);
                    void stopResearchTurn(topicId, turnId).then(() => { setClosedWaitingTurn(progress.progress?.operation_id ?? null); return Promise.all([
                      queryClient.invalidateQueries({ queryKey: queryKeys.runs(topicId) }),
                      queryClient.invalidateQueries({ queryKey: ["research-turn-statuses"] }),
                    ]); }).catch((error) => setInstructionError(describeError(error))).finally(() => setStoppingTurn(false));
                  }}>停止研究</button>
                </div></div>
              ) : null}
              {orphanRun && !activeRun ? (
                <InterruptedTurn
                  number={turns.length + 1}
                  startedAt={orphanRun.created_at}
                  question={(progressMatches(orphanRun.operation_id) ? progress.progress?.turn.question : "") || (turns.length === 0 ? topic!.request.question ?? "" : "")}
                  status={orphanRun.status === "cancelled" || progress.progress?.logical_status === "stopped" ? "cancelled" : "failed"}
                  events={progressMatches(orphanRun.operation_id) ? progress.events : []}
                  first={turns.length === 0}
                  actions={turnActions((progressMatches(orphanRun.operation_id) ? progress.progress?.turn.question : "") || topic!.request.question || "", true, null)}
                />
              ) : null}
              {showLiveTurn ? (
                <LiveTurn
                  sectionRef={liveTurnRef}
                  number={liveNumber}
                  startedAt={live!.progress?.started_at ?? live!.operation.created_at}
                  question={liveQuestion}
                  live={live}
                  onDraftGrow={onDraftGrow}
                  continuing={continuingMode}
                  editedQuestion={continuingMode === "regenerate" && pendingContinue && lastTurn && pendingContinue.question !== lastTurn.question ? pendingContinue.question : null}
                  first={turns.length === 0}
                />
              ) : !live && pendingAsk ? (
                <LiveTurn sectionRef={liveTurnRef} number={turns.length + 1} startedAt={pendingAsk.at} question={pendingAsk.question} live={null} first={turns.length === 0} />
              ) : null}
              {turns.length === 0 && !live && !orphanRun && !pendingAsk && !runs.isLoading && squadStatus === "idle" && !squadDossier ? (
                <div className="turn">
                  <div className="callout" role="note">
                    <span className="h">这个研究还没有开始。</span>
                    <div className="actions">
                      <button type="button" className="btn primary" onClick={startFirstTurn} disabled={locked}>{startTurn.isPending ? "正在提交…" : "开始研究"}</button>
                    </div>
                    {startTurn.isError ? <span className="inline-error" role="alert">{describeError(startTurn.error)}</span> : null}
                  </div>
                </div>
              ) : null}
              <div className="turn earnings-squad-workspace" data-testid="earnings-squad-workspace">
                <section className="chart-card" aria-label="美股财报买方投研小组">
                  <div className="chart-head">
                    <div className="chart-title-wrap">
                      <div className="chart-title-row">
                        <h2 className="chart-title">美股财报买方投研小组</h2>
                        {squadStatus !== "idle" ? (
                          <span className={`role light ${SQUAD_STATUS_META[squadStatus].tone}`}>
                            {SQUAD_STATUS_META[squadStatus].label}
                          </span>
                        ) : null}
                        {squadDossier?.symbol ? (
                          <span className="chart-badge is-updated">{squadDossier.symbol}</span>
                        ) : null}
                      </div>
                      <p className="chart-subtitle">
                        {squadStatus === "running"
                          ? "多 Agent 组织树正在并行取证，可向 Lead 发送中场调度指令调整关注焦点。"
                          : squadDossier
                            ? "已锁定单一事实底稿与买方档案，支持 Memo / Deep Report 秒切及多格式导出。"
                            : "可选：基于 SEC EDGAR 与已配置数据连接器启动 Lead Coordinator、并行专题 Worker、Bear Review 与撰稿小组。"}
                      </p>
                    </div>
                  </div>

                  {squadStatus !== "running" ? (
                    <form
                      className="input-row"
                      aria-label="启动美股财报买方小组"
                      onSubmit={(event) => {
                        event.preventDefault();
                        void handleLaunchSquad();
                      }}
                    >
                      <input
                        type="text"
                        aria-label="美股代码 (Ticker，可选)"
                        placeholder="美股代码，如 AAPL / NVDA（可选，留空自动识别）"
                        value={squadSymbol}
                        onChange={(event) => setSquadSymbol(event.target.value)}
                        disabled={squadLaunching}
                      />
                      <button type="submit" className="btn primary" disabled={squadLaunching}>
                        {squadLaunching ? "正在启动…" : squadDossier ? "重新运行财报小组" : "启动美股财报买方小组"}
                      </button>
                    </form>
                  ) : (
                    <form
                      className="input-row"
                      aria-label="中场调度指令"
                      onSubmit={(event) => {
                        event.preventDefault();
                        void handleSteerSquad();
                      }}
                    >
                      <input
                        type="text"
                        aria-label="向 Lead 发送中场调度指令"
                        placeholder="向 Lead 发送中场调度指令，例如：核查存货周转与 Non-GAAP SBC 加回…"
                        value={steerInput}
                        onChange={(event) => setSteerInput(event.target.value)}
                        disabled={steerBusy}
                      />
                      <button
                        type="submit"
                        className="btn primary"
                        disabled={steerBusy || steerInput.trim().length === 0}
                      >
                        {steerBusy ? "发送调度…" : "发送调度指令"}
                      </button>
                    </form>
                  )}

                  {squadSteeringHistory.length > 0 ? (
                    <div className="chips" aria-label="已下达调度指令">
                      <span>已下达调度：</span>
                      {squadSteeringHistory.map((item) => (
                        <span key={item.instruction_id ?? `seq-${item.sequence}`} className="chart-badge is-updated">
                          #{item.sequence} {item.text}
                        </span>
                      ))}
                    </div>
                  ) : null}

                  {squadError ? (
                    <span className="inline-error" role="alert">{squadError}</span>
                  ) : null}
                  {steerError ? (
                    <span className="inline-error" role="alert">{steerError}</span>
                  ) : null}
                </section>

                {squadStatus !== "idle" || orgGraphState.nodes.length > 0 ? (
                  <OrgGraphPanel
                    key={squadRunId ?? "default"}
                    state={orgGraphState}
                    runStatus={squadStatus}
                    title="投研小组实时组织执行树"
                    onSelectNode={(node) => {
                      const open = node !== null;
                      setOrgDrawerOpen(open);
                      if (open) setPanel(null);
                    }}
                  />
                ) : null}

                {squadDossier ? (
                  <EarningsDossierView
                    dossier={squadDossier}
                    topicId={topicId}
                    reportMode={reportMode}
                    onReportModeChange={setReportMode}
                    onRequestDeepReport={handleRequestDeepReport}
                    deepReportLoading={deepReportLoading}
                    deepReportError={deepReportError}
                    onInspectSource={handleInspectSource}
                    slotRoutingSummary={squadSlotRouting}
                  />
                ) : null}
              </div>
            </TopicContext.Provider>
          )}
        </div>
      </div>
      {view ? (
        <TopicContext.Provider value={view}>
          <div className="dock">
            <div className="dock-inner">
              {live ? (
                awayFromLive ? <LiveCapsule live={live} onJump={scrollToLive} /> : null
              ) : !awaitingUser && suggestions.length > 0 ? (
                <div className="suggest">
                  {suggestions.map((item) => <button key={item.key} type="button" className="link" onClick={() => prefill(item.prefill)}>{item.label}</button>)}
                </div>
              ) : null}
              {cancel.isError ? <span className="inline-error" role="alert">{describeError(cancel.error)}</span> : null}
              {progress.error && activeRun ? <span className="inline-error" role="alert">无法更新研究进度：{describeError(progress.error)}</span> : null}
              <Composer
                ref={composerRef}
                variant="dock"
                value={text}
                onChange={updateText}
                onSubmit={send}
                onOpenSettings={() => setPanel({ kind: "settings" })}
                onAddSources={() => setPanel({ kind: "add" })}
                addDisabled={locked}
                onDropFiles={(files) => { if (files[0]) setPanel({ kind: "add", initial: { tab: "file", file: files[0] } }); }}
                label="追问"
                placeholder={awaitingUser ? "补充这项信息…" : activeRun ? "追加研究要求…" : "继续追问…"}
                submitLabel="发送"
                disabled={startTurn.isPending && !activeRun}
                onStop={live ? () => {
                  const turnId = live.progress?.turn.turn_id;
                  if (!turnId) { if (live.progress) cancel.mutate(live.operation); else setInstructionError("正在取得研究轮次，请稍后停止。"); return; }
                  setStoppingTurn(true);
                  void stopResearchTurn(topicId, turnId).then(() => {
                    void queryClient.invalidateQueries({ queryKey: queryKeys.runs(topicId) });
                    void queryClient.invalidateQueries({ queryKey: queryKeys.topic(topicId) });
                    void queryClient.invalidateQueries({ queryKey: ["research-turn-statuses"] });
                  }).catch((error) => setInstructionError(describeError(error))).finally(() => setStoppingTurn(false));
                } : undefined}
                stopping={cancelling || stoppingTurn}
                busy={startTurn.isPending || instructionBusy}
                canSubmit={text.trim().length > 0}
                tokens={tokens}
                error={instructionError ?? (startTurn.isError && turns.length > 0 ? describeError(startTurn.error) : null)}
              />
            </div>
          </div>
          {panel?.kind === "record" ? (
            <RecordPanel
              tab={panel.tab}
              turnKey={panel.turnKey}
              onTab={(tab: AnswerTab) => setPanel({ ...panel, tab })}
              onTurn={(turnKey) => setPanel({ ...panel, turnKey })}
              onClose={() => setPanel(null)}
            />
          ) : null}
          {panel?.kind === "reader" ? <ReaderPanel key={`${panel.sourceId}:${panel.range?.join(":") ?? ""}`} sourceId={panel.sourceId} range={panel.range} onClose={() => setPanel(null)} /> : null}
          {panel?.kind === "cite" ? <CitationSheet n={panel.n} claimId={panel.claimId} citationIndex={panel.citationIndex} onClose={() => setPanel(null)} /> : null}
          {panel?.kind === "settings" ? (
            <ResearchSettingsPanel mode="topic" options={options} asOfIso={topic!.request.as_of ?? null} scope={topic!.request.scope ?? ""} sources={[...sources.values()]} onApply={setOptions} onClose={() => setPanel(null)} />
          ) : null}
          {panel?.kind === "review" ? <ReviewPanel onClose={() => setPanel(null)} /> : null}
          {regenPrompt ? (
            <ConfirmDialog
              title="重新生成会放弃之后的复核修改"
              description="这一轮回答之后，你在复核中做过修改。重新生成从这一轮之前的版本开始，这些修改不会带到新的回答里；原来的回答和修改仍保留在版本历史中。"
              onCancel={() => setRegenPrompt(null)}
              actions={[{ key: "regen", label: startTurn.isPending ? "正在提交…" : "放弃修改并重新生成", tone: "danger", disabled: startTurn.isPending,
                onSelect: () => regenPrompt && regenerate(regenPrompt.turn, regenPrompt.question, true) }]}
            />
          ) : null}
          {sendPrompt ? (
            <ConfirmDialog
              title="发送新问题前，先保存复核修改吗？"
              description="保存后，研究 Agent 会在你修改过的版本上继续。"
              error={sendPrompt.error}
              onCancel={() => setSendPrompt(null)}
              actions={[
                {
                  key: "save", label: saveVersion.isPending ? "正在保存…" : "保存并发送", tone: "primary", disabled: saveVersion.isPending,
                  onSelect: () => {
                    saveDraft().then((versionId) => { setSendPrompt(null); sendWith(versionId); })
                      .catch((error: unknown) => setSendPrompt({ error: error instanceof Error && !(error instanceof ApiError) ? error.message : saveErrorText(error) }));
                  },
                },
                { key: "discard", label: "放弃修改并发送", tone: "plain", disabled: saveVersion.isPending, onSelect: () => { setDraftState({}); setSendPrompt(null); sendWith(version!.version_id); } },
              ]}
            />
          ) : null}
          {blocker.state === "blocked" ? (
            <ConfirmDialog
              title="离开前保存复核修改吗？"
              description="没有保存的修改离开后会丢失。"
              error={leaveError}
              onCancel={() => { setLeaveError(null); blocker.reset(); }}
              actions={[
                {
                  key: "save", label: saveVersion.isPending ? "正在保存…" : "保存为新版本", tone: "primary", disabled: saveVersion.isPending,
                  onSelect: () => {
                    saveDraft().then(() => { setLeaveError(null); blocker.proceed(); })
                      .catch((error: unknown) => setLeaveError(error instanceof Error && !(error instanceof ApiError) ? error.message : saveErrorText(error)));
                  },
                },
                { key: "discard", label: "放弃修改", tone: "plain", disabled: saveVersion.isPending, onSelect: () => { setDraftState({}); setLeaveError(null); blocker.proceed(); } },
              ]}
            />
          ) : null}
          {panel?.kind === "add" ? (
            <AddSourcesPanel
              asOfIso={topic!.request.as_of ?? null}
              initial={panel.initial}
              locked={locked}
              onAdded={(source) => addToTopic(source, "已添加")}
              onClose={() => setPanel(null)}
            />
          ) : null}
        </TopicContext.Provider>
      ) : null}
    </>
  );
}
