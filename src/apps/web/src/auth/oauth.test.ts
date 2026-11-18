// @vitest-environment-options {"url": "https://app.example.com/"}
import { describe, expect, it, vi } from "vitest";
import { jsonResponse, makeSession, storeSession } from "../test/fixtures";
import { completeOAuthCallback, oauthStartUrl, parseOAuthCallback } from "./oauth";
import { readSession } from "./session";

const RETURN = "https://app.example.com/?return=web";
const TICKET = "A".repeat(43);

describe("parseOAuthCallback", () => {
  it("returns none for a different path or a hash", () => {
    expect(parseOAuthCallback(`https://app.example.com/other?return=web&ticket=${TICKET}`, RETURN)).toEqual({ kind: "none" });
    expect(parseOAuthCallback(`https://app.example.com/?return=web&ticket=${TICKET}#x`, RETURN)).toEqual({ kind: "none" });
    expect(parseOAuthCallback("https://app.example.com/?return=web", RETURN)).toEqual({ kind: "none" });
  });

  it("accepts a ticket and an error", () => {
    expect(parseOAuthCallback(`https://app.example.com/?return=web&ticket=${TICKET}`, RETURN)).toEqual({ kind: "ticket", ticket: TICKET });
    expect(parseOAuthCallback("https://app.example.com/?return=web&error=oauth_callback_failed", RETURN)).toEqual({ kind: "error" });
  });

  it.each([
    ["duplicate param", `https://app.example.com/?return=web&ticket=${TICKET}&ticket=${TICKET}`],
    ["unexpected param", `https://app.example.com/?return=web&ticket=${TICKET}&foo=1`],
    ["code present", `https://app.example.com/?return=web&code=abc`],
    ["access_token present", `https://app.example.com/?return=web&access_token=abc`],
    ["provider_token present", `https://app.example.com/?return=web&provider_token=abc`],
    ["ticket and error", `https://app.example.com/?return=web&ticket=${TICKET}&error=oauth_callback_failed`],
    ["bad ticket", "https://app.example.com/?return=web&ticket=short"],
    ["empty ticket", "https://app.example.com/?return=web&ticket="],
    ["unknown error value", "https://app.example.com/?return=web&error=other"],
    ["changed expected value", `https://app.example.com/?return=evil&ticket=${TICKET}`],
    ["missing expected key", `https://app.example.com/?ticket=${TICKET}`],
  ])("throws for %s", (_name, href) => {
    expect(() => parseOAuthCallback(href, RETURN)).toThrow("oauth_callback_invalid");
  });
});

describe("oauthStartUrl", () => {
  it("throws when not configured and builds the URL otherwise", () => {
    vi.stubEnv("VITE_FORESIGHTNINE_OAUTH_RETURN_URI", "");
    expect(() => oauthStartUrl("google")).toThrow("oauth_not_configured");
    vi.stubEnv("VITE_FORESIGHTNINE_OAUTH_RETURN_URI", RETURN);
    vi.stubEnv("VITE_FORESIGHTNINE_API_BASE_URL", "https://api.example.com/");
    expect(oauthStartUrl("github")).toBe(
      `https://api.example.com/oauth/github/start?return_uri=${encodeURIComponent(RETURN)}`,
    );
  });
});

describe("completeOAuthCallback", () => {
  const session = makeSession();

  function arrive(search: string) {
    window.history.replaceState(null, "", `/${search}`);
  }

  it("returns none when no return URI is configured", async () => {
    vi.stubEnv("VITE_FORESIGHTNINE_OAUTH_RETURN_URI", "");
    arrive(`?return=web&ticket=${TICKET}`);
    await expect(completeOAuthCallback()).resolves.toBe("none");
  });

  it("exchanges the ticket, saves the session and strips the ticket", async () => {
    vi.stubEnv("VITE_FORESIGHTNINE_OAUTH_RETURN_URI", RETURN);
    storeSession(makeSession({ session_id: "old" }));
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => jsonResponse(session));
    vi.stubGlobal("fetch", fetchMock);
    arrive(`?return=web&ticket=${TICKET}`);

    await expect(completeOAuthCallback()).resolves.toBe("signed_in");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("https://app.example.com/v1/auth/oauth/exchange");
    expect(init?.method).toBe("POST");
    expect(init?.body).toBe(JSON.stringify({ ticket: TICKET }));
    expect((init?.headers as Record<string, string>).Authorization).toBeUndefined();
    expect(readSession()).toEqual(session);
    expect(window.location.search).toBe("?return=web");
  });

  it("returns failed when the exchange fails", async () => {
    vi.stubEnv("VITE_FORESIGHTNINE_OAUTH_RETURN_URI", RETURN);
    vi.stubGlobal("fetch", vi.fn(async () => new Response("boom", { status: 500 })));
    arrive(`?return=web&ticket=${TICKET}`);
    await expect(completeOAuthCallback()).resolves.toBe("failed");
    expect(readSession()).toBeNull();
    expect(window.location.search).toBe("?return=web");
  });

  it("returns failed for an error callback and strips it", async () => {
    vi.stubEnv("VITE_FORESIGHTNINE_OAUTH_RETURN_URI", RETURN);
    arrive("?return=web&error=oauth_callback_failed");
    await expect(completeOAuthCallback()).resolves.toBe("failed");
    expect(window.location.search).toBe("?return=web");
  });
});
