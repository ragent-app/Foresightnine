import { afterEach, describe, expect, it, vi } from "vitest";
import { researchClient, researchPollingDelay, ResearchRequestError } from "./general-research-client";

afterEach(() => vi.unstubAllGlobals());

describe("research operation polling", () => {
  it("confirms a specific fact against the current version", async () => {
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response('{}', { status: 201 }));
    vi.stubGlobal("fetch", fetcher);
    await researchClient("https://api.example.com").confirmDirection("topic/one", "version-1", "fact-2");
    expect(String(fetcher.mock.calls[0]?.[0])).toContain("/topics/topic%2Fone/facts/confirm-direction");
    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).toEqual({parent_version_id:"version-1",fact_id:"fact-2"});
  });
  it("creates a calculation using saved fact identifiers", async () => {
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response('{}', { status: 201 }));
    vi.stubGlobal("fetch", fetcher);
    await researchClient("https://api.example.com").createCalculation("topic", "version-1", "difference", "first", "second");
    expect(String(fetcher.mock.calls[0]?.[0])).toContain("/topics/topic/calculations");
    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).toEqual({parent_version_id:"version-1",formula:"difference",first_fact_id:"first",second_fact_id:"second"});
  });
  it("sends an input limit only when explicitly selected", async () => {
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response('{}', { status: 202 }));
    vi.stubGlobal("fetch", fetcher);
    const api = researchClient("https://api.example.com");
    const budget = { search: false, supportReview: false, requireCounterarguments: false, maxElapsedSeconds: 60, maxTotalTokens: 10000 };
    await api.run("topic", "version", "key-one", budget);
    await api.run("topic", "version", "key-two", {...budget, maxInputTokensPerCall: 2500});
    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).not.toHaveProperty("max_input_tokens_per_call");
    expect(JSON.parse(String(fetcher.mock.calls[1]?.[1]?.body)).max_input_tokens_per_call).toBe(2500);
  });

  it("sends isolated review mode and its explicit total call cap", async () => {
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response('{}', { status: 202 }));
    vi.stubGlobal("fetch", fetcher);
    const api = researchClient("https://api.example.com");
    await api.run("topic", "version", "isolated", {search:false,supportReview:true,supportReviewMode:"isolated",
      maxModelCalls:4,requireCounterarguments:true,maxElapsedSeconds:60,maxTotalTokens:20000});
    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).toMatchObject({support_review:true,
      support_review_mode:"isolated",max_model_calls:4,max_total_tokens:20000});
    await api.run("topic", "version", "batch", {search:false,supportReview:true,
      requireCounterarguments:true,maxElapsedSeconds:60,maxTotalTokens:10000});
    const batch = JSON.parse(String(fetcher.mock.calls[1]?.[1]?.body));
    expect(batch.max_model_calls).toBe(2);
    expect(batch).not.toHaveProperty("support_review_mode");
  });
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

describe("research scope", () => {
  it("includes the optional scope in topic creation", async () => {
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response('{}', { status: 201 }));
    vi.stubGlobal("fetch", fetcher);
    const api = researchClient("https://api.example.com");
    await api.create("Revenue?", [], "", "Asia FY2024");
    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).toMatchObject({ question: "Revenue?", scope: "Asia FY2024" });
    await api.create("Revenue?", [], "");
    expect(JSON.parse(String(fetcher.mock.calls[1]?.[1]?.body))).not.toHaveProperty("scope");
  });
});

describe("scope in source discovery", () => {
  it("includes scope only when selected and does not split Unicode", async () => {
    const { sourceDiscoveryQuery } = await import("./general-research-client");
    expect(sourceDiscoveryQuery("Revenue?", "Asia FY2024", false)).toBe("Revenue?");
    expect(sourceDiscoveryQuery("Revenue?", "Asia FY2024", true)).toBe("Revenue? Asia FY2024");
    expect(sourceDiscoveryQuery("问".repeat(80), "🌏".repeat(80), true)).toBe("问".repeat(45) + " " + "🌏".repeat(24));
  });
});

describe("search date precision", () => {
  it("uses the provider's date without asserting a publication time or source validity", async () => {
    const { searchCandidateDate } = await import("./general-research-client");
    const cutoff = "2023-02-01T03:00:00+08:00"; // UTC cutoff day is January 31.
    expect(searchCandidateDate("2023-01-30T00:00:00Z", cutoff)).toEqual({date: "2023-01-30", relation: "before"});
    expect(searchCandidateDate("2023-01-31T00:00:00Z", cutoff)).toEqual({date: "2023-01-31", relation: "same_or_after"});
    expect(searchCandidateDate("2023-02-01T00:00:00Z", cutoff)).toEqual({date: "2023-02-01", relation: "same_or_after"});
    expect(searchCandidateDate(null, cutoff)).toEqual({date: null, relation: "unknown"});
    expect(searchCandidateDate("2023-02-31T00:00:00Z", cutoff)).toEqual({date: null, relation: "unknown"});
    expect(searchCandidateDate("2023-01-30", "invalid")).toEqual({date: "2023-01-30", relation: "unknown"});
    expect(searchCandidateDate("2023-01-30", null)).toEqual({date: "2023-01-30", relation: "unrestricted"});
  });
});
