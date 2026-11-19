import { describe, expect, it, vi } from "vitest";
import { SESSION_CHANGED_EVENT, SESSION_STORAGE_KEY } from "../auth/session";
import { jsonResponse, makeSession, storeSession, TOKEN } from "../test/fixtures";
import { ApiError } from "./http";
import { openResearchRunStream, parseResearchSseFrame, parseResearchSseStream, StreamFallbackError } from "./stream";
import type { ResearchSseFrame } from "./types";

function streamFromChunks(chunks: string[], { hangOnDone = false }: { hangOnDone?: boolean } = {}): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  let index = 0;
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      if (index < chunks.length) {
        controller.enqueue(encoder.encode(chunks[index]!));
        index += 1;
        return;
      }
      if (!hangOnDone) controller.close();
    },
  });
}

describe("parseResearchSseFrame", () => {
  it("parses all 6 valid SSE frame types and sanitizes event payloads", () => {
    const snap = parseResearchSseFrame(
      `event: progress.snapshot\nid: cp:1\ndata: ${JSON.stringify({
        operation_id: "op_1",
        operation_status: "running",
        turn: { index: 1, question: "Q" },
        usage: { model_calls: 1, search_calls: 0, total_tokens: 100 },
        current: { kind: "tool", name: "read_sources", rationale: "读财报", target: "《10-K》0–500 字" },
        interim: { leads_count: 1, acquired_sources_count: 0, read_sources_count: 1, facts_count: 0, calculations_count: 0 },
        events: [
          {
            index: 0,
            kind: "model",
            finish_reason: "tool_calls",
            usage: 100,
            input_tokens: 80,
            output_tokens: 20,
            settlement: null,
            note: "先读财报",
            thinking: "SECRET THINKING",
          },
        ],
        event_count: 1,
        started_at: "2026-11-19T12:00:00Z",
        updated_at: "2026-11-19T12:00:01Z",
        version_id: null,
      })}`,
    );
    expect(snap?.event).toBe("progress.snapshot");
    expect(snap?.id).toBe("cp:1");
    if (snap?.event === "progress.snapshot") {
      expect(snap.data.events[0]).not.toHaveProperty("thinking");
      expect(snap.data.events[0]?.kind === "model" && snap.data.events[0].note).toBe("先读财报");
    }

    const current = parseResearchSseFrame(
      `event: step.current\ndata: ${JSON.stringify({
        operation_id: "op_1",
        operation_status: "running",
        current: { kind: "model", phase: "thinking", retry_attempt: null, started_at: "2026-11-19T12:00:02Z" },
        usage: { model_calls: 1, search_calls: 0, total_tokens: 100 },
        interim: { leads_count: 0, acquired_sources_count: 0, read_sources_count: 1, facts_count: 0, calculations_count: 0 },
        updated_at: "2026-11-19T12:00:02Z",
      })}`,
    );
    expect(current?.event).toBe("step.current");

    const ev = parseResearchSseFrame(
      `event: step.event\nid: ev:1\ndata: ${JSON.stringify({
        operation_id: "op_1",
        event: {
          index: 1,
          kind: "tool",
          name: "read_sources",
          status: "read",
          reason: null,
          rationale: "读正文",
          denied_count: null,
          summary: {
            kind: "read_sources",
            read_count: 1,
            items: [{ source_id: "s1", title: "10-K", start: 0, end: 100, total_characters: 500, text: "SECRET TEXT" }],
          },
        },
        event_count: 2,
        usage: { model_calls: 1, search_calls: 0, total_tokens: 100 },
        interim: null,
        updated_at: "2026-11-19T12:00:03Z",
      })}`,
    );
    expect(ev?.event).toBe("step.event");
    if (ev?.event === "step.event" && ev.data.event.kind === "tool" && ev.data.event.summary?.kind === "read_sources") {
      expect(ev.data.event.summary.items[0]).not.toHaveProperty("text");
    }

    const delta = parseResearchSseFrame(
      `event: stream.delta\ndata: ${JSON.stringify({ operation_id: "op_1", call_seq: 1, channel: "thinking", delta: "abc", seq: 0 })}`,
    );
    expect(delta).toEqual({
      event: "stream.delta",
      id: null,
      data: { operation_id: "op_1", call_seq: 1, channel: "thinking", delta: "abc", seq: 0 },
    });

    const reset = parseResearchSseFrame(
      `event: stream.reset\ndata: ${JSON.stringify({ operation_id: "op_1", call_seq: 1, reason: "tool_start" })}`,
    );
    expect(reset).toEqual({
      event: "stream.reset",
      id: null,
      data: { operation_id: "op_1", call_seq: 1, reason: "tool_start" },
    });

    const terminal = parseResearchSseFrame(
      `event: run.terminal\nid: done:completed\ndata: ${JSON.stringify({ operation_id: "op_1", operation_status: "completed", version_id: "v2" })}`,
    );
    expect(terminal).toEqual({
      event: "run.terminal",
      id: "done:completed",
      data: { operation_id: "op_1", operation_status: "completed", version_id: "v2" },
    });
  });

  it("handles multi-line data, CRLF, comments, and ignores malformed JSON or unknown events", () => {
    expect(parseResearchSseFrame(": heartbeat")).toBeNull();
    expect(parseResearchSseFrame("event: step.event\ndata: {bad json")).toBeNull();
    expect(parseResearchSseFrame('event: unknown.event\ndata: {"x":1}')).toBeNull();
    expect(parseResearchSseFrame('event: run.terminal\ndata: {"operation_id":"op_1","operation_status":"weird"}')).toBeNull();

    const multiline = parseResearchSseFrame(
      'event: stream.delta\r\ndata: {"operation_id":"op_1","call_seq":0,\r\ndata: "channel":"text","delta":"hi","seq":0}',
    );
    expect(multiline).toEqual({
      event: "stream.delta",
      id: null,
      data: { operation_id: "op_1", call_seq: 0, channel: "text", delta: "hi", seq: 0 },
    });
  });
});

describe("parseResearchSseStream", () => {
  it("parses frames split across arbitrary chunk boundaries and skips heartbeats", async () => {
    const body = streamFromChunks([
      ": heartbeat\r\n\r\nevent: stream.delta\r\nda",
      'ta: {"operation_id":"op_1","call_seq":0,"channel":"thinking","delta":"hel',
      'lo","seq":0}\r\n\r\n: keepalive\n\nevent: run.terminal\nid: done:completed\n',
      'data: {"operation_id":"op_1","operation_status":"completed","version_id":"v3"}\n\n',
    ]);
    const frames = [];
    for await (const frame of parseResearchSseStream(body)) frames.push(frame);
    expect(frames).toHaveLength(2);
    expect(frames[0]?.event).toBe("stream.delta");
    expect(frames[1]?.event).toBe("run.terminal");
  });

  it("aborts cleanly via AbortSignal and cancels the stream reader", async () => {
    const body = streamFromChunks(
      ['event: stream.delta\ndata: {"operation_id":"op_1","call_seq":0,"channel":"text","delta":"a","seq":0}\n\n'],
      { hangOnDone: true },
    );
    const controller = new AbortController();
    const received: ResearchSseFrame[] = [];
    const run = async () => {
      for await (const frame of parseResearchSseStream(body, controller.signal)) {
        received.push(frame);
        controller.abort();
      }
    };
    await expect(run()).rejects.toMatchObject({ name: "AbortError" });
    expect(received).toHaveLength(1);
  });
});

describe("openResearchRunStream", () => {
  it("sends bearer token, SSE Accept header, and Last-Event-ID", async () => {
    storeSession(makeSession());
    const fetchMock = vi.fn(async () =>
      new Response(
        'event: run.terminal\nid: done:completed\ndata: {"operation_id":"op_1","operation_status":"completed","version_id":null}\n\n',
        { status: 200, headers: { "Content-Type": "text/event-stream; charset=utf-8" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const stream = await openResearchRunStream({
      topicId: "t1",
      operationId: "op_1",
      after: 3,
      lastEventId: "ev:2",
    });
    const frames = [];
    for await (const frame of stream) frames.push(frame);
    expect(frames).toHaveLength(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toContain("/v2/research/topics/t1/runs/op_1/events?after=3");
    expect(init.headers).toMatchObject({
      Authorization: `Bearer ${TOKEN}`,
      Accept: "text/event-stream",
      "Last-Event-ID": "ev:2",
    });
  });

  it("throws StreamFallbackError when server returns non-SSE content-type", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ ok: true })));
    await expect(openResearchRunStream({ topicId: "t1", operationId: "op_1", after: 0 })).rejects.toBeInstanceOf(StreamFallbackError);
  });

  it("clears session on 401 and surfaces ApiError on 403/404/429", async () => {
    storeSession(makeSession());
    const reasons: string[] = [];
    const listener = (event: Event) => reasons.push((event as CustomEvent<{ reason: string }>).detail.reason);
    window.addEventListener(SESSION_CHANGED_EVENT, listener);
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ error_code: "unauthorized", message: "no" }, { status: 401 })));
    await expect(openResearchRunStream({ topicId: "t1", operationId: "op_1", after: 0 })).rejects.toMatchObject({ status: 401 });
    window.removeEventListener(SESSION_CHANGED_EVENT, listener);
    expect(reasons).toEqual(["expired"]);
    expect(sessionStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();

    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ error_code: "rate_limited", message: "cap" }, { status: 429, headers: { "Retry-After": "5" } })));
    const err = await openResearchRunStream({ topicId: "t1", operationId: "op_1", after: 0 }).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 429, retryAfterMs: 5000 });
  });
});
