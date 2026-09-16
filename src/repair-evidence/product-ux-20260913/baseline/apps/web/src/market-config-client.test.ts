import { describe, expect, it, vi } from "vitest";
import { authenticatedMarketFetcher, parseMarketDataConfig } from "./market-config-client";

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
});
