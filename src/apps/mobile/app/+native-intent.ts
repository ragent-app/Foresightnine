import { getSharedPayloads } from "expo-sharing";

/** Native-only route bridge. It never resolves a URL or performs a network request. */
export function redirectSystemPath({ path }: { readonly path: string; readonly initial: boolean }): string | null {
  try {
    const text = getSharedPayloads().filter((payload) => (payload.shareType === "text" || payload.shareType === "url") && /^(text\/plain|text\/uri-list)?$/i.test(payload.mimeType ?? "text/plain")).map((payload) => payload.value).join("\n").slice(0, 4000);
    return text ? `/evidence?shared=${encodeURIComponent(text)}` : path;
  } catch { return path; }
}
