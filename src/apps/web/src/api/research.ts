import type { ResearchV2 } from "@foresightnine/contracts";
import { apiRequest } from "./http";
import type { ApiRequestOptions } from "./http";
import type { OperationStatus, RecoveryStatus, ResearchInstruction, RunProgress, SourceText, VersionSource } from "./types";

const enc = encodeURIComponent;

function opts(signal?: AbortSignal): ApiRequestOptions {
  return signal ? { signal } : {};
}

function topicPath(topicId: string): string {
  return `/v2/research/topics/${enc(topicId)}`;
}

function runPath(topicId: string, operationId: string): string {
  return `${topicPath(topicId)}/runs/${enc(operationId)}`;
}

function versionPath(topicId: string, versionId: string): string {
  return `${topicPath(topicId)}/versions/${enc(versionId)}`;
}

export function listTopics(signal?: AbortSignal): Promise<ResearchV2.ResearchTopic[]> {
  return apiRequest<ResearchV2.ResearchTopic[]>("/v2/research/topics", opts(signal));
}

export function getTopic(topicId: string, signal?: AbortSignal): Promise<ResearchV2.ResearchTopic> {
  return apiRequest<ResearchV2.ResearchTopic>(topicPath(topicId), opts(signal));
}

export function createTopic(input: ResearchV2.ResearchInput): Promise<ResearchV2.ResearchTopic> {
  return apiRequest<ResearchV2.ResearchTopic>("/v2/research/topics", { method: "POST", body: input });
}

/** Rename (empty title restores the question as the name) or pin; the research question itself never changes. */
export function updateTopic(topicId: string, update: ResearchV2.TopicUpdate): Promise<ResearchV2.ResearchTopic> {
  return apiRequest<ResearchV2.ResearchTopic>(topicPath(topicId), { method: "PATCH", body: update });
}

/** Soft delete; restorable until purge_after. */
export function deleteTopic(topicId: string): Promise<ResearchV2.TopicDeletion> {
  return apiRequest<ResearchV2.TopicDeletion>(topicPath(topicId), { method: "DELETE" });
}

/** Appends a server-made copy of an earlier version (for example a replaced answer) as the newest version. */
export function restoreVersion(topicId: string, versionId: string, parentVersionId: string): Promise<ResearchV2.ResearchTopic> {
  return apiRequest<ResearchV2.ResearchTopic>(`${versionPath(topicId, versionId)}/restore`, { method: "POST", body: { parent_version_id: parentVersionId } });
}

/** A new research from one version, up to and including a turn (the whole version when omitted). */
export function forkTopic(topicId: string, fork: ResearchV2.TopicForkInput): Promise<ResearchV2.ResearchTopic> {
  return apiRequest<ResearchV2.ResearchTopic>(`${topicPath(topicId)}/fork`, { method: "POST", body: fork });
}

export function restoreTopic(topicId: string): Promise<ResearchV2.ResearchTopic> {
  return apiRequest<ResearchV2.ResearchTopic>(`${topicPath(topicId)}/restore`, { method: "POST" });
}

export function startRun(
  topicId: string,
  body: ResearchV2.ResearchRunRequest,
  idempotencyKey: string,
): Promise<ResearchV2.OperationResponse> {
  return apiRequest<ResearchV2.OperationResponse>(`${topicPath(topicId)}/runs`, {
    method: "POST",
    body,
    idempotencyKey,
  });
}

export function sendInstruction(topicId: string, turnId: string, text: string, idempotencyKey: string): Promise<ResearchInstruction> {
  return apiRequest(`${topicPath(topicId)}/turns/${enc(turnId)}/instructions`, { method: "POST", body: { text }, idempotencyKey });
}

export function stopTurn(topicId: string, turnId: string, idempotencyKey: string): Promise<{ turn_id: string; status: "stopped" }> {
  return apiRequest(`${topicPath(topicId)}/turns/${enc(turnId)}/stop`, { method: "POST", body: {}, idempotencyKey });
}

export function acceptFindings(topicId: string, turnId: string): Promise<{ turn_id: string; status: "completed" }> {
  return apiRequest(`${topicPath(topicId)}/turns/${enc(turnId)}/accept-findings`, { method: "POST", body: {} });
}

export interface TurnStatus {
  topic_id: string; turn_id: string; current_operation_id: string | null;
  logical_status: "running" | "awaiting_user" | "completed" | "stopped" | "failed"; version_id?: string | null;
}
export function listTurnStatuses(signal?: AbortSignal): Promise<TurnStatus[]> {
  return apiRequest<TurnStatus[]>("/v2/research/turn-statuses", opts(signal));
}

export type ResearchRunResponse = ResearchV2.OperationResponse & {
  logical_status?: "running" | "awaiting_user" | "completed" | "stopped" | "failed";
  current_operation_id?: string | null;
  turn_id?: string | null;
};

// Backend returns newest first.
export function listRuns(topicId: string, signal?: AbortSignal): Promise<ResearchRunResponse[]> {
  return apiRequest<ResearchRunResponse[]>(`${topicPath(topicId)}/runs`, opts(signal));
}

export function getProgress(
  topicId: string,
  operationId: string,
  after: number,
  signal?: AbortSignal,
): Promise<RunProgress> {
  return apiRequest<RunProgress>(`${runPath(topicId, operationId)}/progress?after=${enc(String(after))}`, opts(signal));
}

export function getRecoveryStatus(
  topicId: string,
  operationId: string,
  signal?: AbortSignal,
): Promise<RecoveryStatus> {
  return apiRequest<RecoveryStatus>(`${runPath(topicId, operationId)}/recovery-status`, opts(signal));
}

export function continueRun(
  topicId: string,
  operationId: string,
  idempotencyKey: string,
): Promise<ResearchV2.OperationResponse> {
  return apiRequest<ResearchV2.OperationResponse>(`${runPath(topicId, operationId)}/continue`, {
    method: "POST",
    idempotencyKey,
  });
}

export function recoverRun(
  topicId: string,
  operationId: string,
  idempotencyKey: string,
): Promise<ResearchV2.OperationResponse> {
  return apiRequest<ResearchV2.OperationResponse>(`${runPath(topicId, operationId)}/recover`, {
    method: "POST",
    idempotencyKey,
  });
}

export function cancelOperation(
  operationId: string,
  cancelToken: string,
  idempotencyKey: string,
): Promise<ResearchV2.OperationResponse> {
  return apiRequest<ResearchV2.OperationResponse>(`/v1/operations/${enc(operationId)}/cancel`, {
    method: "POST",
    body: { cancel_token: cancelToken },
    idempotencyKey,
  });
}

export function listVersionSources(
  topicId: string,
  versionId: string,
  signal?: AbortSignal,
): Promise<VersionSource[]> {
  return apiRequest<VersionSource[]>(`${versionPath(topicId, versionId)}/sources`, opts(signal));
}

export function getVersionSourceText(
  topicId: string,
  versionId: string,
  sourceId: string,
  signal?: AbortSignal,
): Promise<SourceText> {
  return apiRequest<SourceText>(`${versionPath(topicId, versionId)}/sources/${enc(sourceId)}/text`, opts(signal));
}

export function createVersion(topicId: string, input: ResearchV2.VersionInput): Promise<ResearchV2.ResearchTopic> {
  return apiRequest<ResearchV2.ResearchTopic>(`${topicPath(topicId)}/versions`, { method: "POST", body: input });
}

export function exportFull(topicId: string, versionId: string): Promise<ResearchV2.ResearchFullExport> {
  return apiRequest<ResearchV2.ResearchFullExport>(`${versionPath(topicId, versionId)}/export`);
}

export function exportMetadata(topicId: string, versionId: string): Promise<ResearchV2.ResearchMetadataExport> {
  return apiRequest<ResearchV2.ResearchMetadataExport>(`${versionPath(topicId, versionId)}/export/metadata`);
}

export function latestVersion(topic: ResearchV2.ResearchTopic): ResearchV2.ResearchVersion {
  const version = topic.versions[topic.versions.length - 1];
  if (!version) throw new Error("topic_without_versions");
  return version;
}

export function isActiveStatus(status: OperationStatus): boolean {
  return status === "pending" || status === "running";
}

// apps/api/app/main.py create_research_calculation: the backend computes and saves a new version.
export type CreateCalculationRequest = ResearchV2.CreateResearchCalculationRequest;

export function createCalculation(topicId: string, body: CreateCalculationRequest): Promise<ResearchV2.ResearchTopic> {
  return apiRequest<ResearchV2.ResearchTopic>(`${topicPath(topicId)}/calculations`, { method: "POST", body });
}

// apps/api/app/main.py recalculate_research
export function recalculateCalculation(topicId: string, parentVersionId: string, calculationIndex: number): Promise<ResearchV2.ResearchTopic> {
  return apiRequest<ResearchV2.ResearchTopic>(`${topicPath(topicId)}/calculations/recalculate`, {
    method: "POST",
    body: { parent_version_id: parentVersionId, calculation_index: calculationIndex },
  });
}
