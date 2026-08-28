import { sha256Hex } from "@foresightnine/fm-engine";
import { t } from "./i18n";
import type { EvidenceBundle, EvidenceGrade, Language } from "./types";

const URL_PATTERN = /https?:\/\/[^\s<>{}"']+/giu;
const SCHEME_PATTERN = /(?:^|[\s([{])([a-z][a-z\d+.-]*):(?=\/\/)/giu;
const UNSAFE_SCHEME_PATTERN = /(?:^|[\s([{])(?:javascript|data|file|blob|about|content|intent|mailto|tel):/iu;
const MAX_URL_LENGTH = 2_048;

export interface NormalizedShare {
  readonly url: string;
  readonly title: string;
  readonly excerpt: string;
}

export type NormalizeShareResult =
  | { readonly ok: true; readonly value: NormalizedShare }
  | { readonly ok: false; readonly reason: "empty" | "unsupported_url" | "invalid_url" };

function parseIpv4(hostname: string): number[] | null {
  const parts = hostname.split(".");
  if (parts.length !== 4 || parts.some((part) => !/^\d+$/.test(part))) return null;
  const octets = parts.map(Number);
  return octets.every((part) => Number.isInteger(part) && part >= 0 && part <= 255) ? octets : null;
}

function parseIpv6(hostname: string): bigint | null {
  const address = hostname.slice(1, -1);
  const halves = address.split("::");
  if (halves.length > 2) return null;
  const left = halves[0] ? halves[0].split(":") : [];
  const right = halves.length === 2 && halves[1] ? halves[1].split(":") : [];
  const groups = [...left, ...right];
  if (groups.some((group) => !/^[\da-f]{1,4}$/iu.test(group))) return null;
  if (halves.length === 1 && groups.length !== 8) return null;
  if (halves.length === 2 && groups.length >= 8) return null;
  const expanded = [...left, ...Array.from({ length: 8 - groups.length }, () => "0"), ...right];
  return expanded.reduce((value, group) => (value << 16n) | BigInt(Number.parseInt(group, 16)), 0n);
}

function ipv4IsUnsafe(octets: readonly number[]): boolean {
  const [first, second, third] = octets;
  return (
    first === 0 ||
    first === 10 ||
    first === 100 && second! >= 64 && second! <= 127 ||
    first === 127 ||
    first === 169 && second === 254 ||
    first === 172 && second! >= 16 && second! <= 31 ||
    first === 192 && second === 0 && (third === 0 || third === 2) ||
    first === 192 && second === 168 ||
    first === 192 && second === 88 && third === 99 ||
    first === 198 && (second === 18 || second === 19 || second === 51 && third === 100) ||
    first === 203 && second === 0 && third === 113 ||
    first! >= 224
  );
}

function ipv6InRange(value: bigint, network: bigint, prefixLength: number): boolean {
  const shift = BigInt(128 - prefixLength);
  const mask = ((1n << BigInt(prefixLength)) - 1n) << shift;
  return (value & mask) === (network & mask);
}

function ipv6IsUnsafe(value: bigint): boolean {
  const ipv4Mapped = value >> 32n === 0xffffn;
  if (ipv4Mapped) {
    const octets = [24n, 16n, 8n, 0n].map((shift) => Number((value >> shift) & 0xffn));
    return ipv4IsUnsafe(octets);
  }
  const networks = [
    ["100::", 64],
    ["2001::", 32],
    ["2001:2::", 48],
    ["2001:10::", 28],
    ["2001:db8::", 32],
    ["2002::", 16],
    ["3fff::", 20],
  ] as const;
  return (
    value === 0n ||
    value === 1n ||
    (value >> 120n) === 0xffn ||
    (value >> 121n) === 0x7en ||
    (value >> 118n) === 0x3fan ||
    networks.some(([network, prefixLength]) => {
      const parsed = parseIpv6(`[${network}]`);
      return parsed !== null && ipv6InRange(value, parsed, prefixLength);
    })
  );
}

function isUnsafeLiteralAddress(hostname: string): boolean {
  if (hostname.startsWith("[") && hostname.endsWith("]")) {
    const parsed = parseIpv6(hostname);
    return parsed === null || ipv6IsUnsafe(parsed);
  }
  const parsed = parseIpv4(hostname);
  return parsed !== null && ipv4IsUnsafe(parsed);
}

function hasCredentials(raw: string): boolean {
  const authorityStart = raw.indexOf("://") + 3;
  const authority = raw.slice(authorityStart);
  const authorityEnd = authority.search(/[/?#]/u);
  return authority.slice(0, authorityEnd < 0 ? authority.length : authorityEnd).includes("@");
}

/**
 * Sanitise a display-only public source URL. Callers must not fetch the returned
 * URL from the device; network access is limited to the configured project API.
 */
export function normalizeEvidenceUrl(raw: string): string | null {
  const candidate = raw.trim().replace(/[),.;!?]+$/g, "");
  if (!candidate || candidate.length > MAX_URL_LENGTH || candidate.includes("\\") || hasCredentials(candidate)) return null;
  try {
    const parsed = new URL(candidate);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
    if (parsed.username || parsed.password || (parsed.port && parsed.port !== "80" && parsed.port !== "443")) return null;
    const hostname = parsed.hostname.toLowerCase();
    const normalizedHostname = hostname.replace(/\.+$/u, "");
    if (!normalizedHostname || normalizedHostname === "localhost" || normalizedHostname.endsWith(".localhost") || normalizedHostname.endsWith(".local")) return null;
    // DNS names remain no-fetch inputs; only literal addresses can be classified locally.
    if (isUnsafeLiteralAddress(hostname)) return null;
    return parsed.toString();
  } catch {
    return null;
  }
}

export function normalizeSharedText(input: string, language: Language = "zh"): NormalizeShareResult {
  const text = input.replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/g, "").trim();
  if (!text) return { ok: false, reason: "empty" };
  const schemes = [...text.matchAll(SCHEME_PATTERN)].map((match) => match[1]!.toLowerCase());
  if (schemes.some((scheme) => scheme !== "http" && scheme !== "https") || UNSAFE_SCHEME_PATTERN.test(text)) {
    return { ok: false, reason: "unsupported_url" };
  }
  const matches = [...text.matchAll(URL_PATTERN)];
  const match = matches[0]?.[0];
  if (!match) return { ok: false, reason: "unsupported_url" };
  const urls = matches.map((item) => normalizeEvidenceUrl(item[0]));
  const url = urls[0];
  if (!url || urls.some((candidate) => candidate === null)) return { ok: false, reason: "invalid_url" };
  const withoutUrl = text.replace(match, "").replace(/\s+/g, " ").trim();
  const [firstLine, ...rest] = withoutUrl.split(/\n+/).map((line) => line.trim()).filter(Boolean);
  return {
    ok: true,
    value: {
      url,
      title: (firstLine || new URL(url).hostname).slice(0, 160),
      excerpt: (rest.join(" ") || firstLine || t(language, "userSharedNoFetch")).slice(0, 1000),
    },
  };
}

export function normalizeShareToBundle(input: string, now = "2026-08-23T00:00:00Z", language: Language = "zh"): NormalizeShareResult & { readonly bundle?: EvidenceBundle } {
  const normalized = normalizeSharedText(input, language);
  if (!normalized.ok) return normalized;
  const value = normalized.value;
  const base = {
    evidence_id: `shared-${sha256Hex(value.url + value.title).slice(0, 12)}`,
    challenge_id: "mobile-challenge",
    source_type: "commentary" as const,
    url: value.url,
    title: value.title,
    publisher: new URL(value.url).hostname,
    published_at: null,
    retrieved_at: now,
    excerpt: value.excerpt,
    user_note: `${t(language, "userSharedNoFetch")} ${t(language, "unverifiedUserSupplied")}`,
    grade: "C" as EvidenceGrade,
    supports_or_challenges: "context" as const,
    independently_reviewable: false,
  };
  return { ok: true, value, bundle: { ...base, content_hash: sha256Hex(JSON.stringify(base)) } };
}

export function isDuplicateEvidence(existing: readonly EvidenceBundle[], candidate: EvidenceBundle): boolean {
  return existing.some((item) => item.content_hash === candidate.content_hash || (item.url !== "" && item.url === candidate.url));
}
