import type { ResearchV2 } from "@foresightnine/contracts";

// M4 review: the user's unsaved changes are kept as a patch over the latest
// version and applied only when saving, so a new turn or an added source does
// not discard them. Saving always creates a new version (POST …/versions).

export type ReviewState = ResearchV2.ResearchVersion["review"];
export type ClaimRoleInput = ResearchV2.ResearchClaim["role"];

export interface ClaimEdit {
  text: string;
  role: ClaimRoleInput;
}

export interface ReviewDraft {
  review?: ReviewState;
  note?: string;
  summary?: string;
  /** Edits keyed by claim_id; a claim missing from the latest version is ignored. */
  claims?: Readonly<Record<string, ClaimEdit>>;
}

export const EMPTY_DRAFT: ReviewDraft = {};

function claimChanged(claim: ResearchV2.ResearchClaim, edit: ClaimEdit | undefined): boolean {
  return edit !== undefined && (edit.text.trim() !== claim.text || edit.role !== claim.role);
}

/** Claim edits that still apply to `version` and actually change something. */
export function pendingClaimEdits(version: ResearchV2.ResearchVersion, draft: ReviewDraft): string[] {
  const edits = draft.claims ?? {};
  return (version.content.claims ?? []).flatMap((claim) => (claim.claim_id && claimChanged(claim, edits[claim.claim_id]) ? [claim.claim_id] : []));
}

export function isDirty(version: ResearchV2.ResearchVersion, draft: ReviewDraft): boolean {
  return (draft.review !== undefined && draft.review !== version.review)
    || (draft.note !== undefined && draft.note !== (version.note ?? ""))
    || (draft.summary !== undefined && draft.summary !== (version.content.summary ?? ""))
    || pendingClaimEdits(version, draft).length > 0;
}

/** The effective review fields: the draft where set, otherwise the version. */
export function effectiveReview(version: ResearchV2.ResearchVersion, draft: ReviewDraft): { review: ReviewState; note: string; summary: string } {
  return {
    review: draft.review ?? version.review,
    note: draft.note ?? version.note ?? "",
    summary: draft.summary ?? version.content.summary ?? "",
  };
}

/** A reservation needs a note explaining it (spec §4.7). Returns a user-facing problem or null. */
export function draftProblem(version: ResearchV2.ResearchVersion, draft: ReviewDraft): string | null {
  const effective = effectiveReview(version, draft);
  if (effective.review === "reservation" && !effective.note.trim()) return "选择“保留意见”时，请在复核备注中写明原因。";
  const edits = draft.claims ?? {};
  for (const id of pendingClaimEdits(version, draft)) {
    const text = edits[id]!.text.trim();
    if (!text) return "结论文字不能为空。";
    if (text.length > 3000) return "结论文字不能超过 3,000 字。";
  }
  if (effective.note.length > 10000) return "复核备注不能超过 10,000 字。";
  if (effective.summary.length > 100000) return "研究总结过长。";
  return null;
}

/**
 * The body for POST /v2/research/topics/{id}/versions. Content is the latest version's content
 * as fetched (server-produced records unchanged), with the summary and edited claims replaced.
 */
export function buildVersionInput(version: ResearchV2.ResearchVersion, draft: ReviewDraft): ResearchV2.VersionInput {
  const effective = effectiveReview(version, draft);
  const edits = draft.claims ?? {};
  const claims = (version.content.claims ?? []).map((claim) => {
    const edit = claim.claim_id ? edits[claim.claim_id] : undefined;
    return claimChanged(claim, edit) ? { ...claim, text: edit!.text.trim(), role: edit!.role } : claim;
  });
  return {
    parent_version_id: version.version_id,
    review: effective.review,
    note: effective.note,
    content: { ...version.content, summary: effective.summary, claims },
  };
}

// ---------------------------------------------------------------------------
// Version diff (spec §4.7 对比): what changed from one version to the next.

export interface ClaimChange {
  claimId: string;
  before: ResearchV2.ResearchClaim | null;
  after: ResearchV2.ResearchClaim | null;
}

export interface VersionDiff {
  review: { before: ReviewState; after: ReviewState } | null;
  noteChanged: boolean;
  summaryChanged: boolean;
  turnsAdded: ResearchV2.ResearchTurn[];
  sourcesAdded: string[];
  claimsAdded: ClaimChange[];
  claimsRemoved: ClaimChange[];
  claimsChanged: ClaimChange[];
  factsAdded: ResearchV2.ResearchFact[];
  factsRemoved: ResearchV2.ResearchFact[];
  factsChanged: { before: ResearchV2.ResearchFact; after: ResearchV2.ResearchFact }[];
  calculationsAdded: ResearchV2.ResearchCalculation[];
  calculationsRemoved: ResearchV2.ResearchCalculation[];
  calculationsChanged: { before: ResearchV2.ResearchCalculation; after: ResearchV2.ResearchCalculation }[];
}

function byId<T>(items: readonly T[] | undefined, id: (item: T) => string | undefined): Map<string, T> {
  const map = new Map<string, T>();
  for (const item of items ?? []) {
    const key = id(item);
    if (key) map.set(key, item);
  }
  return map;
}

const FACT_KEYS = ["label", "value", "unit", "currency", "period", "source_id", "character_start", "character_end"] as const;
const CALC_KEYS = ["formula_id", "value", "unit", "currency", "decimal_places", "expression"] as const;

function differs<T extends object>(a: T, b: T, keys: readonly (keyof T)[]): boolean {
  return keys.some((key) => (a[key] ?? null) !== (b[key] ?? null));
}

export function diffVersions(before: ResearchV2.ResearchVersion, after: ResearchV2.ResearchVersion): VersionDiff {
  const a = before.content;
  const b = after.content;
  const turnIds = new Set((a.turns ?? []).map((turn) => turn.turn_id));
  const beforeSources = new Set(before.source_ids ?? []);

  const claimsBefore = byId(a.claims, (claim) => claim.claim_id);
  const claimsAfter = byId(b.claims, (claim) => claim.claim_id);
  const claimsAdded: ClaimChange[] = [];
  const claimsChanged: ClaimChange[] = [];
  for (const [id, claim] of claimsAfter) {
    const old = claimsBefore.get(id);
    if (!old) claimsAdded.push({ claimId: id, before: null, after: claim });
    else if (old.text !== claim.text || old.role !== claim.role) claimsChanged.push({ claimId: id, before: old, after: claim });
  }
  const claimsRemoved = [...claimsBefore].filter(([id]) => !claimsAfter.has(id)).map(([id, claim]) => ({ claimId: id, before: claim, after: null }));

  const factsBefore = byId(a.facts, (fact) => fact.fact_id);
  const factsAfter = byId(b.facts, (fact) => fact.fact_id);
  const calcsBefore = byId(a.calculations, (calc) => calc.calculation_id);
  const calcsAfter = byId(b.calculations, (calc) => calc.calculation_id);

  return {
    review: before.review !== after.review ? { before: before.review, after: after.review } : null,
    noteChanged: (before.note ?? "") !== (after.note ?? ""),
    summaryChanged: (a.summary ?? "") !== (b.summary ?? ""),
    turnsAdded: (b.turns ?? []).filter((turn) => !turnIds.has(turn.turn_id)),
    sourcesAdded: (after.source_ids ?? []).filter((id) => !beforeSources.has(id)),
    claimsAdded,
    claimsRemoved,
    claimsChanged,
    factsAdded: [...factsAfter].filter(([id]) => !factsBefore.has(id)).map(([, fact]) => fact),
    factsRemoved: [...factsBefore].filter(([id]) => !factsAfter.has(id)).map(([, fact]) => fact),
    factsChanged: [...factsAfter].flatMap(([id, fact]) => {
      const old = factsBefore.get(id);
      return old && differs(old, fact, FACT_KEYS) ? [{ before: old, after: fact }] : [];
    }),
    calculationsAdded: [...calcsAfter].filter(([id]) => !calcsBefore.has(id)).map(([, calc]) => calc),
    calculationsRemoved: [...calcsBefore].filter(([id]) => !calcsAfter.has(id)).map(([, calc]) => calc),
    calculationsChanged: [...calcsAfter].flatMap(([id, calc]) => {
      const old = calcsBefore.get(id);
      return old && differs(old, calc, CALC_KEYS) ? [{ before: old, after: calc }] : [];
    }),
  };
}

export function diffIsEmpty(diff: VersionDiff): boolean {
  return !diff.review && !diff.noteChanged && !diff.summaryChanged && diff.turnsAdded.length === 0 && diff.sourcesAdded.length === 0
    && diff.claimsAdded.length + diff.claimsRemoved.length + diff.claimsChanged.length === 0
    && diff.factsAdded.length + diff.factsRemoved.length + diff.factsChanged.length === 0
    && diff.calculationsAdded.length + diff.calculationsRemoved.length + diff.calculationsChanged.length === 0;
}

// ---------------------------------------------------------------------------
// New calculation form (spec §4.4 数字 页签). The engine computes first versus second:
// research_calculations.calculate — first is current/numerator, second is baseline/denominator.

export type FormulaId = ResearchV2.ResearchCalculation["formula_id"];

export const FORMULAS: { id: FormulaId; label: string; first: string; second: string; hint: string }[] = [
  { id: "growth_percent", label: "增长率", first: "本期数字", second: "基期数字", hint: "（本期 ÷ 基期 − 1）× 100，两个数字的期间必须不同。" },
  { id: "margin_percent", label: "利润率", first: "分子（例如营业利润）", second: "分母（例如总收入）", hint: "分子 ÷ 分母 × 100，两个数字的期间必须相同。" },
  { id: "difference", label: "差额", first: "被减数", second: "减数", hint: "第一个数字 − 第二个数字，单位和币种需要一致。" },
];
