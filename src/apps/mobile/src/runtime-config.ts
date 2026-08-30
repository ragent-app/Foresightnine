import { normalizeApiBaseUrl } from "@foresightnine/api-client";
import { normalizeMarketProviderUrl, type MarketRuntimeConfig } from "@foresightnine/market-data";

export const STAGING_API_BASE_URL = "https://api-staging.foresightnine.com";

export function configuredApiBaseUrlFromEnvironment(environment: { readonly canonical?: unknown; readonly legacy?: unknown }): string | null {
  const configured = typeof environment.canonical === "string" && environment.canonical.trim().length > 0
    ? environment.canonical
    : typeof environment.legacy === "string" && environment.legacy.trim().length > 0
      ? environment.legacy
      : undefined;
  return normalizeApiBaseUrl(configured);
}

function hasExplicitApiBaseUrl(environment: { readonly canonical?: unknown; readonly legacy?: unknown }): boolean {
  return [environment.canonical, environment.legacy].some((value) => typeof value === "string" && value.trim().length > 0);
}

export function configuredApiBaseUrl(): string | null {
  if (typeof process === "undefined") return null;
  const environment = {
    canonical: process.env.EXPO_PUBLIC_FORESIGHTNINE_API_BASE_URL,
    legacy: process.env.EXPO_PUBLIC_API_BASE_URL,
  };
  const configured = configuredApiBaseUrlFromEnvironment(environment);
  if (configured !== null || hasExplicitApiBaseUrl(environment)) return configured;
  // Vitest must remain deterministic/offline; deployed builds use staging only
  // when neither supported API override is present.
  return process.env.NODE_ENV === "test" ? null : STAGING_API_BASE_URL;
}

export function configuredMarketDataRuntimeConfigFromEnvironment(environment: { readonly mode?: unknown; readonly baseUrl?: unknown }): MarketRuntimeConfig {
  const baseUrl = normalizeMarketProviderUrl(environment.baseUrl);
  // Preserve an explicit public selection so a bad/missing URL is visible as
  // an error instead of silently changing a staging/production build to mock.
  if (environment.mode === "public") return { mode: "public", providerId: "configured-public", ...(baseUrl ? { publicBaseUrl: baseUrl } : {}) };
  return { mode: "mock", providerId: "deterministic-mock" };
}

export function configuredMarketDataRuntimeConfig(): MarketRuntimeConfig {
  if (typeof process === "undefined") return { mode: "mock", providerId: "deterministic-mock" };
  return configuredMarketDataRuntimeConfigFromEnvironment({ mode: process.env.EXPO_PUBLIC_FORESIGHTNINE_MARKET_DATA_MODE, baseUrl: process.env.EXPO_PUBLIC_FORESIGHTNINE_MARKET_DATA_BASE_URL });
}
