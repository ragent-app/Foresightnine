import { apiBaseUrl, oauthReturnUri } from "../api/config";
import { apiRequest } from "../api/http";
import { clearSession, parseSession, saveSession } from "./session";

export const TICKET_PATTERN = /^[A-Za-z0-9_-]{43,256}$/;

export type OAuthProvider = "google" | "github";

const PROVIDERS: readonly OAuthProvider[] = ["google", "github"];
const FORBIDDEN_PARAMS = ["access_token", "provider_token", "code"] as const;
const CALLBACK_ERROR_VALUE = "oauth_callback_failed";

export type OAuthCallbackResult = { kind: "none" } | { kind: "ticket"; ticket: string } | { kind: "error" };

export async function fetchProviders(): Promise<{ provider: OAuthProvider; enabled: boolean }[]> {
  const body = await apiRequest<{ providers?: unknown }>("/v1/auth/providers");
  if (!Array.isArray(body.providers)) return [];
  return body.providers.flatMap((item): { provider: OAuthProvider; enabled: boolean }[] => {
    if (typeof item !== "object" || item === null) return [];
    const { provider, enabled } = item as { provider?: unknown; enabled?: unknown };
    return (provider === "google" || provider === "github") && PROVIDERS.includes(provider)
      ? [{ provider, enabled: enabled === true }]
      : [];
  });
}

export function oauthStartUrl(provider: OAuthProvider): string {
  const returnUri = oauthReturnUri();
  if (returnUri === null) throw new Error("oauth_not_configured");
  return `${apiBaseUrl()}/oauth/${provider}/start?return_uri=${encodeURIComponent(returnUri)}`;
}

export function startOAuth(provider: OAuthProvider): void {
  window.location.assign(oauthStartUrl(provider));
}

export function parseOAuthCallback(href: string, expectedReturnUri: string): OAuthCallbackResult {
  const invalid = () => new Error("oauth_callback_invalid");
  const current = new URL(href);
  const expected = new URL(expectedReturnUri);
  if (
    current.protocol !== expected.protocol
    || current.host !== expected.host
    || current.pathname !== expected.pathname
    || current.hash !== ""
    || href.includes("#")
  ) {
    return { kind: "none" };
  }

  const expectedKeys = new Set(expected.searchParams.keys());
  const allowed = new Set([...expectedKeys, "ticket", "error"]);
  const seen = new Set<string>();
  for (const key of current.searchParams.keys()) {
    if (seen.has(key)) throw invalid();
    seen.add(key);
  }
  for (const forbidden of FORBIDDEN_PARAMS) {
    if (seen.has(forbidden)) throw invalid();
  }
  for (const key of seen) {
    if (!allowed.has(key)) throw invalid();
  }
  for (const key of expectedKeys) {
    const expectedValues = expected.searchParams.getAll(key);
    const actualValues = current.searchParams.getAll(key);
    if (
      expectedValues.length !== 1
      || actualValues.length !== 1
      || actualValues[0] !== expectedValues[0]
    ) {
      throw invalid();
    }
  }

  const hasTicket = current.searchParams.has("ticket");
  const hasError = current.searchParams.has("error");
  if (hasTicket && hasError) throw invalid();
  if (hasTicket) {
    const ticket = current.searchParams.get("ticket") ?? "";
    if (!ticket || !TICKET_PATTERN.test(ticket)) throw invalid();
    return { kind: "ticket", ticket };
  }
  if (hasError) {
    const value = current.searchParams.get("error") ?? "";
    if (value !== CALLBACK_ERROR_VALUE) throw invalid();
    return { kind: "error" };
  }
  return { kind: "none" };
}

function stripCallbackParams(): void {
  try {
    const url = new URL(window.location.href);
    url.searchParams.delete("ticket");
    url.searchParams.delete("error");
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
  } catch {
    // Address bar cleanup is best effort.
  }
}

export async function completeOAuthCallback(): Promise<"none" | "signed_in" | "failed"> {
  const expectedReturnUri = oauthReturnUri();
  if (expectedReturnUri === null) return "none";

  let callback: OAuthCallbackResult;
  try {
    callback = parseOAuthCallback(window.location.href, expectedReturnUri);
  } catch {
    stripCallbackParams();
    return "failed";
  }
  if (callback.kind === "none") return "none";
  if (callback.kind === "error") {
    stripCallbackParams();
    return "failed";
  }

  try {
    // The exchange must not carry any previously stored bearer token.
    clearSession("signed_out");
    const payload = await apiRequest<unknown>("/v1/auth/oauth/exchange", {
      method: "POST",
      body: { ticket: callback.ticket },
    });
    saveSession(parseSession(payload));
    return "signed_in";
  } catch {
    return "failed";
  } finally {
    stripCallbackParams();
  }
}
