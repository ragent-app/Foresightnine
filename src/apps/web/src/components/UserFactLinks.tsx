import type { ResearchClaim, ResearchFact } from "../general-research-client";
import { Button, Select } from "./Controls";

type Link = NonNullable<ResearchClaim["user_fact_links"]>[number];
interface Props { claim: ResearchClaim; facts: ResearchFact[]; editable: boolean; en: boolean; onChange: (links: Link[]) => void }

export function UserFactLinks({ claim, facts, editable, en, onChange }: Props) {
  const links = claim.user_fact_links ?? [];
  const unique = new Map<string, ResearchFact>();
  const duplicates = new Set<string>();
  for (const fact of facts) {
    if (unique.has(fact.fact_id)) duplicates.add(fact.fact_id);
    else unique.set(fact.fact_id, fact);
  }
  const available = [...unique.values()].filter((fact) => !duplicates.has(fact.fact_id));
  if (!editable && !links.length && !claim.user_fact_links_omitted) return null;
  return <div className="general-source">
    <p>{en ? "Your figure links (not independently verified)" : "你关联的数值（未经独立核验）"}</p>
    <p>{en ? "A link records your assessment, not proof that the figure supports or contradicts the finding. Save changes to findings and figures before adding links." : "关联记录你的判断，并不证明数值支持或反驳结论。请先保存结论和数值的修改，再添加关联。"}</p>
    {links.length ? <ul>{links.map((link) => {
      const fact = unique.get(link.fact_id);
      return <li key={`${link.fact_id}:${link.relation}`}>{link.relation === "support" ? (en ? "Supports" : "支持") : (en ? "Opposes" : "反对")}: {fact ? `${fact.label} · ${fact.value} ${fact.unit} · ${fact.period}` : link.fact_id}
        {editable ? <Button onClick={() => onChange(links.filter((item) => item.fact_id !== link.fact_id || item.relation !== link.relation))}>{en ? "Remove" : "移除"}</Button> : null}</li>;
    })}</ul> : <p>{en ? "No figure links recorded." : "尚未关联数值。"}</p>}
    {claim.user_fact_links_omitted ? <p role="status">{en ? "Some links were omitted because a finding or figure changed, or a reference was invalid." : "部分关联因结论或数值变化、引用无效而未保留。"}</p> : null}
    {editable ? (["support", "oppose"] as const).map((relation) => <label key={relation}>{relation === "support" ? (en ? "Link a supporting figure" : "关联支持数值") : (en ? "Link an opposing figure" : "关联反对数值")}
      <Select value="" disabled={links.length >= 20} onChange={(event) => {
        const id = event.target.value;
        if (id && !links.some((item) => item.fact_id === id && item.relation === relation)) onChange([...links, { fact_id: id, relation }]);
      }}><option value="">{en ? "Select a saved figure" : "选择已保存数值"}</option>{available.filter((fact) => !links.some((item) => item.fact_id === fact.fact_id && item.relation === relation)).map((fact) => <option key={fact.fact_id} value={fact.fact_id}>{fact.label} · {fact.value} {fact.unit} · {fact.period}</option>)}</Select>
    </label>) : null}
  </div>;
}
