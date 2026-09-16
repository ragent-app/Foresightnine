export const BILLING_PRODUCT_IDS = ["foresightnine_pro", "foresightnine_max"] as const;
export type BillingProductId = typeof BILLING_PRODUCT_IDS[number];

export const BILLING_BASE_PLAN_ID = "monthly" as const;

export const BILLING_PLAN_LIMITS: Readonly<Record<BillingProductId, number>> = {
  foresightnine_pro: 30_000_000,
  foresightnine_max: 500_000_000,
};

export function isBillingProductId(value: unknown): value is BillingProductId {
  return value === "foresightnine_pro" || value === "foresightnine_max";
}
