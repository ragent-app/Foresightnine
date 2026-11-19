import {
  sanitizeCurrentActivity,
  sanitizeInterimSummary,
  sanitizeProgressEvent,
  sanitizeRunProgress,
  sanitizeRunUsage,
} from "../lib/research";
import { apiRequest } from "./http";
import type {
  OperationStatus,
  ResearchSseFrame,
  StreamChannel,
  TerminalOperationStatus,
} from "./types";

const enc = encodeURIComponent;

const OPERATION_STATUSES = new Set<OperationStatus>(["pending", "running", "completed", "failed", "cancelled"]);
const TERMINAL_STATUSES = new Set<TerminalOperationStatus>(["completed", "failed", "cancelled"]);
const STREAM_CHANNELS = new Set<StreamChannel>(["thinking", "text", "tool_status", "draft_report"]);

const isRecord = (value: unknown): value is Readonly<Record<string, unknown>> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const nonNegInt = (value: unknown): number | null =>
  typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
const str = (value: unknown, limit: number): string | null =>
  typeof value === "string" ? value.slice(0, limit) : null;

export class StreamFallbackError extends Error {
  constructor(message = "stream_unavailable") {
    super(message);
    this.name = "StreamFallbackError";
  }
}

function abortError(): Error {
  if (typeof DOMException !== "undefined") return new DOMException("The operation was aborted.", "AbortError");
  const error = new Error("The operation was aborted.");
  error.name = "AbortError";
  return error;
}

function validateSsePayload(eventType: string, id: string | null, raw: unknown): ResearchSseFrame | null {
  if (!isRecord(raw)) return null;
  switch (eventType) {
    case "progress.snapshot": {
      const snapshot = sanitizeRunProgress(raw);
      return snapshot ? { event: "progress.snapshot", id, data: snapshot } : null;
    }
    case "step.current": {
      const operation_id = str(raw["operation_id"], 100);
      const statusRaw = str(raw["operation_status"], 100);
      const usage = sanitizeRunUsage(raw["usage"]);
      if (!operation_id || !statusRaw || !OPERATION_STATUSES.has(statusRaw as OperationStatus) || !usage) return null;
      return {
        event: "step.current",
        id,
        data: {
          operation_id,
          operation_status: statusRaw as OperationStatus,
          current: sanitizeCurrentActivity(raw["current"]),
          usage,
          interim: sanitizeInterimSummary(raw["interim"]),
          updated_at: str(raw["updated_at"], 100),
        },
      };
    }
    case "step.event": {
      const operation_id = str(raw["operation_id"], 100);
      const event = sanitizeProgressEvent(raw["event"]);
      const event_count = nonNegInt(raw["event_count"]);
      const usage = sanitizeRunUsage(raw["usage"]);
      if (!operation_id || !event || event_count === null || !usage) return null;
      return {
        event: "step.event",
        id,
        data: {
          operation_id,
          event,
          event_count,
          usage,
          interim: sanitizeInterimSummary(raw["interim"]),
          updated_at: str(raw["updated_at"], 100),
        },
      };
    }
    case "stream.delta": {
      const operation_id = str(raw["operation_id"], 100);
      const call_seq = nonNegInt(raw["call_seq"]);
      const channel = str(raw["channel"], 40);
      const delta = str(raw["delta"], 32_768);
      const seq = nonNegInt(raw["seq"]);
      if (!operation_id || call_seq === null || !channel || !STREAM_CHANNELS.has(channel as StreamChannel) || delta === null || seq === null) {
        return null;
      }
      return {
        event: "stream.delta",
        id,
        data: { operation_id, call_seq, channel: channel as StreamChannel, delta, seq },
      };
    }
    case "stream.reset": {
      const operation_id = str(raw["operation_id"], 100);
      const call_seq = nonNegInt(raw["call_seq"]);
      const reason = str(raw["reason"], 100);
      if (!operation_id || call_seq === null || reason === null) return null;
      return {
        event: "stream.reset",
        id,
        data: { operation_id, call_seq, reason },
      };
    }
    case "run.terminal": {
      const operation_id = str(raw["operation_id"], 100);
      const statusRaw = str(raw["operation_status"], 100);
      if (!operation_id || !statusRaw || !TERMINAL_STATUSES.has(statusRaw as TerminalOperationStatus)) return null;
      return {
        event: "run.terminal",
        id,
        data: {
          operation_id,
          operation_status: statusRaw as TerminalOperationStatus,
          version_id: str(raw["version_id"], 100),
        },
      };
    }
    default:
      return null;
  }
}

export function parseResearchSseFrame(rawFrame: string): ResearchSseFrame | null {
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
  return validateSsePayload(eventType, id, parsed);
}

export async function* parseResearchSseStream(
  body: ReadableStream<Uint8Array>,
  signal?: AbortSignal,
): AsyncGenerator<ResearchSseFrame> {
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
        const frame = parseResearchSseFrame(rawFrame);
        if (frame) yield frame;
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) {
      for (const rawFrame of buffer.split(/\r?\n\r?\n/u)) {
        const frame = parseResearchSseFrame(rawFrame);
        if (frame) yield frame;
      }
    }
  } finally {
    signal?.removeEventListener("abort", onAbort);
    await reader.cancel().catch(() => undefined);
  }
}

export async function openResearchRunStream(params: {
  topicId: string;
  operationId: string;
  after: number;
  lastEventId?: string | null;
  signal?: AbortSignal;
}): Promise<AsyncGenerator<ResearchSseFrame>> {
  const { topicId, operationId, after, lastEventId, signal } = params;
  const path = `/v2/research/topics/${enc(topicId)}/runs/${enc(operationId)}/events?after=${enc(String(after))}`;
  const options: Parameters<typeof apiRequest<Response>>[1] = {
    accept: "text/event-stream",
    raw: true,
  };
  if (lastEventId) options.headers = { "Last-Event-ID": lastEventId };
  if (signal) options.signal = signal;
  const response = await apiRequest<Response>(path, options);
  const contentType = response.headers.get("content-type")?.toLowerCase() ?? "";
  if (!contentType.includes("text/event-stream") || !response.body) {
    await response.body?.cancel().catch(() => undefined);
    throw new StreamFallbackError("non_sse_response");
  }
  return parseResearchSseStream(response.body, signal);
}
