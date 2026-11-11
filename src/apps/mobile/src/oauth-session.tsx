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
import type { AccountBillingSummary } from "./billing-api";
import { createSessionBillingRequests } from "./billing-session-scope";
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
  readonly getSession: () => OAuthSession | null;
  readonly invalidateSession: (expected: OAuthSession) => void;
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
  const currentSession = useRef<OAuthSession | null>(null);
  const researchAccess = useRef<"loading" | "ready" | "invalid">("loading");
  const storageQueue = useRef<Promise<void>>(Promise.resolve());
  const persistSession = useCallback((next: OAuthSession | null) => {
    const write = storageQueue.current.then(() => next ? saveOAuthSession(next) : clearOAuthSession());
    storageQueue.current = write.catch(() => undefined);
    return write;
  }, []);
  const canStart = Boolean(apiBaseUrl && isSafeOAuthApiBaseUrl(apiBaseUrl) && returnUri);
  const [billingRequests] = useState(() => createSessionBillingRequests(setBillingSummary));
  const replaceSession = useCallback((next: OAuthSession | null) => {
    // Invalidate synchronously, before React renders the new account or signed-out state.
    billingRequests.bind(apiBaseUrl, next);
    currentSession.current = next;
    researchAccess.current = "ready";
    setSession(next);
  }, [apiBaseUrl, billingRequests]);
  const getSession = useCallback(() => {
    if (researchAccess.current !== "ready") throw new Error("Research sign-in is not ready.");
    return currentSession.current;
  }, []);
  const invalidateSession = useCallback((expected: OAuthSession) => {
    if (currentSession.current !== expected) return;
    replaceSession(null);
    researchAccess.current = "invalid";
    setStatus("error");
    setErrorNotice("登录已过期，请重新登录。 / Sign-in expired. Please sign in again.");
    void persistSession(null).catch(() => undefined);
  }, [persistSession, replaceSession]);

  useEffect(() => {
    billingRequests.resume();
    return () => billingRequests.suspend();
  }, [billingRequests]);

  const refreshBilling = useCallback(async () => {
    await billingRequests.refresh(apiBaseUrl, session);
  }, [apiBaseUrl, billingRequests, session]);

  const verifyPurchase = useCallback(async (purchaseToken: string, productId: BillingProductId) => {
    await billingRequests.verify(apiBaseUrl, session, purchaseToken, productId);
  }, [apiBaseUrl, billingRequests, session]);

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
      await persistSession(next);
      replaceSession(next);
      setErrorNotice("");
      setStatus("signed_in");
    } catch {
      setErrorNotice(safeError("en"));
      setStatus("error");
    }
  }, [apiBaseUrl, persistSession, replaceSession, returnUri]);

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
        replaceSession(stored);
        setStatus(stored ? "signed_in" : "signed_out");
      } catch {
        if (!active) return;
        researchAccess.current = "invalid";
        setErrorNotice(safeError("en"));
        setStatus("error");
      }
      initialized = true;
      const initialUrl = await Linking.getInitialURL();
      const urls = [...pendingUrls, ...(initialUrl ? [initialUrl] : [])];
      pendingUrls.length = 0;
      for (const url of urls) if (active) await processCallback(url);
    })();
    return () => { active = false; subscription.remove(); };
  }, [processCallback, replaceSession]);

  useEffect(() => {
    if (!session) return;
    // bind() already clears on account change; a failed refresh must not erase a later success.
    void refreshBilling().catch(() => undefined);
  }, [refreshBilling, session]);

  const startLogin = useCallback(async (provider: OAuthProvider) => {
    if (!apiBaseUrl || !returnUri) throw new Error("OAuth is not configured safely.");
    await Linking.openURL(oauthStartUrl(apiBaseUrl, provider, returnUri));
  }, [apiBaseUrl, returnUri]);

  const logout = useCallback(async () => {
    const current = session;
    replaceSession(null);
    setStatus("signed_out");
    const cleared = persistSession(null).catch(() => undefined);
    try {
      if (current && apiBaseUrl) await logoutOAuthSession(apiBaseUrl, current.access_token);
    } finally {
      await cleared;
    }
  }, [apiBaseUrl, persistSession, replaceSession, session]);

  const deleteAccount = useCallback(async () => {
    const current = session;
    replaceSession(null);
    setStatus("signed_out");
    const cleared = persistSession(null).catch(() => undefined);
    try {
      if (current && apiBaseUrl) await deleteOAuthAccount(apiBaseUrl, current.access_token);
    } finally {
      await cleared;
    }
  }, [apiBaseUrl, persistSession, replaceSession, session]);

  const value = useMemo<OAuthSessionContextValue>(() => ({ session, billingSummary, status, errorNotice, canStart, startLogin, logout, deleteAccount, refreshBilling, verifyPurchase, getSession, invalidateSession, dismissError: () => { setErrorNotice(""); setStatus(session ? "signed_in" : "signed_out"); } }), [billingSummary, canStart, deleteAccount, errorNotice, getSession, invalidateSession, logout, refreshBilling, session, startLogin, status, verifyPurchase]);
  return <OAuthContext.Provider value={value}>{children}</OAuthContext.Provider>;
}

export function useOAuthSession(): OAuthSessionContextValue {
  const value = useContext(OAuthContext);
  if (!value) throw new Error("OAuth session provider is missing");
  return value;
}
