import { Button } from "./Controls";
import { Header, Disclosure } from "./AppShell";
import { t, useLanguage } from "../i18n";
import { legacyLiveScenarios } from "../scenario-status";
import type { BranchView } from "../types";

interface WorldlineProps {
  readonly branches: readonly BranchView[];
  readonly selected: BranchView["kind"];
  readonly onSelect: (kind: BranchView["kind"]) => void;
  readonly onCompute: () => void;
  readonly canCompute?: boolean;
  readonly generating?: boolean;
  readonly onRegenerate?: (() => void) | undefined;
}

export function WorldlinePanel({ branches, selected, onSelect, onCompute, canCompute = false, generating = false, onRegenerate }: WorldlineProps) {
  const { language } = useLanguage();
  const en = language === 'en';
  const active = branches.find((branch) => branch.kind === selected) ?? branches[1]!;
  const legacy = legacyLiveScenarios(branches);
  const labels = en ? {weak:'Weak transmission',main:'Central transmission',strong:'Strong transmission'} : {weak:'弱传导',main:'主路径',strong:'强传导'};
  const firstReveal = [...active.worldline.event_reveal_schedule]
    .filter(item => item.event_date > active.worldline.baseline_date)
    .map(item => item.reveal_date).sort()[0];
  const eligible = canCompute && !legacy && !generating;
  return <section className="content-column worldline-view">
    <Header title={t(language, "世界线与条件概率")} action={<span className="model-label">{en ? 'Model estimates · uncalibrated' : '模型估计 · 未校准'}</span>} />
    {legacy ? <div className="scenario-upgrade" role="status">
      <strong>{en ? 'Earlier draft' : '旧版草稿'}</strong>
      <p>{en ? 'Regenerate with the premise held true before continuing. Your review and evidence are retained.' : '请重新生成，使三条路径都以此前提成立为条件。审查与证据会保留。'}</p>
      {onRegenerate ? <Button variant="primary" onClick={onRegenerate} disabled={generating}>{generating ? (en ? 'Generating…' : '正在生成…') : (en ? 'Regenerate scenarios' : '重新生成世界线')}</Button> : null}
    </div> : <>
      <div className="probability-row">
        <Probability label={`${en ? 'Premise' : '前提成立'} P(H)`} value={probabilityRange(active.assessment.premise_probability_range)} />
        <Probability label={`${labels[active.kind]} P(W | H)`} value={probabilityRange(active.assessment.conditional_probability_range)} />
        <Probability label={`${en ? 'Joint occurrence' : '同时发生'} P(H ∩ W)`} value={probabilityRange(active.assessment.joint_probability_range)} />
      </div>
      <p className="scenario-assumption">{en ? 'All three paths assume the premise holds. Events below are hypothetical checkpoints, not observed facts.' : '三条路径均假定研究前提成立；以下节点是假设检查点，并非已发生事实。'}</p>
    </>}
    <details className="scenario-premise"><summary>{en ? 'Research premise' : '研究前提'}</summary><p>{active.worldline.premise}</p></details>
    <div className="scenario-window"><span>{en ? 'Scenario start' : '推演起点'} {active.worldline.baseline_date}</span><span>{en ? 'Horizon' : '截至'} {active.worldline.horizon_end}</span></div>
    <div className="scenario-list" role="group" aria-label={t(language, "弱、主、强三条世界线")}>
      {branches.map(branch => <button key={branch.kind} type="button" className={`scenario-card path-${branch.kind} ${selected === branch.kind ? 'is-selected' : ''}`} onClick={() => onSelect(branch.kind)} aria-pressed={selected === branch.kind}>
        <span className="scenario-summary"><span className="scenario-kind">{labels[branch.kind]}{selected === branch.kind ? (en ? ' · Selected' : ' · 已选择') : ''}</span><strong>{t(language, branch.title)}</strong>
          {!legacy ? <span className="scenario-range">P(W | H) {probabilityRange(branch.assessment.conditional_probability_range)}</span> : null}
        </span>
        <span className="scenario-events">{branch.worldline.nodes.map((node,index) => <span className="scenario-event" key={node.node_id}><i aria-hidden="true">{index+1}</i><span><time dateTime={node.event_date}>{node.event_date}</time><span className="scenario-event-copy">{node.event}</span></span></span>)}</span>
      </button>)}
    </div>
    <div className="worldline-footer"><div><strong>{active.title}</strong>{firstReveal ? <p>{en ? 'First scheduled check: ' : '首个计划核验日：'}{firstReveal}</p> : null}</div><Button variant="primary" disabled={!eligible} onClick={() => { if (eligible) onCompute(); }}>{t(language, "进入因子计算")} <span aria-hidden="true">→</span></Button></div>
    <details className="scenario-detail"><summary>{en ? 'Impacts and invalidation conditions' : '资产影响与失效条件'}</summary><p>{active.description}</p><strong>{t(language, "失效条件")}</strong><p>{active.invalidation}</p></details>
    <Disclosure compact />
  </section>;
}

function probabilityRange(range: { readonly minimum: number; readonly maximum: number }): string { return `${(range.minimum * 100).toFixed(2)}–${(range.maximum * 100).toFixed(2)}%`; }
function Probability({ label, value }: { readonly label: string; readonly value: string }) { return <div className="probability"><span>{label}</span><strong>{value}</strong></div>; }

/** Explain this result, rather than presenting generic evidence categories as
 * though those sources had actually been consulted. */
export function WorldlineExplanation({ branch }: {readonly branch?: BranchView | undefined}) {
  const { language } = useLanguage();
  const en = language === 'en';
  return <div className="inspector-content"><h2>{en ? 'Model reasoning' : '模型解释'}</h2>
    {!branch ? <p>{en ? 'Select a generated path to see its reasoning.' : '生成并选择路径后查看依据。'}</p>
      : legacyLiveScenarios([branch]) ? <p>{en ? 'Regenerate the earlier draft first.' : '请先重新生成旧版草稿。'}</p>
      : <>{branch.assessment.adjustment_reasons.map((reason,index) => <p className="inspector-line" key={index}>{reason}</p>)}
        <p className="inspector-callout">{branch.assessment.base_rate_refs.length === 0 ? (en ? 'No base-rate sources attached.' : '未附基准率来源。') : `${en ? 'Referenced sources' : '参考来源'}: ${branch.assessment.base_rate_refs.length}`}</p></>}
  </div>;
}
