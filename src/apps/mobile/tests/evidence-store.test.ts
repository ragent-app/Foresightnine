import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ChallengeRecord } from "@foresightnine/contracts";
import type { MobileStore } from "../src/app-store";
// Executes the real provider methods with deterministic state/ref hooks, not a mounted native renderer.
const h = vi.hoisted(() => ({ slots: [] as unknown[], cursor: 0, effects: [] as Array<() => void | (() => void)>, owner: null as unknown, refresh: vi.fn(), api: { getChallenge: vi.fn(), createSession: vi.fn(), createChallenge: vi.fn(), submitEvidence: vi.fn(), submitChallengeReply: vi.fn() } }));
vi.mock("react", async (original) => ({ ...await original<typeof import("react")>(),
  useState: (initial: unknown) => {
    const i = h.cursor++;
    if (!(i in h.slots)) h.slots[i] = typeof initial === "function" ? initial() : initial;
    return [h.slots[i], (next: unknown) => { h.slots[i] = typeof next === "function" ? next(h.slots[i]) : next; }];
  },
  useRef: (initial: unknown) => { const i = h.cursor++; if (!(i in h.slots)) h.slots[i] = { current: initial }; return h.slots[i]; },
  useCallback: (callback: unknown) => callback, useMemo: (factory: () => unknown) => factory(), useEffect: (effect: () => void | (() => void)) => { h.effects.push(effect); },
}));
vi.mock("react-native", () => ({ AppState: { addEventListener: () => ({ remove: vi.fn() }) } }));
vi.mock("expo-crypto", () => ({ getRandomBytes: () => new Uint8Array(16) }));
vi.mock("@foresightnine/market-data", async (original) => {
  const actual = await original<typeof import("@foresightnine/market-data")>();
  return { ...actual, createMarketRuntimeController: () => ({ ...actual.createMarketRuntimeController(), refresh: h.refresh }) };
});
vi.mock("../src/api-runtime", async (original) => ({ ...await original<typeof import("../src/api-runtime")>(), createMobileApiClient: () => h.api }));
vi.mock("../src/oauth-session", () => ({ useOAuthSession: () => ({ getSession: () => h.owner, session: h.owner, invalidateSession: vi.fn() }) }));
vi.mock("../src/storage", () => ({ loadDraft: vi.fn(), loadLanguage: vi.fn(), saveDraft: vi.fn(async () => {}), saveLanguage: vi.fn(async () => {}) }));
vi.mock("../src/adapters/native-archive", () => ({ loadEncryptedWorkspace: vi.fn(async () => ({ status: "empty" })), saveEncryptedWorkspace: vi.fn(async () => ({})), archiveExists: vi.fn(), loadEncryptedArchive: vi.fn(), saveEncryptedArchive: vi.fn(), sharePrivacySafeDocument: vi.fn() }));
import { AppProvider } from "../src/app-store";
import { normalizeShareToBundle } from "../src/evidence";
import { ASSETS, FIXTURE_STATE, MARKET_SNAPSHOT_BUNDLE } from "../src/fixtures";
import { canEnterWorldline } from "../src/validation";
import { archiveExists, loadEncryptedArchive, saveEncryptedArchive, loadEncryptedWorkspace, saveEncryptedWorkspace } from "../src/adapters/native-archive";
import { buildResearchSnapshot } from "../src/archive";
import { mobileInitialState } from "../src/lifecycle";
import { portfolioMetadata } from "../src/portfolio";
import type { WorkspaceDocument } from "../src/workspace-state";
import { loadDraft, loadLanguage, saveDraft } from "../src/storage";

const before: ChallengeRecord = { schema_version: "v1", created_at: "2026-10-03T00:00:00Z", content_hash: "a".repeat(64), source_refs: [], turn_id: "qa-original", challenge_id: "qa-challenge", thesis_id: "qa-thesis", claims: [], objections: [], missing_evidence: ["Source needed"], questions: ["Original question"], rubric_scores: [], unresolved_issues: [], citation_ids: [], public_fact_citation_ids: [], native_search_used: false, provenance: "synthetic", user_confirmation: "pending", private_evidence_declared: false, status: "EVIDENCE_REQUIRED" };
const accepted: ChallengeRecord = { ...before, turn_id: "qa-accepted", questions: ["New question"], rubric_scores: [{ criterion: "evidence_quality", score: 2, rationale: "Synthetic receipt" }], status: "CHALLENGING" };
const normalized = normalizeShareToBundle("https://example.com/research Synthetic source", undefined, "en");
if (!normalized.ok || !normalized.bundle) throw new Error("Invalid synthetic bundle");
const evidence = normalized.bundle;
function render(storageScope?: string | null): MobileStore { h.cursor = 0; return AppProvider({ children: null, ...(storageScope === undefined ? {} : { storageScope }) }).props.value as MobileStore; }
const flush = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
async function ready() { render().startChallenge(); await flush(); return render(); }
beforeEach(() => {
  vi.clearAllMocks(); h.slots = []; h.cursor = 0; h.effects = []; h.owner = null;
  vi.mocked(loadEncryptedWorkspace).mockResolvedValue({ status: "empty" }); vi.mocked(saveEncryptedWorkspace).mockResolvedValue({} as never);
  vi.mocked(loadDraft).mockResolvedValue(null); vi.mocked(loadLanguage).mockResolvedValue(null); vi.mocked(archiveExists).mockResolvedValue(false); vi.mocked(saveDraft).mockResolvedValue(undefined);
  h.refresh.mockResolvedValue(MARKET_SNAPSHOT_BUNDLE);
  h.api.getChallenge.mockResolvedValue({ challenge: before, operation: { status: "completed", operation_id: "synthetic-operation-id" } });
  h.api.createSession.mockResolvedValue({ session: { session_id: "synthetic-session" } });
  h.api.createChallenge.mockResolvedValue({ challenge: before });
  h.api.submitEvidence.mockResolvedValue({ challenge: accepted });
  h.api.submitChallengeReply.mockResolvedValue({ challenge: before });
});
describe("AppProvider evidence integration", () => {
  it("does not fall back to a local success without an API challenge", async () => {
    expect(await render().addEvidence(evidence)).toBe("failed"); expect(render().state.evidence).toHaveLength(0); expect(h.api.submitEvidence).not.toHaveBeenCalled();
  });
  it("waits for acceptance then publishes returned questions and scores", async () => {
    const store = await ready(); let finish!: (response: { challenge: ChallengeRecord }) => void;
    h.api.submitEvidence.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const work = store.addEvidence(evidence); expect(render().evidenceBusy).toBe(true); expect(render().state.evidence).toHaveLength(0);
    finish({ challenge: accepted }); expect(await work).toBe("added");
    const final = render(); expect(final.evidenceBusy).toBe(false); expect(final.state.evidence).toHaveLength(1);
    expect(final.state.challengeTurns.at(-1)?.review?.questions).toEqual(["New question"]); expect(final.state.rubric.find((item) => item.id === "evidence")?.score).toBe(2);
  });
  it("keeps the ledger unchanged when the server rejects evidence", async () => {
    const store = await ready(); h.api.submitEvidence.mockRejectedValueOnce(new Error("Synthetic failure"));
    expect(await store.addEvidence(evidence)).toBe("failed"); expect(render().state.evidence).toHaveLength(0); expect(render().evidenceBusy).toBe(false);
  });
  it("does not resubmit an already-accepted source", async () => {
    await (await ready()).addEvidence(evidence); expect(await render().addEvidence(evidence)).toBe("duplicate"); expect(h.api.submitEvidence).toHaveBeenCalledOnce();
  });
  it("does not attach evidence to the previous research after editing the thesis", async () => {
    const store = await ready(); store.updateDraft({ viewpoint: "A completely new research premise" });
    expect(await render().addEvidence(evidence)).toBe("failed"); expect(h.api.submitEvidence).not.toHaveBeenCalled();
  });
  it("does not publish late evidence after switching account", async () => {
    const store = await ready(); let finish!: (response: { challenge: ChallengeRecord }) => void;
    h.api.submitEvidence.mockReturnValue(new Promise((resolve) => { finish = resolve; })); const work = store.addEvidence(evidence);
    h.owner = { account: { account_id: "synthetic-other-account" } }; finish({ challenge: accepted });
    expect(await work).toBe("failed"); expect(render().state.evidence).toHaveLength(0);
  });
  it("does not run evidence concurrently with an outstanding review reply", async () => {
    const store = await ready(); let finish!: (response: { challenge: ChallengeRecord }) => void;
    h.api.submitChallengeReply.mockReturnValue(new Promise((resolve) => { finish = resolve; })); store.reply("Synthetic answer");
    expect(await store.addEvidence(evidence)).toBe("busy"); expect(h.api.submitEvidence).not.toHaveBeenCalled();
    finish({ challenge: before }); await flush(); expect(await render().addEvidence(evidence)).toBe("added");
  });
});

describe("AppProvider scenario drafts", () => {
  it("keeps separate draft values while switching reached worldlines", async () => {
    const store = await ready(); store.setScenarioReturn("GRID", "10");
    expect(render().scenarioDrafts.main?.GRID).toBe("10"); render().selectBranch("weak"); render().setScenarioReturn("GRID", "-5");
    expect(render().scenarioDrafts.weak?.GRID).toBe("-5"); render().selectBranch("main"); expect(render().scenarioDrafts.main?.GRID).toBe("10");
  });
  it("hides draft assumptions immediately on account or thesis change", async () => {
    (await ready()).setScenarioReturn("GRID", "10"); expect(render().scenarioDrafts.main?.GRID).toBe("10");
    h.owner = { account: { account_id: "synthetic-new-owner" } }; expect(render().scenarioDrafts).toEqual({});
    render().setScenarioReturn("GRID", "3"); render().updateDraft({ viewpoint: "Different research" }); expect(render().scenarioDrafts).toEqual({});
  });
  it("does not allow arbitrary asset identifiers or cash yield editing", async () => {
    const store = await ready(); store.setScenarioReturn("unknown", "10"); store.setScenarioReturn("CASH", "3"); expect(render().scenarioDrafts).toEqual({});
  });
});

// These deferred transports deliberately ignore AbortSignal to exercise late delivery.
describe("AppProvider request lifecycle", () => {
  it("does not create a challenge after the session request is cancelled", async () => {
    let finish!: (value: unknown) => void;
    h.api.createSession.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    render().startChallenge(); render().cancelChallenge();
    finish({ session: { session_id: "late-session" } }); await flush();
    expect(h.api.createChallenge).not.toHaveBeenCalled(); expect(render().state.challengeStatus).toBe("cancelled");
  });
  it("does not create a challenge under a newly signed-in account", async () => {
    let finish!: (value: unknown) => void;
    h.api.createSession.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    render().startChallenge(); h.owner = { account: { account_id: "new-account" } };
    finish({ session: { session_id: "previous-account-session" } }); await flush();
    expect(h.api.createChallenge).not.toHaveBeenCalled();
  });
  it("ignores late challenge success after editing the thesis", async () => {
    let finish!: (value: unknown) => void;
    h.api.createChallenge.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    render().startChallenge(); await flush(); render().updateDraft({ viewpoint: "New thesis" });
    finish({ challenge: before }); await flush();
    expect(render().state.challengeTurns).toEqual([]); expect(render().state.challengeStatus).toBe("idle");
    expect(await render().addEvidence(evidence)).toBe("failed");
  });
  it("ignores old failure after a newer research has succeeded", async () => {
    let reject!: (error: Error) => void;
    h.api.createChallenge.mockReturnValueOnce(new Promise((_resolve, fail) => { reject = fail; }));
    render().startChallenge(); await flush(); render().updateDraft({ viewpoint: "Second thesis" }); render().startChallenge(); await flush();
    expect(render().state.challengeStatus).toBe("complete"); reject(new Error("old response")); await flush();
    expect(render().state.challengeStatus).toBe("complete"); expect(render().state.exportNotice).toBe("");
  });
  it("single-flights rapid start clicks", async () => {
    let finish!: (value: unknown) => void;
    h.api.createSession.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    const store = render(); store.startChallenge(); store.startChallenge();
    expect(h.api.createSession).toHaveBeenCalledOnce(); finish({ session: { session_id: "one-session" } }); await flush();
  });
  it("keeps cancellation visible when an aborted reply reports an error", async () => {
    let reject!: (error: Error) => void;
    const store = await ready(); h.api.submitChallengeReply.mockReturnValueOnce(new Promise((_resolve, fail) => { reject = fail; }));
    store.reply("Draft answer"); render().cancelChallenge(); reject(new Error("cancelled transport")); await flush();
    expect(render().state.challengeStatus).toBe("cancelled");
  });
  it("does not append a previous-account reply to the current transcript", async () => {
    let finish!: (value: unknown) => void;
    const store = await ready(); const original = store.state.challengeTurns;
    h.api.submitChallengeReply.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    store.reply("Old account answer"); h.owner = { account: { account_id: "new-owner" } };
    finish({ challenge: accepted }); await flush(); expect(render().state.challengeTurns).toEqual(original);
  });
  it("single-flights rapid reply clicks without discarding the draft", async () => {
    let finish!: (value: unknown) => void;
    const store = await ready(); store.setReplyDraft("answer");
    h.api.submitChallengeReply.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    store.reply("answer"); store.reply("answer"); expect(h.api.submitChallengeReply).toHaveBeenCalledOnce();
    expect(render().state.replyDraft).toBe("answer"); finish({ challenge: accepted }); await flush(); expect(render().state.replyDraft).toBe("");
  });
  it("does not fake an offline reply when API challenge connection is missing", () => {
    render().reply("Unconnected answer"); expect(render().state.exportNotice).toContain("审查");
    expect(h.api.submitChallengeReply).not.toHaveBeenCalled();
  });
});

async function fmReady() {
  (await ready()).reply("A real-shaped synthetic answer"); await flush();
  // Seed a reached, reserved-confirmation checkpoint; the production gate is still evaluated.
  h.slots[0] = { ...render().state, confirmation: "reserved", unresolvedIssuesAcknowledged: true, reservationNote: "Synthetic unresolved issue", stage: "worldline" };
  const store = render(); expect(canEnterWorldline(store.state)).toBe(true); return store;
}
describe("AppProvider FM and restore lifecycle", () => {
  it("does not jump back to portfolio from a previous research's late FM success", async () => {
    let finish!: (value: typeof MARKET_SNAPSHOT_BUNDLE) => void;
    const store = await fmReady(); h.refresh.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    store.runFm(); render().updateDraft({ viewpoint: "Replacement thesis" }); finish(MARKET_SNAPSHOT_BUNDLE); await flush();
    expect(render().state.stage).toBe("thesis"); expect(render().state.fmStatus).toBe("idle"); expect(render().state.fmResult).toBeNull();
  });
  it("ignores previous research's FM failure", async () => {
    let reject!: (error: Error) => void;
    const store = await fmReady(); h.refresh.mockReturnValueOnce(new Promise((_resolve, fail) => { reject = fail; }));
    store.runFm(); render().updateDraft({ viewpoint: "Replacement thesis" }); reject(new Error("old FM")); await flush();
    expect(render().state.fmStatus).toBe("idle"); expect(render().state.fmStep).toBe("");
  });
  it("does not publish an FM result after an account switch", async () => {
    let finish!: (value: typeof MARKET_SNAPSHOT_BUNDLE) => void;
    const store = await fmReady(); h.refresh.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    store.runFm(); h.owner = { account: { account_id: "other-FM-owner" } }; finish(MARKET_SNAPSHOT_BUNDLE); await flush();
    expect(render().state.fmResult).toBeNull();
  });
  it("single-flights FM clicks and allows retry after a current failure", async () => {
    let reject!: (error: Error) => void;
    const store = await fmReady(); h.refresh.mockReturnValueOnce(new Promise((_resolve, fail) => { reject = fail; }));
    store.runFm(); store.runFm(); expect(h.refresh).toHaveBeenCalledOnce(); reject(new Error("current error")); await flush();
    expect(render().state.fmStatus).toBe("failed"); render().runFm(); await flush();
    expect(h.refresh).toHaveBeenCalledTimes(2); expect(render().state.fmStatus).toBe("complete"); expect(render().state.fmResult).not.toBeNull();
  });
  it("does not overwrite a new study notice with an old archive restore result", async () => {
    let finish!: (value: null) => void;
    vi.mocked(loadEncryptedArchive).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    const work = render().restoreArchive(); render().updateDraft({ viewpoint: "New work while archive loads" }); finish(null); await work;
    expect(render().state.exportNotice).toBe(""); expect(render().state.viewpoint).toBe("New work while archive loads");
  });
});

describe("workspace hydration and archive ownership", () => {
  function mountEffects() { return [...h.effects].map(effect => effect()).filter((cleanup): cleanup is () => void => typeof cleanup === "function"); }
  const stored = { viewpoint: "Old stored study", capital: "100", currency: "USD" as const, language: "zh" as const, updatedAt: "2026-10-03T00:00:00Z" };
  it("does not read or persist a guest draft while authentication is loading", async () => {
    const store = render(null); mountEffects(); store.updateDraft({ viewpoint: "Synthetic pending edit" }); await flush();
    expect(loadDraft).not.toHaveBeenCalled(); expect(archiveExists).not.toHaveBeenCalled(); expect(saveDraft).not.toHaveBeenCalled();
  });
  it("does not overwrite typing with a late startup draft", async () => {
    let resolve!: (value: typeof stored) => void; vi.mocked(loadDraft).mockReturnValueOnce(new Promise(done => { resolve = done; }));
    render(); mountEffects(); render().updateDraft({ viewpoint: "Typed since startup" }); resolve(stored); await flush();
    expect(render().state.viewpoint).toBe("Typed since startup"); expect(loadDraft).toHaveBeenCalledWith("anonymous");
  });
  it("does not apply a draft that arrives after the workspace is unmounted", async () => {
    let resolve!: (value: typeof stored) => void; vi.mocked(loadDraft).mockReturnValueOnce(new Promise(done => { resolve = done; }));
    const before = render().state.viewpoint; const cleanups = mountEffects(); cleanups.forEach(cleanup => cleanup()); resolve(stored); await flush();
    expect(render().state.viewpoint).toBe(before);
  });
  it("does not overwrite a newly chosen language with delayed preferences", async () => {
    let resolve!: (value: "zh") => void; vi.mocked(loadLanguage).mockReturnValueOnce(new Promise(done => { resolve = done; }));
    render(); mountEffects(); render().setLanguage("en"); resolve("zh"); await flush(); expect(render().state.language).toBe("en");
  });
  it("does not change the language under a study started before preferences arrive", async () => {
    let resolve!: (value: "en") => void; vi.mocked(loadLanguage).mockReturnValueOnce(new Promise(done => { resolve = done; }));
    const originalLanguage = render().state.language; mountEffects(); render().startChallenge(); await flush(); resolve("en"); await flush();
    expect(render().state.language).toBe(originalLanguage);
  });
  it("surfaces failed draft writes without an unhandled rejection", async () => {
    vi.mocked(saveDraft).mockRejectedValueOnce(new Error("synthetic storage error")); render().updateDraft({ viewpoint: "Changed study" }); await flush();
    expect(render().state.exportNotice).toMatch(/本地草稿未保存|Local draft was not saved/);
  });
  it("surfaces a failed initial read without claiming restoration", async () => {
    vi.mocked(loadDraft).mockRejectedValueOnce(new Error("synthetic storage error")); render(); mountEffects(); await flush();
    expect(render().state.exportNotice).toMatch(/本地草稿读取失败|Local draft could not be read/); expect(render().state.draftRestored).toBe(false);
  });
  it("does not publish an old account's save result after switching accounts", async () => {
    h.owner = { account: { account_id: "A" } }; let finish!: () => void;
    vi.mocked(saveEncryptedArchive).mockReturnValueOnce(new Promise(resolve => { finish = () => resolve({} as never); }));
    const saving = render().saveArchive(false); expect(saveEncryptedArchive).toHaveBeenCalledWith(expect.anything(), "account:A");
    h.owner = { account: { account_id: "B" } }; render(); finish(); await saving;
    expect(render().archive).toBeNull(); expect(render().archiveAvailable).toBe(false);
  });
  it("requests archive restoration only from the current account namespace", async () => {
    h.owner = { account: { account_id: "A" } }; vi.mocked(loadEncryptedArchive).mockResolvedValueOnce(null);
    await render().restoreArchive(); expect(loadEncryptedArchive).toHaveBeenCalledWith("account:A");
  });
});

describe("automatic cold-start workspace", () => {
  beforeEach(() => vi.useFakeTimers()); afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); });
  function storedWorkspace(): WorkspaceDocument {
    const state = { ...mobileInitialState, viewpoint: "Saved private study", stage: "evidence" as const, challengeStatus: "complete" as const, challengeTurns: [{ id: "saved-system", author: "system" as const, body: "Synthetic research protocol" }, { id: "saved-q", author: "challenger" as const, body: "Saved question", review: { questions: ["What contradicts this claim?"], objections: [], missingEvidence: [] } }], replyDraft: "Draft reply before restart" };
    return { schema: "f9-mobile-workspace-v1", research: buildResearchSnapshot({ state, branches: [], methodData: { methodVersion: portfolioMetadata.method, dataAsOf: portfolioMetadata.dataAsOf, navLabel: portfolioMetadata.navLabel, fixtureState: FIXTURE_STATE, assetIds: ASSETS.map(a => a.id) } }, true), apiChallengeId: "saved-challenge", apiOperationId: "saved-operation-id", evidenceWorkspace: { sharedText: "Unsubmitted private source", searchQuery: "Saved query", candidates: [] }, scenarioDrafts: { main: { [ASSETS[0]!.id]: "-5" } }, assets: ASSETS };
  }
  function mount() { render(); [...h.effects].forEach(effect => effect()); }
  it("restores stage, questions, source drafts and scenario inputs without model calls", async () => {
    vi.mocked(loadEncryptedWorkspace).mockResolvedValueOnce({ status: "loaded", document: storedWorkspace() }); mount(); await flush(); const store = render();
    expect(store.workspaceReady).toBe(true); expect(store.state.stage).toBe("evidence"); expect(store.state.replyDraft).toBe("Draft reply before restart"); expect(store.evidenceWorkspace.sharedText).toBe("Unsubmitted private source"); expect(store.scenarioDrafts.main?.[ASSETS[0]!.id]).toBe("-5");
    expect(h.api.createSession).not.toHaveBeenCalled(); expect(h.api.createChallenge).not.toHaveBeenCalled(); expect(h.api.submitChallengeReply).not.toHaveBeenCalled();
  });
  it("continues an explicitly submitted reply against the saved research ID", async () => {
    vi.mocked(loadEncryptedWorkspace).mockResolvedValueOnce({ status: "loaded", document: storedWorkspace() }); mount(); await flush(); render().reply("My deliberate next reply"); await flush();
    expect(h.api.submitChallengeReply).toHaveBeenCalledWith("saved-challenge", { message: "My deliberate next reply" }, expect.anything()); expect(h.api.createChallenge).not.toHaveBeenCalled();
  });
  it("reconciles a known research ID using GET rather than starting a second model call", async () => {
    h.api.getChallenge.mockResolvedValueOnce({ challenge: { ...before, challenge_id: "saved-challenge" }, operation: { status: "completed", operation_id: "saved-operation-id" } });
    vi.mocked(loadEncryptedWorkspace).mockResolvedValueOnce({ status: "loaded", document: storedWorkspace() }); mount(); await flush(); render().retryChallenge(); await flush();
    expect(h.api.getChallenge).toHaveBeenCalledWith("saved-challenge", expect.objectContaining({ operationId: "saved-operation-id" })); expect(h.api.createChallenge).not.toHaveBeenCalled(); expect(h.api.submitChallengeReply).not.toHaveBeenCalled();
  });
  it.each([
    { challengeId: "other-challenge", operationId: "saved-operation-id", status: "completed" },
    { challengeId: "saved-challenge", operationId: "other-operation-id", status: "completed" },
    { challengeId: "saved-challenge", operationId: "saved-operation-id", status: "running" },
  ])("does not replace a recovered draft with a mismatched or incomplete receipt: %j", async receipt => {
    h.api.getChallenge.mockResolvedValueOnce({ challenge: { ...before, challenge_id: receipt.challengeId }, operation: { status: receipt.status, operation_id: receipt.operationId } });
    vi.mocked(loadEncryptedWorkspace).mockResolvedValueOnce({ status: "loaded", document: storedWorkspace() }); mount(); await flush();
    const turns = render().state.challengeTurns;
    render().retryChallenge(); await flush();
    expect(render().state.challengeTurns).toEqual(turns);
    expect(h.api.createChallenge).not.toHaveBeenCalled(); expect(h.api.submitChallengeReply).not.toHaveBeenCalled();
    expect(render().state.exportNotice).toBeTruthy();
  });
  it("does not resend an interrupted create request with no saved research ID", async () => {
    const doc = storedWorkspace(); vi.mocked(loadEncryptedWorkspace).mockResolvedValueOnce({ status: "loaded", document: { ...doc, apiChallengeId: null, research: { ...doc.research, challengeStatus: "streaming" } } }); mount(); await flush(); render().retryChallenge(); await flush();
    expect(h.api.createChallenge).not.toHaveBeenCalled(); expect(render().state.exportNotice).toMatch(/未返回研究标识|returned no research ID/);
  });
  it("retains unreadable stored data and blocks automatic overwrite", async () => {
    vi.mocked(loadEncryptedWorkspace).mockResolvedValue({ status: "unreadable" }); mount(); await flush(); const store = render(); h.effects.at(-1)!(); await vi.advanceTimersByTimeAsync(1000);
    expect(store.workspaceReady).toBe(false); expect(store.workspaceStatus).toBe("error"); expect(saveEncryptedWorkspace).not.toHaveBeenCalled();
  });
  it("saves changed intermediate work after hydration and debounce", async () => {
    mount(); await flush(); render().setReplyDraft("Unsaved answer text"); const store = render(); h.effects.at(-1)!(); await vi.advanceTimersByTimeAsync(600);
    expect(store.workspaceReady).toBe(true); expect(saveEncryptedWorkspace).toHaveBeenCalledTimes(1); const [document, scope] = vi.mocked(saveEncryptedWorkspace).mock.calls[0]!;
    expect(document.research.replyDraft).toBe("Unsaved answer text"); expect(scope).toMatch(/^workspace:/); expect(render().workspaceStatus).toBe("saved");
  });
});
