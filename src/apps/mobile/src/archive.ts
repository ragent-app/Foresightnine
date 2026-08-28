import type { ComputeFmResult } from "@foresightnine/fm-engine";
import { t } from "./i18n";
import { resetMobileWorkflowState } from "./lifecycle";
import { ASSETS, MARKET_DATA_AS_OF } from "./fixtures";
import { canConfirmNormally, canConfirmReserved, isCanonicalRubric, isDerivedPublicEvidence } from "./validation";
import type { AppState, Branch, EvidenceBundle, Language, ResearchMethodData, ResearchSnapshot } from "./types";

type FmResult = ComputeFmResult;

export interface PrivacySafeDocument {
  readonly schema: "f9-privacy-safe-research-v1" | "f9-privacy-safe-research-v2";
  readonly title: string;
  readonly viewpoint: string;
  readonly capital: number;
  readonly currency: string;
  readonly selectedBranch: string;
  readonly evidence: readonly EvidenceBundle[];
  readonly privatePremiseIncluded: boolean;
  readonly disclaimer: string;
  /** Optional in restored v1 documents; new exports always write both fields. */
  readonly unresolvedIssuesAcknowledged?: boolean;
  readonly reservationNote?: string;
  readonly researchState?: ResearchSnapshot;
}

export interface ArchiveEnvelope {
  readonly schema: "f9-archive-envelope-v1";
  readonly algorithm: "AES-256-GCM";
  readonly keyRef: "secure-store:f9.archive.key";
  readonly aad: string;
  readonly combined: string;
  readonly createdAt: string;
  readonly documentHash: string;
}

function filterEvidence(evidence: readonly EvidenceBundle[], includePrivatePremise: boolean): readonly EvidenceBundle[] {
  return includePrivatePremise ? evidence : evidence.filter((item) => item.source_type !== "user_private");
}

export function archiveFmResult(result: FmResult | null): ResearchSnapshot["fmResult"] {
  if (!result) return null;
  return {
    cutoff: result.cutoff,
    selectedFactorIds: result.selectedFactorIds,
    z: Array.from(result.weights.z),
    weights: Array.from(result.weights.weights),
    fallback: result.weights.fallback,
    forecasts: Array.from(result.forecasts),
    forecastScale: { crossSectionalVolatility: Array.from(result.forecastScale.crossSectionalVolatility), returnScale: result.forecastScale.returnScale },
    metrics: result.metrics ? { icSeries: Array.from(result.metrics.icSeries), icMean: result.metrics.icMean, icir: result.metrics.icir } : null,
    rebalance: result.rebalance,
    deadband: result.deadband,
    reasons: result.reasons,
    rawClientResult: { targetWeights: Array.from(result.rawClientResult.targetWeights), forecastReturns: Array.from(result.rawClientResult.forecastReturns), transactionCost: result.rawClientResult.transactionCost, reasons: result.rawClientResult.reasons },
  };
}

export function restoreFmResult(snapshot: NonNullable<ResearchSnapshot["fmResult"]>): FmResult {
  const weights = Float64Array.from(snapshot.weights);
  const width = weights.length;
  return {
    cutoff: snapshot.cutoff,
    selectedFactorIds: snapshot.selectedFactorIds,
    factorSignalsByTime: new Map(),
    lastFactorDescriptors: [],
    combination: { combined: new Float64Array(width).fill(Number.NaN), supportWeight: new Float64Array(width), contributions: new Map(), qualityWeights: new Map(), usedFactorIds: snapshot.selectedFactorIds },
    weights: { z: Float64Array.from(snapshot.z), weights, fallback: snapshot.fallback },
    forecastScale: { crossSectionalVolatility: Float64Array.from(snapshot.forecastScale.crossSectionalVolatility), returnScale: snapshot.forecastScale.returnScale },
    forecasts: Float64Array.from(snapshot.forecasts),
    metrics: snapshot.metrics ? { icSeries: Float64Array.from(snapshot.metrics.icSeries), icMean: snapshot.metrics.icMean, icir: snapshot.metrics.icir } : null,
    rebalance: snapshot.rebalance,
    deadband: snapshot.deadband,
    reasons: snapshot.reasons,
    rawClientResult: { targetWeights: Float64Array.from(snapshot.rawClientResult.targetWeights), forecastReturns: Float64Array.from(snapshot.rawClientResult.forecastReturns), transactionCost: snapshot.rawClientResult.transactionCost, reasons: snapshot.rawClientResult.reasons },
  };
}

export function buildResearchSnapshot(input: { readonly state: AppState; readonly branches: readonly Branch[]; readonly methodData: ResearchMethodData }, includePrivatePremise = false): ResearchSnapshot {
  const { state } = input;
  const evidence = normalizeEvidenceArray(filterEvidence(state.evidence, includePrivatePremise));
  return {
    schema: "f9-research-snapshot-v1",
    stage: state.stage,
    language: state.language,
    viewpoint: state.viewpoint,
    capital: state.capital,
    currency: state.currency,
    challengeStatus: state.challengeStatus,
    challengeFailure: state.challengeFailure,
    challengeTurns: state.challengeTurns,
    replyDraft: state.replyDraft,
    rubric: state.rubric,
    evidence,
    searchStatus: state.searchStatus,
    confirmation: state.confirmation,
    unresolvedIssuesAcknowledged: state.unresolvedIssuesAcknowledged,
    reservationNote: state.reservationNote,
    selectedBranch: state.selectedBranch,
    branches: input.branches,
    fmStatus: state.fmStatus,
    fmProgress: state.fmProgress,
    fmStep: state.fmStep,
    fmResult: archiveFmResult(state.fmResult),
    portfolioMode: state.portfolioMode,
    methodData: input.methodData,
  };
}

export function buildPrivacySafeDocument(input: { readonly title: string; readonly viewpoint: string; readonly capital: number; readonly currency: string; readonly selectedBranch: string; readonly evidence: readonly EvidenceBundle[]; readonly language?: Language; readonly researchState?: ResearchSnapshot; readonly unresolvedIssuesAcknowledged?: boolean; readonly reservationNote?: string }, includePrivatePremise = false): PrivacySafeDocument {
  const evidence = normalizeEvidenceArray(filterEvidence(input.evidence, includePrivatePremise));
  return {
    schema: "f9-privacy-safe-research-v2",
    title: input.title,
    viewpoint: input.viewpoint,
    capital: input.capital,
    currency: input.currency,
    selectedBranch: input.selectedBranch,
    // Private evidence is excluded unless the person explicitly opts in to a
    // device-local archive. It is never part of a public/shareable export.
    evidence,
    privatePremiseIncluded: includePrivatePremise,
    unresolvedIssuesAcknowledged: input.unresolvedIssuesAcknowledged ?? input.researchState?.unresolvedIssuesAcknowledged ?? false,
    reservationNote: input.reservationNote ?? input.researchState?.reservationNote ?? "",
    disclaimer: t(input.language ?? "zh", "archiveDisclaimer"),
    ...(input.researchState ? { researchState: { ...input.researchState, evidence: normalizeEvidenceArray(filterEvidence(input.researchState.evidence, includePrivatePremise)) } } : {}),
  };
}

export function makeArchiveEnvelope(parts: Pick<ArchiveEnvelope, "aad" | "combined" | "createdAt" | "documentHash">): ArchiveEnvelope {
  return { schema: "f9-archive-envelope-v1", algorithm: "AES-256-GCM", keyRef: "secure-store:f9.archive.key", ...parts };
}

export function canRestoreArchive(envelope: unknown): envelope is ArchiveEnvelope {
  if (!isRecord(envelope) || !hasOnlyKeys(envelope, ["schema", "algorithm", "keyRef", "aad", "combined", "createdAt", "documentHash"])) return false;
  return envelope.schema === "f9-archive-envelope-v1" && envelope.algorithm === "AES-256-GCM" && envelope.keyRef === "secure-store:f9.archive.key" && envelope.aad === ARCHIVE_AAD && boundedString(envelope.combined, 1, 20_000) && /^[A-Za-z0-9+/]+={0,2}$/.test(envelope.combined) && envelope.combined.length % 4 === 0 && dateString(envelope.createdAt) && typeof envelope.documentHash === "string" && /^[a-f0-9]{64}$/.test(envelope.documentHash);
}

const ARCHIVE_AAD = "f9-archive-envelope-v1";
const MAX_TEXT = 20_000;
const MAX_ARRAY = 10_000;
const EPSILON = 1e-9;

function isRecord(value: unknown): value is Record<string, unknown> { return typeof value === "object" && value !== null && !Array.isArray(value); }
function hasOnlyKeys(value: Record<string, unknown>, allowed: readonly string[]): boolean { const keys = new Set(allowed); return Object.keys(value).every((key) => keys.has(key)); }
function finiteNumber(value: unknown): value is number { return typeof value === "number" && Number.isFinite(value); }
function finiteInteger(value: unknown): value is number { return finiteNumber(value) && Number.isSafeInteger(value); }
function boundedString(value: unknown, min = 0, max = MAX_TEXT): value is string { return typeof value === "string" && value.length >= min && value.length <= max && !value.includes("\u0000"); }
function dateString(value: unknown): value is string { return boundedString(value, 1, 100) && Number.isFinite(Date.parse(value)); }
function approvedDataDate(value: unknown): value is string { return dateString(value) && Date.parse(value) <= Date.parse(MARKET_DATA_AS_OF); }
function enumValue<T extends string>(value: unknown, values: readonly T[]): value is T { return typeof value === "string" && values.includes(value as T); }
function sameStringArray(left: readonly string[], right: readonly string[]): boolean { return left.length === right.length && left.every((item, index) => item === right[index]); }
function stringArray(value: unknown, min = 0, max = MAX_ARRAY): value is readonly string[] {
  if (!Array.isArray(value) || value.length < min || value.length > max) return false;
  for (let index = 0; index < value.length; index += 1) if (!boundedString(value[index], 1, MAX_TEXT)) return false;
  return true;
}
function numberArray(value: unknown, options: { readonly nullable?: boolean; readonly min?: number; readonly integer?: boolean; readonly maxLength?: number }): value is readonly (number | null)[] {
  if (!Array.isArray(value) || value.length > (options.maxLength ?? MAX_ARRAY)) return false;
  for (let index = 0; index < value.length; index += 1) {
    const item = value[index];
    if (item === null && options.nullable) continue;
    if (options.integer ? !finiteInteger(item) : !finiteNumber(item)) return false;
    if (options.min !== undefined && (item as number) < options.min) return false;
  }
  return true;
}
function normaliseNullableNumbers(value: readonly (number | null)[]): readonly number[] { return value.map((item) => item === null ? Number.NaN : item); }
function sameNullableNumberArray(left: readonly (number | null)[], right: readonly (number | null)[]): boolean {
  return left.length === right.length && left.every((item, index) => item === right[index] || (item === null && right[index] === null));
}
function capitalString(value: unknown): value is string {
  if (!boundedString(value, 1, 100) || !/^\d[\d,_\s]*(?:\.\d{1,2})?$/.test(value)) return false;
  const numeric = Number(value.replace(/[,_\s]/g, ""));
  return Number.isFinite(numeric) && numeric > 0 && numeric <= Number.MAX_SAFE_INTEGER;
}
function branchKind(value: unknown): value is "weak" | "main" | "strong" { return enumValue(value, ["weak", "main", "strong"] as const); }

function validEvidence(value: unknown): value is EvidenceBundle {
  if (!isRecord(value) || !hasOnlyKeys(value, ["evidence_id", "challenge_id", "source_type", "url", "title", "publisher", "published_at", "retrieved_at", "excerpt", "user_note", "content_hash", "grade", "supports_or_challenges", "independently_reviewable", "search_provider", "search_result_hash"])) return false;
  if (!boundedString(value.evidence_id, 1, 200) || !boundedString(value.challenge_id, 1, 200) || !enumValue(value.source_type, ["primary", "reliable_secondary", "commentary", "user_private", "model_inference"] as const) || !boundedString(value.url, 0, 4_000) || !boundedString(value.title, 1, 2_000) || !boundedString(value.publisher, 1, 1_000) || !(value.published_at === null || dateString(value.published_at)) || !dateString(value.retrieved_at) || !boundedString(value.excerpt, 0, MAX_TEXT) || !boundedString(value.user_note, 0, MAX_TEXT) || !boundedString(value.content_hash, 1, 200) || !enumValue(value.grade, ["A", "B", "C", "D", "E"] as const) || !enumValue(value.supports_or_challenges, ["supports", "challenges", "context"] as const) || typeof value.independently_reviewable !== "boolean") return false;
  if (value.search_provider !== undefined && !boundedString(value.search_provider, 1, 200)) return false;
  if (value.search_result_hash !== undefined && (typeof value.search_result_hash !== "string" || !/^[a-f0-9]{64}$/.test(value.search_result_hash))) return false;
  if ((value.search_provider === undefined) !== (value.search_result_hash === undefined)) return false;
  return true;
}
function validEvidenceArray(value: unknown): value is readonly EvidenceBundle[] {
  if (!Array.isArray(value) || value.length > MAX_ARRAY) return false;
  for (let index = 0; index < value.length; index += 1) if (!validEvidence(value[index])) return false;
  return true;
}
function normalizeEvidenceArray(value: readonly EvidenceBundle[]): readonly EvidenceBundle[] {
  return value.map((item) => ({ ...item, independently_reviewable: isDerivedPublicEvidence(item) }));
}
function validTurns(value: unknown): value is ResearchSnapshot["challengeTurns"] {
  if (!Array.isArray(value) || value.length > MAX_ARRAY) return false;
  const ids = new Set<string>();
  for (let index = 0; index < value.length; index += 1) {
    const turn = value[index];
    if (!isRecord(turn) || !hasOnlyKeys(turn, ["id", "author", "body"]) || !boundedString(turn.id, 1, 200) || ids.has(turn.id) || !enumValue(turn.author, ["user", "challenger", "system"] as const) || !boundedString(turn.body, 1, MAX_TEXT) || turn.body.trim().length === 0) return false;
    ids.add(turn.id);
  }
  return true;
}
function validRubric(value: unknown, language?: Language): value is ResearchSnapshot["rubric"] {
  if (!Array.isArray(value) || value.length !== 10) return false;
  const ids = new Set<string>();
  for (let index = 0; index < value.length; index += 1) {
    const item = value[index];
    if (!isRecord(item) || !hasOnlyKeys(item, ["id", "label", "score", "critical", "rationale"]) || !boundedString(item.id, 1, 100) || ids.has(item.id) || !boundedString(item.label, 1, 200) || !finiteInteger(item.score) || item.score < 0 || item.score > 4 || typeof item.critical !== "boolean" || !boundedString(item.rationale, 0, MAX_TEXT)) return false;
    ids.add(item.id);
  }
  return isCanonicalRubric(value as ResearchSnapshot["rubric"], language);
}
function probabilityInterval(value: unknown): value is { readonly low: number; readonly high: number } {
  return isRecord(value) && hasOnlyKeys(value, ["low", "high"]) && finiteNumber(value.low) && finiteNumber(value.high) && value.low >= 0 && value.high <= 1 && value.low <= value.high;
}
function validBranchNode(value: unknown): boolean {
  if (!isRecord(value) || !hasOnlyKeys(value, ["id", "time", "event", "occurrenceDate", "revealDate", "dataAsOf", "observableIndicators", "causalParents", "assetAnchors", "sourceRefs", "stateChange", "invalidation"])) return false;
  return boundedString(value.id, 1, 200) && boundedString(value.time, 1, 200) && boundedString(value.event, 1, MAX_TEXT) && dateString(value.occurrenceDate) && dateString(value.revealDate) && approvedDataDate(value.dataAsOf) && stringArray(value.observableIndicators, 1, 100) && stringArray(value.causalParents, 0, 100) && stringArray(value.assetAnchors, 1, 100) && stringArray(value.sourceRefs, 1, 100) && boundedString(value.stateChange, 1, MAX_TEXT) && (value.invalidation === undefined || boundedString(value.invalidation, 1, MAX_TEXT));
}
function validBranch(value: unknown): value is Branch {
  if (!isRecord(value) || !hasOnlyKeys(value, ["kind", "title", "baselineDate", "horizonEnd", "dataAsOf", "fixtureState", "premise", "premisePoint", "conditional", "conditionalPoint", "joint", "jointPoint", "description", "reveal", "nodes", "assetAnchors", "sourceRefs"])) return false;
  if (!branchKind(value.kind) || !boundedString(value.title, 1, 1_000) || !dateString(value.baselineDate) || !dateString(value.horizonEnd) || !approvedDataDate(value.dataAsOf) || value.dataAsOf > value.baselineDate || value.baselineDate > value.horizonEnd || value.fixtureState !== "historical_reference_and_simulated" || !probabilityInterval(value.premise) || !finiteNumber(value.premisePoint) || value.premisePoint < value.premise.low || value.premisePoint > value.premise.high || !probabilityInterval(value.conditional) || !finiteNumber(value.conditionalPoint) || value.conditionalPoint < value.conditional.low || value.conditionalPoint > value.conditional.high || !probabilityInterval(value.joint) || !finiteNumber(value.jointPoint) || !boundedString(value.description, 1, MAX_TEXT) || !boundedString(value.reveal, 1, MAX_TEXT) || !Array.isArray(value.nodes) || value.nodes.length === 0 || value.nodes.length > 100 || !stringArray(value.assetAnchors, 1, 100) || !stringArray(value.sourceRefs, 1, 100)) return false;
  let previousOccurrence = value.baselineDate;
  for (let index = 0; index < value.nodes.length; index += 1) {
    const node = value.nodes[index];
    if (!validBranchNode(node) || node.dataAsOf !== value.dataAsOf || node.occurrenceDate < previousOccurrence || node.occurrenceDate > node.revealDate || node.revealDate > value.horizonEnd) return false;
    previousOccurrence = node.occurrenceDate;
  }
  return true;
}
function validBranches(value: unknown): value is readonly Branch[] {
  if (!Array.isArray(value) || value.length !== 3) return false;
  const kinds = new Set<string>();
  for (let index = 0; index < value.length; index += 1) {
    if (!validBranch(value[index])) return false;
    if (kinds.has(value[index].kind)) return false;
    kinds.add(value[index].kind);
  }
  const first = value[0]!;
  const conditionalPoint = value.reduce((sum, branch) => sum + branch.conditionalPoint, 0);
  const conditionalLow = value.reduce((sum, branch) => sum + branch.conditional.low, 0);
  const conditionalHigh = value.reduce((sum, branch) => sum + branch.conditional.high, 0);
  if (Math.abs(conditionalPoint - 1) > EPSILON || conditionalLow > 1 + EPSILON || conditionalHigh < 1 - EPSILON) return false;
  return value.every((branch) => Math.abs(branch.premise.low - first.premise.low) <= EPSILON && Math.abs(branch.premise.high - first.premise.high) <= EPSILON && Math.abs(branch.premisePoint - first.premisePoint) <= EPSILON && Math.abs(branch.joint.low - branch.premise.low * branch.conditional.low) <= EPSILON && Math.abs(branch.joint.high - branch.premise.high * branch.conditional.high) <= EPSILON && Math.abs(branch.jointPoint - branch.premisePoint * branch.conditionalPoint) <= EPSILON);
}
function validMethodData(value: unknown): value is ResearchMethodData {
  return isRecord(value) && hasOnlyKeys(value, ["methodVersion", "dataAsOf", "navLabel", "fixtureState", "assetIds"]) && boundedString(value.methodVersion, 1, 200) && approvedDataDate(value.dataAsOf) && boundedString(value.navLabel, 1, 2_000) && value.fixtureState === "historical_reference_and_simulated" && stringArray(value.assetIds, ASSETS.length, ASSETS.length) && sameStringArray(value.assetIds as readonly string[], ASSETS.map((asset) => asset.id));
}
function validRebalance(value: unknown): boolean {
  return value === null || (isRecord(value) && hasOnlyKeys(value, ["postTradeNav", "transferred", "cost", "turnover"]) && finiteNumber(value.postTradeNav) && value.postTradeNav > 0 && finiteNumber(value.transferred) && value.transferred >= 0 && finiteNumber(value.cost) && value.cost >= 0 && finiteNumber(value.turnover) && value.turnover >= 0);
}
function validDeadband(value: unknown): boolean {
  return value === null || (isRecord(value) && hasOnlyKeys(value, ["execute", "expectedGain", "cost", "reason"]) && typeof value.execute === "boolean" && finiteNumber(value.expectedGain) && finiteNumber(value.cost) && value.cost >= 0 && enumValue(value.reason, ["initial_or_unknown_forecast", "target_unchanged", "below_cost_deadband", "clears_cost_deadband"] as const));
}
function normalizeFmResult(value: unknown): ResearchSnapshot["fmResult"] | undefined {
  if (value === null) return null;
  if (!isRecord(value) || !hasOnlyKeys(value, ["cutoff", "selectedFactorIds", "z", "weights", "fallback", "forecasts", "forecastScale", "metrics", "rebalance", "deadband", "reasons", "rawClientResult"])) return undefined;
  if (!approvedDataDate(value.cutoff) || !numberArray(value.selectedFactorIds, { integer: true, min: 0, maxLength: 1_000 }) || !numberArray(value.z, { nullable: true }) || !numberArray(value.weights, { min: 0 }) || typeof value.fallback !== "boolean" || !numberArray(value.forecasts, { nullable: true }) || !isRecord(value.forecastScale) || !hasOnlyKeys(value.forecastScale, ["crossSectionalVolatility", "returnScale"]) || !numberArray(value.forecastScale.crossSectionalVolatility, { nullable: true }) || !finiteNumber(value.forecastScale.returnScale) || value.forecastScale.returnScale <= 0 || !validRebalance(value.rebalance) || !validDeadband(value.deadband) || !stringArray(value.reasons, 0, 20) || !isRecord(value.rawClientResult) || !hasOnlyKeys(value.rawClientResult, ["targetWeights", "forecastReturns", "transactionCost", "reasons"]) || !numberArray(value.rawClientResult.targetWeights, { min: 0 }) || !numberArray(value.rawClientResult.forecastReturns, { nullable: true }) || !finiteNumber(value.rawClientResult.transactionCost) || value.rawClientResult.transactionCost < 0 || !stringArray(value.rawClientResult.reasons, 0, 20)) return undefined;
  if (value.metrics !== null) {
    if (!isRecord(value.metrics) || !hasOnlyKeys(value.metrics, ["icSeries", "icMean", "icir"]) || !numberArray(value.metrics.icSeries, { nullable: true }) || !finiteNumber(value.metrics.icMean) || !finiteNumber(value.metrics.icir)) return undefined;
  }
  const selectedFactorIds = value.selectedFactorIds as readonly number[];
  const z = value.z as readonly (number | null)[];
  const weights = value.weights as readonly number[];
  const forecasts = value.forecasts as readonly (number | null)[];
  const forecastScale = value.forecastScale as Record<string, unknown>;
  const crossSectionalVolatility = forecastScale.crossSectionalVolatility as readonly (number | null)[];
  const rawClientResult = value.rawClientResult as Record<string, unknown>;
  const targetWeights = rawClientResult.targetWeights as readonly number[];
  const forecastReturns = rawClientResult.forecastReturns as readonly (number | null)[];
  const metrics = value.metrics === null ? null : value.metrics as Record<string, unknown>;
  const width = weights.length;
  if (width !== ASSETS.length || z.length !== width || forecasts.length !== width || crossSectionalVolatility.length === 0 || targetWeights.length !== width || forecastReturns.length !== width) return undefined;
  // The archive carries both the typed FM result and the raw client handoff.
  // They are one computation, not two independently trusted inputs.
  if (!sameNullableNumberArray(weights, targetWeights) || !sameNullableNumberArray(forecasts, forecastReturns)) return undefined;
  const weightTotal = weights.reduce((sum, item) => sum + item, 0);
  if (!Number.isFinite(weightTotal) || Math.abs(weightTotal - 1) > 1e-6) return undefined;
  const targetWeightTotal = targetWeights.reduce((sum, item) => sum + item, 0);
  if (!Number.isFinite(targetWeightTotal) || Math.abs(targetWeightTotal - 1) > 1e-6) return undefined;
  if (new Set(selectedFactorIds).size !== selectedFactorIds.length) return undefined;
  return {
    cutoff: value.cutoff as string,
    selectedFactorIds,
    z: normaliseNullableNumbers(z),
    weights,
    fallback: value.fallback as boolean,
    forecasts: normaliseNullableNumbers(forecasts),
    forecastScale: { crossSectionalVolatility: normaliseNullableNumbers(crossSectionalVolatility), returnScale: forecastScale.returnScale as number },
    metrics: metrics === null ? null : { icSeries: normaliseNullableNumbers(metrics.icSeries as readonly (number | null)[]), icMean: metrics.icMean as number, icir: metrics.icir as number },
    rebalance: value.rebalance as FmResult["rebalance"],
    deadband: value.deadband as FmResult["deadband"],
    reasons: Array.from(value.reasons as readonly FmResult["reasons"][number][]),
    rawClientResult: { targetWeights, forecastReturns: normaliseNullableNumbers(forecastReturns), transactionCost: rawClientResult.transactionCost as number, reasons: Array.from(rawClientResult.reasons as readonly string[]) },
  };
}

/** The portfolio is reachable only from a fully confirmed, completed FM snapshot. */
export function canEnterPortfolio(state: Pick<AppState, "stage" | "challengeStatus" | "challengeTurns" | "rubric" | "evidence" | "confirmation" | "unresolvedIssuesAcknowledged" | "reservationNote" | "fmStatus" | "fmResult">): boolean {
  if (state.stage !== "portfolio" || state.fmStatus !== "complete" || state.fmResult === null) return false;
  return state.confirmation === "confirmed" ? canConfirmNormally(state) : state.confirmation === "reserved" ? canConfirmReserved(state) : false;
}

export function normalizeResearchSnapshot(value: unknown): ResearchSnapshot | null {
  if (!isRecord(value) || !hasOnlyKeys(value, ["schema", "stage", "language", "viewpoint", "capital", "currency", "challengeStatus", "challengeFailure", "challengeTurns", "replyDraft", "rubric", "evidence", "searchStatus", "confirmation", "unresolvedIssuesAcknowledged", "reservationNote", "selectedBranch", "branches", "fmStatus", "fmProgress", "fmStep", "fmResult", "portfolioMode", "methodData"]) || value.schema !== "f9-research-snapshot-v1" || !enumValue(value.stage, ["thesis", "challenge", "evidence", "worldline", "portfolio", "export"] as const) || !enumValue(value.language, ["zh", "en"] as const) || !boundedString(value.viewpoint, 0, 2_000) || !capitalString(value.capital) || !enumValue(value.currency, ["CNY", "USD", "EUR"] as const) || !enumValue(value.challengeStatus, ["idle", "streaming", "cancelled", "recovering", "failed", "complete"] as const) || !enumValue(value.challengeFailure, ["timeout", "rate_limited", "invalid_provider", "no_search"] as const) && value.challengeFailure !== null || !validTurns(value.challengeTurns) || !boundedString(value.replyDraft, 0, MAX_TEXT) || !validRubric(value.rubric, value.language as Language) || !validEvidenceArray(value.evidence) || !enumValue(value.searchStatus, ["idle", "mock_ready", "loading", "available", "api_unavailable", "api_failed"] as const) || !enumValue(value.confirmation, ["pending", "confirmed", "reserved"] as const) || (value.unresolvedIssuesAcknowledged !== undefined && typeof value.unresolvedIssuesAcknowledged !== "boolean") || (value.reservationNote !== undefined && !boundedString(value.reservationNote, 0, MAX_TEXT)) || !branchKind(value.selectedBranch) || !validBranches(value.branches) || !enumValue(value.fmStatus, ["idle", "computing", "complete", "cancelled", "failed"] as const) || !finiteNumber(value.fmProgress) || value.fmProgress < 0 || value.fmProgress > 100 || !boundedString(value.fmStep, 0, 1_000) || !enumValue(value.portfolioMode, ["raw", "public"] as const) || !validMethodData(value.methodData)) return null;
  if ((value.challengeStatus === "failed") !== (value.challengeFailure !== null)) return null;
  const fmResult = normalizeFmResult(value.fmResult);
  if (fmResult === undefined) return null;
  const evidence = normalizeEvidenceArray(value.evidence as readonly EvidenceBundle[]);

  const lifecycleFieldsPresent = typeof value.unresolvedIssuesAcknowledged === "boolean" && typeof value.reservationNote === "string";
  const originalAcknowledgement = lifecycleFieldsPresent && value.unresolvedIssuesAcknowledged === true;
  const originalReservationNote = lifecycleFieldsPresent ? value.reservationNote! : "";
  let confirmation: ResearchSnapshot["confirmation"] = value.confirmation;
  let acknowledgement = originalAcknowledgement;
  let reservationNote = originalReservationNote;
  const confirmationState = {
    challengeStatus: value.challengeStatus as AppState["challengeStatus"],
    challengeFailure: value.challengeFailure as AppState["challengeFailure"],
    rubric: value.rubric as AppState["rubric"],
    evidence,
    challengeTurns: value.challengeTurns as AppState["challengeTurns"],
    language: value.language as Language,
    unresolvedIssuesAcknowledged: acknowledgement,
    reservationNote,
  };
  const validConfirmation = confirmation === "pending"
    ? !acknowledgement && reservationNote.trim().length === 0
    : confirmation === "confirmed"
      ? canConfirmNormally(confirmationState)
      : canConfirmReserved(confirmationState);
  if (!lifecycleFieldsPresent || !validConfirmation) {
    confirmation = "pending";
    acknowledgement = false;
    reservationNote = "";
  }

  let stage: ResearchSnapshot["stage"] = value.stage;
  let fmStatus: ResearchSnapshot["fmStatus"] = value.fmStatus;
  let fmProgress = value.fmProgress;
  let fmStep = value.fmStep;
  let safeFmResult = fmResult;
  let selectedBranch: ResearchSnapshot["selectedBranch"] = value.selectedBranch;
  if (confirmation === "pending" || value.challengeStatus !== "complete") {
    stage = stage === "thesis" ? "thesis" : "challenge";
    selectedBranch = "main";
    fmStatus = "idle";
    fmProgress = 0;
    fmStep = "";
    safeFmResult = null;
  } else if (fmStatus === "complete" && safeFmResult !== null && (stage === "portfolio" || stage === "export")) {
    fmProgress = 100;
  } else {
    // A result is never enough to enter portfolio: stage, confirmation, and
    // FM status must describe the same lifecycle state. Recovery of an
    // interrupted or contradictory snapshot stops at the confirmed worldline.
    stage = "worldline";
    fmStatus = "idle";
    fmProgress = 0;
    fmStep = "";
    safeFmResult = null;
  }
  const normalizedFailure = value.challengeStatus === "failed" ? value.challengeFailure : null;
  return {
    schema: "f9-research-snapshot-v1", stage, language: value.language, viewpoint: value.viewpoint, capital: value.capital, currency: value.currency,
    challengeStatus: value.challengeStatus, challengeFailure: normalizedFailure, challengeTurns: value.challengeTurns, replyDraft: value.replyDraft, rubric: value.rubric, evidence, searchStatus: value.searchStatus, confirmation, unresolvedIssuesAcknowledged: acknowledgement, reservationNote, selectedBranch, branches: value.branches, fmStatus, fmProgress, fmStep, fmResult: safeFmResult, portfolioMode: value.portfolioMode, methodData: value.methodData,
  };
}

export function canRestoreResearchSnapshot(value: unknown): value is ResearchSnapshot { return normalizeResearchSnapshot(value) !== null; }

export function normalizePrivacySafeDocument(value: unknown): PrivacySafeDocument | null {
  if (!isRecord(value) || !hasOnlyKeys(value, ["schema", "title", "viewpoint", "capital", "currency", "selectedBranch", "evidence", "privatePremiseIncluded", "disclaimer", "unresolvedIssuesAcknowledged", "reservationNote", "researchState"]) || !enumValue(value.schema, ["f9-privacy-safe-research-v1", "f9-privacy-safe-research-v2"] as const) || !boundedString(value.title, 1, 2_000) || !boundedString(value.viewpoint, 1, 2_000) || !finiteNumber(value.capital) || value.capital <= 0 || value.capital > Number.MAX_SAFE_INTEGER || !enumValue(value.currency, ["CNY", "USD", "EUR"] as const) || !branchKind(value.selectedBranch) || !validEvidenceArray(value.evidence) || typeof value.privatePremiseIncluded !== "boolean" || !boundedString(value.disclaimer, 0, MAX_TEXT) || (value.unresolvedIssuesAcknowledged !== undefined && typeof value.unresolvedIssuesAcknowledged !== "boolean") || (value.reservationNote !== undefined && !boundedString(value.reservationNote, 0, MAX_TEXT))) return null;
  if (!value.privatePremiseIncluded && value.evidence.some((item) => item.source_type === "user_private")) return null;
  const evidence = normalizeEvidenceArray(value.evidence as readonly EvidenceBundle[]);
  const researchState = value.researchState === undefined ? undefined : normalizeResearchSnapshot(value.researchState);
  if (value.researchState !== undefined && researchState === null) return null;
  if (researchState && !value.privatePremiseIncluded && researchState.evidence.some((item) => item.source_type === "user_private")) return null;
  return {
    schema: value.schema,
    title: value.title,
    viewpoint: value.viewpoint,
    capital: value.capital,
    currency: value.currency,
    selectedBranch: value.selectedBranch,
    evidence,
    privatePremiseIncluded: value.privatePremiseIncluded,
    disclaimer: value.disclaimer,
    ...(value.unresolvedIssuesAcknowledged !== undefined ? { unresolvedIssuesAcknowledged: value.unresolvedIssuesAcknowledged } : {}),
    ...(value.reservationNote !== undefined ? { reservationNote: value.reservationNote } : {}),
    ...(researchState ? { researchState } : {}),
  };
}

export function canRestorePrivacySafeDocument(value: unknown): value is PrivacySafeDocument { return normalizePrivacySafeDocument(value) !== null; }

/** Hydrates the application state itself; archive UI rendering is not restoration. */
export function restoreAppState(current: AppState, document: unknown): AppState {
  const normalized = normalizePrivacySafeDocument(document);
  if (!normalized) return current;
  const snapshot = normalized.researchState;
  if (!snapshot) {
    const currency = normalized.currency === "USD" || normalized.currency === "EUR" ? normalized.currency : "CNY";
    return { ...resetMobileWorkflowState(current, normalized.viewpoint), capital: String(normalized.capital), currency, draftRestored: true };
  }
  let restoredFmResult: AppState["fmResult"] = null;
  let fmRestoreFailed = false;
  if (snapshot.fmResult) {
    try {
      restoredFmResult = restoreFmResult(snapshot.fmResult);
    } catch {
      fmRestoreFailed = true;
    }
  }
  const restoredStage = fmRestoreFailed && (snapshot.stage === "portfolio" || snapshot.stage === "export") ? "worldline" : snapshot.stage;
  return {
    ...current,
    stage: restoredStage,
    language: snapshot.language,
    viewpoint: snapshot.viewpoint,
    capital: snapshot.capital,
    currency: snapshot.currency,
    draftRestored: true,
    challengeStatus: snapshot.challengeStatus,
    challengeFailure: snapshot.challengeFailure,
    challengeTurns: snapshot.challengeTurns,
    replyDraft: snapshot.replyDraft,
    rubric: snapshot.rubric,
    evidence: snapshot.evidence,
    searchStatus: snapshot.searchStatus,
    confirmation: snapshot.confirmation,
    unresolvedIssuesAcknowledged: snapshot.unresolvedIssuesAcknowledged === true,
    reservationNote: snapshot.reservationNote ?? "",
    selectedBranch: snapshot.selectedBranch,
    branchSnapshot: snapshot.confirmation === "pending" ? null : snapshot.branches,
    fmStatus: fmRestoreFailed ? "idle" : snapshot.fmStatus,
    fmProgress: fmRestoreFailed ? 0 : snapshot.fmProgress,
    fmStep: fmRestoreFailed ? "" : snapshot.fmStep,
    fmResult: restoredFmResult,
    portfolioMode: snapshot.portfolioMode,
  };
}

export function archiveIncludesPrivateEvidence(document: PrivacySafeDocument): boolean {
  return document.evidence.some((item) => item.source_type === "user_private") || Boolean(document.researchState?.evidence.some((item) => item.source_type === "user_private"));
}
