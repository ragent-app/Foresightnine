import { ApiError } from "../api/http";

const BASE_DELAY_MS = 1500;
const MAX_DELAY_MS = 30000;

export function nextPollDelay(previousMs: number, error?: unknown): number {
  if (error === undefined) return BASE_DELAY_MS;
  const backoff = Math.min(Math.max(BASE_DELAY_MS, previousMs) * 2, MAX_DELAY_MS);
  if (
    error instanceof ApiError
    && (error.status === 429 || error.status === 503)
    && error.retryAfterMs !== null
  ) {
    return Math.max(backoff, error.retryAfterMs);
  }
  return backoff;
}
