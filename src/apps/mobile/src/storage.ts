import * as SecureStore from "expo-secure-store";
import type { DraftState } from "./types";

const DRAFT_KEY = "f9.mobile.draft.v1";
const LANGUAGE_KEY = "f9.mobile.language.v1";

export async function saveDraft(draft: DraftState): Promise<void> {
  await SecureStore.setItemAsync(DRAFT_KEY, JSON.stringify(draft));
}

export async function loadDraft(): Promise<DraftState | null> {
  const value = await SecureStore.getItemAsync(DRAFT_KEY);
  if (!value) return null;
  try {
    const parsed: unknown = JSON.parse(value);
    if (!parsed || typeof parsed !== "object") return null;
    const draft = parsed as Partial<DraftState>;
    return typeof draft.viewpoint === "string" && typeof draft.capital === "string" && (draft.currency === "CNY" || draft.currency === "USD" || draft.currency === "EUR") && (draft.language === "zh" || draft.language === "en") && typeof draft.updatedAt === "string" ? draft as DraftState : null;
  } catch {
    return null;
  }
}

export async function saveLanguage(language: DraftState["language"]): Promise<void> {
  await SecureStore.setItemAsync(LANGUAGE_KEY, language);
}

export async function loadLanguage(): Promise<DraftState["language"] | null> {
  const value = await SecureStore.getItemAsync(LANGUAGE_KEY);
  return value === "zh" || value === "en" ? value : null;
}
