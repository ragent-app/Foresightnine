import { ApiError, ApiTransportError } from "../../api/http";

/** User-facing text for a failed request (spec §4.9: show retry timing next to the action). */
export function describeError(error: unknown): string {
  if (error instanceof ApiTransportError) return "网络连接失败，请检查网络后重试。";
  if (error instanceof ApiError) {
    if (error.status === 429 || error.status === 503) {
      const seconds = error.retryAfterMs !== null ? Math.ceil(error.retryAfterMs / 1000) : null;
      return seconds !== null && seconds > 0 ? `服务繁忙，请在 ${seconds} 秒后重试。` : "服务繁忙，请稍后重试。";
    }
    if (error.status === 409) return "研究在你提交前已经更新，请刷新后重试。";
    if (error.status === 422 && error.message === "Verify research tools for this model in model settings.") {
      return "请先到设置 → 模型，验证当前个人模型的研究工具调用。";
    }
    if (error.status === 422 && error.message.startsWith("This Messages endpoint is outside")) {
      return "该服务地址不在部署允许的个人 Messages 端点范围内。";
    }
    if (error.status === 422 && error.message.startsWith("Configure the Messages version")) {
      return "请在模型设置中填写 Messages 协议版本、认证方式和输出上限。";
    }
    if (error.status === 422 || error.status === 400) {
      const detail = error.details[0]?.message;
      return `提交的内容没有通过校验${detail ? `：${detail}` : "。"}`;
    }
    if (error.status === 403) return "没有权限执行这个操作。";
    if (error.status === 404) return "找不到这个研究或资料。";
    return `操作没有完成（${error.code}）。`;
  }
  return "操作没有完成，请重试。";
}
