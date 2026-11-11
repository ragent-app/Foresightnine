import { isSafeOAuthApiBaseUrl, type OAuthSession } from "./oauth-client";

export interface ResearchSessionAccess {
  readonly getSession: () => OAuthSession | null;
  readonly invalidateSession: (expected: OAuthSession) => void;
}

/** Requests use the current in-memory login, never a token captured when the app booted. */
export function authenticatedResearchFetcher(
  apiBaseUrl: string,
  access: ResearchSessionAccess,
  fetcher: typeof fetch = fetch,
): typeof fetch {
  if (!isSafeOAuthApiBaseUrl(apiBaseUrl)) throw new Error("Research API origin is invalid.");
  const origin = new URL(apiBaseUrl).origin;
  return async (input, init) => {
    const request = typeof Request !== "undefined" && input instanceof Request ? input : null;
    const target = new URL(typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url);
    if (target.origin !== origin || target.username || target.password) throw new Error("Research API origin mismatch.");
    // getSession throws while hydration is pending or a session has been invalidated.
    // Neither condition may silently become an anonymous, separately metered request.
    const session = access.getSession();
    if (session && (!/^\S{32,256}$/.test(session.access_token) || !Number.isFinite(Date.parse(session.expires_at)) || Date.parse(session.expires_at) <= Date.now())) {
      access.invalidateSession(session);
      throw new Error("Research sign-in expired.");
    }
    const headers = new Headers(request?.headers);
    new Headers(init?.headers).forEach((value, key) => headers.set(key, value));
    headers.delete("Authorization");
    headers.delete("Cookie");
    if (session) headers.set("Authorization", `Bearer ${session.access_token}`);
    const response = await fetcher(input, { ...init, headers, credentials: "omit", redirect: "error" });
    // Native redirect prevention is also installed in MainApplication's OkHttp factory.
    if (response.status >= 300 && response.status < 400 || response.redirected || response.url && new URL(response.url).origin !== origin) {
      throw new Error("Research API redirects are not accepted.");
    }
    if (access.getSession() !== session) throw new Error("Research account changed.");
    if (session && response.status === 401) access.invalidateSession(session);
    return response;
  };
}
