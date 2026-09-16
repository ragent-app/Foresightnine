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
