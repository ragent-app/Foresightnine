import { useState } from "react";
import type { SourceMetadata } from "../general-research-client";
import { Button, Field, Textarea } from "./Controls";
import { SourceTextSearch } from "./SourceTextSearch";
import { canUseCitationSource } from "../source-text-search";

interface Props {
  sources: SourceMetadata[];
  disabled: boolean;
  en: boolean;
  asOf?: string | null | undefined;
  mode?: "add" | "replace";
  onAdd: (sourceId: string, quotation: string) => void;
  loadSource: (sourceId: string) => Promise<{ source_id: string; text?: string }>;
}

/** A selected quotation is located by the server when the draft is saved. */
export function CitationEditor({ sources, disabled, en, asOf, mode = "add", onAdd, loadSource }: Props) {
  const [sourceId, setSourceId] = useState("");
  const [quotation, setQuotation] = useState("");
  const available = sources.filter((source) => canUseCitationSource(source, asOf));
  if (!available.length) return <p>{en ? "Attach displayable material available by the research cutoff before adding a quotation." : "请先关联允许展示且符合研究截止时间的资料，再添加引文。"}</p>;
  return <details className="general-source">
    <summary>{mode === "replace" ? (en ? "Replace figure quotation" : "替换数值来源引文") : (en ? "Add a source quotation" : "添加来源引文")}</summary>
    <p>{en ? "Paste exact, continuous source text. Saving locates the quotation and checks the research cutoff, but does not verify support." : "粘贴连续的原文。保存时会定位引文并检查研究截止时间，但不会自动证明它支持结论。"}</p>
    <Field label={en ? "Quotation source" : "引文来源"}><select className="text-input select-input" value={sourceId} disabled={disabled} onChange={(event) => setSourceId(event.target.value)}>
      <option value="">{en ? "Select material" : "选择资料"}</option>
      {available.map((source) => <option key={source.source_id} value={source.source_id}>{source.title || source.source_id}</option>)}
    </select></Field>
    {sourceId && available.some((source) => source.source_id === sourceId) ? <SourceTextSearch key={sourceId} sourceId={sourceId} en={en} disabled={disabled} load={loadSource} onSelect={setQuotation} /> : null}
    <Field label={en ? "Exact quotation" : "原文引文"}><Textarea maxLength={1000} value={quotation} disabled={disabled} onChange={(event) => setQuotation(event.target.value)} /></Field>
    <Button disabled={disabled || !sourceId || !quotation.trim() || !available.some((source) => source.source_id === sourceId)} onClick={() => { onAdd(sourceId, quotation); setQuotation(""); }}>
      {mode === "replace" ? (en ? "Use quotation for figure" : "将引文用于此数值") : (en ? "Add quotation" : "添加引文")}
    </Button>
  </details>;
}
