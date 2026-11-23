import type { ResearchV2 } from "@foresightnine/contracts";
import { formulaLabel } from "../../lib/copy";
import { useTopicView } from "./context";

const PART_STATUS = { answered: "已回答", partial: "部分回答", unresolved: "尚未完成" } as const;

function resultFields(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function EvidenceLink({ reference, turnId }: { reference: ResearchV2.QuestionPartReference; turnId: string }) {
  const view = useTopicView();
  if (reference.chart_id) {
    const chart = view.content.charts?.find((item) => item.chart_id === reference.chart_id);
    return chart ? <p>图表 · {chart.title}</p> : null;
  }
  if (reference.analysis_id) {
    const analysis = view.content.turns?.flatMap((turn) => turn.market_analyses ?? [])
      .find((item) => item.analysis_id === reference.analysis_id);
    if (!analysis) return null;
    const { source_id, benchmark_source_id, benchmark_selection_reason } = analysis.request;
    const basis = resultFields(analysis.result.provenance).price_basis_applied;
    const benchmark = resultFields(analysis.result.benchmark);
    return <details><summary>核对行情计算</summary>
      <p>实际价格口径 · {basis === "adjusted" ? "复权" : basis === "raw" || basis === "unadjusted" ? "未复权" : "未记录"}</p>
      {typeof benchmark.aligned_start_date === "string" && typeof benchmark.aligned_end_date === "string" ?
        <p>共同交易窗口 · {benchmark.aligned_start_date} 至 {benchmark.aligned_end_date}</p> : null}
      {typeof benchmark.asset_aligned_return_percent === "string" && typeof benchmark.benchmark_aligned_return_percent === "string" ?
        <p>同期收益率 · 标的 {benchmark.asset_aligned_return_percent}% · 基准 {benchmark.benchmark_aligned_return_percent}%</p> : null}
      {benchmark_selection_reason ? <p>基准选择 · {benchmark_selection_reason}</p> : null}
      {[source_id, benchmark_source_id].map((sid) => sid ? (
        <button type="button" key={sid} className="link" onClick={() => view.openPanel({ kind: "reader", sourceId: sid, range: null })}>
          {view.sourceTitle(sid)}
        </button>
      ) : null)}
    </details>;
  }
  const fact = view.content.facts?.find((item) => item.fact_id === reference.fact_id);
  if (fact) {
    return <p>{reference.origin === "prior" ? "此前记录" : "本轮记录"} · {fact.label} {fact.value} {fact.unit} {fact.period}{" "}
      <button type="button" className="link" onClick={() => view.openPanel({ kind: "reader", sourceId: fact.source_id, range: [fact.character_start, fact.character_end] })}>查看原文</button></p>;
  }
  const calc = view.content.calculations?.find((item) => item.calculation_id === reference.calculation_id);
  if (calc) {
    return <button type="button" className="link" onClick={() => view.openPanel({ kind: "record", tab: "numbers", turnKey: calc.turn_id ?? turnId })}>
      查看计算 · {formulaLabel(calc.formula_id)} · {calc.value} {calc.unit}
    </button>;
  }
  if (reference.claim_id) {
    const claimId = reference.claim_id;
    return <button type="button" className="link" onClick={() => view.gotoClaim(claimId)}>查看引证判断</button>;
  }
  if (reference.source_id) {
    const sourceId = reference.source_id;
    const range = reference.character_start != null && reference.character_end != null
      ? [reference.character_start, reference.character_end] as const : null;
    return <button type="button" className="link" onClick={() => view.openPanel({ kind: "reader", sourceId, range })}>
      查看原文 · {view.sourceTitle(sourceId)}
    </button>;
  }
  return null;
}

export function QuestionParts({ turn }: { turn: ResearchV2.ResearchTurn }) {
  const parts = turn.question_parts ?? [];
  if (!parts.length) return null;
  return (
    <section aria-label="问题各部分的进展">
      <h3>问题各部分的进展</h3>
      <p className="note">回答由模型整理；数字与原文位置的核对不等于语义核验，数字本身不是投资建议。</p>
      <ol>{parts.map((part, index) => (
        <li key={part.id ?? `${index}-${part.question}`}>
          <b>{part.question}</b> · {PART_STATUS[part.status]}
          {part.answer ? <p>{part.answer}</p> : null}
          {part.reason ? <p>{part.reason}</p> : null}
          {(part.references ?? []).map((ref, i) => (
            <EvidenceLink key={i} reference={ref} turnId={turn.turn_id} />
          ))}
        </li>
      ))}</ol>
    </section>
  );
}
