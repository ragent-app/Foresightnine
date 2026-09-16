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
  return values.filter((item) => typeof item.purchaseToken === "string" && item.purchaseToken.length >= 20 && (item.productId === null || isBillingProductId(item.productId)));
}

export function subscribeToBillingPurchases(listener: (purchase: BillingPurchase) => void): EmitterSubscription | null {
  const native = nativeBilling();
  if (!native) return null;
  const emitter = new NativeEventEmitter(NativeModules.ForesightNineBilling);
  return emitter.addListener("foresightnineBillingPurchaseUpdated", (value: unknown) => {
    if (!value || typeof value !== "object") return;
    const item = value as Partial<BillingPurchase>;
    if (typeof item.purchaseToken !== "string" || item.purchaseToken.length < 20) return;
    if (item.productId !== null && item.productId !== undefined && !isBillingProductId(item.productId)) return;
    listener({
      purchaseToken: item.purchaseToken,
      productId: item.productId ?? null,
      basePlanId: item.basePlanId ?? null,
      purchaseState: typeof item.purchaseState === "number" ? item.purchaseState : 0,
      isAcknowledged: item.isAcknowledged === true,
      isAutoRenewing: item.isAutoRenewing === true,
    });
  });
}
