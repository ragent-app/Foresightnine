import { readSession, clearSession } from "../auth/session";
import { apiBaseUrl } from "./config";

export interface ApiErrorDetail {
  location: string;
  message: string;
}

export class ApiError extends Error {
  status: number;
  code: string;
  requestId: string | null;
  retryable: boolean;
  retryAfterMs: number | null;
  details: ApiErrorDetail[];

  constructor(init: {
    status: number;
    code: string;
    message: string;
    requestId: string | null;
    retryable: boolean;
    retryAfterMs: number | null;
    details: ApiErrorDetail[];
  }) {
    super(init.message);
    this.name = "ApiError";
    this.status = init.status;
    this.code = init.code;
    this.requestId = init.requestId;
    this.retryable = init.retryable;
    this.retryAfterMs = init.retryAfterMs;
    this.details = init.details;
  }
}

export class ApiTransportError extends Error {
  constructor(message = "transport_error", options?: { cause?: unknown }) {
    super(message, options);
    this.name = "ApiTransportError";
  }
}

const MAX_TIMER_MS = 2147483647;

export function parseRetryAfter(value: string | null): number | null {
  if (value === null) return null;
  const trimmed = value.trim();
  if (!trimmed) return null;
  let ms: number;
  if (/^\d+$/.test(trimmed)) {
    ms = Number(trimmed) * 1000;
  } else {
    const date = Date.parse(trimmed);
    if (Number.isNaN(date)) return null;
    ms = date - Date.now();
  }
  if (!Number.isFinite(ms)) return null;
  return Math.min(Math.max(ms, 0), MAX_TIMER_MS);
}

async function buildApiError(response: Response): Promise<ApiError> {
  const retryAfterMs = parseRetryAfter(response.headers.get("Retry-After"));
  const fallbackRetryable = response.status === 429 || response.status === 503;
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (typeof body === "object" && body !== null && !Array.isArray(body)) {
    const record = body as Record<string, unknown>;
    if (typeof record.error_code === "string" && record.error_code) {
      const details: ApiErrorDetail[] = Array.isArray(record.details)
        ? record.details.flatMap((item): ApiErrorDetail[] => {
            if (typeof item !== "object" || item === null) return [];
            const entry = item as Record<string, unknown>;
            return typeof entry.location === "string" && typeof entry.message === "string"
              ? [{ location: entry.location, message: entry.message }]
              : [];
          })
        : [];
      return new ApiError({
        status: response.status,
        code: record.error_code,
        message: typeof record.message === "string" ? record.message : record.error_code,
        requestId: typeof record.request_id === "string" ? record.request_id : null,
        retryable: typeof record.retryable === "boolean" ? record.retryable : fallbackRetryable,
        retryAfterMs,
        details,
      });
    }
  }
  const code = `http_${response.status}`;
  return new ApiError({
    status: response.status,
    code,
    message: code,
    requestId: null,
    retryable: fallbackRetryable,
    retryAfterMs,
    details: [],
  });
}

export interface ApiRequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  idempotencyKey?: string;
  signal?: AbortSignal;
  accept?: string;
  headers?: Record<string, string>;
  raw?: boolean;
  /** Multipart body (the browser sets the boundary); used by the streaming document upload only. */
  form?: FormData;
}

function isAbortError(error: unknown): boolean {
  return typeof error === "object" && error !== null && (error as { name?: unknown }).name === "AbortError";
}

export async function apiRequest<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  if (typeof path !== "string" || !path.startsWith("/")) {
    throw new TypeError("api_path_must_start_with_slash");
  }
  const base = new URL(apiBaseUrl());
  const url = new URL(path, `${apiBaseUrl()}/`);
  if (url.origin !== base.origin) {
    throw new TypeError("api_cross_origin_rejected");
  }

  const session = readSession();
  const method = options.method ?? "GET";
  // The API boundary accepts only JSON for POST and PUT (security.py, 415 otherwise), so a
  // write without a body still sends an empty JSON object (363 acceptance: continue, recover, logout).
  const body = options.form !== undefined ? undefined : options.body === undefined && (method === "POST" || method === "PUT") ? {} : options.body;
  const headers: Record<string, string> = {
    ...(options.headers ?? {}),
    Accept: options.accept ?? "application/json",
  };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
  if (session) headers.Authorization = `Bearer ${session.access_token}`;

  const init: RequestInit = {
    method,
    headers,
    credentials: "omit",
    redirect: "error",
    cache: "no-store",
  };
  if (body !== undefined) init.body = JSON.stringify(body);
  if (options.form !== undefined) init.body = options.form;
  if (options.signal) init.signal = options.signal;

  let response: Response;
  try {
    response = await fetch(url.toString(), init);
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw new ApiTransportError("transport_error", { cause: error });
  }

  if (!response.ok) {
    const error = await buildApiError(response);
    if (response.status === 401 && session) clearSession("expired");
    throw error;
  }
  if (response.status === 204) return undefined as T;
  if (options.raw) return response as T;
  return (await response.json()) as T;
}

export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}
