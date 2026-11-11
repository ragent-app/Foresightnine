import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const harness = vi.hoisted(() => ({
  receive: null as ((value: unknown) => void) | null,
  remove: vi.fn(),
  restore: vi.fn(),
  platform: { OS: "android" },
}));
vi.mock("react-native", () => ({
  Platform: harness.platform,
  NativeModules: { ForesightNineBilling: { restorePurchases: harness.restore } },
  NativeEventEmitter: class {
    addListener(_name: string, receive: (value: unknown) => void) {
      harness.receive = receive;
      return { remove: harness.remove };
    }
  },
}));

import { restoreBillingPurchases, subscribeToBillingPurchases } from "../src/billing-client";

const purchase = {
  purchaseToken: "synthetic-purchase-token-for-tests-only",
  productId: "foresightnine_pro", basePlanId: "monthly", purchaseState: 1,
  isAcknowledged: true, isAutoRenewing: true,
};

describe("native billing result boundary", () => {
  beforeEach(() => { harness.receive = null; harness.platform.OS = "android"; vi.clearAllMocks(); });
  afterEach(() => vi.restoreAllMocks());

  it("reports cancellation instead of silently dropping the native result", () => {
    const onPurchase = vi.fn(); const onResult = vi.fn();
    subscribeToBillingPurchases(onPurchase, onResult);
    harness.receive?.({ responseCode: 1, debugMessage: "not for display" });
    expect(onResult).toHaveBeenCalledWith({ kind: "cancelled", responseCode: 1 });
    expect(onPurchase).not.toHaveBeenCalled();
  });

  it.each([-1, 2, 3, 4, 5, 6, 7, 8, 12, 0])("reports non-purchase result %i without exposing native debug text", (responseCode) => {
    const onPurchase = vi.fn(); const onResult = vi.fn();
    subscribeToBillingPurchases(onPurchase, onResult);
    harness.receive?.({ responseCode, debugMessage: "provider internal details" });
    expect(onResult).toHaveBeenCalledWith({ kind: "error", responseCode });
    expect(JSON.stringify(onResult.mock.calls)).not.toContain("provider internal details");
    expect(onPurchase).not.toHaveBeenCalled();
  });

  it.each([1, 2])("forwards purchased/pending state %i to server reconciliation, not to error feedback", (purchaseState) => {
    const onPurchase = vi.fn(); const onResult = vi.fn();
    subscribeToBillingPurchases(onPurchase, onResult);
    harness.receive?.({ ...purchase, purchaseState });
    expect(onPurchase).toHaveBeenCalledWith({ ...purchase, purchaseState });
    expect(onResult).not.toHaveBeenCalled();
  });

  it("does not treat unspecified state as a completed purchase", () => {
    const onPurchase = vi.fn(); const onResult = vi.fn();
    subscribeToBillingPurchases(onPurchase, onResult);
    harness.receive?.({ ...purchase, purchaseState: 0 });
    expect(onPurchase).not.toHaveBeenCalled();
    expect(onResult).toHaveBeenCalledWith({ kind: "error", responseCode: 0 });
  });

  it("ignores malformed records and unknown products", () => {
    const onPurchase = vi.fn(); const onResult = vi.fn();
    subscribeToBillingPurchases(onPurchase, onResult);
    for (const value of [null, "raw", {}, { responseCode: "1" }, { ...purchase, purchaseToken: "short" }, { ...purchase, productId: "unrelated_product" }]) harness.receive?.(value);
    expect(onPurchase).not.toHaveBeenCalled();
    expect(onResult).not.toHaveBeenCalled();
  });

  it("does not forward a purchase when the native result reports cancellation", () => {
    const onPurchase = vi.fn(); const onResult = vi.fn();
    subscribeToBillingPurchases(onPurchase, onResult);
    harness.receive?.({ ...purchase, responseCode: 1 });
    expect(onPurchase).not.toHaveBeenCalled();
    expect(onResult).toHaveBeenCalledWith({ kind: "cancelled", responseCode: 1 });
  });

  it("keeps restore results subject to the same purchase validation", async () => {
    harness.restore.mockResolvedValue([purchase, { ...purchase, purchaseState: 0 }, { ...purchase, productId: "unrelated_product" }]);
    expect(await restoreBillingPurchases()).toEqual([purchase]);
  });

  it("preserves subscription cleanup and no-op behavior outside Android", () => {
    const subscription = subscribeToBillingPurchases(vi.fn(), vi.fn());
    subscription?.remove(); expect(harness.remove).toHaveBeenCalledOnce();
    harness.platform.OS = "ios";
    expect(subscribeToBillingPurchases(vi.fn(), vi.fn())).toBeNull();
  });
});
