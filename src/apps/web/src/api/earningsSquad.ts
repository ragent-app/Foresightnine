import type { ResearchV2 } from "@foresightnine/contracts";
import { apiBaseUrl } from "./config";
import { apiRequest, type ApiRequestOptions } from "./http";
import { StreamFallbackError } from "./stream";
import type { TerminalOperationStatus } from "./types";

const enc = encodeURIComponent;

export type EarningsExportFormat = "markdown" | "csv" | "xlsx";
export type EarningsExportReportMode = "auto" | "memo" | "deep_report";

export type EarningsOrgActionKind = ResearchV2.OrgGraphEvent["action_kind"];
export type EarningsOrgRuntimeState = ResearchV2.OrgGraphEvent["runtime_state"];
export type EarningsModelSlot = NonNullable<ResearchV2.OrgGraphEvent["slot"]>;

export type EarningsOrgGraphStreamEvent = ResearchV2.OrgGraphEvent & {
  readonly operation_id?: string;
};

export interface EarningsSquadTerminalData {
  readonly operation_id: string;
  readonly operation_status: TerminalOperationStatus;
  readonly version_id: string | null;
}

export type EarningsSquadSseFrame =
  | { event: "org_graph_event"; id: string | null; data: EarningsOrgGraphStreamEvent }
  | { event: "run.terminal"; id: string | null; data: EarningsSquadTerminalData };

export interface EarningsExportDownload {
  readonly blob: Blob;
  readonly filename: string;
  readonly format: EarningsExportFormat;
  readonly mediaType: string | null;
  readonly dossierId: string | null;
  readonly redacted: boolean;
}

const ORG_ACTION_KINDS = new Set<EarningsOrgActionKind>([
  "READING",
  "SEARCHING",
  "INSPECTING",
  "DELEGATING",
  "WAITING_SUBORDINATES",
  "RESPONDING",
  "NEEDS_USER",
  "BLOCKED",
  "FAILED",
]);

const ORG_RUNTIME_STATES = new Set<EarningsOrgRuntimeState>([
  "idle",
  "running",
  "waiting",
  "completed",
  "failed",
  "blocked",
  "cancelled",
  "skipped",
]);

const MODEL_SLOTS = new Set<EarningsModelSlot>([
  "fast_extractor",
  "deep_reasoner",
  "bear_reviewer",
  "writer",
]);

const EDGE_KINDS = new Set<NonNullable<ResearchV2.OrgEdgeEvent["edge_kind"]>>([
  "dispatch",
  "report",
  "review",
  "steering",
]);

const TERMINAL_STATUSES = new Set<TerminalOperationStatus>([
  "completed",
  "failed",
  "cancelled",
]);

const EXPORT_EXTENSIONS: Readonly<Record<EarningsExportFormat, string>> = {
  markdown: "md",
  csv: "csv",
  xlsx: "xlsx",
};

function squadBasePath(topicId: string): string {
  return `/v2/research/topics/${enc(topicId)}/earnings-squad`;
}

function buildRequestOptions(options?: {
  method?: ApiRequestOptions["method"];
  body?: unknown;
  idempotencyKey?: string | undefined;
  signal?: AbortSignal | undefined;
}): ApiRequestOptions {
  const out: ApiRequestOptions = {};
  if (options?.method !== undefined) out.method = options.method;
  if (options?.body !== undefined) out.body = options.body;
  if (options?.idempotencyKey !== undefined) out.idempotencyKey = options.idempotencyKey;
  if (options?.signal !== undefined) out.signal = options.signal;
  return out;
}

function isRecord(value: unknown): value is Readonly<Record<string, unknown>> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nonEmptyStr(value: unknown, limit: number): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed ? trimmed.slice(0, limit) : null;
}

function abortError(): Error {
  if (typeof DOMException !== "undefined") {
    return new DOMException("The operation was aborted.", "AbortError");
  }
  const error = new Error("The operation was aborted.");
  error.name = "AbortError";
  return error;
}

function isAbortError(error: unknown): boolean {
  return typeof error === "object" && error !== null && (error as { name?: unknown }).name === "AbortError";
}

function parseEdgeEvent(raw: unknown): ResearchV2.OrgEdgeEvent | null {
  if (!isRecord(raw)) return null;
  const from_node_id = nonEmptyStr(raw["from_node_id"], 120);
  const to_node_id = nonEmptyStr(raw["to_node_id"], 120);
  if (!from_node_id || !to_node_id) return null;
  const edgeKindRaw = nonEmptyStr(raw["edge_kind"], 32);
  const label = nonEmptyStr(raw["label"], 240);
  return {
    from_node_id,
    to_node_id,
    is_review: Boolean(raw["is_review"]),
    ...(edgeKindRaw && EDGE_KINDS.has(edgeKindRaw as NonNullable<ResearchV2.OrgEdgeEvent["edge_kind"]>)
      ? { edge_kind: edgeKindRaw as NonNullable<ResearchV2.OrgEdgeEvent["edge_kind"]> }
      : {}),
    ...(label !== null ? { label } : {}),
  };
}

function parseOrgGraphPayload(raw: Record<string, unknown>): EarningsOrgGraphStreamEvent | null {
  const sequence = raw["sequence"];
  if (typeof sequence !== "number" || !Number.isInteger(sequence) || sequence < 1) return null;

  const stage = nonEmptyStr(raw["stage"], 64);
  const node_id = nonEmptyStr(raw["node_id"], 120);
  const role = nonEmptyStr(raw["role"], 64);
  const display_name = nonEmptyStr(raw["display_name"], 120);
  const runtimeRaw = nonEmptyStr(raw["runtime_state"], 32);
  const actionRaw = nonEmptyStr(raw["action_kind"], 32);
  const current_activity = nonEmptyStr(raw["current_activity"], 400);

  if (!stage || !node_id || !role || !display_name || !runtimeRaw || !actionRaw || !current_activity) {
    return null;
  }
  if (!ORG_RUNTIME_STATES.has(runtimeRaw as EarningsOrgRuntimeState)) return null;
  if (!ORG_ACTION_KINDS.has(actionRaw as EarningsOrgActionKind)) return null;

  const blockerText = typeof raw["blocker_text"] === "string" ? raw["blocker_text"].slice(0, 500) : null;
  const edgeEvent = raw["edge_event"] !== undefined && raw["edge_event"] !== null
    ? parseEdgeEvent(raw["edge_event"])
    : null;
  const timestamp = nonEmptyStr(raw["timestamp"], 100);
  const slotRaw = nonEmptyStr(raw["slot"], 32);
  const parentNodeId = nonEmptyStr(raw["parent_node_id"], 120);
  const operationId = nonEmptyStr(raw["operation_id"], 120);

  return {
    sequence,
    stage,
    node_id,
    role,
    display_name,
    runtime_state: runtimeRaw as EarningsOrgRuntimeState,
    action_kind: actionRaw as EarningsOrgActionKind,
    current_activity,
    blocker_text: blockerText,
    edge_event: edgeEvent,
    ...(timestamp !== null ? { timestamp } : {}),
    ...(slotRaw && MODEL_SLOTS.has(slotRaw as EarningsModelSlot) ? { slot: slotRaw as EarningsModelSlot } : {}),
    ...(parentNodeId !== null ? { parent_node_id: parentNodeId } : {}),
    ...(operationId !== null ? { operation_id: operationId } : {}),
  };
}

/** POST /v2/research/topics/{topic_id}/earnings-squad/run */
export function runEarningsSquad(
  topicId: string,
  request: ResearchV2.EarningsSquadApiRunRequest = {},
  options?: { idempotencyKey?: string; signal?: AbortSignal },
): Promise<ResearchV2.EarningsSquadRunResponse> {
  return apiRequest<ResearchV2.EarningsSquadRunResponse>(
    `${squadBasePath(topicId)}/run`,
    buildRequestOptions({
      method: "POST",
      body: request,
      idempotencyKey: options?.idempotencyKey,
      signal: options?.signal,
    }),
  );
}

/** GET /v2/research/topics/{topic_id}/earnings-squad/dossier */
export function getEarningsDossier(
  topicId: string,
  signal?: AbortSignal,
): Promise<ResearchV2.EarningsSquadDossierResponse> {
  return apiRequest<ResearchV2.EarningsSquadDossierResponse>(
    `${squadBasePath(topicId)}/dossier`,
    buildRequestOptions({ signal }),
  );
}

/** POST /v2/research/topics/{topic_id}/earnings-squad/deep-report */
export function renderDeepReport(
  topicId: string,
  request: ResearchV2.EarningsSquadDeepReportRequest = {},
  options?: { idempotencyKey?: string; signal?: AbortSignal },
): Promise<ResearchV2.EarningsSquadDeepReportResponse> {
  return apiRequest<ResearchV2.EarningsSquadDeepReportResponse>(
    `${squadBasePath(topicId)}/deep-report`,
    buildRequestOptions({
      method: "POST",
      body: request,
      idempotencyKey: options?.idempotencyKey,
      signal: options?.signal,
    }),
  );
}

/** POST /v2/research/topics/{topic_id}/earnings-squad/steer */
export function steerEarningsSquad(
  topicId: string,
  input: string | ResearchV2.EarningsSquadSteerRequest,
  options?: { idempotencyKey?: string; signal?: AbortSignal },
): Promise<ResearchV2.EarningsSquadSteerResponse> {
  const body: ResearchV2.EarningsSquadSteerRequest = typeof input === "string" ? { text: input } : input;
  return apiRequest<ResearchV2.EarningsSquadSteerResponse>(
    `${squadBasePath(topicId)}/steer`,
    buildRequestOptions({
      method: "POST",
      body,
      idempotencyKey: options?.idempotencyKey,
      signal: options?.signal,
    }),
  );
}

/** Relative API path for GET /v2/research/topics/{topic_id}/earnings-squad/export */
export function exportEarningsDossierPath(
  topicId: string,
  format: EarningsExportFormat = "markdown",
  reportMode: EarningsExportReportMode = "auto",
): string {
  const params = new URLSearchParams({ format });
  if (reportMode !== "auto") {
    params.set("report_mode", reportMode);
  }
  return `${squadBasePath(topicId)}/export?${params.toString()}`;
}

/** Full origin-checked URL for downloading a dossier export. */
export function exportEarningsDossierUrl(
  topicId: string,
  format: EarningsExportFormat = "markdown",
  reportMode: EarningsExportReportMode = "auto",
): string {
  return `${apiBaseUrl()}${exportEarningsDossierPath(topicId, format, reportMode)}`;
}

function parseFilenameFromDisposition(disposition: string | null, fallbackExt: string): string {
  if (disposition) {
    const utf8Match = /filename\*=UTF-8''([^;\s]+)/i.exec(disposition);
    if (utf8Match?.[1]) {
      try {
        return decodeURIComponent(utf8Match[1]);
      } catch {
        return utf8Match[1];
      }
    }
    const quotedMatch = /filename="([^"]+)"/i.exec(disposition);
    if (quotedMatch?.[1]) return quotedMatch[1];
    const bareMatch = /filename=([^;\s]+)/i.exec(disposition);
    if (bareMatch?.[1]) return bareMatch[1];
  }
  return `earnings-dossier.${fallbackExt}`;
}

function triggerBrowserSave(blob: Blob, filename: string): void {
  if (typeof document === "undefined" || typeof URL === "undefined" || typeof URL.createObjectURL !== "function") {
    return;
  }
  const objectUrl = URL.createObjectURL(blob);
  const revoke = typeof URL.revokeObjectURL === "function" ? URL.revokeObjectURL.bind(URL) : null;
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  if (revoke) {
    setTimeout(() => revoke(objectUrl), 1000);
  }
}

/**
 * Authenticated multi-format dossier export download (`markdown` | `csv` | `xlsx`).
 * Triggers browser file download via Blob URL when available unless `autoSave: false`.
 */
export async function downloadEarningsExport(
  topicId: string,
  format: EarningsExportFormat,
  options?: {
    reportMode?: EarningsExportReportMode;
    autoSave?: boolean;
    signal?: AbortSignal;
  },
): Promise<EarningsExportDownload> {
  const path = exportEarningsDossierPath(topicId, format, options?.reportMode ?? "auto");
  const response = await apiRequest<Response>(path, {
    accept: "*/*",
    raw: true,
    ...(options?.signal !== undefined ? { signal: options.signal } : {}),
  });
  const blob = await response.blob();
  const filename = parseFilenameFromDisposition(
    response.headers.get("Content-Disposition"),
    EXPORT_EXTENSIONS[format],
  );
  const dossierId = response.headers.get("X-Dossier-Id");
  const redacted = response.headers.get("X-Export-Redacted") === "true";
  const mediaType = response.headers.get("Content-Type");

  if (options?.autoSave !== false) {
    triggerBrowserSave(blob, filename);
  }

  return {
    blob,
    filename,
    format,
    mediaType,
    dossierId,
    redacted,
  };
}

/** Parse one SSE frame emitted by GET /v2/research/topics/{topic_id}/earnings-squad/events. */
export function parseOrgGraphSseFrame(rawFrame: string): EarningsSquadSseFrame | null {
  if (!rawFrame.trim()) return null;
  let eventType = "message";
  let id: string | null = null;
  const dataLines: string[] = [];

  for (const line of rawFrame.split(/\r?\n/u)) {
    if (!line || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    const value = colon === -1 ? "" : line[colon + 1] === " " ? line.slice(colon + 2) : line.slice(colon + 1);
    if (field === "event") eventType = value;
    else if (field === "data") dataLines.push(value);
    else if (field === "id" && !value.includes("\0")) id = value;
  }

  if (dataLines.length === 0) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(dataLines.join("\n"));
  } catch {
    return null;
  }
  if (!isRecord(parsed)) return null;

  if (eventType === "org_graph_event") {
    const payload = parseOrgGraphPayload(parsed);
    return payload ? { event: "org_graph_event", id, data: payload } : null;
  }
  if (eventType === "run.terminal") {
    const operation_id = nonEmptyStr(parsed["operation_id"], 120);
    const statusRaw = nonEmptyStr(parsed["operation_status"], 32);
    if (!operation_id || !statusRaw || !TERMINAL_STATUSES.has(statusRaw as TerminalOperationStatus)) {
      return null;
    }
    const version_id = typeof parsed["version_id"] === "string" ? parsed["version_id"].slice(0, 120) : null;
    return {
      event: "run.terminal",
      id,
      data: {
        operation_id,
        operation_status: statusRaw as TerminalOperationStatus,
        version_id,
      },
    };
  }
  return null;
}

export async function* parseOrgGraphSseStream(
  body: ReadableStream<Uint8Array>,
  signal?: AbortSignal,
): AsyncGenerator<EarningsSquadSseFrame> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const onAbort = () => {
    void reader.cancel().catch(() => undefined);
  };
  if (signal) {
    if (signal.aborted) {
      await reader.cancel().catch(() => undefined);
      throw abortError();
    }
    signal.addEventListener("abort", onAbort, { once: true });
  }
  try {
    while (true) {
      if (signal?.aborted) throw abortError();
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      const frames = buffer.split(/\r?\n\r?\n/u);
      buffer = frames.pop() ?? "";
      for (const rawFrame of frames) {
        const frame = parseOrgGraphSseFrame(rawFrame);
        if (frame) yield frame;
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) {
      for (const rawFrame of buffer.split(/\r?\n\r?\n/u)) {
        const frame = parseOrgGraphSseFrame(rawFrame);
        if (frame) yield frame;
      }
    }
  } finally {
    signal?.removeEventListener("abort", onAbort);
    await reader.cancel().catch(() => undefined);
  }
}

/** Open an authenticated SSE stream for `org_graph_event` frames. */
export async function openEarningsSquadEventStream(params: {
  topicId: string;
  after?: number;
  lastEventId?: string | null;
  signal?: AbortSignal;
}): Promise<AsyncGenerator<EarningsSquadSseFrame>> {
  const { topicId, after = 0, lastEventId, signal } = params;
  const seqFloor = Number.isInteger(after) && after >= 0 ? after : 0;
  const path = `${squadBasePath(topicId)}/events?after=${enc(String(seqFloor))}`;
  const options: ApiRequestOptions = {
    accept: "text/event-stream",
    raw: true,
    ...(lastEventId ? { headers: { "Last-Event-ID": lastEventId } } : {}),
    ...(signal !== undefined ? { signal } : {}),
  };
  const response = await apiRequest<Response>(path, options);
  const contentType = response.headers.get("content-type")?.toLowerCase() ?? "";
  if (!contentType.includes("text/event-stream") || !response.body) {
    await response.body?.cancel().catch(() => undefined);
    throw new StreamFallbackError("non_sse_response");
  }
  return parseOrgGraphSseStream(response.body, signal);
}

/**
 * Subscribe to sequenced `org_graph_event` SSE frames with strict monotonic sequence deduplication.
 * Returns an unsubscribe function that cancels the stream cleanly.
 */
export function subscribeEarningsSquadEvents(params: {
  topicId: string;
  after?: number;
  lastEventId?: string | null;
  signal?: AbortSignal;
  onEvent: (event: EarningsOrgGraphStreamEvent) => void;
  onTerminal?: (terminal: EarningsSquadTerminalData) => void;
  onError?: (error: unknown) => void;
  onClose?: () => void;
}): () => void {
  const controller = new AbortController();
  const externalSignal = params.signal;
  const forwardAbort = () => controller.abort();

  if (externalSignal) {
    if (externalSignal.aborted) {
      controller.abort();
      return () => undefined;
    }
    externalSignal.addEventListener("abort", forwardAbort, { once: true });
  }

  let highestSequence = Number.isInteger(params.after) && (params.after ?? 0) >= 0 ? (params.after ?? 0) : 0;
  let sawTerminal = false;

  void (async () => {
    try {
      const stream = await openEarningsSquadEventStream({
        topicId: params.topicId,
        after: highestSequence,
        ...(params.lastEventId !== undefined ? { lastEventId: params.lastEventId } : {}),
        signal: controller.signal,
      });
      for await (const frame of stream) {
        if (controller.signal.aborted) break;
        if (frame.event === "org_graph_event") {
          if (frame.data.sequence <= highestSequence) continue;
          highestSequence = frame.data.sequence;
          params.onEvent(frame.data);
        } else if (frame.event === "run.terminal") {
          sawTerminal = true;
          params.onTerminal?.(frame.data);
          break;
        }
      }
      if (!controller.signal.aborted && !sawTerminal) {
        params.onClose?.();
      }
    } catch (error) {
      if (!controller.signal.aborted && !isAbortError(error)) {
        params.onError?.(error);
      }
    } finally {
      externalSignal?.removeEventListener("abort", forwardAbort);
    }
  })();

  return () => {
    externalSignal?.removeEventListener("abort", forwardAbort);
    controller.abort();
  };
}
