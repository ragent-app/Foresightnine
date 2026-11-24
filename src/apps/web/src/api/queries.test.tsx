import { StrictMode } from "react";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SESSION_STORAGE_KEY } from "../auth/session";
import { jsonResponse, storeSession } from "../test/fixtures";
import { queryKeys, useActiveRun, useRunProgress } from "./queries";
import type { OperationStatus, RunProgress } from "./types";

function makeClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
}

function wrapperFor(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <StrictMode>
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      </StrictMode>
    );
  };
}

function progress(status: OperationStatus, eventCount: number, indexes: number[]): RunProgress {
  return {
    operation_id: "op1",
    operation_status: status,
    turn: { index: 0, question: "q" },
    usage: { model_calls: 1, search_calls: 0, total_tokens: 10 },
    current: null,
    events: indexes.map((index) => ({ index, kind: "submission" as const, status: "ok" })),
    event_count: eventCount,
    started_at: null,
    updated_at: null,
    version_id: null,
  };
}

function sseFrame(event: string, data: unknown, id?: string): string {
  return `${id ? `event: ${event}\nid: ${id}\n` : `event: ${event}\n`}data: ${JSON.stringify(data)}\n\n`;
}

function sseResponse(chunks: string[], { keepOpen = false }: { keepOpen?: boolean } = {}): Response {
  const encoder = new TextEncoder();
  let idx = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (idx < chunks.length) {
        controller.enqueue(encoder.encode(chunks[idx]!));
        idx += 1;
        return;
      }
      if (!keepOpen) controller.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": "text/event-stream; charset=utf-8" },
  });
}

// Like a real fetch, rejects with AbortError when the request was aborted (StrictMode
// aborts the first effect's request), so only live requests reach the handler.
function abortAwareFetch(handler: (url: string, init?: RequestInit) => Response | Promise<Response>) {
  return vi.fn(async (url: string, init?: RequestInit) => {
    await Promise.resolve();
    if (init?.signal?.aborted) throw new DOMException("aborted", "AbortError");
    return handler(url, init);
  });
}

beforeEach(() => {
  storeSession();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useRunProgress (SSE primary + polling fallback)", () => {
  it("consumes SSE snapshot, step.current, step.event (deduped), isolated deltas, and run.terminal", async () => {
    const snap = progress("running", 1, [0]);
    const step0 = { index: 0, kind: "submission" as const, status: "ok" };
    const step1 = { index: 1, kind: "tool" as const, name: "read_sources", status: "read", reason: null, rationale: "读报告", denied_count: null };
    const controller: { push?: (text: string) => void; close?: () => void } = {};
    const encoder = new TextEncoder();

    const fetchMock = abortAwareFetch(() => {
      const body = new ReadableStream<Uint8Array>({
        start(ctrl) {
          controller.push = (text: string) => ctrl.enqueue(encoder.encode(text));
          controller.close = () => ctrl.close();
          ctrl.enqueue(encoder.encode(sseFrame("progress.snapshot", snap, "cp:1")));
        },
      });
      return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = makeClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");

    const { result } = renderHook(() => useRunProgress("t1", "op1"), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.events).toHaveLength(1));
    expect(result.current.active).toBe(true);

    const streamUpdates = vi.fn();
    const unsubscribe = result.current.subscribeStream(streamUpdates);

    act(() => {
      controller.push?.(": heartbeat\n\n");
      controller.push?.(
        sseFrame("step.current", {
          operation_id: "op1",
          operation_status: "running",
          current: { kind: "tool", name: "read_sources", rationale: "读正文", target: "《年报》" },
          usage: { model_calls: 1, search_calls: 0, total_tokens: 10 },
          interim: { leads_count: 0, acquired_sources_count: 0, read_sources_count: 1, facts_count: 0, calculations_count: 0 },
          updated_at: "2026-11-19T12:00:01Z",
        }),
      );
      // Duplicate event index 0 plus new event index 1
      controller.push?.(
        sseFrame("step.event", {
          operation_id: "op1",
          event: step0,
          event_count: 1,
          usage: { model_calls: 1, search_calls: 0, total_tokens: 10 },
          interim: null,
          updated_at: "2026-11-19T12:00:01Z",
        }, "ev:0"),
      );
      controller.push?.(
        sseFrame("step.event", {
          operation_id: "op1",
          event: step1,
          event_count: 2,
          usage: { model_calls: 1, search_calls: 0, total_tokens: 10 },
          interim: null,
          updated_at: "2026-11-19T12:00:02Z",
        }, "ev:1"),
      );
      controller.push?.(sseFrame("stream.delta", { operation_id: "op1", call_seq: 1, channel: "thinking", delta: "思考中", seq: 0 }));
    });

    await waitFor(() => expect(result.current.events).toHaveLength(2));
    expect(result.current.progress?.current).toEqual({
      kind: "tool",
      name: "read_sources",
      rationale: "读正文",
      target: "《年报》",
      started_at: null,
    });
    expect(result.current.getStreamSnapshot()).toMatchObject({ callSeq: 1, thinking: "思考中", lastSeq: 0 });
    await waitFor(() => expect(streamUpdates).toHaveBeenCalledTimes(1));

    act(() => {
      controller.push?.(sseFrame("stream.reset", { operation_id: "op1", call_seq: 1, reason: "tool_start" }));
      controller.push?.(sseFrame("run.terminal", { operation_id: "op1", operation_status: "completed", version_id: "v5" }, "done:completed"));
      controller.close?.();
    });

    await waitFor(() => expect(result.current.active).toBe(false));
    expect(result.current.progress?.operation_status).toBe("completed");
    expect(result.current.progress?.version_id).toBe("v5");
    expect(result.current.getStreamSnapshot().thinking).toBe("");
    unsubscribe();

    const keys = invalidate.mock.calls.map((call) => JSON.stringify(call[0]?.queryKey));
    expect(keys).toContain(JSON.stringify(queryKeys.topic("t1")));
    expect(keys).toContain(JSON.stringify(queryKeys.runs("t1")));
    expect(keys).toContain(JSON.stringify(queryKeys.topics));
    expect(keys).toContain(JSON.stringify(["recovery", "t1"]));
  });

  it("reconnects with after and Last-Event-ID using exponential backoff and resumes without duplicates", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const seenRequests: { url: string; lastId?: string | undefined }[] = [];
    let attempt = 0;
    const fetchMock = abortAwareFetch((url, init) => {
      attempt += 1;
      const headers = (init?.headers ?? {}) as Record<string, string>;
      seenRequests.push({ url, lastId: headers["Last-Event-ID"] });
      if (attempt === 1) {
        // Drops after snapshot + event 0 before terminal
        return sseResponse([
          sseFrame("progress.snapshot", progress("running", 1, [0]), "cp:1"),
        ]);
      }
      return sseResponse([
        sseFrame("progress.snapshot", { ...progress("running", 1, []), events: [] }, "cp:1"),
        sseFrame("step.event", {
          operation_id: "op1",
          event: { index: 1, kind: "submission", status: "ok" },
          event_count: 2,
          usage: { model_calls: 1, search_calls: 0, total_tokens: 10 },
          interim: null,
          updated_at: null,
        }, "ev:1"),
        sseFrame("run.terminal", { operation_id: "op1", operation_status: "completed", version_id: "v2" }, "done:completed"),
      ]);
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useRunProgress("t1", "op1"), { wrapper: wrapperFor(makeClient()) });
    await waitFor(() => expect(result.current.events).toHaveLength(1));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1100);
    });
    await waitFor(() => expect(result.current.active).toBe(false));
    expect(result.current.events.map((e) => e.index)).toEqual([0, 1]);
    expect(seenRequests).toHaveLength(2);
    expect(seenRequests[0]!.url).toContain("/events?after=0");
    expect(seenRequests[1]!.url).toContain("/events?after=1");
    expect(seenRequests[1]!.lastId).toBe("cp:1");
  });

  it("falls back to /progress polling after 3 SSE connection failures", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const urls: string[] = [];
    const pollResponses = [progress("running", 2, [0, 1]), progress("completed", 3, [2])];
    let pollIdx = 0;

    const fetchMock = abortAwareFetch((url) => {
      urls.push(url);
      if (url.includes("/events")) {
        return jsonResponse({ error_code: "boom", message: "down" }, { status: 503 });
      }
      const next = pollResponses[Math.min(pollIdx, pollResponses.length - 1)]!;
      pollIdx += 1;
      return jsonResponse(next);
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useRunProgress("t1", "op1"), { wrapper: wrapperFor(makeClient()) });
    // Exhaust 3 SSE backoff attempts: 1000ms + 2000ms + 4000ms
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1100);
      await vi.advanceTimersByTimeAsync(2100);
      await vi.advanceTimersByTimeAsync(4100);
    });

    await waitFor(() => expect(result.current.events).toHaveLength(2));
    expect(result.current.active).toBe(true);
    expect(urls.filter((u) => u.includes("/events"))).toHaveLength(4);
    expect(urls.some((u) => u.includes("/progress?after=0"))).toBe(true);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1600);
    });
    await waitFor(() => expect(result.current.active).toBe(false));
    expect(result.current.events.map((e) => e.index)).toEqual([0, 1, 2]);
    expect(urls.some((u) => u.includes("/progress?after=2"))).toBe(true);
  });

  it("falls back immediately to polling on 429 or 405 from SSE endpoint", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    for (const status of [429, 405]) {
      const urls: string[] = [];
      const fetchMock = abortAwareFetch((url) => {
        urls.push(url);
        if (url.includes("/events")) return jsonResponse({ error_code: "cap", message: "cap" }, { status });
        return jsonResponse(progress("completed", 1, [0]));
      });
      vi.stubGlobal("fetch", fetchMock);
      const { result, unmount } = renderHook(() => useRunProgress("t1", "op1"), { wrapper: wrapperFor(makeClient()) });
      await waitFor(() => expect(result.current.active).toBe(false));
      expect(result.current.events.map((e) => e.index)).toEqual([0]);
      expect(urls.filter((u) => u.includes("/events"))).toHaveLength(1);
      expect(urls.some((u) => u.includes("/progress?after=0"))).toBe(true);
      unmount();
    }
  });

  it("stops permanently on 401, 403, and 404 without retrying or falling back", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    for (const status of [401, 403, 404]) {
      storeSession();
      const fetchMock = vi.fn(async () => jsonResponse({ error_code: "err", message: "x" }, { status }));
      vi.stubGlobal("fetch", fetchMock);
      const { result, unmount } = renderHook(() => useRunProgress("t1", "op1"), { wrapper: wrapperFor(makeClient()) });
      await waitFor(() => expect(result.current.active).toBe(false));
      expect(result.current.error).toMatchObject({ status });
      if (status === 401) expect(sessionStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
      const calls = fetchMock.mock.calls.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10000);
      });
      expect(fetchMock.mock.calls.length).toBe(calls);
      unmount();
    }
  });

  it("aborts open SSE connection cleanly on unmount", async () => {
    let aborted = false;
    const fetchMock = abortAwareFetch((_url, init) => {
      init?.signal?.addEventListener("abort", () => {
        aborted = true;
      });
      return sseResponse([sseFrame("progress.snapshot", progress("running", 0, []), "cp:0")], { keepOpen: true });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result, unmount } = renderHook(() => useRunProgress("t1", "op1"), { wrapper: wrapperFor(makeClient()) });
    await waitFor(() => expect(result.current.progress?.operation_status).toBe("running"));
    unmount();
    expect(aborted).toBe(true);
  });

  it("does not connect without an operation id", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useRunProgress("t1", null), { wrapper: wrapperFor(makeClient()) });
    expect(result.current.active).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("batches rapid stream.delta bursts, tracks activeChannel, handles submission_rejected notices, and enforces caps", async () => {
    const controller: { push?: (chunk: string) => void; close?: () => void } = {};
    const encoder = new TextEncoder();
    const fetchMock = abortAwareFetch(() => {
      const body = new ReadableStream<Uint8Array>({
        start(ctrl) {
          controller.push = (text: string) => ctrl.enqueue(encoder.encode(text));
          controller.close = () => ctrl.close();
          ctrl.enqueue(encoder.encode(sseFrame("progress.snapshot", progress("running", 0, []), "cp:0")));
        },
      });
      return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result, unmount } = renderHook(() => useRunProgress("t1", "op1"), { wrapper: wrapperFor(makeClient()) });
    await waitFor(() => expect(result.current.progress?.operation_status).toBe("running"));

    const notifications = vi.fn();
    const unsubscribe = result.current.subscribeStream(notifications);

    // 20 rapid synchronous deltas coalesce into a single rAF/batch notification.
    act(() => {
      for (let i = 0; i < 20; i += 1) {
        controller.push?.(
          sseFrame("stream.delta", {
            operation_id: "op1",
            call_seq: 1,
            channel: i < 10 ? "thinking" : "draft_report",
            delta: i < 10 ? `思${i}` : `稿${i}`,
            seq: i,
          }),
        );
      }
    });

    await waitFor(() => expect(result.current.getStreamSnapshot().lastSeq).toBe(19));
    expect(result.current.getStreamSnapshot()).toMatchObject({
      callSeq: 1,
      thinking: "思0思1思2思3思4思5思6思7思8思9",
      draftReport: "稿10稿11稿12稿13稿14稿15稿16稿17稿18稿19",
      activeChannel: "draft_report",
      retractedNotice: null,
      lastSeq: 19,
    });
    await waitFor(() => expect(notifications.mock.calls.length).toBeGreaterThanOrEqual(1));
    expect(notifications.mock.calls.length).toBeLessThanOrEqual(3);

    // submission_rejected moves the draft aside (still readable) and keeps the notice across tool_status until new prose arrives.
    const countBeforeReset = notifications.mock.calls.length;
    act(() => {
      controller.push?.(sseFrame("stream.reset", { operation_id: "op1", call_seq: 1, reason: "submission_rejected" }));
    });
    await waitFor(() => expect(notifications.mock.calls.length).toBe(countBeforeReset + 1));
    expect(result.current.getStreamSnapshot()).toMatchObject({
      draftReport: "",
      heldDraft: "稿10稿11稿12稿13稿14稿15稿16稿17稿18稿19",
      thinking: "",
      text: "",
      toolStatus: null,
      activeChannel: null,
      retractedNotice: "核验发现需要修改的地方，正在修改这份草稿…",
    });

    act(() => {
      controller.push?.(sseFrame("stream.delta", { operation_id: "op1", call_seq: 2, channel: "tool_status", delta: "正在修正引文…", seq: 0 }));
    });
    await waitFor(() => expect(result.current.getStreamSnapshot().toolStatus).toBe("正在修正引文…"));
    expect(result.current.getStreamSnapshot().retractedNotice).toBe("核验发现需要修改的地方，正在修改这份草稿…");
    expect(result.current.getStreamSnapshot().heldDraft).toBe("稿10稿11稿12稿13稿14稿15稿16稿17稿18稿19");

    // New thinking/draft_report clears retractedNotice; caps prevent unbounded memory growth.
    const huge = "A".repeat(25_000);
    act(() => {
      controller.push?.(sseFrame("stream.delta", { operation_id: "op1", call_seq: 2, channel: "thinking", delta: huge, seq: 1 }));
    });
    await waitFor(() => expect(result.current.getStreamSnapshot().thinking.length).toBe(24_000));
    const snap = result.current.getStreamSnapshot();
    expect(snap.retractedNotice).toBeNull();
    expect(snap.thinking.startsWith("…\n")).toBe(true);

    // submission_partial clears everything immediately.
    act(() => {
      controller.push?.(sseFrame("stream.reset", { operation_id: "op1", call_seq: 2, reason: "submission_partial" }));
      controller.close?.();
    });
    await waitFor(() => expect(result.current.getStreamSnapshot().thinking).toBe(""));
    expect(result.current.getStreamSnapshot().retractedNotice).toBeNull();
    expect(result.current.getStreamSnapshot().heldDraft).toBe("");
    unsubscribe();
    unmount();
  });
});

describe("useActiveRun", () => {
  it("picks the newest active run and the newest run overall", async () => {
    const op = (id: string, status: OperationStatus) => ({ operation: { operation_id: id, status } });
    const fetchMock = vi.fn(async () =>
      jsonResponse([op("newest", "completed"), op("active-new", "running"), op("active-old", "pending"), op("old", "failed")]),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useActiveRun("t1"), { wrapper: wrapperFor(makeClient()) });
    await waitFor(() => expect(result.current.activeRun).not.toBeNull());
    expect(result.current.activeRun?.operation_id).toBe("active-new");
    expect(result.current.latestRun?.operation_id).toBe("newest");
  });

  it("returns null when no run is active", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse([{ operation: { operation_id: "a", status: "completed" } }])));
    const { result } = renderHook(() => useActiveRun("t1"), { wrapper: wrapperFor(makeClient()) });
    await waitFor(() => expect(result.current.latestRun).not.toBeNull());
    expect(result.current.activeRun).toBeNull();
  });
});
