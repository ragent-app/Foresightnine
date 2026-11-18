import type { SourceMetadata } from "../general-research-client";

interface Props { value: unknown; sources: SourceMetadata[]; segments?: unknown; runSelection?: unknown; pdfDiagnostics?: unknown; en: boolean }

const labels: Record<string, readonly [string, string]> = {
  simulated: ["simulated source excluded", "模拟资料已排除"],
  source_not_authorized: ["source use not authorized", "资料用途未获授权"],
  model_permission_missing: ["model processing not permitted", "未授权模型处理"],
  display_permission_missing: ["display not permitted", "未授权展示"],
  availability_unestablished: ["availability at cutoff not established", "截止时点可用性未确认"],
  source_limit: ["source count limit reached", "超过资料数量上限"],
  complete: ["complete extracted text selected", "已选入完整提取文本"],
  partial: ["partial extracted text selected", "仅选入部分提取文本"],
  not_selected: ["not selected for the initial prompt", "未选入首次模型输入"],
  unknown: ["initial selection unknown", "首次选取范围未知"],
  excluded: ["excluded from analysis", "未纳入分析"],
};

const analysisLabels: Record<string, readonly [string, string]> = {
  produced: ["sent to analysis; the run produced a structured result", "已送入分析；本次运行产生了结构化结果"],
  not_completed: ["analysis stage did not complete", "分析阶段未完成"],
  not_started: ["analysis stage did not start", "分析阶段未开始"],
  unknown: ["analysis status unknown", "分析状态未知"],
};

export function SourceProcessing({ value, sources, segments, runSelection, pdfDiagnostics, en }: Props) {
  if (!Array.isArray(value) || !value.length) return null;
  const entries = value.filter((item): item is { source_id: string; eligibility: string; initial_selection: string; selection_status?: string; analysis_status?: string; selection_gap?: string | null } =>
    item && typeof item === "object" && typeof item.source_id === "string" &&
    typeof item.eligibility === "string" && typeof item.initial_selection === "string");
  if (!entries.length) return null;
  const sourceMap = new Map(sources.map((source) => [source.source_id, source]));
  const selected = Array.isArray(segments) ? segments.filter((item): item is { source_id: string; reading_order_status?: string } =>
    item && typeof item === "object" && typeof item.source_id === "string") : [];
  return <details className="general-source"><summary>{en ? "Initial source selection" : "首轮选入范围"}</summary>
    <p>{en ? "Selection and stage status do not establish source quality, model comprehension, or support for a claim." : "选取范围和阶段状态不能证明资料质量、模型理解或资料支持某项主张。"}</p>
    <ul>{entries.map((item, index) => {
      const code = item.eligibility === "eligible" ? item.selection_status ?? item.initial_selection : item.eligibility;
      const analysis = analysisLabels[item.analysis_status ?? "unknown"] ?? ["analysis status unknown", "分析状态未知"];
      const source = sourceMap.get(item.source_id);
      const pdf = source?.retrieval_method === "user_pdf" || source?.original_pdf_available === true;
      const pdfSegments = selected.filter((segment) => segment.source_id === item.source_id);
      const diagnosticRows = Array.isArray(pdfDiagnostics) ? pdfDiagnostics.filter((row) => row && typeof row === "object" && row.source_id === item.source_id) as Record<string, unknown>[] : [];
      const pdfDetail = diagnosticRows.length ? diagnosticRows.map((diagnostic) => {
        const prefix = diagnostic.stage === "gap_followup" ? (en ? "follow-up: " : "补证：") : (en ? "initial analysis: " : "首次分析：");
        const status = diagnostic.geometry_status;
        return prefix + (status === "mapped" ? (en ? "local geometry result available; whole-document layout remains unverified" : "本地几何结果可用；全文版式仍未核验") : status === "limit_reached" ? (en ? "geometry grouping reached its limit; remaining structure is unknown" : "几何分组达到上限，其余结构未知") : status === "unavailable" ? (en ? "geometry mapping unavailable; layout is unknown" : "几何映射不可用，版式未知") : (en ? "geometry mapping not recorded" : "未记录几何映射状态"));
      }).join("; ") : (en ? "geometry mapping not recorded" : "未记录几何映射状态");
      const orderUnverified = pdfSegments.length > 0 && pdfSegments.every((segment) => segment.reading_order_status === "not_independently_verified");
      return <li key={`${item.source_id}-${index}`}>{source?.title || item.source_id}: {labels[code]?.[en ? 0 : 1] ?? (en ? "status unavailable" : "状态不可用")}; {analysis[en ? 0 : 1]}{item.selection_gap === "budget_or_no_complete_unit" ? (en ? "; budget or no complete context unit" : "；预算不足或没有完整上下文单元") : item.selection_gap === "not_selected_reason_unknown" ? (en ? "; reason for not selecting is unknown" : "；未选入的原因未知") : null}{pdf ? `; ${pdfDetail}` : null}{pdf ? (orderUnverified
        ? (en ? "; PDF reading order has not been independently checked against the original" : "；PDF 阅读顺序尚未与原件独立核验")
        : (en ? "; PDF reading-order verification is unknown" : "；PDF 阅读顺序核验状态未知")) : null}</li>;
    })}</ul>
    {Array.isArray(runSelection) && runSelection.length ? <><h4>{en ? "Recorded analysis and follow-up ranges" : "已记录的初始分析与补证范围"}</h4>
      <p>{en ? "These ranges record model input, not exhaustive review or semantic support." : "范围记录各阶段送入的材料，不代表穷尽核查或语义支持。"}</p>
      <ul>{runSelection.map((row: unknown, index: number) => {
        if (!row || typeof row !== "object" || !("source_id" in row) || typeof row.source_id !== "string") return null;
        const stages = "stage_ranges" in row && Array.isArray(row.stage_ranges) ? row.stage_ranges : [];
        return <li key={`${row.source_id}-${index}`}>{sourceMap.get(row.source_id)?.title || row.source_id}: {stages.map((stage: unknown) => {
          if (!stage || typeof stage !== "object" || !("stage" in stage) || !("ranges" in stage) || !Array.isArray(stage.ranges)) return "";
          const title = stage.stage === "analysis" ? (en ? "initial analysis" : "初始分析") : (en ? "follow-up" : "补证");
          return `${title}: ${stage.ranges.map((range: { start?: number; end?: number }) => `${range.start}–${range.end}`).join(", ")}`;
        }).filter(Boolean).join("; ") || (en ? "no recorded ranges" : "无已记录范围")}
          {"coverage" in row && row.coverage === "unknown" ? (en ? "; overall recorded coverage is unknown" : "；累计范围未知") : null}</li>;
      })}</ul></> : null}
  </details>;
}
