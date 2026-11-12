import { useState } from "react";
import type { SourceMetadata } from "../general-research-client";
import { Button, Field, Textarea } from "./Controls";

interface Props {
  sources: SourceMetadata[];
  disabled: boolean;
  en: boolean;
  onAdd: (sourceId: string, quotation: string) => void;
}

/** A selected quotation is located by the server when the draft is saved. */
export function CitationEditor({ sources, disabled, en, onAdd }: Props) {
  const [sourceId, setSourceId] = useState("");
  const [quotation, setQuotation] = useState("");
  const available = sources.filter((source) => source.status === "available" && source.permissions?.display);
  if (!available.length) return <p>{en ? "Attach displayable material before adding a quotation." : "请先关联允许展示的资料，再添加引文。"}</p>;
  return <details className="general-source">
    <summary>{en ? "Add a source quotation" : "添加来源引文"}</summary>
    <p>{en ? "Paste exact, continuous source text. Saving locates the quotation and checks the research cutoff, but does not verify support." : "粘贴连续的原文。保存时会定位引文并检查研究截止时间，但不会自动证明它支持结论。"}</p>
    <Field label={en ? "Quotation source" : "引文来源"}><select className="text-input select-input" value={sourceId} disabled={disabled} onChange={(event) => setSourceId(event.target.value)}>
      <option value="">{en ? "Select material" : "选择资料"}</option>
      {available.map((source) => <option key={source.source_id} value={source.source_id}>{source.title || source.source_id}</option>)}
    </select></Field>
    <Field label={en ? "Exact quotation" : "原文引文"}><Textarea maxLength={1000} value={quotation} disabled={disabled} onChange={(event) => setQuotation(event.target.value)} /></Field>
    <Button disabled={disabled || !sourceId || !quotation.trim() || !available.some((source) => source.source_id === sourceId)} onClick={() => { onAdd(sourceId, quotation); setQuotation(""); }}>
      {en ? "Add quotation" : "添加引文"}
    </Button>
  </details>;
}
