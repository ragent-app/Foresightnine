import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { sha256Hex } from "@foresightnine/fm-engine";
// Controlled native doubles: failure recovery/ordering, not hardware AES or power-loss certification.
const h = vi.hoisted(() => ({ files: new Map<string, string>(), encrypt: vi.fn(), decrypt: vi.fn(), remove: vi.fn(), create: vi.fn(), write: vi.fn(), text: vi.fn(), getKey: vi.fn(), setKey: vi.fn(), generate: vi.fn(), failure: "", oversized: false }));
vi.mock("expo-crypto", () => ({ AESEncryptionKey: { import: vi.fn(async () => ({})), generate: h.generate }, AESSealedData: { fromCombined: (bytes: Uint8Array) => bytes }, aesEncryptAsync: h.encrypt, aesDecryptAsync: h.decrypt }));
vi.mock("expo-secure-store", () => ({ getItemAsync: h.getKey, setItemAsync: h.setKey }));
vi.mock("expo-sharing", () => ({ isAvailableAsync: vi.fn(), shareAsync: vi.fn() }));
vi.mock("expo-file-system", () => ({ Paths: { document: "synthetic-document", cache: "synthetic-cache" }, File: class {
  name: string;
  constructor(...parts: string[]) { this.name = parts.at(-1)!; }
  get exists() { return h.files.has(this.name); }
  get size() { return h.oversized ? 3 * 1024 * 1024 : (h.files.get(this.name)?.length ?? 0); }
  delete() { h.remove(this.name); h.files.delete(this.name); }
  create() { h.create(this.name); if (h.failure === "create") throw new Error("synthetic create failure"); h.files.set(this.name, ""); }
  write(value: string) { h.write(value, this.name); if (h.failure === "partial") { h.files.set(this.name, value.slice(0, 25)); throw new Error("synthetic write failure"); } h.files.set(this.name, h.failure === "silent-truncate" ? value.slice(0, 25) : value); }
  async text() { h.text(this.name); return h.files.get(this.name)!; }
} }));
import { archiveExists, loadEncryptedArchive, saveEncryptedArchive, loadEncryptedWorkspace, saveEncryptedWorkspace } from "../src/adapters/native-archive";
import { buildPrivacySafeDocument, makeArchiveEnvelope } from "../src/archive";
import { buildWorkspaceDocument, workspaceStorageScope } from "../src/workspace-state";
import { mobileInitialState } from "../src/lifecycle";
import { ASSETS, FIXTURE_STATE } from "../src/fixtures";
import { portfolioMetadata } from "../src/portfolio";
const encode = (text: string) => btoa(String.fromCharCode(...new TextEncoder().encode(text)));
const LEGACY = "foresightnine-research-v1.sealed.json";
const valid = (title = "Synthetic draft") => buildPrivacySafeDocument({ title, viewpoint: "A synthetic research premise", capital: 100, currency: "USD", selectedBranch: "main", evidence: [] });
function legacy() {
  const text = JSON.stringify(valid("Previous study"));
  h.files.set(LEGACY, JSON.stringify(makeArchiveEnvelope({ aad: "f9-archive-envelope-v1", combined: encode(text), createdAt: "2026-10-03T00:00:00Z", documentHash: sha256Hex(text) })));
}
beforeEach(() => {
  vi.clearAllMocks(); vi.stubGlobal("__DEV__", false); h.files.clear(); h.failure = ""; h.oversized = false;
  h.getKey.mockResolvedValue("synthetic-key-reference"); h.setKey.mockResolvedValue(undefined);
  h.generate.mockResolvedValue({ encoded: async () => "synthetic-generated-key" });
  h.encrypt.mockImplementation(async (text: Uint8Array) => { if (!(text instanceof Uint8Array)) throw new Error("Test requires explicit UTF-8 bytes: native strings are base64"); return { combined: async () => encode(new TextDecoder().decode(text)) }; });
  h.decrypt.mockImplementation(async (bytes: Uint8Array) => bytes);
});
afterEach(() => vi.unstubAllGlobals());
describe("native archive write boundary", () => {
  it("rejects malformed study input without encrypting or removing the existing archive", async () => {
    legacy(); await expect(saveEncryptedArchive({ ...valid(), capital: -1 })).rejects.toThrow("archive_snapshot_invalid");
    expect(h.encrypt).not.toHaveBeenCalled(); expect(h.remove).not.toHaveBeenCalled(); expect(h.write).not.toHaveBeenCalled();
  });
  it("rejects an invalid sealed result before deleting an existing archive", async () => {
    legacy(); h.encrypt.mockResolvedValueOnce({ combined: async () => "%not-base64%" });
    await expect(saveEncryptedArchive(valid())).rejects.toThrow("archive_envelope_invalid");
    expect(h.remove).not.toHaveBeenCalled(); expect(h.write).not.toHaveBeenCalled();
  });
  it("continues to write a structurally valid bounded sealed result", async () => {
    await expect(saveEncryptedArchive(valid())).resolves.toMatchObject({ schema: "f9-archive-envelope-v1" });
    expect(h.write).toHaveBeenCalledOnce(); expect(String(h.write.mock.calls[0]?.[0])).not.toContain("A synthetic research premise");
    expect(await loadEncryptedArchive()).toEqual(valid());
  });
  it("rejects oversized files before reading or parsing their contents", async () => {
    legacy(); h.oversized = true;
    await expect(loadEncryptedArchive()).resolves.toBeNull(); expect(h.text).not.toHaveBeenCalled();
  });
});
describe("archive journal recovery", () => {
  it.each(["create", "partial", "silent-truncate"])("preserves the last study after %s failure", async failure => {
    await saveEncryptedArchive(valid("Study one"));
    h.failure = failure; await expect(saveEncryptedArchive(valid("Study two"))).rejects.toThrow(); h.failure = "";
    expect((await loadEncryptedArchive())?.title).toBe("Study one");
  });
  it("recovers the previous valid slot after newest ciphertext corruption", async () => {
    await saveEncryptedArchive(valid("Study one")); await saveEncryptedArchive(valid("Study two"));
    h.files.set(h.write.mock.calls.at(-1)![1] as string, "{truncated");
    expect((await loadEncryptedArchive())?.title).toBe("Study one");
  });
  it("rotates bounded slots without removing the legacy archive", async () => {
    legacy(); const original = h.files.get(LEGACY);
    for (let n = 1; n <= 5; n++) await saveEncryptedArchive(valid(`Study ${n}`));
    expect(h.files.size).toBe(3); expect(h.files.get(LEGACY)).toBe(original);
    expect((await loadEncryptedArchive())?.title).toBe("Study 5"); expect(await archiveExists()).toBe(true);
  });
  it("orders revisions independently of wall-clock rollback", async () => {
    const now = vi.spyOn(Date.prototype, "toISOString");
    now.mockReturnValueOnce("2026-10-03T00:00:00Z"); await saveEncryptedArchive(valid("Study one"));
    now.mockReturnValueOnce("2026-10-02T00:00:00Z"); await saveEncryptedArchive(valid("Study two")); now.mockRestore();
    expect((await loadEncryptedArchive())?.title).toBe("Study two");
  });
  it("serializes overlapping saves and creates just one first-use key", async () => {
    let key: string | null = null;
    h.getKey.mockImplementation(async () => key); h.setKey.mockImplementation(async (_name: string, value: string) => { key = value; });
    await Promise.all([saveEncryptedArchive(valid("Study one")), saveEncryptedArchive(valid("Study two"))]);
    expect(h.generate).toHaveBeenCalledTimes(1); expect((await loadEncryptedArchive())?.title).toBe("Study two");
    expect(h.files.size).toBe(2);
  });
  it("does not generate a replacement key when an existing archive has lost its key", async () => {
    legacy(); const original = h.files.get(LEGACY); h.getKey.mockResolvedValue(null);
    expect(await loadEncryptedArchive()).toBeNull(); await expect(saveEncryptedArchive(valid())).rejects.toThrow();
    expect(h.generate).not.toHaveBeenCalled(); expect(h.write).not.toHaveBeenCalled(); expect(h.files.get(LEGACY)).toBe(original);
  });
  it("keeps unreadable archives intact rather than silently starting over", async () => {
    h.files.set(LEGACY, "{truncated"); await expect(saveEncryptedArchive(valid())).rejects.toThrow();
    expect(h.files.get(LEGACY)).toBe("{truncated"); expect(h.write).not.toHaveBeenCalled();
  });
  it("allows a first-ever failed save to be retried", async () => {
    h.failure = "partial"; await expect(saveEncryptedArchive(valid())).rejects.toThrow(); h.failure = "";
    await saveEncryptedArchive(valid("Recovered first study")); expect((await loadEncryptedArchive())?.title).toBe("Recovered first study");
  });
  it("preserves an interrupted first slot and uses the still-unused slot", async () => {
    const broken = "foresightnine-research-journal-0.sealed.json"; h.files.set(broken, "{interrupted");
    await saveEncryptedArchive(valid("Recovered first study"));
    expect(h.files.get(broken)).toBe("{interrupted"); expect((await loadEncryptedArchive())?.title).toBe("Recovered first study");
  });
  it("a rejected save does not poison the queue", async () => {
    await saveEncryptedArchive(valid("Study one")); h.failure = "partial";
    await expect(saveEncryptedArchive(valid("Study two"))).rejects.toThrow(); h.failure = "";
    await saveEncryptedArchive(valid("Study three")); expect((await loadEncryptedArchive())?.title).toBe("Study three");
  });
  it("restores after module restart without relying on in-memory revision state", async () => {
    await saveEncryptedArchive(valid("Study one")); await saveEncryptedArchive(valid("Study two"));
    vi.resetModules();
    const restarted = await import("../src/adapters/native-archive");
    expect((await restarted.loadEncryptedArchive())?.title).toBe("Study two");
  });
  it("falls back when the native bridge rejects newest ciphertext authentication", async () => {
    await saveEncryptedArchive(valid("Study one")); await saveEncryptedArchive(valid("Study two"));
    h.decrypt.mockImplementation(async (bytes: Uint8Array) => { if (new TextDecoder().decode(bytes).includes('"revision":2')) throw new Error("synthetic GCM authentication failure"); return bytes; });
    expect((await loadEncryptedArchive())?.title).toBe("Study one");
  });
  it("captures a caller's input before queued work begins", async () => {
    const document = { ...valid("Study one") }; const saving = saveEncryptedArchive(document); document.title = "Mutated later";
    await saving; expect((await loadEncryptedArchive())?.title).toBe("Study one");
  });
  it("rejects a decrypted document with invalid structure even with a matching hash", async () => {
    const text = JSON.stringify({ ...valid(), capital: -1 });
    h.files.set(LEGACY, JSON.stringify(makeArchiveEnvelope({ aad: "f9-archive-envelope-v1", combined: encode(text), createdAt: "2026-10-03T00:00:00Z", documentHash: sha256Hex(text) })));
    expect(await loadEncryptedArchive()).toBeNull();
  });
});

describe("account-scoped archives", () => {
  it("does not adopt an unowned legacy archive into any account or guest workspace", async () => {
    legacy(); expect(await loadEncryptedArchive("account:A")).toBeNull(); expect(await loadEncryptedArchive("anonymous")).toBeNull(); expect(h.files.has(LEGACY)).toBe(true);
  });
  it("isolates two accounts and the anonymous workspace", async () => {
    await saveEncryptedArchive(valid("A study"), "account:A"); await saveEncryptedArchive(valid("B study"), "account:B");
    expect((await loadEncryptedArchive("account:A"))?.title).toBe("A study");
    expect((await loadEncryptedArchive("account:B"))?.title).toBe("B study");
    expect(await loadEncryptedArchive("anonymous")).toBeNull();
    expect(new Set(h.getKey.mock.calls.map(call => call[0])).size).toBe(2);
  });
  it("binds the encrypted journal to its owner even if a ciphertext file is moved", async () => {
    await saveEncryptedArchive(valid("A study"), "account:A"); const aName = h.write.mock.calls.at(-1)![1] as string;
    await saveEncryptedArchive(valid("B study"), "account:B"); const bName = h.write.mock.calls.at(-1)![1] as string;
    h.files.set(bName, h.files.get(aName)!);
    expect(await loadEncryptedArchive("account:B")).toBeNull(); expect((await loadEncryptedArchive("account:A"))?.title).toBe("A study");
  });
  it("creates separate first-use secure-store keys and reuses only the same owner's key", async () => {
    const keys = new Map<string, string>(); h.getKey.mockImplementation(async name => keys.get(name) ?? null); h.setKey.mockImplementation(async (name, value) => { keys.set(name, value); });
    await saveEncryptedArchive(valid("A study"), "account:A"); await saveEncryptedArchive(valid("B study"), "account:B"); await saveEncryptedArchive(valid("A updated"), "account:A");
    expect(keys.size).toBe(2); expect(h.generate).toHaveBeenCalledTimes(2); expect((await loadEncryptedArchive("account:A"))?.title).toBe("A updated");
  });
  it("uses opaque filenames and secure-store key names", async () => {
    const scope = "account:synthetic.person@example.com/../../other"; await saveEncryptedArchive(valid(), scope);
    expect([...h.files.keys()].some(name => name.includes("synthetic.person") || name.includes("..") || name.includes("@"))).toBe(false);
    expect(h.getKey.mock.calls.every(call => !String(call[0]).includes("synthetic.person"))).toBe(true);
  });
  it("overlapping saves remain in the account captured at invocation", async () => {
    await Promise.all([saveEncryptedArchive(valid("A study"), "account:A"), saveEncryptedArchive(valid("B study"), "account:B")]);
    expect((await loadEncryptedArchive("account:A"))?.title).toBe("A study"); expect((await loadEncryptedArchive("account:B"))?.title).toBe("B study");
  });
});

describe("encrypted automatic workspace boundary", () => {
  const scope = workspaceStorageScope("account:SYNTHETIC", "https://app.foresightnine.com");
  const document = () => buildWorkspaceDocument({ state: { ...mobileInitialState, replyDraft: "Synthetic private draft" }, branches: [], methodData: { methodVersion: portfolioMetadata.method, dataAsOf: portfolioMetadata.dataAsOf, navLabel: portfolioMetadata.navLabel, fixtureState: FIXTURE_STATE, assetIds: ASSETS.map(a => a.id) }, apiChallengeId: null, evidenceWorkspace: { sharedText: "Synthetic private source", searchQuery: "", candidates: [] }, scenarioDrafts: {}, assets: ASSETS });
  it("distinguishes a missing workspace from unreadable data", async () => {
    expect(await loadEncryptedWorkspace(scope)).toEqual({ status: "empty" }); await saveEncryptedWorkspace(document(), scope); const name = h.write.mock.calls.at(-1)![1] as string; h.files.set(name, "{broken"); expect(await loadEncryptedWorkspace(scope)).toEqual({ status: "unreadable" });
  });
  it("round-trips through the journal without plaintext file writes or sharing manual archives", async () => {
    const doc = document(); await saveEncryptedWorkspace(doc, scope); const result = await loadEncryptedWorkspace(scope);
    expect(result).toEqual({ status: "loaded", document: doc }); expect(String(h.write.mock.calls[0]?.[0])).not.toContain("Synthetic private draft"); expect(await loadEncryptedArchive("account:SYNTHETIC")).toBeNull();
  });
  it("reports a lost key rather than replacing the existing workspace", async () => {
    await saveEncryptedWorkspace(document(), scope); h.getKey.mockResolvedValue(null); expect(await loadEncryptedWorkspace(scope)).toEqual({ status: "unreadable" }); expect(h.generate).not.toHaveBeenCalled();
  });
});
