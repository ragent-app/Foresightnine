import type { BillingPurchase } from "./billing-client";

export interface BillingReconciliationResult {
  readonly verified: number;
  readonly failed: number;
  readonly pending: number;
  readonly ignored: number;
}

/**
 * Reconcile a Play purchase snapshot without letting one bad token prevent the
 * remaining subscriptions from reaching the server. The processed set is
 * scoped by the authenticated ForesightNine session, so a logout/login switch
 * cannot accidentally suppress verification for the next account.
 */
export async function reconcileBillingPurchases(
  purchases: readonly BillingPurchase[],
  sessionScope: string,
  processed: Set<string>,
  verify: (purchase: BillingPurchase) => Promise<void>,
): Promise<BillingReconciliationResult> {
  let verified = 0;
  let failed = 0;
  let pending = 0;
  let ignored = 0;
  const seen = new Set<string>();

  for (const purchase of purchases) {
    if (seen.has(purchase.purchaseToken)) {
      ignored += 1;
      continue;
    }
    seen.add(purchase.purchaseToken);
    if (purchase.purchaseState !== 1) {
      pending += 1;
      continue;
    }
    if (!purchase.productId) {
      ignored += 1;
      continue;
    }
    const key = `${sessionScope}:${purchase.purchaseToken}`;
    if (processed.has(key)) {
      ignored += 1;
      continue;
    }
    processed.add(key);
    try {
      await verify(purchase);
      verified += 1;
    } catch {
      processed.delete(key);
      failed += 1;
    }
  }

  return { verified, failed, pending, ignored };
}
