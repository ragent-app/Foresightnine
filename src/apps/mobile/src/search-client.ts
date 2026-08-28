import { sha256Hex } from "@foresightnine/fm-engine";
import { normalizeEvidenceUrl } from "./evidence";
import { t } from "./i18n";
import type { EvidenceBundle, Language } from "./types";

const MAX_QUERY_LENGTH = 70;
const MAX_CANDIDATES = 10;
const MAX_TITLE_LENGTH = 500;
const MAX_EXCERPT_LENGTH = 10_000;

export interface ProjectSearchCandidate {
  readonly searchResultHash: string;
  readonly searchProvider: string;
  readonly title: string;
  readonly url: string;
  readonly excerpt: string;
  readonly publisher: string | null;
  readonly publishedAt: string | null;
  readonly retrievedAt: string;
  readonly independentlyReviewable: boolean;
}

export interface ProjectSearchResult {
  readonly status: "available" | "api_unavailable" | "api_failed";
  readonly candidates: readonly ProjectSearchCandidate[];
}

export interface ProjectSearchOptions {
  readonly apiBaseUrl?: string;
  readonly fetcher?: typeof fetch;
  readonly signal?: AbortSignal;
}

function configuredApiBase(): string {
  return typeof process === "undefined" ? "" : process.env.EXPO_PUBLIC_API_BASE_URL ?? "";
}

/** Only an HTTPS public project API can be configured in an app bundle. */
export function safeProjectApiBase(raw: string): string | null {
  try {
    const parsed = new URL(raw.trim());
    const hostname = parsed.hostname.toLowerCase().replace(/\.+$/u, "");
    if (
      parsed.protocol !== "https:"
      || parsed.username
      || parsed.password
      || parsed.port
      || parsed.search
      || parsed.hash
      || !hostname
      || hostname === "localhost"
      || hostname.endsWith(".localhost")
      || hostname.endsWith(".local")
      || !hostname.includes(".")
      || /^[\d.:\[\]]+$/u.test(hostname)
    ) return null;
    return `${parsed.origin}${parsed.pathname.replace(/\/$/u, "")}`;
  } catch {
    return null;
  }
}

function safeIsoDate(value: unknown): string | null {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/u.test(value)) return null;
  return Number.isNaN(Date.parse(`${value}T00:00:00.000Z`)) ? null : value;
}

function safeRetrievedAt(value: unknown): string {
  if (typeof value === "string" && !Number.isNaN(Date.parse(value))) return value;
  return new Date().toISOString();
}

function candidateFromUnknown(value: unknown): ProjectSearchCandidate | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const title = typeof record.title === "string" ? record.title.trim() : "";
  const excerpt = typeof record.excerpt === "string" ? record.excerpt.trim() : "";
  const provider = typeof record.search_provider === "string" ? record.search_provider.trim() : "";
  const hash = typeof record.search_result_hash === "string" ? record.search_result_hash.trim() : "";
  const url = typeof record.url === "string" ? normalizeEvidenceUrl(record.url) : null;
  if (!title || title.length > MAX_TITLE_LENGTH || !excerpt || excerpt.length > MAX_EXCERPT_LENGTH || !provider || !/^[a-f0-9]{64}$/u.test(hash) || !url || typeof record.independently_reviewable !== "boolean") return null;
  const publisher = typeof record.publisher === "string" && record.publisher.trim() ? record.publisher.trim().slice(0, 500) : null;
  return {
    searchResultHash: hash,
    searchProvider: provider,
    title,
    url,
    excerpt,
    publisher,
    publishedAt: safeIsoDate(record.published_at),
    retrievedAt: safeRetrievedAt(record.retrieved_at),
    independentlyReviewable: record.independently_reviewable,
  };
}

function parseSearchResponse(value: unknown): ProjectSearchResult | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  if (record.schema_version !== "v1" || typeof record.provider !== "string" || record.fallback !== "client_bundle" || typeof record.project_search_available !== "boolean" || !Array.isArray(record.candidates)) return null;
  if (!record.project_search_available && record.candidates.length !== 0) return null;
  const candidates = record.candidates.slice(0, MAX_CANDIDATES).flatMap((candidate) => {
    const parsed = candidateFromUnknown(candidate);
    return parsed ? [parsed] : [];
  });
  if (!record.project_search_available) return { status: "api_unavailable", candidates: [] };
  return { status: "available", candidates };
}

/**
 * Query only the deployer-configured ForesightNine API. Search-result links stay
 * inert display data; a person must explicitly choose one before a local bundle
 * is created. No provider credential reaches Android.
 */
export async function searchProjectCandidates(query: string, options: ProjectSearchOptions = {}): Promise<ProjectSearchResult> {
  const baseUrl = safeProjectApiBase(options.apiBaseUrl ?? configuredApiBase());
  const normalizedQuery = query.trim().slice(0, MAX_QUERY_LENGTH);
  if (!baseUrl || !normalizedQuery) return { status: "api_unavailable", candidates: [] };
  const endpoint = new URL("v1/search/candidates", `${baseUrl}/`);
  endpoint.searchParams.set("query", normalizedQuery);
  endpoint.searchParams.set("limit", String(MAX_CANDIDATES));
  try {
    const init: RequestInit = { method: "GET", headers: { Accept: "application/json" }, credentials: "omit" };
    if (options.signal) init.signal = options.signal;
    const response = await (options.fetcher ?? fetch)(endpoint.toString(), init);
    if (!response.ok) return { status: "api_failed", candidates: [] };
    return parseSearchResponse(await response.json()) ?? { status: "api_failed", candidates: [] };
  } catch {
    return { status: "api_failed", candidates: [] };
  }
}

/** User selection preserves discovery provenance but never upgrades a candidate to a fact. */
export function candidateToLocalBundle(candidate: ProjectSearchCandidate, language: Language): EvidenceBundle {
  const base = {
    evidence_id: `search-${sha256Hex(candidate.searchResultHash).slice(0, 12)}`,
    challenge_id: "mobile-challenge",
    source_type: "commentary" as const,
    url: candidate.url,
    title: candidate.title,
    publisher: candidate.publisher ?? new URL(candidate.url).hostname,
    published_at: candidate.publishedAt,
    retrieved_at: candidate.retrievedAt,
    excerpt: candidate.excerpt,
    user_note: language === "en" ? "User selected an unverified project-search candidate; review the source before treating it as evidence." : "用户选择了未经验证的项目搜索候选；在作为证据前需要核对原始来源。",
    grade: "C" as const,
    supports_or_challenges: "context" as const,
    independently_reviewable: false,
    search_provider: candidate.searchProvider,
    search_result_hash: candidate.searchResultHash,
  };
  return { ...base, content_hash: sha256Hex(JSON.stringify(base)) };
}

export function searchCandidateNotice(language: Language, candidate: ProjectSearchCandidate): string {
  return language === "en"
    ? `Candidate selected from ${candidate.searchProvider}. It remains unverified until you inspect the source.`
    : `已选择来自 ${candidate.searchProvider} 的候选。核对原始来源前它仍是未经验证的信息。`;
}
