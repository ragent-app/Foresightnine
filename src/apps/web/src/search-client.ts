import { isSafeEvidenceUrl } from "./validation";
import { configuredApiBaseUrl } from "./runtime-config";
import type { SearchCandidate, SearchCandidatesResult } from "./types";

export const SEARCH_CANDIDATE_LIMIT = 10;

export interface SearchCandidatesOptions {
  readonly apiBaseUrl?: string;
  readonly fetcher?: typeof fetch;
  readonly signal?: AbortSignal;
}

export function clientBundleFallback(): SearchCandidatesResult {
  return { provider: "client_bundle", project_search_available: false, fallback: "client_bundle", candidates: [] };
}

export function safeConfiguredApiBase(value: unknown): string | null {
  if (typeof value !== "string" || !value || value !== value.trim() || !isSafeEvidenceUrl(value)) return null;
  try {
    const parsed = new URL(value);
    if (parsed.search || parsed.hash) return null;
    return parsed.toString().replace(/\/$/, "");
  } catch {
    return null;
  }
}

function requiredString(value: unknown, maxLength: number): string | null {
  return typeof value === "string" && value === value.trim() && value.length > 0 && value.length <= maxLength ? value : null;
}

function optionalDateTime(value: unknown): string | undefined | null {
  if (value === undefined) return undefined;
  const dateTime = requiredString(value, 80);
  return dateTime !== null && Number.isFinite(Date.parse(dateTime)) ? dateTime : null;
}

function candidateFromUnknown(value: unknown): SearchCandidate | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const title = requiredString(record.title, 240);
  const url = requiredString(record.url, 2_048);
  const excerpt = requiredString(record.excerpt, 4_000);
  const searchProvider = requiredString(record.search_provider, 100);
  const searchResultHash = requiredString(record.search_result_hash, 64);
  const retrievedAt = optionalDateTime(record.retrieved_at);
  const publishedAt = optionalDateTime(record.published_at);
  const publisher = record.publisher === undefined ? undefined : requiredString(record.publisher, 160);
  if (!title || !url || !excerpt || !searchProvider || !searchResultHash || !retrievedAt || publishedAt === null || publisher === null || typeof record.independently_reviewable !== "boolean" || !/^[a-f0-9]{64}$/.test(searchResultHash) || !isSafeEvidenceUrl(url)) return null;
  return { title, url, excerpt, search_provider: searchProvider, search_result_hash: searchResultHash, retrieved_at: retrievedAt, ...(publishedAt ? { published_at: publishedAt } : {}), ...(publisher ? { publisher } : {}), independently_reviewable: record.independently_reviewable };
}

function parseSearchResponse(value: unknown): SearchCandidatesResult | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const provider = requiredString(record.provider, 100);
  if (record.schema_version !== "v1" || !provider || typeof record.project_search_available !== "boolean" || record.fallback !== "client_bundle" || !Array.isArray(record.candidates)) return null;
  if (!record.project_search_available && record.candidates.length > 0) return null;
  const candidates: SearchCandidate[] = [];
  for (const item of record.candidates.slice(0, SEARCH_CANDIDATE_LIMIT)) {
    const candidate = candidateFromUnknown(item);
    if (candidate) candidates.push(candidate);
  }
  return { provider, project_search_available: record.project_search_available, fallback: "client_bundle", candidates };
}

export async function searchEvidenceCandidates(query: string, options: SearchCandidatesOptions = {}): Promise<SearchCandidatesResult> {
  const baseUrl = safeConfiguredApiBase(options.apiBaseUrl ?? configuredApiBaseUrl());
  const normalizedQuery = query.trim().slice(0, 70);
  if (!baseUrl || !normalizedQuery) return clientBundleFallback();
  const endpoint = new URL("v1/search/candidates", `${baseUrl}/`);
  endpoint.searchParams.set("query", normalizedQuery);
  endpoint.searchParams.set("limit", String(SEARCH_CANDIDATE_LIMIT));
  try {
    const response = await (options.fetcher ?? fetch)(endpoint, { method: "GET", headers: { Accept: "application/json" }, credentials: "omit", ...(options.signal ? { signal: options.signal } : {}) });
    if (!response.ok) return clientBundleFallback();
    return parseSearchResponse(await response.json()) ?? clientBundleFallback();
  } catch {
    return clientBundleFallback();
  }
}
