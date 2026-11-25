import { apiRequest, newIdempotencyKey } from "./http";

export type CollisionSeverity = "red" | "yellow" | "green" | "info" | "not_applicable";
export type BasisType = "gaap" | "non_gaap" | "operating_kpi" | "unknown" | "inherited_call_default";
export type CallDefaultBasis = "non_gaap" | "gaap" | "mixed" | "unspecified";
export type LedgerMatchStatus =
  | "same_period_10q_matched"
  | "awaiting_current_10q_using_8k_and_prior_10q"
  | "fpi_20f_6k_limited"
  | "financial_sic_gated"
  | "non_sec_entity";
export type EvasionLevel = "direct" | "intermediate" | "fully_evasive";
export type EvasionSubtype =
  | "none"
  | "omission"
  | "selective_part_omission"
  | "vagueness_obfuscation"
  | "topic_pivot"
  | "restatement";
export type CommitmentStatus =
  | "pending_10q"
  | "pending_next_call"
  | "met"
  | "missed"
  | "manual_nongaap_review"
  | "silent_drop_high_risk"
  | "likely_concluded_or_unasked"
  | "muted";

export interface PreviewSourceSummary {
  source_id: string;
  title: string;
  retrieval_method: string;
  archetype: string;
  authority_tier: string;
  confidence: number;
  strong_feature_count: number;
  auto_trigger_eligible: boolean;
  negative_filter_hit?: string | null;
  sellside_timing?: string | null;
  call_default_basis: CallDefaultBasis;
  detected_symbol?: string | null;
  fiscal_period_key?: string | null;
  call_date_iso?: string | null;
  qa_exchange_count: number;
  pending_ocr_pages: number;
}

export interface CallAuditPreviewResponse {
  auto_trigger_eligible: boolean;
  entity_key?: string | null;
  detected_entity_key?: string | null;
  detected_symbol?: string | null;
  detected_cik?: string | null;
  fiscal_period_key?: string | null;
  call_date_iso?: string | null;
  call_default_basis: CallDefaultBasis;
  management_source_ids: string[];
  mgmt_source_ids?: string[];
  sellside_source_ids: string[];
  total_pending_ocr_pages: number;
  sources: PreviewSourceSummary[];
}

export interface GroundedQuoteRef {
  source_id: string;
  quotation: string;
  verified: boolean;
  raw_char_start?: number | null;
  raw_char_end?: number | null;
  page_number?: number | null;
  slide_number?: number | null;
  timestamp_start?: string | null;
  match_space?: "raw" | "norm_text" | "norm_table" | null;
  quote_context_warning?: string | null;
  surrounding_context?: string | null;
}

export interface CollisionFinding {
  finding_id: string;
  track: string;
  collision_kind: string;
  severity: CollisionSeverity;
  title: string;
  management_statement: string;
  ledger_evidence: string;
  formula_note?: string | null;
  basis_badge?: string | null;
  period_badge?: string | null;
  restated_later?: boolean;
  restatement_note?: string | null;
  linked_pillar?: string | null;
  skeptic_note?: string | null;
  quotes: GroundedQuoteRef[];
  priority_rank?: number;
}

export interface QaEvasionFinding {
  finding_id: string;
  exchange_index: number;
  analyst_name: string;
  analyst_firm?: string | null;
  executive_name: string;
  executive_role: string;
  evasion_level: EvasionLevel;
  behavior_subtype: EvasionSubtype;
  display_severity: CollisionSeverity;
  legitimate_confidentiality: boolean;
  collapsed_by_default: boolean;
  sub_questions: string[];
  unanswered_sub_questions: string[];
  topic_tags: string[];
  followup_count: number;
  rationale: string;
  question_quote: GroundedQuoteRef;
  answer_quote: GroundedQuoteRef;
}

export interface GuidanceRangeSnapshot {
  fiscal_period_key: string;
  call_date?: string | null;
  value_low?: string | null;
  value_high?: string | null;
  unit?: string | null;
  scale?: string | null;
}

export interface VerbalCommitmentRecord {
  commitment_id: string;
  owner_id?: string | null;
  entity_key: string;
  source_id: string;
  source_fiscal_period?: string | null;
  call_date?: string | null;
  metric_canonical: string;
  kpi_key?: string | null;
  basis: BasisType;
  effective_basis: "gaap" | "non_gaap" | "operating_kpi" | "unknown";
  period_scope: string;
  target_fy?: number | null;
  target_fq?: number | null;
  target_period_key?: string | null;
  operator: string;
  value_low?: string | null;
  value_high?: string | null;
  unit?: string | null;
  scale?: string | null;
  currency?: string | null;
  verification_tier: string;
  status: CommitmentStatus;
  actual_value?: string | null;
  actual_period_end?: string | null;
  verified_against_accession?: string | null;
  verified_at_filed_date?: string | null;
  delta_note?: string | null;
  is_high_profile_theme?: boolean;
  theme_key?: string | null;
  qualitative_milestone?: string | null;
  commitment_text: string;
  linked_pillar?: string | null;
  muted: boolean;
  range_drift_note?: string | null;
  range_history?: GuidanceRangeSnapshot[];
  skeptic_note?: string | null;
  quote?: GroundedQuoteRef | null;
}

export interface SpeakerBaselineShiftFinding {
  executive_name: string;
  speaker_role: string;
  quarters_observed: number;
  current_hedge_density: number;
  historical_avg_hedge_density: number;
  hedge_density_delta: number;
  current_evasion_rate_pct: number;
  historical_avg_evasion_rate_pct: number;
  evasion_rate_delta_pp: number;
  severity: CollisionSeverity;
  summary: string;
}

export interface ExpectationComparisonItem {
  tier: "v25_historical_credibility" | "analyst_cluster_hotspot" | "sellside_third_party" | "v3b_byok_consensus";
  authority_tier: "sec_public_prior" | "call_qa_signal" | "third_party_opinion" | "byok_consensus";
  title: string;
  summary: string;
  sellside_firm?: string | null;
  sellside_timing?: "pre_earnings_preview" | "post_earnings_review" | "unspecified" | null;
  analyst_count?: number | null;
  exportable?: boolean;
  quote?: GroundedQuoteRef | null;
}

export interface CallVerificationReport {
  schema_version: "1";
  owner_id?: string | null;
  entity_key: string;
  cik?: string | null;
  symbol?: string | null;
  company_name?: string | null;
  fiscal_period_key?: string | null;
  call_date?: string | null;
  call_default_basis: CallDefaultBasis;
  ledger_status: LedgerMatchStatus;
  awaiting_current_10q: boolean;
  overall_light: CollisionSeverity;
  headline: string;
  red_count: number;
  yellow_count: number;
  green_count: number;
  evasion_red_yellow_count: number;
  evasion_total_exchanges: number;
  evasion_distribution: Record<string, number>;
  confidentiality_whitelisted_count: number;
  collision_findings: CollisionFinding[];
  qa_evasions: QaEvasionFinding[];
  commitments: VerbalCommitmentRecord[];
  speaker_baseline_shifts: SpeakerBaselineShiftFinding[];
  expectation_benchmarks: ExpectationComparisonItem[];
  skeptic_calibration: string[];
  open_gaps: string[];
  source_ids: string[];
  source_hashes?: Record<string, string>;
  verified_quote_count: number;
  total_quote_count: number;
  rules_sha256: string;
  evaluation_rules_version: string;
  context_budget_version: string;
  text_only_evasion_disclaimer: string;
  model_id?: string | null;
  total_tokens: number;
  cache_read_input_tokens?: number;
  cache_creation_input_tokens?: number;
  llm_call_count: number;
  generated_at: string;
}

export interface CallAuditReportEnvelope {
  available: boolean;
  awaiting_current_10q: boolean;
  last_filing_fingerprint_check_at?: string | null;
  report: CallVerificationReport | null;
  ledger?: Record<string, unknown> | null;
  markdown?: string | null;
}

export interface CallAuditRunRequest {
  source_ids: string[];
  topic_id?: string | null;
  entity_key?: string | null;
  symbol?: string | null;
  cik?: string | null;
  fiscal_year?: number | null;
  fiscal_quarter?: number | null;
  as_of?: string | null;
}

export interface CallAuditStageEvent {
  sequence: number;
  stage: string;
  payload?: Record<string, unknown>;
  timestamp?: string;
}

export interface CallAuditRunResponse {
  operation_id: string;
  status: string;
  entity_key?: string | null;
  cached?: boolean;
  replayed?: boolean;
  report?: CallVerificationReport | null;
  ledger?: Record<string, unknown> | null;
  markdown?: string | null;
}

export interface CallAuditOperationView {
  operation_id: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled" | string;
  entity_key?: string | null;
  topic_id?: string | null;
  stage?: string | null;
  stages: CallAuditStageEvent[];
  report?: CallVerificationReport | null;
  ledger?: Record<string, unknown> | null;
  markdown?: string | null;
  error?: string | null;
  updated_at?: string | null;
}

export interface CallAuditSseFrame {
  event: string;
  id: string | null;
  data: Record<string, unknown>;
}

export interface CheckFilingsResponse {
  checked_ciks: string[];
  triggered_ciks: string[];
  inbox_item_ids: string[];
  cooldown_minutes: number;
}

const enc = encodeURIComponent;

export function previewCallAudit(sourceIds: string[], signal?: AbortSignal): Promise<CallAuditPreviewResponse> {
  return apiRequest<CallAuditPreviewResponse>("/v2/research/call-audit/preview", {
    method: "POST",
    body: { source_ids: sourceIds },
    ...(signal ? { signal } : {}),
  });
}

export function runCallAudit(
  body: CallAuditRunRequest,
  idempotencyKey = newIdempotencyKey(),
): Promise<CallAuditRunResponse> {
  return apiRequest<CallAuditRunResponse>("/v2/research/call-audit/run", {
    method: "POST",
    body,
    idempotencyKey,
  });
}

export function getCallAuditOperation(
  operationId: string,
  signal?: AbortSignal,
): Promise<CallAuditOperationView> {
  return apiRequest<CallAuditOperationView>(
    `/v2/research/call-audit/operations/${enc(operationId)}`,
    signal ? { signal } : {},
  );
}

export function cancelCallAuditOperation(operationId: string): Promise<{ operation_id: string; status: string }> {
  return apiRequest<{ operation_id: string; status: string }>(
    `/v2/research/call-audit/operations/${enc(operationId)}/cancel`,
    { method: "POST", body: {} },
  );
}

export function getCallAuditReport(
  params: { topic_id?: string; entity_key?: string; symbol?: string; cache_only?: boolean; as_of?: string },
  signal?: AbortSignal,
): Promise<CallAuditReportEnvelope> {
  const qs = new URLSearchParams();
  if (params.topic_id) qs.set("topic_id", params.topic_id);
  if (params.entity_key) qs.set("entity_key", params.entity_key);
  if (params.symbol) qs.set("symbol", params.symbol);
  qs.set("cache_only", String(params.cache_only ?? true));
  if (params.as_of) qs.set("as_of", params.as_of);
  return apiRequest<CallAuditReportEnvelope>(
    `/v2/research/call-audit/report?${qs.toString()}`,
    signal ? { signal } : {},
  );
}

export function checkCallAuditFilings(body: {
  topic_id?: string | null;
  entity_keys?: string[];
  force?: boolean;
}): Promise<CheckFilingsResponse> {
  return apiRequest<CheckFilingsResponse>("/v2/research/call-audit/check-filings", {
    method: "POST",
    body,
  });
}

export function patchCallAuditCommitment(
  commitmentId: string,
  entityKey: string,
  muted: boolean,
): Promise<{ commitment_id: string; entity_key: string; muted: boolean }> {
  return apiRequest<{ commitment_id: string; entity_key: string; muted: boolean }>(
    `/v2/research/call-audit/commitments/${enc(commitmentId)}`,
    { method: "PATCH", body: { entity_key: entityKey, muted } },
  );
}

export function mergeCallAuditEntities(body: {
  source_entity_key: string;
  target_entity_key: string;
  aliases?: string[];
}): Promise<{ source_entity_key: string; target_entity_key: string; merged_commitments: number }> {
  return apiRequest<{ source_entity_key: string; target_entity_key: string; merged_commitments: number }>(
    "/v2/research/call-audit/entities/merge",
    { method: "POST", body },
  );
}

export function parseCallAuditSseFrame(rawFrame: string): CallAuditSseFrame | null {
  if (!rawFrame.trim()) return null;
  let event = "progress";
  let id: string | null = null;
  const dataLines: string[] = [];
  for (const line of rawFrame.split(/\r?\n/u)) {
    if (!line || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    const value = colon === -1 ? "" : line[colon + 1] === " " ? line.slice(colon + 2) : line.slice(colon + 1);
    if (field === "event") event = value;
    else if (field === "data") dataLines.push(value);
    else if (field === "id" && !value.includes("\0")) id = value;
  }
  if (dataLines.length === 0) return null;
  try {
    const parsed = JSON.parse(dataLines.join("\n"));
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return null;
    return { event, id, data: parsed as Record<string, unknown> };
  } catch {
    return null;
  }
}

export async function* openCallAuditEventStream(params: {
  operationId: string;
  after?: number;
  lastEventId?: string | null;
  signal?: AbortSignal;
}): AsyncGenerator<CallAuditSseFrame> {
  const { operationId, after = 0, lastEventId, signal } = params;
  const path = `/v2/research/call-audit/operations/${enc(operationId)}/events?after=${enc(String(after))}`;
  const response = await apiRequest<Response>(path, {
    accept: "text/event-stream",
    raw: true,
    ...(lastEventId ? { headers: { "Last-Event-ID": lastEventId } } : {}),
    ...(signal ? { signal } : {}),
  });
  const contentType = response.headers.get("content-type")?.toLowerCase() ?? "";
  if (!contentType.includes("text/event-stream") || !response.body) {
    await response.body?.cancel().catch(() => undefined);
    throw new Error("call_audit_sse_unavailable");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const onAbort = () => {
    void reader.cancel().catch(() => undefined);
  };
  if (signal) {
    if (signal.aborted) {
      await reader.cancel().catch(() => undefined);
      return;
    }
    signal.addEventListener("abort", onAbort, { once: true });
  }
  try {
    while (true) {
      if (signal?.aborted) break;
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      const frames = buffer.split(/\r?\n\r?\n/u);
      buffer = frames.pop() ?? "";
      for (const rawFrame of frames) {
        const frame = parseCallAuditSseFrame(rawFrame);
        if (frame) yield frame;
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) {
      for (const rawFrame of buffer.split(/\r?\n\r?\n/u)) {
        const frame = parseCallAuditSseFrame(rawFrame);
        if (frame) yield frame;
      }
    }
  } finally {
    signal?.removeEventListener("abort", onAbort);
    await reader.cancel().catch(() => undefined);
  }
}

export function formatQuoteLocationBadge(quote: GroundedQuoteRef | null | undefined): string | null {
  if (!quote) return null;
  const parts: string[] = [];
  if (typeof quote.slide_number === "number" && quote.slide_number > 0) parts.push(`Slide ${quote.slide_number}`);
  else if (typeof quote.page_number === "number" && quote.page_number > 0) parts.push(`第 ${quote.page_number} 页`);
  if (quote.timestamp_start) parts.push(quote.timestamp_start);
  return parts.length > 0 ? parts.join(" · ") : null;
}
