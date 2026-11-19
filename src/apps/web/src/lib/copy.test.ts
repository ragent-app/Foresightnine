import { describe, expect, it } from "vitest";
import type { ResearchV2 } from "@foresightnine/contracts";
import {
  CREATED_BY_LABEL, INVALID_NOTE, OPERATION_STATUS_LABEL, PERMISSION_LABEL, PERMISSION_ORDER, RECOVERY_REASON_LABEL,
  RETRIEVAL_METHOD_LABEL, REVIEW_LABEL, SOURCE_KIND_LABEL, TURN_STATUS_LABEL, VERIFICATION_LABEL,
  calcChecks, gapText, recoveryReasonLabel, retrievalMethodLabel, toolLabel, toolReasonLabel, toolStatusLabel, turnReasonLabel,
  CALC_NOTE, LEAD_NOTE, VERIFY_NOTE, GAP_LABEL,
} from "./copy";

describe("labels", () => {
  it("maps operation and turn status", () => {
    expect(OPERATION_STATUS_LABEL).toEqual({ pending: "排队中", running: "研究中", completed: "已结束", failed: "运行中断", cancelled: "已取消" });
    expect(TURN_STATUS_LABEL).toEqual({ completed: "完成", partial: "部分完成" });
  });
  it("labels tools with fallbacks", () => {
    expect(toolLabel("read_sources")).toBe("阅读资料");
    expect(toolLabel("submit_research")).toBe("提交结论");
    expect(toolLabel("calculate_financials")).toBe("计算");
    expect(toolLabel("mystery")).toBe("mystery");
    expect(toolLabel(null)).toBe("工具");
  });
  it("labels tool status and reason", () => {
    expect(toolStatusLabel("invalid")).toBe("参数被拒");
    expect(toolStatusLabel("denied_or_unavailable")).toBe("无权限或不可用");
    expect(toolStatusLabel("weird")).toBe("weird");
    expect(toolReasonLabel("search_response_invalid")).toBe("搜索服务返回的格式有误");
    expect(toolReasonLabel("other")).toBe("other");
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
    expect(Object.keys(RECOVERY_REASON_LABEL)).toHaveLength(7);
    expect(recoveryReasonLabel("budget_exhausted")).toBe("预算已用完");
    expect(recoveryReasonLabel("???")).toBe("当前无法恢复");
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
    expect(retrievalMethodLabel("user_pdf")).toBe("上传 PDF");
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
    expect(Object.keys(GAP_LABEL)).toHaveLength(17);
  });
  it("handles provider_error, model_reported and unknown codes", () => {
    expect(gapText({ code: "provider_error", message: "429" })).toBe("模型服务返回错误。（429）");
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
});
