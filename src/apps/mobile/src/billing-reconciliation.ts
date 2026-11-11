import type { BillingPurchase } from "./billing-client";

export interface BillingReconciliationResult {
  readonly verified: number;
  readonly failed: number;
  readonly pending: number;
  readonly ignored: number;
}

// Successful event deduplication and in-flight work are different states. A
// restore must recheck a successful token: renewals can keep the same token.
const inFlightByLedger = new WeakMap<Set<string>, Map<string, Promise<boolean>>>();

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
  options: { readonly refreshVerified?: boolean } = {},
): Promise<BillingReconciliationResult> {
  let verified = 0;
  let failed = 0;
  let pending = 0;
  let ignored = 0;
  const seen = new Set<string>();
  let inFlight = inFlightByLedger.get(processed);
  if (!inFlight) {
    inFlight = new Map();
    inFlightByLedger.set(processed, inFlight);
  }

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
    const existing = inFlight.get(key);
    if (existing) {
      // Wait before callers read the account summary, otherwise a concurrent
      // restore can fetch the old entitlement while verification is running.
      if (await existing) ignored += 1;
      else failed += 1;
      continue;
    }
    if (!options.refreshVerified && processed.has(key)) {
      ignored += 1;
      continue;
    }
    const task = (async () => {
      try {
        await verify(purchase);
        processed.add(key);
        return true;
      } catch {
        return false;
      }
    })();
    inFlight.set(key, task);
    try {
      if (await task) verified += 1;
      else failed += 1;
    } finally {
      if (inFlight.get(key) === task) inFlight.delete(key);
    }
  }

  return { verified, failed, pending, ignored };
}
