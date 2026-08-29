import type {
  ChallengeMessageRequest,
  ChallengeOperationResponse,
  ConfirmChallengeRequest,
  CreateChallengeRequest,
  CreateSessionRequest,
  ErrorDetail,
  EvidenceSubmissionRequest,
  ErrorResponse,
  OperationResponse,
  ProbabilityOperationResponse,
  ProbabilityRequest,
  SessionOperationResponse,
  SseEvent,
  WorldlineBundleOperationResponse,
  WorldlineRequest,
} from "@foresightnine/contracts";
import { parseSseBody, parseSseText, type SseReplayEvent } from "./sse.js";
import { ContractValidationError, validateErrorResponse, validateResponse } from "./validation.js";

export interface ApiRequestOptions { readonly signal?: AbortSignal; readonly idempotencyKey?: string; }
export interface ApiClientOptions { readonly baseUrl: string; readonly fetcher?: typeof fetch; readonly idempotencyKeyFactory?: () => string; }
export interface SseOptions extends ApiRequestOptions { readonly lastEventId?: string; }

export function normalizeApiBaseUrl(value: unknown): string | null {
  if (typeof value !== "string" || value !== value.trim() || value.trim().length === 0) return null;
  try {
    const url = new URL(value);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) return null;
    return url.toString().replace(/\/$/u, "");
  } catch { return null; }
}

export function apiModeFor(baseUrl: unknown): "api" | "offline-mock" {
  return normalizeApiBaseUrl(baseUrl) ? "api" : "offline-mock";
}

function newIdempotencyKey(): string {
  const cryptoApi = globalThis.crypto;
  try {
    if (cryptoApi && typeof cryptoApi.randomUUID === "function") return cryptoApi.randomUUID();
    if (cryptoApi && typeof cryptoApi.getRandomValues === "function") {
      const bytes = cryptoApi.getRandomValues(new Uint8Array(16));
      return `f9-${Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
    }
  } catch { /* fail closed below when the runtime's secure source is unusable */ }
  throw new Error("Secure idempotency key generation is unavailable; inject idempotencyKeyFactory.");
}

function isAbortError(error: unknown): boolean {
  return (typeof DOMException !== "undefined" && error instanceof DOMException && error.name === "AbortError")
    || (error instanceof Error && error.name === "AbortError");
}

function abortError(): Error {
  if (typeof DOMException !== "undefined") return new DOMException("The operation was aborted.", "AbortError");
  const error = new Error("The operation was aborted.");
  error.name = "AbortError";
  return error;
}

function throwIfAborted(signal?: AbortSignal): void {
  if (signal?.aborted) throw abortError();
}

function retryAfter(value: string | null): number | undefined {
  if (!value) return undefined;
  const seconds = Number(value);
  if (Number.isFinite(seconds) && seconds >= 0) return seconds;
  const date = Date.parse(value);
  if (!Number.isNaN(date)) return Math.max(0, Math.ceil((date - Date.now()) / 1000));
  return undefined;
}

export class ApiError extends Error {
  public readonly status: number;
  public readonly errorCode: ErrorResponse["error_code"] | undefined;
  public readonly retryable: boolean;
  public readonly retryAfter: number | undefined;
  public readonly requestId: string | undefined;
  public readonly details: readonly ErrorDetail[];

  public constructor(status: number, response: ErrorResponse | null, retryAfterSeconds?: number) {
    super(`API request failed with status ${status}.`);
    this.name = "ApiError";
    this.status = status;
    this.errorCode = response?.error_code;
    this.retryable = response?.retryable ?? (status >= 500 || status === 429);
    this.retryAfter = retryAfterSeconds ?? undefined;
    this.requestId = response?.request_id;
    this.details = response?.details ?? [];
  }
}

export class ApiTransportError extends Error {
  public constructor() { super("API request could not be completed."); this.name = "ApiTransportError"; }
}

export class ApiClient {
  private readonly baseUrl: string;
  private readonly fetcher: typeof fetch;
  private readonly idempotencyKeyFactory: () => string;

  public constructor(options: ApiClientOptions) {
    const normalized = normalizeApiBaseUrl(options.baseUrl);
    if (!normalized) throw new Error("A valid API base URL is required.");
    this.baseUrl = normalized;
    this.fetcher = options.fetcher ?? fetch;
    this.idempotencyKeyFactory = options.idempotencyKeyFactory ?? newIdempotencyKey;
  }

  private url(path: string): string { return new URL(path.replace(/^\//u, ""), `${this.baseUrl}/`).toString(); }

  public async request<T>(path: string, init: RequestInit, contract: Parameters<typeof validateResponse>[1]): Promise<T> {
    const method = (init.method ?? "GET").toUpperCase();
    const headers = new Headers(init.headers);
    if (method === "POST") {
      const idempotencyKey = headers.get("Idempotency-Key") ?? this.idempotencyKeyFactory();
      if (idempotencyKey.length < 8 || idempotencyKey.length > 128) throw new Error("Invalid idempotency key.");
      headers.set("Idempotency-Key", idempotencyKey);
      if (init.body !== undefined && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    }
    const requestInit: RequestInit = { ...init, method, headers, credentials: init.credentials ?? "omit" };
    let response: Response;
    try { response = await this.fetcher(this.url(path), requestInit); }
    catch (error) {
      if (isAbortError(error)) throw error;
      throw new ApiTransportError();
    }
    if (!response.ok) {
      let parsed: ErrorResponse | null = null;
      try { parsed = validateErrorResponse(await response.json()); } catch { parsed = null; }
      throw new ApiError(response.status, parsed, retryAfter(response.headers.get("Retry-After")));
    }
    try { return validateResponse<T>(await response.json(), contract); }
    catch (error) { if (error instanceof ContractValidationError) throw error; throw new ApiTransportError(); }
  }

  private post<T>(path: string, body: object, contract: Parameters<typeof validateResponse>[1], options: ApiRequestOptions = {}): Promise<T> {
    const idempotencyKey = options.idempotencyKey ?? this.idempotencyKeyFactory();
    if (idempotencyKey.length < 8 || idempotencyKey.length > 128) throw new Error("Invalid idempotency key.");
    const init: RequestInit = {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(body),
      credentials: "omit",
    };
    if (options.signal) init.signal = options.signal;
    return this.request<T>(path, init, contract);
  }

  public createSession(request: CreateSessionRequest = {}, options?: ApiRequestOptions): Promise<SessionOperationResponse> {
    return this.post("v1/sessions", request, "SessionOperationResponse", options);
  }

  public createChallenge(request: CreateChallengeRequest, options?: ApiRequestOptions): Promise<ChallengeOperationResponse> {
    return this.post("v1/challenges", request, "ChallengeOperationResponse", options);
  }

  public submitChallengeReply(challengeId: string, request: ChallengeMessageRequest, options?: ApiRequestOptions): Promise<ChallengeOperationResponse> {
    return this.post(`v1/challenges/${encodeURIComponent(challengeId)}/messages`, request, "ChallengeOperationResponse", options);
  }

  public startChallengeReply(challengeId: string, request: ChallengeMessageRequest, options?: ApiRequestOptions): Promise<OperationResponse> {
    return this.post(`v1/challenges/${encodeURIComponent(challengeId)}/messages/async`, request, "OperationResponse", options);
  }

  public submitEvidence(challengeId: string, request: EvidenceSubmissionRequest, options?: ApiRequestOptions): Promise<ChallengeOperationResponse> {
    return this.post(`v1/challenges/${encodeURIComponent(challengeId)}/evidence`, request, "ChallengeOperationResponse", options);
  }

  public confirmChallenge(challengeId: string, request: ConfirmChallengeRequest, options?: ApiRequestOptions): Promise<ChallengeOperationResponse> {
    return this.post(`v1/challenges/${encodeURIComponent(challengeId)}/confirm`, request, "ChallengeOperationResponse", options);
  }

  public createWorldlines(request: WorldlineRequest, options?: ApiRequestOptions): Promise<WorldlineBundleOperationResponse> {
    return this.post("v1/worldlines", request, "WorldlineBundleOperationResponse", options);
  }

  public assessProbability(request: ProbabilityRequest, options?: ApiRequestOptions): Promise<ProbabilityOperationResponse> {
    return this.post("v1/probabilities/assess", request, "ProbabilityOperationResponse", options);
  }

  public cancelOperation(operationId: string, cancelToken: string, options?: ApiRequestOptions): Promise<OperationResponse> {
    return this.post(`v1/operations/${encodeURIComponent(operationId)}/cancel`, { cancel_token: cancelToken }, "OperationResponse", options);
  }

  public resumeOperation(operationId: string, resumeToken: string, options?: ApiRequestOptions): Promise<OperationResponse> {
    return this.post(`v1/operations/${encodeURIComponent(operationId)}/resume`, { resume_token: resumeToken }, "OperationResponse", options);
  }

  public async *streamOperationEvents(operationId: string, options: SseOptions = {}): AsyncGenerator<SseReplayEvent> {
    const headers: Record<string, string> = { Accept: "text/event-stream" };
    if (options.lastEventId) headers["Last-Event-ID"] = options.lastEventId;
    let response: Response;
    try { response = await this.fetcher(this.url(`v1/operations/${encodeURIComponent(operationId)}/events`), { method: "GET", headers, credentials: "omit", ...(options.signal ? { signal: options.signal } : {}) }); }
    catch (error) {
      if (isAbortError(error)) throw error;
      throw new ApiTransportError();
    }
    if (!response.ok) {
      let parsed: ErrorResponse | null = null;
      try { parsed = validateErrorResponse(await response.json()); } catch { parsed = null; }
      throw new ApiError(response.status, parsed, retryAfter(response.headers.get("Retry-After")));
    }
    if (response.body) {
      yield* parseSseBody(response.body, options.signal);
      return;
    }
    throwIfAborted(options.signal);
    let text: string;
    try { text = await response.text(); }
    catch (error) {
      if (isAbortError(error)) throw error;
      throw new ApiTransportError();
    }
    throwIfAborted(options.signal);
    yield* parseSseText(text);
  }
}
