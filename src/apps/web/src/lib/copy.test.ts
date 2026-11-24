import { describe, expect, it } from "vitest";
import type { ResearchV2 } from "@foresightnine/contracts";
import {
  CREATED_BY_LABEL, INVALID_NOTE, MODEL_FAILURE_LABEL, MODEL_PHASE_LABEL, OPERATION_STATUS_LABEL, PERMISSION_LABEL, PERMISSION_ORDER, RECOVERY_REASON_LABEL,
  RETRIEVAL_METHOD_LABEL, REVIEW_LABEL, SOURCE_KIND_LABEL, TURN_REASON_LABEL, TURN_STATUS_LABEL, VERIFICATION_LABEL,
  calcChecks, formatInterimParts, formatInterimSummary, formulaLabel, gapText, modelFailureLabel, modelPhaseLabel, recoveryReasonLabel, retrievalMethodLabel, toolLabel, toolReasonLabel, toolStatusLabel, turnReasonLabel,
  CALC_NOTE, LEAD_NOTE, TOOL_REASON_LABEL, LIVE_STAGED_TAG, PROCESS_NOTE_LABEL, VERIFY_NOTE, GAP_LABEL,
} from "./copy";

describe("labels", () => {
  it("maps operation and turn status", () => {
    expect(OPERATION_STATUS_LABEL).toEqual({ pending: "排队中", running: "研究中", completed: "已结束", failed: "运行中断", cancelled: "已取消" });
    expect(TURN_STATUS_LABEL).toEqual({ completed: "完成", partial: "部分完成" });
  });
  it("labels tools with fallbacks", () => {
    expect(toolLabel("read_sources")).toBe("阅读资料");
    expect(toolLabel("search_source")).toBe("检索资料");
    expect(toolLabel("outline_source")).toBe("查看资料目录");
    expect(toolLabel("search_library")).toBe("检索资料库");
    expect(toolLabel("news_search")).toBe("搜索线索");
    expect(toolLabel("sec_company_concept")).toBe("查询 SEC 结构化数据");
    expect(toolLabel("submit_research")).toBe("提交核验");
    expect(toolLabel("calculate_financials")).toBe("计算");
    expect(toolLabel("issuer_documents")).toBe("查询公司官网文档");
    expect(toolLabel("extract_statements")).toBe("抽取财务报表");
    expect(toolLabel("mystery")).toBe("mystery");
    expect(toolLabel(null)).toBe("工具");
  });
  it("labels tool status and reason", () => {
    expect(toolStatusLabel("invalid")).toBe("参数被拒");
    expect(toolStatusLabel("restricted")).toBe("受限");
    expect(toolStatusLabel("denied_or_unavailable")).toBe("无权限或不可用");
    expect(toolStatusLabel("weird")).toBe("weird");
    expect(toolReasonLabel("search_response_invalid")).toBe("搜索服务返回的格式有误");
    expect(toolReasonLabel("source_user_initiated_only")).toContain("个人使用模式");
    expect(toolReasonLabel("issuer_site_robots_disallow")).toContain("robots.txt");
    expect(toolReasonLabel("issuer_site_unreachable")).toContain("大陆");
    expect(toolReasonLabel("issuer_site_robots_unavailable")).toContain("robots.txt");
    expect(toolReasonLabel("search_query_provider_limit")).toBe("搜索词过长，超出服务商限制");
    for (const reason of ["search_quota_exhausted", "search_rate_limited", "gdelt_rate_limited"]) {
      expect(TOOL_REASON_LABEL[reason]).toBeTruthy();
      expect(toolReasonLabel(reason)).not.toContain(reason);
    }
    expect(toolReasonLabel("search_quota_exhausted")).toContain("额度");
    expect(GAP_LABEL.search_quota_exhausted).toContain("重置");
    expect(GAP_LABEL.search_rate_limited).toContain("限流");
    for (const reason of [
      "lead_not_discovered",
      "acquisition_not_authorized_or_disabled",
      "sec_contact_unconfigured_or_unavailable",
      "sec_contact_unconfigured",
      "http_forbidden_or_challenge",
      "not_found",
      "source_too_large",
      "source_format_unsupported",
      "source_blocked_policy",
      "source_timeout",
      "source_unavailable",
      "historical_cutoff",
      "source_cannot_be_acquired",
      "source_no_matching_facts",
      "invalid_url",
      "invalid_candidate",
      "retrieval_failed",
      "source_permission_unknown",
      "same_source_version",
      "source_capacity",
    ]) {
      const label = toolReasonLabel(reason);
      expect(label).toBeTruthy();
      expect(label).not.toBe(reason);
    }
    // Unlabelled machine codes are not shown; free text passes through.
    expect(toolReasonLabel("other")).toBeNull();
    expect(toolReasonLabel("来源暂不可用")).toBe("来源暂不可用");
    expect(toolReasonLabel(null)).toBeNull();
    expect(INVALID_NOTE).toBe("“参数被拒”表示模型提交的参数没有通过校验，已退回给模型修正。");
  });
  it("labels turn reasons", () => {
    expect(turnReasonLabel("no_submission")).toBe("模型没有提交结果就停止了");
    expect(turnReasonLabel("provider_cancelled")).toBe("已取消");
    expect(turnReasonLabel("submitted", "  证据不足  ")).toBe("模型说明：证据不足");
    expect(turnReasonLabel("submitted", "   ")).toBe("模型已提交结果");
    expect(turnReasonLabel("submitted", null)).toBe("模型已提交结果");
    expect(turnReasonLabel("boom")).toBe("模型服务返回错误（boom）");
  });
  it("labels recovery reasons", () => {
    expect(Object.keys(RECOVERY_REASON_LABEL)).toHaveLength(9);
    expect(recoveryReasonLabel("budget_exhausted")).toBe("预算已用完");
    const repeated = recoveryReasonLabel("repeated_truncation");
    expect(repeated).toBe(RECOVERY_REASON_LABEL["repeated_truncation"]);
    expect(repeated).not.toBe("当前无法恢复");
    expect(repeated).not.toContain("repeated_truncation");
    expect(repeated).not.toContain("/continue");
    expect(repeated).toMatch(/输出|上下文/);
    expect(repeated).toMatch(/缩小/);
    expect(repeated).toMatch(/查看|导出/);
    const repeatedTimeout = recoveryReasonLabel("repeated_timeout");
    expect(repeatedTimeout).toBe(RECOVERY_REASON_LABEL["repeated_timeout"]);
    expect(repeatedTimeout).not.toBe("当前无法恢复");
    expect(repeatedTimeout).not.toContain("repeated_timeout");
    expect(repeatedTimeout).not.toContain("provider_timeout");
    expect(repeatedTimeout).not.toContain("/continue");
    expect(repeatedTimeout).toMatch(/超时/);
    expect(repeatedTimeout).toMatch(/继续研究/);
    expect(repeatedTimeout).toMatch(/缩小/);
    expect(repeatedTimeout).toMatch(/查看|导出/);
    expect(recoveryReasonLabel("???")).toBe("当前无法恢复");
    expect(recoveryReasonLabel("repeated_output_truncation")).toBe("当前无法恢复");
    expect(recoveryReasonLabel("")).toBe("");
    expect(recoveryReasonLabel(undefined)).toBe("");
    expect(recoveryReasonLabel(null)).toBe("");
  });
  it("covers the static tables", () => {
    expect(VERIFICATION_LABEL["user_declared_not_verified"]).toBe("来源信息由用户声明，未核验");
    expect(PERMISSION_ORDER).toEqual(["store", "model_input", "display", "export"]);
    expect(PERMISSION_LABEL["model_input"]).toBe("供模型读取");
    expect(REVIEW_LABEL["reservation"]).toBe("保留意见");
    expect(CREATED_BY_LABEL["agent"]).toBe("研究 Agent");
    expect(SOURCE_KIND_LABEL["market_data"]).toBe("行情数据");
    expect(RETRIEVAL_METHOD_LABEL["sec_filing_html"]).toBe("SEC 文件");
    expect(RETRIEVAL_METHOD_LABEL["web_html"]).toBe("网页抓取");
    expect(RETRIEVAL_METHOD_LABEL["web_pdf"]).toBe("网页 PDF");
    expect(retrievalMethodLabel("user_pdf")).toBe("上传 PDF");
    expect(retrievalMethodLabel("web_html")).toBe("网页抓取");
    expect(retrievalMethodLabel("web_pdf")).toBe("网页 PDF");
    expect(retrievalMethodLabel("new_method")).toBe("new_method");
    expect(VERIFY_NOTE).toContain("需要你判断");
    expect(CALC_NOTE).toContain("未核验");
    expect(LEAD_NOTE).toContain("线索");
  });
});

describe("gapText", () => {
  it("uses fixed text for known codes", () => {
    expect(gapText({ code: "cancelled", message: "x" })).toBe("研究已取消。");
    expect(gapText({ code: "no_cited_claims", message: "x" })).toBe("还没有引文位置已核实的结论。请添加或读取相关资料后继续研究。");
    const timeoutGap = gapText({ code: "provider_timeout", message: "Provider timed out without a usable result." });
    expect(timeoutGap).toBe(GAP_LABEL["provider_timeout"]);
    expect(timeoutGap).toMatch(/单次.*超时/);
    expect(timeoutGap).not.toContain("provider_timeout");
    expect(timeoutGap).not.toContain("Provider timed out");
    expect(timeoutGap).not.toBe(gapText({ code: "provider_no_result", message: "x" }));
    expect(Object.keys(GAP_LABEL)).toHaveLength(23);
    expect(GAP_LABEL).toHaveProperty("market_data_key_required");
  });
  it("handles provider_error, model_reported and unknown codes", () => {
    expect(gapText({ code: "provider_error", message: "429" })).toBe("模型服务返回错误。（429）");
    expect(gapText({ code: "provider_error", message: "Provider output was truncated." })).toBe("模型输出达到单次长度上限而被截断。");
    expect(gapText({ code: "provider_error", message: "Provider context limit was reached." })).toBe("输入与对话上下文超出模型窗口上限。");
    expect(gapText({ code: "provider_error", message: "Provider rejected the request." })).toBe("模型服务拒绝了本次请求。");
    expect(gapText({ code: "provider_error", message: "Provider rejected the request; no result was accepted." })).toBe("模型服务拒绝了本次请求。");
    expect(gapText({ code: "provider_output_truncated", message: "raw" })).toBe("模型输出达到单次长度上限而被截断。");
    expect(gapText({ code: "provider_context_limit", message: "raw" })).toBe("输入与对话上下文超出模型窗口上限。");
    expect(gapText({ code: "provider_request_rejected", message: "raw" })).toBe("模型服务拒绝了本次请求。");
    expect(gapText({ code: "model_reported", message: "资料不足" })).toBe("模型说明：资料不足");
    expect(gapText({ code: "brand_new", message: "raw message" })).toBe("raw message（原文）");
  });
});

describe("calcChecks", () => {
  const base: ResearchV2.ResearchCalculation = {
    calculation_id: "c1", decimal_places: 2, expression: "a/b", formula_id: "difference", formula_version: "1",
    input_precision_status: "x", input_verification: "y", inputs: [], rounding: "half_up", unit: "percent", value: "1",
  };
  it("returns nothing when no field is set", () => {
    expect(calcChecks(base)).toEqual([]);
    expect(calcChecks({ ...base, input_status: "", result_status: null })).toEqual([]);
  });
  it("orders and tones the four fields", () => {
    const checks = calcChecks({
      ...base,
      period_granularity_check: "not_established",
      result_status: "matched",
      input_status: "unverifiable",
      period_order_check: "not_checked",
    });
    expect(checks).toEqual([
      { label: "输入不可核验", tone: "review" },
      { label: "结果一致", tone: "support" },
      { label: "期间顺序未检查", tone: "review" },
      { label: "期间粒度未确认", tone: "review" },
    ]);
  });
  it("falls back for unknown values with neutral tone", () => {
    expect(calcChecks({ ...base, result_status: "mismatch" })).toEqual([{ label: "result_status: mismatch", tone: "neutral" }]);
  });
  it("names model timeouts and dropped connections as stop reasons", () => {
    expect(turnReasonLabel("provider_timeout")).toBe("模型服务请求超时");
    expect(turnReasonLabel("provider_transport_error")).toBe("与模型服务的连接中断");
    expect(turnReasonLabel("provider_weird")).toBe("模型服务返回错误（provider_weird）");
  });

  it("localizes truncation, context limit, request rejection and sibling provider failure codes without leaking raw codes", () => {
    const codes = [
      "provider_output_truncated",
      "provider_context_limit",
      "provider_request_rejected",
      "provider_output_refused",
      "provider_unavailable",
      "provider_rate_limited",
      "provider_budget_exhausted",
      "provider_budget_unknown",
      "provider_secret_missing",
      "provider_configuration_invalid",
      "invalid_provider_output",
      "provider_no_result",
      "token_budget_exhausted",
    ] as const;

    for (const code of codes) {
      const turnLabel = turnReasonLabel(code);
      expect(TURN_REASON_LABEL[code]).toBeTruthy();
      expect(turnLabel).toBe(TURN_REASON_LABEL[code]);
      expect(turnLabel).not.toContain(code);
      expect(turnLabel).not.toMatch(/^模型服务返回错误（/);

      const failureLabel = modelFailureLabel(code);
      expect(MODEL_FAILURE_LABEL[code]).toBeTruthy();
      expect(failureLabel).toBe(MODEL_FAILURE_LABEL[code]);
      expect(failureLabel).not.toContain(code);
      expect(failureLabel).not.toMatch(/^请求没有返回可用结果（/);
    }

    expect(turnReasonLabel("provider_output_truncated")).toBe("模型输出达到单次长度上限而被截断");
    expect(turnReasonLabel("provider_context_limit")).toBe("输入与对话上下文超出模型窗口上限");
    expect(turnReasonLabel("provider_request_rejected")).toBe("模型服务拒绝了本次请求");
    expect(turnReasonLabel("search_timeout")).toBe("搜索超时");

    expect(modelFailureLabel("provider_output_truncated")).toBe("输出达到单次长度上限被截断");
    expect(modelFailureLabel("provider_context_limit")).toBe("上下文超出窗口上限");
    expect(modelFailureLabel("provider_request_rejected")).toBe("请求被模型服务拒绝");
    expect(modelFailureLabel("provider_cancelled")).toBe("已取消");
    expect(modelFailureLabel(null)).toBe("请求没有返回可用结果");
    expect(modelFailureLabel(undefined)).toBe("请求没有返回可用结果");
    expect(modelFailureLabel("custom_code")).toBe("请求没有返回可用结果（custom_code）");
  });
});

describe("L1 streaming labels", () => {
  it("labels model phases and retry attempts", () => {
    expect(MODEL_PHASE_LABEL).toEqual({
      connecting: "连接模型中",
      thinking: "模型思考中",
      responding: "整理回复中",
      retrying: "重试模型请求",
    });
    expect(modelPhaseLabel("connecting")).toBe("连接模型中");
    expect(modelPhaseLabel("retrying", 2)).toBe("重试模型请求（第 2 次）");
    expect(modelPhaseLabel(null)).toBe("模型思考中");
    expect(modelPhaseLabel("unknown", 0)).toBe("模型思考中");
  });

  it("labels formulas and interim counts", () => {
    expect(formulaLabel("growth_percent")).toBe("增长率");
    expect(formulaLabel("margin_percent")).toBe("利润率");
    expect(formulaLabel("difference")).toBe("差额");
    expect(formulaLabel("ratio")).toBe("比率");
    expect(formulaLabel("cagr")).toBe("复合年增长率");
    expect(formulaLabel("sum")).toBe("合计");
    expect(formulaLabel("subtract")).toBe("净差额");
    expect(formulaLabel("weighted_average")).toBe("加权平均");
    expect(formulaLabel("ttm_sum")).toBe("滚动十二个月合计");
    expect(formulaLabel("custom")).toBe("custom");
    expect(PROCESS_NOTE_LABEL).toBe("思路 · 过程说明（非正式结论）");
    expect(LIVE_STAGED_TAG).toBe("运行中暂存");
    expect(formatInterimParts(null)).toEqual([]);
    expect(formatInterimSummary({ leads_count: 0, acquired_sources_count: 0, read_sources_count: 0, facts_count: 0, calculations_count: 0 })).toBe("");
    expect(formatInterimSummary({ leads_count: 6, acquired_sources_count: 2, read_sources_count: 2, facts_count: 3, calculations_count: 1 }))
      .toBe("发现 6 条线索 · 获取 2 份资料 · 记下 3 个数字 · 完成 1 项计算");
    expect(formatInterimSummary({ leads_count: 0, acquired_sources_count: 0, read_sources_count: 1, facts_count: 0, calculations_count: 0 }))
      .toBe("已读 1 份资料");
  });
});
