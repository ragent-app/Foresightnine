import type { ModelConfig, ModelProtocol } from "../api/settings";

export const PROTOCOL_LABEL: Readonly<Record<ModelProtocol, string>> = {
  openai_chat_completions: "OpenAI Chat Completions",
  anthropic_messages: "Anthropic Messages",
};

/** Only OpenAI-compatible endpoints have a connection probe (main.py test_model_config). */
export function canTestProtocol(protocol: ModelProtocol | undefined): boolean {
  return protocol === "openai_chat_completions";
}

function isPrivateIpv4(host: string): boolean {
  const parts = host.split(".").map(Number);
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return false;
  const [a, b] = parts as [number, number, number, number];
  return a === 10 || a === 127 || a === 0 || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || (a === 100 && b >= 64 && b <= 127);
}

/**
 * Early, user-facing check of openai_compatible.from_account_values: public HTTPS on port 443.
 * The backend still resolves DNS and decides; this only catches obvious mistakes.
 */
export function baseUrlProblem(raw: string): string | null {
  const value = raw.trim();
  if (!value) return "请输入服务地址";
  if (value !== raw) return "地址前后不能有空格";
  if (value.includes("\\")) return "地址格式不正确";
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return "地址格式不正确";
  }
  if (url.protocol !== "https:") return "只支持 https 地址";
  if (url.username || url.password) return "地址不能包含账号或密码";
  if (url.port && url.port !== "443") return "只支持 443 端口";
  if (url.search || url.hash || value.includes("?") || value.includes("#")) return "地址不能包含 ? 或 # 之后的部分";
  const host = url.hostname.toLowerCase().replace(/^\[|\]$/g, "");
  if (host === "localhost" || host.endsWith(".localhost") || host.endsWith(".local") || host.endsWith(".internal") || isPrivateIpv4(host) || host.includes(":")) {
    return "只支持公网地址";
  }
  return null;
}

function origin(raw: string | undefined): string | null {
  if (!raw) return null;
  try {
    const url = new URL(raw);
    return `${url.protocol}//${url.hostname}:${url.port || "443"}`;
  } catch {
    return null;
  }
}

/** A saved personal configuration, active or not (platform mode keeps it for switching back). */
export function hasSavedPersonal(config: ModelConfig | undefined): boolean {
  return Boolean(config?.base_url && config.model && config.key_hint);
}

/**
 * model_credentials.update: a saved key can be kept only while the origin (scheme, host, port)
 * and protocol stay the same; a first personal configuration always needs a key.
 */
export function keyRequired(saved: ModelConfig | undefined, protocol: ModelProtocol, baseUrl: string): boolean {
  // A platform-mode account may still hold a saved, inactive personal configuration with its key.
  if (!saved || !saved.key_hint) return true;
  return saved.protocol !== protocol || origin(saved.base_url) !== origin(baseUrl.trim());
}

export function apiKeyProblem(raw: string, required: boolean): string | null {
  if (!raw) return required ? "请输入 API Key" : null;
  if (raw !== raw.trim()) return "API Key 前后不能有空格";
  if (raw.length < 8) return "API Key 至少 8 个字符";
  if (raw.length > 512) return "API Key 过长";
  return null;
}

export function modelNameProblem(raw: string): string | null {
  if (!raw) return "请输入模型名称";
  if (raw !== raw.trim()) return "模型名称前后不能有空格";
  if (raw.length > 128) return "模型名称不能超过 128 个字符";
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f]/.test(raw)) return "模型名称不能包含控制字符";
  return null;
}

/** Share of the period's quota used, 0–1, for the usage bar. */
export function quotaShare(consumed: number, reserved: number, limit: number): { used: number; reserved: number } {
  if (limit <= 0) return { used: 0, reserved: 0 };
  const used = Math.min(1, consumed / limit);
  return { used, reserved: Math.min(1 - used, reserved / limit) };
}
