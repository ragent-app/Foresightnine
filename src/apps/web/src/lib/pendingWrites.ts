import { newIdempotencyKey } from "../api/http";

// Idempotent writes that survive a page reload.
//
// Scope convention: `run:<accountId>:<topicId>:<parentVersionId>`.
// Fingerprint: stable JSON (sorted keys) of the request body. The same scope
// and fingerprint reuse the stored Idempotency-Key; a changed body gets a new key.

const STORAGE_KEY = "foresightnine.web.pending-writes.v1";
const MAX_AGE_MS = 24 * 60 * 60 * 1000;

interface PendingWrite {
  key: string;
  fingerprint: string;
  createdAt: number;
}

type PendingMap = Record<string, PendingWrite>;

function readAll(): PendingMap {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return {};
    const now = Date.now();
    const result: PendingMap = {};
    for (const [scope, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof value !== "object" || value === null) continue;
      const entry = value as Record<string, unknown>;
      if (
        typeof entry.key === "string"
        && typeof entry.fingerprint === "string"
        && typeof entry.createdAt === "number"
        && now - entry.createdAt <= MAX_AGE_MS
      ) {
        result[scope] = { key: entry.key, fingerprint: entry.fingerprint, createdAt: entry.createdAt };
      }
    }
    return result;
  } catch {
    return {};
  }
}

function writeAll(map: PendingMap): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(map));
  } catch {
    // Storage unavailable: keys are then only valid for this page load.
  }
}

export function stableStringify(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map((item) => stableStringify(item)).join(",")}]`;
  if (typeof value === "object" && value !== null) {
    const record = value as Record<string, unknown>;
    const entries = Object.keys(record)
      .filter((key) => record[key] !== undefined)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableStringify(record[key])}`);
    return `{${entries.join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

export function getOrCreateWriteKey(scope: string, fingerprint: string): string {
  const map = readAll();
  const existing = map[scope];
  if (existing && existing.fingerprint === fingerprint) return existing.key;
  const key = newIdempotencyKey();
  map[scope] = { key, fingerprint, createdAt: Date.now() };
  writeAll(map);
  return key;
}

export function settleWrite(scope: string): void {
  const map = readAll();
  if (!(scope in map)) return;
  delete map[scope];
  writeAll(map);
}
