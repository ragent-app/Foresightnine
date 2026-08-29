import type { SseEvent } from "@foresightnine/contracts";
import { validateResponse } from "./validation.js";

export interface SseReplayEvent {
  readonly id: string;
  readonly data: SseEvent;
}

function abortError(): Error {
  if (typeof DOMException !== "undefined") return new DOMException("The operation was aborted.", "AbortError");
  const error = new Error("The operation was aborted.");
  error.name = "AbortError";
  return error;
}

function parseFrame(frame: string): SseReplayEvent | null {
  let id = "";
  const data: string[] = [];
  for (const line of frame.split(/\r?\n/u)) {
    if (line.startsWith("id:")) id = line.slice(3).trim();
    if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /u, ""));
  }
  if (!id || data.length === 0) return null;
  let value: SseEvent;
  try { value = validateResponse<SseEvent>(JSON.parse(data.join("\n")), "SseEvent"); }
  catch (error) {
    if (error instanceof SyntaxError) throw new Error("Invalid SSE event payload.");
    throw error;
  }
  if (id !== `${value.operation_id}:${value.sequence}`) throw new Error("Invalid SSE replay event id.");
  return { id, data: value };
}

export function parseSseText(text: string): readonly SseReplayEvent[] {
  return text.split(/\r?\n\r?\n/u).flatMap((frame) => {
    if (!frame.trim()) return [];
    const parsed = parseFrame(frame);
    return parsed ? [parsed] : [];
  });
}

export async function* parseSseBody(body: ReadableStream<Uint8Array>, signal?: AbortSignal): AsyncGenerator<SseReplayEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      if (signal?.aborted) throw abortError();
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      const frames = buffer.split(/\r?\n\r?\n/u);
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        const parsed = parseFrame(frame);
        if (parsed) yield parsed;
      }
    }
    buffer += decoder.decode();
    const parsed = parseFrame(buffer);
    if (parsed) yield parsed;
  } finally {
    await reader.cancel().catch(() => undefined);
  }
}
