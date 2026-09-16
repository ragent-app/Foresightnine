import { describe, expect, it, vi } from "vitest";
import {
  createModelRequestGenerationGuard,
  deleteModelConfig,
  fetchModelConfig,
  isLoadedModelConfigOwner,
  modelConfigSessionKey,
  normalizeModelBaseUrl,
  parseModelConfig,
  parseModelConfigTestResponse,
  saveModelConfig,
  testModelConfig,
} from "./model-config-client";
import { sameOriginOAuthFetcher } from "./api-runtime";
import type { OAuthSession } from "./oauth-client";

const session = {
  account: { account_id: "account_model_test", email: "model@example.test", created_at: "2026-09-13T00:00:00Z" },
  session_id: "session_model_test",
  access_token: ["oauth", "token", "for", "model", "tests"].join("-"),
  expires_at: "2099-09-13T00:00:00Z",
  auth_mode: "oauth",
  provider: "google",
} as OAuthSession;

const platformConfig = { schema_version: "v1" as const, mode: "platform" as const, configured: true };
const personalConfig = {
  schema_version: "v1" as const,
  mode: "personal" as const,
  configured: true,
  base_url: "https://models.example.test/v1",
  model: "test-model",
  key_hint: "••••1234",
  updated_at: "2026-09-13T00:00:00Z",
};

describe("model configuration response parser", () => {
  it("hides account A configuration during a direct A-to-B session transition", () => {
    const accountA = session;
    const accountB = {
      ...session,
      account: { ...session.account, account_id: "account_model_b" },
      session_id: "session_model_b",
    };
    const loadedForA = modelConfigSessionKey(accountA);
    expect(isLoadedModelConfigOwner(loadedForA, accountA)).toBe(true);
    expect(isLoadedModelConfigOwner(loadedForA, accountB)).toBe(false);
    expect(isLoadedModelConfigOwner(null, accountB)).toBe(false);
  });

  it("invalidates account-bound async work when the session generation changes", () => {
    const guard = createModelRequestGenerationGuard();
    const accountA = guard.next();
    expect(guard.isCurrent(accountA)).toBe(true);
    const accountB = guard.next();
    expect(guard.isCurrent(accountA)).toBe(false);
    expect(guard.isCurrent(accountB)).toBe(true);
  });

  it("accepts complete v1 contracts and rejects incomplete personal responses", () => {
    expect(parseModelConfig({ ...platformConfig, updated_at: null })).toEqual(platformConfig);
    expect(parseModelConfig(personalConfig)).toEqual(personalConfig);
    expect(() => parseModelConfig({ ...personalConfig, base_url: null, model: null, key_hint: null, updated_at: null })).toThrow("personal model");
    expect(() => parseModelConfig({ ...platformConfig, base_url: "https://models.example.test/v1" })).toThrow("platform model");
  });

  it("accepts only the exact four-bullet four-alphanumeric key hint", () => {
    expect(parseModelConfig({ ...personalConfig, key_hint: "••••Ab09" }).key_hint).toBe("••••Ab09");
    for (const keyHint of ["••••123", "••••12345", "••••Ab_9", "secret-1234", "xxxx1234"]) {
      expect(() => parseModelConfig({ ...personalConfig, key_hint: keyHint })).toThrow("key hint");
    }
  });

  it("rejects extra fields, malformed URLs, and malformed test statuses", () => {
    expect(() => parseModelConfig({ ...platformConfig, extra: true })).toThrow();
    expect(() => parseModelConfig({ ...personalConfig, base_url: "http://public.example.test/v1" })).toThrow();
    expect(() => parseModelConfigTestResponse({ status: "ready", detail: "secret" })).toThrow();
    expect(parseModelConfigTestResponse({ status: "unavailable" })).toBe("unavailable");
  });
});

describe("model configuration URL and API client", () => {
  it("normalizes compatible HTTPS URLs and rejects credential/query state", () => {
    expect(normalizeModelBaseUrl("https://models.example.test/v1/")).toBe("https://models.example.test/v1");
    for (const value of ["http://localhost:3000/v1", "https://127.0.0.1/v1", "https://models.example.test:8443/v1", "https://user:pass@models.example.test/v1", "https://models.example.test/v1?key=value", "http://public.example.test/v1", "not a URL"]) {
      expect(normalizeModelBaseUrl(value)).toBeNull();
    }
  });

  it("uses bearer-authenticated GET/PUT/DELETE/POST endpoints and parses every response", async () => {
    const originalFetch = globalThis.fetch;
    const requests: Array<{ input: RequestInfo | URL; init: RequestInit }> = [];
    const responses = [personalConfig, personalConfig, platformConfig, { status: "ready" }];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init: init ?? {} });
      return new Response(JSON.stringify(responses[requests.length - 1]), { status: 200, headers: { "Content-Type": "application/json" } });
    }) as typeof fetch;
    try {
      await expect(fetchModelConfig("https://app.foresightnine.test", session)).resolves.toEqual(personalConfig);
      const replacementKey = ["new", "test", "value"].join("-");
      await expect(saveModelConfig("https://app.foresightnine.test", session, { mode: "personal", base_url: "https://models.example.test/v1/", model: "test-model", api_key: replacementKey })).resolves.toEqual(personalConfig);
      await expect(deleteModelConfig("https://app.foresightnine.test", session)).resolves.toEqual(platformConfig);
      await expect(testModelConfig("https://app.foresightnine.test", session)).resolves.toBe("ready");

      expect(requests.map(({ input, init }) => [String(input), init.method])).toEqual([
        ["https://app.foresightnine.test/v1/account/model-config", "GET"],
        ["https://app.foresightnine.test/v1/account/model-config", "PUT"],
        ["https://app.foresightnine.test/v1/account/model-config", "DELETE"],
        ["https://app.foresightnine.test/v1/account/model-config/test", "POST"],
      ]);
      for (const request of requests) expect(new Headers(request.init.headers).get("Authorization")).toBe(`Bearer ${session.access_token}`);
      expect(new Headers(requests[1]?.init.headers).get("Content-Type")).toBe("application/json");
      expect(JSON.parse(String(requests[1]?.init.body))).toEqual({ mode: "personal", base_url: "https://models.example.test/v1", model: "test-model", api_key: replacementKey });
      expect(new Headers(requests[3]?.init.headers).get("Content-Type")).toBe("application/json");
      expect(requests[3]?.init.body).toBe("{}");
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it("never sends a masked hint as a replacement key", async () => {
    const originalFetch = globalThis.fetch;
    const mock = vi.fn();
    globalThis.fetch = mock as typeof fetch;
    try {
      await expect(saveModelConfig("https://app.foresightnine.test", session, { mode: "personal", api_key: "••••" + "1234" })).rejects.toThrow("Invalid model configuration update");
      expect(mock).not.toHaveBeenCalled();
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it("binds the current OAuth session to same-origin research requests only", async () => {
    const originalFetch = globalThis.fetch;
    const calls: Array<{ input: RequestInfo | URL; init: RequestInit }> = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ input, init: init ?? {} });
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
    try {
      const fetcher = sameOriginOAuthFetcher("https://app.foresightnine.test", () => session);
      await fetcher("https://app.foresightnine.test/v1/challenges", { method: "POST" });
      const headers = new Headers(calls[0]?.init.headers);
      expect(headers.get("Authorization")).toBe(`Bearer ${session.access_token}`);
      await expect(fetcher("https://other.example.test/v1/challenges")).rejects.toThrow("Cross-origin API request blocked");
      expect(calls).toHaveLength(1);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it("invalidates browser account state after an authenticated 401", async () => {
    const originalFetch = globalThis.fetch;
    const invalidated = vi.fn();
    globalThis.fetch = vi.fn(async () => new Response("{}", { status: 401 })) as typeof fetch;
    try {
      const fetcher = sameOriginOAuthFetcher("https://app.foresightnine.test", () => session, invalidated);
      const response = await fetcher("https://app.foresightnine.test/v1/challenges");
      expect(response.status).toBe(401);
      expect(invalidated).toHaveBeenCalledOnce();
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});
