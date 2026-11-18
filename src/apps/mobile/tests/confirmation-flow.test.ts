import { describe, expect, it, vi } from "vitest";
import type { ChallengeRecord, WorldlineBundle } from "@foresightnine/contracts";
vi.mock("expo-crypto", () => ({ getRandomBytes: () => new Uint8Array(16) }));
import { ConfirmationInputChanged, continueAfterConfirmation, createConfirmationFlow } from "../src/confirmation-flow";
import { legacyProbabilityAssessments, legacyWorldlines } from "./fixtures/legacy-web-worldlines";

const record: ChallengeRecord = { schema_version: "v1", created_at: "2026-10-03T00:00:00Z", content_hash: "a".repeat(64), source_refs: [], turn_id: "qa-confirm", challenge_id: "qa-challenge", thesis_id: "qa-thesis", claims: [], objections: [], missing_evidence: [], questions: [], rubric_scores: [], unresolved_issues: [], citation_ids: [], public_fact_citation_ids: [], native_search_used: false, provenance: "synthetic", user_confirmation: "confirmed_with_reservations", private_evidence_declared: false, status: "CONFIRMED" };
const bundle: WorldlineBundle = { schema_version: "v1", created_at: "2026-10-03T00:00:00Z", source_refs: [], content_hash: "b".repeat(64), bundle_id: "qa-bundle", challenge_id: record.challenge_id, worldlines: legacyWorldlines, probability_assessments: legacyProbabilityAssessments };
function deferred<T>() { let resolve!: (value: T) => void; let reject!: (error: Error) => void; const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
function setup() {
  let key = 0; const factory = vi.fn(() => `synthetic-idempotency-${++key}`);
  const flow = createConfirmationFlow(factory);
  const confirmChallenge = vi.fn().mockResolvedValue({ challenge: record });
  const createWorldlines = vi.fn().mockResolvedValue({ bundle });
  const input = { api: { confirmChallenge, createWorldlines }, ownerScope: "account:synthetic-a", challengeId: record.challenge_id, reserved: true, acknowledged: true, note: "Synthetic unresolved evidence", language: "en" as const, isCurrent: () => true };
  return { flow, input, factory, confirmChallenge, createWorldlines };
}

describe("two-stage worldline confirmation", () => {
  it("waits for confirmation and complete worldlines, keeping their keys distinct", async () => {
    const h = setup(); const pending = deferred<{ bundle: WorldlineBundle }>(); h.createWorldlines.mockReturnValue(pending.promise);
    let completed = false; const work = h.flow.run(h.input).then((result) => { completed = true; return result; });
    await Promise.resolve(); expect(h.confirmChallenge).toHaveBeenCalledOnce(); expect(h.createWorldlines).toHaveBeenCalledOnce(); expect(completed).toBe(false);
    expect(h.confirmChallenge.mock.calls[0]?.[2]?.idempotencyKey).not.toBe(h.createWorldlines.mock.calls[0]?.[1]?.idempotencyKey);
    expect(h.createWorldlines.mock.calls[0]?.[0]).toEqual({ challenge_id: record.challenge_id, horizon_end: "2028-08-31" });
    pending.resolve({ bundle }); expect((await work)?.branches).toHaveLength(3); expect(h.flow.isBusy()).toBe(false);
  });
  it("ignores concurrent confirmation clicks rather than opening two model operations", async () => {
    const h = setup(); const pending = deferred<{ challenge: ChallengeRecord }>(); h.confirmChallenge.mockReturnValue(pending.promise);
    const first = h.flow.run(h.input); expect(h.flow.isBusy()).toBe(true); expect(await h.flow.run(h.input)).toBeNull();
    pending.resolve({ challenge: record }); await first; expect(h.confirmChallenge).toHaveBeenCalledOnce(); expect(h.createWorldlines).toHaveBeenCalledOnce();
  });
  it("reuses the confirmation key after a lost response", async () => {
    const h = setup(); h.confirmChallenge.mockRejectedValueOnce(new Error("synthetic response lost"));
    await expect(h.flow.run(h.input)).rejects.toThrow("response lost"); await h.flow.run(h.input);
    expect(h.confirmChallenge.mock.calls[0]?.[2]?.idempotencyKey).toBe(h.confirmChallenge.mock.calls[1]?.[2]?.idempotencyKey);
    expect(h.createWorldlines).toHaveBeenCalledOnce(); expect(h.factory).toHaveBeenCalledTimes(2);
  });
  it("does not reconfirm a server-confirmed challenge when worldline delivery failed", async () => {
    const h = setup(); h.createWorldlines.mockRejectedValueOnce(new Error("synthetic timeout"));
    await expect(h.flow.run(h.input)).rejects.toThrow("timeout"); const recovered = await h.flow.run(h.input);
    expect(recovered?.branches).toHaveLength(3); expect(h.confirmChallenge).toHaveBeenCalledOnce();
    expect(h.createWorldlines.mock.calls[0]?.[1]?.idempotencyKey).toBe(h.createWorldlines.mock.calls[1]?.[1]?.idempotencyKey);
  });
  it("reopens a completed result without issuing either request again", async () => {
    const h = setup(); const result = await h.flow.run(h.input); expect(await h.flow.run(h.input)).toBe(result);
    expect(h.confirmChallenge).toHaveBeenCalledOnce(); expect(h.createWorldlines).toHaveBeenCalledOnce();
  });
  it("cancels after confirmation without generating a worldline", async () => {
    const h = setup(); const pending = deferred<{ challenge: ChallengeRecord }>(); h.confirmChallenge.mockReturnValue(pending.promise);
    const work = h.flow.run(h.input); const rejected = expect(work).rejects.toMatchObject({ name: "AbortError" });
    h.flow.cancel(); pending.resolve({ challenge: record }); await rejected;
    expect(h.confirmChallenge.mock.calls[0]?.[2]?.signal.aborted).toBe(true); expect(h.createWorldlines).not.toHaveBeenCalled();
  });
  it("does not send a request for stale research", async () => {
    const h = setup(); await expect(h.flow.run({ ...h.input, isCurrent: () => false })).rejects.toMatchObject({ name: "AbortError" });
    expect(h.confirmChallenge).not.toHaveBeenCalled();
  });
  it("rejects late worldlines after the account or research changes", async () => {
    const h = setup(); const pending = deferred<{ bundle: WorldlineBundle }>(); h.createWorldlines.mockReturnValue(pending.promise);
    let current = true; const work = h.flow.run({ ...h.input, isCurrent: () => current }); const rejected = expect(work).rejects.toMatchObject({ name: "AbortError" });
    await Promise.resolve(); current = false; pending.resolve({ bundle }); await rejected;
  });
  it.each([{ challenge_id: "other" }, { status: "CHALLENGING" }, { user_confirmation: "confirmed" }])("rejects an inconsistent accepted confirmation", async (change) => {
    const h = setup(); h.confirmChallenge.mockResolvedValue({ challenge: { ...record, ...change } });
    await expect(h.flow.run(h.input)).rejects.toThrow("inconsistent"); expect(h.createWorldlines).not.toHaveBeenCalled(); expect(h.flow.isBusy()).toBe(false);
  });
  it("rejects another study's worldline bundle", async () => {
    const h = setup(); h.createWorldlines.mockResolvedValue({ bundle: { ...bundle, challenge_id: "other" } });
    await expect(h.flow.run(h.input)).rejects.toThrow("another research");
  });
  it("requires all three distinct branch kinds", async () => {
    const h = setup(); h.createWorldlines.mockResolvedValue({ bundle: { ...bundle, worldlines: bundle.worldlines.map((worldline) => ({ ...worldline, branch_kind: "weak" })) } });
    await expect(h.flow.run(h.input)).rejects.toThrow("incomplete");
  });
  it("does not change a payload under an uncertain confirmation receipt", async () => {
    const h = setup(); h.confirmChallenge.mockRejectedValueOnce(new Error("synthetic unknown outcome"));
    await expect(h.flow.run(h.input)).rejects.toThrow();
    await expect(h.flow.run({ ...h.input, note: "Different reservation" })).rejects.toBeInstanceOf(ConfirmationInputChanged);
    expect(h.confirmChallenge).toHaveBeenCalledOnce();
  });
  it("does not share receipts across account scopes", async () => {
    const h = setup(); await h.flow.run(h.input); await h.flow.run({ ...h.input, ownerScope: "account:synthetic-b" });
    expect(h.confirmChallenge).toHaveBeenCalledTimes(2);
    expect(h.confirmChallenge.mock.calls[0]?.[2]?.idempotencyKey).not.toBe(h.confirmChallenge.mock.calls[1]?.[2]?.idempotencyKey);
  });
  it("keeps the original account's completed receipt when switching away and back", async () => {
    const h = setup(); const first = await h.flow.run(h.input);
    await h.flow.run({ ...h.input, ownerScope: "account:synthetic-b" });
    expect(await h.flow.run(h.input)).toBe(first); expect(h.confirmChallenge).toHaveBeenCalledTimes(2); expect(h.createWorldlines).toHaveBeenCalledTimes(2);
  });
  it("sends normal confirmation without a reservation note", async () => {
    const h = setup(); h.confirmChallenge.mockResolvedValue({ challenge: { ...record, user_confirmation: "confirmed" } });
    await h.flow.run({ ...h.input, reserved: false }); expect(h.confirmChallenge.mock.calls[0]?.[1]).toEqual({ command: "CONFIRM", unresolved_issues_acknowledged: true });
  });
});

describe("confirmation navigation waits for readiness and focus", () => {
  it("does not navigate while pending, and moves only after success", async () => {
    const pending = deferred<boolean>(); const navigate = vi.fn();
    const work = continueAfterConfirmation(() => pending.promise, true, () => true, navigate);
    expect(navigate).not.toHaveBeenCalled(); pending.resolve(true); expect(await work).toBe(true); expect(navigate).toHaveBeenCalledOnce();
  });
  it("stays on review after failure", async () => { const navigate = vi.fn(); expect(await continueAfterConfirmation(async () => false, true, () => true, navigate)).toBe(false); expect(navigate).not.toHaveBeenCalled(); });
  it("does not drag the user back after they navigate elsewhere", async () => { const navigate = vi.fn(); expect(await continueAfterConfirmation(async () => true, true, () => false, navigate)).toBe(false); expect(navigate).not.toHaveBeenCalled(); });
});
