import type { ResearchV2 } from "@foresightnine/contracts";
import { apiRequest } from "./http";
import type { ApiRequestOptions } from "./http";
import type { OperationStatus, RecoveryStatus, RunProgress, SourceText, VersionSource } from "./types";

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

// Backend returns newest first.
export function listRuns(topicId: string, signal?: AbortSignal): Promise<ResearchV2.OperationResponse[]> {
  return apiRequest<ResearchV2.OperationResponse[]>(`${topicPath(topicId)}/runs`, opts(signal));
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
