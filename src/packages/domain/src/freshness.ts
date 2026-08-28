/**
 * Deterministic, client-safe freshness policy.
 *
 * The policy never consults the wall clock. Callers provide both the data
 * timestamp and the runtime as-of timestamp, which makes Web, Android, tests,
 * and offline archives evaluate the same input identically.
 */
export type ProviderFreshnessStatus = "fresh" | "stale" | "unavailable";

export type DeclaredFreshness = "current" | "historical_only" | "stale";

export type FreshnessStatus =
  | "fresh"
  | "historical_only"
  | "stale"
  | "provider_stale"
  | "provider_unavailable"
  | "invalid_date"
  | "future_date";

export interface FreshnessPolicy {
  /** Inclusive maximum age. Data exactly at this age is still fresh. */
  readonly maxAgeMs: number;
}

export const DEFAULT_FRESHNESS_POLICY: FreshnessPolicy = Object.freeze({
  maxAgeMs: 24 * 60 * 60 * 1000,
});

export interface FreshnessInput {
  readonly dataAsOf: string;
  readonly runtimeAsOf: string;
  readonly providerStatus: ProviderFreshnessStatus;
  readonly declaredFreshness?: DeclaredFreshness;
}

export interface FreshnessDecision {
  readonly status: FreshnessStatus;
  readonly ageMs: number | null;
  readonly isCurrent: boolean;
  readonly historicalOnly: boolean;
  readonly allowHistoricalDisplay: boolean;
  readonly allowCurrentPublicDefault: boolean;
  readonly label: "current" | "historical_only";
}

function parseIsoAsOf(value: string): number | null {
  const trimmed = value.trim();
  const dateOnly = /^(\d{4})-(\d{2})-(\d{2})$/.exec(trimmed);
  if (dateOnly) {
    const year = Number(dateOnly[1]);
    const month = Number(dateOnly[2]);
    const day = Number(dateOnly[3]);
    const parsed = new Date(Date.UTC(year, month - 1, day));
    if (parsed.getUTCFullYear() !== year || parsed.getUTCMonth() !== month - 1 || parsed.getUTCDate() !== day) return null;
    return parsed.getTime();
  }

  // Date-time inputs must use an explicit offset so their meaning does not
  // depend on the device's local timezone. Fractional seconds are allowed.
  const dateTime = /^(\d{4})-(\d{2})-(\d{2})T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.exec(trimmed);
  if (!dateTime) return null;
  const year = Number(dateTime[1]);
  const month = Number(dateTime[2]);
  const day = Number(dateTime[3]);
  const calendarDate = new Date(Date.UTC(year, month - 1, day));
  if (calendarDate.getUTCFullYear() !== year || calendarDate.getUTCMonth() !== month - 1 || calendarDate.getUTCDate() !== day) return null;
  const parsed = Date.parse(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

function invalidDecision(status: "invalid_date" | "future_date"): FreshnessDecision {
  return {
    status,
    ageMs: null,
    isCurrent: false,
    historicalOnly: true,
    allowHistoricalDisplay: true,
    allowCurrentPublicDefault: false,
    label: "historical_only",
  };
}

function nonCurrentDecision(status: Exclude<FreshnessStatus, "fresh">, ageMs: number | null): FreshnessDecision {
  return {
    status,
    ageMs,
    isCurrent: false,
    historicalOnly: true,
    allowHistoricalDisplay: true,
    allowCurrentPublicDefault: false,
    label: "historical_only",
  };
}

export function evaluateFreshness(input: FreshnessInput, policy: FreshnessPolicy = DEFAULT_FRESHNESS_POLICY): FreshnessDecision {
  if (!Number.isFinite(policy.maxAgeMs) || policy.maxAgeMs < 0) throw new RangeError("Freshness maxAgeMs must be a finite non-negative number");
  const dataTime = parseIsoAsOf(input.dataAsOf);
  const runtimeTime = parseIsoAsOf(input.runtimeAsOf);
  if (dataTime === null || runtimeTime === null) return invalidDecision("invalid_date");
  const ageMs = runtimeTime - dataTime;
  if (ageMs < 0) return invalidDecision("future_date");
  if (input.providerStatus === "unavailable") return nonCurrentDecision("provider_unavailable", ageMs);
  if (input.providerStatus === "stale") return nonCurrentDecision("provider_stale", ageMs);
  if (input.declaredFreshness === "historical_only") return nonCurrentDecision("historical_only", ageMs);
  if (input.declaredFreshness === "stale" || ageMs > policy.maxAgeMs) return nonCurrentDecision("stale", ageMs);
  return {
    status: "fresh",
    ageMs,
    isCurrent: true,
    historicalOnly: false,
    allowHistoricalDisplay: true,
    allowCurrentPublicDefault: true,
    label: "current",
  };
}
