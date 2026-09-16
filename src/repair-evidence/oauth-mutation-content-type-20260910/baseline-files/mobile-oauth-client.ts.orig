export type OAuthProvider = "google" | "github";
export const MOBILE_OAUTH_SESSION_KEY = "foresightnine.mobile.oauth-session.v1";

export interface OAuthSession {
  readonly account: { readonly account_id: string; readonly email: string; readonly created_at: string };
  readonly session_id: string;
  readonly access_token: string;
  readonly expires_at: string;
  readonly auth_mode: "oauth";
  readonly provider: OAuthProvider;
}

export interface SecureSessionStore {
  readonly getItemAsync: (key: string) => Promise<string | null>;
  readonly setItemAsync: (key: string, value: string) => Promise<void>;
  readonly deleteItemAsync: (key: string) => Promise<void>;
}

export const secureSessionStore: SecureSessionStore = {
  getItemAsync: async (key) => (await import("expo-secure-store")).getItemAsync(key),
  setItemAsync: async (key, value) => { await (await import("expo-secure-store")).setItemAsync(key, value); },
  deleteItemAsync: async (key) => { await (await import("expo-secure-store")).deleteItemAsync(key); },
};

const TICKET_PATTERN = /^[A-Za-z0-9_-]{43,256}$/;
const IDENTIFIER_PATTERN = /^[A-Za-z0-9_-]{8,80}$/;
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const ISO_INSTANT_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/;

function safeReturnUri(value: string): boolean {
  try {
    const url = new URL(value);
    const custom = url.protocol === "foresightnine:" && Boolean(url.hostname);
    return (url.protocol === "https:" || custom) && !url.username && !url.password && !url.hash;
  } catch {
    return false;
  }
}

export function isSafeOAuthReturnUri(value: string): boolean { return safeReturnUri(value); }

function safeApiBaseUrl(value: string): boolean {
  try {
    const url = new URL(value);
    const loopback = url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
    return (url.protocol === "https:" || loopback) && Boolean(url.hostname) && !url.username && !url.password && !url.search && !url.hash;
  } catch {
    return false;
  }
}

export function isSafeOAuthApiBaseUrl(value: string): boolean { return safeApiBaseUrl(value); }

export function configuredOAuthReturnUriFromEnvironment(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed && safeReturnUri(trimmed) ? trimmed : null;
}

export function configuredOAuthReturnUri(): string | null {
  if (typeof process === "undefined") return null;
  return configuredOAuthReturnUriFromEnvironment(process.env.EXPO_PUBLIC_FORESIGHTNINE_OAUTH_RETURN_URI);
}

export function oauthStartUrl(apiBaseUrl: string, provider: OAuthProvider, returnUri: string, configuredReturnUri: string | null = configuredOAuthReturnUri()): string {
  if (!safeApiBaseUrl(apiBaseUrl) || (provider !== "google" && provider !== "github") || !configuredReturnUri || returnUri !== configuredReturnUri || !safeReturnUri(returnUri)) throw new Error("OAuth return URI is not configured safely.");
  const url = new URL(`oauth/${provider}/start`, `${apiBaseUrl.replace(/\/+$/, "")}/`);
  url.searchParams.set("return_uri", returnUri);
  return url.toString();
}

function sameTarget(actual: URL, expected: URL): boolean {
  return actual.protocol === expected.protocol && actual.hostname === expected.hostname && actual.port === expected.port && actual.pathname === expected.pathname && actual.hash === "" && !actual.username && !actual.password;
}

export function parseOAuthCallbackUrl(value: string, expectedReturnUri: string): { readonly kind: "none" | "ticket" | "error"; readonly ticket?: string; readonly error?: "oauth_callback_failed" } {
  if (!safeReturnUri(expectedReturnUri)) throw new Error("OAuth return URI is not configured safely.");
  const actual = new URL(value);
  const expected = new URL(expectedReturnUri);
  if (!sameTarget(actual, expected)) return { kind: "none" };
  const counts = new Map<string, number>();
  for (const key of actual.searchParams.keys()) counts.set(key, (counts.get(key) ?? 0) + 1);
  for (const [key, count] of counts) {
    if (![...expected.searchParams.keys(), "ticket", "error"].includes(key) || count !== 1) throw new Error("OAuth callback parameters are invalid.");
  }
  for (const [key, expectedValue] of expected.searchParams) if (actual.searchParams.get(key) !== expectedValue) throw new Error("OAuth callback target mismatch.");
  if (["access_token", "provider_token", "code"].some((key) => actual.searchParams.has(key))) throw new Error("OAuth callback exposed a forbidden credential parameter.");
  const hasTicket = actual.searchParams.has("ticket");
  const hasError = actual.searchParams.has("error");
  const ticket = actual.searchParams.get("ticket");
  const error = actual.searchParams.get("error");
  if ((hasTicket && !ticket) || (hasError && !error) || (ticket && error)) throw new Error("OAuth callback result is invalid.");
  if (ticket) { if (!TICKET_PATTERN.test(ticket)) throw new Error("OAuth callback ticket is invalid."); return { kind: "ticket", ticket }; }
  if (error) { if (error !== "oauth_callback_failed") throw new Error("OAuth callback error is invalid."); return { kind: "error", error }; }
  return { kind: "none" };
}

function parseSession(payload: unknown): OAuthSession {
  if (!payload || typeof payload !== "object") throw new Error("OAuth session response is invalid.");
  const value = payload as Record<string, unknown>;
  const account = value.account;
  if (!account || typeof account !== "object") throw new Error("OAuth session response is invalid.");
  const accountValue = account as Record<string, unknown>;
  if (Object.keys(value).sort().join(",") !== "access_token,account,auth_mode,expires_at,provider,session_id" || Object.keys(accountValue).sort().join(",") !== "account_id,created_at,email") throw new Error("OAuth session response is invalid.");
  if (typeof accountValue.account_id !== "string" || !IDENTIFIER_PATTERN.test(accountValue.account_id) || typeof accountValue.email !== "string" || !EMAIL_PATTERN.test(accountValue.email) || typeof accountValue.created_at !== "string" || !ISO_INSTANT_PATTERN.test(accountValue.created_at) || !Number.isFinite(Date.parse(accountValue.created_at))) throw new Error("OAuth session response is invalid.");
  if (typeof value.session_id !== "string" || !IDENTIFIER_PATTERN.test(value.session_id) || typeof value.access_token !== "string" || !/^\S{32,256}$/.test(value.access_token) || typeof value.expires_at !== "string" || !ISO_INSTANT_PATTERN.test(value.expires_at) || !Number.isFinite(Date.parse(value.expires_at)) || Date.parse(value.expires_at) <= Date.now() || value.auth_mode !== "oauth" || (value.provider !== "google" && value.provider !== "github")) throw new Error("OAuth session response is invalid.");
  return { account: { account_id: accountValue.account_id, email: accountValue.email, created_at: accountValue.created_at }, session_id: value.session_id, access_token: value.access_token, expires_at: value.expires_at, auth_mode: "oauth", provider: value.provider };
}

export async function exchangeOAuthTicket(apiBaseUrl: string, ticket: string, fetcher: typeof fetch = fetch): Promise<OAuthSession> {
  if (!safeApiBaseUrl(apiBaseUrl) || !TICKET_PATTERN.test(ticket)) throw new Error("OAuth ticket is invalid.");
  const response = await fetcher(new URL("v1/auth/oauth/exchange", `${apiBaseUrl.replace(/\/+$/, "")}/`), { method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json" }, body: JSON.stringify({ ticket }), credentials: "omit" });
  if (!response.ok) throw new Error("OAuth exchange failed.");
  return parseSession(await response.json());
}

export async function readOAuthSession(store: SecureSessionStore = secureSessionStore): Promise<OAuthSession | null> {
  const raw = await store.getItemAsync(MOBILE_OAUTH_SESSION_KEY);
  if (!raw) return null;
  try { const session = parseSession(JSON.parse(raw)); return session; } catch { await store.deleteItemAsync(MOBILE_OAUTH_SESSION_KEY); return null; }
}

export function saveOAuthSession(session: OAuthSession, store: SecureSessionStore = secureSessionStore): Promise<void> { return store.setItemAsync(MOBILE_OAUTH_SESSION_KEY, JSON.stringify(session)); }
export function clearOAuthSession(store: SecureSessionStore = secureSessionStore): Promise<void> { return store.deleteItemAsync(MOBILE_OAUTH_SESSION_KEY); }

async function authenticatedMutation(apiBaseUrl: string, path: string, accessToken: string, method: "POST" | "DELETE", fetcher: typeof fetch): Promise<void> {
  if (!safeApiBaseUrl(apiBaseUrl) || !/^\S{32,256}$/.test(accessToken)) throw new Error("OAuth session is invalid.");
  const response = await fetcher(new URL(path.replace(/^\/+/, ""), `${apiBaseUrl.replace(/\/+$/, "")}/`), { method, headers: { Accept: "application/json", Authorization: `Bearer ${accessToken}` }, credentials: "omit" });
  if (!response.ok) throw new Error("OAuth account operation failed.");
}

export function logoutOAuthSession(apiBaseUrl: string, accessToken: string, fetcher: typeof fetch = fetch): Promise<void> { return authenticatedMutation(apiBaseUrl, "/v1/auth/logout", accessToken, "POST", fetcher); }
export function deleteOAuthAccount(apiBaseUrl: string, accessToken: string, fetcher: typeof fetch = fetch): Promise<void> { return authenticatedMutation(apiBaseUrl, "/v1/auth/account", accessToken, "DELETE", fetcher); }
