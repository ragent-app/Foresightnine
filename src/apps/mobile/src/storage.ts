import * as SecureStore from "expo-secure-store";
import { researchOwnerHash } from "./research-owner";
import type { DraftState } from "./types";

const LANGUAGE_KEY = "f9.mobile.language.v1";
const writes = new Map<string, Promise<unknown>>();
function draftKey(scope: string): string { return `f9.mobile.draft.v2.${researchOwnerHash(scope)}`; }
function ordered<T>(key: string, operation: () => Promise<T>): Promise<T> {
  const result = (writes.get(key) ?? Promise.resolve()).then(operation);
  const tail = result.then(() => undefined, () => undefined);
  writes.set(key, tail);
  void tail.then(() => { if (writes.get(key) === tail) writes.delete(key); });
  return result;
}
function validDraft(value: unknown): value is DraftState {
  if (!value || typeof value !== "object") return false;
  if (Object.keys(value).some(key => !["viewpoint", "capital", "currency", "language", "updatedAt"].includes(key))) return false;
  const draft = value as Partial<DraftState>;
  return typeof draft.viewpoint === "string" && draft.viewpoint.length <= 20_000 && typeof draft.capital === "string" && draft.capital.length <= 100 && (draft.currency === "CNY" || draft.currency === "USD" || draft.currency === "EUR") && (draft.language === "zh" || draft.language === "en") && typeof draft.updatedAt === "string" && Number.isFinite(Date.parse(draft.updatedAt));
}
export async function saveDraft(draft: DraftState, scope = "anonymous"): Promise<void> {
  if (!validDraft(draft)) throw new Error("invalid_research_draft");
  const snapshot = JSON.stringify(draft); // Capture before waiting for older writes.
  return ordered(draftKey(scope), () => SecureStore.setItemAsync(draftKey(scope), snapshot));
}

export async function loadDraft(scope = "anonymous"): Promise<DraftState | null> {
  return ordered(draftKey(scope), async () => {
    const value = await SecureStore.getItemAsync(draftKey(scope));
    if (!value || value.length > 100_000) return null;
    try { const parsed: unknown = JSON.parse(value); return validDraft(parsed) ? parsed : null; }
    catch { return null; }
  });
}

export async function saveLanguage(language: DraftState["language"]): Promise<void> {
  return ordered(LANGUAGE_KEY, () => SecureStore.setItemAsync(LANGUAGE_KEY, language));
}
export async function loadLanguage(): Promise<DraftState["language"] | null> {
  return ordered(LANGUAGE_KEY, async () => {
    const value = await SecureStore.getItemAsync(LANGUAGE_KEY);
    return value === "zh" || value === "en" ? value : null;
  });
}
