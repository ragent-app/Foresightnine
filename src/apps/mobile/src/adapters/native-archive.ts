import { AESEncryptionKey, AESSealedData, aesDecryptAsync, aesEncryptAsync } from "expo-crypto";
import { File, Paths } from "expo-file-system";
import * as SecureStore from "expo-secure-store";
import { isAvailableAsync, shareAsync } from "expo-sharing";
import { sha256Hex } from "@foresightnine/fm-engine";
import { canRestoreArchive, makeArchiveEnvelope, type ArchiveEnvelope, type PrivacySafeDocument } from "../archive";

const KEY_NAME = "f9.archive.key.v1";
const ARCHIVE_FILE = "foresightnine-research-v1.sealed.json";
const AAD = "f9-archive-envelope-v1";

function archiveFile(): File { return new File(Paths.document, ARCHIVE_FILE); }

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
  if (!file.exists) return null;
  let envelope: unknown;
  try { envelope = JSON.parse(await file.text()); } catch { return null; }
  if (!canRestoreArchive(envelope) || envelope.aad !== AAD) return null;
  try {
    const key = await encryptionKey();
    const sealed = AESSealedData.fromCombined(envelope.combined);
    const plaintext = await aesDecryptAsync(sealed, key, { additionalData: new TextEncoder().encode(AAD), output: "bytes" });
    const document = JSON.parse(new TextDecoder().decode(plaintext)) as PrivacySafeDocument;
    return sha256Hex(JSON.stringify(document)) === envelope.documentHash ? document : null;
  } catch {
    return null;
  }
}

export async function archiveExists(): Promise<boolean> { return archiveFile().exists; }

/** A deliberately public-safe export: private evidence is rejected before this boundary. */
export async function sharePrivacySafeDocument(document: PrivacySafeDocument): Promise<boolean> {
  if (document.privatePremiseIncluded || document.evidence.some((item) => item.source_type === "user_private")) throw new Error("private_evidence_export_blocked");
  if (!(await isAvailableAsync())) return false;
  const file = new File(Paths.cache, "foresightnine-public-research.json");
  if (file.exists) file.delete();
  file.create({ overwrite: true, intermediates: true });
  file.write(JSON.stringify(document, null, 2));
  await shareAsync(file.uri, { mimeType: "application/json", dialogTitle: "Export privacy-safe research" });
  return true;
}
