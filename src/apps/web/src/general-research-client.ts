import { sameOriginOAuthFetcher } from "./api-runtime";
import type { ResearchV2 } from "@foresightnine/contracts";

export const MAX_RESEARCH_PDF_BYTES = 5_000_000;

export interface SourcePosition { page?: number; character_start?: number; character_end?: number }
export interface SourceLocation extends SourcePosition { status?: string; matches?: SourcePosition[]; truncated?: boolean; same_page_candidate_index?: number }
export interface SourceContextResult { title: string; before: string; selected: string; after: string; page_start: number | null; page_end: number | null; table_context?: { context_start: number; context_end: number; before: string; selected: string; after: string; truncated_before: boolean } }
export interface SourceTableStructure { status: "available" | "unavailable" | "busy" | "timeout"; source_sha256?: string; truncated_before?: boolean; rows?: { text: string; width: number; cells: { start: number; end: number; text: string; kind: "td" | "th" }[] }[] }
export interface ResearchClaim {
  text: string;
  claim_id?: string;
  responds_to_claim_ids?: string[];
  suggested_responds_to_claim_ids?: string[];
  responds_to_omitted?: number;
  authorship?: string;
  role?: string;
  support_status?: string;
  causal_attribution_unverified?: boolean;
  counterpoint_relevance_unverified?: boolean;
  prior_findings_truncated?: boolean;
  prior_findings_scope_unverified?: boolean;
  source_scope_absence_unverified?: boolean;
  support_review?: { verdict: "supports" | "does_not_support" | "contradicts" | "uncertain"; method: string; independently_verified: false; diagnostic_only?: boolean; reported_verdict?: string; guard_reasons?: string[] };
  citations?: { source_id: string; quotation?: string; quotation_match?: string; partial_token?: boolean; support_status?: string; location?: SourceLocation }[];
  suggested_fact_evidence?: { fact_id: string; source_id: string; value: string; unit: string; period: string; quotation: string; location: SourceLocation; status: string }[];
  suggested_related_quotations?: { source_id: string; quotation: string; location: SourceLocation; matched_amounts: string[]; from_claim_index: number; status: string }[];
  citation_context_warnings?: { source_id: string; citation_index: number; context_metric: string; context_character_start?: number; context_character_end?: number; status: string }[];
  uncited_percentages?: string[];
  percentage_unit_unverified?: string[];
  reused_percentages?: string[];
  uncited_percentage_bounds?: string[];
  uncited_basis_points?: string[];
  uncited_percentage_points?: string[];
  suggested_calculations?: { calculation_index: number; claimed_percentage: string; formula_id: string; input_fact_ids: string[]; status: string }[];
  uncited_amounts?: string[];
  uncited_claim_amounts?: string[];
  uncited_dollar_amounts?: string[];
  uncited_quarters?: string[];
  balance_direction_warnings?: { source_id: string; citation_index: number; basis?: "trade_deficit" | "operating_profit" | "revision_vs_change" | "period_rate_comparison" | "metric_value_attribution" | "quoted_direction_conflict"; claimed_text: string; status: string }[];
  uncited_fact_values?: string[];
  rate_unit_warnings?: { claimed_text: string; status: string }[];
  uncited_exact_dates?: string[];
  publication_date_warnings?: { claimed_text: string; source_id: string; declared_published_at: string; status: string }[];
}
export interface ResearchFact { extraction_method?: string; structured_metadata?: Record<string, unknown>; numeric_normalization?: string; currency_context_unverified?: boolean; currency_quote_missing?: boolean; scale_quote_missing?: boolean; table_alignment_unverified?: boolean; table_period_conflict?: { declared_year: string; column_year: string }; table_structure_alignment?: { status: "matched" | "conflict"; column_date: string; declared_date: string; value_column_start: number; value_column_end: number }; xbrl_source_consistency?: { method: string; concept: string; currency: string; scale: number; entity_cik: string; period_start: string; period_end: string; source_sha256: string }; table_confirmation?: { confirmed_by: "user"; fact_sha256: string; confirmed_at: string }; currency_conflict_unverified?: boolean; currency_unspecified?: boolean; direction_context_unverified?: boolean; direction_confirmation?: { confirmed_by: "user"; fact_sha256: string; confirmed_at: string }; source_value_warnings?: string[]; fact_id: string; label: string; value: string; unit: string; currency?: string | null; period: string; source_id?: string | null; quotation?: string | null; quotation_match?: string; verification: string; authorship?: string; location?: SourceLocation }
export interface ResearchContent {
  summary: string;
  sources: { source_id: string; title?: string; url?: string | null; is_simulated?: boolean; pdf_text_order?: "layout" | "reading" | null; event_at?: string | null; published_at?: string | null; obtained_at?: string | null; revised_at?: string | null }[];
  source_candidates: { title: string; url: string; status: string; published_at?: string | null }[];
  facts: ResearchFact[];
  evidence_differences?: { label: string; period: string; unit: string; currency: string | null; status: string; matching_basis?: string; truncated: boolean; facts: { fact_id: string; source_id: string; value: string; label?: string; period?: string }[] }[];
  calculations: { formula_id: string; formula_version?: string; period_order_check?: string; period_granularity_check?: string; expression: string; inputs: { fact_id: string; value: string; unit: string; period: string }[]; value: string; unit: string; authorship?: string; input_status?: string; legacy_unit_warning?: string }[];
  calculation_differences?: { claim_index: number; calculation_index: number; claimed_value: string; calculated_value: string; unit: string; status: string }[];
  amount_differences?: { claim_index: number; claimed_amount: string; source_fact_ids: string[]; status: string }[];
  claims: ResearchClaim[];
  limitations: string[];
  run: Record<string, unknown>;
}
export interface ResearchVersion {
  source_ids?: string[];
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
  request: { question: string; source_ids: string[]; scope?: string; as_of?: string | null };
  versions: ResearchVersion[];
}
export interface ResearchOperation {
  operation_id: string;
  status: string;
  cancel_token: string;
}
export interface SourceMetadata { original_pdf_available?: boolean; original_html_available?: boolean; derived_from_source_id?: string | null; pdf_text_spacing_warning?: boolean; source_id: string; title?: string; url?: string; status: string; is_simulated?: boolean; event_at?: string | null; published_at?: string | null; obtained_at?: string | null; revised_at?: string | null; retrieval_method?: string; pdf_text_order?: "layout" | "reading" | null; permissions?: { display: boolean; export: boolean; model_input?: boolean } }
export interface DiscoveryCandidate { title: string; url: string; excerpt: string; published_at?: string | null }
export interface IssuerCandidate { cik: string; ticker: string; name: string; exchange: string | null }
export interface RunBudget { selectedSourceIds?: string[]; questionOverride?: string; search: boolean; supportReview: boolean; counterpointFollowup?: boolean; gapFollowup?: boolean; planResearch?: boolean; supportReviewMode?: "batch" | "isolated"; maxModelCalls?: number; requireCounterarguments: boolean; maxElapsedSeconds: number; maxTotalTokens: number; maxInputTokensPerCall?: number; maxCostUsd?: string; focusCitation?: { claim_index: number; citation_index: number }; includeScopeInSearch?: boolean; autoFetchOfficial?: boolean }

export function sourceDiscoveryQuery(question: string, scope: string, includeScope: boolean): string {
  const q = Array.from(question.trim()), s = Array.from(scope.trim());
  if (!includeScope || !s.length) return q.slice(0, 70).join("");
  const prefix = q.slice(0, 45);
  return `${prefix.join("")} ${s.slice(0, 69 - prefix.length).join("")}`;
}

/** Search providers return a date, not an independently verified timestamp. */
export function searchCandidateDate(publishedAt?: string | null, cutoff?: string | null): { date: string | null; relation: "unknown" | "unrestricted" | "before" | "same_or_after" } {
  const date = publishedAt?.match(/^\d{4}-\d{2}-\d{2}(?=T|$)/)?.[0] ?? null;
  const dateMs = date ? Date.parse(`${date}T00:00:00Z`) : NaN;
  if (!date || !Number.isFinite(dateMs) || new Date(dateMs).toISOString().slice(0, 10) !== date) return { date: null, relation: "unknown" };
  if (!cutoff) return { date, relation: "unrestricted" };
  const cutoffMs = Date.parse(cutoff);
  if (!Number.isFinite(cutoffMs)) return { date, relation: "unknown" };
  return { date, relation: date < new Date(cutoffMs).toISOString().slice(0, 10) ? "before" : "same_or_after" };
}
export interface RunInputSnapshot {
  captured_at: string;
  parent_version_id: string;
  as_of: string | null;
  planned_official_source_id?: string;
  budget: ResearchV2.ResearchRunRequest;
  sources: { source_id: string; content_sha256: string; is_simulated?: boolean; pdf_text_order?: "layout" | "reading" | null; permissions: ResearchV2.SourcePermissions }[];
  model: string | null;
  provider: string | null;
  provider_limits?: { max_input_tokens?: number | null; max_output_tokens?: number | null };
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
    sourceText: (topicId: string, versionId: string, sourceId: string) => request<{ source_id: string; text?: string }>(
      `/v2/research/topics/${encodeURIComponent(topicId)}/versions/${encodeURIComponent(versionId)}/sources/${encodeURIComponent(sourceId)}/text`),
    sourceContext: (sourceId: string, start: number, end: number) => request<SourceContextResult>(
      `/v2/research/sources/${encodeURIComponent(sourceId)}/context?start=${start}&end=${end}`),
    sourceTableStructure: (sourceId: string, start: number, end: number) => request<SourceTableStructure>(
      `/v2/research/sources/${encodeURIComponent(sourceId)}/table-structure?start=${start}&end=${end}`),
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
    uploadPdf: (title: string, pdfBase64: string, modelInput: boolean, allowExport: boolean, publishedAt: string, eventAt = "", revisedAt = "", isSimulated = false, textOrder: "layout" | "reading" = "layout") =>
      request<{ source_id: string }>("/v2/research/sources/pdf", { title, pdf_base64: pdfBase64,
        ...sourceTimes(publishedAt, eventAt, revisedAt), is_simulated: isSimulated, text_order: textOrder,
        permissions: { store: true, display: true, model_input: modelInput, export: allowExport } }),
    uploadHtml: (title: string, htmlBase64: string, modelInput: boolean, allowExport: boolean, publishedAt: string, eventAt = "", revisedAt = "", isSimulated = false) =>
      request<{ source_id: string }>("/v2/research/sources/html", { title, html_base64: htmlBase64,
        ...sourceTimes(publishedAt, eventAt, revisedAt), is_simulated: isSimulated,
        permissions: { store: true, display: true, model_input: modelInput, export: allowExport } }),
    reextractPdf: (sourceId: string, textOrder: "layout" | "reading", key: string) => request<{ source_id: string }>(
      `/v2/research/sources/${encodeURIComponent(sourceId)}/reextract`, { text_order: textOrder }, key),
    search: (query: string) => request<{ project_search_available: boolean; candidates: DiscoveryCandidate[] }>(
      `/v1/search/candidates?query=${encodeURIComponent(Array.from(query).slice(0, 70).join(""))}&limit=5`),
    create: (question: string, sourceIds: string[], asOf: string, scope = "") => request<ResearchTopic>("/v2/research/topics", { question, source_ids: sourceIds,
      ...(scope ? { scope } : {}),
      ...(asOf ? { as_of: new Date(asOf).toISOString() } : {}) }),
    attach: (topicId: string, parent: string, sourceIds: string[]) => request<ResearchTopic>(
      `/v2/research/topics/${encodeURIComponent(topicId)}/sources`, { parent_version_id: parent, source_ids: sourceIds }),
    confirmDirection: (topicId: string, parent: string, factId: string) => request<ResearchTopic>(
      `/v2/research/topics/${encodeURIComponent(topicId)}/facts/confirm-direction`, { parent_version_id: parent, fact_id: factId }),
    confirmTable: (topicId: string, parent: string, factId: string) => request<ResearchTopic>(
      `/v2/research/topics/${encodeURIComponent(topicId)}/facts/confirm-table`, { parent_version_id: parent, fact_id: factId }),
    createCalculation: (topicId: string, parent: string, formula: "growth_percent" | "margin_percent" | "difference", firstFactId: string, secondFactId: string) => request<ResearchTopic>(
      `/v2/research/topics/${encodeURIComponent(topicId)}/calculations`, { parent_version_id: parent, formula, first_fact_id: firstFactId, second_fact_id: secondFactId }),
    recalculate: (topicId: string, parent: string, index: number) => request<ResearchTopic>(
      `/v2/research/topics/${encodeURIComponent(topicId)}/calculations/recalculate`, { parent_version_id: parent, calculation_index: index }),
    run: (topicId: string, parent: string, key: string, budget: RunBudget) => request<{ operation: ResearchOperation }>(
      `/v2/research/topics/${encodeURIComponent(topicId)}/runs`, { parent_version_id: parent,
        max_search_calls: budget.search ? 1 : 0, max_elapsed_seconds: budget.maxElapsedSeconds,
        max_total_tokens: budget.maxTotalTokens, max_model_calls: budget.supportReview && budget.supportReviewMode === "isolated" ? budget.maxModelCalls ?? 2 : 1 + Number(budget.supportReview) + Number(budget.counterpointFollowup ?? false) + Number(budget.gapFollowup ?? false) + Number(budget.planResearch ?? false), support_review: budget.supportReview,
        ...(budget.counterpointFollowup ? { counterpoint_followup: true } : {}),
        ...(budget.gapFollowup ? { gap_followup: true } : {}),
        ...(budget.planResearch ? { plan_research: true } : {}),
        ...(budget.supportReview && budget.supportReviewMode === "isolated" ? { support_review_mode: "isolated" } : {}),
        require_counterarguments: budget.requireCounterarguments,
        ...(budget.maxInputTokensPerCall !== undefined ? { max_input_tokens_per_call: budget.maxInputTokensPerCall } : {}),
        ...(budget.selectedSourceIds !== undefined ? { selected_source_ids: budget.selectedSourceIds } : {}),
        ...(budget.questionOverride ? { question_override: budget.questionOverride } : {}),
        ...(budget.includeScopeInSearch ? { include_scope_in_search: true } : {}),
        ...(budget.autoFetchOfficial ? { auto_fetch_official: true } : {}),
        ...(budget.focusCitation ? { focus_citation: budget.focusCitation } : {}),
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
      if (blob.size > MAX_RESEARCH_PDF_BYTES) throw new Error("original_too_large");
      return blob;
    },
    originalHtml: async (sourceId: string) => {
      const response = await fetcher(new URL(`/v2/research/sources/${encodeURIComponent(sourceId)}/original`, `${baseUrl.replace(/\/+$/, "")}/`), { method: "GET", headers: { Accept: "application/octet-stream" } });
      if (!response.ok || !response.headers.get("content-type")?.toLowerCase().startsWith("application/octet-stream")) throw new Error("original_unavailable");
      const blob = await response.blob();
      if (blob.size > MAX_RESEARCH_PDF_BYTES) throw new Error("original_too_large");
      return blob;
    },
  };
}
