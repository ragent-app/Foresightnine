import { describe, expect, it } from "vitest";
import { ApiError } from "@foresightnine/api-client";
import type { ChallengeRecord } from "@foresightnine/contracts";
import { apiErrorNotice, challengeToUi, operationErrorNotice } from "./api-runtime";

function apiError(status: number, retryable: boolean, retryAfter?: number): ApiError {
  return new ApiError(status, {
    schema_version: "v1",
    error_code: status === 429 ? "quota_exceeded" : "internal_error",
    message: "redacted",
    request_id: "request:test",
    retryable,
    details: [],
  }, retryAfter);
}

describe("apiErrorNotice", () => {
  it("distinguishes provider unavailability from rate limiting", () => {
    expect(apiErrorNotice(apiError(503, true, 15), "zh")).toBe("模型供应商暂时不可用。 请在 15 秒后重试。");
    expect(apiErrorNotice(apiError(503, true, 15), "en")).toBe("The model provider is temporarily unavailable. Retry after 15s.");
  });

  it("keeps quota feedback specific", () => {
    expect(apiErrorNotice(apiError(429, true, 30), "zh")).toBe("API 触发限流。 请在 30 秒后重试。");
    expect(apiErrorNotice(apiError(429, true, 30), "en")).toBe("API rate limit reached. Retry after 30s.");
  });

  it("maps retryable SSE operation failures to the provider-unavailable notice", () => {
    const response = {
      schema_version: "v1" as const,
      error_code: "internal_error" as const,
      message: "redacted",
      request_id: "request:sse",
      retryable: true,
      details: [],
    };
    expect(operationErrorNotice(response, true, "zh")).toBe("模型供应商暂时不可用。");
    expect(operationErrorNotice(response, true, "en")).toBe("The model provider is temporarily unavailable.");
  });
});

function challenge(status: ChallengeRecord["status"], turnId: string): ChallengeRecord {
  return {
    schema_version: "v1", created_at: "2026-08-10T00:00:00Z", source_refs: [{ ref_id: "mock:challenge", source_type: "model_inference", label: "test" }], content_hash: "a".repeat(64),
    turn_id: turnId, challenge_id: "challenge:truthful", thesis_id: "thesis:truthful", claims: ["Claim"], objections: ["Objection"], missing_evidence: status === "EVIDENCE_REQUIRED" ? ["Evidence"] : [], questions: ["Question"],
    rubric_scores: ["premise_clarity", "time_boundary", "trigger_observability", "causal_chain", "counterfactuals", "evidence_quality", "asset_transmission", "already_priced", "falsifiability", "user_confirmation"].map((criterion) => ({ criterion: criterion as ChallengeRecord["rubric_scores"][number]["criterion"], score: 3, rationale: "Test rationale" })),
    unresolved_issues: status === "EVIDENCE_REQUIRED" ? ["Issue"] : [], citation_ids: [], public_fact_citation_ids: [], native_search_used: false, provenance: "test", user_confirmation: status === "CONFIRMED" ? "confirmed" : "pending", private_evidence_declared: false, status,
  };
}

describe("challengeToUi", () => {
  it("retains chronology and marks completed model operations ready for the next user action", () => {
    const initial = challengeToUi(challenge("CHALLENGING", "turn:1"), "Initial thesis", "en", "initial");
    const replied = challengeToUi(challenge("EVIDENCE_REQUIRED", "turn:2"), "Initial thesis", "en", "reply", "My dated reply", initial.turns);
    expect(initial.status).toBe("complete");
    expect(replied.status).toBe("complete");
    expect(replied.turns.map((turn) => [turn.author, turn.body])).toEqual([
      ["user", "Initial thesis"], ["challenger", expect.stringContaining("Objection")],
      ["user", "My dated reply"], ["challenger", expect.stringContaining("Objection")],
    ]);
    expect(challengeToUi(challenge("CONFIRMED", "turn:2"), "Initial thesis", "en", "reply", "My dated reply", initial.turns).status).toBe("complete");
  });

  it("preserves the provider's structured review sections instead of flattening them into one paragraph", () => {
    const record = challenge("EVIDENCE_REQUIRED", "turn:structured");
    const update = challengeToUi(record, "Initial thesis", "en", "initial");
    const reviewer = update.turns.find((turn) => turn.author === "challenger");

    expect(reviewer?.review).toEqual({
      objections: ["Objection"],
      missingEvidence: ["Evidence"],
      questions: ["Question"],
    });
  });

  it("retains a cancelled initial transcript when a terminal operation is restarted", () => {
    const prior = [
      { id: "api-user-pending", author: "user" as const, time: "now", body: "Initial thesis" },
      { id: "cancelled-2", author: "system" as const, time: "now", body: "Cancelled; retained." },
    ];
    const restarted = challengeToUi(challenge("CHALLENGING", "turn:restart"), "Initial thesis", "en", "initial", "", prior);
    expect(restarted.turns.map((turn) => turn.author)).toEqual(["user", "system", "challenger"]);
    expect(restarted.turns.filter((turn) => turn.author === "user")).toHaveLength(1);
  });

  it("reconciles an evidence response without inventing a user reply", () => {
    const initial = challengeToUi(challenge("CHALLENGING", "turn:1"), "Initial thesis", "en", "initial");
    const reconciled = challengeToUi(challenge("EVIDENCE_REQUIRED", "turn:evidence"), "Initial thesis", "en", "evidence", "", initial.turns);
    expect(reconciled.turns.map((turn) => turn.author)).toEqual(["user", "challenger", "challenger"]);
    expect(reconciled.turns.at(-1)?.id).toBe("api-challenger-turn:evidence");
    expect(reconciled.rubric).toEqual(expect.arrayContaining([expect.objectContaining({ score: 3 })]));
  });
});
