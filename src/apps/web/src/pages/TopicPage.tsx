import type { ResearchV2 } from "@foresightnine/contracts";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError } from "../api/http";
import { useActiveRun, useCancelRun, useContinueTurn, useRecoverTurn, useRecoveryStatus, useRunProgress, useStartTurn, useTopic, useVersionSources } from "../api/queries";
import { exportFull, exportMetadata, isActiveStatus, latestVersion } from "../api/research";
import type { AvailableVersionSource } from "../api/types";
import { gapText, REVIEW_LABEL } from "../lib/copy";
import { displayTurnNumber, formatDateTimeWithOffset } from "../lib/format";
import { buildCitationIndex, suggestionsFor, topicTitle, type AnswerTab } from "../lib/research";
import { useLayout } from "../ui/AppLayout";
import { Composer, type ComposerHandle, type ComposerToken } from "../ui/Composer";
import { Icon } from "../ui/icons";
import { MEDIA, prefersReducedMotion, useMedia, useScrolledPast } from "../ui/media";
import { Menu } from "../ui/Menu";
import { NetworkBanner, Toolbar } from "../ui/Toolbar";
import { describeError } from "./research/errors";
import { DEFAULT_OPTIONS, optionsValid, runRequestFields, type RunOptions } from "./research/options";
import { ResearchSettingsPanel } from "./research/ResearchSettingsPanel";
import { CitationSheet } from "./topic/Citation";
import { TopicContext, type FocusCitation, type LiveRun, type PanelState, type TopicView } from "./topic/context";
import { InterruptedTurn, PendingTurn, TurnView, type TurnActions } from "./topic/Conversation";
import { LiveBar } from "./topic/LiveBar";
import { ReaderPanel } from "./topic/ReaderPanel";
import { RecordPanel } from "./topic/RecordPanel";

interface PendingAsk {
  question: string;
  at: string;
  turnsAt: number;
}

function downloadJson(data: unknown, name: string): void {
  const blob = new Blob([`${JSON.stringify(data, null, 2)}\n`], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
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
  const composerRef = useRef<ComposerHandle>(null);
  const scrolled = useScrolledPast(scrollRef);

  const [panel, setPanel] = useState<PanelState>(null);
  const [text, setText] = useState("");
  const [focus, setFocus] = useState<FocusCitation | null>(null);
  const [options, setOptions] = useState<RunOptions>(DEFAULT_OPTIONS);
  const [pendingAsk, setPendingAsk] = useState<PendingAsk | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");

  const startTurn = useStartTurn(topicId);
  const cancel = useCancelRun(topicId);
  const continueTurn = useContinueTurn(topicId);
  const recoverTurn = useRecoverTurn(topicId);

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
    setPanel(null); setText(""); setFocus(null); setPendingAsk(null); setNotice(null); setOptions(DEFAULT_OPTIONS);
  }, [topicId]);

  // Pending question bubble: hide once the turn is saved or the run stops without one.
  useEffect(() => {
    if (pendingAsk && (turns.length > pendingAsk.turnsAt || (orphanRun && orphanRun.created_at >= pendingAsk.at))) setPendingAsk(null);
  }, [pendingAsk, turns.length, orphanRun]);

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

  const prefill = useCallback((value: string) => {
    setPanel(null);
    setText(value);
    requestAnimationFrame(() => composerRef.current?.focus());
  }, []);

  const view: TopicView | null = useMemo(() => {
    if (!topic || !version) return null;
    const live: LiveRun | null = activeRun
      ? { operation: activeRun, progress: progressMatches(activeRun.operation_id) ? progress.progress : null, events: progressMatches(activeRun.operation_id) ? progress.events : [] }
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
      gotoClaim: (claimId) => {
        if (!inspector) setPanel(null);
        requestAnimationFrame(() => {
          const element = document.getElementById(`claim-${claimId}`);
          if (!element) return;
          element.scrollIntoView?.({ block: "center", behavior: prefersReducedMotion() ? "auto" : "smooth" });
          flash(element);
        });
      },
    };
    // progressMatches reads progress.progress, listed below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [topic, version, content, turns, citations, sources, activeRun, progress.progress, progress.events, turnNumbers, topicId, inspector]);

  const locked = activeRun !== null || startTurn.isPending;
  const cancelling = cancel.isPending || (cancel.isSuccess && cancel.variables?.operation_id === activeRun?.operation_id);

  const send = () => {
    const question = text.trim();
    if (!version || !question || locked || !optionsValid(options)) return;
    const body: ResearchV2.ResearchRunRequest = {
      parent_version_id: version.version_id,
      question_override: question,
      ...runRequestFields(options),
      ...(focus ? { focus_citation: { claim_id: focus.claimId, citation_index: focus.citationIndex } } : {}),
    };
    startTurn.mutate(body, {
      onSuccess: () => {
        setPendingAsk({ question, at: new Date().toISOString(), turnsAt: turns.length });
        setText("");
        setFocus(null);
        requestAnimationFrame(() => {
          const element = scrollRef.current;
          element?.scrollTo?.({ top: element.scrollHeight, behavior: prefersReducedMotion() ? "auto" : "smooth" });
        });
      },
    });
  };

  const startFirstTurn = () => {
    if (!version || locked || !optionsValid(options)) return;
    startTurn.mutate({ parent_version_id: version.version_id, ...runRequestFields(options) });
  };

  const exportVersion = async (kind: "full" | "metadata") => {
    if (!version) return;
    try {
      const data = kind === "full" ? await exportFull(topicId, version.version_id) : await exportMetadata(topicId, version.version_id);
      downloadJson(data, `foresightnine-${topicId}-${version.version_id}${kind === "metadata" ? "-metadata" : ""}.json`);
    } catch (error) {
      setNotice(error instanceof ApiError && error.status === 403 ? "有资料不允许导出，无法导出完整记录。可以只导出元数据。" : describeError(error));
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
  const turnActions = (question: string): TurnActions => ({
    recovery: recovery.data,
    checking: recovery.isLoading,
    busy: continueTurn.isPending || recoverTurn.isPending,
    locked,
    error: continueTurn.error ? describeError(continueTurn.error) : recoverTurn.error ? describeError(recoverTurn.error) : null,
    onContinue: () => { if (recoveryOperation) continueTurn.mutate(recoveryOperation); },
    onDeliver: () => { if (recoveryOperation) recoverTurn.mutate(recoveryOperation); },
    onAskAgain: () => prefill(question),
    onFillGap: lastGap ? () => prefill(`请继续补充上一轮的缺口：${gapText(lastGap)}`) : null,
  });

  const live = view?.live ?? null;
  const liveIndex = live?.progress?.turn.index ?? 0;
  const showLiveTurn = live !== null && (live.progress ? liveIndex > turns.length : pendingAsk !== null || turns.length === 0);
  const liveQuestion = (live?.progress?.turn.question || pendingAsk?.question || (turns.length === 0 ? topic?.request.question : "")) ?? "";

  const tokens: ComposerToken[] = focus
    ? [{ key: "focus", label: `深挖引用 ${focus.n}`, removeLabel: "移除深挖", onRemove: () => setFocus(null) }]
    : [];
  const suggestions = view && !locked ? suggestionsFor(content) : [];
  const versionLabel = version ? `v${topic!.versions.length} ${REVIEW_LABEL[version.review] ?? version.review}` : "";

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
            { key: "settings", label: "研究设置", icon: "tune", onSelect: () => setPanel({ kind: "settings" }) },
            "separator",
            { key: "export", label: "导出完整记录（JSON）", icon: "download", onSelect: () => void exportVersion("full"), disabled: !version },
            { key: "metadata", label: "仅导出元数据", icon: "download", onSelect: () => void exportVersion("metadata"), disabled: !version },
          ]}
        />
      </Toolbar>
      <NetworkBanner />
      {notice ? <div className="net-banner notice" role="alert">{notice}</div> : null}
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
                <div className="meta">
                  {topic!.request.as_of ? <><span>截止 {formatDateTimeWithOffset(topic!.request.as_of)}</span><span aria-hidden="true">·</span></> : null}
                  <span>{versionLabel}</span><span aria-hidden="true">·</span><span>{turns.length} 轮</span>
                </div>
              </header>
              {turns.map((turn, index) => (
                <TurnView
                  key={turn.turn_id}
                  turn={turn}
                  actions={index === turns.length - 1 && turn.status === "partial" && !orphanRun ? turnActions(turn.question) : null}
                />
              ))}
              {orphanRun && !activeRun ? (
                <InterruptedTurn
                  number={turns.length + 1}
                  startedAt={orphanRun.created_at}
                  question={(progressMatches(orphanRun.operation_id) ? progress.progress?.turn.question : "") || (turns.length === 0 ? topic!.request.question ?? "" : "")}
                  status={orphanRun.status === "cancelled" ? "cancelled" : "failed"}
                  actions={turnActions((progressMatches(orphanRun.operation_id) ? progress.progress?.turn.question : "") || topic!.request.question || "")}
                />
              ) : null}
              {showLiveTurn ? (
                <PendingTurn number={liveIndex > 0 ? liveIndex : turns.length + 1} startedAt={live!.progress?.started_at ?? live!.operation.created_at} question={liveQuestion} />
              ) : !live && pendingAsk ? (
                <PendingTurn number={turns.length + 1} startedAt={pendingAsk.at} question={pendingAsk.question} />
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
                <LiveBar live={live} cancelling={cancelling} onCancel={() => cancel.mutate(live.operation)} />
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
                label="追问"
                placeholder={locked ? "本轮研究进行中，可以先取消再提问" : "继续追问…"}
                submitLabel="发送"
                disabled={locked}
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
            <ResearchSettingsPanel mode="topic" options={options} asOfIso={topic!.request.as_of ?? null} scope={topic!.request.scope ?? ""} onApply={setOptions} onClose={() => setPanel(null)} />
          ) : null}
        </TopicContext.Provider>
      ) : null}
    </>
  );
}
