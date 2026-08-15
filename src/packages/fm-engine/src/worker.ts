import { computeFm, type ComputeFmInput, type ComputeFmResult } from "./orchestration.js";

export interface FmComputeMessage {
  readonly type: "compute";
  readonly requestId: string;
  readonly input: ComputeFmInput;
}

export interface FmWorkerError {
  readonly code: "invalid_message" | "compute_failed";
  readonly message: string;
}

export type FmWorkerResponse =
  | { readonly type: "result"; readonly requestId: string; readonly result: ComputeFmResult }
  | { readonly type: "error"; readonly requestId: string; readonly error: FmWorkerError };

export interface FmWorkerPort {
  postMessage(message: FmWorkerResponse): void;
  onmessage: ((event: { readonly data: unknown }) => void) | null;
}

function isRecord(value: unknown): value is { readonly [key: string]: unknown } {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function invalid(requestId: string, message: string): FmWorkerResponse {
  return { type: "error", requestId, error: { code: "invalid_message", message } };
}

/** Mount on a browser DedicatedWorkerGlobalScope or a deterministic fake port. */
export function mountFmWorker(port: FmWorkerPort): () => void {
  const previous = port.onmessage;
  port.onmessage = (event) => {
    const message = event.data;
    const requestId = isRecord(message) && typeof message.requestId === "string" ? message.requestId : "unknown";
    if (!isRecord(message) || message.type !== "compute" || typeof message.requestId !== "string" || !isRecord(message.input)) {
      port.postMessage(invalid(requestId, "invalid compute message"));
      return;
    }
    try {
      const result = computeFm(message.input as unknown as ComputeFmInput);
      port.postMessage({ type: "result", requestId, result });
    } catch {
      // Worker errors must not expose stack traces, paths, or arbitrary thrown values.
      port.postMessage({ type: "error", requestId, error: { code: "compute_failed", message: "FM computation failed" } });
    }
  };
  return () => { port.onmessage = previous; };
}
