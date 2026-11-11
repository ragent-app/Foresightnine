import { ApiError, type ApiClient } from '@foresightnine/api-client';
import type { ConfirmChallengeRequest, ChallengeOperationResponse } from '@foresightnine/contracts';

type Store = Pick<Storage, 'getItem' | 'setItem'>;
const HORIZON = '2028-08-31';

/** A confirmation receipt is durable even if scenario generation is interrupted.
 * Re-read it; never re-confirm a CONFIRMED challenge. Reuse the worldline key
 * after refresh/connection loss so an already-running model isn't billed twice.
 * Only a structured terminal API error permits a new explicit user attempt.
 */
export async function completeWorldlineFlow(client: ApiClient, challengeId: string,
  confirmation: ConfirmChallengeRequest,
  options: { operationId: string; signal: AbortSignal; storage: Store; onConfirmed: (receipt: ChallengeOperationResponse) => void }) {
  const key = `foresightnine.worldline-attempt.v2:${challengeId}:${HORIZON}`;
  const stored = options.storage.getItem(key);
  const attempt = stored !== null && /^\d{1,6}$/.test(stored) ? Number(stored) : 0;
  // Write before any model POST. Storage failure stops submission rather than
  // sacrificing durable replay protection while showing a recoverable error.
  options.storage.setItem(key, String(attempt));
  const receipt = await client.getChallenge(challengeId, { signal: options.signal, operationId: options.operationId });
  const requestDigest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(confirmation)))), b => b.toString(16).padStart(2,'0')).join('').slice(0,24);
  const confirmed = receipt.challenge.status === 'CONFIRMED' ? receipt
    : await client.confirmChallenge(challengeId, confirmation, { signal: options.signal, idempotencyKey: `confirm:${challengeId}:${requestDigest}` });
  options.onConfirmed(confirmed);
  try {
    return await client.createWorldlines({challenge_id: challengeId, horizon_end: HORIZON},
      {signal: options.signal, idempotencyKey: `worldlines-v2:${challengeId}:${HORIZON}:${attempt}`});
  } catch (error) {
    // Cloudflare HTML, disconnects, cancellation and malformed responses do not
    // prove the model operation failed: retain the original key for recovery.
    if (error instanceof ApiError && error.requestId && ['internal_error','quota_exceeded'].includes(error.errorCode ?? '')
        && [429,500,503].includes(error.status)) options.storage.setItem(key, String(attempt + 1));
    throw error;
  }
}
