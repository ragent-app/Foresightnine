import type { Session } from "../auth/session";
import { SESSION_STORAGE_KEY } from "../auth/session";

export const TOKEN = "t".repeat(40);

export function makeSession(overrides: Partial<Session> = {}): Session {
  return {
    account: { account_id: "acc_1", email: "a@example.com", created_at: "2026-01-01T00:00:00Z" },
    session_id: "sess_1",
    access_token: TOKEN,
    expires_at: new Date(Date.now() + 3600_000).toISOString(),
    auth_mode: "oauth",
    provider: "google",
    ...overrides,
  };
}

export function storeSession(session: Session = makeSession()): Session {
  sessionStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(session));
  return session;
}

export function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}
