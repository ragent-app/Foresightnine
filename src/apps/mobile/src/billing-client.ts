import { NativeEventEmitter, NativeModules, Platform, type EmitterSubscription } from "react-native";
import { BILLING_BASE_PLAN_ID, BILLING_PRODUCT_IDS, isBillingProductId, type BillingProductId } from "./billing-catalog";

export interface BillingProduct {
  readonly productId: BillingProductId;
  readonly basePlanId: string;
  readonly offerToken: string;
  readonly formattedPrice: string | null;
  readonly currencyCode: string | null;
  readonly priceAmountMicros: number;
  readonly billingPeriod: string | null;
}

export interface BillingPurchase {
  readonly purchaseToken: string;
  readonly productId: BillingProductId | null;
  readonly basePlanId: string | null;
  readonly purchaseState: number;
  readonly isAcknowledged: boolean;
  readonly isAutoRenewing: boolean;
}

export interface BillingFlowResult {
  readonly kind: "cancelled" | "error";
  readonly responseCode: number;
}

function purchaseUpdate(value: unknown): { readonly kind: "purchase"; readonly purchase: BillingPurchase } | BillingFlowResult | null {
  if (!value || typeof value !== "object") return null;
  const item = value as Partial<BillingPurchase> & { readonly responseCode?: unknown };
  // A cancelled/failed native result has no purchase token. Do not drop it or
  // copy provider debug text (which may contain internal details) into the UI.
  if (typeof item.responseCode === "number" && Number.isInteger(item.responseCode)) {
    return { kind: item.responseCode === 1 ? "cancelled" : "error", responseCode: item.responseCode };
  }
  if (typeof item.purchaseToken !== "string" || item.purchaseToken.length < 20 || item.purchaseToken.length > 4096) return null;
  if (item.productId !== null && item.productId !== undefined && !isBillingProductId(item.productId)) return null;
  if (item.purchaseState !== 1 && item.purchaseState !== 2) return { kind: "error", responseCode: 0 };
  return {
    kind: "purchase",
    purchase: {
      purchaseToken: item.purchaseToken,
      productId: item.productId ?? null,
      basePlanId: typeof item.basePlanId === "string" ? item.basePlanId : null,
      purchaseState: item.purchaseState,
      isAcknowledged: item.isAcknowledged === true,
      isAutoRenewing: item.isAutoRenewing === true,
    },
  };
}

interface NativeBillingModule {
  queryProducts(productIds: readonly string[]): Promise<readonly BillingProduct[]>;
  launchPurchase(productId: string, offerToken: string, obfuscatedAccountId: string): Promise<boolean>;
  restorePurchases(): Promise<readonly BillingPurchase[]>;
  addListener?: (eventName: string) => void;
}

function nativeBilling(): NativeBillingModule | null {
  if (Platform.OS !== "android") return null;
  return (NativeModules.ForesightNineBilling as NativeBillingModule | undefined) ?? null;
}

export function billingAvailable(): boolean { return nativeBilling() !== null; }

export async function queryBillingProducts(): Promise<readonly BillingProduct[]> {
  const native = nativeBilling();
  if (!native) return [];
  const values = await native.queryProducts(BILLING_PRODUCT_IDS);
  return values.filter((item) => isBillingProductId(item.productId) && item.basePlanId === BILLING_BASE_PLAN_ID && Boolean(item.offerToken));
}

export async function launchBillingPurchase(product: BillingProduct, obfuscatedAccountId: string): Promise<boolean> {
  const native = nativeBilling();
  if (!native || !isBillingProductId(product.productId) || product.basePlanId !== BILLING_BASE_PLAN_ID) throw new Error("Google Play Billing is unavailable.");
  return native.launchPurchase(product.productId, product.offerToken, obfuscatedAccountId);
}

export async function restoreBillingPurchases(): Promise<readonly BillingPurchase[]> {
  const native = nativeBilling();
  if (!native) return [];
  const values = await native.restorePurchases();
  if (!Array.isArray(values)) throw new Error("Google Play purchase response is invalid.");
  return values.flatMap((value) => {
    const event = purchaseUpdate(value);
    return event?.kind === "purchase" ? [event.purchase] : [];
  });
}

export function subscribeToBillingPurchases(listener: (purchase: BillingPurchase) => void, onResult?: (result: BillingFlowResult) => void): EmitterSubscription | null {
  const native = nativeBilling();
  if (!native) return null;
  const emitter = new NativeEventEmitter(NativeModules.ForesightNineBilling);
  return emitter.addListener("foresightnineBillingPurchaseUpdated", (value: unknown) => {
    const event = purchaseUpdate(value);
    if (!event) return;
    if (event.kind === "purchase") listener(event.purchase);
    else onResult?.(event);
  });
}
