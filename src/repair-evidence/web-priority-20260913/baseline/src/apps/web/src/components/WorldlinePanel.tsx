import { Button, StatusDot } from "./Controls";
import { Header, Disclosure } from "./AppShell";
import { t, useLanguage } from "../i18n";
import type { BranchView } from "../types";

interface WorldlineProps { readonly branches: readonly BranchView[]; readonly selected: BranchView["kind"]; readonly onSelect: (kind: BranchView["kind"]) => void; readonly onCompute: () => void; readonly canCompute?: boolean; }

export function WorldlinePanel({ branches, selected, onSelect, onCompute, canCompute = false }: WorldlineProps) {
  const { language } = useLanguage();
  const active = branches.find((branch) => branch.kind === selected) ?? branches[1]!;
  const activePathLabel = language === "en" ? ({ weak: "Weak W−", main: "Main W₀", strong: "Strong W+" } as const)[active.kind] : active.title;
  const firstPostBaselineReveal = active.worldline.event_reveal_schedule.find((item) => item.event_date > active.worldline.baseline_date)?.reveal_date ?? "—";
  return <section className="content-column worldline-view">
    <Header title={t(language, "世界线与条件概率")} action={<span className="model-label">{t(language, "区间优先 · 当前未校准")}</span>} />
    <div className="probability-row"><Probability label={`${t(language, "前提成立")} P(H)`} value={probabilityRange(active.assessment.premise_probability_range)} /><Probability label={`${activePathLabel} P(W | H)`} value={probabilityRange(active.assessment.conditional_probability_range)} /><Probability label={`${activePathLabel} P(H ∩ W)`} value={probabilityRange(active.assessment.joint_probability_range)} /></div>
    <p className="calibration-line"><StatusDot tone="warning" /> {t(language, "当前状态：")}<code>model_estimate_unvalidated</code></p>
    <div className="premise-panel"><strong>{t(language, "当前 premise")}</strong><p>{active.worldline.premise}</p><small>{t(language, "该前提来自本次用户输入；三条分支共享它，只改变传导路径。")}</small></div>
    <div className="worldline-canvas" role="group" aria-label={t(language, "弱、主、强三条世界线")}>
      <div className="timeline-axis"><span>{t(language, "现在")}</span><span>{t(language, "6个月")}</span><span>{t(language, "12个月")}</span><span>{t(language, "24个月")}</span></div>
      {branches.map((branch) => <button key={branch.kind} className={`worldline-path path-${branch.kind} ${selected === branch.kind ? "is-selected" : ""}`} onClick={() => onSelect(branch.kind)} aria-pressed={selected === branch.kind}><span className="path-copy"><strong>{t(language, branch.title)}</strong><small>{t(language, branch.description)}</small><em>{t(language, "条件概率 P(W | H)")} <b>{branch.range}</b></em></span><span className="path-track">{branch.nodes.map((node, index) => <span className="path-node" key={node}><i>{index + 1}</i><small>{t(language, node)}</small></span>)}</span></button>)}
    </div>
    <div className="worldline-footer"><div><strong>{t(language, active.title)}</strong><p>{t(language, "事件发生时间与市场可见时间分开记录。首个揭示：")}{firstPostBaselineReveal}{t(language, "。")}</p></div><Button variant="secondary" onClick={() => onSelect(active.kind)}>{t(language, "确认选择")}</Button><Button variant="primary" disabled={!canCompute} onClick={() => { if (canCompute) onCompute(); }}>{t(language, "进入因子计算")} <span aria-hidden="true">→</span></Button></div>
    <div className="invalidation"><strong>{t(language, "失效条件")}</strong><span>{t(language, active.invalidation)}</span><span>{t(language, "世界线分支可以修订，概率不是事实。")}</span></div>
    <Disclosure compact />
  </section>;
}

function probabilityRange(range: { readonly minimum: number; readonly maximum: number }): string { return `${(range.minimum * 100).toFixed(2)}–${(range.maximum * 100).toFixed(2)}%`; }
function Probability({ label, value }: { readonly label: string; readonly value: string }) { return <div className="probability"><span>{label}</span><strong>{value}</strong><small>model_estimate_unvalidated</small></div>; }
