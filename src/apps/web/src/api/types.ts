// Types for endpoints that are not in the generated contract. Each mirrors backend code.

// apps/api/app/research_progress.py build_progress
export type OperationStatus = "pending" | "running" | "completed" | "failed" | "cancelled";

// apps/api/app/research_progress.py build_progress
export type ProgressEvent =
  | { index: number; kind: "model"; finish_reason: string | null; usage: number | null; input_tokens: number | null; output_tokens: number | null; settlement: string | null }
  | { index: number; kind: "model_failure"; code: string | null }
  | { index: number; kind: "tool"; name: string | null; status: string | null; reason: string | null; rationale: string | null; denied_count: number | null }
  | { index: number; kind: "submission"; status: string | null };

// apps/api/app/research_progress.py build_progress
export interface RunProgress {
  operation_id: string;
  operation_status: OperationStatus;
  turn: { index: number; question: string };
  usage: { model_calls: number; search_calls: number; total_tokens: number };
  current: { kind: "model" } | { kind: "tool"; name: string } | null;
  events: ProgressEvent[];
  event_count: number;
  started_at: string | null;
  updated_at: string | null;
  version_id: string | null;
}

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
  sec_available: boolean;
  historical_prices_available: boolean;
  sources: { name: string; output_type: string; permission: string; configured: boolean; hosts: string[]; coverage: string }[];
}
