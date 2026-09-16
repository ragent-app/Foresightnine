import { isBillingProductId, BILLING_BASE_PLAN_ID, type BillingProductId } from "./billing-catalog";

export interface AccountBillingSummary {
  readonly plan: "free" | "pro" | "max";
  readonly entitlement_status: "active" | "pending" | "grace_period" | "on_hold" | "canceled" | "expired";
  readonly product_id: string | null;
  readonly base_plan_id: string | null;
  readonly quota_limit: number;
  readonly quota_consumed: number;
  readonly quota_reserved: number;
  readonly quota_remaining: number;
  readonly quota_resets_at: string | null;
  readonly provider_model: string;
}

export interface VerifiedBillingResult {
  readonly summary: AccountBillingSummary;
  readonly purchase_state?: string;
}

function safeApiBaseUrl(value: string): boolean {
  try {
    const url = new URL(value);
    const loopback = url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
    return (url.protocol === "https:" || loopback) && Boolean(url.hostname) && !url.username && !url.password && !url.search && !url.hash;
  } catch { return false; }
}

function parseSummary(payload: unknown): AccountBillingSummary {
  if (!payload || typeof payload !== "object") throw new Error("Account summary response is invalid.");
  const value = payload as Record<string, unknown>;
  const required = ["plan", "entitlement_status", "product_id", "base_plan_id", "quota_limit", "quota_consumed", "quota_reserved", "quota_remaining", "quota_resets_at", "provider_model"];
  for (const key of required) if (!(key in value)) throw new Error("Account summary response is invalid.");
  if (value.plan !== "free" && value.plan !== "pro" && value.plan !== "max") throw new Error("Account summary response is invalid.");
  const statuses = ["active", "pending", "grace_period", "on_hold", "canceled", "expired"] as const;
  const status = statuses.find((item) => value.entitlement_status === item);
  if (!status || typeof value.provider_model !== "string" || !value.provider_model) throw new Error("Account summary response is invalid.");
  if ((value.product_id !== null && typeof value.product_id !== "string") || (value.base_plan_id !== null && typeof value.base_plan_id !== "string") || (value.quota_resets_at !== null && typeof value.quota_resets_at !== "string")) throw new Error("Account summary response is invalid.");
  const quota = {} as Record<string, number>;
  for (const key of ["quota_limit", "quota_consumed", "quota_reserved", "quota_remaining"]) {
    if (typeof value[key] !== "number" || !Number.isSafeInteger(value[key]) || value[key] < 0) throw new Error("Account summary response is invalid.");
    quota[key] = value[key] as number;
  }
  return { plan: value.plan, entitlement_status: status, product_id: value.product_id, base_plan_id: value.base_plan_id, quota_limit: quota.quota_limit!, quota_consumed: quota.quota_consumed!, quota_reserved: quota.quota_reserved!, quota_remaining: quota.quota_remaining!, quota_resets_at: value.quota_resets_at, provider_model: value.provider_model };
}

export async function fetchAccountBillingSummary(apiBaseUrl: string, accessToken: string, fetcher: typeof fetch = fetch, signal?: AbortSignal): Promise<AccountBillingSummary> {
  if (!safeApiBaseUrl(apiBaseUrl) || !/^\S{32,256}$/.test(accessToken)) throw new Error("OAuth session is invalid.");
  const response = await fetcher(new URL("v1/account", `${apiBaseUrl.replace(/\/+$/, "")}/`), { method: "GET", headers: { Accept: "application/json", Authorization: `Bearer ${accessToken}` }, credentials: "omit", ...(signal ? { signal } : {}) });
  if (!response.ok) throw new Error("Account summary request failed.");
  return parseSummary(await response.json());
}

export async function verifyGooglePlayPurchase(apiBaseUrl: string, accessToken: string, purchaseToken: string, productId: BillingProductId, fetcher: typeof fetch = fetch): Promise<VerifiedBillingResult> {
  if (!safeApiBaseUrl(apiBaseUrl) || !/^\S{32,256}$/.test(accessToken) || purchaseToken.length < 20 || !isBillingProductId(productId)) throw new Error("Billing request is invalid.");
  const response = await fetcher(new URL("v1/billing/google-play/verify", `${apiBaseUrl.replace(/\/+$/, "")}/`), { method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` }, body: JSON.stringify({ purchase_token: purchaseToken, product_id: productId, base_plan_id: BILLING_BASE_PLAN_ID }), credentials: "omit" });
  if (!response.ok) throw new Error("Google Play purchase verification failed.");
  const payload = await response.json() as Record<string, unknown>;
  return { summary: parseSummary(payload), ...(typeof payload.purchase_state === "string" ? { purchase_state: payload.purchase_state } : {}) };
}
