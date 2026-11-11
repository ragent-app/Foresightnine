import type { ComputeFmResult } from "@foresightnine/fm-engine";
import { Button, StatusDot } from "./Controls";
import { Header } from "./AppShell";
import { t, useLanguage } from "../i18n";
import type { FmStatus } from "../types";

interface FmProps {
  readonly status: FmStatus;
  readonly progress: number;
  readonly step: string;
  readonly resultReady: boolean;
  readonly result?: ComputeFmResult | null;
  readonly onCompute: () => void;
  readonly onCancel: () => void;
  readonly onPortfolio: () => void;
}
const factorNames: Readonly<Record<number, readonly [string, string]>> = {
  10: ["动量", "Momentum"], 11: ["月度动量", "Monthly momentum"],
  12: ["RSI 14", "RSI 14"], 13: ["价格 / 均线", "Price / SMA"],
  20: ["质量", "Quality"], 30: ["动量排名", "Momentum rank"],
};
function number(value: number | undefined): string {
  return value === undefined || !Number.isFinite(value) ? "—" : value.toFixed(3);
}

export function FmPanel({ status, step, result, onCompute, onCancel, onPortfolio }: FmProps) {
  const { language } = useLanguage();
  const en = language === "en";
  // A previous result may remain in the draft while retrying. It is not the new run's evidence.
  const current = status === "complete" ? result : null;
  const progressLabel = en ? ({"正在获取金融数据…":"Fetching market data…", "正在准备本地因子输入…":"Preparing factor inputs…", "正在提交因子表达式…":"Submitting factor expressions…", "合成信号与标准化 · 运行中":"Combining and standardizing signals…"} as Record<string,string>)[step] ?? t(language, step) : step;
  const summary = status === "computing" ? (en ? "Computing on this device" : "正在本机计算")
    : status === "failed" ? (en ? "Computation failed" : "计算失败")
    : status === "cancelled" ? (en ? "Computation cancelled" : "计算已取消")
    : current ? t(language, "本地计算完成") : t(language, "等待本地计算");
  return <section className="content-column fm-view">
    <Header title={t(language, "本地因子计算")} action={<span className="operation-label"><StatusDot tone={current ? "sage" : status === "failed" ? "danger" : "accent"} />{summary}</span>} />
    {status === "computing" ? <div className="fm-run-status" role="status" aria-live="polite"><p>{progressLabel}</p><progress aria-label={en ? "Factor computation in progress" : "因子计算进行中"} /><Button variant="secondary" onClick={onCancel}>{en ? "Cancel computation" : "取消计算"}</Button></div> : null}
    {current ? <>
      <div className="fm-run-summary"><p>{en ? "Data cutoff" : "本次数据截止"} <time>{current.cutoff}</time></p><p>{en ? "Factors contributing" : "实际参与合成"} <strong>{current.combination.usedFactorIds.length} / {current.lastFactorDescriptors.length}</strong></p></div>
      {current.weights.fallback ? <p className="notice notice-warning" role="status">{en ? "Equal-weight fallback: insufficient usable factor signals; this is not an optimized allocation." : "等权回退：有效因子信号不足；这不是因子优化后的配置。"}</p> : null}
      {current.lastFactorDescriptors.length ? <div className="factor-table-wrap" tabIndex={0} aria-label={en ? "Factor results; scroll horizontally if needed" : "因子结果，可横向滚动"}><table className="factor-table">
        <caption>{en ? "This run's factor results" : "本次因子结果"}</caption><thead><tr>{[en ? "Factor" : "因子", "IC", "ICIR", en ? "Applied weight" : "采用权重", en ? "Status" : "状态"].map(label => <th key={label}>{label}</th>)}</tr></thead>
        <tbody>{current.lastFactorDescriptors.map(factor => {
          const used = current.combination.usedFactorIds.includes(factor.id);
          const selected = current.selectedFactorIds.includes(factor.id);
          const label = factorNames[factor.id]?.[en ? 1 : 0] ?? `${en ? "Factor" : "因子"} #${factor.id}`;
          const admission = used ? (en ? "Contributing" : "已参与") : selected ? (en ? "Insufficient signal" : "信号不足") : (en ? "Not selected" : "未入选");
          return <tr key={factor.id}><th scope="row">{label}</th><td>{number(factor.icMean)}</td><td>{number(factor.icir)}</td><td>{number(current.combination.qualityWeights.get(factor.id))}</td><td>{admission}</td></tr>;
        })}</tbody></table></div> : <p>{en ? "No factors were admitted in this run." : "本次没有可采用的因子。"}</p>}
      <details className="fm-method-details"><summary>{en ? "Method and interpretation" : "计算口径与解读"}</summary><p>{en ? "IC/ICIR are the inputs actually used by this run, not independently validated future performance. Three market anchors provide only a limited diagnostic, not a full historical backtest." : "IC/ICIR 是本次实际采用的输入指标，不代表已验证的未来表现。三锚点只能提供有限诊断，不是完整历史回测。"}</p><p>q = max(|IC|, 1e−6) × max(|ICIR|, 1e−6)</p><p>{en ? "A negative IC reverses signal direction. Unusable or unselected factors do not receive an applied weight. FM weights are calculated from market features, not from worldline probability." : "负 IC 改变信号方向；未入选或信号不可用的因子不计采用权重。FM 权重来自市场特征，并非由世界线概率直接生成。"}</p></details>
    </> : status !== "computing" ? <p>{en ? "Your research draft is retained. Run the calculation to obtain factor results." : "研究草稿已保留；运行计算后显示实际因子结果。"}</p> : null}
    {status === "failed" ? <p className="notice notice-warning" role="alert">{t(language, "因子计算或金融数据获取失败，已保留研究状态。")}</p> : null}
    <div className="fm-controls">{status !== "computing" ? current ? <><Button variant="primary" onClick={onPortfolio}>{t(language, "查看配置方案")}</Button><Button variant="quiet" onClick={onCompute}>{en ? "Recompute" : "重新计算"}</Button></> : <Button variant="primary" onClick={onCompute}>{t(language, "开始本地计算")}</Button> : null}</div>
  </section>;
}

export function FmExplanation({ result }: { readonly result: ComputeFmResult | null }) {
  const { language } = useLanguage();
  const en = language === "en";
  return <div className="inspector-content"><h2>{en ? "Calculation record" : "计算记录"}</h2><p>FM-v1 · Web Worker</p>{result ? <><p>{en ? "Cutoff" : "截止时点"} <time>{result.cutoff}</time></p><p>{en ? "Contributing factor IDs" : "参与合成的因子编号"}：{result.combination.usedFactorIds.join(", ") || "—"}</p></> : <p>{en ? "Awaiting this run's result." : "等待本次计算结果。"}</p>}<p>{en ? "A completed calculation does not establish current prices or current FX. Allocation freshness is checked separately." : "计算完成不等于价格或汇率仍有效；配置页会另行检查数据时效。"}</p></div>;
}
