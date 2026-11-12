import { sameOriginOAuthFetcher } from "./api-runtime";
import type { ResearchV2 } from "@foresightnine/contracts";

export interface SourcePosition { page?: number; character_start?: number; character_end?: number }
export interface SourceLocation extends SourcePosition { status?: string; matches?: SourcePosition[]; truncated?: boolean }
export interface SourceContextResult { title: string; before: string; selected: string; after: string; page_start: number | null; page_end: number | null }
export interface ResearchClaim {
  text: string;
  authorship?: string;
  role?: string;
  support_status?: string;
  support_review?: { verdict: "supports" | "does_not_support" | "contradicts" | "uncertain"; method: string; independently_verified: false };
  citations?: { source_id: string; quotation?: string; quotation_match?: string; support_status?: string; location?: SourceLocation }[];
  suggested_fact_evidence?: { fact_id: string; source_id: string; value: string; unit: string; period: string; quotation: string; location: SourceLocation; status: string }[];
  uncited_percentages?: string[];
  publication_date_warnings?: { claimed_text: string; source_id: string; declared_published_at: string; status: string }[];
}
export interface ResearchFact { fact_id: string; label: string; value: string; unit: string; currency?: string | null; period: string; source_id?: string | null; quotation?: string | null; quotation_match?: string; verification: string; authorship?: string; location?: SourceLocation }
export interface ResearchContent {
  summary: string;
  sources: { source_id: string; title?: string; url?: string | null; is_simulated?: boolean; event_at?: string | null; published_at?: string | null; obtained_at?: string | null; revised_at?: string | null }[];
  source_candidates: { title: string; url: string; status: string; published_at?: string | null }[];
  facts: ResearchFact[];
  evidence_differences?: { label: string; period: string; unit: string; currency: string | null; status: string; truncated: boolean; facts: { fact_id: string; source_id: string; value: string }[] }[];
  calculations: { formula_id: string; formula_version?: string; expression: string; inputs: { fact_id: string; value: string; unit: string; period: string }[]; value: string; unit: string; authorship?: string; input_status?: string }[];
  calculation_differences?: { claim_index: number; calculation_index: number; claimed_value: string; calculated_value: string; unit: string; status: string }[];
  amount_differences?: { claim_index: number; claimed_amount: string; source_fact_ids: string[]; status: string }[];
  claims: ResearchClaim[];
  limitations: string[];
  run: Record<string, unknown>;
}
export interface ResearchVersion {
  version_id: string;
  created_by?: "user" | "agent" | "unknown";
  parent_version_id?: string | null;
  created_at: string;
  review: "draft" | "confirmed" | "reservation";
  note: string;
  content: ResearchContent;
}
export interface ResearchTopic {
  topic_id: string;
  request: { question: string; source_ids: string[] };
  versions: ResearchVersion[];
}
export interface ResearchOperation {
  operation_id: string;
  status: string;
  cancel_token: string;
}
export interface SourceMetadata { source_id: string; title?: string; url?: string; status: string; is_simulated?: boolean; event_at?: string | null; published_at?: string | null; obtained_at?: string | null; revised_at?: string | null; retrieval_method?: string; permissions?: { display: boolean; export: boolean } }
export interface DiscoveryCandidate { title: string; url: string; excerpt: string }
export interface IssuerCandidate { cik: string; ticker: string; name: string; exchange: string | null }
export interface RunBudget { search: boolean; supportReview: boolean; maxElapsedSeconds: number; maxTotalTokens: number; maxCostUsd?: string }
export interface RunInputSnapshot {
  captured_at: string;
  parent_version_id: string;
  as_of: string | null;
  budget: ResearchV2.ResearchRunRequest;
  sources: { source_id: string; content_sha256: string; is_simulated?: boolean; permissions: ResearchV2.SourcePermissions }[];
  model: string | null;
  provider: string | null;
}

export class ResearchRequestError extends Error {
  constructor(readonly status: number, readonly retryAfterMs: number | null) {
    super(`request_${status}`);
  }
}

export function researchPollingDelay(error: unknown, previousMs: number): number {
  const backoff = Math.min(Math.max(1500, previousMs) * 2, 30000);
  if (error instanceof ResearchRequestError && (error.status === 429 || error.status === 503) && error.retryAfterMs !== null) {
    return Math.max(backoff, error.retryAfterMs);
  }
  return backoff;
}

function retryAfterMs(header: string | null): number | null {
  if (!header) return null;
  const seconds = /^\d+$/.test(header.trim()) ? Number(header) : (Date.parse(header) - Date.now()) / 1000;
  return Number.isFinite(seconds) && seconds >= 0 ? Math.min(2147483647, seconds * 1000) : null;
}

function sourceTimes(publishedAt: string, eventAt: string, revisedAt: string) {
  return {
    ...(publishedAt ? { published_at: new Date(publishedAt).toISOString() } : {}),
    ...(eventAt ? { event_at: new Date(eventAt).toISOString() } : {}),
    ...(revisedAt ? { revised_at: new Date(revisedAt).toISOString() } : {}),
  };
}

export function researchClient(baseUrl: string) {
  const fetcher = sameOriginOAuthFetcher(baseUrl);
  async function request<T>(path: string, body?: unknown, key?: string): Promise<T> {
    const response = await fetcher(new URL(path, `${baseUrl.replace(/\/+$/, "")}/`), {
      method: body === undefined ? "GET" : "POST", headers: {
        Accept: "application/json", ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...(key ? { "Idempotency-Key": key } : {}),
      }, ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (!response.ok) throw new ResearchRequestError(response.status, retryAfterMs(response.headers.get("Retry-After")));
    return await response.json() as T;
  }
  return {
    list: () => request<ResearchTopic[]>("/v2/research/topics"),
    get: (id: string) => request<ResearchTopic>(`/v2/research/topics/${encodeURIComponent(id)}`),
    sources: (topicId: string, versionId: string) => request<SourceMetadata[]>(
      `/v2/research/topics/${encodeURIComponent(topicId)}/versions/${encodeURIComponent(versionId)}/sources`),
    sourceContext: (sourceId: string, start: number, end: number) => request<SourceContextResult>(
      `/v2/research/sources/${encodeURIComponent(sourceId)}/context?start=${start}&end=${end}`),
    createSource: (title: string, text: string, url: string, modelInput: boolean, allowExport: boolean, publishedAt: string, eventAt = "", revisedAt = "", isSimulated = false) =>
      request<{ source_id: string }>("/v2/research/sources", { title, text, ...(url ? { url } : {}),
        ...sourceTimes(publishedAt, eventAt, revisedAt), is_simulated: isSimulated,
        permissions: { store: true, display: true, model_input: modelInput, export: allowExport } }),
    fetchSource: (title: string, url: string, modelInput: boolean, allowExport: boolean, publishedAt: string, eventAt = "", revisedAt = "", isSimulated = false) =>
      request<{ source_id: string }>("/v2/research/sources/fetch", { title, url,
        ...sourceTimes(publishedAt, eventAt, revisedAt), is_simulated: isSimulated,
        permissions: { store: true, display: true, model_input: modelInput, export: allowExport } }),
    fetchSecConcept: (cik: string, taxonomy: string, tag: string, unit: string, filingBefore: string, modelInput: boolean, allowExport: boolean) =>
      request<{ source_id: string }>("/v2/research/sources/sec-concept", { cik, taxonomy, tag,
        ...(unit ? { unit } : {}), ...(filingBefore ? { filing_before: filingBefore } : {}),
        permissions: { store: true, display: true, model_input: modelInput, export: allowExport } }),
    searchSecIssuers: (query: string) => request<{ candidates: IssuerCandidate[]; retrieved_at: string; source_url: string; limitation: string }>(
      "/v2/research/identities/sec-search", { query }),
    uploadPdf: (title: string, pdfBase64: string, modelInput: boolean, allowExport: boolean, publishedAt: string, eventAt = "", revisedAt = "", isSimulated = false) =>
      request<{ source_id: string }>("/v2/research/sources/pdf", { title, pdf_base64: pdfBase64,
        ...sourceTimes(publishedAt, eventAt, revisedAt), is_simulated: isSimulated,
        permissions: { store: true, display: true, model_input: modelInput, export: allowExport } }),
    search: (query: string) => request<{ project_search_available: boolean; candidates: DiscoveryCandidate[] }>(
      `/v1/search/candidates?query=${encodeURIComponent(query.slice(0, 70))}&limit=5`),
    create: (question: string, sourceIds: string[], asOf: string) => request<ResearchTopic>("/v2/research/topics", { question, source_ids: sourceIds,
      ...(asOf ? { as_of: new Date(asOf).toISOString() } : {}) }),
    attach: (topicId: string, parent: string, sourceIds: string[]) => request<ResearchTopic>(
      `/v2/research/topics/${encodeURIComponent(topicId)}/sources`, { parent_version_id: parent, source_ids: sourceIds }),
    recalculate: (topicId: string, parent: string, index: number) => request<ResearchTopic>(
      `/v2/research/topics/${encodeURIComponent(topicId)}/calculations/recalculate`, { parent_version_id: parent, calculation_index: index }),
    run: (topicId: string, parent: string, key: string, budget: RunBudget) => request<{ operation: ResearchOperation }>(
      `/v2/research/topics/${encodeURIComponent(topicId)}/runs`, { parent_version_id: parent,
        max_search_calls: budget.search ? 1 : 0, max_elapsed_seconds: budget.maxElapsedSeconds,
        max_total_tokens: budget.maxTotalTokens, max_model_calls: budget.supportReview ? 2 : 1, support_review: budget.supportReview,
        ...(budget.maxCostUsd ? { max_cost_usd: budget.maxCostUsd } : {}) } satisfies ResearchV2.ResearchRunRequest, key),
    operation: (topicId: string, operationId: string) => request<{ operation: ResearchOperation }>(
      `/v2/research/topics/${encodeURIComponent(topicId)}/runs/${encodeURIComponent(operationId)}`),
    runs: (topicId: string) => request<{ operation: ResearchOperation }[]>(
      `/v2/research/topics/${encodeURIComponent(topicId)}/runs`),
    runInputs: (topicId: string, operationId: string) => request<RunInputSnapshot>(
      `/v2/research/topics/${encodeURIComponent(topicId)}/runs/${encodeURIComponent(operationId)}/inputs`),
    cancel: (operation: ResearchOperation, key: string) => request<{ operation: ResearchOperation }>(
      `/v1/operations/${encodeURIComponent(operation.operation_id)}/cancel`, { cancel_token: operation.cancel_token }, key),
    save: (topicId: string, version: ResearchVersion, summary: string, review: ResearchVersion["review"], note: string, claims: ResearchClaim[], facts: ResearchFact[]) =>
      request<ResearchTopic>(`/v2/research/topics/${encodeURIComponent(topicId)}/versions`, {
        parent_version_id: version.version_id, content: { ...version.content, summary, claims, facts }, review, note,
      }),
    exportVersion: (topicId: string, versionId: string) => request<ResearchV2.ResearchFullExport>(
      `/v2/research/topics/${encodeURIComponent(topicId)}/versions/${encodeURIComponent(versionId)}/export`),
    exportMetadata: (topicId: string, versionId: string) => request<ResearchV2.ResearchMetadataExport>(
      `/v2/research/topics/${encodeURIComponent(topicId)}/versions/${encodeURIComponent(versionId)}/export/metadata`),
    originalPdf: async (sourceId: string) => {
      const response = await fetcher(new URL(`/v2/research/sources/${encodeURIComponent(sourceId)}/original`, `${baseUrl.replace(/\/+$/, "")}/`), { method: "GET", headers: { Accept: "application/pdf" } });
      if (!response.ok || !response.headers.get("content-type")?.toLowerCase().startsWith("application/pdf")) throw new Error("original_unavailable");
      const blob = await response.blob();
      if (blob.size > 1_500_000) throw new Error("original_too_large");
      return blob;
    },
  };
}
