export interface BillingAppStateSource {
  readonly currentState: string | null;
  addEventListener(event: "change", listener: (state: string) => void): { remove(): void };
}

/** Refresh after returning from Play or another app, not on every active event. */
export function subscribeToBillingForeground(source: BillingAppStateSource, onResume: () => void): { remove(): void } {
  let previous = source.currentState;
  let listening = true;
  const subscription = source.addEventListener("change", (next) => {
    if (!listening) return;
    const resumed = next === "active" && (previous === "background" || previous === "inactive");
    previous = next;
    if (resumed) onResume();
  });
  return { remove() { listening = false; subscription.remove(); } };
}
