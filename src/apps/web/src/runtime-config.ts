import { normalizeApiBaseUrl } from "@foresightnine/api-client";
import { normalizeMarketProviderUrl, type MarketRuntimeConfig } from "@foresightnine/market-data";

export function configuredApiBaseUrlFromEnvironment(environment: { readonly canonical?: unknown; readonly legacy?: unknown }): string | null {
  const configured = typeof environment.canonical === "string" && environment.canonical.trim().length > 0
    ? environment.canonical
    : typeof environment.legacy === "string" && environment.legacy.trim().length > 0
      ? environment.legacy
      : undefined;
  return normalizeApiBaseUrl(configured);
}

export function configuredApiBaseUrl(): string | null {
  return configuredApiBaseUrlFromEnvironment({
    canonical: import.meta.env.VITE_FORESIGHTNINE_API_BASE_URL,
    legacy: import.meta.env.VITE_API_BASE_URL,
  });
}

export function configuredMarketDataRuntimeConfigFromEnvironment(environment: { readonly mode?: unknown; readonly baseUrl?: unknown }): MarketRuntimeConfig {
  const baseUrl = normalizeMarketProviderUrl(environment.baseUrl);
  // Preserve an explicit public selection even when its URL is malformed or
  // missing. The runtime controller must expose a configuration error rather
  // than silently substituting mock data in a staging/production build.
  if (environment.mode === "public") return { mode: "public", providerId: "configured-public", ...(baseUrl ? { publicBaseUrl: baseUrl } : {}) };
  return { mode: "mock", providerId: "deterministic-mock" };
}

export function configuredMarketDataRuntimeConfig(): MarketRuntimeConfig {
  return configuredMarketDataRuntimeConfigFromEnvironment({ mode: import.meta.env.VITE_FORESIGHTNINE_MARKET_DATA_MODE, baseUrl: import.meta.env.VITE_FORESIGHTNINE_MARKET_DATA_BASE_URL });
}
