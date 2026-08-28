import type { EvidenceDraft, EvidenceErrors, Currency } from "./types";

export interface ThesisForm {
  readonly viewpoint: string;
  readonly capital: string;
  readonly currency: Currency;
}

export interface ThesisErrors {
  viewpoint?: string;
  capital?: string;
}

export function validateThesis(form: ThesisForm): ThesisErrors {
  const errors: ThesisErrors = {};
  const viewpoint = form.viewpoint.trim();
  if (viewpoint.length === 0) errors.viewpoint = "请先写下你的观点。";
  else if (viewpoint.length > 2000) errors.viewpoint = "观点最多 2,000 个字符。";
  const amount = Number(form.capital.replace(/,/g, ""));
  if (!form.capital.trim() || !Number.isFinite(amount) || amount <= 0) errors.capital = "请输入大于 0 的可投资金额。";
  return errors;
}

export function parseCapital(value: string): number {
  const amount = Number(value.replace(/,/g, ""));
  return Number.isFinite(amount) ? amount : 0;
}

export function percentRange(minimum: number, maximum: number): string {
  return `${Math.round(minimum * 100)}–${Math.round(maximum * 100)}%`;
}

function isPrivateIpv4(hostname: string): boolean {
  if (!/^\d+(?:\.\d+){0,3}$/.test(hostname)) return false;
  const parts = hostname.split(".").map(Number);
  const limits = [0xffffffff, 0xffffff, 0xffff, 0xff];
  if (parts.length < 1 || parts.length > 4 || parts.some((part, index) => !Number.isInteger(part) || part < 0 || part > limits[index]!)) return true;
  const value = parts.length === 1 ? parts[0]! : parts.length === 2 ? parts[0]! * 0x1000000 + parts[1]! : parts.length === 3 ? parts[0]! * 0x1000000 + parts[1]! * 0x10000 + parts[2]! : parts[0]! * 0x1000000 + parts[1]! * 0x10000 + parts[2]! * 0x100 + parts[3]!;
  const first = Math.floor(value / 0x1000000);
  const second = Math.floor(value / 0x10000) % 0x100;
  return first === 0 || first === 10 || first === 127 || (first === 100 && second >= 64 && second <= 127) || (first === 169 && second === 254) || (first === 172 && second >= 16 && second <= 31) || (first === 192 && (second === 0 || second === 168)) || (first === 198 && (second === 18 || second === 19 || second === 51)) || (first === 203 && second === 0) || first >= 224;
}

export function isSafeEvidenceUrl(value: string): boolean {
  try {
    const parsed = new URL(value);
    const hostname = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, "");
    // WHATWG URL preserves a DNS trailing dot. Strip it for classification so
    // localhost. and dotted loopback/private literals cannot bypass the same
    // SSRF guard as their canonical spellings.
    const normalizedHostname = hostname.replace(/\.+$/u, "");
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return false;
    const defaultPort = parsed.protocol === "https:" ? "443" : "80";
    if (!normalizedHostname || parsed.username || parsed.password || (parsed.port && parsed.port !== defaultPort) || normalizedHostname.includes(":") || normalizedHostname === "localhost" || normalizedHostname.endsWith(".localhost") || normalizedHostname.endsWith(".local") || normalizedHostname.endsWith(".internal") || isPrivateIpv4(normalizedHostname)) return false;
    return true;
  } catch {
    return false;
  }
}

export function validateSearchEvidence(draft: EvidenceDraft): EvidenceErrors {
  const errors: EvidenceErrors = {};
  const url = draft.url.trim();
  const noUrlAllowed = draft.grade === "D" || draft.grade === "E";
  const title = draft.title.trim();
  const publisher = draft.publisher.trim();
  const publishedAt = draft.published_at.trim();
  const excerpt = draft.excerpt.trim();
  const note = draft.user_note.trim();
  if ((!url && !noUrlAllowed) || (url && !isSafeEvidenceUrl(url))) errors.url = "A–C 证据必须填写安全的 http(s) URL；禁止本机、内网、凭据和危险地址。D/E 可留空。";
  if (title.length === 0 || title.length > 240) errors.title = "标题必填且最多 240 个字符。";
  if (publisher.length === 0 || publisher.length > 160) errors.publisher = "发布者必填且最多 160 个字符。";
  const publishedTimestamp = Date.parse(publishedAt);
  if (!publishedAt && noUrlAllowed && !url) {
    // A no-URL D/E bundle may omit publication time, matching the API fallback path.
  } else if (!/^\d{4}-\d{2}-\d{2}(?:T.*)?$/.test(publishedAt) || !Number.isFinite(publishedTimestamp)) errors.published_at = "请使用有效发布日期，例如 2026-08-09。";
  else if (publishedTimestamp > Date.parse("2026-08-09T23:59:59Z")) errors.published_at = "发布日期不能晚于数据截止日 2026-08-09。";
  if (excerpt.length === 0 || excerpt.length > 4000) errors.excerpt = "摘录必填且最多 4,000 个字符。";
  if (note.length > 1000) errors.user_note = "用户说明最多 1,000 个字符。";
  return errors;
}

export function currencySymbol(currency: Currency): string {
  return currency === "USD" ? "$" : currency === "EUR" ? "€" : "¥";
}
