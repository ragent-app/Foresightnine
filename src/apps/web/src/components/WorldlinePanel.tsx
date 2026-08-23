import { Button, StatusDot } from "./Controls";
import { Header, Disclosure } from "./AppShell";
import type { BranchView } from "../types";

interface WorldlineProps { readonly branches: readonly BranchView[]; readonly selected: BranchView["kind"]; readonly onSelect: (kind: BranchView["kind"]) => void; readonly onCompute: () => void; }

export function WorldlinePanel({ branches, selected, onSelect, onCompute }: WorldlineProps) {
  const active = branches.find((branch) => branch.kind === selected) ?? branches[1]!;
  return <section className="content-column worldline-view">
    <Header title="世界线与条件概率" action={<span className="model-label">区间优先 · 当前未校准</span>} />
    <div className="probability-row"><Probability label="前提成立 P(H)" value="35–49%" /><Probability label="主世界线 P(W₀ | H)" value="45–62%" /><Probability label="联合概率 P(H ∩ W₀)" value="16–30%" /></div>
    <p className="calibration-line"><StatusDot tone="warning" /> 当前状态：<code>model_estimate_unvalidated</code></p>
    <div className="premise-panel"><strong>当前 premise</strong><p>{active.worldline.premise}</p><small>该前提来自本次用户输入；三条分支共享它，只改变传导路径。</small></div>
    <div className="worldline-canvas" role="group" aria-label="弱、主、强三条世界线">
      <div className="timeline-axis"><span>现在</span><span>6个月</span><span>12个月</span><span>24个月</span></div>
      {branches.map((branch) => <button key={branch.kind} className={`worldline-path path-${branch.kind} ${selected === branch.kind ? "is-selected" : ""}`} onClick={() => onSelect(branch.kind)} aria-pressed={selected === branch.kind}><span className="path-copy"><strong>{branch.title}</strong><small>{branch.description}</small><em>条件概率 P(W | H) <b>{branch.range}</b></em></span><span className="path-track">{branch.nodes.map((node, index) => <span className="path-node" key={node}><i>{index + 1}</i><small>{node}</small></span>)}</span></button>)}
    </div>
    <div className="worldline-footer"><div><strong>{active.title}</strong><p>事件发生时间与市场可见时间分开记录。首个揭示：{active.reveal}。</p></div><Button variant="secondary" onClick={() => onSelect(active.kind)}>确认选择</Button><Button variant="primary" onClick={onCompute}>进入因子计算 <span aria-hidden="true">→</span></Button></div>
    <div className="invalidation"><strong>失效条件</strong><span>{active.invalidation}</span><span>世界线分支可以修订，概率不是事实。</span></div>
    <Disclosure compact />
  </section>;
}

function Probability({ label, value }: { readonly label: string; readonly value: string }) { return <div className="probability"><span>{label}</span><strong>{value}</strong><small>model_estimate_unvalidated</small></div>; }
