import { useSyncExternalStore } from "react";

export type AuthMode = "oauth" | "local_mock";

export interface Session {
  account: { account_id: string; email: string; created_at: string };
  session_id: string;
  access_token: string;
  expires_at: string;
  auth_mode: AuthMode;
  provider?: "google" | "github";
}

export type SessionChangeReason = "signed_in" | "signed_out" | "expired" | "deleted";

export const SESSION_STORAGE_KEY = "foresightnine.web.session.v2";
export const SESSION_CHANGED_EVENT = "foresightnine:session-changed";

const TOKEN_PATTERN = /^\S{32,256}$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value);
  return actual.length === keys.length && keys.every((key) => Object.prototype.hasOwnProperty.call(value, key));
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

export function parseSession(payload: unknown): Session {
  const invalid = () => new Error("invalid_session");
  if (!isRecord(payload)) throw invalid();
  const base = ["account", "session_id", "access_token", "expires_at", "auth_mode"] as const;
  const authMode = payload.auth_mode;
  if (authMode !== "oauth" && authMode !== "local_mock") throw invalid();
  const keys = authMode === "oauth" ? [...base, "provider"] : [...base];
  if (!hasExactKeys(payload, keys)) throw invalid();

  const account = payload.account;
  if (!isRecord(account) || !hasExactKeys(account, ["account_id", "email", "created_at"])) throw invalid();
  const { account_id, email, created_at } = account;
  if (!nonEmptyString(account_id) || !nonEmptyString(email) || !nonEmptyString(created_at)) throw invalid();

  const { session_id, access_token, expires_at } = payload;
  if (!nonEmptyString(session_id)) throw invalid();
  if (typeof access_token !== "string" || !TOKEN_PATTERN.test(access_token)) throw invalid();
  if (typeof expires_at !== "string" || Number.isNaN(Date.parse(expires_at))) throw invalid();

  const session: Session = {
    account: { account_id, email, created_at },
    session_id,
    access_token,
    expires_at,
    auth_mode: authMode,
  };
  if (authMode === "oauth") {
    const provider = payload.provider;
    if (provider !== "google" && provider !== "github") throw invalid();
    session.provider = provider;
  }
  return session;
}

function readRaw(): string | null {
  try {
    return sessionStorage.getItem(SESSION_STORAGE_KEY);
  } catch {
    return null;
  }
}

function removeRaw(): void {
  try {
    sessionStorage.removeItem(SESSION_STORAGE_KEY);
  } catch {
    // Storage unavailable: behaves as no session.
  }
}

function parseRaw(raw: string | null): Session | null {
  if (raw === null) return null;
  try {
    const session = parseSession(JSON.parse(raw));
    if (Date.parse(session.expires_at) <= Date.now()) {
      removeRaw();
      return null;
    }
    return session;
  } catch {
    removeRaw();
    return null;
  }
}

export function readSession(): Session | null {
  return parseRaw(readRaw());
}

function emit(reason: SessionChangeReason): void {
  window.dispatchEvent(new CustomEvent(SESSION_CHANGED_EVENT, { detail: { reason } }));
}

export function saveSession(session: Session): void {
  try {
    sessionStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(session));
  } catch {
    // Storage unavailable: the session cannot be persisted.
  }
  emit("signed_in");
}

export function clearSession(reason: "signed_out" | "expired" | "deleted"): void {
  removeRaw();
  emit(reason);
}

let cachedRaw: string | null = null;
let cachedSession: Session | null = null;

function getSnapshot(): Session | null {
  const raw = readRaw();
  if (raw === cachedRaw) {
    if (cachedSession && Date.parse(cachedSession.expires_at) <= Date.now()) {
      removeRaw();
      cachedRaw = null;
      cachedSession = null;
    }
    return cachedSession;
  }
  cachedRaw = raw;
  cachedSession = parseRaw(raw);
  if (cachedSession === null) cachedRaw = readRaw();
  return cachedSession;
}

function subscribe(onChange: () => void): () => void {
  window.addEventListener(SESSION_CHANGED_EVENT, onChange);
  window.addEventListener("storage", onChange);
  return () => {
    window.removeEventListener(SESSION_CHANGED_EVENT, onChange);
    window.removeEventListener("storage", onChange);
  };
}

export function useSession(): Session | null {
  return useSyncExternalStore(subscribe, getSnapshot, () => null);
}
