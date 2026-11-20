import type { ResearchV2 } from "@foresightnine/contracts";
import type { InterimSummary } from "../api/types";
import { formatInteger } from "./format";

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
  search_source: "检索资料",
  outline_source: "查看资料目录",
  discover_sources: "搜索线索",
  acquire_sources: "获取原文",
  sec_company_concept: "查询 SEC 结构化数据",
  historical_prices: "获取历史行情",
  resolve_entity: "识别公司与证券",
  market_data: "获取行情数据",
  company_financials: "查询财务报表数据",
  filings: "查询申报文件",
  record_facts: "记录数字",
  calculate_financials: "计算",
  create_chart: "生成图表",
  submit_research: "提交结论",
};

export const CHART_TYPE_LABEL: Readonly<Record<string, string>> = {
  bar: "柱状图",
  grouped_bar: "分组柱状图",
  stacked_bar: "堆叠柱状图",
  line: "折线图",
  combo_bar_line: "柱线组合图",
  waterfall: "瀑布图",
  scatter: "散点图",
  price_series: "行情走势图",
  causal_graph: "逻辑传导图",
};

export function chartTypeLabel(chartType: string | null | undefined): string {
  if (!chartType) return "图表";
  return CHART_TYPE_LABEL[chartType] ?? chartType;
}

export const CAUSAL_CATEGORY_LABEL: Readonly<Record<string, string>> = {
  macro: "宏观",
  industry: "行业",
  company: "公司",
  asset: "资产",
  scenario: "情景",
};

export const CAUSAL_STATUS_LABEL: Readonly<Record<string, string>> = {
  confirmed: "已核实",
  assumption: "假设",
  invalidated: "已失效",
};

export const CAUSAL_DIRECTION_LABEL: Readonly<Record<string, string>> = {
  positive: "正向传导",
  negative: "负向传导",
  conditional: "条件传导",
  uncertain: "不确定",
};

export function toolLabel(name: string | null): string {
  if (name === null) return "工具";
  return TOOL_LABEL[name] ?? name;
}

export const TOOL_STATUS_LABEL: Readonly<Record<string, string>> = {
  completed: "完成",
  read: "已读",
  acquired: "已获取",
  retrieved: "已获取",
  duplicate: "重复",
  restricted: "受限",
  unprocessed: "未处理",
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
  search_no_results: "搜索没有找到相关结果",
  search_sources_denied: "搜索到的线索均不符合研究来源限制",
  search_query_invalid: "搜索词无效",
  search_provider_unconfigured: "搜索服务未配置",
  search_query_provider_limit: "搜索词过长，超出服务商限制",
  search_timeout: "搜索超时",
  search_cancelled: "搜索已取消",
  search_provider_timeout: "搜索服务响应超时",
  search_provider_unavailable: "搜索服务暂时不可用",
  search_provider_rejected: "搜索服务拒绝了请求",
  search_budget_exhausted: "搜索次数或时间预算已用完",
  search_cost_unknown: "当前设置了费用上限，无法执行搜索",
  search_budget_unsupported: "当前搜索服务不支持预算限制",
  historical_price_source_not_authorized_or_unavailable: "没有已授权的行情来源",
  historical_price_budget_unavailable: "超出时间或费用预算，无法获取历史行情",
  historical_price_rate_limited: "历史行情服务请求过于频繁，请稍后再试",
  historical_price_transport_error: "历史行情获取连接中断",
  historical_price_provider_rejected: "历史行情服务未能提供该序列",
  historical_price_session_unconfirmed: "所选结束日期尚未确认收盘，请选更早的交易日",
  lead_not_discovered: "该网址不在本轮搜索返回的线索中",
  acquisition_not_authorized_or_disabled: "本轮未开启自动或在线获取原文",
  sec_contact_unconfigured_or_unavailable: "当前环境未配置 SEC 联系信息，SEC 查询不可用",
  sec_contact_unconfigured: "当前环境未配置 SEC 联系信息",
  http_forbidden_or_challenge: "目标网站拒绝访问（401/403 或反爬验证）",
  not_found: "目标网页不存在（404/410）",
  source_too_large: "网页或提取文本超过 5 MB / 200 万字符上限",
  source_format_unsupported: "目标网址返回的格式不受支持（仅支持 HTML、纯文本和 PDF）",
  source_blocked_policy: "该网址或跳转目标被策略拦截",
  source_blocked_terms: "该网站的条款不允许自动访问，请改用公司官网副本或自行上传文件",
  source_rate_limited: "目标网站请求过于频繁（429），请稍后再试",
  source_invalid: "获取到的文档内容为空或格式无效",
  source_timeout: "获取原文超时",
  source_unavailable: "目标网站暂时不可达或重定向不安全",
  historical_cutoff: "研究设置了截止时间，不支持自动获取或实时外部查询",
  source_cannot_be_acquired: "候选链接均未能成功获取原文",
  unsupported_source: "不支持这个来源地址",
  unit_or_period_not_found_in_returned_records: "SEC 返回记录中没有匹配的单位或期间",
  source_no_matching_facts: "在 SEC 返回记录中未找到匹配的事实",
  identity_not_unique: "SEC 公司名称或代码匹配不唯一",
  incomplete_or_invalid_query: "SEC 结构化查询参数不完整或无效",
  cost_unknown: "当前设置了费用上限，无法执行该外部查询",
  permission_denied: "没有权限执行该查询",
  time_budget: "剩余时间不足，未能执行查询",
  sec_query_failed: "SEC 结构化查询失败",
  source_not_authorized_or_unavailable: "该资料未获准用于当前研究或不可用",
  invalid_url: "网址无效或不受支持",
  invalid_candidate: "搜索线索格式无效",
  retrieval_failed: "获取资料失败",
  source_permission_unknown: "资料权限未明确",
  same_source_version: "该资料版本已存在于当前研究中",
  source_capacity: "来源数量或存储容量已达上限",
};

export function toolReasonLabel(reason: string | null): string | null {
  if (reason === null) return null;
  return TOOL_REASON_LABEL[reason] ?? reason;
}

export const TURN_REASON_LABEL: Readonly<Record<string, string>> = {
  no_submission: "模型没有提交结果就停止了",
  no_progress: "模型重复相同操作，证据没有变化，已停止",
  native_tools_unavailable: "当前模型不支持研究工具调用",
  external_result_unknown: "有一次外部请求的结果未知，需要核对后才能继续",
  incomplete_model_response: "模型回复不完整，无法安全执行",
  settlement_failed: "模型回复已保存，用量结算待核对",
  provider_cancelled: "已取消",
  acquisition_result_unknown: "资料获取结果未知",
  provider_timeout: "模型服务请求超时",
  provider_transport_error: "与模型服务的连接中断",
  provider_output_truncated: "模型输出达到单次长度上限而被截断",
  provider_context_limit: "输入与对话上下文超出模型窗口上限",
  provider_request_rejected: "模型服务拒绝了本次请求",
  provider_output_refused: "模型服务拒绝生成该内容",
  provider_unavailable: "模型服务暂时不可用",
  provider_rate_limited: "模型服务请求过于频繁，请稍后重试",
  provider_budget_exhausted: "已达到本次研究或账户的用量/时间预算上限",
  provider_budget_unknown: "无法确认本次模型调用的用量边界",
  provider_secret_missing: "当前环境未配置模型服务密钥",
  provider_configuration_invalid: "模型服务配置无效",
  invalid_provider_output: "模型服务返回了格式无效的响应",
  provider_no_result: "模型请求没有返回可用结果",
  token_budget_exhausted: "Token 预算已用完",
};

/** Model request failures as shown in the steps list (research_runtime model_failure codes). */
export const MODEL_FAILURE_LABEL: Readonly<Record<string, string>> = {
  provider_timeout: "请求超时",
  provider_transport_error: "连接中断",
  provider_cancelled: "已取消",
  provider_output_truncated: "输出达到单次长度上限被截断",
  provider_context_limit: "上下文超出窗口上限",
  provider_request_rejected: "请求被模型服务拒绝",
  provider_output_refused: "内容被服务拒绝生成",
  provider_unavailable: "模型服务暂时不可用",
  provider_rate_limited: "请求过于频繁",
  provider_budget_exhausted: "已达到用量或时间预算上限",
  provider_budget_unknown: "无法确认用量边界",
  provider_secret_missing: "未配置模型服务密钥",
  provider_configuration_invalid: "模型服务配置无效",
  invalid_provider_output: "返回格式无效",
  provider_no_result: "请求没有返回可用结果",
  token_budget_exhausted: "Token 预算已用完",
};

export function modelFailureLabel(code: string | null | undefined): string {
  if (!code) return "请求没有返回可用结果";
  return MODEL_FAILURE_LABEL[code] ?? `请求没有返回可用结果（${code}）`;
}

export function turnReasonLabel(reason: string, stoppingNote?: string | null): string {
  if (reason === "submitted") {
    const note = stoppingNote?.trim();
    return note ? `模型说明：${note}` : "模型已提交结果";
  }
  return TURN_REASON_LABEL[reason] ?? TOOL_REASON_LABEL[reason] ?? `模型服务返回错误（${reason}）`;
}

export const RECOVERY_REASON_LABEL: Readonly<Record<string, string>> = {
  running: "运行仍在进行",
  no_checkpoint: "没有可恢复的检查点",
  changed: "研究、资料或模型配置已变化",
  unknown_external: "外部请求结果未知",
  already_used: "这个检查点已经用过",
  budget_exhausted: "预算已用完",
  repeated_truncation: "连续多次达到模型输出或上下文长度上限，无法再通过“继续研究”恢复；建议缩小问题范围重新提问，或先查看、导出已收集的部分事实",
  repeated_timeout: "连续多次发生模型单次调用超时，无法再通过“继续研究”恢复；建议缩小问题范围重新提问，或先查看、导出已收集的资料与部分事实",
  unavailable: "当前无法恢复",
};

export function recoveryReasonLabel(reason: string | null | undefined): string {
  if (!reason) return "";
  return RECOVERY_REASON_LABEL[reason] ?? "当前无法恢复";
}

const PROVIDER_ERROR_MESSAGE_LABEL: Readonly<Record<string, string>> = {
  "Provider output was truncated.": "模型输出达到单次长度上限而被截断。",
  "Provider context limit was reached.": "输入与对话上下文超出模型窗口上限。",
  "Provider rejected the request.": "模型服务拒绝了本次请求。",
  "Provider rejected the request; no result was accepted.": "模型服务拒绝了本次请求。",
  "Provider declined the request.": "模型服务拒绝生成该内容。",
  "Provider timed out without a usable result.": "模型服务请求超时。",
  "Provider transport failed without a usable result.": "与模型服务的连接中断。",
  "Provider stream ended unexpectedly.": "模型流式连接意外中断。",
  "Provider stream ended before [DONE].": "模型流式响应未完整结束。",
  "Provider stream returned an error.": "模型流式服务返回错误。",
  "Provider returned no usable result.": "模型服务未返回可用结果。",
  "Provider returned no usable structured result.": "模型服务未返回可用结构化结果。",
  "Provider returned an invalid response.": "模型服务返回了格式无效的响应。",
  "Provider returned an invalid chat completion.": "模型服务返回了格式无效的响应。",
  "Provider request was cancelled.": "已取消模型请求。",
  "Provider request was cancelled; result may be unknown.": "已取消模型请求，结果可能未知。",
  "Provider request was cancelled; result discarded.": "已取消模型请求，响应已丢弃。",
  "Provider request was cancelled; result was discarded.": "已取消模型请求，响应已丢弃。",
  "Provider result discarded.": "已取消模型请求，响应已丢弃。",
  "Provider secret is not configured.": "当前环境未配置模型服务密钥。",
  "LLM provider secret is not configured.": "当前环境未配置模型服务密钥。",
  "Messages configuration is invalid.": "模型服务配置无效。",
  "Provider capability or schema configuration is invalid.": "模型服务能力或参数结构配置无效。",
  "Output budget is exhausted.": "模型输出预算已用完。",
  "Research output budget is exhausted.": "模型输出预算已用完。",
  "Time budget is exhausted.": "研究时间预算已用完。",
  "Provider time budget is exhausted.": "研究时间预算已用完。",
  "Research time budget is exhausted.": "研究时间预算已用完。",
  "Model input budget exceeded.": "本次输入超出模型预算上限。",
  "Estimated input exceeds the configured per-call budget.": "预估输入超出单次调用预算上限。",
  "Estimated input exhausts the task token budget.": "预估输入已耗尽本轮任务 Token 预算。",
  "The planned call exceeds the available account token quota.": "本次调用预计超出账户可用 Token 配额。",
  "Provider output usage cannot be bounded.": "无法确认本次模型调用的输出用量边界。",
  "Cannot verify context fit without a configured input counter.": "未配置输入计数能力，无法确认上下文是否超出上限。",
  "Cannot verify context fit for this input.": "无法确认当前输入是否超出模型上下文上限。",
  "Prior model usage is unknown.": "前序模型用量未知，无法继续预算校验。",
  "A bounded per-call output limit is required for account admission.": "账户配额校验要求设置单次输出上限。",
  "The provider cannot enforce the requested output reservation.": "模型服务无法执行当前预留输出限制。",
  "Provider limits cannot be enforced.": "无法执行模型服务用量限制。",
  "Provider request ended without a usable result.": "模型请求没有返回可用结果。",
  "Provider failed without a usable result.": "模型服务未返回可用结果。",
  "The model provider reported an error.": "模型服务返回错误。",
};

export const GAP_LABEL: Readonly<Record<string, string>> = {
  no_cited_claims: "还没有引文位置已核实的结论。请添加或读取相关资料后继续研究。",
  claims_omitted_unverified_location: "部分结论因引文位置无法核实而未保留。",
  search_response_invalid: "搜索服务返回的格式有误，暂时无法提供线索。",
  search_candidates_not_acquired: "搜索找到了候选链接，但当前来源无法获取原文。",
  historical_prices_unavailable: "所请求的标的和期间没有已授权的历史行情。",
  market_data_key_required: "未配置个人行情 key，行情数据不可用；报告中引用的价格并非来自行情数据。请在设置中添加自己的 Twelve Data key。",
  provider_no_result: "模型请求没有返回可用结果。",
  provider_timeout: "单次模型调用超时，未能返回完整结果。",
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
  if (gap.code === "provider_error") {
    const localized = PROVIDER_ERROR_MESSAGE_LABEL[gap.message.trim()];
    return localized ?? `模型服务返回错误。（${gap.message}）`;
  }
  if (gap.code === "model_reported") return `模型说明：${gap.message}`;
  const knownGap = GAP_LABEL[gap.code];
  if (knownGap) return knownGap;
  const knownTurnReason = TURN_REASON_LABEL[gap.code];
  if (knownTurnReason) return `${knownTurnReason}。`;
  return `${gap.message}（原文）`;
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
  web_html: "网页抓取",
  web_pdf: "网页 PDF",
  sec_companyconcept: "SEC 结构化数据",
  sec_filing_html: "SEC 文件",
  historical_prices: "历史行情",
};

export function retrievalMethodLabel(method: string): string {
  return RETRIEVAL_METHOD_LABEL[method] ?? method;
}

export const FORMULA_LABEL: Readonly<Record<string, string>> = {
  growth_percent: "增长率",
  margin_percent: "利润率",
  difference: "差额",
};

export function formulaLabel(formulaId: string): string {
  return FORMULA_LABEL[formulaId] ?? formulaId;
}

export const MODEL_PHASE_LABEL: Readonly<Record<string, string>> = {
  connecting: "连接模型中",
  thinking: "模型思考中",
  responding: "整理回复中",
  retrying: "重试模型请求",
};

export function modelPhaseLabel(phase: string | null | undefined, retryAttempt?: number | null): string {
  const base = (phase && MODEL_PHASE_LABEL[phase]) || "模型思考中";
  if (typeof retryAttempt === "number" && Number.isInteger(retryAttempt) && retryAttempt > 0) {
    return `${base}（第 ${retryAttempt} 次）`;
  }
  return base;
}

export function formatInterimParts(interim: InterimSummary | null | undefined): string[] {
  if (!interim) return [];
  const parts: string[] = [];
  if (interim.leads_count > 0) parts.push(`发现 ${formatInteger(interim.leads_count)} 条线索`);
  if (interim.acquired_sources_count > 0) parts.push(`获取 ${formatInteger(interim.acquired_sources_count)} 份资料`);
  else if (interim.read_sources_count > 0) parts.push(`已读 ${formatInteger(interim.read_sources_count)} 份资料`);
  if (interim.facts_count > 0) parts.push(`记下 ${formatInteger(interim.facts_count)} 个数字`);
  if (interim.calculations_count > 0) parts.push(`完成 ${formatInteger(interim.calculations_count)} 项计算`);
  return parts;
}

export function formatInterimSummary(interim: InterimSummary | null | undefined): string {
  return formatInterimParts(interim).join(" · ");
}

export const PROCESS_NOTE_LABEL = "思路 · 过程说明（非正式结论）";
export const LIVE_STAGED_TAG = "运行中暂存";
export const VERIFY_NOTE = "系统已核对引文位置和数字；结论是否被引文充分支持，需要你判断。";
export const CALC_NOTE = "计算使用已记录的输入；原始数据的精度和口径未核验。";
export const LEAD_NOTE = "搜索结果只是线索，获取并阅读原文后才能作为证据。";
