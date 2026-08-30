import { canonicalJson, sha256Hex, type ComputeFmInput, type FactorAst, type FactorExpression } from "@foresightnine/fm-engine";

export type SnapshotAnchor = "year_start" | "latest_month_start" | "latest_completed_session";
export const SNAPSHOT_ANCHORS: readonly SnapshotAnchor[] = ["year_start", "latest_month_start", "latest_completed_session"];
export type ProviderMode = "public" | "byok" | "mock";
export type SnapshotStatus = "ok" | "stale" | "missing";
export type MarketDataStatus = ProviderMode | "limited_data";

export interface MarketSnapshot {
  readonly assetId: string;
  readonly anchor: SnapshotAnchor;
  /** Canonical provider trading/session date, or null when the provider has no row. */
  readonly asOf: string | null;
  readonly retrievedAt: string;
  readonly timezone: string;
  readonly price: number | null;
  readonly close: number | null;
  readonly currency: string;
  readonly change?: number | null;
  /** Scalar feature values keyed by the names used by the explicit FM factor AST. */
  readonly features?: Readonly<Record<string, number | null>>;
  readonly status: SnapshotStatus;
  readonly warnings?: readonly string[];
}

export interface MarketSnapshotBundle {
  readonly schemaVersion: "market-snapshot-bundle/v1";
  readonly requestedAt: string;
  readonly cutoff: string;
  readonly timezone: string;
  readonly assets: readonly string[];
  readonly snapshots: readonly MarketSnapshot[];
  readonly provider: { readonly id: string; readonly mode: ProviderMode };
  readonly warnings: readonly string[];
  readonly integrityHash: string;
}

export interface MarketDataRequest {
  readonly assetIds: readonly string[];
  readonly cutoff: string;
  readonly timezone: string;
  readonly requestedAt?: string;
}

export interface MarketDataSource {
  fetchSnapshots(request: MarketDataRequest, signal?: AbortSignal): Promise<MarketSnapshotBundle>;
}

export class MarketDataValidationError extends Error {
  public readonly code:
    | "invalid_bundle"
    | "invalid_request"
    | "invalid_provider_url"
    | "unknown_asset"
    | "duplicate_snapshot"
    | "malformed_date"
    | "future_data"
    | "invalid_price"
    | "oversized_payload"
    | "incomplete_bundle"
    | "byok_reused";

  public constructor(code: MarketDataValidationError["code"], message: string) {
    super(message);
    this.name = "MarketDataValidationError";
    this.code = code;
  }
}

export const MARKET_DATA_LIMITS = Object.freeze({
  maxAssets: 64,
  maxSnapshots: 64 * SNAPSHOT_ANCHORS.length,
  maxRequestBytes: 64 * 1024,
  maxResponseBytes: 1024 * 1024,
  maxWarnings: 128,
  maxFeaturesPerSnapshot: 64,
});

type CanonicalValue = null | boolean | number | string | readonly CanonicalValue[] | { readonly [key: string]: CanonicalValue };

function isRecord(value: unknown): value is Readonly<Record<string, unknown>> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNonEmptyString(value: unknown, max = 256): value is string {
  return typeof value === "string" && value.trim().length > 0 && value.length <= max;
}

function parseDateOnly(value: unknown): number | null {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/u.test(value)) return null;
  const [yearText, monthText, dayText] = value.split("-");
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const date = new Date(Date.UTC(year, month - 1, day));
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day ? date.getTime() : null;
}

function parseDateTime(value: unknown): number | null {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/u.test(value)) return null;
  const datePart = value.slice(0, 10);
  if (parseDateOnly(datePart) === null) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function parseBoundary(value: string): { readonly date: string; readonly time: number } | null {
  const dateTime = parseDateTime(value);
  if (dateTime !== null) return { date: value.slice(0, 10), time: dateTime };
  const dateOnly = parseDateOnly(value);
  return dateOnly === null ? null : { date: value, time: dateOnly + 86_399_999 };
}

function assertTimezone(value: unknown): asserts value is string {
  if (!isNonEmptyString(value, 128) || !/^[A-Za-z0-9_+./:-]+$/u.test(value)) throw new MarketDataValidationError("invalid_bundle", "timezone must be a bounded identifier");
}

function assertRequest(request: MarketDataRequest): { readonly cutoff: { readonly date: string; readonly time: number }; readonly assetIds: readonly string[] } {
  if (!Array.isArray(request.assetIds) || request.assetIds.length === 0 || request.assetIds.length > MARKET_DATA_LIMITS.maxAssets) throw new MarketDataValidationError("invalid_request", "assetIds must be a bounded non-empty array");
  const ids = request.assetIds.map((assetId) => {
    if (!isNonEmptyString(assetId, 128)) throw new MarketDataValidationError("invalid_request", "assetIds must contain non-empty identifiers");
    return assetId;
  });
  if (new Set(ids).size !== ids.length) throw new MarketDataValidationError("invalid_request", "assetIds must be unique");
  const cutoff = parseBoundary(request.cutoff);
  if (cutoff === null) throw new MarketDataValidationError("malformed_date", "cutoff must be an ISO date or offset datetime");
  assertTimezone(request.timezone);
  if (request.requestedAt !== undefined && parseDateTime(request.requestedAt) === null) throw new MarketDataValidationError("malformed_date", "requestedAt must be an offset datetime");
  return { cutoff, assetIds: ids };
}

function assertPrice(value: unknown, field: string): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) throw new MarketDataValidationError("invalid_price", `${field} must be a finite non-negative number or null`);
  return value;
}

function assertChange(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== "number" || !Number.isFinite(value)) throw new MarketDataValidationError("invalid_price", "change must be a finite number or null");
  return value;
}

function canonicalSnapshot(snapshot: MarketSnapshot): CanonicalValue {
  const features: { [key: string]: CanonicalValue } = {};
  for (const key of Object.keys(snapshot.features ?? {}).sort()) features[key] = snapshot.features?.[key] ?? null;
  return {
    anchor: snapshot.anchor,
    asOf: snapshot.asOf,
    assetId: snapshot.assetId,
    change: snapshot.change ?? null,
    close: snapshot.close,
    currency: snapshot.currency,
    features,
    price: snapshot.price,
    retrievedAt: snapshot.retrievedAt,
    status: snapshot.status,
    timezone: snapshot.timezone,
    warnings: [...(snapshot.warnings ?? [])],
  };
}

function snapshotSort(left: MarketSnapshot, right: MarketSnapshot): number {
  return left.assetId.localeCompare(right.assetId) || SNAPSHOT_ANCHORS.indexOf(left.anchor) - SNAPSHOT_ANCHORS.indexOf(right.anchor);
}

function canonicalBundleData(bundle: Omit<MarketSnapshotBundle, "integrityHash"> | MarketSnapshotBundle): CanonicalValue {
  return {
    assets: [...bundle.assets],
    cutoff: bundle.cutoff,
    integrityHash: null,
    provider: { id: bundle.provider.id, mode: bundle.provider.mode },
    requestedAt: bundle.requestedAt,
    schemaVersion: bundle.schemaVersion,
    snapshots: [...bundle.snapshots].sort(snapshotSort).map(canonicalSnapshot),
    timezone: bundle.timezone,
    warnings: [...bundle.warnings],
  };
}

export function serializeMarketSnapshotBundle(bundle: MarketSnapshotBundle): string {
  return canonicalJson(canonicalBundleData(bundle));
}

export function computeMarketSnapshotIntegrityHash(bundle: Omit<MarketSnapshotBundle, "integrityHash"> | MarketSnapshotBundle): string {
  return sha256Hex(canonicalJson(canonicalBundleData(bundle)));
}

/** Explicitly redacted serialization. BYOK credentials are never part of a bundle. */
export function redactedMarketSnapshotBundleJson(bundle: MarketSnapshotBundle): string {
  return serializeMarketSnapshotBundle(bundle);
}

function normalizeWarnings(value: unknown, field: string): readonly string[] {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > MARKET_DATA_LIMITS.maxWarnings || value.some((item) => !isNonEmptyString(item, 512))) throw new MarketDataValidationError("invalid_bundle", `${field} must contain bounded warning strings`);
  return value.map((item) => item as string);
}

function normalizeFeatures(value: unknown): Readonly<Record<string, number | null>> | undefined {
  if (value === undefined) return undefined;
  if (!isRecord(value) || Object.keys(value).length > MARKET_DATA_LIMITS.maxFeaturesPerSnapshot) throw new MarketDataValidationError("invalid_bundle", "features must be a bounded object");
  const result: Record<string, number | null> = {};
  for (const [key, item] of Object.entries(value)) {
    if (!isNonEmptyString(key, 128)) throw new MarketDataValidationError("invalid_bundle", "feature names must be bounded strings");
    if (item !== null && (typeof item !== "number" || !Number.isFinite(item))) throw new MarketDataValidationError("invalid_bundle", `feature ${key} must be finite or null`);
    result[key] = item as number | null;
  }
  return result;
}

function normalizeSnapshot(value: unknown, timezone: string, cutoff: { readonly date: string; readonly time: number }): MarketSnapshot {
  if (!isRecord(value)) throw new MarketDataValidationError("invalid_bundle", "snapshot must be an object");
  const assetId = value.assetId;
  const anchor = value.anchor;
  const asOf = value.asOf;
  const retrievedAt = value.retrievedAt;
  const snapshotTimezone = value.timezone;
  const status = value.status;
  if (!isNonEmptyString(assetId, 128) || !SNAPSHOT_ANCHORS.includes(anchor as SnapshotAnchor)) throw new MarketDataValidationError("invalid_bundle", "snapshot assetId and anchor are required");
  if (!isNonEmptyString(retrievedAt, 64) || parseDateTime(retrievedAt) === null) throw new MarketDataValidationError("malformed_date", "snapshot retrievedAt must be an offset datetime");
  const retrievedTime = parseDateTime(retrievedAt);
  if (retrievedTime === null || retrievedTime > cutoff.time) throw new MarketDataValidationError("future_data", `snapshot retrievedAt ${retrievedAt} is after cutoff`);
  assertTimezone(snapshotTimezone);
  if (snapshotTimezone !== timezone) throw new MarketDataValidationError("invalid_bundle", "snapshot timezone must match the request timezone");
  if (status !== "ok" && status !== "stale" && status !== "missing") throw new MarketDataValidationError("invalid_bundle", "snapshot status is invalid");
  if (status === "missing") {
    if (asOf !== null && asOf !== undefined) throw new MarketDataValidationError("invalid_bundle", "missing snapshots cannot have asOf");
  } else {
    if (parseDateOnly(asOf) === null) throw new MarketDataValidationError("malformed_date", "snapshot asOf must be a canonical date");
    if ((asOf as string) > cutoff.date) throw new MarketDataValidationError("future_data", `snapshot asOf ${(asOf as string)} is after cutoff ${cutoff.date}`);
  }
  const price = assertPrice(value.price, "price");
  const close = assertPrice(value.close, "close");
  if (status === "missing" && (price !== null || close !== null)) throw new MarketDataValidationError("invalid_price", "missing snapshots cannot have prices");
  if (status !== "missing" && price === null && close === null) throw new MarketDataValidationError("invalid_price", "ok/stale snapshots require price or close");
  if (price === 0 || close === 0) throw new MarketDataValidationError("invalid_price", "prices must be positive when present");
  if (!isNonEmptyString(value.currency, 16)) throw new MarketDataValidationError("invalid_bundle", "currency is required");
  const change = value.change === undefined ? undefined : assertChange(value.change);
  const features = normalizeFeatures(value.features);
  const warnings = value.warnings === undefined ? undefined : normalizeWarnings(value.warnings, "snapshot warnings");
  return {
    assetId,
    anchor: anchor as SnapshotAnchor,
    asOf: asOf === undefined ? null : asOf as string | null,
    retrievedAt,
    timezone: snapshotTimezone,
    price,
    close,
    currency: value.currency,
    ...(change === undefined ? {} : { change }),
    ...(features === undefined ? {} : { features }),
    status,
    ...(warnings === undefined ? {} : { warnings }),
  };
}

export function validateMarketSnapshotBundle(value: unknown, request: MarketDataRequest): MarketSnapshotBundle {
  const parsedRequest = assertRequest(request);
  if (!isRecord(value)) throw new MarketDataValidationError("invalid_bundle", "bundle must be an object");
  if (JSON.stringify(value).length > MARKET_DATA_LIMITS.maxResponseBytes) throw new MarketDataValidationError("oversized_payload", "market data response exceeds the bounded payload limit");
  if (value.schemaVersion !== "market-snapshot-bundle/v1" || !isNonEmptyString(value.requestedAt, 64) || parseDateTime(value.requestedAt) === null || value.cutoff !== request.cutoff || value.timezone !== request.timezone) throw new MarketDataValidationError("invalid_bundle", "bundle metadata is invalid");
  if (!Array.isArray(value.assets) || value.assets.length !== parsedRequest.assetIds.length || value.assets.some((asset) => typeof asset !== "string") || value.assets.some((asset, index) => asset !== parsedRequest.assetIds[index])) throw new MarketDataValidationError("invalid_bundle", "bundle assets must exactly match the requested order");
  if (!Array.isArray(value.snapshots) || value.snapshots.length > MARKET_DATA_LIMITS.maxSnapshots) throw new MarketDataValidationError("oversized_payload", "snapshot rows exceed the bounded limit");
  if (!isRecord(value.provider) || !isNonEmptyString(value.provider.id, 128) || (value.provider.mode !== "public" && value.provider.mode !== "byok" && value.provider.mode !== "mock")) throw new MarketDataValidationError("invalid_bundle", "provider id and mode are required");
  const warnings = normalizeWarnings(value.warnings, "bundle warnings");
  const snapshots = value.snapshots.map((snapshot) => normalizeSnapshot(snapshot, request.timezone, parsedRequest.cutoff));
  const requestedAssets = new Set(parsedRequest.assetIds);
  const keys = new Set<string>();
  for (const snapshot of snapshots) {
    if (!requestedAssets.has(snapshot.assetId)) throw new MarketDataValidationError("unknown_asset", `unknown asset ${snapshot.assetId}`);
    const key = `${snapshot.assetId}\u0000${snapshot.anchor}`;
    if (keys.has(key)) throw new MarketDataValidationError("duplicate_snapshot", `duplicate snapshot ${snapshot.assetId}/${snapshot.anchor}`);
    keys.add(key);
  }
  const expectedCount = parsedRequest.assetIds.length * SNAPSHOT_ANCHORS.length;
  if (keys.size !== expectedCount) throw new MarketDataValidationError("incomplete_bundle", "bundle must include one explicit row for every asset and anchor; use status=missing when unavailable");
  if (!isNonEmptyString(value.integrityHash, 128) || value.integrityHash !== computeMarketSnapshotIntegrityHash({
    schemaVersion: "market-snapshot-bundle/v1",
    requestedAt: value.requestedAt,
    cutoff: value.cutoff,
    timezone: value.timezone,
    assets: value.assets,
    snapshots,
    provider: value.provider as { readonly id: string; readonly mode: ProviderMode },
    warnings,
  })) throw new MarketDataValidationError("invalid_bundle", "bundle integrityHash does not match non-secret data");
  return {
    schemaVersion: "market-snapshot-bundle/v1",
    requestedAt: value.requestedAt,
    cutoff: value.cutoff,
    timezone: value.timezone,
    assets: [...value.assets] as string[],
    snapshots,
    provider: { id: value.provider.id, mode: value.provider.mode },
    warnings,
    integrityHash: value.integrityHash,
  };
}

function dateFromParts(year: number, month: number, day: number): string {
  return `${year.toString().padStart(4, "0")}-${month.toString().padStart(2, "0")}-${day.toString().padStart(2, "0")}`;
}

function previousWeekday(date: Date): Date {
  const day = date.getUTCDay();
  if (day === 0) date.setUTCDate(date.getUTCDate() - 2);
  if (day === 6) date.setUTCDate(date.getUTCDate() - 1);
  return date;
}

/** Weekend-aware calendar anchors. Exchange holidays remain provider-owned. */
export function resolveSnapshotAnchors(cutoff: string): Readonly<Record<SnapshotAnchor, string>> {
  const boundary = parseBoundary(cutoff);
  if (boundary === null) throw new MarketDataValidationError("malformed_date", "cutoff must be an ISO date or offset datetime");
  const [yearText, monthText] = boundary.date.split("-");
  const year = Number(yearText);
  const month = Number(monthText);
  const yearStart = previousWeekday(new Date(Date.UTC(year, 0, 1)));
  const monthStart = previousWeekday(new Date(Date.UTC(year, month - 1, 1)));
  const latest = previousWeekday(new Date(Date.UTC(year, month - 1, Number(boundary.date.slice(8, 10)))));
  return {
    year_start: dateFromParts(yearStart.getUTCFullYear(), yearStart.getUTCMonth() + 1, yearStart.getUTCDate()),
    latest_month_start: dateFromParts(monthStart.getUTCFullYear(), monthStart.getUTCMonth() + 1, monthStart.getUTCDate()),
    latest_completed_session: dateFromParts(latest.getUTCFullYear(), latest.getUTCMonth() + 1, latest.getUTCDate()),
  };
}

export interface MockMarketSnapshotOptions {
  readonly providerId?: string;
  readonly requestedAt?: string;
}

export function createMockMarketSnapshotBundle(request: MarketDataRequest, options: MockMarketSnapshotOptions = {}): MarketSnapshotBundle {
  const parsed = assertRequest(request);
  const anchors = resolveSnapshotAnchors(request.cutoff);
  const requestedAt = options.requestedAt ?? request.requestedAt ?? (request.cutoff.length === 10 ? `${request.cutoff}T00:00:00Z` : request.cutoff);
  const momentumBase = [1, 2, 3, 4, 5, 2];
  const qualityBase = [5, 4, 2, 3, 1, 4];
  const momentumStep = [0.1, 0.05, -0.04, 0.02, 0.08, 0.03];
  const qualityStep = [-0.1, 0.06, 0.1, 0.04, 0.08, 0.02];
  const snapshots: MarketSnapshot[] = [];
  for (const [assetIndex, assetId] of parsed.assetIds.entries()) {
    for (const [anchorIndex, anchor] of SNAPSHOT_ANCHORS.entries()) {
      snapshots.push({
        assetId,
        anchor,
        asOf: anchors[anchor],
        retrievedAt: requestedAt,
        timezone: request.timezone,
        price: 100 + assetIndex * 7 + anchorIndex,
        close: 100 + assetIndex * 7 + anchorIndex,
        currency: "USD",
        change: anchorIndex === 0 ? null : 0.01 * anchorIndex,
        features: {
          momentum: (momentumBase[assetIndex % momentumBase.length] ?? assetIndex + 1) + anchorIndex * (momentumStep[assetIndex % momentumStep.length] ?? 0.1),
          quality: (qualityBase[assetIndex % qualityBase.length] ?? assetIndex + 2) + anchorIndex * (qualityStep[assetIndex % qualityStep.length] ?? 0.05),
        },
        status: "ok",
      });
    }
  }
  const base: Omit<MarketSnapshotBundle, "integrityHash"> = {
    schemaVersion: "market-snapshot-bundle/v1",
    requestedAt,
    cutoff: request.cutoff,
    timezone: request.timezone,
    assets: [...parsed.assetIds],
    snapshots,
    provider: { id: options.providerId ?? "deterministic-mock", mode: "mock" },
    warnings: ["Deterministic mock/offline market data; not live market data."],
  };
  return { ...base, integrityHash: computeMarketSnapshotIntegrityHash(base) };
}

export interface MarketDataProviderOptions {
  readonly baseUrl: string;
  readonly providerId: string;
  readonly fetcher?: typeof fetch;
}

export function normalizeMarketProviderUrl(value: unknown): string | null {
  if (!isNonEmptyString(value, 512)) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash || !url.hostname) return null;
    return url.toString().replace(/\/$/u, "");
  } catch {
    return null;
  }
}

function providerBody(request: MarketDataRequest): Record<string, unknown> {
  const anchors = resolveSnapshotAnchors(request.cutoff);
  return { assetIds: [...request.assetIds], anchors, cutoff: request.cutoff, timezone: request.timezone, requestedAt: request.requestedAt };
}

function providerUrl(baseUrl: string): string { return new URL("snapshots", `${baseUrl}/`).toString(); }

interface ProviderPayload {
  readonly snapshots: unknown;
  readonly warnings?: unknown;
}

function normalizeProviderPayload(value: unknown): ProviderPayload {
  if (!isRecord(value) || !Array.isArray(value.snapshots)) throw new MarketDataValidationError("invalid_bundle", "provider response must contain a snapshots array");
  return { snapshots: value.snapshots, ...(value.warnings === undefined ? {} : { warnings: value.warnings }) };
}

async function fetchProviderBundle(request: MarketDataRequest, signal: AbortSignal | undefined, mode: Exclude<ProviderMode, "mock">, providerId: string, baseUrl: string, apiKey: string | undefined, fetcher: typeof fetch): Promise<MarketSnapshotBundle> {
  assertRequest(request);
  const body = JSON.stringify(providerBody(request));
  if (body.length > MARKET_DATA_LIMITS.maxRequestBytes) throw new MarketDataValidationError("oversized_payload", "market data request exceeds the bounded payload limit");
  const headers: Record<string, string> = { Accept: "application/json", "Content-Type": "application/json" };
  if (mode === "byok" && apiKey !== undefined) headers.Authorization = `Bearer ${apiKey}`;
  const response = await fetcher(providerUrl(baseUrl), { method: "POST", headers, body, credentials: "omit", ...(signal === undefined ? {} : { signal }) });
  if (!response.ok) throw new Error(`Market data provider returned HTTP ${response.status}.`);
  const responseText = await response.text();
  if (responseText.length > MARKET_DATA_LIMITS.maxResponseBytes) throw new MarketDataValidationError("oversized_payload", "market data response exceeds the bounded payload limit");
  let payload: unknown;
  try { payload = JSON.parse(responseText) as unknown; } catch { throw new MarketDataValidationError("invalid_bundle", "provider response is not JSON"); }
  const providerPayload = normalizeProviderPayload(payload);
  const requestedAt = request.requestedAt ?? new Date().toISOString();
  const base: Omit<MarketSnapshotBundle, "integrityHash"> = {
    schemaVersion: "market-snapshot-bundle/v1",
    requestedAt,
    cutoff: request.cutoff,
    timezone: request.timezone,
    assets: [...request.assetIds],
    snapshots: providerPayload.snapshots as readonly MarketSnapshot[],
    provider: { id: providerId, mode },
    warnings: normalizeWarnings(providerPayload.warnings, "provider warnings"),
  };
  return validateMarketSnapshotBundle({ ...base, integrityHash: computeMarketSnapshotIntegrityHash(base) }, request);
}

function configuredFetcher(fetcher: typeof fetch | undefined): typeof fetch { return fetcher ?? fetch.bind(globalThis); }

export function createPublicMarketDataSource(options: MarketDataProviderOptions): MarketDataSource {
  const baseUrl = normalizeMarketProviderUrl(options.baseUrl);
  if (baseUrl === null) throw new MarketDataValidationError("invalid_provider_url", "public market provider must use an approved HTTPS URL");
  const fetcher = configuredFetcher(options.fetcher);
  return { fetchSnapshots: (request, signal) => fetchProviderBundle(request, signal, "public", options.providerId, baseUrl, undefined, fetcher) };
}

/** The key is captured by a closure and is used for one request only. It is not a source property, bundle field, hash input, or serialized value. */
export function createByokMarketDataSource(options: MarketDataProviderOptions & { readonly apiKey: string }): MarketDataSource {
  const baseUrl = normalizeMarketProviderUrl(options.baseUrl);
  if (baseUrl === null) throw new MarketDataValidationError("invalid_provider_url", "BYOK market provider must use an approved HTTPS URL");
  if (!isNonEmptyString(options.apiKey, 4096)) throw new MarketDataValidationError("invalid_request", "BYOK apiKey must be a non-empty bounded string");
  const providerId = options.providerId;
  let requestOnlyKey: string | undefined = options.apiKey;
  const fetcher = configuredFetcher(options.fetcher);
  let used = false;
  return {
    fetchSnapshots: async (request, signal) => {
      if (used) throw new MarketDataValidationError("byok_reused", "BYOK market data source is single-use.");
      used = true;
      try {
        return await fetchProviderBundle(request, signal, "byok", providerId, baseUrl, requestOnlyKey, fetcher);
      } finally {
        requestOnlyKey = undefined;
      }
    },
  };
}

function referencedFields(node: FactorAst, output: Set<string>): void {
  switch (node.op) {
    case "field": output.add(node.name); return;
    case "constant": return;
    case "add": case "sub": case "mul": case "div": referencedFields(node.left, output); referencedFields(node.right, output); return;
    case "neg": case "abs": case "rank": case "rolling_mean": case "rolling_std": referencedFields(node.input, output); return;
  }
}

export interface MarketSnapshotFmInputResult {
  readonly status: "ready" | "limited_data";
  readonly dataStatus: MarketDataStatus;
  readonly input: ComputeFmInput;
  readonly warnings: readonly string[];
  readonly requiredFields: readonly string[];
  readonly validFeatureCells: number;
}

export interface MarketSnapshotFmInputOptions {
  readonly currentValues?: ArrayLike<number>;
  readonly maxFactors?: number;
  readonly initialAllocation?: boolean;
  readonly costBps?: number;
}

/** Converts the validated bundle into the existing local FM input without forward-return or metric invention. */
export function marketSnapshotBundleToFmInput(bundle: MarketSnapshotBundle, factorExpressions: readonly FactorExpression[], options: MarketSnapshotFmInputOptions = {}): MarketSnapshotFmInputResult {
  const request: MarketDataRequest = { assetIds: bundle.assets, cutoff: bundle.cutoff, timezone: bundle.timezone, requestedAt: bundle.requestedAt };
  const validated = validateMarketSnapshotBundle(bundle, request);
  const fields = new Set<string>();
  for (const factor of factorExpressions) referencedFields(factor.expression, fields);
  fields.delete("close");
  const requiredFields = [...fields].sort();
  const snapshotsByKey = new Map(validated.snapshots.map((snapshot) => [`${snapshot.assetId}\u0000${snapshot.anchor}`, snapshot]));
  const observations = [...SNAPSHOT_ANCHORS].map((anchor) => {
    const rows = validated.assets.map((assetId) => snapshotsByKey.get(`${assetId}\u0000${anchor}`)!);
    const fieldsForObservation: Record<string, readonly (number | null)[]> = {
      close: rows.map((row) => row.close ?? row.price),
    };
    for (const field of requiredFields) fieldsForObservation[field] = rows.map((row) => row.features?.[field] ?? null);
    return { asOf: rows.find((row) => row.asOf !== null)?.asOf ?? validated.cutoff.slice(0, 10), fields: fieldsForObservation };
  }).sort((left, right) => left.asOf.localeCompare(right.asOf));
  let validFeatureCells = 0;
  let limited = false;
  for (const observation of observations) {
    const closeValues = observation.fields.close ?? [];
    if (closeValues.some((value) => value === null || !Number.isFinite(value))) limited = true;
    for (const field of requiredFields) {
      const values = observation.fields[field] ?? [];
      validFeatureCells += values.filter((value) => value !== null && Number.isFinite(value)).length;
      if (values.some((value) => value === null || !Number.isFinite(value))) limited = true;
    }
  }
  if (validated.snapshots.some((snapshot) => snapshot.status !== "ok")) limited = true;
  const input: ComputeFmInput = {
    cutoff: validated.cutoff,
    observations,
    factors: factorExpressions,
    ...(options.maxFactors === undefined ? {} : { maxFactors: options.maxFactors }),
    forecastHorizon: 1,
    ...(options.currentValues === undefined ? {} : { currentValues: Float64Array.from(options.currentValues) }),
    ...(options.initialAllocation === undefined ? {} : { initialAllocation: options.initialAllocation }),
    ...(options.costBps === undefined ? {} : { costBps: options.costBps }),
  };
  const warnings = [
    ...validated.warnings,
    ...validated.snapshots.flatMap((snapshot) => snapshot.warnings ?? []),
    "Only three point-in-time anchors are supplied; this is not a backtest window.",
    "Forward returns are unavailable; IC/ICIR remain offline factor metadata and are not recomputed from three snapshots.",
  ];
  return { status: limited ? "limited_data" : "ready", dataStatus: limited ? "limited_data" : validated.provider.mode, input, warnings: [...new Set(warnings)], requiredFields, validFeatureCells };
}

export function marketDataStatus(bundle: MarketSnapshotBundle): MarketDataStatus {
  return bundle.snapshots.some((snapshot) => snapshot.status !== "ok") ? "limited_data" : bundle.provider.mode;
}
