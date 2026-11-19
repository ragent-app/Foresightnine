import { StrictMode } from "react";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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

// Like a real fetch, rejects with AbortError when the request was aborted (StrictMode
// aborts the first effect's request), so only live requests reach the handler.
function abortAwareFetch(handler: (url: string) => Response) {
  return vi.fn(async (url: string, init?: RequestInit) => {
    await Promise.resolve();
    if (init?.signal?.aborted) throw new DOMException("aborted", "AbortError");
    return handler(url);
  });
}

beforeEach(() => {
  storeSession();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useRunProgress", () => {
  it("polls with after=event_count, merges events and stops when completed", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const urls: string[] = [];
    const responses = [progress("running", 2, [0, 1]), progress("completed", 3, [2])];
    const fetchMock = abortAwareFetch((url) => {
      urls.push(url);
      return jsonResponse(responses[Math.min(urls.length - 1, responses.length - 1)]);
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = makeClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");

    const { result } = renderHook(() => useRunProgress("t1", "op1"), { wrapper: wrapperFor(client) });

    await waitFor(() => expect(result.current.events).toHaveLength(2));
    expect(result.current.active).toBe(true);
    expect(urls[urls.length - 1]).toContain("progress?after=0");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1600);
    });
    await waitFor(() => expect(result.current.active).toBe(false));

    expect(result.current.events.map((event) => event.index)).toEqual([0, 1, 2]);
    expect(result.current.progress?.operation_status).toBe("completed");
    expect(urls.some((url) => url.includes("progress?after=2"))).toBe(true);
    const keys = invalidate.mock.calls.map((call) => JSON.stringify(call[0]?.queryKey));
    expect(keys).toContain(JSON.stringify(queryKeys.topic("t1")));
    expect(keys).toContain(JSON.stringify(queryKeys.runs("t1")));
    expect(keys).toContain(JSON.stringify(queryKeys.topics));
    expect(keys).toContain(JSON.stringify(["recovery", "t1"]));

    const calls = fetchMock.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(fetchMock.mock.calls.length).toBe(calls);
  });

  it("stops polling on 404 and keeps the error", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = vi.fn(async () => jsonResponse({ error_code: "not_found", message: "x" }, { status: 404 }));
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useRunProgress("t1", "op1"), { wrapper: wrapperFor(makeClient()) });

    await waitFor(() => expect(result.current.active).toBe(false));
    expect(result.current.error).toMatchObject({ status: 404 });
    const calls = fetchMock.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(40000);
    });
    expect(fetchMock.mock.calls.length).toBe(calls);
  });

  it("backs off and retries after a server error", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let count = 0;
    const fetchMock = abortAwareFetch(() => {
      count += 1;
      if (count === 1) return jsonResponse({ error_code: "boom", message: "x" }, { status: 500 });
      return jsonResponse(progress("completed", 0, []));
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useRunProgress("t1", "op1"), { wrapper: wrapperFor(makeClient()) });

    await waitFor(() => expect(result.current.error).toMatchObject({ status: 500 }));
    expect(result.current.active).toBe(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3100);
    });
    await waitFor(() => expect(result.current.active).toBe(false));
    expect(result.current.error).toBeNull();
  });

  it("does not poll without an operation id", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useRunProgress("t1", null), { wrapper: wrapperFor(makeClient()) });
    expect(result.current.active).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
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
