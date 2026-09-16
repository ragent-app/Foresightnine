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

export type OAuthSessionStatus = "loading" | "signed_out" | "signed_in" | "error";

export interface OAuthSessionContextValue {
  readonly session: OAuthSession | null;
  readonly status: OAuthSessionStatus;
  readonly errorNotice: string;
  readonly canStart: boolean;
  readonly startLogin: (provider: OAuthProvider) => Promise<void>;
  readonly logout: () => Promise<void>;
  readonly deleteAccount: () => Promise<void>;
  readonly dismissError: () => void;
}

const OAuthContext = createContext<OAuthSessionContextValue | null>(null);

function safeError(language: "zh" | "en"): string {
  return language === "en" ? "Secure sign-in could not be completed." : "安全登录未能完成。";
}

export function OAuthSessionProvider({ children }: PropsWithChildren): React.JSX.Element {
  const apiBaseUrl = configuredApiBaseUrl();
  const returnUri = configuredOAuthReturnUri();
  const [session, setSession] = useState<OAuthSession | null>(null);
  const [status, setStatus] = useState<OAuthSessionStatus>("loading");
  const [errorNotice, setErrorNotice] = useState("");
  const exchangedTickets = useRef(new Set<string>());
  const canStart = Boolean(apiBaseUrl && isSafeOAuthApiBaseUrl(apiBaseUrl) && returnUri);

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

  const startLogin = useCallback(async (provider: OAuthProvider) => {
    if (!apiBaseUrl || !returnUri) throw new Error("OAuth is not configured safely.");
    await Linking.openURL(oauthStartUrl(apiBaseUrl, provider, returnUri));
  }, [apiBaseUrl, returnUri]);

  const logout = useCallback(async () => {
    const current = session;
    setSession(null);
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
    setStatus("signed_out");
    try {
      if (current && apiBaseUrl) await deleteOAuthAccount(apiBaseUrl, current.access_token);
    } finally {
      await clearOAuthSession().catch(() => undefined);
    }
  }, [apiBaseUrl, session]);

  const value = useMemo<OAuthSessionContextValue>(() => ({ session, status, errorNotice, canStart, startLogin, logout, deleteAccount, dismissError: () => { setErrorNotice(""); setStatus(session ? "signed_in" : "signed_out"); } }), [canStart, deleteAccount, errorNotice, logout, session, startLogin, status]);
  return <OAuthContext.Provider value={value}>{children}</OAuthContext.Provider>;
}

export function useOAuthSession(): OAuthSessionContextValue {
  const value = useContext(OAuthContext);
  if (!value) throw new Error("OAuth session provider is missing");
  return value;
}
