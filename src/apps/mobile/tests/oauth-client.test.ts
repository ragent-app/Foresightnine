import { describe, expect, it, vi } from "vitest";
import { clearOAuthSession, configuredOAuthReturnUriFromEnvironment, deleteOAuthAccount, exchangeOAuthTicket, logoutOAuthSession, oauthStartUrl, parseOAuthCallbackUrl, readOAuthSession, saveOAuthSession, MOBILE_OAUTH_SESSION_KEY } from "../src/oauth-client";

const session = { account: { account_id: "account_12345678", email: "user@example.test", created_at: "2026-08-31T00:00:00Z" }, session_id: "session_12345678", access_token: "token_abcdefghijklmnopqrstuvwxyz0123456789", expires_at: "2026-09-30T00:00:00Z", auth_mode: "oauth" as const, provider: "google" as const };
const store = () => { const data = new Map<string, string>(); return { getItemAsync: vi.fn(async (key: string) => data.get(key) ?? null), setItemAsync: vi.fn(async (key: string, value: string) => { data.set(key, value); }), deleteItemAsync: vi.fn(async (key: string) => { data.delete(key); }) }; };

describe("mobile OAuth client boundary", () => {
  it("requires an explicit safe HTTPS or foresightnine return URI", () => {
    expect(configuredOAuthReturnUriFromEnvironment(undefined)).toBeNull();
    expect(configuredOAuthReturnUriFromEnvironment("http://localhost/callback")).toBeNull();
    expect(configuredOAuthReturnUriFromEnvironment("foresightnine://oauth/complete")).toBe("foresightnine://oauth/complete");
  });
  it("builds an encoded server start URL and strictly parses callbacks", () => {
    const target = "foresightnine://oauth/complete?surface=mobile";
    const url = new URL(oauthStartUrl("https://api.example.test", "google", target, target));
    expect(url.pathname).toBe("/oauth/google/start");
    const ticket = "A".repeat(43);
    expect(parseOAuthCallbackUrl(`${target}&ticket=${ticket}`, target)).toEqual({ kind: "ticket", ticket });
    expect(() => parseOAuthCallbackUrl(`${target}&ticket=${ticket}&ticket=${ticket}`, target)).toThrow();
    expect(() => parseOAuthCallbackUrl(`${target}&access_token=secret`, target)).toThrow();
  });
  it("exchanges ticket and keeps access token out of the request URL", async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify(session), { status: 200 }));
    await exchangeOAuthTicket("https://api.example.test", "A".repeat(43), fetcher);
    const call = fetcher.mock.calls[0] as unknown as [RequestInfo | URL, RequestInit];
    expect(String(call[0])).not.toContain(session.access_token);
    expect(call[1]).toMatchObject({ body: JSON.stringify({ ticket: "A".repeat(43) }) });
  });
  it("preserves an explicitly configured API path when building OAuth URLs", async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify(session), { status: 200 }));
    const start = new URL(oauthStartUrl("https://api.example.test/f9", "google", "https://app.example.test/oauth/callback", "https://app.example.test/oauth/callback"));
    expect(start.pathname).toBe("/f9/oauth/google/start");
    await exchangeOAuthTicket("https://api.example.test/f9", "A".repeat(43), fetcher);
    expect(String((fetcher.mock.calls[0] as unknown as [RequestInfo | URL, RequestInit])[0])).toContain("https://api.example.test/f9/v1/auth/oauth/exchange");
  });
  it("rejects malformed response timestamps and identifiers", async () => {
    const malformed = { ...session, expires_at: "2026-09-30 00:00:00" };
    const fetcher = vi.fn(async () => new Response(JSON.stringify(malformed), { status: 200 }));
    await expect(exchangeOAuthTicket("https://api.example.test", "A".repeat(43), fetcher)).rejects.toThrow();
    const malformedId = { ...session, account: { ...session.account, account_id: "!" } };
    const idFetcher = vi.fn(async () => new Response(JSON.stringify(malformedId), { status: 200 }));
    await expect(exchangeOAuthTicket("https://api.example.test", "A".repeat(43), idFetcher)).rejects.toThrow();
  });
  it("uses SecureStore adapter and bearer headers for lifecycle operations", async () => {
    const sessionStore = store();
    await saveOAuthSession(session, sessionStore);
    expect((await readOAuthSession(sessionStore))?.account.email).toBe(session.account.email);
    const fetcher = vi.fn(async () => new Response("{}", { status: 200 }));
    await logoutOAuthSession("https://api.example.test", session.access_token, fetcher);
    await deleteOAuthAccount("https://api.example.test", session.access_token, fetcher);
    expect((fetcher.mock.calls[0] as unknown as [RequestInfo | URL, RequestInit])[1]).toMatchObject({ method: "POST", headers: expect.objectContaining({ Authorization: `Bearer ${session.access_token}` }) });
    await clearOAuthSession(sessionStore);
    expect((await sessionStore.getItemAsync(MOBILE_OAUTH_SESSION_KEY))).toBeNull();
  });
});
