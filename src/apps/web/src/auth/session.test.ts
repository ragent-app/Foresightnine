import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { makeSession } from "../test/fixtures";
import {
  SESSION_STORAGE_KEY,
  clearSession,
  parseSession,
  readSession,
  saveSession,
  useSession,
} from "./session";

describe("parseSession", () => {
  it("accepts valid oauth and local_mock payloads", () => {
    const oauth = makeSession();
    expect(parseSession(oauth)).toEqual(oauth);
    const { provider: _provider, ...rest } = makeSession({ auth_mode: "local_mock" });
    expect(parseSession(rest)).toEqual(rest);
  });

  it("rejects invalid payloads", () => {
    const valid = makeSession();
    const { provider: _p, ...noProvider } = valid;
    expect(() => parseSession({ ...valid, extra: 1 })).toThrow("invalid_session");
    expect(() => parseSession(noProvider)).toThrow("invalid_session");
    expect(() => parseSession({ ...valid, provider: "other" })).toThrow("invalid_session");
    expect(() => parseSession({ ...valid, access_token: "short" })).toThrow("invalid_session");
    expect(() => parseSession({ ...valid, expires_at: "not a date" })).toThrow("invalid_session");
    expect(() => parseSession({ ...valid, account: { ...valid.account, extra: "x" } })).toThrow("invalid_session");
    expect(() => parseSession({ ...valid, account: { ...valid.account, email: "" } })).toThrow("invalid_session");
    expect(() => parseSession({ ...valid, auth_mode: "local_mock" })).toThrow("invalid_session");
    expect(() => parseSession(null)).toThrow("invalid_session");
  });
});

describe("readSession", () => {
  it("returns null and removes an expired session", () => {
    sessionStorage.setItem(
      SESSION_STORAGE_KEY,
      JSON.stringify(makeSession({ expires_at: new Date(Date.now() - 1000).toISOString() })),
    );
    expect(readSession()).toBeNull();
    expect(sessionStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
  });

  it("returns null and removes an invalid entry", () => {
    sessionStorage.setItem(SESSION_STORAGE_KEY, "{not json");
    expect(readSession()).toBeNull();
    expect(sessionStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
  });
});

describe("useSession", () => {
  it("updates on save and clear with a stable snapshot", () => {
    const { result, rerender } = renderHook(() => useSession());
    expect(result.current).toBeNull();
    const session = makeSession();
    act(() => saveSession(session));
    expect(result.current).toEqual(session);
    const first = result.current;
    rerender();
    expect(result.current).toBe(first);
    act(() => clearSession("signed_out"));
    expect(result.current).toBeNull();
  });
});
