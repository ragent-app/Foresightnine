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

describe("research source timestamps", () => {
  it("loads source text through its research-version scope", async () => {
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response('{"source_id":"s","text":"data"}', { status: 200 }));
    vi.stubGlobal("fetch", fetcher);
    const result = await researchClient("https://api.example.com").sourceText("topic", "version", "s");
    expect(result.text).toBe("data");
    expect(String(fetcher.mock.calls[0]?.[0])).toBe("https://api.example.com/v2/research/topics/topic/versions/version/sources/s/text");
    expect(fetcher.mock.calls[0]?.[1]).toMatchObject({ method: "GET" });
  });

  it("sends distinct event, publication and revision times for text, official pages and PDFs", async () => {
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response('{"source_id":"s"}', { status: 201 }));
    vi.stubGlobal("fetch", fetcher);
    const api = researchClient("https://api.example.com");
    const published = "2024-02-01T10:00", event = "2023-12-31T12:00", revised = "2024-03-01T09:00";
    await api.createSource("A", "Text", "", true, false, published, event, revised, true);
    await api.fetchSource("B", "https://www.bea.gov/example", true, false, published, event, revised, true);
    await api.uploadPdf("C", "JVBERg==", true, false, published, event, revised, true);
    for (const [, init] of fetcher.mock.calls) {
      const body = JSON.parse(String(init?.body));
      expect(body).toMatchObject({ event_at: new Date(event).toISOString(),
        published_at: new Date(published).toISOString(), revised_at: new Date(revised).toISOString(), is_simulated: true });
    }
    await api.createSource("D", "Text", "", true, false, "");
    const empty = JSON.parse(String(fetcher.mock.calls[3]?.[1]?.body));
    expect(empty).not.toHaveProperty("event_at");
    expect(empty).not.toHaveProperty("published_at");
    expect(empty).not.toHaveProperty("revised_at");
    expect(empty.is_simulated).toBe(false);
  });
});
