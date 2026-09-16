import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type PropsWithChildren } from "react";
import { Linking } from "react-native";
import { configuredApiBaseUrl } from "./runtime-config";
import {
  clearOAuthSession,
  configuredOAuthReturnUri,
  deleteOAuthAccount,
  exchangeOAuthTicket,
  isSafeOAuthApiBaseUrl,
  logoutOAuthSession,
  oauthStartUrl,
  parseOAuthCallbackUrl,
  readOAuthSession,
  saveOAuthSession,
  type OAuthProvider,
  type OAuthSession,
} from "./oauth-client";
import { fetchAccountBillingSummary, verifyGooglePlayPurchase, type AccountBillingSummary } from "./billing-api";
import type { BillingProductId } from "./billing-catalog";

export type OAuthSessionStatus = "loading" | "signed_out" | "signed_in" | "error";

export interface OAuthSessionContextValue {
  readonly session: OAuthSession | null;
  readonly billingSummary: AccountBillingSummary | null;
  readonly status: OAuthSessionStatus;
  readonly errorNotice: string;
  readonly canStart: boolean;
  readonly startLogin: (provider: OAuthProvider) => Promise<void>;
  readonly logout: () => Promise<void>;
  readonly deleteAccount: () => Promise<void>;
  readonly dismissError: () => void;
  readonly refreshBilling: () => Promise<void>;
  readonly verifyPurchase: (purchaseToken: string, productId: BillingProductId) => Promise<void>;
}

const OAuthContext = createContext<OAuthSessionContextValue | null>(null);

function safeError(language: "zh" | "en"): string {
  return language === "en" ? "Secure sign-in could not be completed." : "安全登录未能完成。";
}

export function OAuthSessionProvider({ children }: PropsWithChildren): React.JSX.Element {
  const apiBaseUrl = configuredApiBaseUrl();
  const returnUri = configuredOAuthReturnUri();
  const [session, setSession] = useState<OAuthSession | null>(null);
  const [billingSummary, setBillingSummary] = useState<AccountBillingSummary | null>(null);
  const [status, setStatus] = useState<OAuthSessionStatus>("loading");
  const [errorNotice, setErrorNotice] = useState("");
  const exchangedTickets = useRef(new Set<string>());
  const canStart = Boolean(apiBaseUrl && isSafeOAuthApiBaseUrl(apiBaseUrl) && returnUri);

  const refreshBilling = useCallback(async () => {
    if (!apiBaseUrl || !session) {
      setBillingSummary(null);
      return;
    }
    setBillingSummary(await fetchAccountBillingSummary(apiBaseUrl, session.access_token));
  }, [apiBaseUrl, session]);

  const verifyPurchase = useCallback(async (purchaseToken: string, productId: BillingProductId) => {
    if (!apiBaseUrl || !session) throw new Error("An account is required for purchase verification.");
    const result = await verifyGooglePlayPurchase(apiBaseUrl, session.access_token, purchaseToken, productId);
    setBillingSummary(result.summary);
  }, [apiBaseUrl, session]);

  const processCallback = useCallback(async (rawUrl: string) => {
    if (!returnUri || !apiBaseUrl) return;
    let result: ReturnType<typeof parseOAuthCallbackUrl>;
    try {
      result = parseOAuthCallbackUrl(rawUrl, returnUri);
    } catch {
      setErrorNotice(safeError("en"));
      setStatus("error");
      return;
    }
    if (result.kind === "none") return;
    if (result.kind === "error") {
      setErrorNotice(safeError("en"));
      setStatus("error");
      return;
    }
    const ticket = result.ticket;
    if (!ticket || exchangedTickets.current.has(ticket)) return;
    exchangedTickets.current.add(ticket);
    try {
      const next = await exchangeOAuthTicket(apiBaseUrl, ticket);
      await saveOAuthSession(next);
      setSession(next);
      setBillingSummary(null);
      setErrorNotice("");
      setStatus("signed_in");
    } catch {
      setErrorNotice(safeError("en"));
      setStatus("error");
    }
  }, [apiBaseUrl, returnUri]);

  useEffect(() => {
    let active = true;
    let initialized = false;
    const pendingUrls: string[] = [];
    const subscription = Linking.addEventListener("url", ({ url }) => {
      if (!initialized) pendingUrls.push(url);
      else void processCallback(url);
    });
    void (async () => {
      try {
        const stored = await readOAuthSession();
        if (!active) return;
        setSession(stored);
        setBillingSummary(null);
        setStatus(stored ? "signed_in" : "signed_out");
      } catch {
        if (!active) return;
        setStatus("signed_out");
      }
      initialized = true;
      const initialUrl = await Linking.getInitialURL();
      const urls = [...pendingUrls, ...(initialUrl ? [initialUrl] : [])];
      pendingUrls.length = 0;
      for (const url of urls) if (active) await processCallback(url);
    })();
    return () => { active = false; subscription.remove(); };
  }, [processCallback]);

  useEffect(() => {
    if (!session) {
      setBillingSummary(null);
      return;
    }
    let active = true;
    void refreshBilling().catch(() => { if (active) setBillingSummary(null); });
    return () => { active = false; };
  }, [refreshBilling, session]);

  const startLogin = useCallback(async (provider: OAuthProvider) => {
    if (!apiBaseUrl || !returnUri) throw new Error("OAuth is not configured safely.");
    await Linking.openURL(oauthStartUrl(apiBaseUrl, provider, returnUri));
  }, [apiBaseUrl, returnUri]);

  const logout = useCallback(async () => {
    const current = session;
    setSession(null);
    setBillingSummary(null);
    setStatus("signed_out");
    try {
      if (current && apiBaseUrl) await logoutOAuthSession(apiBaseUrl, current.access_token);
    } finally {
      await clearOAuthSession().catch(() => undefined);
    }
  }, [apiBaseUrl, session]);

  const deleteAccount = useCallback(async () => {
    const current = session;
    setSession(null);
    setBillingSummary(null);
    setStatus("signed_out");
    try {
      if (current && apiBaseUrl) await deleteOAuthAccount(apiBaseUrl, current.access_token);
    } finally {
      await clearOAuthSession().catch(() => undefined);
    }
  }, [apiBaseUrl, session]);

  const value = useMemo<OAuthSessionContextValue>(() => ({ session, billingSummary, status, errorNotice, canStart, startLogin, logout, deleteAccount, refreshBilling, verifyPurchase, dismissError: () => { setErrorNotice(""); setStatus(session ? "signed_in" : "signed_out"); } }), [billingSummary, canStart, deleteAccount, errorNotice, logout, refreshBilling, session, startLogin, status, verifyPurchase]);
  return <OAuthContext.Provider value={value}>{children}</OAuthContext.Provider>;
}

export function useOAuthSession(): OAuthSessionContextValue {
  const value = useContext(OAuthContext);
  if (!value) throw new Error("OAuth session provider is missing");
  return value;
}
