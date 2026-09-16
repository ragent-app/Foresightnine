import { useCallback, useEffect, useRef, useState } from "react";
import * as Crypto from "expo-crypto";
import { Linking, Pressable, Text, View } from "react-native";
import { useMobileStore } from "../src/app-store";
import { billingAvailable, launchBillingPurchase, queryBillingProducts, restoreBillingPurchases, subscribeToBillingPurchases, type BillingProduct, type BillingPurchase } from "../src/billing-client";
import { useOAuthSession } from "../src/oauth-session";
import { t } from "../src/i18n";
import { BottomNav, Button, Card, Header, Screen, SocialLoginPanel, Status, styles } from "../src/ui";

export default function ConsoleScreen(): React.JSX.Element {
  const { state, marketDataState } = useMobileStore();
  const oauth = useOAuthSession();
  const [products, setProducts] = useState<readonly BillingProduct[]>([]);
  const [billingNotice, setBillingNotice] = useState("");
  const [billingBusy, setBillingBusy] = useState(false);
  const pendingPurchases = useRef<BillingPurchase[]>([]);
  const verifiedTokens = useRef(new Set<string>());
  const processPurchase = useCallback(async (purchase: BillingPurchase) => {
    if (purchase.purchaseState !== 1 || !purchase.productId) return;
    if (!oauth.session) {
      pendingPurchases.current = [...pendingPurchases.current, purchase];
      setBillingNotice(state.language === "en" ? "Sign in before completing purchase verification." : "请先登录，再完成购买验证。");
      return;
    }
    if (verifiedTokens.current.has(purchase.purchaseToken)) return;
    verifiedTokens.current.add(purchase.purchaseToken);
    try {
      await oauth.verifyPurchase(purchase.purchaseToken, purchase.productId);
      setBillingNotice(state.language === "en" ? "Purchase verified by the server." : "购买已由服务端验证。");
    } catch {
      verifiedTokens.current.delete(purchase.purchaseToken);
      setBillingNotice(state.language === "en" ? "Purchase verification failed; no entitlement was granted." : "购买验证失败，未授予会员权益。");
    }
  }, [oauth, state.language]);
  useEffect(() => {
    if (!billingAvailable()) return;
    let active = true;
    void queryBillingProducts().then((items) => { if (active) setProducts(items); }).catch(() => { if (active) setBillingNotice(state.language === "en" ? "Google Play products are unavailable." : "Google Play 商品暂不可用。"); });
    const subscription = subscribeToBillingPurchases((purchase) => { void processPurchase(purchase); });
    return () => { active = false; subscription?.remove(); };
  }, [processPurchase, state.language]);
  useEffect(() => {
    if (!oauth.session || pendingPurchases.current.length === 0) return;
    const pending = pendingPurchases.current;
    pendingPurchases.current = [];
    for (const purchase of pending) void processPurchase(purchase);
  }, [oauth.session, processPurchase]);
  const restore = useCallback(async () => {
    if (!oauth.session) {
      setBillingNotice(state.language === "en" ? "Sign in to restore purchases." : "请先登录，再恢复购买。");
      return;
    }
    setBillingBusy(true);
    try {
      const purchases = await restoreBillingPurchases();
      for (const purchase of purchases) await processPurchase(purchase);
      await oauth.refreshBilling();
      setBillingNotice(state.language === "en" ? `${purchases.length} purchase record(s) checked.` : `已检查 ${purchases.length} 条购买记录。`);
    } catch {
      setBillingNotice(state.language === "en" ? "Purchase restore failed." : "恢复购买失败。");
    } finally { setBillingBusy(false); }
  }, [oauth, processPurchase, state.language]);
  const buy = useCallback(async (product: BillingProduct) => {
    if (!oauth.session) {
      setBillingNotice(state.language === "en" ? "Sign in before buying a subscription." : "购买订阅前请先登录。");
      return;
    }
    setBillingBusy(true);
    try {
      const obfuscatedAccountId = await Crypto.digestStringAsync(Crypto.CryptoDigestAlgorithm.SHA256, oauth.session.account.account_id);
      await launchBillingPurchase(product, obfuscatedAccountId);
      setBillingNotice(state.language === "en" ? "Google Play purchase flow opened." : "已打开 Google Play 购买流程。");
    } catch {
      setBillingNotice(state.language === "en" ? "Google Play purchase flow could not start." : "无法启动 Google Play 购买流程。");
    } finally { setBillingBusy(false); }
  }, [oauth.session, state.language]);
  const billingPlan = oauth.billingSummary?.plan === "max" ? "Max" : oauth.billingSummary?.plan === "pro" ? "Pro" : "Free";
  return <Screen><Header title={t(state.language, "consoleTitle")} subtitle={t(state.language, "consoleLead")} />
    <SocialLoginPanel />
    <Card><Text style={styles.heading}>{t(state.language, "providerRouting")}</Text><Status tone="good">{t(state.language, "configured")}</Status><Text style={styles.strong}>{t(state.language, "openaiCompatibleServer")}</Text><Text style={styles.body}>{t(state.language, "openaiCompatibleServerBody")}</Text></Card>
    <Card tone="soft"><Text style={styles.heading}>{t(state.language, "personalProvider")}</Text><Text style={styles.body}>{t(state.language, "personalProviderBody")}</Text></Card>
    <Card><Text style={styles.heading}>{t(state.language, "clientFm")}</Text><Status tone={marketDataState.status === "error" ? "warning" : marketDataState.status === "ready" ? "good" : "neutral"}>{marketDataState.mode} · {marketDataState.status}</Status><Text style={styles.body}>{t(state.language, "cutoffFixture")}</Text></Card>
    <Card tone="soft"><Text style={styles.heading}>{t(state.language, "subscription")}</Text>{oauth.billingSummary ? <><Status tone={oauth.billingSummary.entitlement_status === "active" ? "good" : "warning"}>{billingPlan} · {oauth.billingSummary.entitlement_status}</Status><Text style={styles.strong}>{oauth.billingSummary.quota_remaining.toLocaleString()} / {oauth.billingSummary.quota_limit.toLocaleString()} tokens</Text><Text style={styles.muted}>{oauth.billingSummary.provider_model}</Text></> : <Text style={styles.body}>{t(state.language, "subscriptionBody")}</Text>}{billingAvailable() ? <><View style={styles.stack}>{products.map((product) => <View key={`${product.productId}:${product.basePlanId}`} style={styles.listRow}><Text style={styles.strong}>{product.productId === "foresightnine_pro" ? "Pro" : "Max"} · {product.formattedPrice ?? "—"} / month</Text><Button variant="secondary" disabled={billingBusy || !oauth.session} onPress={() => { void buy(product); }} label={state.language === "en" ? "Buy subscription" : "购买订阅"}>{state.language === "en" ? "Buy" : "购买"}</Button></View>)}</View><Button variant="quiet" disabled={billingBusy || !oauth.session} onPress={() => { void restore(); }} label={state.language === "en" ? "Restore purchases" : "恢复购买"}>{state.language === "en" ? "Restore purchases" : "恢复购买"}</Button></> : <Text style={styles.muted}>{state.language === "en" ? "Google Play Billing is available only in the Android native build." : "Google Play Billing 仅在 Android 原生构建中可用。"}</Text>}{billingNotice ? <Text style={styles.muted}>{billingNotice}</Text> : null}</Card>
    <Card><Text style={styles.heading}>{t(state.language, "privacyPolicy")}</Text><Text style={styles.body}>{t(state.language, "privacyPolicyBody")}</Text><Pressable accessibilityRole="link" accessibilityLabel={t(state.language, "privacyPolicy")} onPress={() => { void Linking.openURL("https://app.foresightnine.com/privacy.html"); }}><Text style={styles.strong}>{t(state.language, "openPrivacyPolicy")}</Text></Pressable></Card>
    <Card><Text style={styles.heading}>{t(state.language, "researchBasis")}</Text><Text style={styles.strong}>{t(state.language, "researchLoop")}</Text><Text style={styles.body}>{t(state.language, "researchLoopBody")}</Text><Text style={styles.strong}>{t(state.language, "clientFm")}</Text><Text style={styles.body}>{t(state.language, "clientFmBody")}</Text></Card>
    <Card tone="warning"><Text style={styles.heading}>{t(state.language, "releaseGates")}</Text><Text style={styles.muted}>{t(state.language, "releaseGatesBody")}</Text></Card><BottomNav />
  </Screen>;
}
