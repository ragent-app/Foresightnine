import type { ResearchV2 } from "@foresightnine/contracts";
import type {
  CoverageDocument,
  CoverageMapView,
  CoverageRow,
  CurrentActivity,
  InterimSummary,
  ModelPhase,
  OperationStatus,
  ProgressEvent,
  ReconciliationCell,
  ReconciliationRow,
  ReconciliationView,
  RunProgress,
  RunUsage,
  ToolEventSummary,
  TurnAttributionView,
} from "../api/types";
import { gapText, MODEL_FAILURE_LABEL, toolLabel, toolReasonLabel, toolStatusLabel } from "./copy";
import { formatInteger } from "./format";

type ResearchContent = ResearchV2.ResearchContent;
type ResearchTurn = ResearchV2.ResearchTurn;
type ResearchClaim = ResearchV2.ResearchClaim;
type ResearchFact = ResearchV2.ResearchFact;
type ResearchCalculation = ResearchV2.ResearchCalculation;
type ResearchChart = ResearchV2.ResearchChart;
type Citation = ResearchV2.Citation;
type ResearchTopic = ResearchV2.ResearchTopic;

const RATIONALE_LIMIT = 300;
const NAME_LIMIT = 100;
const NOTE_LIMIT = 600;
const TITLE_LIMIT = 160;
const HOST_LIMIT = 120;
const DETAIL_LIMIT = 200;
const PREVIEW_LIMIT = 5;

function isRecord(value: unknown): value is Readonly<Record<string, unknown>> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
const int = (value: unknown): number | null => (typeof value === "number" && Number.isInteger(value) ? value : null);
const nonNegInt = (value: unknown): number | null => (typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null);
const text = (value: unknown, limit: number): string | null => (typeof value === "string" ? value.slice(0, limit) : null);
function cleanNote(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed ? trimmed.slice(0, NOTE_LIMIT) : null;
}

function parseToolSummary(raw: unknown): ToolEventSummary | null {
  if (!isRecord(raw)) return null;
  switch (raw["kind"]) {
    case "news_search": {
      const found_count = nonNegInt(raw["found_count"]);
      const eligible_count = nonNegInt(raw["eligible_count"]);
      const denied_count = nonNegInt(raw["denied_count"]);
      if (found_count === null || eligible_count === null || denied_count === null) return null;
      const previewRaw = Array.isArray(raw["leads_preview"]) ? raw["leads_preview"].slice(0, PREVIEW_LIMIT) : [];
      const leads_preview: { title: string; host: string; published_at: string | null }[] = [];
      for (const item of previewRaw) {
        if (!isRecord(item)) continue;
        const title = text(item["title"], TITLE_LIMIT) ?? "";
        const host = text(item["host"], HOST_LIMIT) ?? "";
        if (!title && !host) continue;
        leads_preview.push({
          title,
          host,
          published_at: text(item["published_at"], NAME_LIMIT),
        });
      }
      const cluster_count = nonNegInt(raw["cluster_count"]);
      const attributions = Array.isArray(raw["attributions"])
        ? raw["attributions"].filter((x): x is string => typeof x === "string").slice(0, 4)
        : [];
      return {
        kind: "news_search", found_count, eligible_count, denied_count, leads_preview,
        ...(cluster_count !== null ? { cluster_count } : {}),
        ...(attributions.length > 0 ? { attributions } : {}),
      };
    }
    case "search_library": {
      const hit_count = nonNegInt(raw["hit_count"]);
      const source_count = nonNegInt(raw["source_count"]);
      const unsearched_pages = nonNegInt(raw["unsearched_pages"]);
      if (hit_count === null || source_count === null || unsearched_pages === null) return null;
      const previewRaw = Array.isArray(raw["hits_preview"]) ? raw["hits_preview"].slice(0, PREVIEW_LIMIT) : [];
      const hits_preview: { title: string; page: number | null }[] = [];
      for (const item of previewRaw) {
        if (isRecord(item)) hits_preview.push({ title: text(item["title"], TITLE_LIMIT) ?? "", page: nonNegInt(item["page"]) });
      }
      return {
        kind: "search_library", query: text(raw["query"], TITLE_LIMIT) ?? "", hit_count, source_count, unsearched_pages,
        complete: raw["complete"] === true, hits_preview,
      };
    }
    case "acquire_sources": {
      const acquired_count = nonNegInt(raw["acquired_count"]);
      const failed_count = nonNegInt(raw["failed_count"]);
      if (acquired_count === null || failed_count === null) return null;
      const itemsRaw = Array.isArray(raw["items"]) ? raw["items"].slice(0, PREVIEW_LIMIT) : [];
      const items: { title: string; host: string; status: string; reason: string | null; total_characters: number | null }[] = [];
      for (const item of itemsRaw) {
        if (!isRecord(item)) continue;
        const status = text(item["status"], NAME_LIMIT);
        if (!status) continue;
        items.push({
          title: text(item["title"], TITLE_LIMIT) ?? "",
          host: text(item["host"], HOST_LIMIT) ?? "",
          status,
          reason: text(item["reason"], NAME_LIMIT),
          total_characters: nonNegInt(item["total_characters"]),
        });
      }
      return { kind: "acquire_sources", acquired_count, failed_count, items };
    }
    case "sec_company_concept": {
      const entity = text(raw["entity"], DETAIL_LIMIT);
      const taxonomy = text(raw["taxonomy"], NAME_LIMIT);
      const tag = text(raw["tag"], DETAIL_LIMIT);
      const unit = text(raw["unit"], NAME_LIMIT);
      const status = text(raw["status"], NAME_LIMIT);
      if (!entity || !taxonomy || !tag || !unit || !status) return null;
      return {
        kind: "sec_company_concept",
        entity,
        taxonomy,
        tag,
        unit,
        status,
        title: text(raw["title"], TITLE_LIMIT),
        total_characters: nonNegInt(raw["total_characters"]),
      };
    }
    case "historical_prices": {
      const instrument_id = text(raw["instrument_id"], NAME_LIMIT);
      const start_date = text(raw["start_date"], NAME_LIMIT);
      const end_date = text(raw["end_date"], NAME_LIMIT);
      const status = text(raw["status"], NAME_LIMIT);
      if (!instrument_id || !start_date || !end_date || !status) return null;
      return {
        kind: "historical_prices",
        instrument_id,
        start_date,
        end_date,
        status,
        title: text(raw["title"], TITLE_LIMIT),
      };
    }
    case "read_sources": {
      const read_count = nonNegInt(raw["read_count"]);
      if (read_count === null) return null;
      const itemsRaw = Array.isArray(raw["items"]) ? raw["items"].slice(0, PREVIEW_LIMIT) : [];
      const items: { source_id: string; title: string; start: number; end: number; total_characters: number | null }[] = [];
      for (const item of itemsRaw) {
        if (!isRecord(item)) continue;
        const source_id = text(item["source_id"], NAME_LIMIT);
        const title = text(item["title"], TITLE_LIMIT) ?? "";
        const start = nonNegInt(item["start"]);
        const end = nonNegInt(item["end"]);
        if (!source_id || start === null || end === null) continue;
        items.push({
          source_id,
          title,
          start,
          end,
          total_characters: nonNegInt(item["total_characters"]),
        });
      }
      return { kind: "read_sources", read_count, items };
    }
    case "record_facts": {
      const recorded_count = nonNegInt(raw["recorded_count"]);
      if (recorded_count === null) return null;
      const previewRaw = Array.isArray(raw["facts_preview"]) ? raw["facts_preview"].slice(0, PREVIEW_LIMIT) : [];
      const facts_preview: { label: string; value: string; unit: string; currency: string | null; period: string }[] = [];
      for (const item of previewRaw) {
        if (!isRecord(item)) continue;
        const label = text(item["label"], DETAIL_LIMIT);
        const value = text(item["value"], NAME_LIMIT);
        const unit = text(item["unit"], NAME_LIMIT);
        const period = text(item["period"], NAME_LIMIT);
        if (!label || value === null || !unit || !period) continue;
        facts_preview.push({
          label,
          value,
          unit,
          currency: text(item["currency"], NAME_LIMIT),
          period,
        });
      }
      return { kind: "record_facts", recorded_count, facts_preview };
    }
    case "calculate_financials": {
      const calculated_count = nonNegInt(raw["calculated_count"]);
      if (calculated_count === null) return null;
      const previewRaw = Array.isArray(raw["calcs_preview"]) ? raw["calcs_preview"].slice(0, PREVIEW_LIMIT) : [];
      const calcs_preview: { formula_id: string; value: string; unit: string; currency: string | null; expression: string }[] = [];
      for (const item of previewRaw) {
        if (!isRecord(item)) continue;
        const formula_id = text(item["formula_id"], NAME_LIMIT);
        const value = text(item["value"], NAME_LIMIT);
        const unit = text(item["unit"], NAME_LIMIT);
        const expression = text(item["expression"], DETAIL_LIMIT);
        if (!formula_id || value === null || !unit || !expression) continue;
        calcs_preview.push({
          formula_id,
          value,
          unit,
          currency: text(item["currency"], NAME_LIMIT),
          expression,
        });
      }
      return { kind: "calculate_financials", calculated_count, calcs_preview };
    }
    case "create_chart": {
      const chart_id = text(raw["chart_id"], NAME_LIMIT)?.trim() ?? "";
      const chart_type = text(raw["chart_type"], 40)?.trim() ?? "";
      const title = text(raw["title"], TITLE_LIMIT)?.trim() ?? "";
      const series_count = nonNegInt(raw["series_count"]);
      const point_count = nonNegInt(raw["point_count"]);
      if (!chart_id || !chart_type || !title || series_count === null || point_count === null) return null;
      return { kind: "create_chart", chart_id, chart_type, title, series_count, point_count };
    }
    default:
      return null;
  }
}

const OPERATION_STATUSES = new Set<OperationStatus>(["pending", "running", "completed", "failed", "cancelled"]);
const MODEL_PHASES = new Set<ModelPhase>(["connecting", "thinking", "responding", "retrying"]);

/** Sanitizes a single checkpoint or SSE progress event, preserving an explicit `index` when valid. */
export function sanitizeProgressEvent(event: unknown, fallbackIndex?: number): ProgressEvent | null {
  if (!isRecord(event)) return null;
  const index = nonNegInt(event["index"]) ?? fallbackIndex;
  if (index === undefined) return null;
  switch (event["kind"]) {
    case "model": {
      const detail = isRecord(event["usage_detail"]) ? event["usage_detail"] : {};
      const note = cleanNote(event["note"]);
      return {
        index,
        kind: "model",
        finish_reason: text(event["finish_reason"], NAME_LIMIT),
        usage: int(event["usage"]),
        input_tokens: int(detail["input_tokens"]) ?? int(event["input_tokens"]),
        output_tokens: int(detail["output_tokens"]) ?? int(event["output_tokens"]),
        settlement: text(event["settlement"], NAME_LIMIT),
        ...(note ? { note } : {}),
      };
    }
    case "model_failure":
      return { index, kind: "model_failure", code: text(event["code"], NAME_LIMIT) };
    case "tool": {
      const summary = parseToolSummary(event["summary"]);
      return {
        index,
        kind: "tool",
        name: text(event["name"], NAME_LIMIT),
        status: text(event["status"], NAME_LIMIT),
        reason: text(event["reason"], NAME_LIMIT),
        rationale: text(event["rationale"], RATIONALE_LIMIT),
        denied_count: int(event["denied_count"]),
        ...(summary ? { summary } : {}),
      };
    }
    case "submission":
      return { index, kind: "submission", status: text(event["status"], NAME_LIMIT) };
    default:
      return null;
  }
}

export function sanitizeCurrentActivity(raw: unknown): CurrentActivity | null {
  if (!isRecord(raw)) return null;
  if (raw["kind"] === "model") {
    const phaseRaw = text(raw["phase"], NAME_LIMIT);
    const phase = phaseRaw && MODEL_PHASES.has(phaseRaw as ModelPhase) ? (phaseRaw as ModelPhase) : null;
    const attemptRaw = nonNegInt(raw["retry_attempt"]);
    return {
      kind: "model",
      phase,
      retry_attempt: attemptRaw !== null && attemptRaw > 0 ? attemptRaw : null,
      started_at: text(raw["started_at"], NAME_LIMIT),
    };
  }
  if (raw["kind"] === "tool") {
    const name = text(raw["name"], NAME_LIMIT);
    if (!name) return null;
    return {
      kind: "tool",
      name,
      rationale: text(raw["rationale"], RATIONALE_LIMIT),
      target: text(raw["target"], DETAIL_LIMIT),
      started_at: text(raw["started_at"], NAME_LIMIT),
    };
  }
  return null;
}

export function sanitizeInterimSummary(raw: unknown): InterimSummary | null {
  if (!isRecord(raw)) return null;
  const leads_count = nonNegInt(raw["leads_count"]);
  const acquired_sources_count = nonNegInt(raw["acquired_sources_count"]);
  const read_sources_count = nonNegInt(raw["read_sources_count"]);
  const facts_count = nonNegInt(raw["facts_count"]);
  const calculations_count = nonNegInt(raw["calculations_count"]);
  if (
    leads_count === null ||
    acquired_sources_count === null ||
    read_sources_count === null ||
    facts_count === null ||
    calculations_count === null
  ) {
    return null;
  }
  return { leads_count, acquired_sources_count, read_sources_count, facts_count, calculations_count };
}

export function sanitizeRunUsage(raw: unknown): RunUsage | null {
  if (!isRecord(raw)) return null;
  const model_calls = nonNegInt(raw["model_calls"]);
  const search_calls = nonNegInt(raw["search_calls"]);
  const total_tokens = nonNegInt(raw["total_tokens"]);
  if (model_calls === null || search_calls === null || total_tokens === null) return null;
  return { model_calls, search_calls, total_tokens };
}

export function sanitizeRunProgress(raw: unknown): RunProgress | null {
  if (!isRecord(raw)) return null;
  const operation_id = text(raw["operation_id"], NAME_LIMIT);
  const statusRaw = text(raw["operation_status"], NAME_LIMIT);
  if (!operation_id || !statusRaw || !OPERATION_STATUSES.has(statusRaw as OperationStatus)) return null;
  const turnRaw = isRecord(raw["turn"]) ? raw["turn"] : {};
  const eventsRaw = Array.isArray(raw["events"]) ? raw["events"] : [];
  const events: ProgressEvent[] = [];
  eventsRaw.forEach((item, pos) => {
    const clean = sanitizeProgressEvent(item, pos);
    if (clean) events.push(clean);
  });
  const interim = sanitizeInterimSummary(raw["interim"]);
  return {
    operation_id,
    operation_status: statusRaw as OperationStatus,
    turn: {
      index: int(turnRaw["index"]) ?? 0,
      question: text(turnRaw["question"], 2000) ?? "",
    },
    usage: sanitizeRunUsage(raw["usage"]) ?? { model_calls: 0, search_calls: 0, total_tokens: 0 },
    current: sanitizeCurrentActivity(raw["current"]),
    ...(interim ? { interim } : {}),
    events,
    event_count: nonNegInt(raw["event_count"]) ?? events.length,
    started_at: text(raw["started_at"], NAME_LIMIT),
    updated_at: text(raw["updated_at"], NAME_LIMIT),
    version_id: text(raw["version_id"], NAME_LIMIT),
  };
}

/** Mirrors apps/api/app/research_progress.py sanitize_event; `index` is the position in the raw array. */
export function parseTurnEvents(raw: readonly unknown[] | undefined): ProgressEvent[] {
  const result: ProgressEvent[] = [];
  (raw ?? []).forEach((event, index) => {
    if (!isRecord(event)) return;
    // Stored ResearchTurn.events use the raw array index as the authoritative sequence position.
    const clean = sanitizeProgressEvent({ ...event, index }, index);
    if (clean) result.push(clean);
  });
  return result;
}

export type StepTone = "support" | "review" | "error" | "neutral";
export type StepRow =
  | {
      kind: "tool";
      index: number;
      tool: string | null;
      label: string;
      status: string;
      tone: StepTone;
      rationale: string | null;
      reason: string | null;
      summary?: ToolEventSummary | null;
    }
  | { kind: "model"; index: number; text: string; note?: string | null };

function statusTone(status: string | null): StepTone {
  switch (status) {
    case "completed":
    case "read":
    case "acquired":
    case "ok":
    case "saved":
    case "created":
      return "support";
    case "invalid":
    case "unavailable":
    case "denied_or_unavailable":
    case "empty":
    case "needs_attention":
      return "review";
    case "failed":
    case "unknown":
      return "error";
    default:
      return "neutral";
  }
}

export function stepRows(events: ProgressEvent[]): StepRow[] {
  return events.map((event): StepRow => {
    switch (event.kind) {
      case "tool": {
        const summary = parseToolSummary(event.summary);
        return {
          kind: "tool",
          index: event.index,
          tool: event.name,
          label: toolLabel(event.name),
          status: toolStatusLabel(event.status),
          tone: statusTone(event.status),
          rationale: event.rationale,
          reason: toolReasonLabel(event.reason),
          ...(summary ? { summary } : {}),
        };
      }
      case "submission":
        return {
          kind: "tool",
          index: event.index,
          tool: "submit_research",
          label: toolLabel("submit_research"),
          status: toolStatusLabel(event.status),
          tone: statusTone(event.status),
          rationale: null,
          reason: null,
        };
      case "model": {
        let label = "模型调用";
        if (event.input_tokens !== null && event.output_tokens !== null) {
          label = `模型调用 · ${formatInteger(event.input_tokens)} 输入 / ${formatInteger(event.output_tokens)} 输出 Token`;
        } else if (event.usage !== null) {
          label = `模型调用 · ${formatInteger(event.usage)} Token`;
        }
        const note = cleanNote(event.note);
        return { kind: "model", index: event.index, text: label, ...(note ? { note } : {}) };
      }
      case "model_failure":
        return {
          kind: "model",
          index: event.index,
          text: `模型调用失败 · ${event.code ? MODEL_FAILURE_LABEL[event.code] ?? `请求没有返回可用结果（${event.code}）` : "请求没有返回可用结果"}`,
        };
    }
  });
}

export function toolStepCount(events: readonly ProgressEvent[]): number {
  return events.filter((event) => event.kind === "tool" || event.kind === "submission").length;
}

export type ClaimRole = "support" | "counter" | "review";
export const ROLE_LABEL: Readonly<Record<ClaimRole, string>> = { support: "支持", counter: "反方", review: "待复核" };

export function claimRole(claim: ResearchClaim): ClaimRole {
  if ((claim.challenges_claim_ids ?? []).length > 0) return "review";
  return claim.role === "counterargument" ? "counter" : "support";
}

export interface CitationEntry {
  n: number;
  citation: Citation;
  claimId: string;
  citationIndex: number;
}
export interface CitationIndex {
  numberFor(citation: Citation): number;
  entries: CitationEntry[];
}

const citationKey = (c: Citation): string => `${c.source_id}\u0000${c.location.character_start}\u0000${c.location.character_end}`;

export function buildCitationIndex(claims: readonly ResearchClaim[]): CitationIndex {
  const numbers = new Map<string, number>();
  const entries: CitationEntry[] = [];
  claims.forEach((claim) => {
    (claim.citations ?? []).forEach((citation, citationIndex) => {
      const key = citationKey(citation);
      if (numbers.has(key)) return;
      const n = entries.length + 1;
      numbers.set(key, n);
      entries.push({ n, citation, claimId: claim.claim_id ?? "", citationIndex });
    });
  });
  return { numberFor: (citation) => numbers.get(citationKey(citation)) ?? 0, entries };
}

function select<T extends { readonly turn_id?: string | null }>(
  items: readonly T[] | undefined,
  ids: readonly string[] | undefined,
  idOf: (item: T) => string | undefined,
  turnId: string,
): T[] {
  const all = items ?? [];
  if (ids && ids.length > 0) {
    const wanted = new Set(ids);
    return all.filter((item) => {
      const id = idOf(item);
      return id !== undefined && wanted.has(id);
    });
  }
  return all.filter((item) => item.turn_id === turnId);
}

export function turnContent(
  content: ResearchContent,
  turn: ResearchTurn,
): { claims: ResearchClaim[]; facts: ResearchFact[]; calculations: ResearchCalculation[] } {
  return {
    claims: select(content.claims, turn.claim_ids, (c) => c.claim_id, turn.turn_id),
    facts: select(content.facts, turn.fact_ids, (f) => f.fact_id, turn.turn_id),
    calculations: select(content.calculations, turn.calculation_ids, (c) => c.calculation_id, turn.turn_id),
  };
}

/** Charts belonging to `turn`, preserving `turn.chart_ids` order or falling back to `chart.turn_id`. */
export function turnCharts(content: ResearchContent, turn: ResearchTurn): ResearchChart[] {
  const all = content.charts ?? [];
  if (all.length === 0) return [];
  const ids = turn.chart_ids;
  if (ids && ids.length > 0) {
    const byId = new Map(all.map((chart) => [chart.chart_id, chart]));
    const seen = new Set<string>();
    const result: ResearchChart[] = [];
    for (const id of ids) {
      if (seen.has(id)) continue;
      const chart = byId.get(id);
      if (!chart) continue;
      seen.add(id);
      result.push(chart);
    }
    return result;
  }
  return all.filter((chart) => chart.turn_id === turn.turn_id);
}

export function partialSummary({ facts, calculations, claims, groundedParts = 0 }: { facts: number; calculations: number; claims: number; groundedParts?: number }): string {
  if (facts === 0 && calculations === 0 && claims === 0) return groundedParts > 0
    ? `已保存 ${groundedParts} 项有依据的研究结果或缺口。`
    : "这一轮没有取得可用成果。";
  const parts: string[] = [];
  if (facts > 0) parts.push(`已记录 ${facts} 个数字`);
  if (calculations > 0) parts.push(`完成 ${calculations} 项计算`);
  parts.push(claims === 0 ? "还没有形成可引用的结论" : `形成 ${claims} 条结论`);
  return `${parts.join("，")}。`;
}

export type AnswerTab = "claims" | "numbers" | "sources" | "steps";

export function answerSummary(turn: ResearchTurn, content: ResearchContent): { label: string; tab: AnswerTab }[] {
  const { claims, facts, calculations } = turnContent(content, turn);
  const result: { label: string; tab: AnswerTab }[] = [];
  if (claims.length > 0) result.push({ label: `${claims.length} 条结论`, tab: "claims" });
  if (facts.length > 0 || calculations.length > 0) {
    const parts: string[] = [];
    if (facts.length > 0) parts.push(`${facts.length} 个数字`);
    if (calculations.length > 0) parts.push(`${calculations.length} 项计算`);
    result.push({ label: parts.join(" · "), tab: "numbers" });
  }
  const sources = new Set((turn.read_ranges ?? []).map((range) => range.source_id)).size;
  if (sources > 0) result.push({ label: `${sources} 份资料`, tab: "sources" });
  const steps = toolStepCount(parseTurnEvents(turn.events));
  if (steps > 0) result.push({ label: `过程 ${steps} 步`, tab: "steps" });
  return result;
}

export function turnDurationMs(turn: ResearchTurn): number | null {
  const ms = Date.parse(turn.finished_at) - Date.parse(turn.started_at);
  return Number.isFinite(ms) ? ms : null;
}

export function readCoverage(
  content: ResearchContent,
  sourceId: string,
): { ranges: [number, number][]; readCharacters: number; totalCharacters: number | null } {
  const raw: [number, number][] = [];
  for (const turn of content.turns ?? []) {
    for (const range of turn.read_ranges ?? []) {
      if (range.source_id === sourceId && range.character_end > range.character_start) {
        raw.push([range.character_start, range.character_end]);
      }
    }
  }
  raw.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const ranges: [number, number][] = [];
  for (const [start, end] of raw) {
    const last = ranges[ranges.length - 1];
    if (last && start <= last[1]) last[1] = Math.max(last[1], end);
    else ranges.push([start, end]);
  }
  const readCharacters = ranges.reduce((sum, [start, end]) => sum + (end - start), 0);
  const row = (content.sources ?? []).find((source) => source["source_id"] === sourceId);
  const coverage = row?.["initial_prompt_coverage"];
  const total = isRecord(coverage) ? coverage["text_characters"] : undefined;
  const totalCharacters = typeof total === "number" && Number.isInteger(total) && total >= 0 ? total : null;
  return { ranges, readCharacters, totalCharacters };
}

/**
 * Whether a user-made version after ``baseVersionId`` changed content. Regenerating a turn starts
 * from that base, so such review edits would not carry over (the server enforces the same rule).
 */
export function reviewEditsAfter(versions: readonly ResearchV2.ResearchVersion[], baseVersionId: string): boolean {
  const start = versions.findIndex((version) => version.version_id === baseVersionId);
  if (start < 0) return false;
  for (let index = start + 1; index < versions.length; index += 1) {
    const version = versions[index]!;
    if (version.created_by === "user" && JSON.stringify(version.content) !== JSON.stringify(versions[index - 1]!.content)) return true;
  }
  return false;
}

/** The newest version that still holds the turn a regenerated turn replaced. */
export function versionWithTurn(versions: readonly ResearchV2.ResearchVersion[], turnId: string): ResearchV2.ResearchVersion | null {
  for (let index = versions.length - 1; index >= 0; index -= 1) {
    if (versions[index]!.content.turns?.some((turn) => turn.turn_id === turnId)) return versions[index]!;
  }
  return null;
}

const TITLE_FALLBACK_CHARS = 24;

/** Until a title is summarised (or for topics from before titles), the question's opening stands in. */
export function shortQuestion(question: string): string {
  const text = question.replace(/\s+/g, " ").trim();
  if (text.length <= TITLE_FALLBACK_CHARS) return text;
  const sentence = /^(.{6,}?)[。！？!?；;]/.exec(text)?.[1];
  if (sentence && sentence.length <= TITLE_FALLBACK_CHARS) return sentence;
  return `${text.slice(0, TITLE_FALLBACK_CHARS).trimEnd()}…`;
}

export function topicTitle(topic: ResearchTopic): string {
  const named = topic.title?.trim();
  if (named) return named;
  const question = topic.request.question?.trim();
  if (question) return shortQuestion(question);
  for (let i = topic.versions.length - 1; i >= 0; i -= 1) {
    const first = topic.versions[i]?.content.turns?.[0]?.question.trim();
    if (first) return shortQuestion(first);
  }
  return "文档概览";
}

export function suggestionsFor(content: ResearchContent): { key: "gap" | "counter"; label: string; prefill: string }[] {
  const result: { key: "gap" | "counter"; label: string; prefill: string }[] = [];
  const turns = content.turns ?? [];
  const lastGaps = turns[turns.length - 1]?.gaps ?? [];
  const firstGap = lastGaps[0];
  if (firstGap) {
    result.push({ key: "gap", label: "补充上一轮的缺口", prefill: `请继续补充上一轮的缺口：${gapText(firstGap)}` });
  }
  if ((content.claims ?? []).some((claim) => claimRole(claim) === "support")) {
    result.push({ key: "counter", label: "寻找反方证据", prefill: "请寻找与当前主要结论相反的证据，并说明它们的强度。" });
  }
  return result;
}

// ---- run views saved on a turn (reconciliation, coverage map, attributions); parsed defensively, never trusted ----

const BADGES = new Set(["consistent", "rounding_only", "differs_minor", "differs_material"]);

function zhEn(value: unknown): { zh: string; en: string } {
  if (isRecord(value)) return { zh: text(value["zh"], DETAIL_LIMIT) ?? "", en: text(value["en"], DETAIL_LIMIT) ?? "" };
  return { zh: "", en: "" };
}

export function parseReconciliation(raw: unknown): ReconciliationView | null {
  if (!isRecord(raw) || !Array.isArray(raw["rows"])) return null;
  const rows: ReconciliationRow[] = [];
  for (const item of raw["rows"].slice(0, 50)) {
    if (!isRecord(item) || typeof item["row_id"] !== "string") continue;
    const status = item["status"];
    if (status !== "single_source" && status !== "consistent" && status !== "rounding_only" && status !== "differs") continue;
    const badgeRaw = item["badge"];
    const badge = typeof badgeRaw === "string" && BADGES.has(badgeRaw)
      ? (badgeRaw as ReconciliationRow["badge"])
      : (status === "differs" ? "differs_minor" : status);
    const cells: ReconciliationCell[] = [];
    for (const cell of Array.isArray(item["cells"]) ? item["cells"].slice(0, 12) : []) {
      if (!isRecord(cell)) continue;
      cells.push({
        fact_id: text(cell["fact_id"], NAME_LIMIT) ?? "",
        source_id: text(cell["source_id"], NAME_LIMIT) ?? "",
        source_class: text(cell["source_class"], NAME_LIMIT) ?? "other",
        host: text(cell["host"], HOST_LIMIT),
        display_value: text(cell["display_value"], NAME_LIMIT) ?? "",
        published_at: text(cell["published_at"], NAME_LIMIT),
        as_of: text(cell["as_of"], NAME_LIMIT),
        is_reference: cell["is_reference"] === true,
        low_ocr_confidence: cell["low_ocr_confidence"] === true,
        quotation_excerpt: text(cell["quotation_excerpt"], 500),
      });
    }
    const ref = isRecord(item["reference"]) ? item["reference"] : null;
    rows.push({
      row_id: item["row_id"],
      title: zhEn(item["title"]),
      status,
      severity: text(item["severity"], NAME_LIMIT),
      badge,
      cells,
      reference: ref
        ? { value: ref["value"] === null || ref["value"] === undefined ? null : String(ref["value"]).slice(0, NAME_LIMIT), source_class: text(ref["source_class"], NAME_LIMIT) }
        : null,
      independent_source_count: nonNegInt(item["independent_source_count"]) ?? cells.length,
    });
  }
  if (rows.length === 0) return null;
  const counts: Record<string, number> = {};
  if (isRecord(raw["counts"])) {
    for (const [k, v] of Object.entries(raw["counts"])) {
      const n = nonNegInt(v);
      if (n !== null) counts[k] = n;
    }
  }
  return { version: nonNegInt(raw["version"]) ?? 1, counts, rows };
}

export function parseCoverageMap(raw: unknown): CoverageMapView | null {
  if (!isRecord(raw) || !Array.isArray(raw["rows"])) return null;
  const rows: CoverageRow[] = [];
  for (const item of raw["rows"].slice(0, 12)) {
    if (!isRecord(item) || typeof item["category"] !== "string" || typeof item["status"] !== "string") continue;
    rows.push({
      category: item["category"],
      label: zhEn(item["label"]),
      status: item["status"],
      status_label: zhEn(item["status_label"]),
      data_as_of: text(item["data_as_of"], NAME_LIMIT),
      is_stale: typeof item["is_stale"] === "boolean" ? item["is_stale"] : null,
      trading_days_behind: nonNegInt(item["trading_days_behind"]),
      days_behind_clock: nonNegInt(item["days_behind_clock"]),
      not_established: Array.isArray(item["not_established"])
        ? item["not_established"].filter((x): x is string => typeof x === "string").slice(0, 8).map((x) => x.slice(0, 300))
        : [],
      sources_obtained: nonNegInt(item["sources_obtained"]) ?? 0,
    });
  }
  const documents: CoverageDocument[] = [];
  for (const item of Array.isArray(raw["documents"]) ? raw["documents"].slice(0, 30) : []) {
    if (!isRecord(item) || typeof item["source_id"] !== "string") continue;
    const pages = isRecord(item["pages"]) ? item["pages"] : null;
    const byStatus: Record<string, number> = {};
    if (pages && isRecord(pages["by_status"])) {
      for (const [k, v] of Object.entries(pages["by_status"])) {
        const n = nonNegInt(v);
        if (n !== null) byStatus[k] = n;
      }
    }
    documents.push({
      origin: text(item["origin"], NAME_LIMIT),
      ingestion_kind: text(item["ingestion_kind"], NAME_LIMIT),
      retrieval_method: text(item["retrieval_method"], NAME_LIMIT),
      permissions: isRecord(item["permissions"]) ? Object.fromEntries(Object.entries(item["permissions"]).filter((entry): entry is [string, boolean] => typeof entry[1] === "boolean")) : null,
      source_id: item["source_id"],
      category: text(item["category"], NAME_LIMIT) ?? "other",
      title: text(item["title"], TITLE_LIMIT),
      chars_total: nonNegInt(item["chars_total"]),
      read_fraction: typeof item["read_fraction"] === "number" ? item["read_fraction"] : null,
      pages: pages
        ? {
            total: nonNegInt(pages["total"]) ?? 0,
            by_status: byStatus,
            read: nonNegInt(pages["read"]) ?? 0,
            unread_text_pages: nonNegInt(pages["unread_text_pages"]) ?? 0,
            unsearched_count: nonNegInt(pages["unsearched_count"]) ?? 0,
            ocr_low_confidence_pages: Array.isArray(pages["ocr_low_confidence_pages"])
              ? pages["ocr_low_confidence_pages"].filter((x): x is number => typeof x === "number").slice(0, 50)
              : [],
          }
        : null,
    });
  }
  const clock = isRecord(raw["research_clock"]) ? raw["research_clock"] : null;
  const run = isRecord(raw["run"]) ? raw["run"] : null;
  return {
    version: nonNegInt(raw["version"]) ?? 1,
    run_status: run ? text(run["status"], NAME_LIMIT) : null,
    clock_date: clock ? text(clock["utc_date"], NAME_LIMIT) : null,
    rows,
    documents,
  };
}

export function parseAttributions(raw: unknown): TurnAttributionView[] {
  const out: TurnAttributionView[] = [];
  for (const item of Array.isArray(raw) ? raw.slice(0, 8) : []) {
    if (!isRecord(item)) continue;
    const id = text(item["id"], NAME_LIMIT);
    const label = text(item["text"], DETAIL_LIMIT);
    if (id && label) out.push({ id, text: label, url: text(item["url"], 300) });
  }
  return out;
}
