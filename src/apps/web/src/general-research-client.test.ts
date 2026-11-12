import { afterEach, describe, expect, it, vi } from "vitest";
import { researchClient, researchPollingDelay, ResearchRequestError } from "./general-research-client";

afterEach(() => vi.unstubAllGlobals());

describe("research operation polling", () => {
  it("honors a server retry delay and retains the status for read-only retries", async () => {
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response("", { status: 429, headers: { "Retry-After": "45" } }));
    vi.stubGlobal("fetch", fetcher);
    const request = researchClient("https://api.example.com").operation("topic", "operation");
    const error = await request.catch((value: unknown) => value);
    expect(error).toBeInstanceOf(ResearchRequestError);
    expect(error).toMatchObject({ status: 429, retryAfterMs: 45000 });
    expect(researchPollingDelay(error, 1500)).toBe(45000);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0]?.[1]).toMatchObject({ method: "GET" });
  });

  it("backs off transient errors and caps repeated network backoff", () => {
    expect(researchPollingDelay(new TypeError("network"), 1500)).toBe(3000);
    expect(researchPollingDelay(new TypeError("network"), 30000)).toBe(30000);
    expect(researchPollingDelay(new ResearchRequestError(503, 90000), 1500)).toBe(90000);
  });
});
