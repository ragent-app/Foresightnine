import type {
  ChallengeOperationResponse,
  ChallengeRecord,
  ErrorResponse,
  Operation,
  OperationResponse,
  ProbabilityAssessment,
  ProbabilityOperationResponse,
  ResearchSession,
  SessionOperationResponse,
  SseEvent,
  Worldline,
  WorldlineBundle,
  WorldlineBundleOperationResponse,
} from "@foresightnine/contracts";

export type ResponseContract =
  | "SessionOperationResponse"
  | "ChallengeOperationResponse"
  | "WorldlineBundleOperationResponse"
  | "ProbabilityOperationResponse"
  | "OperationResponse"
  | "SseEvent";

export class ContractValidationError extends Error {
  public constructor(contract: ResponseContract | string) {
    super(`Invalid ${contract} response.`);
    this.name = "ContractValidationError";
  }
}

type RecordValue = Record<string, unknown>;

function assertKeys(value: RecordValue, contract: string, allowedKeys: readonly string[]): void {
  if (Object.keys(value).some((key) => !allowedKeys.includes(key))) throw new ContractValidationError(contract);
}

function record(value: unknown, contract: string, allowedKeys?: readonly string[]): RecordValue {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new ContractValidationError(contract);
  const item = value as RecordValue;
  if (allowedKeys) assertKeys(item, contract, allowedKeys);
  return item;
}

function string(value: unknown, contract: string): string {
  if (typeof value !== "string" || value.trim().length === 0) throw new ContractValidationError(contract);
  return value;
}

function identifier(value: unknown, contract: string): string {
  const item = string(value, contract);
  if (item.length > 160 || !/^[A-Za-z0-9][A-Za-z0-9._:-]*$/u.test(item)) throw new ContractValidationError(contract);
  return item;
}

function hash(value: unknown, contract: string): string {
  const item = string(value, contract);
  if (!/^[a-f0-9]{64}$/u.test(item)) throw new ContractValidationError(contract);
  return item;
}

function dateTime(value: unknown, contract: string): string {
  const item = string(value, contract);
  if (!/T/u.test(item) || !/(?:Z|[+-]\d{2}:?\d{2})$/u.test(item) || Number.isNaN(Date.parse(item))) throw new ContractValidationError(contract);
  return item;
}

function dateOnly(value: unknown, contract: string): string {
  const item = string(value, contract);
  if (!/^\d{4}-\d{2}-\d{2}$/u.test(item) || Number.isNaN(Date.parse(`${item}T00:00:00.000Z`))) throw new ContractValidationError(contract);
  return item;
}

function number(value: unknown, contract: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) throw new ContractValidationError(contract);
  return value;
}

function boolean(value: unknown, contract: string): boolean {
  if (typeof value !== "boolean") throw new ContractValidationError(contract);
  return value;
}

function array(value: unknown, contract: string): readonly unknown[] {
  if (!Array.isArray(value)) throw new ContractValidationError(contract);
  return value;
}

function schema(value: RecordValue, contract: string): void {
  if (value.schema_version !== "v1") throw new ContractValidationError(contract);
}

function metadata(value: RecordValue, contract: string): void {
  // The enclosing record validator owns the model-specific allow-list. This
  // helper only checks the shared metadata fields, so it also works for
  // nested metadata-bearing objects such as AssetImpact.
  schema(value, contract);
  dateTime(value.created_at, contract);
  array(value.source_refs, contract).forEach((item) => {
    const source = record(item, contract, ["ref_id", "source_type", "label", "url"]);
    identifier(source.ref_id, contract);
    if (!["public", "user_submitted", "model_inference", "market_data", "registry", "factor_library"].includes(String(source.source_type))) throw new ContractValidationError(contract);
    const label = string(source.label, contract);
    if (label.length > 500) throw new ContractValidationError(contract);
    if (source.url !== undefined) { const url = string(source.url, contract); try { new URL(url); } catch { throw new ContractValidationError(contract); } }
  });
  hash(value.content_hash, contract);
}

function operation(value: unknown): Operation {
  const item = record(value, "Operation", ["schema_version", "created_at", "source_refs", "content_hash", "operation_id", "endpoint", "status", "updated_at", "idempotency_key_hash", "cancel_token", "resume_token", "last_event_id"]);
  metadata(item, "Operation");
  identifier(item.operation_id, "Operation");
  const endpoint = string(item.endpoint, "Operation");
  if (!/^\/v1\//u.test(endpoint)) throw new ContractValidationError("Operation");
  if (!["pending", "running", "completed", "failed", "cancelled"].includes(String(item.status))) throw new ContractValidationError("Operation");
  dateTime(item.updated_at, "Operation");
  hash(item.idempotency_key_hash, "Operation");
  const cancelToken = string(item.cancel_token, "Operation");
  const resumeToken = string(item.resume_token, "Operation");
  if (cancelToken.length < 16 || cancelToken.length > 200 || resumeToken.length < 16 || resumeToken.length > 200) throw new ContractValidationError("Operation");
  const lastEventId = string(item.last_event_id, "Operation");
  if (!/^[A-Za-z0-9._:-]+:[0-9]+$/u.test(lastEventId)) throw new ContractValidationError("Operation");
  return item as unknown as Operation;
}

function session(value: unknown): ResearchSession {
  const item = record(value, "ResearchSession", ["schema_version", "created_at", "source_refs", "content_hash", "session_id", "status", "expires_at"]);
  metadata(item, "ResearchSession");
  identifier(item.session_id, "ResearchSession");
  if (!["DRAFT", "CHALLENGING", "EVIDENCE_REQUIRED", "CONFIRMED", "WORLDLINE_GENERATING", "WORLDLINE_READY", "DATA_PREPARING", "CLIENT_COMPUTING", "PORTFOLIO_READY", "EXPORTED"].includes(String(item.status))) throw new ContractValidationError("ResearchSession");
  dateTime(item.expires_at, "ResearchSession");
  return item as unknown as ResearchSession;
}

function rubricScore(value: unknown): void {
  const item = record(value, "ChallengeRecord", ["criterion", "score", "rationale"]);
  if (!["premise_clarity", "time_boundary", "trigger_observability", "causal_chain", "counterfactuals", "evidence_quality", "asset_transmission", "already_priced", "falsifiability", "user_confirmation"].includes(String(item.criterion))) throw new ContractValidationError("ChallengeRecord");
  if (![0, 1, 2, 3, 4].includes(item.score as number)) throw new ContractValidationError("ChallengeRecord");
  const rationale = string(item.rationale, "ChallengeRecord");
  if (rationale.length > 2000) throw new ContractValidationError("ChallengeRecord");
}

function challenge(value: unknown): ChallengeRecord {
  const item = record(value, "ChallengeRecord", ["schema_version", "created_at", "source_refs", "content_hash", "turn_id", "challenge_id", "thesis_id", "claims", "objections", "missing_evidence", "questions", "rubric_scores", "unresolved_issues", "citation_ids", "public_fact_citation_ids", "native_search_used", "provenance", "user_confirmation", "reservation_note", "private_evidence_declared", "status"]);
  metadata(item, "ChallengeRecord");
  ["turn_id", "challenge_id", "thesis_id"].forEach((key) => identifier(item[key], "ChallengeRecord"));
  const provenance = string(item.provenance, "ChallengeRecord");
  if (provenance.length > 500) throw new ContractValidationError("ChallengeRecord");
  const claims = array(item.claims, "ChallengeRecord");
  if (claims.length < 1) throw new ContractValidationError("ChallengeRecord");
  ["claims", "objections", "missing_evidence", "unresolved_issues"].forEach((key) => array(item[key], "ChallengeRecord").forEach((entry) => string(entry, "ChallengeRecord")));
  const questions = array(item.questions, "ChallengeRecord");
  if (questions.length < 1 || questions.length > 3) throw new ContractValidationError("ChallengeRecord");
  questions.forEach((entry) => string(entry, "ChallengeRecord"));
  ["citation_ids", "public_fact_citation_ids"].forEach((key) => array(item[key], "ChallengeRecord").forEach((entry) => identifier(entry, "ChallengeRecord")));
  const rubric = array(item.rubric_scores, "ChallengeRecord");
  if (rubric.length !== 10) throw new ContractValidationError("ChallengeRecord");
  rubric.forEach(rubricScore);
  boolean(item.native_search_used, "ChallengeRecord");
  if (!["pending", "confirmed", "confirmed_with_reservations"].includes(String(item.user_confirmation))) throw new ContractValidationError("ChallengeRecord");
  boolean(item.private_evidence_declared, "ChallengeRecord");
  if (!["CHALLENGING", "EVIDENCE_REQUIRED", "CONFIRMED"].includes(String(item.status))) throw new ContractValidationError("ChallengeRecord");
  if (item.reservation_note !== undefined) { const note = string(item.reservation_note, "ChallengeRecord"); if (note.length > 2000) throw new ContractValidationError("ChallengeRecord"); }
  return item as unknown as ChallengeRecord;
}

function worldline(value: unknown): Worldline {
  const item = record(value, "Worldline", ["schema_version", "created_at", "source_refs", "content_hash", "worldline_id", "bundle_id", "branch_kind", "title", "premise", "baseline_date", "horizon_end", "nodes", "edges", "asset_anchors", "asset_impacts", "event_reveal_schedule", "invalidation_conditions", "provenance", "status"]);
  metadata(item, "Worldline");
  ["worldline_id", "bundle_id"].forEach((key) => identifier(item[key], "Worldline"));
  ["title", "premise", "provenance"].forEach((key) => string(item[key], "Worldline"));
  ["baseline_date", "horizon_end"].forEach((key) => dateOnly(item[key], "Worldline"));
  if (!["weak", "main", "strong"].includes(String(item.branch_kind)) || item.status !== "WORLDLINE_READY") throw new ContractValidationError("Worldline");
  const nodes = array(item.nodes, "Worldline");
  if (nodes.length < 1) throw new ContractValidationError("Worldline");
  nodes.forEach((entry) => {
    const node = record(entry, "Worldline", ["node_id", "time_window", "event_date", "data_as_of", "event", "observable_indicators", "causal_parents", "state_changes", "confidence"]);
    identifier(node.node_id, "Worldline");
    ["time_window", "event"].forEach((key) => string(node[key], "Worldline"));
    ["event_date", "data_as_of"].forEach((key) => dateOnly(node[key], "Worldline"));
    ["observable_indicators", "state_changes"].forEach((key) => array(node[key], "Worldline").forEach((value) => string(value, "Worldline")));
    array(node.causal_parents, "Worldline").forEach((value) => identifier(value, "Worldline"));
    if (!["low", "medium", "high"].includes(String(node.confidence))) throw new ContractValidationError("Worldline");
  });
  array(item.edges, "Worldline").forEach((entry) => {
    const edge = record(entry, "Worldline", ["from_node_id", "to_node_id"]);
    identifier(edge.from_node_id, "Worldline");
    identifier(edge.to_node_id, "Worldline");
  });
  array(item.asset_anchors, "Worldline").forEach((value) => identifier(value, "Worldline"));
  const impacts = array(item.asset_impacts, "Worldline");
  if (impacts.length < 1) throw new ContractValidationError("Worldline");
  impacts.forEach((entry) => {
    const impact = record(entry, "Worldline", ["schema_version", "created_at", "source_refs", "content_hash", "impact_id", "economic_exposure_id", "instrument_id", "direction", "severity", "start_window", "peak_window", "decay", "first_order_reason", "second_order_reason", "already_priced_score", "evidence_refs"]);
    metadata(impact, "Worldline");
    ["impact_id", "economic_exposure_id"].forEach((key) => identifier(impact[key], "Worldline"));
    if (impact.instrument_id !== undefined) identifier(impact.instrument_id, "Worldline");
    if (!["positive", "negative", "mixed"].includes(String(impact.direction)) || !["low", "medium", "high"].includes(String(impact.severity))) throw new ContractValidationError("Worldline");
    ["start_window", "peak_window", "decay", "first_order_reason"].forEach((key) => string(impact[key], "Worldline"));
    if (impact.second_order_reason !== undefined) string(impact.second_order_reason, "Worldline");
    if (![0, 1, 2, 3, 4].includes(impact.already_priced_score as number)) throw new ContractValidationError("Worldline");
    array(impact.evidence_refs, "Worldline").forEach((value) => identifier(value, "Worldline"));
  });
  array(item.event_reveal_schedule, "Worldline").forEach((entry) => {
    const reveal = record(entry, "Worldline", ["event_id", "event_date", "reveal_date", "rule"]);
    identifier(reveal.event_id, "Worldline");
    dateOnly(reveal.event_date, "Worldline");
    dateOnly(reveal.reveal_date, "Worldline");
    if (!["scheduled", "leak", "observed"].includes(String(reveal.rule))) throw new ContractValidationError("Worldline");
  });
  const invalidations = array(item.invalidation_conditions, "Worldline");
  if (invalidations.length < 1) throw new ContractValidationError("Worldline");
  invalidations.forEach((value) => string(value, "Worldline"));
  return item as unknown as Worldline;
}

function probability(value: unknown): ProbabilityAssessment {
  const item = record(value, "ProbabilityAssessment", ["schema_version", "created_at", "source_refs", "content_hash", "assessment_id", "worldline_id", "premise_probability", "premise_probability_range", "conditional_probability", "conditional_probability_range", "joint_probability", "joint_probability_range", "confidence", "base_rate_refs", "adjustment_reasons", "calibration_status"]);
  metadata(item, "ProbabilityAssessment");
  ["assessment_id", "worldline_id"].forEach((key) => identifier(item[key], "ProbabilityAssessment"));
  ["confidence", "calibration_status"].forEach((key) => string(item[key], "ProbabilityAssessment"));
  ["premise_probability", "conditional_probability", "joint_probability"].forEach((key) => { const value = number(item[key], "ProbabilityAssessment"); if (value < 0 || value > 1) throw new ContractValidationError("ProbabilityAssessment"); });
  ["premise_probability_range", "conditional_probability_range", "joint_probability_range"].forEach((key) => {
    const range = record(item[key], "ProbabilityAssessment", ["minimum", "maximum"]);
    const minimum = number(range.minimum, "ProbabilityAssessment");
    const maximum = number(range.maximum, "ProbabilityAssessment");
    if (minimum < 0 || maximum > 1 || minimum > maximum) throw new ContractValidationError("ProbabilityAssessment");
  });
  if (!["low", "medium", "high"].includes(String(item.confidence)) || !["model_estimate_unvalidated", "historically_calibrated"].includes(String(item.calibration_status))) throw new ContractValidationError("ProbabilityAssessment");
  array(item.base_rate_refs, "ProbabilityAssessment").forEach((value) => identifier(value, "ProbabilityAssessment"));
  const reasons = array(item.adjustment_reasons, "ProbabilityAssessment");
  if (reasons.length < 1) throw new ContractValidationError("ProbabilityAssessment");
  reasons.forEach((value) => string(value, "ProbabilityAssessment"));
  return item as unknown as ProbabilityAssessment;
}

const SAFE_DETAIL_MESSAGE = "The API rejected or could not complete this request.";

function safeErrorDetails(value: readonly RecordValue[], retryable: boolean): readonly { readonly location: string; readonly message: string }[] {
  return value.map((detail) => ({
    location: typeof detail.location === "string" && /^[A-Za-z0-9_.:[\]-]{1,80}$/u.test(detail.location) ? detail.location : "server",
    message: retryable ? "Please retry after the indicated delay." : SAFE_DETAIL_MESSAGE,
  }));
}

function errorResponse(value: unknown): ErrorResponse {
  const item = record(value, "ErrorResponse", ["schema_version", "error_code", "message", "request_id", "retryable", "details"]);
  schema(item, "ErrorResponse");
  if (!["bad_request", "validation_error", "not_found", "invalid_state_transition", "confirmation_requirements_not_met", "idempotency_conflict", "invalid_operation_token", "invalid_last_event_id", "contract_violation", "lifecycle_conflict", "quota_exceeded", "unsupported_media_type", "request_too_large", "invalid_request", "rate_limited", "captcha_required", "internal_error"].includes(String(item.error_code))) throw new ContractValidationError("ErrorResponse");
  string(item.message, "ErrorResponse");
  identifier(item.request_id, "ErrorResponse");
  boolean(item.retryable, "ErrorResponse");
  const details = array(item.details, "ErrorResponse").map((entry) => {
    const detail = record(entry, "ErrorResponse", ["location", "message"]);
    string(detail.location, "ErrorResponse");
    string(detail.message, "ErrorResponse");
    return detail;
  });
  return { ...item, message: SAFE_DETAIL_MESSAGE, details: safeErrorDetails(details, item.retryable as boolean) } as unknown as ErrorResponse;
}

function sseEvent(value: unknown): SseEvent {
  const base = record(value, "SseEvent");
  const event = base.event;
  const variantKeys = event === "operation.started" || event === "operation.progress"
    ? ["schema_version", "operation_id", "sequence", "emitted_at", "event", "message", ...(event === "operation.progress" ? ["progress"] : [])]
    : event === "heartbeat"
      ? ["schema_version", "operation_id", "sequence", "emitted_at", "event"]
      : event === "operation.error"
        ? ["schema_version", "operation_id", "sequence", "emitted_at", "event", "error", "retryable"]
        : event === "operation.complete"
          ? ["schema_version", "operation_id", "sequence", "emitted_at", "event", "result_ref", "result_hash"]
          : [];
  const item = record(value, "SseEvent", variantKeys);
  schema(item, "SseEvent");
  identifier(item.operation_id, "SseEvent");
  const sequence = number(item.sequence, "SseEvent");
  if (!Number.isInteger(sequence) || sequence < 1) throw new ContractValidationError("SseEvent");
  dateTime(item.emitted_at, "SseEvent");
  if (!["operation.started", "operation.progress", "heartbeat", "operation.error", "operation.complete"].includes(String(event))) throw new ContractValidationError("SseEvent");
  if (item.event === "operation.started") string(item.message, "SseEvent");
  if (item.event === "operation.progress") { const progress = number(item.progress, "SseEvent"); if (progress < 0 || progress > 1) throw new ContractValidationError("SseEvent"); string(item.message, "SseEvent"); }
  if (item.event === "operation.error") { const error = errorResponse(item.error); boolean(item.retryable, "SseEvent"); return { ...item, error } as unknown as SseEvent; }
  if (item.event === "operation.complete") { identifier(item.result_ref, "SseEvent"); hash(item.result_hash, "SseEvent"); }
  return item as unknown as SseEvent;
}

export function validateResponse<T>(value: unknown, contract: ResponseContract): T {
  const item = record(value, contract,
    contract === "OperationResponse" ? ["operation"]
      : contract === "SessionOperationResponse" ? ["operation", "session"]
        : contract === "ChallengeOperationResponse" ? ["operation", "challenge"]
          : contract === "WorldlineBundleOperationResponse" ? ["operation", "bundle"]
            : contract === "ProbabilityOperationResponse" ? ["operation", "probability"]
              : undefined);
  if (contract === "SseEvent") return sseEvent(item) as T;
  if (contract === "OperationResponse") { operation(item.operation); return item as unknown as T; }
  if (contract === "SessionOperationResponse") { operation(item.operation); session(item.session); return item as unknown as SessionOperationResponse as T; }
  if (contract === "ChallengeOperationResponse") { operation(item.operation); challenge(item.challenge); return item as unknown as ChallengeOperationResponse as T; }
  if (contract === "WorldlineBundleOperationResponse") {
    operation(item.operation);
    const bundle = record(item.bundle, "WorldlineBundle", ["schema_version", "created_at", "source_refs", "content_hash", "bundle_id", "challenge_id", "worldlines", "probability_assessments"]);
    metadata(bundle, "WorldlineBundle");
    ["bundle_id", "challenge_id"].forEach((key) => identifier(bundle[key], "WorldlineBundle"));
    const worldlines = array(bundle.worldlines, "WorldlineBundle");
    const probabilities = array(bundle.probability_assessments, "WorldlineBundle");
    if (worldlines.length !== 3 || probabilities.length !== 3) throw new ContractValidationError("WorldlineBundle");
    worldlines.forEach((entry) => worldline(entry));
    probabilities.forEach((entry) => probability(entry));
    return item as unknown as WorldlineBundleOperationResponse as T;
  }
  if (contract === "ProbabilityOperationResponse") { operation(item.operation); probability(item.probability); return item as unknown as ProbabilityOperationResponse as T; }
  throw new ContractValidationError(contract);
}

export function validateErrorResponse(value: unknown): ErrorResponse | null {
  try { return errorResponse(value); } catch { return null; }
}
