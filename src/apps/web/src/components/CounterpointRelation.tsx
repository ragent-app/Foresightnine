import type { ResearchClaim } from "../general-research-client";
import { Button, Select } from "./Controls";

interface Props {
  claims: ResearchClaim[];
  index: number;
  editable: boolean;
  showSuggestions: boolean;
  en: boolean;
  onTargetsChange: (targets: string[]) => void;
}

export function CounterpointRelation({ claims, index, editable, showSuggestions, en, onTargetsChange }: Props) {
  const counterpoint = claims[index];
  if (!counterpoint || counterpoint.role !== "counterargument") return null;
  const targets = counterpoint.responds_to_claim_ids ?? [];
  const suggestions = showSuggestions ? counterpoint.suggested_responds_to_claim_ids ?? [] : [];
  const findings = claims.filter((item) => item.role !== "counterargument" && typeof item.claim_id === "string");
  if (!editable && !targets.length && !suggestions.length && !counterpoint.responds_to_omitted) return null;
  return <div className="general-source">
    <p>{en ? "Related findings (not independently verified)" : "关联结论（关系未经独立核验）"}</p>
    {targets.length ? <ul>{targets.map((id) => {
      const found = findings.find((item) => item.claim_id === id);
      return <li key={id}>{found?.text ?? (en ? "Finding unavailable" : "关联结论不可用")}{editable ? <Button onClick={() => onTargetsChange(targets.filter((value) => value !== id))}>{en ? "Remove link" : "移除关联"}</Button> : null}</li>;
    })}</ul> : <p>{en ? "No finding linked." : "尚未关联结论。"}</p>}
    {suggestions.length ? <><p>{en ? "Model-suggested links (not verified):" : "模型建议的关联（尚未核验）："}</p><ul>{suggestions.map((id) => {
      const found = findings.find((item) => item.claim_id === id);
      return <li key={id}>{found?.text ?? (en ? "Finding unavailable" : "关联结论不可用")}{editable && found && !targets.includes(id) && targets.length < 20 ? <Button onClick={() => onTargetsChange([...targets, id])}>{en ? "Keep link" : "保留关联"}</Button> : null}</li>;
    })}</ul></> : null}
    {counterpoint.responds_to_omitted ? <p role="status">{en ? "Some invalid or unavailable links were omitted when this version was saved." : "保存时已省略部分无效或不可用的关联。"}</p> : null}
    {editable ? <label>{en ? "Link a finding" : "关联一条结论"}<Select value="" disabled={targets.length >= 20 || !findings.length} onChange={(event) => {
      const id = event.target.value;
      if (id && !targets.includes(id)) onTargetsChange([...targets, id]);
    }}><option value="">{en ? "Select a finding" : "选择结论"}</option>{findings.filter((item) => item.claim_id && !targets.includes(item.claim_id)).map((item) => <option key={item.claim_id} value={item.claim_id}>{item.text}</option>)}</Select></label> : null}
    {editable && !findings.length ? <p>{en ? "Save the findings first to assign their IDs." : "请先保存结论以生成标识。"}</p> : null}
  </div>;
}
