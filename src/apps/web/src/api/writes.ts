import type { ResearchV2 } from "@foresightnine/contracts";
import { readSession } from "../auth/session";
import { getOrCreateWriteKey, settleWrite, stableStringify } from "../lib/pendingWrites";
import { ApiError } from "./http";
import { cancelOperation, continueRun, createTopic, latestVersion, recoverRun, startRun } from "./research";

export function runScope(accountId: string, topicId: string, parentVersionId: string): string {
  return `run:${accountId}:${topicId}:${parentVersionId}`;
}

function isDefinitiveRejection(error: unknown): boolean {
  return (
    error instanceof ApiError
    && error.status >= 400
    && error.status < 500
    && error.status !== 408
    && error.status !== 429
  );
}

// Reuses the stored Idempotency-Key until the write is definitively settled:
// success or a non-retryable 4xx. Transport errors, 5xx, 408, 429 and aborts keep the key.
export async function idempotentWrite<T>(
  scope: string,
  body: unknown,
  send: (key: string) => Promise<T>,
): Promise<T> {
  const key = getOrCreateWriteKey(scope, stableStringify(body ?? null));
  try {
    const result = await send(key);
    settleWrite(scope);
    return result;
  } catch (error) {
    if (isDefinitiveRejection(error)) settleWrite(scope);
    throw error;
  }
}

export async function startTurn(
  topicId: string,
  body: ResearchV2.ResearchRunRequest,
): Promise<ResearchV2.OperationResponse> {
  const session = readSession();
  if (!session) throw new Error("no_session");
  const scope = runScope(session.account.account_id, topicId, body.parent_version_id);
  return idempotentWrite(scope, body, (key) => startRun(topicId, body, key));
}

export interface CreateResearchResult {
  topic: ResearchV2.ResearchTopic;
  operation: ResearchV2.Operation | null;
  startError: unknown | null;
}

export async function createResearch(
  input: ResearchV2.ResearchInput,
  run: Omit<ResearchV2.ResearchRunRequest, "parent_version_id">,
): Promise<CreateResearchResult> {
  const topic = await createTopic(input);
  try {
    const parent = latestVersion(topic).version_id;
    const response = await startTurn(topic.topic_id, { ...run, parent_version_id: parent });
    return { topic, operation: response.operation, startError: null };
  } catch (startError) {
    return { topic, operation: null, startError };
  }
}

export function cancelRun(operation: ResearchV2.Operation): Promise<ResearchV2.OperationResponse> {
  return idempotentWrite(
    `cancel:${operation.operation_id}`,
    { cancel_token: operation.cancel_token },
    (key) => cancelOperation(operation.operation_id, operation.cancel_token, key),
  );
}

export function continueTurn(topicId: string, operationId: string): Promise<ResearchV2.OperationResponse> {
  return idempotentWrite(`continue:${operationId}`, null, (key) => continueRun(topicId, operationId, key));
}

export function recoverTurn(topicId: string, operationId: string): Promise<ResearchV2.OperationResponse> {
  return idempotentWrite(`recover:${operationId}`, null, (key) => recoverRun(topicId, operationId, key));
}
