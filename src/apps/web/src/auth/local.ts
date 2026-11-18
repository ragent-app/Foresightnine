import { apiRequest } from "../api/http";
import { parseSession, saveSession, type Session } from "./session";

function checkCredentials(email: string, password: string): void {
  if (email.length < 3 || email.length > 254 || password.length < 12 || password.length > 256) {
    throw new Error("invalid_credentials_format");
  }
}

async function localAuth(path: string, email: string, password: string): Promise<Session> {
  checkCredentials(email, password);
  const payload = await apiRequest<unknown>(path, { method: "POST", body: { email, password } });
  const session = parseSession(payload);
  saveSession(session);
  return session;
}

export function localRegister(email: string, password: string): Promise<Session> {
  return localAuth("/v1/auth/local/register", email, password);
}

export function localLogin(email: string, password: string): Promise<Session> {
  return localAuth("/v1/auth/local/login", email, password);
}
