import { afterEach, describe, expect, it, vi } from "vitest";
import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { authenticatedResearchFetcher } from "../src/research-auth-fetch";
import type { OAuthSession } from "../src/oauth-client";
vi.mock("expo-crypto", () => ({ getRandomBytes: () => new Uint8Array(16) }));
import { createMobileApiClient } from "../src/api-runtime";

const api = "https://app.example.test";
const account = (id: string): OAuthSession => ({ account: { account_id: id, email: `${id}@example.test`, created_at: "2026-01-01T00:00:00Z" }, session_id: id, access_token: `synthetic-research-session-token-${id}`, expires_at: "2099-01-01T00:00:00Z", auth_mode: "oauth", provider: "google" });
function setup() {
  let current: OAuthSession | null = account("account-a");
  const getSession = vi.fn(() => current);
  const invalidateSession = vi.fn();
  const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response("{}", { status: 200 }));
  return { getSession, invalidateSession, fetcher, set: (next: OAuthSession | null) => { current = next; }, send: authenticatedResearchFetcher(api, { getSession, invalidateSession }, fetcher) };
}
afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); });

describe("mobile research request/account binding", () => {
  it("uses the current login on each call and clears caller-supplied credentials for deliberate anonymous use", async () => {
    const h = setup(); const a = h.getSession();
    await h.send(`${api}/v1/sessions`, { headers: { Authorization: "Bearer unrelated", Cookie: "not-to-send", "Idempotency-Key": "qa-idempotency" } });
    expect(new Headers(h.fetcher.mock.calls[0]?.[1]?.headers).get("Authorization")).toBe(`Bearer ${a!.access_token}`);
    expect(new Headers(h.fetcher.mock.calls[0]?.[1]?.headers).has("Cookie")).toBe(false);
    expect(new Headers(h.fetcher.mock.calls[0]?.[1]?.headers).get("Idempotency-Key")).toBe("qa-idempotency");
    const b = account("account-b"); h.set(b); await h.send(`${api}/v1/sessions`);
    expect(new Headers(h.fetcher.mock.calls[1]?.[1]?.headers).get("Authorization")).toBe(`Bearer ${b.access_token}`);
    h.set(null); await h.send(`${api}/v1/sessions`, { headers: { Authorization: "Bearer stale" } });
    expect(new Headers(h.fetcher.mock.calls[2]?.[1]?.headers).has("Authorization")).toBe(false);
  });
  it("preserves request headers, body, method and abort signal", async () => {
    const h = setup(); const controller = new AbortController();
    const request = new Request(`${api}/v1/sessions`, { headers: { "X-QA": "fixture" } });
    await h.send(request, { method: "POST", body: "{}", signal: controller.signal, credentials: "include", redirect: "follow" });
    expect(h.fetcher.mock.calls[0]?.[1]).toMatchObject({ method: "POST", body: "{}", signal: controller.signal, credentials: "omit", redirect: "error" });
    expect(new Headers(h.fetcher.mock.calls[0]?.[1]?.headers).get("X-QA")).toBe("fixture");
  });
  it.each(["https://other.example.test/v1/sessions", "http://app.example.test/v1/sessions", "https://app.example.test.evil.test/", "https://someone@app.example.test/v1/sessions"])("blocks an unintended target before sending credentials: %s", async (target) => {
    const h = setup(); await expect(h.send(target)).rejects.toThrow("origin mismatch"); expect(h.fetcher).not.toHaveBeenCalled();
  });
  it("also blocks cross-origin Request objects and accepts same-origin URL objects", async () => {
    const h = setup(); await expect(h.send(new Request("https://other.example.test/"))).rejects.toThrow("origin mismatch");
    await h.send(new URL(`${api}/v1/account`)); expect(h.fetcher).toHaveBeenCalledOnce();
  });
  it("stops rather than becoming anonymous while hydration or invalidation blocks access", async () => {
    const h = setup(); h.getSession.mockImplementation(() => { throw new Error("Research sign-in is not ready."); });
    await expect(h.send(`${api}/v1/sessions`)).rejects.toThrow("not ready"); expect(h.fetcher).not.toHaveBeenCalled();
  });
  it.each(["2000-01-01T00:00:00Z", "not-a-date"])("rejects expired/invalid sessions before dispatch", async (expires_at) => {
    const h = setup(); const expired = { ...h.getSession()!, expires_at }; h.set(expired);
    await expect(h.send(`${api}/v1/sessions`)).rejects.toThrow("expired");
    expect(h.invalidateSession).toHaveBeenCalledWith(expired); expect(h.fetcher).not.toHaveBeenCalled();
  });
  it("invalidates an authenticated 401 without retrying the request anonymously", async () => {
    const h = setup(); h.fetcher.mockResolvedValue(new Response("{}", { status: 401 }));
    expect((await h.send(`${api}/v1/sessions`)).status).toBe(401);
    expect(h.invalidateSession).toHaveBeenCalledWith(h.getSession()); expect(h.fetcher).toHaveBeenCalledOnce();
  });
  it("does not sign out a new account because an old account request returned 401", async () => {
    const h = setup(); let finish!: (response: Response) => void;
    h.fetcher.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const request = h.send(`${api}/v1/sessions`); h.set(account("account-b")); finish(new Response("{}", { status: 401 }));
    await expect(request).rejects.toThrow("account changed"); expect(h.invalidateSession).not.toHaveBeenCalled();
  });
  it("rejects late successful results after logout", async () => {
    const h = setup(); let finish!: (response: Response) => void;
    h.fetcher.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const request = h.send(`${api}/v1/sessions`); h.set(null); finish(new Response("{}"));
    await expect(request).rejects.toThrow("account changed");
  });
  it("retains login on quota/permission errors rather than conflating them with expiry", async () => {
    const h = setup(); h.fetcher.mockResolvedValue(new Response("{}", { status: 403 }));
    expect((await h.send(`${api}/v1/sessions`)).status).toBe(403); expect(h.invalidateSession).not.toHaveBeenCalled();
  });
  it("rejects redirect responses even from a transport that returns them directly", async () => {
    const h = setup(); h.fetcher.mockResolvedValue(new Response(null, { status: 307, headers: { Location: "https://other.example.test" } }));
    await expect(h.send(`${api}/v1/sessions`)).rejects.toThrow("redirects"); expect(h.fetcher).toHaveBeenCalledOnce();
  });
  it("wires the production mobile client through the account-bound fetcher", async () => {
    vi.stubEnv("EXPO_PUBLIC_FORESIGHTNINE_API_BASE_URL", api);
    const h = setup(); h.fetcher.mockResolvedValue(new Response("{}", { status: 403 })); vi.stubGlobal("fetch", h.fetcher);
    const client = createMobileApiClient({ getSession: h.getSession, invalidateSession: h.invalidateSession });
    expect(client).not.toBeNull();
    await expect(client!.createSession({ client_locale: "en-US" })).rejects.toMatchObject({ status: 403 });
    expect(String(h.fetcher.mock.calls[0]?.[0])).toBe(`${api}/v1/sessions`);
    expect(new Headers(h.fetcher.mock.calls[0]?.[1]?.headers).get("Authorization")).toBe(`Bearer ${h.getSession()!.access_token}`);
  });
  it("does not follow a real local HTTP redirect in the JavaScript fetch transport", async () => {
    let targetHits = 0;
    const target = createServer((_req, res) => { targetHits++; res.end("{}"); });
    const source = createServer((_req, res) => { const address = target.address(); if (!address || typeof address === "string") throw new Error("Missing test address"); res.writeHead(307, { Location: `http://127.0.0.1:${address.port}/redirect-target` }); res.end(); });
    try {
      await new Promise<void>((resolve) => target.listen(0, "127.0.0.1", resolve));
      await new Promise<void>((resolve) => source.listen(0, "127.0.0.1", resolve));
      const address = source.address(); if (!address || typeof address === "string") throw new Error("Missing test address");
      const origin = `http://127.0.0.1:${address.port}`; const owner = account("local-qa");
      const send = authenticatedResearchFetcher(origin, { getSession: () => owner, invalidateSession: vi.fn() });
      await expect(send(`${origin}/v1/sessions`)).rejects.toThrow(); expect(targetHits).toBe(0);
    } finally {
      source.closeAllConnections(); target.closeAllConnections();
      await Promise.all([new Promise<void>((resolve) => source.close(() => resolve())), new Promise<void>((resolve) => target.close(() => resolve()))]);
    }
  });
  it("keeps native redirect blocking installed before React Native starts (source guard, not native execution)", () => {
    const source = readFileSync(fileURLToPath(new URL("../android/app/src/main/java/com/foresightnine/mobile/MainApplication.kt", import.meta.url).href), "utf8");
    expect(source).toContain(".followRedirects(false)"); expect(source).toContain(".followSslRedirects(false)");
    expect(source.indexOf("OkHttpClientProvider.setOkHttpClientFactory")).toBeLessThan(source.indexOf("loadReactNative(this)"));
  });
});
