import type { ResearchV2 } from "@foresightnine/contracts";

export const OPERATION_STATUS_LABEL: Readonly<Record<string, string>> = {
  pending: "排队中",
  running: "研究中",
  completed: "已结束",
  failed: "运行中断",
  cancelled: "已取消",
};

export const TURN_STATUS_LABEL: Readonly<Record<string, string>> = {
  completed: "完成",
  partial: "部分完成",
};

export const TOOL_LABEL: Readonly<Record<string, string>> = {
  read_sources: "阅读资料",
  discover_sources: "搜索线索",
  acquire_sources: "获取原文",
  historical_prices: "获取历史行情",
  record_facts: "记录数字",
  calculate_financials: "计算",
  submit_research: "提交结论",
};

export function toolLabel(name: string | null): string {
  if (name === null) return "工具";
  return TOOL_LABEL[name] ?? name;
}

export const TOOL_STATUS_LABEL: Readonly<Record<string, string>> = {
  completed: "完成",
  read: "已读",
  acquired: "已获取",
  failed: "失败",
  invalid: "参数被拒",
  unavailable: "不可用",
  denied_or_unavailable: "无权限或不可用",
  unknown: "结果未知",
};

export function toolStatusLabel(status: string | null): string {
  if (status === null) return "";
  return TOOL_STATUS_LABEL[status] ?? status;
}

export const INVALID_NOTE = "“参数被拒”表示模型提交的参数没有通过校验，已退回给模型修正。";

export const TOOL_REASON_LABEL: Readonly<Record<string, string>> = {
  search_not_authorized_or_unavailable: "搜索没有开启或不可用",
  search_response_invalid: "搜索服务返回的格式有误",
  historical_price_source_not_authorized_or_unavailable: "没有已授权的行情来源",
};

export function toolReasonLabel(reason: string | null): string | null {
  if (reason === null) return null;
  return TOOL_REASON_LABEL[reason] ?? reason;
}

const TURN_REASON_LABEL: Readonly<Record<string, string>> = {
  no_submission: "模型没有提交结果就停止了",
  no_progress: "模型重复相同操作，证据没有变化，已停止",
  native_tools_unavailable: "当前模型不支持研究工具调用",
  external_result_unknown: "有一次外部请求的结果未知，需要核对后才能继续",
  incomplete_model_response: "模型回复不完整，无法安全执行",
  settlement_failed: "模型回复已保存，用量结算待核对",
  provider_cancelled: "已取消",
  acquisition_result_unknown: "资料获取结果未知",
};

export function turnReasonLabel(reason: string, stoppingNote?: string | null): string {
  if (reason === "submitted") {
    const note = stoppingNote?.trim();
    return note ? `模型说明：${note}` : "模型已提交结果";
  }
  return TURN_REASON_LABEL[reason] ?? `模型服务返回错误（${reason}）`;
}

export const RECOVERY_REASON_LABEL: Readonly<Record<string, string>> = {
  running: "运行仍在进行",
  no_checkpoint: "没有可恢复的检查点",
  changed: "研究、资料或模型配置已变化",
  unknown_external: "外部请求结果未知",
  already_used: "这个检查点已经用过",
  budget_exhausted: "预算已用完",
  unavailable: "当前无法恢复",
};

export function recoveryReasonLabel(reason: string | null): string {
  if (reason === null) return "";
  return RECOVERY_REASON_LABEL[reason] ?? "当前无法恢复";
}

export const GAP_LABEL: Readonly<Record<string, string>> = {
  no_cited_claims: "还没有引文位置已核实的结论。请添加或读取相关资料后继续研究。",
  claims_omitted_unverified_location: "部分结论因引文位置无法核实而未保留。",
  search_response_invalid: "搜索服务返回的格式有误，暂时无法提供线索。",
  search_candidates_not_acquired: "搜索找到了候选链接，但当前来源无法获取原文。",
  historical_prices_unavailable: "所请求的标的和期间没有已授权的历史行情。",
  provider_no_result: "模型请求没有返回可用结果。",
  source_selection_partial: "部分资料还没有读完，未读部分中是否有相关内容尚不确定。",
  run_stopped: "上一次运行已经停止，需要重新提问。",
  external_result_unknown: "有一次外部请求的结果未知，需要核对后才能继续。",
  native_tools_unavailable: "当前模型不支持研究工具调用。",
  incomplete_model_response: "模型回复不完整，无法安全执行。",
  settlement_failed: "模型回复已保存，用量结算待核对。",
  cancelled: "研究已取消。",
  no_progress: "模型重复相同操作，证据没有变化，已停止。",
  no_submission: "模型没有提交结果就停止了。",
  search_result_unknown: "搜索结果未知，需要核对。",
  acquisition_result_unknown: "资料获取结果未知，需要核对。",
};

export function gapText(gap: { code: string; message: string }): string {
  if (gap.code === "provider_error") return `模型服务返回错误。（${gap.message}）`;
  if (gap.code === "model_reported") return `模型说明：${gap.message}`;
  return GAP_LABEL[gap.code] ?? `${gap.message}（原文）`;
}

export const VERIFICATION_LABEL: Readonly<Record<string, string>> = {
  numeric_and_location_match_semantics_not_verified: "数字与位置已核对，语义未核验",
  user_declared_not_verified: "来源信息由用户声明，未核验",
};

export const CALC_CHECK_LABEL: Readonly<Record<string, string>> = {
  "input_status:unverifiable": "输入不可核验",
  "result_status:matched": "结果一致",
  "period_order_check:not_checked": "期间顺序未检查",
  "period_granularity_check:not_established": "期间粒度未确认",
};

export type CalcCheckTone = "support" | "review" | "neutral";

export function calcChecks(calc: ResearchV2.ResearchCalculation): { label: string; tone: CalcCheckTone }[] {
  const fields = [
    ["input_status", calc.input_status],
    ["result_status", calc.result_status],
    ["period_order_check", calc.period_order_check],
    ["period_granularity_check", calc.period_granularity_check],
  ] as const;
  const checks: { label: string; tone: CalcCheckTone }[] = [];
  for (const [field, value] of fields) {
    if (typeof value !== "string" || value === "") continue;
    const key = `${field}:${value}`;
    const label = CALC_CHECK_LABEL[key] ?? `${field}: ${value}`;
    const tone: CalcCheckTone = key === "result_status:matched" ? "support" : key in CALC_CHECK_LABEL ? "review" : "neutral";
    checks.push({ label, tone });
  }
  return checks;
}

export const PERMISSION_LABEL: Readonly<Record<string, string>> = {
  store: "保存",
  model_input: "供模型读取",
  display: "展示原文",
  export: "导出",
};
export const PERMISSION_ORDER = ["store", "model_input", "display", "export"] as const;

export const REVIEW_LABEL: Readonly<Record<string, string>> = {
  draft: "草稿",
  confirmed: "已确认",
  reservation: "保留意见",
};

export const CREATED_BY_LABEL: Readonly<Record<string, string>> = {
  agent: "研究 Agent",
  user: "你",
  unknown: "未知",
};

export const SOURCE_KIND_LABEL: Readonly<Record<string, string>> = {
  user_supplied: "用户提供",
  official: "官方资料",
  market_data: "行情数据",
};

export const RETRIEVAL_METHOD_LABEL: Readonly<Record<string, string>> = {
  user_supplied: "粘贴文本",
  user_pdf: "上传 PDF",
  user_html: "上传 HTML",
  official_html: "官方网页",
  sec_companyconcept: "SEC 结构化数据",
  sec_filing_html: "SEC 文件",
  historical_prices: "历史行情",
};

export function retrievalMethodLabel(method: string): string {
  return RETRIEVAL_METHOD_LABEL[method] ?? method;
}

export const VERIFY_NOTE = "系统已核对引文位置和数字；结论是否被引文充分支持，需要你判断。";
export const CALC_NOTE = "计算使用已记录的输入；原始数据的精度和口径未核验。";
export const LEAD_NOTE = "搜索结果只是线索，获取并阅读原文后才能作为证据。";
