import { canonicalJson, sha256Hex, evaluateFactorAst, FactorAstError, type ComputeFmInput, type FactorAst, type FactorExpression } from "@foresightnine/fm-engine";

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

/** Point-in-time FX rates expressed as CNY per unit of currency. */
export interface FxSnapshot {
  readonly asOf: string;
  readonly retrievedAt: string;
  readonly provider: string;
  readonly cnyPerUnit: Readonly<Record<string, number>>;
}

export interface MarketSnapshotBundle {
  readonly schemaVersion: "market-snapshot-bundle/v1";
  readonly requestedAt: string;
  readonly cutoff: string;
  readonly timezone: string;
  readonly assets: readonly string[];
  readonly snapshots: readonly MarketSnapshot[];
  /** Absent/null FX is valid evidence but never sufficient for Public Default. */
  readonly fx?: FxSnapshot | null;
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

interface CutoffBoundary {
  readonly date: string;
  readonly asOfDate: string;
  readonly time: number;
}

function parseBoundary(value: string): CutoffBoundary | null {
  const dateTime = parseDateTime(value);
  if (dateTime !== null) {
    const calendarDate = new Date(Date.UTC(Number(value.slice(0, 4)), Number(value.slice(5, 7)) - 1, Number(value.slice(8, 10))));
    calendarDate.setUTCDate(calendarDate.getUTCDate() - 1);
    return { date: value.slice(0, 10), asOfDate: calendarDate.toISOString().slice(0, 10), time: dateTime };
  }
  const dateOnly = parseDateOnly(value);
  return dateOnly === null ? null : { date: value, asOfDate: value, time: dateOnly + 86_399_999 };
}

function assertTimezone(value: unknown): asserts value is string {
  if (!isNonEmptyString(value, 128) || !/^[A-Za-z0-9_+./:-]+$/u.test(value)) throw new MarketDataValidationError("invalid_bundle", "timezone must be a bounded identifier");
}

function assertRequest(request: MarketDataRequest): { readonly cutoff: CutoffBoundary; readonly assetIds: readonly string[] } {
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
  const fxRates: { [key: string]: CanonicalValue } = {};
  for (const key of Object.keys(bundle.fx?.cnyPerUnit ?? {}).sort()) fxRates[key] = bundle.fx?.cnyPerUnit[key] ?? null;
  return {
    assets: [...bundle.assets],
    cutoff: bundle.cutoff,
    fx: bundle.fx === undefined || bundle.fx === null ? null : { asOf: bundle.fx.asOf, retrievedAt: bundle.fx.retrievedAt, provider: bundle.fx.provider, cnyPerUnit: fxRates },
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

function normalizeFxSnapshot(value: unknown, cutoff: CutoffBoundary): FxSnapshot | null {
  if (value === undefined || value === null) return null;
  if (!isRecord(value) || !isNonEmptyString(value.asOf, 32) || parseDateOnly(value.asOf) === null || value.asOf > cutoff.asOfDate
    || !isNonEmptyString(value.retrievedAt, 64) || parseDateTime(value.retrievedAt) === null
    || Date.parse(value.retrievedAt) > cutoff.time || !isNonEmptyString(value.provider, 128)
    || !isRecord(value.cnyPerUnit) || Object.keys(value.cnyPerUnit).length === 0 || Object.keys(value.cnyPerUnit).length > 64) {
    throw new MarketDataValidationError("invalid_bundle", "fx snapshot metadata is invalid");
  }
  const cnyPerUnit: Record<string, number> = {};
  for (const [currency, rate] of Object.entries(value.cnyPerUnit)) {
    if (!isNonEmptyString(currency, 16) || typeof rate !== "number" || !Number.isFinite(rate) || rate <= 0) throw new MarketDataValidationError("invalid_bundle", "fx rates must be finite positive numbers");
    cnyPerUnit[currency] = rate;
  }
  if (cnyPerUnit.CNY === undefined) throw new MarketDataValidationError("invalid_bundle", "fx snapshot must include CNY");
  return { asOf: value.asOf, retrievedAt: value.retrievedAt, provider: value.provider, cnyPerUnit };
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

function normalizeSnapshot(value: unknown, timezone: string, cutoff: CutoffBoundary): MarketSnapshot {
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
    if ((asOf as string) > cutoff.asOfDate) throw new MarketDataValidationError("future_data", `snapshot asOf ${(asOf as string)} is after the last completed date ${cutoff.asOfDate}`);
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
  const fx = normalizeFxSnapshot(value.fx, parsedRequest.cutoff);
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
    fx,
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
    ...(fx === null ? {} : { fx }),
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
  const effectiveDate = new Date(Date.UTC(Number(boundary.date.slice(0, 4)), Number(boundary.date.slice(5, 7)) - 1, Number(boundary.date.slice(8, 10))));
  // A date-only cutoff denotes the end of that calendar day. An explicit
  // datetime is conservatively treated as an intraday boundary, so the
  // current calendar date can never be mistaken for a completed session.
  if (cutoff.includes("T")) effectiveDate.setUTCDate(effectiveDate.getUTCDate() - 1);
  const year = effectiveDate.getUTCFullYear();
  const month = effectiveDate.getUTCMonth() + 1;
  const yearStart = previousWeekday(new Date(Date.UTC(year, 0, 1)));
  const monthStart = previousWeekday(new Date(Date.UTC(year, month - 1, 1)));
  const latest = previousWeekday(effectiveDate);
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
  readonly fx?: unknown;
  readonly warnings?: unknown;
}

function normalizeProviderPayload(value: unknown): ProviderPayload {
  if (!isRecord(value) || !Array.isArray(value.snapshots)) throw new MarketDataValidationError("invalid_bundle", "provider response must contain a snapshots array");
  return { snapshots: value.snapshots, ...(value.fx === undefined ? {} : { fx: value.fx }), ...(value.warnings === undefined ? {} : { warnings: value.warnings }) };
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
  const cutoff = assertRequest(request).cutoff;
  const fx = normalizeFxSnapshot(providerPayload.fx, cutoff);
  const base: Omit<MarketSnapshotBundle, "integrityHash"> = {
    schemaVersion: "market-snapshot-bundle/v1",
    requestedAt,
    cutoff: request.cutoff,
    timezone: request.timezone,
    assets: [...request.assetIds],
    snapshots: providerPayload.snapshots as readonly MarketSnapshot[],
    fx,
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

export type MarketRuntimeStatus = "idle" | "loading" | "ready" | "limited" | "error";

export interface MarketRuntimeState {
  readonly status: MarketRuntimeStatus;
  readonly mode: ProviderMode;
  readonly providerId: string | null;
  readonly bundle: MarketSnapshotBundle | null;
  readonly integrityHash: string | null;
  readonly lastError: string | null;
}

export interface MarketRuntimeConfig {
  readonly mode: ProviderMode;
  readonly providerId?: string;
  readonly publicBaseUrl?: string;
  readonly byokBaseUrl?: string;
  readonly byokApiKey?: string;
}

export interface MarketRuntimeController {
  readonly getState: () => MarketRuntimeState;
  readonly subscribe: (listener: (state: MarketRuntimeState) => void) => () => void;
  readonly configure: (config: MarketRuntimeConfig) => void;
  readonly refresh: (request: MarketDataRequest) => Promise<MarketSnapshotBundle>;
}

const INITIAL_RUNTIME_STATE: MarketRuntimeState = { status: "idle", mode: "mock", providerId: null, bundle: null, integrityHash: null, lastError: null };

function runtimeErrorMessage(error: unknown): string {
  return error instanceof Error && error.message.trim().length > 0 ? error.message : "Market data provider is unavailable.";
}

function withoutByokKey(config: MarketRuntimeConfig): MarketRuntimeConfig {
  const { byokApiKey: _discarded, ...safeConfig } = config;
  return safeConfig;
}

/** Shared state machine for Web and Android. Secrets are held only by the selected source closure. */
export function createMarketRuntimeController(options: { readonly fetcher?: typeof fetch } = {}): MarketRuntimeController {
  let config: MarketRuntimeConfig = { mode: "mock", providerId: "deterministic-mock" };
  let source: MarketDataSource | null = null;
  let state = INITIAL_RUNTIME_STATE;
  const listeners = new Set<(next: MarketRuntimeState) => void>();
  const emit = (next: MarketRuntimeState): void => { state = next; listeners.forEach((listener) => listener(state)); };
  const configure = (next: MarketRuntimeConfig): void => {
    config = { ...next };
    source = null;
    emit({ status: "idle", mode: next.mode, providerId: next.providerId ?? null, bundle: null, integrityHash: null, lastError: null });
  };
  const refresh = async (request: MarketDataRequest): Promise<MarketSnapshotBundle> => {
    emit({ ...state, status: "loading", lastError: null });
    try {
      if (source === null) {
        const providerId = config.providerId ?? (config.mode === "mock" ? "deterministic-mock" : "configured-provider");
        if (config.mode === "mock") source = { fetchSnapshots: async (nextRequest) => createMockMarketSnapshotBundle(nextRequest, { providerId }) };
        else if (config.mode === "public") {
          if (!config.publicBaseUrl) throw new MarketDataValidationError("invalid_provider_url", "Public provider URL is not configured.");
          source = createPublicMarketDataSource({ baseUrl: config.publicBaseUrl, providerId, ...(options.fetcher ? { fetcher: options.fetcher } : {}) });
        } else {
          if (!config.byokBaseUrl || !config.byokApiKey) throw new MarketDataValidationError("invalid_request", "BYOK provider URL and key are required.");
          source = createByokMarketDataSource({ baseUrl: config.byokBaseUrl, providerId, apiKey: config.byokApiKey, ...(options.fetcher ? { fetcher: options.fetcher } : {}) });
        }
      }
      const bundle = await source.fetchSnapshots(request);
      const status = marketDataStatus(bundle) === "limited_data" ? "limited" : "ready";
      emit({ status, mode: config.mode, providerId: bundle.provider.id, bundle, integrityHash: bundle.integrityHash, lastError: null });
      if (config.mode === "byok") {
        source = null;
        config = withoutByokKey(config);
      }
      return bundle;
    } catch (error) {
      source = null;
      if (config.mode === "byok") config = withoutByokKey(config);
      emit({ ...state, status: "error", bundle: null, integrityHash: null, lastError: runtimeErrorMessage(error) });
      throw error;
    }
  };
  return { getState: () => state, subscribe: (listener) => { listeners.add(listener); return () => listeners.delete(listener); }, configure, refresh };
}

export interface LatestPriceAsset {
  readonly price: number;
  readonly quoteCurrency: string;
  readonly asOf: string;
  readonly freshness: string;
  readonly assetClass?: string;
}

type AssetWithId = LatestPriceAsset & ({ readonly id: string } | { readonly assetId: string });

function latestPriceAssetId(asset: AssetWithId): string {
  return "assetId" in asset ? asset.assetId : asset.id;
}

/** Applies only the latest completed-session rows; cash remains a fixture-safe residual. */
export function applyLatestCompletedSessionPrices<T extends AssetWithId>(assets: readonly T[], bundle: MarketSnapshotBundle): readonly T[] {
  const latest = new Map(bundle.snapshots.filter((snapshot) => snapshot.anchor === "latest_completed_session").map((snapshot) => [snapshot.assetId, snapshot]));
  return assets.map((asset) => {
    const assetId = latestPriceAssetId(asset);
    if (asset.assetClass === "cash" || assetId === "CASH") return asset;
    const row = latest.get(assetId);
    const price = row?.close ?? row?.price;
    if (!row || row.status !== "ok" || price === null || price === undefined || !Number.isFinite(price) || price <= 0) throw new MarketDataValidationError("invalid_price", `No valid latest completed-session price for ${assetId}.`);
    return { ...asset, price, quoteCurrency: row.currency, asOf: row.asOf ?? bundle.cutoff.slice(0, 10), freshness: "current" };
  });
}

export interface AnchorFactorMetricObservation {
  /** Cross-sectional Spearman IC of the factor at this anchor against the forward return to the next anchor. */
  readonly ic: number;
  readonly asOf: string;
  readonly forwardAsOf: string;
  readonly assets: number;
}

export interface AnchorFactorMetric {
  readonly factorId: number;
  readonly icMean: number;
  readonly icir: number;
  readonly observations: readonly AnchorFactorMetricObservation[];
  /** True only when at least two valid anchor-to-anchor IC points exist. */
  readonly sufficient: boolean;
  readonly error?: "unknown_field" | "invalid_data" | "insufficient_assets" | "insufficient_anchors";
}

/**
 * Compute IC/ICIR for each factor from the three price anchors themselves.
 *
 * The factor is evaluated at each anchor, the forward return is the close
 * change to the next anchor, and the IC is the cross-sectional Spearman
 * correlation between the two. With three anchors this yields at most two IC
 * points, so the metrics carry explicit low-power warnings upstream instead
 * of masquerading as a backtest. Mock bundles keep their library metadata;
 * this function is for live licensed bundles.
 */
export function computeAnchorFactorMetrics(bundle: MarketSnapshotBundle, factorExpressions: readonly FactorExpression[]): readonly AnchorFactorMetric[] {
  const request: MarketDataRequest = { assetIds: bundle.assets, cutoff: bundle.cutoff, timezone: bundle.timezone, requestedAt: bundle.requestedAt };
  const validated = validateMarketSnapshotBundle(bundle, request);
  const snapshotsByKey = new Map(validated.snapshots.map((snapshot) => [`${snapshot.assetId}\u0000${snapshot.anchor}`, snapshot]));
  const anchorObservations = [...SNAPSHOT_ANCHORS].map((anchor) => {
    const rows = validated.assets.map((assetId) => snapshotsByKey.get(`${assetId}\u0000${anchor}`)!);
    const featureFields = [...new Set(rows.flatMap((row) => Object.keys(row.features ?? {})))].sort();
    return {
      asOf: rows.find((row) => row.asOf !== null)?.asOf ?? validated.cutoff.slice(0, 10),
      fields: {
        close: rows.map((row) => row.close ?? row.price),
        ...Object.fromEntries(featureFields.map((field) => [field, rows.map((row) => row.features?.[field] ?? null)])),
      } as Record<string, readonly (number | null)[]>,
    };
  }).sort((left, right) => left.asOf.localeCompare(right.asOf));
  if (anchorObservations.length < 2) {
    return factorExpressions.map((factor): AnchorFactorMetric => ({ factorId: factor.id, icMean: 0, icir: 0, observations: [], sufficient: false, error: "insufficient_anchors" }));
  }
  const legs = anchorObservations.slice(0, -1).map((observation, index) => {
    const next = anchorObservations[index + 1]!;
    const closeSeries = observation.fields.close ?? [];
    const forwardReturns = closeSeries.map((close, asset) => {
      const nextClose = (next.fields.close ?? [])[asset];
      return typeof close === "number" && typeof nextClose === "number" && close > 0 && nextClose > 0 ? nextClose / close - 1 : null;
    });
    return { asOf: observation.asOf, forwardAsOf: next.asOf, forwardReturns, signals: observation.fields };
  });
  return factorExpressions.map((factor): AnchorFactorMetric => {
    let signalsByAsset: readonly Float64Array[];
    try {
      signalsByAsset = legs.map((leg) => evaluateFactorAst(factor.expression, [{ asOf: leg.asOf, fields: leg.signals }])[0]!);
    } catch (error) {
      const code = error instanceof FactorAstError && error.code === "unknown_field" ? "unknown_field" : "invalid_data";
      return { factorId: factor.id, icMean: 0, icir: 0, observations: [], sufficient: false, error: code };
    }
    const legIcs: number[] = [];
    const observations: AnchorFactorMetricObservation[] = [];
    let insufficientAssets = false;
    legs.forEach((leg, index) => {
      const signalValues = signalsByAsset[index]!;
      const signalSeries: number[] = [];
      const returnSeries: number[] = [];
      signalValues.forEach((signal, asset) => {
        const forward = leg.forwardReturns[asset];
        if (Number.isFinite(signal) && typeof forward === "number" && Number.isFinite(forward)) {
          signalSeries.push(signal);
          returnSeries.push(forward);
        }
      });
      if (signalSeries.length < 5) {
        insufficientAssets = true;
        return;
      }
      const ic = spearman(signalSeries, returnSeries);
      if (!Number.isFinite(ic)) return;
      legIcs.push(ic);
      observations.push({ ic, asOf: leg.asOf, forwardAsOf: leg.forwardAsOf, assets: signalSeries.length });
    });
    if (legIcs.length < 2) {
      return { factorId: factor.id, icMean: legIcs.length === 0 ? 0 : meanOf(legIcs), icir: 0, observations, sufficient: false, error: legIcs.length === 0 && insufficientAssets ? "insufficient_assets" : "insufficient_anchors" };
    }
    const icMeanValue = meanOf(legIcs);
    const sampleStd = Math.sqrt(legIcs.reduce((total, value) => total + (value - icMeanValue) ** 2, 0) / (legIcs.length - 1));
    return {
      factorId: factor.id,
      icMean: icMeanValue,
      icir: sampleStd < 1e-12 ? 0 : icMeanValue / sampleStd,
      observations,
      sufficient: true,
    };
  });
}

function meanOf(values: readonly number[]): number {
  return values.length === 0 ? 0 : values.reduce((total, value) => total + value, 0) / values.length;
}

function rankAverage(values: readonly number[]): number[] {
  const indexed = values.map((value, index) => ({ value, index })).sort((left, right) => left.value - right.value);
  const ranks = new Array<number>(values.length).fill(0);
  let position = 0;
  while (position < indexed.length) {
    let end = position;
    while (end + 1 < indexed.length && indexed[end + 1]!.value === indexed[position]!.value) end += 1;
    const average = (position + end) / 2 + 1;
    for (let slot = position; slot <= end; slot += 1) ranks[indexed[slot]!.index] = average;
    position = end + 1;
  }
  return ranks;
}

function spearman(x: readonly number[], y: readonly number[]): number {
  const rx = rankAverage(x);
  const ry = rankAverage(y);
  const ax = meanOf(rx);
  const ay = meanOf(ry);
  let numerator = 0;
  let left = 0;
  let right = 0;
  for (let index = 0; index < x.length; index += 1) {
    const dx = rx[index]! - ax;
    const dy = ry[index]! - ay;
    numerator += dx * dy;
    left += dx * dx;
    right += dy * dy;
  }
  return Math.sqrt(left * right) < 1e-12 ? 0 : numerator / Math.sqrt(left * right);
}
