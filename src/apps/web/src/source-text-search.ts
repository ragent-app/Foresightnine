import type { SourceMetadata } from "./general-research-client";

export interface SourceTextMatch { start: number; end: number; left: number; right: number }

/** Mirror the server's display, simulation and declared cutoff checks. */
export function canUseCitationSource(source: SourceMetadata, asOf?: string | null): boolean {
  if (source.status !== "available" || !source.permissions?.display || source.is_simulated) return false;
  if (!asOf) return true;
  const cutoff = Date.parse(asOf);
  const published = source.published_at ? Date.parse(source.published_at) : NaN;
  const revised = source.revised_at ? Date.parse(source.revised_at) : null;
  return Number.isFinite(cutoff) && Number.isFinite(published) && published <= cutoff &&
    (revised === null || (Number.isFinite(revised) && revised <= cutoff));
}

/** Literal case-insensitive search; indices address the original JS string. */
export function searchSourceText(text: string, query: string): { items: SourceTextMatch[]; truncated: boolean } {
  const term = query.trim();
  if (!text || term.length < 2 || term.length > 120) return { items: [], truncated: false };
  const regex = new RegExp(term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "giu");
  const items: SourceTextMatch[] = [];
  let match: RegExpExecArray | null;
  while ((match = regex.exec(text)) !== null) {
    if (items.length === 20) return { items, truncated: true };
    const start = match.index, end = start + match[0].length;
    let left = Math.max(0, start - 120), right = Math.min(text.length, end + 120);
    const word = (value: string) => /^[A-Za-z0-9_]$/.test(value);
    while (left > 0 && start - left < 180 && word(text[left] ?? "") && word(text[left - 1] ?? "")) left--;
    while (right < text.length && right - end < 180 && word(text[right - 1] ?? "") && word(text[right] ?? "")) right++;
    items.push({ start, end, left, right });
  }
  return { items, truncated: false };
}
