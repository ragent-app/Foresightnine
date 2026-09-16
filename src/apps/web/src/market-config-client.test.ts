import { describe, expect, it, vi } from "vitest";
import { authenticatedMarketFetcher, createMarketRequestGenerationGuard, isLoadedMarketConfigOwner, marketConfigSessionKey, parseMarketDataConfig, saveMarketDataConfig, testMarketDataProvider } from "./market-config-client";

const response = {
  schema_version: "v1",
  fallback_order: ["market_data", "alpha_vantage", "twelve_data", "finnhub", "yahoo"],
  providers: [
    ["market_data", true], ["alpha_vantage", true], ["twelve_data", true], ["finnhub", true], ["yahoo", false],
  ].map(([provider_id, requires_key]) => ({ provider_id, display_name: provider_id, enabled: true, requires_key, personal_key_configured: false, platform_available: true })),
};

describe("market data configuration client", () => {
  it("accepts the complete typed provider set", () => {
    expect(parseMarketDataConfig(response).fallback_order).toEqual(response.fallback_order);
  });

  it("accepts FastAPI nullable optional fields without exposing a key", () => {
    const nullable = {
      ...response,
      updated_at: null,
      providers: response.providers.map((provider) => ({ ...provider, key_hint: null })),
    };
    const parsed = parseMarketDataConfig(nullable);
    expect(parsed.updated_at).toBeUndefined();
    expect(parsed.providers.every((provider) => provider.key_hint === undefined)).toBe(true);
  });

  it("accepts only a fixed masked key hint and a valid timestamp", () => {
    const masked = {
      ...response,
      updated_at: "2026-09-13T00:00:00Z",
      providers: response.providers.map((provider, index) => ({ ...provider, key_hint: index === 0 ? "••••Ab09" : null })),
    };
    expect(parseMarketDataConfig(masked).providers[0]?.key_hint).toBe("••••Ab09");
    expect(() => parseMarketDataConfig({ ...masked, providers: masked.providers.map((provider, index) => ({ ...provider, key_hint: index === 0 ? "full-provider-secret" : null })) })).toThrow("Invalid market data provider response.");
    expect(() => parseMarketDataConfig({ ...masked, updated_at: "not-a-date" })).toThrow("Invalid market data configuration timestamp.");
  });

  it("rejects duplicate or incomplete provider order", () => {
    expect(() => parseMarketDataConfig({ ...response, fallback_order: ["yahoo", "yahoo"] })).toThrow();
  });

  it("sends the bearer only to the configured API origin", async () => {
    const original = globalThis.fetch;
    const calls: RequestInit[] = [];
    const mock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => { calls.push(init ?? {}); return new Response("{}", { status: 200 }); });
    globalThis.fetch = mock as typeof fetch;
    try {
      const session = { access_token: "private-token" } as never;
      const fetcher = authenticatedMarketFetcher("https://app.foresightnine.com", () => session);
      await fetcher("https://app.foresightnine.com/snapshots", { headers: { Accept: "application/json" } });
      await fetcher("https://example.com/snapshots", { headers: { Accept: "application/json" } });
      const firstHeaders = new Headers(calls[0]?.headers);
      const secondHeaders = new Headers(calls[1]?.headers);
      expect(firstHeaders.get("Authorization")).toBe("Bearer private-token");
      expect(secondHeaders.has("Authorization")).toBe(false);
    } finally {
      globalThis.fetch = original;
    }
  });

  it("invalidates account A work when account B starts and forwards abort signals", async () => {
    const guard = createMarketRequestGenerationGuard();
    const accountA = guard.next();
    const accountB = guard.next();
    expect(guard.isCurrent(accountA)).toBe(false);
    expect(guard.isCurrent(accountB)).toBe(true);

    const original = globalThis.fetch;
    const calls: RequestInit[] = [];
    globalThis.fetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      calls.push(init ?? {});
      if (calls.length === 1) return new Response(JSON.stringify(response), { status: 200 });
      return new Response(JSON.stringify({ provider_id: "yahoo", status: "ready" }), { status: 200 });
    }) as typeof fetch;
    const controller = new AbortController();
    const session = { access_token: "account-b-token" } as never;
    try {
      await saveMarketDataConfig("https://app.foresightnine.test", session, response.fallback_order as never, response.providers.map((item) => ({ provider_id: item.provider_id, enabled: item.enabled })) as never, controller.signal);
      await testMarketDataProvider("https://app.foresightnine.test", session, "yahoo", controller.signal);
      expect(calls.every((call) => call.signal === controller.signal)).toBe(true);
    } finally {
      globalThis.fetch = original;
    }
  });

  it("blocks a mutation until the currently rendered account configuration has loaded", () => {
    const accountA = { account: { account_id: "account-a" }, session_id: "session-a" } as never;
    const accountB = { account: { account_id: "account-b" }, session_id: "session-b" } as never;
    const loadedForA = marketConfigSessionKey(accountA);
    expect(isLoadedMarketConfigOwner(loadedForA, accountA)).toBe(true);
    expect(isLoadedMarketConfigOwner(loadedForA, accountB)).toBe(false);
    expect(isLoadedMarketConfigOwner(null, accountB)).toBe(false);
  });
});
