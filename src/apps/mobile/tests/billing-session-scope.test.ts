import { describe, expect, it, vi } from "vitest";
import { BILLING_REQUEST_TIMEOUT_MS, createSessionBillingRequests } from "../src/billing-session-scope";
import type { AccountBillingSummary } from "../src/billing-api";
import type { OAuthSession } from "../src/oauth-client";

const api = "https://app.example.test";
const session = (id: string): OAuthSession => ({
  account: { account_id: id, email: `${id}@example.test`, created_at: "2026-01-01T00:00:00Z" },
  session_id: id, access_token: `synthetic-session-token-${id}`, expires_at: "2099-01-01T00:00:00Z", auth_mode: "oauth", provider: "google",
});
const free: AccountBillingSummary = { plan: "free", entitlement_status: "active", product_id: null, base_plan_id: null, quota_limit: 200000, quota_consumed: 0, quota_reserved: 0, quota_remaining: 200000, quota_resets_at: null, provider_model: "synthetic-model" };
const pro: AccountBillingSummary = { ...free, plan: "pro", product_id: "foresightnine_pro", base_plan_id: "monthly", quota_limit: 30000000, quota_remaining: 30000000 };
const purchaseToken = "synthetic-purchase-token-not-a-real-receipt";
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function setup() {
  const publish = vi.fn();
  const summary = vi.fn().mockResolvedValue(free);
  const verify = vi.fn().mockResolvedValue({ summary: pro });
  const requests = createSessionBillingRequests(publish, { summary, verify });
  const owner = session("owner-a");
  requests.bind(api, owner); publish.mockClear();
  return { requests, owner, publish, summary, verify };
}

describe("billing request scope follows the active login", () => {
  it("publishes only the authenticated server summary and passes the correct account credentials", async () => {
    const h = setup();
    await h.requests.refresh(api, h.owner);
    expect(h.summary).toHaveBeenCalledWith(api, h.owner.access_token, undefined, expect.any(AbortSignal));
    expect(h.publish).toHaveBeenLastCalledWith(free);
    await h.requests.verify(api, h.owner, purchaseToken, "foresightnine_pro");
    expect(h.verify).toHaveBeenCalledWith(api, h.owner.access_token, purchaseToken, "foresightnine_pro", undefined, expect.any(AbortSignal));
    expect(h.publish).toHaveBeenLastCalledWith(pro);
  });

  it.each(["refresh", "verify"] as const)("rejects a late %s result after sign-out without repainting entitlement", async (kind) => {
    const h = setup(); const pending = deferred<AccountBillingSummary | {summary: AccountBillingSummary}>();
    (kind === "refresh" ? h.summary : h.verify).mockReturnValueOnce(pending.promise);
    const work = kind === "refresh" ? h.requests.refresh(api, h.owner) : h.requests.verify(api, h.owner, purchaseToken, "foresightnine_pro");
    const failure = expect(work).rejects.toThrow("session changed");
    await Promise.resolve();
    h.requests.bind(api, null);
    pending.resolve(kind === "refresh" ? pro : { summary: pro });
    await failure;
    expect(h.publish.mock.calls).toEqual([[null]]);
  });

  it("does not block a new account behind an old request, or overwrite its completed summary", async () => {
    const h = setup(); const slow = deferred<AccountBillingSummary>();
    h.summary.mockReturnValueOnce(slow.promise);
    const old = expect(h.requests.refresh(api, h.owner)).rejects.toThrow("session changed");
    await Promise.resolve();
    const other = session("owner-b"); h.requests.bind(api, other);
    await h.requests.refresh(api, other);
    slow.resolve(pro); await old;
    expect(h.publish.mock.calls).toEqual([[null], [free]]);
    expect(h.summary).toHaveBeenLastCalledWith(api, other.access_token, undefined, expect.any(AbortSignal));
  });

  it("does not dispatch queued verification after sign-out", async () => {
    const h = setup(); const slow = deferred<AccountBillingSummary>();
    h.summary.mockReturnValueOnce(slow.promise);
    const first = expect(h.requests.refresh(api, h.owner)).rejects.toThrow("session changed");
    const queued = expect(h.requests.verify(api, h.owner, purchaseToken, "foresightnine_pro")).rejects.toThrow("session changed");
    await Promise.resolve(); h.requests.bind(api, null); slow.resolve(free);
    await Promise.all([first, queued]);
    expect(h.verify).not.toHaveBeenCalled();
  });

  it("serializes a slow initial summary before verification, so Free cannot overwrite Pro", async () => {
    const h = setup(); const slow = deferred<AccountBillingSummary>();
    h.summary.mockReturnValueOnce(slow.promise);
    const first = h.requests.refresh(api, h.owner);
    const second = h.requests.verify(api, h.owner, purchaseToken, "foresightnine_pro");
    await Promise.resolve(); expect(h.verify).not.toHaveBeenCalled();
    slow.resolve(free); await Promise.all([first, second]);
    expect(h.publish.mock.calls).toEqual([[free], [pro]]);
  });

  it("does not fetch stored entitlement until outstanding verification settles", async () => {
    const h = setup(); const slow = deferred<{summary: AccountBillingSummary}>();
    h.verify.mockReturnValueOnce(slow.promise); h.summary.mockResolvedValue(pro);
    const first = h.requests.verify(api, h.owner, purchaseToken, "foresightnine_pro");
    const second = h.requests.refresh(api, h.owner);
    await Promise.resolve(); expect(h.summary).not.toHaveBeenCalled();
    slow.resolve({ summary: pro }); await Promise.all([first, second]);
    expect(h.publish.mock.calls).toEqual([[pro], [pro]]);
  });

  it("keeps known entitlement on refresh failure and allows subsequent retry", async () => {
    const h = setup(); await h.requests.verify(api, h.owner, purchaseToken, "foresightnine_pro");
    h.summary.mockRejectedValueOnce(new Error("synthetic offline"));
    await expect(h.requests.refresh(api, h.owner)).rejects.toThrow("synthetic offline");
    expect(h.publish.mock.calls).toEqual([[pro]]);
    h.summary.mockResolvedValue({ ...pro, entitlement_status: "expired", quota_remaining: 0 });
    await h.requests.refresh(api, h.owner);
    expect(h.publish).toHaveBeenLastCalledWith(expect.objectContaining({ entitlement_status: "expired", quota_remaining: 0 }));
  });

  it("rejects stale closures before any network call", async () => {
    const h = setup(); h.requests.bind(api, session("owner-b"));
    await expect(h.requests.refresh(api, h.owner)).rejects.toThrow("active account");
    await expect(h.requests.verify(api, h.owner, purchaseToken, "foresightnine_pro")).rejects.toThrow("active account");
    expect(h.summary).not.toHaveBeenCalled(); expect(h.verify).not.toHaveBeenCalled();
  });

  it("does not let an anonymous stale refresh clear a signed-in summary", async () => {
    const h = setup(); await h.requests.refresh(api, h.owner); h.publish.mockClear();
    await h.requests.refresh(api, null);
    expect(h.publish).not.toHaveBeenCalled();
    await expect(h.requests.verify(api, null, purchaseToken, "foresightnine_pro")).rejects.toThrow("active account");
  });

  it("invalidates old generations even when the same session object is rebound", async () => {
    const h = setup(); const slow = deferred<AccountBillingSummary>(); h.summary.mockReturnValueOnce(slow.promise);
    const old = expect(h.requests.refresh(api, h.owner)).rejects.toThrow("session changed");
    await Promise.resolve(); h.requests.bind(api, null); h.requests.bind(api, h.owner);
    await h.requests.refresh(api, h.owner); slow.resolve(pro); await old;
    expect(h.publish).toHaveBeenLastCalledWith(free);
  });

  it("invalidates on API origin change", async () => {
    const h = setup(); h.requests.bind("https://other.example.test", h.owner);
    await expect(h.requests.refresh(api, h.owner)).rejects.toThrow("active account");
    expect(h.summary).not.toHaveBeenCalled();
  });

  it("stops writes on unmount and supports lifecycle resume without reviving old work", async () => {
    const h = setup(); const slow = deferred<AccountBillingSummary>(); h.summary.mockReturnValueOnce(slow.promise);
    const old = expect(h.requests.refresh(api, h.owner)).rejects.toThrow("session changed");
    await Promise.resolve(); h.requests.suspend();
    await expect(h.requests.refresh(api, h.owner)).rejects.toThrow("active account");
    h.requests.resume(); await h.requests.refresh(api, h.owner);
    slow.resolve(pro); await old;
    expect(h.publish.mock.calls).toEqual([[free]]);
  });

  it.each(["refresh", "verify"] as const)("bounds a stalled %s, aborts its request and unblocks retry without accepting late output", async (kind) => {
    vi.useFakeTimers();
    try {
      const h = setup(); const slow = deferred<AccountBillingSummary | {summary: AccountBillingSummary}>();
      const endpoint = kind === "refresh" ? h.summary : h.verify;
      endpoint.mockReturnValueOnce(slow.promise);
      const work = kind === "refresh" ? h.requests.refresh(api, h.owner) : h.requests.verify(api, h.owner, purchaseToken, "foresightnine_pro");
      const failed = expect(work).rejects.toThrow("timed out");
      await Promise.resolve();
      const signal = endpoint.mock.calls[0]!.at(-1) as AbortSignal;
      await vi.advanceTimersByTimeAsync(BILLING_REQUEST_TIMEOUT_MS); await failed;
      expect(signal.aborted).toBe(true); expect(h.publish).not.toHaveBeenCalled();
      await h.requests.refresh(api, h.owner);
      slow.resolve(kind === "refresh" ? pro : { summary: pro });
      await Promise.resolve();
      expect(h.publish.mock.calls).toEqual([[free]]);
      expect(vi.getTimerCount()).toBe(0);
    } finally { vi.useRealTimers(); }
  });
});
