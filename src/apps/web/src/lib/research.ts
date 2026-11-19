import type { ResearchV2 } from "@foresightnine/contracts";
import type { ProgressEvent } from "../api/types";
import { gapText, MODEL_FAILURE_LABEL, toolLabel, toolReasonLabel, toolStatusLabel } from "./copy";
import { formatInteger } from "./format";

type ResearchContent = ResearchV2.ResearchContent;
type ResearchTurn = ResearchV2.ResearchTurn;
type ResearchClaim = ResearchV2.ResearchClaim;
type ResearchFact = ResearchV2.ResearchFact;
type ResearchCalculation = ResearchV2.ResearchCalculation;
type Citation = ResearchV2.Citation;
type ResearchTopic = ResearchV2.ResearchTopic;

const RATIONALE_LIMIT = 300;
const NAME_LIMIT = 100;

function isRecord(value: unknown): value is Readonly<Record<string, unknown>> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
const int = (value: unknown): number | null => (typeof value === "number" && Number.isInteger(value) ? value : null);
const text = (value: unknown, limit: number): string | null => (typeof value === "string" ? value.slice(0, limit) : null);

/** Mirrors apps/api/app/research_progress.py sanitize_event; `index` is the position in the raw array. */
export function parseTurnEvents(raw: readonly unknown[] | undefined): ProgressEvent[] {
  const result: ProgressEvent[] = [];
  (raw ?? []).forEach((event, index) => {
    if (!isRecord(event)) return;
    switch (event["kind"]) {
      case "model": {
        const detail = isRecord(event["usage_detail"]) ? event["usage_detail"] : {};
        result.push({
          index,
          kind: "model",
          finish_reason: text(event["finish_reason"], NAME_LIMIT),
          usage: int(event["usage"]),
          input_tokens: int(detail["input_tokens"]),
          output_tokens: int(detail["output_tokens"]),
          settlement: text(event["settlement"], NAME_LIMIT),
        });
        return;
      }
      case "model_failure":
        result.push({ index, kind: "model_failure", code: text(event["code"], NAME_LIMIT) });
        return;
      case "tool":
        result.push({
          index,
          kind: "tool",
          name: text(event["name"], NAME_LIMIT),
          status: text(event["status"], NAME_LIMIT),
          reason: text(event["reason"], NAME_LIMIT),
          rationale: text(event["rationale"], RATIONALE_LIMIT),
          denied_count: int(event["denied_count"]),
        });
        return;
      case "submission":
        result.push({ index, kind: "submission", status: text(event["status"], NAME_LIMIT) });
        return;
      default:
        return;
    }
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
    }
  | { kind: "model"; index: number; text: string };

function statusTone(status: string | null): StepTone {
  switch (status) {
    case "completed":
    case "read":
    case "acquired":
      return "support";
    case "invalid":
    case "unavailable":
    case "denied_or_unavailable":
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
      case "tool":
        return {
          kind: "tool",
          index: event.index,
          tool: event.name,
          label: toolLabel(event.name),
          status: toolStatusLabel(event.status),
          tone: statusTone(event.status),
          rationale: event.rationale,
          reason: toolReasonLabel(event.reason),
        };
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
        return { kind: "model", index: event.index, text: label };
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

export function partialSummary({ facts, calculations, claims }: { facts: number; calculations: number; claims: number }): string {
  if (facts === 0 && calculations === 0 && claims === 0) return "这一轮没有取得可用成果。";
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

export function topicTitle(topic: ResearchTopic): string {
  const question = topic.request.question?.trim();
  if (question) return question;
  for (let i = topic.versions.length - 1; i >= 0; i -= 1) {
    const first = topic.versions[i]?.content.turns?.[0]?.question.trim();
    if (first) return first;
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
