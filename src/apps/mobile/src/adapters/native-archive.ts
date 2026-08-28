import { AESEncryptionKey, AESSealedData, aesDecryptAsync, aesEncryptAsync } from "expo-crypto";
import { File, Paths } from "expo-file-system";
import * as SecureStore from "expo-secure-store";
import { isAvailableAsync, shareAsync } from "expo-sharing";
import { sha256Hex } from "@foresightnine/fm-engine";
import { archiveIncludesPrivateEvidence, canRestoreArchive, makeArchiveEnvelope, type ArchiveEnvelope, type PrivacySafeDocument } from "../archive";

const KEY_NAME = "f9.archive.key.v1";
const ARCHIVE_FILE = "foresightnine-research-v1.sealed.json";
const AAD = "f9-archive-envelope-v1";

function archiveFile(): File { return new File(Paths.document, ARCHIVE_FILE); }
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

async function encryptionKey(): Promise<AESEncryptionKey> {
  const stored = await SecureStore.getItemAsync(KEY_NAME);
  if (stored) return AESEncryptionKey.import(stored, "base64");
  const key = await AESEncryptionKey.generate();
  await SecureStore.setItemAsync(KEY_NAME, await key.encoded("base64"));
  return key;
}

function encodeDocument(document: PrivacySafeDocument): Uint8Array {
  return new TextEncoder().encode(JSON.stringify(document));
}

export async function saveEncryptedArchive(document: PrivacySafeDocument): Promise<ArchiveEnvelope> {
  const key = await encryptionKey();
  const sealed = await aesEncryptAsync(encodeDocument(document), key, { additionalData: new TextEncoder().encode(AAD), tagLength: 16 });
  const combined = await sealed.combined("base64");
  const envelope = makeArchiveEnvelope({
    aad: AAD,
    combined,
    createdAt: new Date().toISOString(),
    documentHash: sha256Hex(JSON.stringify(document)),
  });
  const file = archiveFile();
  if (file.exists) file.delete();
  file.create({ overwrite: true, intermediates: true });
  file.write(JSON.stringify(envelope));
  return envelope;
}

export async function loadEncryptedArchive(): Promise<PrivacySafeDocument | null> {
  const file = archiveFile();
  if (!file.exists) { reportRestoreRejection("missing"); return null; }
  let envelope: unknown;
  try { envelope = JSON.parse(await file.text()); } catch { reportRestoreRejection("envelope"); return null; }
  if (!canRestoreArchive(envelope)) { reportRestoreRejection("envelope"); return null; }
  if (envelope.aad !== AAD) { reportRestoreRejection("aad"); return null; }
  try {
    const key = await encryptionKey();
    const sealed = AESSealedData.fromCombined(base64ToBytes(envelope.combined));
    const plaintext = await aesDecryptAsync(sealed, key, { additionalData: new TextEncoder().encode(AAD), output: "bytes" });
    const document = JSON.parse(new TextDecoder().decode(plaintext)) as PrivacySafeDocument;
    if (sha256Hex(JSON.stringify(document)) !== envelope.documentHash) { reportRestoreRejection("hash"); return null; }
    return document;
  } catch {
    reportRestoreRejection("decrypt");
    return null;
  }
}

export async function archiveExists(): Promise<boolean> { return archiveFile().exists; }

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
