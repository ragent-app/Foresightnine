import { getRandomBytes } from "expo-crypto";
import type { ApiRequestOptions } from "@foresightnine/api-client";
import type { ChallengeRecord, ConfirmChallengeRequest, WorldlineBundle, WorldlineRequest } from "@foresightnine/contracts";
import { worldlineBundleToBranches } from "./api-runtime";
import type { Branch, Language } from "./types";

interface ConfirmationApi {
  confirmChallenge(id: string, body: ConfirmChallengeRequest, options?: ApiRequestOptions): Promise<{ readonly challenge: ChallengeRecord }>;
  createWorldlines(body: WorldlineRequest, options?: ApiRequestOptions): Promise<{ readonly bundle: WorldlineBundle }>;
}
interface ConfirmationInput {
  readonly api: ConfirmationApi;
  readonly ownerScope: string;
  readonly challengeId: string;
  readonly reserved: boolean;
  readonly acknowledged: boolean;
  readonly note: string;
  readonly language: Language;
  readonly isCurrent: () => boolean;
}
export interface ConfirmedWorldlines { readonly challenge: ChallengeRecord; readonly branches: readonly Branch[]; }
interface Receipt {
  readonly scope: string;
  readonly signature: string;
  readonly confirmKey: string;
  readonly worldlineKey: string;
  challenge?: ChallengeRecord;
  result?: ConfirmedWorldlines;
}
export class ConfirmationInputChanged extends Error {
  constructor() { super("Confirmation input changed; recover using the original confirmation."); }
}
const requestKey = () => `f9-${Array.from(getRandomBytes(16), (byte) => byte.toString(16).padStart(2, "0")).join("")}`;

export async function continueAfterConfirmation(confirm: (reserved: boolean) => Promise<boolean>, reserved: boolean, isFocused: () => boolean, navigate: () => void): Promise<boolean> {
  if (!await confirm(reserved) || !isFocused()) return false;
  navigate();
  return true;
}

/** Keeps the two command receipts distinct, including after an uncertain network outcome. */
export function createConfirmationFlow(keyFactory: () => string = requestKey) {
  let active: AbortController | null = null;
  const receipts = new Map<string, Receipt>();
  return {
    isBusy: () => active !== null,
    cancel: () => { active?.abort(); },
    async run(input: ConfirmationInput): Promise<ConfirmedWorldlines | null> {
      if (active) return null;
      const controller = new AbortController();
      const assertCurrent = () => {
        if (controller.signal.aborted || !input.isCurrent()) {
          const error = new Error("Confirmation no longer belongs to this research."); error.name = "AbortError"; throw error;
        }
      };
      assertCurrent();
      const body: ConfirmChallengeRequest = { command: input.reserved ? "CONFIRM_WITH_RESERVATIONS" : "CONFIRM", unresolved_issues_acknowledged: input.acknowledged, ...(input.reserved ? { reservation_note: input.note } : {}) };
      const scope = JSON.stringify([input.ownerScope, input.challengeId]);
      const signature = JSON.stringify(body);
      let receipt = receipts.get(scope);
      if (!receipt) {
        receipt = { scope, signature, confirmKey: keyFactory(), worldlineKey: keyFactory() };
        receipts.set(scope, receipt);
      }
      // Changing payload under an uncertain receipt could duplicate or contradict an accepted confirmation.
      if (receipt.signature !== signature) throw new ConfirmationInputChanged();
      const current = receipt;
      if (current.result) return current.result;
      active = controller;
      try {
        if (!current.challenge) {
          const response = await input.api.confirmChallenge(input.challengeId, body, { signal: controller.signal, idempotencyKey: current.confirmKey });
          const expected = input.reserved ? "confirmed_with_reservations" : "confirmed";
          if (response.challenge.challenge_id !== input.challengeId || response.challenge.status !== "CONFIRMED" || response.challenge.user_confirmation !== expected) throw new Error("Server confirmation result is inconsistent.");
          current.challenge = response.challenge;
        }
        assertCurrent();
        const response = await input.api.createWorldlines({ challenge_id: input.challengeId, horizon_end: "2028-08-31" }, { signal: controller.signal, idempotencyKey: current.worldlineKey });
        assertCurrent();
        if (response.bundle.challenge_id !== input.challengeId) throw new Error("Worldline belongs to another research.");
        const branches = worldlineBundleToBranches(response.bundle, input.language);
        if (branches.length !== 3 || new Set(branches.map((branch) => branch.kind)).size !== 3 || branches.some((branch) => !["weak", "main", "strong"].includes(branch.kind))) throw new Error("Worldline branches are incomplete.");
        current.result = { challenge: current.challenge, branches };
        return current.result;
      } finally { active = null; }
    },
  };
}
