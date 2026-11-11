import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearOAuthSession, configuredOAuthReturnUriFromEnvironment, deleteOAuthAccount, exchangeOAuthTicket, fetchAccountBillingSummary, logoutOAuthSession, oauthStartUrl, parseOAuthCallbackUrl, readOAuthSession, saveOAuthSession, WEB_OAUTH_SESSION_KEY, type OAuthSession } from "./oauth-client";

const session: OAuthSession = { account: { account_id: "account_12345678", email: "user@example.test", created_at: "2026-08-31T00:00:00Z" }, session_id: "session_12345678", access_token: "token_abcdefghijklmnopqrstuvwxyz0123456789", expires_at: "2026-09-30T00:00:00Z", auth_mode: "oauth", provider: "google" };

describe("web OAuth client boundary", () => {
  beforeEach(() => {
    // Freeze Date so fixture expiry does not make tests calendar-dependent.
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-09-01T00:00:00Z"));
  });
  afterEach(() => vi.useRealTimers());

  it("rejects a stored session at its expiry boundary", () => {
    const storage = new Map<string, string>();
    const fakeStorage = { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => { storage.set(key, value); }, removeItem: (key: string) => { storage.delete(key); } } as Storage;
    saveOAuthSession(session, fakeStorage);
    vi.setSystemTime(new Date(session.expires_at));
    expect(readOAuthSession(fakeStorage)).toBeNull();
  });

  it("requires an explicit HTTPS return URI", () => {
    expect(configuredOAuthReturnUriFromEnvironment(undefined)).toBeNull();
    expect(configuredOAuthReturnUriFromEnvironment(" ")).toBeNull();
    expect(configuredOAuthReturnUriFromEnvironment("http://localhost/callback")).toBeNull();
    expect(configuredOAuthReturnUriFromEnvironment("https://app.example.test/oauth/complete")).toBe("https://app.example.test/oauth/complete");
  });

  it("builds a server start URL with encoded return URI", () => {
    const url = new URL(oauthStartUrl("https://api.example.test/", "google", "https://app.example.test/oauth/complete?surface=web", "https://app.example.test/oauth/complete?surface=web"));
    expect(url.pathname).toBe("/oauth/google/start");
    expect(url.searchParams.get("return_uri")).toBe("https://app.example.test/oauth/complete?surface=web");
  });

  it("strictly parses ticket callbacks and rejects bearer credentials", () => {
    const target = "https://app.example.test/oauth/complete?surface=web";
    const ticket = "A".repeat(43);
    expect(parseOAuthCallbackUrl(`${target}&ticket=${ticket}`, target)).toEqual({ kind: "ticket", ticket });
    expect(parseOAuthCallbackUrl(`${target}&error=oauth_callback_failed`, target)).toEqual({ kind: "error", error: "oauth_callback_failed" });
    expect(() => parseOAuthCallbackUrl(`${target}&access_token=secret`, target)).toThrow();
    expect(() => parseOAuthCallbackUrl(`${target}&ticket=short`, target)).toThrow();
    expect(() => parseOAuthCallbackUrl(`${target}&ticket=`, target)).toThrow();
    expect(() => parseOAuthCallbackUrl(`${target}&ticket=${ticket}&ticket=${ticket}`, target)).toThrow();
    expect(parseOAuthCallbackUrl(`https://user:pass@app.example.test/oauth/complete?surface=web&ticket=${ticket}`, target)).toEqual({ kind: "none" });
    expect(() => oauthStartUrl("https://api.example.test", "google", target, null)).toThrow();
  });

  it("exchanges once and sends bearer only in the authorization header", async () => {
    const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => new Response(JSON.stringify(session), { status: 200, headers: { "Content-Type": "application/json" } }));
    const result = await exchangeOAuthTicket("https://api.example.test", "A".repeat(43), fetcher);
    expect(result).toEqual(session);
    const exchangeCall = fetcher.mock.calls[0] as unknown as [RequestInfo | URL, RequestInit];
    expect(String(exchangeCall[0])).toContain("/v1/auth/oauth/exchange");
    expect(exchangeCall[1]).toMatchObject({ body: JSON.stringify({ ticket: "A".repeat(43) }) });
    expect(JSON.stringify(exchangeCall)).not.toContain(session.access_token);
  });

  it("uses sessionStorage only and clears it after logout/delete", async () => {
    const storage = new Map<string, string>();
    const fakeStorage = { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => { storage.set(key, value); }, removeItem: (key: string) => { storage.delete(key); } } as Storage;
    saveOAuthSession(session, fakeStorage);
    expect(readOAuthSession(fakeStorage)).toEqual(session);
    const fetcher = vi.fn(async () => new Response("{}", { status: 200 }));
    await logoutOAuthSession("https://api.example.test", session.access_token, fetcher);
    const logoutCall = fetcher.mock.calls[0] as unknown as [RequestInfo | URL, RequestInit];
    expect(logoutCall[1]).toMatchObject({ method: "POST", body: JSON.stringify({}), headers: expect.objectContaining({ Authorization: `Bearer ${session.access_token}`, "Content-Type": "application/json" }) });
    clearOAuthSession(fakeStorage);
    await deleteOAuthAccount("https://api.example.test", session.access_token, fetcher);
    const deleteCall = fetcher.mock.calls[1] as unknown as [RequestInfo | URL, RequestInit];
    expect(deleteCall[1]).toMatchObject({ method: "DELETE" });
    expect(fakeStorage.getItem(WEB_OAUTH_SESSION_KEY)).toBeNull();
  });

  it("reads only the server-authoritative account quota with the bearer header", async () => {
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({
      account: { account_id: "account_12345678", email: "user@example.test" },
      plan: "pro",
      entitlement_status: "active",
      product_id: "foresightnine_pro",
      base_plan_id: "monthly",
      quota_limit: 30000000,
      quota_consumed: 1200,
      quota_reserved: 4500,
      quota_remaining: 29994300,
      quota_resets_at: "2026-10-01T00:00:00Z",
      provider_model: "glm-5.3-flash",
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    const result = await fetchAccountBillingSummary("https://api.example.test", session.access_token, fetcher);
    expect(result).toMatchObject({ plan: "pro", quota_remaining: 29994300, provider_model: "glm-5.3-flash" });
    const call = fetcher.mock.calls[0] as unknown as [RequestInfo | URL, RequestInit];
    expect(String(call[0])).toContain("/v1/account");
    expect(call[1]).toMatchObject({ method: "GET", credentials: "omit", headers: expect.objectContaining({ Authorization: `Bearer ${session.access_token}` }) });
    expect(JSON.stringify(call)).not.toContain("foresightnine_pro");
  });

  it("rejects an account quota response with extra fields", async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ account: {}, extra: true }), { status: 200 }));
    await expect(fetchAccountBillingSummary("https://api.example.test", session.access_token, fetcher)).rejects.toThrow("Account summary response is invalid.");
  });
});
