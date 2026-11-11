import { Button, Input, Select } from "./Controls";
import { Header } from "./AppShell";
import { useLanguage } from "../i18n";
import type { BranchView, StressScenario } from "../types";
import { BRANCH_KINDS, buildNavExport, parseReturn, type BranchKind, type NavAssumptions, type NavExportData, type PathMap } from "../nav-data";

interface NavProps {
  readonly branches: readonly BranchView[]; readonly selected: BranchKind; readonly onSelect: (kind: BranchKind) => void;
  readonly stress: StressScenario; readonly onExport: () => void; readonly drawdownVisible?: boolean; readonly onToggleDrawdown?: () => void;
  readonly data?: NavExportData; readonly assumptions?: NavAssumptions;
  readonly onAssumption?: (worldline: string, instrument: string, value: string) => void; readonly saveError?: boolean;
}
const EMPTY = buildNavExport();
export function NavPanel({branches,selected,onSelect,onExport,drawdownVisible=true,onToggleDrawdown=()=>{},data=EMPTY,assumptions={},onAssumption,saveError=false}: NavProps) {
  const {language} = useLanguage(); const en = language === "en";
  const active = branches.find(b => b.kind === selected);
  const scenario = data.scenarios[selected];
  const label = (kind: BranchKind) => en ? ({weak:"Weak",main:"Main",strong:"Strong"}[kind]) : ({weak:"弱情景",main:"主情景",strong:"强情景"}[kind]);
  const ready = BRANCH_KINDS.filter(k => data.scenarios[k].status === "ready");
  return <section className="content-column nav-view">
    <Header title={en ? "Portfolio paths" : "组合路径"} action={<Select value={selected} onChange={e=>onSelect(e.target.value as BranchKind)} aria-label={en ? "Choose worldline" : "选择世界线"}>{branches.map(b=><option key={b.kind} value={b.kind}>{b.title} · P(W | H) {(b.assessment.conditional_probability_range.minimum*100).toFixed(2)}–{(b.assessment.conditional_probability_range.maximum*100).toFixed(2)}%</option>)}</Select>} />
    <section className="chart-frame nav-inputs" aria-label={en ? "Return assumptions" : "涨跌假设"}>
      <h2>{en ? `${label(selected)} · horizon returns` : `${label(selected)} · 期末涨跌假设`}</h2>
      <p>{en ? "Enter each holding's return in your base currency. Worldline direction is qualitative, not a numerical forecast." : "填写各持仓以组合本币计的涨跌幅；世界线方向仅供参考，不自动换算收益率。"}</p>
      {scenario.inputs.length > 0 ? <div className="nav-assumption-list">{scenario.inputs.map(input=>{
        const impact=active?.worldline.asset_impacts.find(i=>i.instrument_id===input.instrument);
        const value=assumptions[scenario.worldlineId]?.[input.instrument] ?? "";
        const invalid=value.trim()!=="" && parseReturn(value)===null;
        const hint=impact ? (en ? impact.direction : {positive:"正向",negative:"负向",mixed:"双向"}[impact.direction]) : (en ? "No mapped impact" : "无对应冲击描述");
        return <label className="nav-assumption-row" key={input.instrument}><span><strong>{input.asset}</strong><small>{input.instrument} · {(input.weight*100).toFixed(1)}% · {hint}</small></span><span className="nav-percent-field"><Input aria-label={`${input.asset} ${label(selected)} %`} type="text" inputMode="decimal" maxLength={24} placeholder={en ? "Required" : "待填写"} value={value} disabled={!onAssumption} aria-invalid={invalid} onChange={e=>onAssumption?.(scenario.worldlineId,input.instrument,e.target.value)}/><span>%</span></span>{invalid ? <small className="nav-input-error">{en ? "Enter −100 to 1000" : "范围 −100 至 1000"}</small> : null}</label>;
      })}</div> : <p role="status">{en ? "No selected holdings. Cash remains unchanged." : "当前没有已配置持仓，现金保持不变。"}</p>}
      <p className="nav-input-status" role="status">{scenario.status === "ready" ? `${en ? "Portfolio horizon change" : "组合期末变化"} ${((scenario.terminalReturn ?? 0)*100).toFixed(2)}%` : scenario.status === "missing_inputs" ? `${en ? "Missing valid assumptions" : "待补全有效假设"}：${scenario.missingAssets.join("、")}` : (en ? "A valid allocation and consistent worldline dates are required." : "需要有效配置和一致的世界线日期。")}</p>
      {saveError ? <p role="alert">{en ? "Draft not saved on this device; keep this page open or export it." : "本机草稿保存失败；请保留当前页面或导出。"}</p> : <small>{en ? "Saved on this device for this account and worldline. Blank is not zero." : "按账号与世界线在本机保存；留空不等于零。"}</small>}
    </section>
    {ready.length > 0 ? <>
      <div className="chart-frame"><h2>{en ? "Assumed NAV · starts at 1" : "假设净值 · 起点为 1"}</h2><p>{data.dates[0]} → {data.dates.at(-1)}</p><NavChart paths={data} dates={data.dates} selected={selected} en={en}/><div className="chart-legend">{BRANCH_KINDS.map(k=><span key={k} className={`legend-${k}`}>{label(k)} · {data.scenarios[k].status === "ready" ? `${(data.scenarios[k].terminalReturn!*100).toFixed(2)}%` : (en ? "Pending" : "待填写")}</span>)}</div></div>
      <div className="drawdown-frame"><div className="drawdown-heading"><h2>{en ? "Assumed path drawdown" : "假设路径回撤"}</h2><Button variant="secondary" onClick={onToggleDrawdown}>{en ? (drawdownVisible ? "Hide drawdown" : "Show drawdown") : (drawdownVisible ? "隐藏回撤" : "显示回撤")}</Button></div>{drawdownVisible ? <NavChart paths={data.drawdown} dates={data.dates} selected={selected} drawdown en={en}/> : <p role="status">{en ? "Drawdown chart hidden" : "回撤图已隐藏"}</p>}</div>
    </> : <p role="status">{en ? "Complete one scenario to draw its path; no sample curves are substituted." : "填写一个情景即可查看路径；当前没有使用示例曲线替代。"}</p>}
    <details className="nav-method"><summary>{en ? "Calculation and limits" : "计算方式与边界"}</summary><p>{en ? "Selected rounded holding amounts × user returns, plus unchanged residual cash. Fixed units; linearly interpolated between worldline start and end. These are assumptions, not historical prices, volatility simulations, calibrated forecasts or probability quantiles." : "按当前模式的实际配置金额 × 用户涨跌假设，加上不计息的剩余现金。持仓单位不变，在世界线起止日期之间线性插值；不是历史价格、波动率模拟、校准预测或概率分位数。"}</p><p>{en ? "Include FX effects in base-currency returns. Fees, taxes and dividends are not added separately. Linear-path drawdown does not estimate intra-period risk. No model call or token charge." : "本币收益应包含汇率变化；不另加费用、税款或分红。线性路径回撤不代表期间真实风险；此计算不调用模型、不消耗额度。"}</p></details>
    <div className="nav-bottom"><span>{en ? "Completed scenarios" : "已填写情景"} {ready.length}/3</span><Button variant="secondary" disabled={ready.length===0} onClick={onExport}>↓ {en ? "Export path data" : "导出路径数据"}</Button></div>
    {ready.length > 0 ? <div className="data-table"><details><summary>{en ? "Path data" : "查看路径数据表"}</summary><table><caption>{en ? "Assumed NAV, not historical observations" : "假设净值，非历史观测"}</caption><thead><tr><th>{en ? "Date" : "日期"}</th>{BRANCH_KINDS.map(k=><th key={k}>{label(k)}</th>)}</tr></thead><tbody>{data.dates.map((date,i)=><tr key={`${date}-${i}`}><td>{date}</td>{BRANCH_KINDS.map(k=><td key={k}>{data[k][i]?.toFixed(4) ?? "—"}</td>)}</tr>)}</tbody></table></details></div> : null}
  </section>;
}
function NavChart({paths,dates,selected,drawdown=false,en}:{readonly paths:PathMap;readonly dates:readonly string[];readonly selected:BranchKind;readonly drawdown?:boolean;readonly en:boolean}) {
  const width=600,height=drawdown ? 180 : 260,pad={left:85,right:22,top:28,bottom:40};
  const all=BRANCH_KINDS.flatMap(k=>paths[k]); const lo=Math.min(drawdown ? 0 : 1,...all),hi=Math.max(drawdown ? 0 : 1,...all); const spread=Math.max(hi-lo,0.02);
  const min=lo-spread*0.12,max=hi+spread*0.12;
  const x=(i:number)=>pad.left+i*(width-pad.left-pad.right)/24;
  const y=(v:number)=>pad.top+(max-v)*(height-pad.top-pad.bottom)/(max-min);
  const label=(k:BranchKind)=>en ? ({weak:"Weak",main:"Main",strong:"Strong"}[k]) : ({weak:"弱情景",main:"主情景",strong:"强情景"}[k]);
  const title=en ? (drawdown ? "Drawdown of user-assumed paths" : "User-assumed portfolio paths") : (drawdown ? "用户假设路径回撤" : "用户假设组合路径");
  return <svg className="nav-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={title}><title>{title}</title><g className="chart-grid" aria-hidden="true">{[0,1,2,3].map(i=>{const v=min+(max-min)*i/3;return <g key={i}><line x1={pad.left} x2={width-pad.right} y1={y(v)} y2={y(v)}/><text className="axis-value" x={3} y={y(v)+4}>{drawdown ? `${(v*100).toFixed(1)}%` : v.toFixed(2)}</text></g>;})}</g>{BRANCH_KINDS.filter(k=>paths[k].length>0).map(k=><g key={k} className={`series-group series-group-${k} ${selected===k ? "is-emphasized" : "is-context"}`} role="img" aria-label={label(k)}><polyline vectorEffect="non-scaling-stroke" className={`series-${k}`} points={paths[k].map((v,i)=>`${x(i)},${y(v)}`).join(" ")}/></g>)}<text className="axis-label" x={pad.left} y={height-6}>{dates[0]}</text><text className="axis-label" textAnchor="end" x={width-pad.right} y={height-6}>{dates.at(-1)}</text></svg>;
}
