import { describe, expect, it, vi } from "vitest";
import { reconcileBillingPurchases } from "../src/billing-reconciliation";
import type { BillingPurchase } from "../src/billing-client";

function purchase(token: string, state = 1, productId: BillingPurchase["productId"] = "foresightnine_pro"): BillingPurchase {
  return {
    purchaseToken: token,
    productId,
    basePlanId: "monthly",
    purchaseState: state,
    isAcknowledged: false,
    isAutoRenewing: true,
  };
}

describe("mobile billing reconciliation", () => {
  it("verifies every unique purchased subscription and reports pending records", async () => {
    const verify = vi.fn(async () => undefined);
    const processed = new Set<string>();
    const result = await reconcileBillingPurchases(
      [purchase("token-a"), purchase("token-a"), purchase("token-b", 2), purchase("token-c", 1, null)],
      "session-a",
      processed,
      verify,
    );

    expect(result).toEqual({ verified: 1, failed: 0, pending: 1, ignored: 2 });
    expect(verify).toHaveBeenCalledTimes(1);
    expect(processed).toEqual(new Set(["session-a:token-a"]));
  });

  it("retries failed verification but suppresses successful duplicates only within one session", async () => {
    const processed = new Set<string>();
    const failing = vi.fn(async () => { throw new Error("provider unavailable"); });
    expect(await reconcileBillingPurchases([purchase("token-a")], "session-a", processed, failing)).toMatchObject({ failed: 1 });
    expect(processed.size).toBe(0);

    const passing = vi.fn(async () => undefined);
    expect(await reconcileBillingPurchases([purchase("token-a")], "session-a", processed, passing)).toMatchObject({ verified: 1 });
    expect(await reconcileBillingPurchases([purchase("token-a")], "session-a", processed, passing)).toMatchObject({ ignored: 1 });
    expect(await reconcileBillingPurchases([purchase("token-a")], "session-b", processed, passing)).toMatchObject({ verified: 1 });
    expect(passing).toHaveBeenCalledTimes(2);
  });
});

describe("explicit subscription refresh", () => {
  it("rechecks a previously verified token when restoring renewed/cancelled subscription state", async () => {
    const processed = new Set<string>(); const verify = vi.fn(async () => undefined);
    await reconcileBillingPurchases([purchase("token-a")], "session-a", processed, verify);
    expect(await reconcileBillingPurchases([purchase("token-a")], "session-a", processed, verify, { refreshVerified: true })).toMatchObject({ verified: 1, ignored: 0 });
    expect(verify).toHaveBeenCalledTimes(2);
  });

  it("coalesces an overlapping restore and waits for the pending server verification", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const verify = vi.fn(() => gate); const processed = new Set<string>();
    const first = reconcileBillingPurchases([purchase("token-a")], "session-a", processed, verify);
    let secondSettled = false;
    const second = reconcileBillingPurchases([purchase("token-a")], "session-a", processed, verify, { refreshVerified: true }).then((result) => { secondSettled = true; return result; });
    await Promise.resolve(); await Promise.resolve();
    expect(secondSettled).toBe(false);
    expect(verify).toHaveBeenCalledOnce();
    release();
    expect(await first).toMatchObject({ verified: 1 });
    expect(await second).toMatchObject({ ignored: 1 });
  });

  it("reports shared in-flight failures and permits a subsequent retry", async () => {
    let reject!: (reason: Error) => void;
    const gate = new Promise<void>((_resolve, fail) => { reject = fail; });
    const verify = vi.fn(() => gate); const processed = new Set<string>();
    const first = reconcileBillingPurchases([purchase("token-a")], "session-a", processed, verify);
    const second = reconcileBillingPurchases([purchase("token-a")], "session-a", processed, verify, { refreshVerified: true });
    reject(new Error("synthetic verification failure"));
    expect(await first).toMatchObject({ failed: 1 });
    expect(await second).toMatchObject({ failed: 1 });
    expect(processed.size).toBe(0);
    expect(await reconcileBillingPurchases([purchase("token-a")], "session-a", processed, async () => undefined, { refreshVerified: true })).toMatchObject({ verified: 1 });
  });

  it("retries a failed status refresh without forgetting the earlier verification", async () => {
    const processed = new Set(["session-a:token-a"]);
    const verify = vi.fn(async () => { throw new Error("temporary provider failure"); });
    expect(await reconcileBillingPurchases([purchase("token-a")], "session-a", processed, verify, { refreshVerified: true })).toMatchObject({ failed: 1 });
    expect(processed.has("session-a:token-a")).toBe(true);
    expect(await reconcileBillingPurchases([purchase("token-a")], "session-a", processed, async () => undefined, { refreshVerified: true })).toMatchObject({ verified: 1 });
  });

  it("never verifies pending records even on a forced refresh", async () => {
    const verify = vi.fn(async () => undefined);
    expect(await reconcileBillingPurchases([purchase("token-a", 2)], "session-a", new Set(), verify, { refreshVerified: true })).toMatchObject({ pending: 1, verified: 0 });
    expect(verify).not.toHaveBeenCalled();
  });

  it("does not share an in-flight request across authenticated sessions", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const verify = vi.fn(() => gate); const processed = new Set<string>();
    const first = reconcileBillingPurchases([purchase("token-a")], "session-a", processed, verify);
    const second = reconcileBillingPurchases([purchase("token-a")], "session-b", processed, verify, { refreshVerified: true });
    expect(verify).toHaveBeenCalledTimes(2); release();
    await Promise.all([first, second]);
    expect(processed.size).toBe(2);
  });
});
