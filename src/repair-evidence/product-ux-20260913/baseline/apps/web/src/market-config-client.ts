import type { OAuthSession } from "./oauth-client";

export type MarketProviderId = "market_data" | "alpha_vantage" | "twelve_data" | "finnhub" | "yahoo";

export interface MarketProviderSummary {
  readonly provider_id: MarketProviderId;
  readonly display_name: string;
  readonly enabled: boolean;
  readonly requires_key: boolean;
  readonly personal_key_configured: boolean;
  readonly platform_available: boolean;
  readonly key_hint?: string;
}

export interface MarketDataConfig {
  readonly schema_version: "v1";
  readonly fallback_order: readonly MarketProviderId[];
  readonly providers: readonly MarketProviderSummary[];
  readonly updated_at?: string;
}

export interface MarketProviderDraft {
  readonly provider_id: MarketProviderId;
  readonly enabled: boolean;
  readonly api_key?: string;
  readonly clear_api_key?: boolean;
}

function endpoint(apiBaseUrl: string, suffix = ""): string {
  return new URL(`v1/account/market-data-config${suffix}`, `${apiBaseUrl.replace(/\/+$/, "")}/`).toString();
}

async function checkedJson(response: Response): Promise<unknown> {
  if (!response.ok) throw new Error(`market-data-config HTTP ${response.status}`);
  return response.json() as Promise<unknown>;
}

function isProviderId(value: unknown): value is MarketProviderId {
  return ["market_data", "alpha_vantage", "twelve_data", "finnhub", "yahoo"].includes(value as MarketProviderId);
}

export function parseMarketDataConfig(value: unknown): MarketDataConfig {
  if (!value || typeof value !== "object") throw new Error("Invalid market data configuration response.");
  const record = value as Record<string, unknown>;
  if (record.schema_version !== "v1" || !Array.isArray(record.fallback_order) || !Array.isArray(record.providers)) throw new Error("Invalid market data configuration response.");
  const order = record.fallback_order;
  if (order.length !== 5 || order.some((item) => !isProviderId(item)) || new Set(order).size !== order.length) throw new Error("Invalid market data provider order.");
  const providers = record.providers.map((item): MarketProviderSummary => {
    if (!item || typeof item !== "object") throw new Error("Invalid market data provider response.");
    const provider = item as Record<string, unknown>;
    if (!isProviderId(provider.provider_id) || typeof provider.display_name !== "string" || typeof provider.enabled !== "boolean" || typeof provider.requires_key !== "boolean" || typeof provider.personal_key_configured !== "boolean" || typeof provider.platform_available !== "boolean" || (provider.key_hint !== undefined && typeof provider.key_hint !== "string")) throw new Error("Invalid market data provider response.");
    return {
      provider_id: provider.provider_id,
      display_name: provider.display_name,
      enabled: provider.enabled,
      requires_key: provider.requires_key,
      personal_key_configured: provider.personal_key_configured,
      platform_available: provider.platform_available,
      ...(typeof provider.key_hint === "string" ? { key_hint: provider.key_hint } : {}),
    };
  });
  if (providers.length !== 5 || new Set(providers.map((item) => item.provider_id)).size !== providers.length) throw new Error("Invalid market data provider response.");
  return { schema_version: "v1", fallback_order: order as MarketProviderId[], providers, ...(typeof record.updated_at === "string" ? { updated_at: record.updated_at } : {}) };
}

function authorization(session: OAuthSession): HeadersInit {
  return { Accept: "application/json", Authorization: `Bearer ${session.access_token}` };
}

export async function fetchMarketDataConfig(apiBaseUrl: string, session: OAuthSession, signal?: AbortSignal): Promise<MarketDataConfig> {
  const response = await fetch(endpoint(apiBaseUrl), { method: "GET", headers: authorization(session), credentials: "omit", ...(signal ? { signal } : {}) });
  return parseMarketDataConfig(await checkedJson(response));
}

export async function saveMarketDataConfig(apiBaseUrl: string, session: OAuthSession, fallbackOrder: readonly MarketProviderId[], providers: readonly MarketProviderDraft[]): Promise<MarketDataConfig> {
  const response = await fetch(endpoint(apiBaseUrl), {
    method: "PUT",
    headers: { ...authorization(session), "Content-Type": "application/json" },
    credentials: "omit",
    body: JSON.stringify({ fallback_order: fallbackOrder, providers }),
  });
  return parseMarketDataConfig(await checkedJson(response));
}

export async function testMarketDataProvider(apiBaseUrl: string, session: OAuthSession, providerId: MarketProviderId): Promise<"ready" | "unavailable"> {
  const response = await fetch(endpoint(apiBaseUrl, "/test"), {
    method: "POST",
    headers: { ...authorization(session), "Content-Type": "application/json" },
    credentials: "omit",
    body: JSON.stringify({ provider_id: providerId }),
  });
  const value = await checkedJson(response);
  if (!value || typeof value !== "object" || (value as Record<string, unknown>).provider_id !== providerId || !["ready", "unavailable"].includes((value as Record<string, unknown>).status as string)) throw new Error("Invalid market data provider test response.");
  return (value as { status: "ready" | "unavailable" }).status;
}

/** Add a bearer only to the configured ForesightNine API origin. */
export function authenticatedMarketFetcher(apiBaseUrl: string, readSession: () => OAuthSession | null): typeof fetch {
  const allowedOrigin = new URL(apiBaseUrl).origin;
  return async (input, init) => {
    const target = new URL(typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url);
    const headers = new Headers(init?.headers);
    const session = readSession();
    if (session && target.origin === allowedOrigin) headers.set("Authorization", `Bearer ${session.access_token}`);
    return fetch(input, { ...init, headers, credentials: "omit", redirect: "error" });
  };
}
