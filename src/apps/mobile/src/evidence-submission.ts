import { getRandomBytes } from "expo-crypto";
import type { ApiRequestOptions } from "@foresightnine/api-client";
import type { ChallengeRecord, EvidenceSubmissionRequest } from "@foresightnine/contracts";
import type { AppState, EvidenceBundle } from "./types";
import { challengeToMobileUi } from "./api-runtime";
import { isDuplicateEvidence } from "./evidence";

export type EvidenceAddResult = "added" | "duplicate" | "failed" | "busy";
interface EvidenceApi {
  submitEvidence(id: string, body: EvidenceSubmissionRequest, options?: ApiRequestOptions): Promise<{ readonly challenge: ChallengeRecord }>;
}
interface Input {
  readonly api: EvidenceApi;
  readonly ownerScope: string;
  readonly challengeId: string;
  readonly evidence: EvidenceBundle;
  readonly isCurrent: () => boolean;
}
interface Receipt {
  readonly signature: string;
  readonly key: string;
  readonly request: EvidenceSubmissionRequest;
  result?: ChallengeRecord;
}
export class EvidenceInputChanged extends Error {
  constructor() { super("Recover the original evidence submission before changing it."); }
}
export function evidenceRequest(evidence: EvidenceBundle): EvidenceSubmissionRequest {
  return { title: evidence.title, retrieved_at: evidence.retrieved_at, excerpt: evidence.excerpt, user_note: evidence.user_note, content_hash: evidence.content_hash,
    ...(evidence.url ? { url: evidence.url } : {}), ...(evidence.publisher ? { publisher: evidence.publisher } : {}), ...(evidence.published_at ? { published_at: evidence.published_at } : {}),
    ...(evidence.search_provider && evidence.search_result_hash ? { search_provider: evidence.search_provider, search_result_hash: evidence.search_result_hash } : {}) };
}
export function applyAcceptedEvidence(state: AppState, evidence: EvidenceBundle, record: ChallengeRecord): AppState {
  const update = challengeToMobileUi(record, state.viewpoint, state.language, "", state.challengeTurns);
  return { ...state, evidence: isDuplicateEvidence(state.evidence, evidence) ? state.evidence : [...state.evidence, evidence], challengeTurns: update.turns, rubric: update.rubric, challengeStatus: update.status, exportNotice: "" };
}
const requestKey = () => `f9-${Array.from(getRandomBytes(16), (byte) => byte.toString(16).padStart(2, "0")).join("")}`;

/** Retry the same logical evidence with its original request bytes/key, not a new operation key. */
export function createEvidenceSubmission(keyFactory: () => string = requestKey) {
  const receipts = new Map<string, Receipt>();
  let active: AbortController | null = null;
  return {
    isBusy: () => active !== null,
    cancel: () => active?.abort(),
    async submit(input: Input): Promise<ChallengeRecord | null> {
      if (active) return null;
      const controller = new AbortController();
      const assertCurrent = () => {
        if (controller.signal.aborted || !input.isCurrent()) {
          const error = new Error("Evidence no longer belongs to the current research."); error.name = "AbortError"; throw error;
        }
      };
      assertCurrent();
      const request = evidenceRequest(input.evidence);
      // Imported text gets a new retrieval time/hash each click; retries retain the original payload.
      const { retrieved_at: _retrieved, content_hash: _hash, ...content } = request;
      const scope = JSON.stringify([input.ownerScope, input.challengeId, request.url || request.excerpt]);
      const signature = JSON.stringify(content);
      let receipt = receipts.get(scope);
      if (!receipt) { receipt = { signature, key: keyFactory(), request }; receipts.set(scope, receipt); }
      if (receipt.signature !== signature) throw new EvidenceInputChanged();
      if (receipt.result) return receipt.result;
      active = controller;
      try {
        const response = await input.api.submitEvidence(input.challengeId, receipt.request, { signal: controller.signal, idempotencyKey: receipt.key });
        if (response.challenge.challenge_id !== input.challengeId || !["CHALLENGING", "EVIDENCE_REQUIRED"].includes(response.challenge.status)) throw new Error("Evidence response is inconsistent.");
        receipt.result = response.challenge;
        assertCurrent();
        return receipt.result;
      } finally { active = null; }
    },
  };
}
