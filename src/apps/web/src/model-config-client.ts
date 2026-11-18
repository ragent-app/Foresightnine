import { normalizeApiBaseUrl } from "@foresightnine/api-client";
import type { OAuthSession } from "./oauth-client";

export type ModelConfigMode = "platform" | "personal";
export type ModelProtocol = "openai_chat_completions" | "anthropic_messages";
export type ModelConfigTestStatus = "ready" | "unavailable";

export interface ModelConfig {
  readonly schema_version: "v1";
  readonly mode: ModelConfigMode;
  readonly configured: boolean;
  readonly protocol?: ModelProtocol;
  readonly base_url?: string;
  readonly model?: string;
  readonly key_hint?: string;
  readonly updated_at?: string;
}

export interface ModelConfigUpdate {
  readonly mode: ModelConfigMode;
  readonly protocol?: ModelProtocol;
  readonly base_url?: string;
  readonly model?: string;
  readonly api_key?: string;
  readonly clear_api_key?: boolean;
}

export interface ModelRequestGenerationGuard {
  readonly next: () => number;
  readonly isCurrent: (generation: number) => boolean;
}

export function createModelRequestGenerationGuard(): ModelRequestGenerationGuard {
  let current = 0;
  return {
    next: () => ++current,
    isCurrent: (generation) => generation === current,
  };
}

export function modelConfigSessionKey(session: OAuthSession): string {
  return `${session.account.account_id}:${session.session_id}`;
}

export function isLoadedModelConfigOwner(loadedSessionKey: string | null, session: OAuthSession | null): boolean {
  return session !== null && loadedSessionKey === modelConfigSessionKey(session);
}

const MODEL_CONFIG_PATH = "v1/account/model-config";
const KEY_HINT_PATTERN = /^••••[A-Za-z0-9]{4}$/u;
const ISO_TIMESTAMP_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$/u;
const MODEL_NAME_MAX_LENGTH = 128;
const KEY_MAX_LENGTH = 512;

/** Normalize a user-supplied compatible endpoint without accepting URL credentials or query state. */
export function normalizeModelBaseUrl(value: unknown): string | null {
  if (typeof value !== "string" || value !== value.trim() || value.length === 0) return null;
  try {
    const url = new URL(value);
    const hostname = url.hostname.toLowerCase().replace(/\.$/u, "");
    if (url.protocol !== "https:" || (url.port && url.port !== "443") || !hostname || url.username || url.password || url.search || url.hash) return null;
    if (hostname === "localhost" || hostname.endsWith(".local") || !hostname.includes(".") || hostname.includes(":") || /^\d+(?:\.\d+){0,3}$/u.test(hostname)) return null;
    return url.toString().replace(/\/$/u, "");
  } catch {
    return null;
  }
}

function hasOnlyKeys(record: Record<string, unknown>, allowed: readonly string[]): boolean {
  const keys = new Set(allowed);
  return Object.keys(record).every((key) => keys.has(key));
}

function isCleanText(value: unknown, maxLength: number): value is string {
  return typeof value === "string"
    && value.length > 0
    && value.length <= maxLength
    && value === value.trim()
    && !/[\u0000-\u001f\u007f]/u.test(value);
}

function parseOptionalText(record: Record<string, unknown>, key: string, maxLength: number): string | undefined {
  const value = record[key];
  if (value === undefined || value === null) return undefined;
  if (!isCleanText(value, maxLength)) throw new Error("Invalid model configuration response.");
  return value;
}

function parseOptionalTimestamp(record: Record<string, unknown>): string | undefined {
  const value = record.updated_at;
  if (value === undefined || value === null) return undefined;
  if (typeof value !== "string" || !ISO_TIMESTAMP_PATTERN.test(value) || !Number.isFinite(Date.parse(value))) throw new Error("Invalid model configuration timestamp.");
  return value;
}

export function parseModelConfig(value: unknown): ModelConfig {
  if (!value || typeof value !== "object") throw new Error("Invalid model configuration response.");
  const record = value as Record<string, unknown>;
  if (!hasOnlyKeys(record, ["schema_version", "mode", "configured", "protocol", "base_url", "model", "key_hint", "updated_at"])) throw new Error("Invalid model configuration response.");
  if (record.schema_version !== "v1" || (record.mode !== "platform" && record.mode !== "personal") || typeof record.configured !== "boolean") throw new Error("Invalid model configuration response.");

  const protocolWire = record.protocol;
  if (protocolWire !== undefined && protocolWire !== null && protocolWire !== "openai_chat_completions" && protocolWire !== "anthropic_messages") throw new Error("Invalid model configuration protocol.");
  const baseUrlWire = record.base_url;
  const baseUrl = baseUrlWire === undefined || baseUrlWire === null ? undefined : normalizeModelBaseUrl(baseUrlWire);
  if (baseUrlWire !== undefined && baseUrlWire !== null && !baseUrl) throw new Error("Invalid model configuration response.");
  const model = parseOptionalText(record, "model", MODEL_NAME_MAX_LENGTH);
  const keyHintWire = record.key_hint;
  if (keyHintWire !== undefined && keyHintWire !== null && (typeof keyHintWire !== "string" || !KEY_HINT_PATTERN.test(keyHintWire))) throw new Error("Invalid model configuration key hint.");
  const updatedAt = parseOptionalTimestamp(record);

  if (record.mode === "personal" && (!record.configured || !baseUrl || !model || typeof keyHintWire !== "string")) throw new Error("Invalid personal model configuration response.");
  if (record.mode === "platform" && (protocolWire != null || baseUrl !== undefined || model !== undefined || keyHintWire !== undefined && keyHintWire !== null)) throw new Error("Invalid platform model configuration response.");

  return {
    schema_version: "v1",
    mode: record.mode,
    configured: record.configured,
    ...(record.mode === "personal" ? { protocol: protocolWire ?? "openai_chat_completions" } : {}),
    ...(baseUrl ? { base_url: baseUrl } : {}),
    ...(model ? { model } : {}),
    ...(typeof keyHintWire === "string" ? { key_hint: keyHintWire } : {}),
    ...(updatedAt ? { updated_at: updatedAt } : {}),
  };
}

export function parseModelConfigTestResponse(value: unknown): ModelConfigTestStatus {
  if (!value || typeof value !== "object") throw new Error("Invalid model configuration test response.");
  const record = value as Record<string, unknown>;
  if (!hasOnlyKeys(record, ["status"]) || (record.status !== "ready" && record.status !== "unavailable")) throw new Error("Invalid model configuration test response.");
  return record.status;
}

function endpoint(apiBaseUrl: string, suffix = ""): string {
  const normalized = normalizeApiBaseUrl(apiBaseUrl);
  if (!normalized) throw new Error("API base URL is invalid.");
  return new URL(`${MODEL_CONFIG_PATH}${suffix}`, `${normalized}/`).toString();
}

function authorization(session: OAuthSession): HeadersInit {
  if (!session || typeof session.access_token !== "string" || !/^\S+$/.test(session.access_token)) throw new Error("OAuth session is invalid.");
  return { Accept: "application/json", Authorization: `Bearer ${session.access_token}` };
}

async function checkedJson(response: Response): Promise<unknown> {
  if (!response.ok) throw new Error(`Model configuration request failed (${response.status}).`);
  return response.json() as Promise<unknown>;
}

function validateUpdate(update: ModelConfigUpdate): ModelConfigUpdate {
  if (!update || typeof update !== "object" || (update.mode !== "platform" && update.mode !== "personal")) throw new Error("Invalid model configuration update.");
  if (update.protocol !== undefined && update.protocol !== "openai_chat_completions" && update.protocol !== "anthropic_messages") throw new Error("Invalid model configuration update.");
  if (update.mode === "platform" && update.protocol !== undefined) throw new Error("Invalid model configuration update.");
  const normalizedBaseUrl = update.base_url === undefined ? undefined : normalizeModelBaseUrl(update.base_url);
  if (update.base_url !== undefined && !normalizedBaseUrl) throw new Error("Invalid model configuration update.");
  if (update.model !== undefined && !isCleanText(update.model, MODEL_NAME_MAX_LENGTH)) throw new Error("Invalid model configuration update.");
  if (update.api_key !== undefined && (!isCleanText(update.api_key, KEY_MAX_LENGTH) || KEY_HINT_PATTERN.test(update.api_key))) throw new Error("Invalid model configuration update.");
  if (update.clear_api_key !== undefined && update.clear_api_key !== true) throw new Error("Invalid model configuration update.");
  if (update.api_key !== undefined && update.clear_api_key === true) throw new Error("Invalid model configuration update.");
  return {
    mode: update.mode,
    ...(update.protocol !== undefined ? { protocol: update.protocol } : {}),
    ...(normalizedBaseUrl ? { base_url: normalizedBaseUrl } : {}),
    ...(update.model !== undefined ? { model: update.model } : {}),
    ...(update.api_key !== undefined ? { api_key: update.api_key } : {}),
    ...(update.clear_api_key === true ? { clear_api_key: true } : {}),
  };
}

export async function fetchModelConfig(apiBaseUrl: string, session: OAuthSession, signal?: AbortSignal): Promise<ModelConfig> {
  const response = await fetch(endpoint(apiBaseUrl), { method: "GET", headers: authorization(session), credentials: "omit", ...(signal ? { signal } : {}) });
  return parseModelConfig(await checkedJson(response));
}

export async function saveModelConfig(apiBaseUrl: string, session: OAuthSession, update: ModelConfigUpdate, signal?: AbortSignal): Promise<ModelConfig> {
  const body = validateUpdate(update);
  const response = await fetch(endpoint(apiBaseUrl), {
    method: "PUT",
    headers: { ...authorization(session), "Content-Type": "application/json" },
    credentials: "omit",
    body: JSON.stringify(body),
    ...(signal ? { signal } : {}),
  });
  return parseModelConfig(await checkedJson(response));
}

export async function deleteModelConfig(apiBaseUrl: string, session: OAuthSession, signal?: AbortSignal): Promise<ModelConfig> {
  const response = await fetch(endpoint(apiBaseUrl), { method: "DELETE", headers: authorization(session), credentials: "omit", ...(signal ? { signal } : {}) });
  return parseModelConfig(await checkedJson(response));
}

export async function testModelConfig(apiBaseUrl: string, session: OAuthSession, signal?: AbortSignal): Promise<ModelConfigTestStatus> {
  const response = await fetch(endpoint(apiBaseUrl, "/test"), { method: "POST", headers: { ...authorization(session), "Content-Type": "application/json" }, credentials: "omit", body: "{}", ...(signal ? { signal } : {}) });
  return parseModelConfigTestResponse(await checkedJson(response));
}
