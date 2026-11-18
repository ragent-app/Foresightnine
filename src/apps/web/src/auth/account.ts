import { apiRequest } from "../api/http";
import { clearSession, readSession } from "./session";

export async function signOut(): Promise<void> {
  const session = readSession();
  if (session) {
    try {
      await apiRequest<void>(
        session.auth_mode === "local_mock" ? "/v1/auth/local/logout" : "/v1/auth/logout",
        { method: "POST" },
      );
    } catch {
      // Sign-out always completes locally.
    }
  }
  clearSession("signed_out");
}

export async function deleteAccount(): Promise<void> {
  const session = readSession();
  if (!session) throw new Error("no_session");
  await apiRequest<void>(
    session.auth_mode === "local_mock" ? "/v1/auth/local/account" : "/v1/auth/account",
    { method: "DELETE" },
  );
  clearSession("deleted");
}

export function getAccount(): Promise<unknown> {
  return apiRequest<unknown>("/v1/account");
}
