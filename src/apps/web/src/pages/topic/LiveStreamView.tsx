import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import type { TransientStreamState } from "../../api/types";
import { RETRACTED_SUBMISSION_NOTICE } from "../../api/queries";
import { PROCESS_NOTE_LABEL } from "../../lib/copy";
import { formatInteger } from "../../lib/format";
import { splitStableMarkdownBlocks } from "../../lib/markdownStream";
import { Icon } from "../../ui/icons";
import { MarkdownContent } from "../../ui/MarkdownContent";
import type { LiveStreamStore } from "./context";

const EMPTY_STREAM: TransientStreamState = {
  callSeq: null,
  thinking: "",
  text: "",
  toolStatus: null,
  draftReport: "",
  activeChannel: null,
  retractedNotice: null,
  lastSeq: -1,
};

const noopSubscribe = () => () => undefined;
const emptySnapshot = () => EMPTY_STREAM;

export function useLiveStream(stream?: LiveStreamStore | undefined): TransientStreamState {
  return useSyncExternalStore(
    stream ? stream.subscribe : noopSubscribe,
    stream ? stream.getSnapshot : emptySnapshot,
    emptySnapshot,
  );
}

/** Whether a draft is on screen; re-renders only when that changes, not on every token. */
export function useHasDraft(stream?: LiveStreamStore | undefined): boolean {
  return useSyncExternalStore(
    stream ? stream.subscribe : noopSubscribe,
    () => {
      const state = stream ? stream.getSnapshot() : EMPTY_STREAM;
      return Boolean(state.draftReport.trim() || state.heldDraft?.trim());
    },
    () => false,
  );
}

/**
 * The report as it is being written, in the place the answer will appear. A draft sent back by
 * review stays readable while the writer revises it, instead of disappearing.
 */
export function LiveDraftPreview({ stream, onGrow }: { stream?: LiveStreamStore | undefined; onGrow?: (() => void) | undefined }) {
  const state = useLiveStream(stream);
  const writing = state.draftReport.trim().length > 0;
  const held = !writing && (state.heldDraft ?? "").trim().length > 0;
  const text = writing ? state.draftReport : held ? state.heldDraft ?? "" : "";
  const blockCount = useMemo(() => {
    if (!text) return 0;
    const split = splitStableMarkdownBlocks(text);
    return split.stableBlocks.length + (split.activeTail.trim() ? 1 : 0);
  }, [text]);
  const onGrowRef = useRef(onGrow);
  onGrowRef.current = onGrow;

  useEffect(() => {
    if (blockCount > 0) onGrowRef.current?.();
  }, [blockCount]);

  if (!text) return null;

  const streamingDraft = writing && state.activeChannel === "draft_report";
  return (
    <div className={`draft-preview${held ? " is-held" : ""}`} role="region" aria-label="报告草稿">
      <p className="draft-state" role="status">
        {held ? RETRACTED_SUBMISSION_NOTICE : streamingDraft ? "正在写报告 · 写完后会核对引文和数字" : "报告草稿 · 正在核对引文和数字"}
      </p>
      <div className="report draft-report">
        <MarkdownContent
          content={text}
          variant="draft"
          streaming={writing}
          showCursor={streamingDraft}
        />
      </div>
    </div>
  );
}

function hasStepStreamContent(state: TransientStreamState): boolean {
  return Boolean(
    state.thinking.length > 0
      || state.text.length > 0
      || state.toolStatus
      || state.retractedNotice
      || state.draftReport.length > 0,
  );
}

export function LiveThinkingBlock({
  thinking,
  callSeq,
  streaming,
}: {
  thinking: string;
  callSeq: number | null;
  streaming: boolean;
}) {
  const hasThinking = thinking.length > 0;
  const [open, setOpen] = useState(true);
  const bodyRef = useRef<HTMLDivElement>(null);
  const stickBottomRef = useRef(true);

  useEffect(() => {
    if (hasThinking) {
      setOpen(true);
      stickBottomRef.current = true;
    }
  }, [hasThinking, callSeq]);

  useEffect(() => {
    const el = bodyRef.current;
    if (!el || !open || !stickBottomRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [thinking, open]);

  if (!hasThinking) return null;

  const onScroll = () => {
    const el = bodyRef.current;
    if (!el) return;
    stickBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight <= 24;
  };

  return (
    <div className="ls-thinking">
      <div className="ls-thinking-head">
        <span className="ls-thinking-label">模型思考过程 · 非正式结论</span>
        <span className="meta ls-char-count">{`约 ${formatInteger(thinking.length)} 字`}</span>
        <button
          type="button"
          className="link ls-thinking-toggle"
          aria-expanded={open}
          aria-label={open ? "收起模型思考过程" : "展开模型思考过程"}
          onClick={() => setOpen((value) => !value)}
        >
          {open ? "收起思考" : "展开思考"}
          <Icon name={open ? "up" : "down"} size={13} />
        </button>
      </div>
      {open ? (
        <div ref={bodyRef} className="ls-thinking-body" onScroll={onScroll}>
          {thinking}
          {streaming ? <span className="stream-cursor" aria-hidden="true" /> : null}
        </div>
      ) : null}
    </div>
  );
}

function LiveStepContent({ state }: { state: TransientStreamState }) {
  const draftChars = state.draftReport.length;
  const showToolStatus = Boolean(state.toolStatus) || draftChars > 0;
  const statusLine = state.toolStatus
    ? (draftChars > 0 ? `${state.toolStatus}（已生成约 ${formatInteger(draftChars)} 字）` : state.toolStatus)
    : draftChars > 0
      ? `正在起草研究报告并整理引文，完成后将自动核验原文位置…（已生成约 ${formatInteger(draftChars)} 字）`
      : null;

  return (
    <div className="ls-step">
      {state.retractedNotice && !state.heldDraft ? <p className="ls-retracted">{state.retractedNotice}</p> : null}
      {showToolStatus && statusLine ? <p className="ls-tool-status">{statusLine}</p> : null}
      <LiveThinkingBlock
        thinking={state.thinking}
        callSeq={state.callSeq}
        streaming={state.activeChannel === "thinking"}
      />
      {state.text.length > 0 ? (
        <div className="ls-text-block">
          <span className="pt-note-label">{PROCESS_NOTE_LABEL}</span>
          <div className="ls-process-text">
            <MarkdownContent
              content={state.text}
              variant="inline"
              streaming
              showCursor={state.activeChannel === "text"}
            />
          </div>
        </div>
      ) : null}
    </div>
  );
}

/** Renders live transient thinking / process text / tool status inside the active step row. */
export function LiveActiveStream({ stream }: { stream?: LiveStreamStore | undefined }) {
  const state = useLiveStream(stream);
  if (!hasStepStreamContent(state)) return null;
  return <LiveStepContent state={state} />;
}

/** Renders a transient active step row inside ProcessTimeline when `current` is momentarily null. */
export function LiveActiveStreamRow({ stream }: { stream?: LiveStreamStore | undefined }) {
  const state = useLiveStream(stream);
  if (!hasStepStreamContent(state)) return null;
  return (
    <li className="pt-row pt-current pt-stream-row">
      <span className="pt-ic" aria-hidden="true"><span className="dot running" /></span>
      <div className="pt-main">
        <div className="pt-head">
          <span className="pt-label">模型输出中</span>
          <span className="role light neutral">进行中</span>
        </div>
        <LiveStepContent state={state} />
      </div>
    </li>
  );
}

/** Shown inside LiveTurn before any committed events or `current` step arrive. */
export function LiveEmptyOrStream({ stream, idleText, inPanel = false }: {
  stream?: LiveStreamStore | undefined;
  idleText: string;
  inPanel?: boolean | undefined;
}) {
  const state = useLiveStream(stream);
  if (!hasStepStreamContent(state)) {
    return <p className={inPanel ? "empty-note" : "meta live-turn-empty"}>{idleText}</p>;
  }
  return (
    <ol className={`pt-list${inPanel ? " in-panel" : ""}`} aria-label="研究过程时间线">
      <li className="pt-row pt-current pt-stream-row">
        <span className="pt-ic" aria-hidden="true"><span className="dot running" /></span>
        <div className="pt-main">
          <div className="pt-head">
            <span className="pt-label">模型输出中</span>
            <span className="role light neutral">进行中</span>
          </div>
          <LiveStepContent state={state} />
        </div>
      </li>
    </ol>
  );
}
