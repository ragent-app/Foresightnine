import type { ResearchV2 } from "@foresightnine/contracts";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useBlocker, useParams } from "react-router-dom";
import { ApiError } from "../api/http";
import { useActiveRun, useAttachSources, useCancelRun, useContinueTurn, useRecoverTurn, useRecoveryStatus, useReextractPdf, useRunProgress, useSaveVersion, useStartTurn, useTopic, useVersionSources } from "../api/queries";
import { exportFull, exportMetadata, isActiveStatus, latestVersion } from "../api/research";
import type { AvailableVersionSource, CreatedSource } from "../api/types";
import { gapText } from "../lib/copy";
import { saveJson } from "../lib/download";
import { displayTurnNumber, formatDateTimeWithOffset } from "../lib/format";
import { buildCitationIndex, claimRole, suggestionsFor, topicTitle, type AnswerTab } from "../lib/research";
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
import { InterruptedTurn, LiveTurn, TurnView, type TurnActions } from "./topic/Conversation";
import { LiveCapsule } from "./topic/LiveBar";
import { ReaderPanel } from "./topic/ReaderPanel";
import { RecordPanel } from "./topic/RecordPanel";
import { ReviewPanel, saveErrorText } from "./topic/ReviewPanel";

interface PendingAsk {
  question: string;
  at: string;
  turnsAt: number;
  operationId: string;
}

interface PendingContinue {
  mode: "continue" | "deliver";
  targetTurnId: string | null;
  operationId: string | null;
  question: string;
}

function flash(element: Element): void {
  element.classList.add("flash");
  setTimeout(() => element.classList.remove("flash"), 900);
}

export function TopicPage() {
  const { topicId = "" } = useParams();
  const topicQuery = useTopic(topicId);
  const runs = useActiveRun(topicId);
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
  const [focus, setFocus] = useState<FocusCitation | null>(null);
  const [options, setOptions] = useState<RunOptions>(DEFAULT_OPTIONS);
  const [pendingAsk, setPendingAsk] = useState<PendingAsk | null>(null);
  const [askedOp, setAskedOp] = useState<{ operationId: string; question: string } | null>(null);
  const [pendingContinue, setPendingContinue] = useState<PendingContinue | null>(null);
  const [awayFromLive, setAwayFromLive] = useState(false);
  const [evidence, setEvidenceState] = useState<ReadonlyMap<string, boolean>>(new Map());
  const setEvidence = useCallback((turnId: string, open: boolean) => {
    setEvidenceState((previous) => new Map(previous).set(turnId, open));
  }, []);
  const [notice, setNotice] = useState<{ text: string; tone: "info" | "error" } | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const [draft, setDraftState] = useState<ReviewDraft>({});
  // Asked before sending while review changes are unsaved (spec §4.3).
  const [sendPrompt, setSendPrompt] = useState<{ error: string | null } | null>(null);

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
  const activeRun = runs.activeRun;
  const latestRun = runs.latestRun;
  const orphanRun =
    latestRun && !isActiveStatus(latestRun.status) && (latestRun.status === "failed" || latestRun.status === "cancelled")
      && topic && !turns.some((turn) => turn.operation_ids.includes(latestRun.operation_id))
      ? latestRun
      : null;
  const progress = useRunProgress(topicId, activeRun?.operation_id ?? orphanRun?.operation_id ?? null);
  const progressMatches = (operationId: string | undefined) => progress.progress?.operation_id === operationId;
  const recoveryOperation = activeRun
    ? null
    : orphanRun?.operation_id ?? (lastTurn?.status === "partial" ? lastTurn.operation_ids[lastTurn.operation_ids.length - 1] ?? null : null);
  const recovery = useRecoveryStatus(topicId, recoveryOperation);
  const sourcesQuery = useVersionSources(topicId, version?.version_id ?? null);

  useEffect(() => {
    setPanelOpen(panel !== null && panel.kind !== "cite");
    return () => setPanelOpen(false);
  }, [panel, setPanelOpen]);

  // Reset per-topic state when navigating between topics.
  useEffect(() => {
    setPanel(null); setText(""); setFocus(null); setPendingAsk(null); setAskedOp(null); setPendingContinue(null); setAwayFromLive(false);
    nearBottomRef.current = true; lastScrollTopRef.current = 0; suppressScrollUntilRef.current = 0;
    setNotice(null); setOptions(DEFAULT_OPTIONS); setDraftState({}); setSendPrompt(null);
  }, [topicId]);

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
    const live: LiveRun | null = activeRun
      ? {
          operation: activeRun,
          progress: progressMatches(activeRun.operation_id) ? progress.progress : null,
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
        return typeof row?.title === "string" ? row.title : "资料";
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
  }, [topic, version, content, turns, citations, sources, activeRun, progress.progress, progress.events, progress.getStreamSnapshot, progress.subscribeStream, turnNumbers, topicId, inspector, locked, addToTopic, draft, setDraft, dirty, saveDraft, saveVersion.isPending, notify, evidence, setEvidence]);
  const cancelling = cancel.isPending || (cancel.isSuccess && cancel.variables?.operation_id === activeRun?.operation_id);

  const send = () => {
    const question = text.trim();
    if (!version || !question || locked || !optionsValid(options)) return;
    if (dirty) { setSendPrompt({ error: null }); return; }
    sendWith(version.version_id);
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
        setText("");
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
  const liveIndex = live?.progress?.turn.index ?? 0;
  const liveCommitted = live !== null && turns.some((turn) => turn.operation_ids.includes(live.operation.operation_id));
  const liveTerminal = live?.progress != null && !isActiveStatus(live.progress.operation_status);
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
                <h1>{title}</h1>
                {/* Version and turn count live in 复核与版本; the header only names what needs attention. */}
                {topic!.request.as_of || dirty ? (
                  <div className="meta">
                    {topic!.request.as_of ? <span>数据截止 {formatDateTimeWithOffset(topic!.request.as_of)}</span> : null}
                    {dirty ? <button type="button" className="link meta-link unsaved" onClick={() => setPanel({ kind: "review" })}>有未保存的修改</button> : null}
                  </div>
                ) : null}
              </header>
              {turns.map((turn, index) => (
                <TurnView
                  key={turn.turn_id}
                  turn={turn}
                  first={index === 0 && turn.question.trim() === title.trim()}
                  actions={index === turns.length - 1 && turn.status === "partial" && !orphanRun && !activeRun ? turnActions(turn.question, turn.reason !== "submitted", turn.turn_id) : null}
                />
              ))}
              {orphanRun && !activeRun ? (
                <InterruptedTurn
                  number={turns.length + 1}
                  startedAt={orphanRun.created_at}
                  question={(progressMatches(orphanRun.operation_id) ? progress.progress?.turn.question : "") || (turns.length === 0 ? topic!.request.question ?? "" : "")}
                  status={orphanRun.status === "cancelled" ? "cancelled" : "failed"}
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
                  first={turns.length === 0 && liveQuestion.trim() === title.trim()}
                />
              ) : !live && pendingAsk ? (
                <LiveTurn sectionRef={liveTurnRef} number={turns.length + 1} startedAt={pendingAsk.at} question={pendingAsk.question} live={null} first={turns.length === 0 && pendingAsk.question.trim() === title.trim()} />
              ) : null}
              {turns.length === 0 && !live && !orphanRun && !pendingAsk && !runs.isLoading ? (
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
              ) : suggestions.length > 0 ? (
                <div className="suggest">
                  {suggestions.map((item) => <button key={item.key} type="button" className="link" onClick={() => prefill(item.prefill)}>{item.label}</button>)}
                </div>
              ) : null}
              {cancel.isError ? <span className="inline-error" role="alert">{describeError(cancel.error)}</span> : null}
              <Composer
                ref={composerRef}
                variant="dock"
                value={text}
                onChange={setText}
                onSubmit={send}
                onOpenSettings={() => setPanel({ kind: "settings" })}
                onAddSources={() => setPanel({ kind: "add" })}
                addDisabled={locked}
                onDropFiles={(files) => { if (files[0]) setPanel({ kind: "add", initial: { tab: "file", file: files[0] } }); }}
                label="追问"
                placeholder={locked ? "研究进行中，可以先停止再提问" : "继续追问…"}
                submitLabel="发送"
                disabled={locked}
                onStop={live ? () => cancel.mutate(live.operation) : undefined}
                stopping={cancelling}
                busy={startTurn.isPending}
                canSubmit={text.trim().length > 0}
                tokens={tokens}
                error={startTurn.isError && turns.length > 0 ? describeError(startTurn.error) : null}
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
