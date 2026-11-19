import { describe, expect, it, vi } from "vitest";
import { SESSION_CHANGED_EVENT, SESSION_STORAGE_KEY } from "../auth/session";
import { jsonResponse, makeSession, storeSession, TOKEN } from "../test/fixtures";
import { ApiError, apiRequest, parseRetryAfter } from "./http";

function stubFetch(response: () => Response) {
  const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => response());
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function headersOf(fetchMock: ReturnType<typeof stubFetch>): Record<string, string> {
  return (fetchMock.mock.calls[0]?.[1]?.headers ?? {}) as Record<string, string>;
}

describe("apiRequest", () => {
  it("attaches the bearer token from the stored session", async () => {
    storeSession(makeSession());
    const fetchMock = stubFetch(() => jsonResponse({ ok: true }));
    await expect(apiRequest("/v1/account")).resolves.toEqual({ ok: true });
    expect(headersOf(fetchMock).Authorization).toBe(`Bearer ${TOKEN}`);
    expect(headersOf(fetchMock).Accept).toBe("application/json");
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({ credentials: "omit", redirect: "error", cache: "no-store" });
  });

  it("omits the bearer token without a session", async () => {
    const fetchMock = stubFetch(() => jsonResponse({}));
    await apiRequest("/v1/account");
    expect(headersOf(fetchMock).Authorization).toBeUndefined();
    expect(headersOf(fetchMock)["Content-Type"]).toBeUndefined();
  });

  it("rejects cross-origin paths", async () => {
    const fetchMock = stubFetch(() => jsonResponse({}));
    await expect(apiRequest("//evil.example.com/x")).rejects.toBeInstanceOf(TypeError);
    await expect(apiRequest("v1/relative")).rejects.toBeInstanceOf(TypeError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("parses the backend error body into ApiError", async () => {
    stubFetch(() => jsonResponse({
      schema_version: "v1",
      error_code: "validation_failed",
      message: "bad input",
      request_id: "req_1",
      retryable: false,
      details: [{ location: "body.x", message: "required" }],
    }, { status: 422 }));
    const error = await apiRequest("/v1/x").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 422,
      code: "validation_failed",
      message: "bad input",
      requestId: "req_1",
      retryable: false,
      retryAfterMs: null,
      details: [{ location: "body.x", message: "required" }],
    });
  });

  it("falls back to http_<status> for unparseable bodies", async () => {
    stubFetch(() => new Response("oops", { status: 503 }));
    const error = await apiRequest("/v1/x").catch((e: unknown) => e);
    expect(error).toMatchObject({ code: "http_503", retryable: true, requestId: null, details: [] });
    stubFetch(() => new Response("oops", { status: 500 }));
    await expect(apiRequest("/v1/x")).rejects.toMatchObject({ code: "http_500", retryable: false });
  });

  it("clears the session and dispatches expired on 401 with a session", async () => {
    storeSession(makeSession());
    const events: string[] = [];
    const listener = (event: Event) => events.push((event as CustomEvent<{ reason: string }>).detail.reason);
    window.addEventListener(SESSION_CHANGED_EVENT, listener);
    stubFetch(() => jsonResponse({ error_code: "unauthorized", message: "no" }, { status: 401 }));
    await expect(apiRequest("/v1/account")).rejects.toMatchObject({ status: 401 });
    window.removeEventListener(SESSION_CHANGED_EVENT, listener);
    expect(events).toEqual(["expired"]);
    expect(sessionStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
  });

  it("does not dispatch on 401 without a session", async () => {
    const listener = vi.fn();
    window.addEventListener(SESSION_CHANGED_EVENT, listener);
    stubFetch(() => new Response("", { status: 401 }));
    await expect(apiRequest("/v1/account")).rejects.toMatchObject({ status: 401 });
    window.removeEventListener(SESSION_CHANGED_EVENT, listener);
    expect(listener).not.toHaveBeenCalled();
  });

  it("parses Retry-After seconds and HTTP dates", async () => {
    expect(parseRetryAfter("7")).toBe(7000);
    expect(parseRetryAfter(null)).toBeNull();
    expect(parseRetryAfter("garbage")).toBeNull();
    expect(parseRetryAfter("99999999999")).toBe(2147483647);
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
    expect(parseRetryAfter("Thu, 01 Jan 2026 00:00:10 GMT")).toBe(10000);
    expect(parseRetryAfter("Wed, 31 Dec 2025 00:00:00 GMT")).toBe(0);
    vi.useRealTimers();

    stubFetch(() => new Response("", { status: 429, headers: { "Retry-After": "3" } }));
    await expect(apiRequest("/v1/x")).rejects.toMatchObject({ status: 429, retryAfterMs: 3000, retryable: true });
  });

  it("sends the Idempotency-Key and JSON content type", async () => {
    const fetchMock = stubFetch(() => jsonResponse({}));
    await apiRequest("/v1/x", { method: "POST", body: { a: 1 }, idempotencyKey: "key-1" });
    expect(headersOf(fetchMock)["Idempotency-Key"]).toBe("key-1");
    expect(headersOf(fetchMock)["Content-Type"]).toBe("application/json");
    expect(fetchMock.mock.calls[0]?.[1]?.body).toBe('{"a":1}');
  });

  it("sends an empty JSON object for a POST without a body, because the API accepts only JSON writes", async () => {
    const fetchMock = stubFetch(() => jsonResponse({}));
    await apiRequest("/v1/x", { method: "POST", idempotencyKey: "key-2" });
    expect(headersOf(fetchMock)["Content-Type"]).toBe("application/json");
    expect(fetchMock.mock.calls[0]?.[1]?.body).toBe("{}");
  });

  it("sends no body or content type for GET and DELETE", async () => {
    const fetchMock = stubFetch(() => jsonResponse({}));
    await apiRequest("/v1/x");
    await apiRequest("/v1/x", { method: "DELETE" });
    for (const call of fetchMock.mock.calls) {
      expect(call[1]?.body).toBeUndefined();
      expect((call[1]?.headers as Record<string, string>)["Content-Type"]).toBeUndefined();
    }
  });

  it("returns undefined for 204", async () => {
    stubFetch(() => new Response(null, { status: 204 }));
    await expect(apiRequest("/v1/x", { method: "DELETE" })).resolves.toBeUndefined();
  });
});
