import { beforeEach, describe, expect, it, vi } from "vitest";
const h = vi.hoisted(() => ({ values: new Map<string, string>(), get: vi.fn(), set: vi.fn() }));
vi.mock("expo-secure-store", () => ({ getItemAsync: h.get, setItemAsync: h.set }));
import { loadDraft, loadLanguage, saveDraft, saveLanguage } from "../src/storage";
import { researchOwnerHash, researchOwnerScope } from "../src/research-owner";
const draft = (viewpoint = "Synthetic study") => ({ viewpoint, capital: "1000", currency: "USD" as const, language: "en" as const, updatedAt: "2026-10-03T00:00:00Z" });
const flush = async () => { for (let n = 0; n < 10; n++) await Promise.resolve(); };
beforeEach(() => { vi.clearAllMocks(); h.values.clear(); h.get.mockImplementation(async key => h.values.get(key) ?? null); h.set.mockImplementation(async (key, value) => { h.values.set(key, value); }); });
describe("owner-scoped local drafts", () => {
  it("isolates accounts and guest drafts", async () => {
    await saveDraft(draft("A study"), "account:A"); await saveDraft(draft("B study"), "account:B"); await saveDraft(draft("Guest study"), "anonymous");
    expect((await loadDraft("account:A"))?.viewpoint).toBe("A study"); expect((await loadDraft("account:B"))?.viewpoint).toBe("B study"); expect((await loadDraft())?.viewpoint).toBe("Guest study");
  });
  it("does not guess ownership of the previous global draft", async () => {
    h.values.set("f9.mobile.draft.v1", JSON.stringify(draft("Unowned old draft")));
    expect(await loadDraft("account:A")).toBeNull(); expect(await loadDraft()).toBeNull(); expect(h.values.has("f9.mobile.draft.v1")).toBe(true);
  });
  it("uses valid opaque SecureStore key names", async () => {
    await saveDraft(draft(), "account:synthetic@example.com/../other");
    const key = h.set.mock.calls[0]![0] as string; expect(key).toMatch(/^f9\.mobile\.draft\.v2\.[a-f0-9]{64}$/); expect(key).not.toContain("synthetic");
  });
  it("serializes writes and reads for one account", async () => {
    let release!: () => void; h.set.mockImplementationOnce((key, value) => new Promise<void>(resolve => { release = () => { h.values.set(key, value); resolve(); }; }));
    const first = saveDraft(draft("First"), "account:A"); const second = saveDraft(draft("Second"), "account:A"); const read = loadDraft("account:A");
    await flush(); expect(h.set).toHaveBeenCalledTimes(1); expect(h.get).not.toHaveBeenCalled(); release();
    await Promise.all([first, second]); expect((await read)?.viewpoint).toBe("Second");
  });
  it("a slow write for A does not block B", async () => {
    let release!: () => void; h.set.mockImplementationOnce(() => new Promise<void>(resolve => { release = resolve; }));
    const first = saveDraft(draft(), "account:A"); await flush(); await saveDraft(draft("B study"), "account:B");
    expect((await loadDraft("account:B"))?.viewpoint).toBe("B study"); release(); await first;
  });
  it("failed writes do not poison subsequent saves", async () => {
    h.set.mockRejectedValueOnce(new Error("synthetic disk error")); await expect(saveDraft(draft(), "account:A")).rejects.toThrow();
    await saveDraft(draft("Retry"), "account:A"); expect((await loadDraft("account:A"))?.viewpoint).toBe("Retry");
  });
  it("captures draft content before asynchronous execution", async () => {
    const input = draft("Captured"); const save = saveDraft(input, "account:A"); input.viewpoint = "Changed later"; await save;
    expect((await loadDraft("account:A"))?.viewpoint).toBe("Captured");
  });
  it("rejects malformed, oversized and extra-field persisted data", async () => {
    for (const input of [{ ...draft(), updatedAt: "bad" }, { ...draft(), viewpoint: "x".repeat(20_001) }, { ...draft(), unexpected: "test" }]) {
      await expect(saveDraft(input, "account:A")).rejects.toThrow();
      h.values.set(`f9.mobile.draft.v2.${researchOwnerHash("account:A")}`, JSON.stringify(input)); expect(await loadDraft("account:A")).toBeNull();
    }
    expect(h.set).not.toHaveBeenCalled();
  });
  it("keeps language a shared non-research preference", async () => { await saveLanguage("zh"); expect(await loadLanguage()).toBe("zh"); });
  it("separates hydration, anonymous and account namespaces without tying them to a token", () => {
    expect(researchOwnerScope(null, "loading")).toBeNull(); expect(researchOwnerScope(null, "signed_out")).toBe("anonymous");
    expect(researchOwnerScope({ account: { account_id: "A" } }, "signed_in")).toBe("account:A"); expect(() => researchOwnerHash("")).toThrow();
  });
});
