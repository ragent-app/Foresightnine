import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ApiError } from "../../api/http";
import { useSourceText } from "../../api/queries";
import { PERMISSION_LABEL, VERIFICATION_LABEL } from "../../lib/copy";
import { formatInteger } from "../../lib/format";
import { prefersReducedMotion } from "../../ui/media";
import { Panel, useKeyShortcut } from "../../ui/Panel";
import { useTopicView } from "./context";
import { DownloadOriginalButton, Permissions, sourceMeta } from "./RecordPanel";

type Range = readonly [number, number];
const CONTEXT = 2000;
const STEP = 4000;
const DEFAULT_CHECK = VERIFICATION_LABEL.numeric_and_location_match_semantics_not_verified ?? "";

function inferPageLabel(
  documentPages: { method?: string; pages?: [number, number, string][] } | null | undefined,
  offset: number | null,
): string | null {
  if (offset === null || !documentPages?.pages) return null;
  const isPptx = documentPages.method === "pptx_slides_v1";
  for (let i = 0; i < documentPages.pages.length; i += 1) {
    const entry = documentPages.pages[i];
    if (entry && offset >= entry[0] && offset < entry[1]) return isPptx ? `Slide ${i + 1}` : `第 ${i + 1} 页`;
  }
  return null;
}

/** Spec §4.5: source text with the current citation highlighted and other cited spans underlined. */
export function ReaderPanel({
  sourceId,
  range,
  locBadge,
  onClose,
}: {
  sourceId: string;
  range: Range | null;
  locBadge?: string | null;
  onClose: () => void;
}) {
  const view = useTopicView();
  const source = view.sources.get(sourceId);
  const text = useSourceText(view.topicId, view.version.version_id, sourceId, true);
  const forbidden = text.error instanceof ApiError && text.error.status === 403;

  // Every cited span in this source: claim citations and recorded facts, ordered by position.
  const cited = useMemo(() => {
    const seen = new Map<string, Range>();
    for (const claim of view.content.claims ?? []) {
      for (const citation of claim.citations ?? []) {
        if (citation.source_id !== sourceId) continue;
        const span: Range = [citation.location.character_start, citation.location.character_end];
        seen.set(span.join(":"), span);
      }
    }
    for (const fact of view.content.facts ?? []) {
      if (fact.source_id !== sourceId) continue;
      const span: Range = [fact.location.character_start, fact.location.character_end];
      seen.set(span.join(":"), span);
    }
    if (range) seen.set(range.join(":"), range);
    return [...seen.values()].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  }, [view.content, sourceId, range]);

  const initial = range ? cited.findIndex((span) => span[0] === range[0] && span[1] === range[1]) : -1;
  const [index, setIndex] = useState(initial);
  const current = index >= 0 ? cited[index] ?? null : null;

  const body = text.data?.text ?? "";
  const length = body.length;
  const currentOutOfBounds = Boolean(current && length > 0 && (current[0] >= length || current[1] <= 0));
  const [windowRange, setWindowRange] = useState<Range>([0, 0]);
  useEffect(() => {
    if (!length) return;
    if (current && !currentOutOfBounds) {
      setWindowRange([Math.max(0, current[0] - CONTEXT), Math.min(length, current[1] + CONTEXT)]);
    } else {
      setWindowRange([0, Math.min(length, STEP)]);
    }
  }, [current, currentOutOfBounds, length]);

  // Scroll once per citation change, after the text window containing it has rendered.
  const markRef = useRef<HTMLElement>(null);
  const pendingScroll = useRef(true);
  useEffect(() => { pendingScroll.current = true; }, [current]);
  useEffect(() => {
    if (!pendingScroll.current || !markRef.current) return;
    pendingScroll.current = false;
    markRef.current.scrollIntoView?.({ block: "center", behavior: prefersReducedMotion() ? "auto" : "smooth" });
  });

  const go = (delta: number) => setIndex((value) => Math.min(cited.length - 1, Math.max(0, (value < 0 ? (delta > 0 ? -1 : cited.length) : value) + delta)));
  useKeyShortcut({ j: () => go(1), k: () => go(-1) }, cited.length > 0);

  const [start, end] = windowRange;
  const content: ReactNode[] = [];
  if (length) {
    // Priority-ordered render segments: `current` highlight ALWAYS wins over overlapping background `cited` spans.
    type RenderSeg = { s: number; e: number; isCurrent: boolean; key: string };
    const segments: RenderSeg[] = [];
    const curStart = current && !currentOutOfBounds ? Math.max(start, Math.min(end, current[0])) : null;
    const curEnd = current && !currentOutOfBounds ? Math.max(start, Math.min(end, current[1])) : null;
    const hasActiveCur = curStart !== null && curEnd !== null && curEnd > curStart;
    if (hasActiveCur) {
      segments.push({ s: curStart, e: curEnd, isCurrent: true, key: `on:${current![0]}:${current![1]}` });
    }
    for (const span of cited) {
      if (current && span[0] === current[0] && span[1] === current[1]) continue;
      const s = Math.max(start, span[0]);
      const e = Math.min(end, span[1]);
      if (e <= s) continue;
      if (hasActiveCur) {
        if (s < curStart && e > curStart) {
          segments.push({ s, e: curStart, isCurrent: false, key: `c:${s}:${curStart}` });
          if (e > curEnd) segments.push({ s: curEnd, e, isCurrent: false, key: `c:${curEnd}:${e}` });
        } else if (s >= curStart && e <= curEnd) {
          continue; // Fully enclosed inside active current mark
        } else if (s < curEnd && e > curEnd) {
          segments.push({ s: curEnd, e, isCurrent: false, key: `c:${curEnd}:${e}` });
        } else {
          segments.push({ s, e, isCurrent: false, key: `c:${s}:${e}` });
        }
      } else {
        segments.push({ s, e, isCurrent: false, key: `c:${s}:${e}` });
      }
    }
    segments.sort((a, b) => a.s - b.s || (a.isCurrent ? -1 : 1));
    let position = start;
    for (const seg of segments) {
      if (seg.e <= position) continue;
      const segStart = Math.max(position, seg.s);
      if (segStart > position) content.push(body.slice(position, segStart));
      content.push(
        <mark key={seg.key} className={seg.isCurrent ? "on" : "cited"} ref={seg.isCurrent ? markRef : undefined}>
          {body.slice(segStart, seg.e)}
        </mark>,
      );
      position = seg.e;
    }
    if (position < end) content.push(body.slice(position, end));
  }

  const sha = text.data?.content_sha256 ?? source?.content_sha256 ?? "";
  const resolvedTitle = source?.title ?? text.data?.title ?? view.sourceTitle(sourceId);
  const effectiveLocBadge = locBadge ?? inferPageLabel(text.data?.document_pages, current ? current[0] : null);
  const fallbackPerms = !source ? text.data?.permissions : undefined;
  return (
    <Panel
      title={resolvedTitle}
      onClose={onClose}
      detents
      footerClassName="split"
      footer={
        <div className="actions-row">
          <button type="button" className="btn" onClick={() => go(-1)} disabled={cited.length === 0 || index === 0} title="上一处（K）">上一处</button>
          <button type="button" className="btn" onClick={() => go(1)} disabled={cited.length === 0 || index === cited.length - 1} title="下一处（J）">下一处</button>
          {source ? <DownloadOriginalButton source={source} /> : null}
        </div>
      }
    >
      <div className="list">
        <div className="item">
          {source ? <span className="meta">{sourceMeta(source)}</span> : text.data?.retrieval_method ? <span className="meta">跨期 / 账号级资料 · {text.data.retrieval_method}</span> : null}
          {source ? <Permissions source={source} /> : fallbackPerms ? (
            <span className="meta">
              {(["store", "model_input", "display", "export"] as const)
                .map((k) => `${PERMISSION_LABEL[k]}${fallbackPerms[k] ? "允许" : "禁止"}`)
                .join(" · ")}
            </span>
          ) : null}
          <span className="meta">
            {effectiveLocBadge ? <><span className="v3a-loc-pill">{effectiveLocBadge}</span>{" "}</> : null}
            {currentOutOfBounds
              ? "引文字符偏移超出当前原文长度，已定位到文档起始处"
              : current
                ? `当前引文：字符 ${current[0]}–${current[1]} · ${DEFAULT_CHECK}`
                : `已标出 ${cited.length} 处引用`}
            {sha ? <> · <span className="mono">sha256 {sha.slice(0, 8)} {sha.slice(8, 16)}…</span></> : null}
          </span>
        </div>
      </div>
      {text.isLoading ? <><div className="skeleton line" /><div className="skeleton line" /><div className="skeleton line short" /></> : null}
      {forbidden ? <p className="empty-note">这份资料不允许展示原文。</p> : null}
      {text.isError && !forbidden ? <p className="empty-note">原文暂时无法加载。</p> : null}
      {length ? (
        <>
          {start > 0 ? <button type="button" className="link" onClick={() => setWindowRange([Math.max(0, start - STEP), end])}>加载前文（前面还有 {formatInteger(start)} 字符）</button> : null}
          <div className="reader-text">{content}</div>
          {end < length ? <button type="button" className="link" onClick={() => setWindowRange([start, Math.min(length, end + STEP)])}>加载更多（后面还有 {formatInteger(length - end)} 字符）</button> : null}
        </>
      ) : null}
    </Panel>
  );
}
