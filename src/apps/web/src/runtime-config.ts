import { normalizeApiBaseUrl } from "@foresightnine/api-client";

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
