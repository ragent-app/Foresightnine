// Types for endpoints that are not in the generated contract. Each mirrors backend code.

// apps/api/app/research_progress.py build_progress
export type OperationStatus = "pending" | "running" | "completed" | "failed" | "cancelled";

export type ModelPhase = "connecting" | "thinking" | "responding" | "retrying";

export interface NewsSearchSummary {
  kind: "news_search";
  found_count: number;
  eligible_count: number;
  denied_count: number;
  cluster_count?: number;
  attributions?: string[];
  leads_preview: { title: string; host: string; published_at: string | null }[];
}

export interface SearchLibrarySummary {
  kind: "search_library";
  query: string;
  hit_count: number;
  source_count: number;
  unsearched_pages: number;
  complete: boolean;
  hits_preview: { title: string; page: number | null }[];
}

export interface AcquireSourcesSummary {
  kind: "acquire_sources";
  acquired_count: number;
  failed_count: number;
  items: { title: string; host: string; status: string; reason: string | null; total_characters: number | null }[];
}

export interface SecCompanyConceptSummary {
  kind: "sec_company_concept";
  entity: string;
  taxonomy: string;
  tag: string;
  unit: string;
  status: string;
  title: string | null;
  total_characters: number | null;
}

export interface HistoricalPricesSummary {
  kind: "historical_prices";
  instrument_id: string;
  start_date: string;
  end_date: string;
  status: string;
  title: string | null;
}

export interface ReadSourcesSummary {
  kind: "read_sources";
  read_count: number;
  items: { source_id: string; title: string; start: number; end: number; total_characters: number | null }[];
}

export interface RecordFactsSummary {
  kind: "record_facts";
  recorded_count: number;
  facts_preview: { label: string; value: string; unit: string; currency: string | null; period: string }[];
}

export interface CalculateFinancialsSummary {
  kind: "calculate_financials";
  calculated_count: number;
  calcs_preview: { formula_id: string; value: string; unit: string; currency: string | null; expression: string }[];
}

export interface CreateChartSummary {
  kind: "create_chart";
  chart_id: string;
  chart_type: string;
  title: string;
  series_count: number;
  point_count: number;
}

export type ToolEventSummary =
  | NewsSearchSummary
  | SearchLibrarySummary
  | AcquireSourcesSummary
  | SecCompanyConceptSummary
  | HistoricalPricesSummary
  | ReadSourcesSummary
  | RecordFactsSummary
  | CalculateFinancialsSummary
  | CreateChartSummary;

export interface InterimSummary {
  leads_count: number;
  acquired_sources_count: number;
  read_sources_count: number;
  facts_count: number;
  calculations_count: number;
}

export type CurrentActivity =
  | { kind: "model"; phase?: ModelPhase | null; retry_attempt?: number | null; started_at?: string | null }
  | { kind: "tool"; name: string; rationale?: string | null; target?: string | null; started_at?: string | null; hosts?: string[] };

// apps/api/app/research_progress.py build_progress
export type ProgressEvent =
  | { index: number; kind: "model"; finish_reason: string | null; usage: number | null; input_tokens: number | null; output_tokens: number | null; settlement: string | null; note?: string | null }
  | { index: number; kind: "model_failure"; code: string | null }
  | { index: number; kind: "tool"; name: string | null; status: string | null; reason: string | null; rationale: string | null; denied_count: number | null; summary?: ToolEventSummary | null }
  | { index: number; kind: "submission"; status: string | null };

export interface RunUsage {
  model_calls: number;
  search_calls: number;
  total_tokens: number;
}

// apps/api/app/research_progress.py build_progress
export interface ResearchInstruction {
  instruction_id: string; turn_id: string; operation_id: string; sequence: number; text: string; received_at: string; status: string;
  delivery_status?: string | null; reason?: string | null; references?: (string | { fact_id?: string; claim_id?: string; calculation_id?: string })[];
  text_locations?: { field?: "report" | "partial_notes"; instruction_sequence?: number; start: number; end: number; excerpt: string }[];
}

export interface RunProgress {
  operation_id: string;
  operation_status: OperationStatus;
  turn: { index: number; question: string; turn_id?: string };
  logical_status?: "running" | "awaiting_user" | "completed" | "stopped" | "failed";
  run_mode?: "earnings_squad" | "legacy_single_loop" | null;
  squad_run_id?: string | null;
  /** The topic's title, which the first turn summarises from its question while it runs. */
  topic_title?: string | null;
  clarification?: { question: string; why_it_matters: string; requirement_id: string } | null;
  current_operation_id?: string | null;
  instructions?: ResearchInstruction[];
  usage: RunUsage;
  current: CurrentActivity | null;
  interim?: InterimSummary | null;
  events: ProgressEvent[];
  event_count: number;
  started_at: string | null;
  updated_at: string | null;
  version_id: string | null;
}

export type StreamChannel = "thinking" | "text" | "tool_status" | "draft_report";

export interface TransientStreamState {
  callSeq: number | null;
  thinking: string;
  text: string;
  toolStatus: string | null;
  draftReport: string;
  /** The last draft sent back by review; it stays readable until the writer starts the next one. */
  heldDraft?: string;
  activeChannel: StreamChannel | null;
  retractedNotice: string | null;
  lastSeq: number;
}

export type TerminalOperationStatus = "completed" | "failed" | "cancelled";

export type ResearchSseFrame =
  | { event: "progress.snapshot"; id: string | null; data: RunProgress }
  | {
      event: "step.current";
      id: string | null;
      data: {
        operation_id: string;
        operation_status: OperationStatus;
        current: CurrentActivity | null;
        usage: RunUsage;
        interim?: InterimSummary | null;
        updated_at: string | null;
      };
    }
  | {
      event: "step.event";
      id: string | null;
      data: {
        operation_id: string;
        event: ProgressEvent;
        event_count: number;
        usage: RunUsage;
        interim?: InterimSummary | null;
        updated_at: string | null;
      };
    }
  | {
      event: "stream.delta";
      id: string | null;
      data: {
        operation_id: string;
        call_seq: number;
        channel: StreamChannel;
        delta: string;
        seq: number;
      };
    }
  | {
      event: "stream.reset";
      id: string | null;
      data: {
        operation_id: string;
        call_seq: number;
        reason: string;
      };
    }
  | {
      event: "run.terminal";
      id: string | null;
      data: {
        operation_id: string;
        operation_status: TerminalOperationStatus;
        version_id: string | null;
      };
    };

// apps/api/app/main.py get_research_recovery_status
export interface RecoveryStatus {
  can_continue: boolean;
  can_deliver: boolean;
  reason: string | null;
  checkpoint: string | null;
  version_id: string | null;
}

// apps/api/app/main.py list_research_version_sources
export interface SourcePermissionFlags {
  store: boolean;
  model_input: boolean;
  display: boolean;
  export: boolean;
}

// apps/api/app/main.py list_research_version_sources
export interface AvailableVersionSource {
  status: "available";
  source_id: string;
  title: string;
  kind: "user_supplied" | "official" | "market_data";
  is_simulated: boolean;
  published_at: string | null;
  event_at: string | null;
  revised_at: string | null;
  obtained_at: string;
  content_sha256: string;
  retrieval_method: string;
  pdf_text_order: string | null;
  derived_from_source_id: string | null;
  permissions: SourcePermissionFlags;
  url?: string;
  original_pdf_available?: boolean;
  original_html_available?: boolean;
  original_document_available?: boolean;
  pdf_text_spacing_warning?: boolean;
  verification: string;
  extraction_disclosure?: string;
}

// apps/api/app/main.py list_research_version_sources
export type VersionSource = AvailableVersionSource | { status: "unavailable"; source_id: string };

// apps/api/app/main.py list_research_version_sources (source text endpoint)
export interface SourceText {
  source_id: string;
  text: string;
  content_sha256: string;
}

// apps/api/app/main.py _visible_research_source: creation endpoints return the
// source metadata; `text` is present only when display is permitted and the
// endpoint does not strip it (PDF, HTML and SEC filing uploads never return it).
export type CreatedSource = Omit<AvailableVersionSource, "status"> & { text?: string; url?: string | null };

// apps/api/app/research_uploads.py HtmlUploadRequest (not in the generated contract)
export interface HtmlUploadRequest {
  title: string;
  html_base64: string;
  is_simulated?: boolean;
  published_at?: string | null;
  event_at?: string | null;
  revised_at?: string | null;
  permissions: SourcePermissionFlags;
}

// apps/api/app/research_sec.py search_sec_identities
export interface SecIdentity {
  cik: string;
  ticker: string;
  name: string;
  exchange: string | null;
}

export interface SecIdentitySearchResult {
  candidates: SecIdentity[];
  source_url: string;
  retrieved_at: string;
  response_sha256: string;
  limitation: string;
}

// apps/api/app/main.py get_research_capabilities (research_source_catalog.SourceCapability)
export interface ResearchCapabilities {
  schema_version: "v1";
  search_available: boolean;
  web_fetch_enabled?: boolean;
  sec_available: boolean;
  market_data_available: boolean;
  market_data_key_required: boolean;
  sec_financials_available: boolean;
  filings_available: boolean;
  sources: { name: string; output_type: string; permission: string; configured: boolean; hosts: string[]; coverage: string }[];
}

// apps/api/app/research_reconciliation.py ui_view (versioned; saved on ResearchTurn.reconciliation)
export type ReconciliationBadge = "single_source" | "consistent" | "rounding_only" | "differs_minor" | "differs_material";

export interface ReconciliationCell {
  fact_id: string;
  source_id: string;
  source_class: string;
  host: string | null;
  display_value: string;
  published_at: string | null;
  as_of: string | null;
  is_reference: boolean;
  low_ocr_confidence: boolean;
  quotation_excerpt: string | null;
}

export interface ReconciliationRow {
  row_id: string;
  title: { zh: string; en: string };
  status: "single_source" | "consistent" | "rounding_only" | "differs";
  severity: string | null;
  badge: ReconciliationBadge;
  cells: ReconciliationCell[];
  reference: { value: string | null; source_class: string | null } | null;
  independent_source_count: number;
}

export interface ReconciliationView {
  version: number;
  counts: Record<string, number>;
  rows: ReconciliationRow[];
}

// apps/api/app/research_coverage_map.py ui_view (versioned; saved on ResearchTurn.coverage_map)
export interface CoverageRow {
  category: string;
  label: { zh: string; en: string };
  status: string;
  status_label: { zh: string; en: string };
  data_as_of: string | null;
  is_stale: boolean | null;
  trading_days_behind: number | null;
  days_behind_clock: number | null;
  not_established: string[];
  sources_obtained: number;
}

export interface CoverageDocument {
  origin: string | null;
  ingestion_kind: string | null;
  retrieval_method: string | null;
  permissions: Record<string, boolean> | null;
  source_id: string;
  category: string;
  title: string | null;
  chars_total: number | null;
  read_fraction: number | null;
  pages: {
    total: number;
    by_status: Record<string, number>;
    read: number;
    unread_text_pages: number;
    unsearched_count: number;
    ocr_low_confidence_pages: number[];
  } | null;
}

export interface CoverageMapView {
  version: number;
  run_status: string | null;
  clock_date: string | null;
  rows: CoverageRow[];
  documents: CoverageDocument[];
}

export interface TurnAttributionView {
  id: string;
  text: string;
  url: string | null;
}
