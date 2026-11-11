import { fetchAccountBillingSummary, verifyGooglePlayPurchase, type AccountBillingSummary } from "./billing-api";
import type { BillingProductId } from "./billing-catalog";
import type { OAuthSession } from "./oauth-client";

export const BILLING_REQUEST_TIMEOUT_MS = 25_000;

interface BillingRequests {
  readonly summary: typeof fetchAccountBillingSummary;
  readonly verify: typeof verifyGooglePlayPurchase;
}

/** Serializes reads/writes within a login and prevents late results crossing login boundaries. */
export function createSessionBillingRequests(
  publish: (summary: AccountBillingSummary | null) => void,
  requests: BillingRequests = { summary: fetchAccountBillingSummary, verify: verifyGooglePlayPurchase },
) {
  let current: { api: string | null; session: OAuthSession | null } = { api: null, session: null };
  let generation = 0;
  let active = true;
  let queue: Promise<void> = Promise.resolve();

  function invalidate() { generation += 1; queue = Promise.resolve(); }
  function bind(api: string | null, session: OAuthSession | null) {
    invalidate();
    current = { api, session };
    if (active) publish(null);
  }

  async function run(api: string | null, session: OAuthSession | null, request: (api: string, session: OAuthSession, signal: AbortSignal) => Promise<AccountBillingSummary>) {
    const expected = generation;
    const isCurrent = () => active && generation === expected && current.api === api && current.session === session;
    if (!api || !session || !isCurrent()) throw new Error("An active account session is required.");
    const task = queue.then(async () => {
      // A queued request must not authenticate with an account that has since signed out.
      if (!isCurrent()) throw new Error("Account session changed.");
      const controller = new AbortController();
      let timer: ReturnType<typeof setTimeout> | undefined;
      const timeout = new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => {
          reject(new Error("Account billing request timed out."));
          controller.abort();
        }, BILLING_REQUEST_TIMEOUT_MS);
      });
      try {
        const summary = await Promise.race([request(api, session, controller.signal), timeout]);
        if (!isCurrent()) throw new Error("Account session changed.");
        publish(summary);
      } finally { clearTimeout(timer); }
    });
    // A failed request must not poison subsequent retries. Its caller still receives the error.
    queue = task.catch(() => undefined);
    await task;
  }

  return {
    bind,
    suspend() { active = false; invalidate(); },
    resume() { active = true; },
    async refresh(api: string | null, session: OAuthSession | null) {
      // Anonymous/stale callbacks do not clear a newly signed-in account's summary.
      if (!api || !session) return;
      await run(api, session, (base, owner, signal) => requests.summary(base, owner.access_token, undefined, signal));
    },
    async verify(api: string | null, session: OAuthSession | null, token: string, product: BillingProductId) {
      await run(api, session, async (base, owner, signal) => (await requests.verify(base, owner.access_token, token, product, undefined, signal)).summary);
    },
  };
}
