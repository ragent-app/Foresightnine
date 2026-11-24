import type { CurrentActivity, ProgressEvent } from "../api/types";

export type StageKey = "plan" | "find" | "read" | "compute" | "write";
export type StageState = "done" | "active" | "pending";

export interface Stage {
  key: StageKey;
  label: string;
  state: StageState;
  /** A note on the stage, e.g. which draft the writer is on. */
  detail: string;
}

const STAGE_LABEL: Readonly<Record<StageKey, string>> = {
  plan: "理解问题",
  find: "找资料",
  read: "读资料",
  compute: "记录与计算",
  write: "写报告与核验",
};

const ORDER: readonly StageKey[] = ["plan", "find", "read", "compute", "write"];

const TOOL_STAGE: Readonly<Record<string, StageKey>> = {
  plan_research_requirements: "plan",
  news_search: "find",
  search_library: "find",
  acquire_sources: "find",
  resolve_entity: "find",
  market_data: "find",
  historical_prices: "find",
  sec_company_concept: "find",
  company_financials: "find",
  filings: "find",
  issuer_documents: "find",
  read_sources: "read",
  search_source: "read",
  outline_source: "read",
  extract_statements: "read",
  record_facts: "compute",
  calculate_financials: "compute",
  create_chart: "compute",
  create_table: "compute",
  checkpoint_report_draft: "write",
  checkpoint_question_part: "write",
  submit_research: "write",
};

export function stageOf(tool: string | null | undefined): StageKey | null {
  return tool ? TOOL_STAGE[tool] ?? null : null;
}

function summaryOf(event: ProgressEvent): Record<string, unknown> {
  return event.kind === "tool" && event.summary && typeof event.summary === "object" ? event.summary as unknown as Record<string, unknown> : {};
}

function items(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter((item): item is Record<string, unknown> => !!item && typeof item === "object") : [];
}

/**
 * Five plain stages instead of every tool call. A stage the agent returns to is active again;
 * a stage with work behind it and not active is done.
 */
export function liveStages(events: readonly ProgressEvent[], current: CurrentActivity | null): Stage[] {
  const seen = new Set<StageKey>();
  let last: StageKey | null = null;
  let drafts = 0;
  for (const event of events) {
    const stage = event.kind === "submission" ? "write" : event.kind === "tool" ? stageOf(event.name) : null;
    if (!stage) continue;
    seen.add(stage);
    last = stage;
    if ((event.kind === "tool" && event.name === "submit_research") || event.kind === "submission") drafts += 1;
  }
  const active: StageKey | null = current
    ? current.kind === "tool" ? stageOf(current.name) ?? last : last === "write" ? "write" : last ?? "plan"
    : last;
  // Counts of sources, facts and calculations are in the run's one-line summary above the stages.
  const detail: Record<StageKey, string> = { plan: "", find: "", read: "", compute: "", write: drafts > 1 ? `第 ${drafts} 稿` : "" };
  const activeIndex = active ? ORDER.indexOf(active) : -1;
  return ORDER.map((key, index) => ({
    key,
    label: STAGE_LABEL[key],
    state: key === active ? "active" : seen.has(key) || index < activeIndex ? "done" : "pending",
    detail: detail[key],
  }));
}

/** Titles of the most recently obtained documents, newest first. */
export function latestFinds(events: readonly ProgressEvent[], limit = 2): string[] {
  const titles: string[] = [];
  for (const event of [...events].reverse()) {
    if (event.kind !== "tool" || event.name !== "acquire_sources") continue;
    for (const item of items(summaryOf(event).items)) {
      const title = typeof item.title === "string" ? item.title.trim() : "";
      if (title && (item.status === "acquired" || item.status === "retrieved") && !titles.includes(title)) titles.push(title);
      if (titles.length >= limit) return titles;
    }
  }
  return titles;
}
