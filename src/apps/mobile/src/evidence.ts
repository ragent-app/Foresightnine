import { sha256Hex } from "@foresightnine/fm-engine";
import type { EvidenceBundle, EvidenceGrade } from "./types";

const URL_PATTERN = /https?:\/\/[^\s<>{}"']+/giu;

export interface NormalizedShare {
  readonly url: string;
  readonly title: string;
  readonly excerpt: string;
}

export type NormalizeShareResult =
  | { readonly ok: true; readonly value: NormalizedShare }
  | { readonly ok: false; readonly reason: "empty" | "unsupported_url" | "invalid_url" };

function cleanUrl(raw: string): string | null {
  const candidate = raw.trim().replace(/[),.;!?]+$/g, "");
  try {
    const parsed = new URL(candidate);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
    parsed.username = "";
    parsed.password = "";
    return parsed.toString();
  } catch {
    return null;
  }
}

export function normalizeSharedText(input: string): NormalizeShareResult {
  const text = input.replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/g, "").trim();
  if (!text) return { ok: false, reason: "empty" };
  const match = text.match(URL_PATTERN)?.[0];
  if (!match) return { ok: false, reason: "unsupported_url" };
  const url = cleanUrl(match);
  if (!url) return { ok: false, reason: "invalid_url" };
  const withoutUrl = text.replace(match, "").replace(/\s+/g, " ").trim();
  const [firstLine, ...rest] = withoutUrl.split(/\n+/).map((line) => line.trim()).filter(Boolean);
  return {
    ok: true,
    value: {
      url,
      title: (firstLine || new URL(url).hostname).slice(0, 160),
      excerpt: (rest.join(" ") || firstLine || "用户从浏览器分享的链接；未自动抓取远程页面。").slice(0, 1000),
    },
  };
}

export function normalizeShareToBundle(input: string, now = "2026-08-23T00:00:00Z"): NormalizeShareResult & { readonly bundle?: EvidenceBundle } {
  const normalized = normalizeSharedText(input);
  if (!normalized.ok) return normalized;
  const value = normalized.value;
  const base = {
    evidence_id: `shared-${sha256Hex(value.url + value.title).slice(0, 12)}`,
    challenge_id: "mobile-challenge",
    source_type: "reliable_secondary" as const,
    url: value.url,
    title: value.title,
    publisher: new URL(value.url).hostname,
    published_at: null,
    retrieved_at: now,
    excerpt: value.excerpt,
    user_note: "来自 Android Share Intent；客户端没有自动访问该 URL。",
    grade: "C" as EvidenceGrade,
    supports_or_challenges: "context" as const,
    independently_reviewable: true,
  };
  return { ok: true, value, bundle: { ...base, content_hash: sha256Hex(JSON.stringify(base)) } };
}

export function isDuplicateEvidence(existing: readonly EvidenceBundle[], candidate: EvidenceBundle): boolean {
  return existing.some((item) => item.content_hash === candidate.content_hash || (item.url !== "" && item.url === candidate.url));
}
