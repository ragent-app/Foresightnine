import type { OAuthProvider } from "./components/SocialLoginPanel";

export const WEB_OAUTH_SESSION_KEY = "foresightnine.web.oauth-session.v1";

export interface OAuthSession {
  readonly account: { readonly account_id: string; readonly email: string; readonly created_at: string };
  readonly session_id: string;
  readonly access_token: string;
  readonly expires_at: string;
  readonly auth_mode: "oauth";
  readonly provider: OAuthProvider;
}

export interface OAuthCallbackResult {
  readonly kind: "none" | "ticket" | "error";
  readonly ticket?: string;
  readonly error?: "oauth_callback_failed";
}

const TICKET_PATTERN = /^[A-Za-z0-9_-]{43,256}$/;

function isSafeHttpsUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && Boolean(url.hostname) && !url.username && !url.password && !url.hash;
  } catch {
    return false;
  }
}

function isSafeApiBaseUrl(value: string): boolean {
  try {
    const url = new URL(value);
    const local = url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
    return (url.protocol === "https:" || local) && Boolean(url.hostname) && !url.username && !url.password && !url.search && !url.hash;
  } catch {
    return false;
  }
}

export function configuredOAuthReturnUriFromEnvironment(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed && isSafeHttpsUrl(trimmed) ? trimmed : null;
}

export function configuredOAuthReturnUri(): string | null {
  return configuredOAuthReturnUriFromEnvironment(import.meta.env.VITE_FORESIGHTNINE_OAUTH_RETURN_URI);
}

export function oauthStartUrl(apiBaseUrl: string, provider: OAuthProvider, returnUri: string, configuredReturnUri: string | null = configuredOAuthReturnUri()): string {
  if (provider !== "google" && provider !== "github") throw new Error("Unsupported OAuth provider.");
  if (!isSafeApiBaseUrl(apiBaseUrl) || !configuredReturnUri || !isSafeHttpsUrl(configuredReturnUri) || returnUri !== configuredReturnUri) throw new Error("OAuth return URI is not configured safely.");
  const url = new URL(`/oauth/${provider}/start`, `${apiBaseUrl.replace(/\/+$/, "")}/`);
  url.searchParams.set("return_uri", returnUri);
  return url.toString();
}

function sameCallbackTarget(actual: URL, expected: URL): boolean {
  return actual.protocol === expected.protocol && actual.host === expected.host && actual.pathname === expected.pathname && actual.hash === "" && !actual.username && !actual.password;
}

export function parseOAuthCallbackUrl(value: string, expectedReturnUri: string): OAuthCallbackResult {
  if (!isSafeHttpsUrl(expectedReturnUri)) throw new Error("OAuth return URI is not configured safely.");
  const actual = new URL(value);
  const expected = new URL(expectedReturnUri);
  if (!sameCallbackTarget(actual, expected)) return { kind: "none" };
  const counts = new Map<string, number>();
  for (const key of actual.searchParams.keys()) counts.set(key, (counts.get(key) ?? 0) + 1);
  for (const [key, count] of counts) {
    if (![...expected.searchParams.keys(), "ticket", "error"].includes(key)) throw new Error("Unexpected OAuth callback parameter.");
    if (count !== 1) throw new Error("OAuth callback contains duplicate parameters.");
  }
  for (const [key, expectedValue] of expected.searchParams) {
    if (actual.searchParams.get(key) !== expectedValue) throw new Error("OAuth callback target mismatch.");
  }
  if (actual.searchParams.has("access_token") || actual.searchParams.has("provider_token") || actual.searchParams.has("code")) throw new Error("OAuth callback exposed a forbidden credential parameter.");
  const ticketPresent = actual.searchParams.has("ticket");
  const errorPresent = actual.searchParams.has("error");
  const ticket = actual.searchParams.get("ticket");
  const error = actual.searchParams.get("error");
  if (ticket && error) throw new Error("OAuth callback contains conflicting results.");
  if ((ticketPresent && !ticket) || (errorPresent && !error)) throw new Error("OAuth callback result is empty.");
  if (ticket) {
    if (!TICKET_PATTERN.test(ticket)) throw new Error("OAuth callback ticket is invalid.");
    return { kind: "ticket", ticket };
  }
  if (error) return error === "oauth_callback_failed" ? { kind: "error", error } : (() => { throw new Error("OAuth callback error is invalid."); })();
  return { kind: "none" };
}

function parseSession(payload: unknown): OAuthSession {
  if (!payload || typeof payload !== "object") throw new Error("OAuth session response is invalid.");
  const value = payload as Record<string, unknown>;
  const account = value.account;
  if (!account || typeof account !== "object") throw new Error("OAuth session response is invalid.");
  const accountValue = account as Record<string, unknown>;
  if (Object.keys(value).sort().join(",") !== "access_token,account,auth_mode,expires_at,provider,session_id" || Object.keys(accountValue).sort().join(",") !== "account_id,created_at,email") throw new Error("OAuth session response is invalid.");
  if (typeof accountValue.account_id !== "string" || !accountValue.account_id || typeof accountValue.email !== "string" || !accountValue.email || typeof accountValue.created_at !== "string" || !accountValue.created_at) throw new Error("OAuth session response is invalid.");
  if (typeof value.session_id !== "string" || !value.session_id || typeof value.access_token !== "string" || !/^\S{32,256}$/.test(value.access_token) || typeof value.expires_at !== "string" || !value.expires_at || !Number.isFinite(Date.parse(value.expires_at)) || Date.parse(value.expires_at) <= Date.now() || value.auth_mode !== "oauth" || (value.provider !== "google" && value.provider !== "github")) throw new Error("OAuth session response is invalid.");
  return { account: { account_id: accountValue.account_id, email: accountValue.email, created_at: accountValue.created_at }, session_id: value.session_id, access_token: value.access_token, expires_at: value.expires_at, auth_mode: "oauth", provider: value.provider };
}

export async function exchangeOAuthTicket(apiBaseUrl: string, ticket: string, fetcher: typeof fetch = fetch): Promise<OAuthSession> {
  if (!isSafeApiBaseUrl(apiBaseUrl) || !TICKET_PATTERN.test(ticket)) throw new Error("OAuth ticket is invalid.");
  const response = await fetcher(new URL("/v1/auth/oauth/exchange", `${apiBaseUrl.replace(/\/+$/, "")}/`), { method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json" }, body: JSON.stringify({ ticket }), credentials: "omit" });
  if (!response.ok) throw new Error("OAuth exchange failed.");
  return parseSession(await response.json());
}

export function readOAuthSession(storage: Storage = sessionStorage): OAuthSession | null {
  const raw = storage.getItem(WEB_OAUTH_SESSION_KEY);
  if (!raw) return null;
  try { const session = parseSession(JSON.parse(raw)); if (Date.parse(session.expires_at) <= Date.now()) throw new Error("OAuth session expired."); return session; } catch { storage.removeItem(WEB_OAUTH_SESSION_KEY); return null; }
}

export function saveOAuthSession(session: OAuthSession, storage: Storage = sessionStorage): void { storage.setItem(WEB_OAUTH_SESSION_KEY, JSON.stringify(session)); }
export function clearOAuthSession(storage: Storage = sessionStorage): void { storage.removeItem(WEB_OAUTH_SESSION_KEY); }

async function authenticatedMutation(path: string, accessToken: string, fetcher: typeof fetch): Promise<void> {
  if (!isSafeApiBaseUrl(new URL(path).origin) || !/^\S{32,256}$/.test(accessToken)) throw new Error("OAuth session is invalid.");
  const response = await fetcher(path, { method: path.endsWith("/account") ? "DELETE" : "POST", headers: { Accept: "application/json", Authorization: `Bearer ${accessToken}` }, credentials: "omit" });
  if (!response.ok) throw new Error("OAuth account operation failed.");
}

export function logoutOAuthSession(apiBaseUrl: string, accessToken: string, fetcher: typeof fetch = fetch): Promise<void> { if (!isSafeApiBaseUrl(apiBaseUrl)) return Promise.reject(new Error("API base URL is invalid.")); return authenticatedMutation(new URL("/v1/auth/logout", `${apiBaseUrl.replace(/\/+$/, "")}/`).toString(), accessToken, fetcher); }
export function deleteOAuthAccount(apiBaseUrl: string, accessToken: string, fetcher: typeof fetch = fetch): Promise<void> { if (!isSafeApiBaseUrl(apiBaseUrl)) return Promise.reject(new Error("API base URL is invalid.")); return authenticatedMutation(new URL("/v1/auth/account", `${apiBaseUrl.replace(/\/+$/, "")}/`).toString(), accessToken, fetcher); }
