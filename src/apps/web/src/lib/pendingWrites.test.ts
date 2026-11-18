import { describe, expect, it, vi } from "vitest";
import { getOrCreateWriteKey, settleWrite, stableStringify } from "./pendingWrites";

const STORAGE_KEY = "foresightnine.web.pending-writes.v1";
const SCOPE = "run:acc:topic:ver";

describe("pendingWrites", () => {
  it("reuses the key for the same scope and fingerprint", () => {
    const a = getOrCreateWriteKey(SCOPE, "fp1");
    expect(getOrCreateWriteKey(SCOPE, "fp1")).toBe(a);
  });

  it("creates a new key for a different fingerprint", () => {
    const a = getOrCreateWriteKey(SCOPE, "fp1");
    const b = getOrCreateWriteKey(SCOPE, "fp2");
    expect(b).not.toBe(a);
    expect(getOrCreateWriteKey(SCOPE, "fp2")).toBe(b);
  });

  it("removes the entry on settle", () => {
    const a = getOrCreateWriteKey(SCOPE, "fp1");
    settleWrite(SCOPE);
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}")[SCOPE]).toBeUndefined();
    expect(getOrCreateWriteKey(SCOPE, "fp1")).not.toBe(a);
  });

  it("ignores entries older than 24 hours", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
    const a = getOrCreateWriteKey(SCOPE, "fp1");
    vi.setSystemTime(new Date("2026-01-01T23:59:00Z"));
    expect(getOrCreateWriteKey(SCOPE, "fp1")).toBe(a);
    vi.setSystemTime(new Date("2026-01-02T00:01:00Z"));
    expect(getOrCreateWriteKey(SCOPE, "fp1")).not.toBe(a);
  });

  it("produces a stable fingerprint independent of key order", () => {
    expect(stableStringify({ b: 1, a: { d: [1, { z: 1, y: 2 }], c: undefined } }))
      .toBe(stableStringify({ a: { c: undefined, d: [1, { y: 2, z: 1 }] }, b: 1 }));
  });
});
