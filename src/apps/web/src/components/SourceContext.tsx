import { useEffect, useState } from "react";
import type { SourceContextResult, SourceLocation } from "../general-research-client";
import { Button } from "./Controls";

interface Props {
  sourceId: string;
  location?: SourceLocation | undefined;
  quotation?: string | undefined;
  en: boolean;
  candidate?: number | undefined;
  onUseContext?: ((quotation: string) => void) | undefined;
  onAddSelected?: ((quotation: string) => void) | undefined;
  load: (sourceId: string, start: number, end: number) => Promise<SourceContextResult>;
}

export function SourceContext({ sourceId, location, quotation, en, load, candidate, onUseContext, onAddSelected }: Props) {
  const [open, setOpen] = useState(false);
  const [context, setContext] = useState<SourceContextResult | null>(null);
  const [failed, setFailed] = useState(false);
  const start = location?.character_start;
  const end = location?.character_end;
  const valid = typeof start === "number" && typeof end === "number" &&
    Number.isSafeInteger(start) && Number.isSafeInteger(end) && start >= 0 && end > start && end - start <= 1000;

  useEffect(() => {
    if (!open || !valid || start === undefined || end === undefined) return;
    let active = true;
    setContext(null); setFailed(false);
    void load(sourceId, start, end).then((value) => { if (active) setContext(value); })
      .catch(() => { if (active) setFailed(true); });
    return () => { active = false; };
  }, [open, valid, start, end, sourceId, load]);

  if (location?.status === "ambiguous" && Array.isArray(location.matches)) return <div className="source-context">
    <p>{en ? "This quotation has multiple matching locations. Compare the candidates." : "这段引文有多个匹配位置，请对比候选位置。"}</p>
    {location.matches.slice(0, 5).map((match, index) => match && typeof match === "object" ? <SourceContext key={`${match.character_start}:${match.character_end}:${index}`}
      sourceId={sourceId} location={{ ...(typeof match.character_start === "number" ? { character_start: match.character_start } : {}),
        ...(typeof match.character_end === "number" ? { character_end: match.character_end } : {}),
        ...(typeof match.page === "number" ? { page: match.page } : {}) }} quotation={quotation} en={en} load={load} candidate={index + 1} onUseContext={onUseContext} /> : null)}
    {location.truncated ? <p>{en ? "Additional matches were omitted." : "还有未显示的匹配位置。"}</p> : null}
  </div>;
  if (!valid) return null;
  return <div className="source-context">
    <Button aria-expanded={open} onClick={() => setOpen((value) => !value)}>{candidate ? (open ? (en ? `Close location ${candidate}` : `收起候选位置 ${candidate}`) : (en ? `View location ${candidate}` : `查看候选位置 ${candidate}`)) : onAddSelected ? (open ? (en ? "Close suggested passage" : "收起候选原文") : (en ? "View suggested passage" : "查看候选原文")) : open ? (en ? "Close source context" : "收起原文上下文") : (en ? "View source context" : "查看原文上下文")}</Button>
    {open && failed ? <p role="status">{en ? "Source context is unavailable or permission has changed." : "无法查看原文上下文，资料可能不可用或权限已变化。"}</p> : null}
    {open && !failed && !context ? <p role="status">{en ? "Loading source context…" : "正在读取原文上下文…"}</p> : null}
    {open && context ? <div>
      <p>{context.title}{context.page_start !== null ? ` · ${en ? "page" : "第"} ${context.page_start}${context.page_end !== context.page_start ? `–${context.page_end}` : ""}${en ? "" : " 页"}` : ""}</p>
      {quotation === undefined ? <p role="status">{onAddSelected ? (en ? "This is a suggested source passage. Check its relevance before adding it." : "这是候选原文，请核对相关性后再添加。") : (en ? "No saved quotation is available for comparison." : "没有可供比对的已保存引文。")}</p> : quotation !== context.selected ? <p role="status">{en ? "The saved quotation does not match the selected source text." : "保存的引文与所选原文位置不一致。"}</p> : <p>{en ? "The quotation matches this text; its support for the finding is not independently verified." : "引文与此处原文一致；是否支持结论尚未独立核验。"}</p>}
      <pre>{context.before}<mark>{context.selected}</mark>{context.after}</pre>
      {onAddSelected && context.selected.trim() && Array.from(context.selected).length <= 1000 ? <Button onClick={() => onAddSelected(context.selected)}>{en ? "Add selected passage as quotation" : "添加所选原文作为引文"}</Button> : null}
      {candidate && onUseContext && quotation === context.selected && Array.from(context.before + context.selected + context.after).length <= 1000 ? <>
        <p>{en ? "You can use the displayed context as a quotation. Check that it supports the finding; saving will check its location again." : "可将显示的上下文作为引文。请核对其是否支持结论；保存时会重新检查位置。"}</p>
        <Button onClick={() => onUseContext(context.before + context.selected + context.after)}>{en ? "Use this context as quotation" : "使用此上下文作为引文"}</Button>
      </> : null}
    </div> : null}
  </div>;
}
