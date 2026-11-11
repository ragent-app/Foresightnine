import { describe, expect, it, vi } from "vitest";
import type { ChallengeRecord } from "@foresightnine/contracts";
vi.mock("expo-crypto", () => ({ getRandomBytes: () => new Uint8Array(16) }));
import { applyAcceptedEvidence, createEvidenceSubmission, EvidenceInputChanged } from "../src/evidence-submission";
import { normalizeShareToBundle } from "../src/evidence";
import { mobileInitialState } from "../src/lifecycle";

const record: ChallengeRecord = { schema_version: "v1", created_at: "2026-10-03T00:00:00Z", content_hash: "a".repeat(64), source_refs: [], turn_id: "qa-evidence-turn", challenge_id: "qa-challenge", thesis_id: "qa-thesis", claims: [], objections: ["Updated objection"], missing_evidence: [], questions: ["Updated question"], rubric_scores: [{ criterion: "evidence_quality", score: 2, rationale: "Synthetic accepted source" }], unresolved_issues: [], citation_ids: [], public_fact_citation_ids: [], native_search_used: false, provenance: "synthetic", user_confirmation: "pending", private_evidence_declared: false, status: "CHALLENGING" };
const normalized = normalizeShareToBundle("https://example.com/research Synthetic test source", undefined, "en");
if (!normalized.ok || !normalized.bundle) throw new Error("Invalid synthetic evidence fixture");
const evidence = normalized.bundle;
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done; }); return { promise, resolve }; }
function setup() {
  let key = 0;
  const submitEvidence = vi.fn().mockResolvedValue({ challenge: record });
  const flow = createEvidenceSubmission(() => `synthetic-${++key}`);
  const input = { api: { submitEvidence }, ownerScope: "account:synthetic-a", challengeId: record.challenge_id, evidence, isCurrent: () => true };
  return { flow, input, submitEvidence };
}
describe("evidence submission receipts", () => {
  it("waits for server acceptance and permits only one pending request", async () => {
    const h = setup(); const pending = deferred<{ challenge: ChallengeRecord }>(); h.submitEvidence.mockReturnValue(pending.promise);
    let completed = false; const first = h.flow.submit(h.input).then((result) => { completed = true; return result; });
    expect(h.flow.isBusy()).toBe(true); expect(await h.flow.submit(h.input)).toBeNull(); expect(completed).toBe(false);
    pending.resolve({ challenge: record }); expect(await first).toBe(record); expect(h.flow.isBusy()).toBe(false); expect(h.submitEvidence).toHaveBeenCalledOnce();
  });
  it("retries an uncertain outcome with original body/key despite a new client timestamp/hash", async () => {
    const h = setup(); h.submitEvidence.mockRejectedValueOnce(new Error("Synthetic timeout"));
    await expect(h.flow.submit(h.input)).rejects.toThrow("timeout");
    await h.flow.submit({ ...h.input, evidence: { ...evidence, retrieved_at: "2026-10-04T00:00:00Z", content_hash: "b".repeat(64) } });
    expect(h.submitEvidence.mock.calls[0]?.[1]).toEqual(h.submitEvidence.mock.calls[1]?.[1]);
    expect(h.submitEvidence.mock.calls[0]?.[2]?.idempotencyKey).toBe(h.submitEvidence.mock.calls[1]?.[2]?.idempotencyKey);
  });
  it("does not resubmit completed evidence", async () => {
    const h = setup(); await h.flow.submit(h.input); await h.flow.submit(h.input); expect(h.submitEvidence).toHaveBeenCalledOnce();
  });
  it("rejects changed content under an uncertain receipt", async () => {
    const h = setup(); h.submitEvidence.mockRejectedValueOnce(new Error("Synthetic timeout")); await expect(h.flow.submit(h.input)).rejects.toThrow();
    await expect(h.flow.submit({ ...h.input, evidence: { ...evidence, user_note: "Changed" } })).rejects.toBeInstanceOf(EvidenceInputChanged); expect(h.submitEvidence).toHaveBeenCalledOnce();
  });
  it("scopes keys to account and research", async () => {
    const h = setup(); await h.flow.submit(h.input);
    await h.flow.submit({ ...h.input, ownerScope: "account:synthetic-b" });
    h.submitEvidence.mockResolvedValue({ challenge: { ...record, challenge_id: "qa-other" } });
    await h.flow.submit({ ...h.input, challengeId: "qa-other" });
    expect(new Set(h.submitEvidence.mock.calls.map((call) => call[2].idempotencyKey)).size).toBe(3);
  });
  it("rejects an already-stale request without sending", async () => {
    const h = setup(); await expect(h.flow.submit({ ...h.input, isCurrent: () => false })).rejects.toMatchObject({ name: "AbortError" }); expect(h.submitEvidence).not.toHaveBeenCalled();
  });
  it("rejects a late response when the account or study changes", async () => {
    const h = setup(); const pending = deferred<{ challenge: ChallengeRecord }>(); h.submitEvidence.mockReturnValue(pending.promise);
    let current = true; const work = h.flow.submit({ ...h.input, isCurrent: () => current }); const check = expect(work).rejects.toMatchObject({ name: "AbortError" });
    current = false; pending.resolve({ challenge: record }); await check;
  });
  it("cancels stale UI work while retaining an accepted receipt for later recovery", async () => {
    const h = setup(); const pending = deferred<{ challenge: ChallengeRecord }>(); h.submitEvidence.mockReturnValue(pending.promise);
    const work = h.flow.submit(h.input); const check = expect(work).rejects.toMatchObject({ name: "AbortError" });
    h.flow.cancel(); expect(h.submitEvidence.mock.calls[0]?.[2]?.signal.aborted).toBe(true);
    pending.resolve({ challenge: record }); await check;
    expect(await h.flow.submit(h.input)).toBe(record); expect(h.submitEvidence).toHaveBeenCalledOnce();
  });
  it.each([{ challenge_id: "other" }, { status: "CONFIRMED" }])("rejects an inconsistent response: %j", async (change) => {
    const h = setup(); h.submitEvidence.mockResolvedValueOnce({ challenge: { ...record, ...change } });
    await expect(h.flow.submit(h.input)).rejects.toThrow("inconsistent"); expect(h.flow.isBusy()).toBe(false);
  });
});
describe("accepted evidence updates", () => {
  it("adds the source and updates formal questions/rubric while retaining the prior conversation and reply draft", () => {
    const before = { ...mobileInitialState, replyDraft: "Unsent reply", challengeTurns: [{ id: "prior", author: "user" as const, body: "Prior response" }], exportNotice: "Prior error" };
    const after = applyAcceptedEvidence(before, evidence, record);
    expect(after.evidence).toEqual([evidence]); expect(after.challengeTurns[0]).toEqual(before.challengeTurns[0]);
    expect(after.challengeTurns.at(-1)?.review?.questions).toEqual(["Updated question"]);
    expect(after.rubric.find((item) => item.id === "evidence")?.score).toBe(2); expect(after.replyDraft).toBe("Unsent reply");
    expect(after.challengeStatus).toBe("complete"); expect(after.exportNotice).toBe(""); expect(before.evidence).toHaveLength(0);
  });
  it("does not duplicate ledger entries or conversation turns on recovery", () => {
    const once = applyAcceptedEvidence(mobileInitialState, evidence, record); const twice = applyAcceptedEvidence(once, evidence, record);
    expect(twice.evidence).toHaveLength(1); expect(twice.challengeTurns).toEqual(once.challengeTurns);
  });
});
