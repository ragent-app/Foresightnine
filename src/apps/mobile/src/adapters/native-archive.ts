import { AESEncryptionKey, AESSealedData, aesDecryptAsync, aesEncryptAsync } from "expo-crypto";
import { File, Paths } from "expo-file-system";
import * as SecureStore from "expo-secure-store";
import { isAvailableAsync, shareAsync } from "expo-sharing";
import { canRestoreWorkspaceDocument, serializeWorkspaceDocument, type WorkspaceDocument } from "../workspace-state";
import { researchOwnerHash } from "../research-owner";
import { sha256Hex } from "@foresightnine/fm-engine";
import { archiveIncludesPrivateEvidence, canRestoreArchive, canRestorePrivacySafeDocument, makeArchiveEnvelope, serializeArchiveDocument, MAX_ARCHIVE_SEALED_CHARS, type ArchiveEnvelope, type PrivacySafeDocument } from "../archive";

const KEY_NAME = "f9.archive.key.v1";
const ARCHIVE_FILE = "foresightnine-research-v1.sealed.json";
const AAD = "f9-archive-envelope-v1";

function reportRestoreRejection(stage: "missing" | "envelope" | "aad" | "decrypt" | "hash"): void {
  // Development-only diagnostics intentionally exclude the envelope, ciphertext,
  // key, document, and caught error so device-local research is never logged.
  if (__DEV__) console.warn(`[archive] restore rejected: ${stage}`);
}
function base64ToBytes(value: string): Uint8Array {
  // The native AES bridge is most reliable when `fromCombined` receives bytes.
  // `combined` remains base64 in the JSON envelope, never plaintext.
  if (typeof globalThis.atob !== "function") throw new Error("base64_decoder_unavailable");
  const binary = globalThis.atob(value);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

// Two bounded slots: never truncate the newest authenticated study to save its successor.
// This survives interrupted/truncated application writes; Expo offers no fsync durability
// guarantee here, so it is not a claim of hardware/power-loss atomicity.
const SLOT_FILES = ["foresightnine-research-journal-0.sealed.json", "foresightnine-research-journal-1.sealed.json"] as const;
type ArchiveLocation = { legacy: string | null; slots: readonly [string, string]; keyName: string; ownerHash: string | null };
function archiveLocation(scope?: string): ArchiveLocation {
  // Undefined is retained only for an explicit legacy reader/tool; the app supplies a scope.
  if (scope === undefined) return { legacy: ARCHIVE_FILE, slots: SLOT_FILES, keyName: KEY_NAME, ownerHash: null };
  const hash = researchOwnerHash(scope);
  return { legacy: null, slots: [`foresightnine-research-${hash}-0.sealed.json`, `foresightnine-research-${hash}-1.sealed.json`], keyName: `f9.archive.key.v2.${hash}`, ownerHash: hash };
}
function candidateNames(location: ArchiveLocation): readonly string[] { return location.legacy ? [location.legacy, ...location.slots] : location.slots; }
const JOURNAL_SCHEMA = "f9-archive-journal-v1";
let archiveQueue: Promise<unknown> = Promise.resolve();
function ordered<T>(operation: () => Promise<T>): Promise<T> {
  const result = archiveQueue.then(operation);
  archiveQueue = result.catch(() => undefined);
  return result;
}

async function encryptionKey(create: boolean, location: ArchiveLocation): Promise<AESEncryptionKey | null> {
  const stored = await SecureStore.getItemAsync(location.keyName);
  if (stored) return AESEncryptionKey.import(stored, "base64");
  if (!create) return null; // Reading an existing archive must never replace a lost key.
  const key = await AESEncryptionKey.generate();
  await SecureStore.setItemAsync(location.keyName, await key.encoded("base64"));
  return key;
}

type Codec<T> = { serialize(value: T): Uint8Array; valid(value: unknown): value is T };
const archiveCodec: Codec<PrivacySafeDocument> = { serialize: serializeArchiveDocument, valid: canRestorePrivacySafeDocument };
const workspaceCodec: Codec<WorkspaceDocument> = { serialize: serializeWorkspaceDocument, valid: canRestoreWorkspaceDocument };
type Candidate<T> = { fileName: string; revision: number; document: T; envelope: ArchiveEnvelope };
async function readCandidate<T>(fileName: string, key: AESEncryptionKey, location: ArchiveLocation, codec: Codec<T>): Promise<Candidate<T> | null> {
  try {
    const file = new File(Paths.document, fileName);
    if (!file.exists) return null;
    if (file.size > MAX_ARCHIVE_SEALED_CHARS + 4096) { reportRestoreRejection("envelope"); return null; }
    const envelope: unknown = JSON.parse(await file.text());
    if (!canRestoreArchive(envelope)) { reportRestoreRejection("envelope"); return null; }
    if (envelope.aad !== AAD) { reportRestoreRejection("aad"); return null; }
    const sealed = AESSealedData.fromCombined(base64ToBytes(envelope.combined));
    const plaintext = await aesDecryptAsync(sealed, key, { additionalData: new TextEncoder().encode(AAD), output: "bytes" });
    const decoded: unknown = JSON.parse(new TextDecoder().decode(plaintext));
    if (sha256Hex(JSON.stringify(decoded)) !== envelope.documentHash) { reportRestoreRejection("hash"); return null; }
    if (fileName === location.legacy) {
      return codec.valid(decoded) ? { fileName, revision: 0, document: decoded, envelope } : null;
    }
    // Revision is inside the authenticated ciphertext, not an editable filename,
    // wall-clock timestamp or unauthenticated manifest/pointer.
    if (!decoded || typeof decoded !== "object") return null;
    const journal = decoded as Record<string, unknown>;
    if (Object.keys(journal).length !== (location.ownerHash ? 4 : 3) || (location.ownerHash !== null && journal.ownerHash !== location.ownerHash) || journal.schema !== JOURNAL_SCHEMA || !Number.isSafeInteger(journal.revision) || typeof journal.revision !== "number" || journal.revision < 1 || !codec.valid(journal.document)) return null;
    return { fileName, revision: journal.revision, document: journal.document, envelope };
  } catch {
    reportRestoreRejection("decrypt");
    return null;
  }
}

async function newest<T>(key: AESEncryptionKey, location: ArchiveLocation, codec: Codec<T>): Promise<Candidate<T> | null> {
  const candidates = await Promise.all(candidateNames(location).map(name => readCandidate(name, key, location, codec)));
  return candidates.reduce<Candidate<T> | null>((best, item) => item && (!best || item.revision > best.revision) ? item : best, null);
}

function saveEncryptedDocument<T>(document: T, scope: string | undefined, codec: Codec<T>): Promise<ArchiveEnvelope> {
  // Snapshot and validate now: callers can mutate their input before a queued write starts.
  let serialized: string;
  try { serialized = new TextDecoder().decode(codec.serialize(document)); }
  catch (error) { return Promise.reject(error); }
  const location = archiveLocation(scope);
  return ordered(async () => {
    const existing = candidateNames(location).filter(name => new File(Paths.document, name).exists);
    const present = existing.length > 0;
    const unusedSlot = location.slots.find(name => !existing.includes(name));
    const key = await encryptionKey(!present, location);
    if (!key) throw new Error("archive_key_missing");
    const previous = await newest(key, location, codec);
    if (present && !previous && ((location.legacy !== null && existing.includes(location.legacy)) || !unusedSlot)) throw new Error("archive_existing_unreadable");
    const revision = (previous?.revision ?? 0) + 1;
    if (!Number.isSafeInteger(revision)) throw new Error("archive_revision_exhausted");
    const plaintext = JSON.stringify({ schema: JOURNAL_SCHEMA, revision, document: JSON.parse(serialized), ...(location.ownerHash ? { ownerHash: location.ownerHash } : {}) });
    const sealed = await aesEncryptAsync(new TextEncoder().encode(plaintext), key, { additionalData: new TextEncoder().encode(AAD), tagLength: 16 });
    const envelope = makeArchiveEnvelope({ aad: AAD, combined: await sealed.combined("base64"), createdAt: new Date().toISOString(), documentHash: sha256Hex(plaintext) });
    if (!canRestoreArchive(envelope)) throw new Error("archive_envelope_invalid");
    const target = previous ? (previous.fileName === location.slots[0] ? location.slots[1] : location.slots[0]) : (unusedSlot ?? location.slots[0]);
    const file = new File(Paths.document, target);
    let created = false;
    try {
      file.create({ overwrite: true, intermediates: true });
      created = true;
      file.write(JSON.stringify(envelope));
      // A successful native call alone does not prove a restorable snapshot was written.
      const verified = await readCandidate(target, key, location, codec);
      if (!verified || verified.revision !== revision || verified.envelope.documentHash !== envelope.documentHash) throw new Error("archive_write_verification_failed");
    } catch (error) {
      // Only remove this attempted replacement, never the last valid candidate.
      // If termination prevents cleanup, restore still searches both slots.
      if (created) { try { if (file.exists) file.delete(); } catch { /* preserve last valid slot */ } }
      throw error;
    }
    return envelope;
  });
}

export type WorkspaceLoadResult = { status: "loaded"; document: WorkspaceDocument } | { status: "empty" | "unreadable" };
function readEncryptedDocument<T>(scope: string | undefined, codec: Codec<T>): Promise<{ status: "loaded"; document: T } | { status: "empty" | "unreadable" }> {
  const location = archiveLocation(scope);
  return ordered(async () => {
    try {
      if (!candidateNames(location).some(name => new File(Paths.document, name).exists)) return { status: "empty" };
      const key = await encryptionKey(false, location);
      if (!key) return { status: "unreadable" };
      const result = await newest(key, location, codec);
      return result ? { status: "loaded", document: result.document } : { status: "unreadable" };
    } catch { reportRestoreRejection("decrypt"); return { status: "unreadable" }; }
  });
}
export function saveEncryptedArchive(document: PrivacySafeDocument, scope?: string): Promise<ArchiveEnvelope> { return saveEncryptedDocument(document, scope, archiveCodec); }
export async function loadEncryptedArchive(scope?: string): Promise<PrivacySafeDocument | null> {
  const result = await readEncryptedDocument(scope, archiveCodec); return result.status === "loaded" ? result.document : null;
}
export function saveEncryptedWorkspace(document: WorkspaceDocument, scope: string): Promise<ArchiveEnvelope> { return saveEncryptedDocument(document, scope, workspaceCodec); }
export function loadEncryptedWorkspace(scope: string): Promise<WorkspaceLoadResult> { return readEncryptedDocument(scope, workspaceCodec); }
export async function archiveExists(scope?: string): Promise<boolean> { return (await loadEncryptedArchive(scope)) !== null; }

/** A deliberately public-safe export: private evidence is rejected before this boundary. */
export async function sharePrivacySafeDocument(document: PrivacySafeDocument): Promise<boolean> {
  if (document.privatePremiseIncluded || archiveIncludesPrivateEvidence(document)) throw new Error("private_evidence_export_blocked");
  if (!(await isAvailableAsync())) return false;
  const file = new File(Paths.cache, "foresightnine-public-research.json");
  if (file.exists) file.delete();
  file.create({ overwrite: true, intermediates: true });
  file.write(JSON.stringify(document, null, 2));
  await shareAsync(file.uri, { mimeType: "application/json", dialogTitle: "Export privacy-safe research" });
  return true;
}
