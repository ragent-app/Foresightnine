import type { ResearchV2 } from "@foresightnine/contracts";

/** Next-run options shown in the research settings panel (spec §4.6). Form values stay strings. */
export interface RunOptions {
  /** Web search for leads. On by default (user decision 2026-11-19); sent explicitly as true/false. */
  discover: "on" | "off";
  autoFetch: "auto" | "off";
  budget: BudgetFields;
  /** Sources the next turn reads; null means every source of the topic (backend default). */
  selectedSourceIds: readonly string[] | null;
}

export interface BudgetFields {
  modelCalls: string;
  searchCalls: string;
  totalTokens: string;
  minutes: string;
  costUsd: string;
  inputTokensPerCall: string;
}

export type BudgetKey = keyof BudgetFields;

export const EMPTY_BUDGET: BudgetFields = { modelCalls: "", searchCalls: "", totalTokens: "", minutes: "", costUsd: "", inputTokensPerCall: "" };
export const DEFAULT_OPTIONS: RunOptions = { discover: "on", autoFetch: "auto", budget: EMPTY_BUDGET, selectedSourceIds: null };

export const BUDGET_FIELDS: { key: BudgetKey; label: string; decimal?: boolean; placeholder: string }[] = [
  { key: "modelCalls", label: "模型调用上限", placeholder: "不设上限" },
  { key: "searchCalls", label: "搜索次数上限", placeholder: "不设上限" },
  { key: "totalTokens", label: "Token 上限", placeholder: "不设上限" },
  { key: "minutes", label: "运行时长上限（分钟）", placeholder: "不设上限" },
  { key: "costUsd", label: "费用上限（美元）", decimal: true, placeholder: "不设上限" },
  { key: "inputTokensPerCall", label: "单次输入 Token 上限", placeholder: "自动" },
];

const INTEGER = /^\d+$/;
const DECIMAL = /^\d+(\.\d{1,6})?$/;
// Backend limits: research_runs.py ResearchRunRequest; minutes become seconds.
const MAX_INTEGER = 1_000_000_000;

export function budgetError(key: BudgetKey, raw: string): string | null {
  const value = raw.trim();
  if (value === "") return null;
  if (key === "costUsd") return DECIMAL.test(value) ? null : "请输入不小于 0 的金额，最多 6 位小数";
  if (!INTEGER.test(value)) return "请输入不小于 0 的整数";
  if (Number(value) > MAX_INTEGER) return "数值过大";
  if (key === "inputTokensPerCall" && Number(value) === 0) return "单次输入 Token 上限必须大于 0";
  return null;
}

export function optionsValid(options: RunOptions): boolean {
  return BUDGET_FIELDS.every(({ key }) => budgetError(key, options.budget[key]) === null);
}

function integerOrNull(raw: string): number | null {
  const value = raw.trim();
  return value === "" ? null : Number(value);
}

/**
 * Maps the panel to run request fields. Empty means "not set"; an explicit 0 is sent as 0
 * (AGENTS.md: no budget is added unless the user sets one).
 *
 * `discover_sources` is always sent as a boolean. The backend treats an explicit null as caller
 * input and then as "off" (research_runs.resolve_automatic_research_request only fills omitted
 * fields), so null must never be sent.
 */
export function runRequestFields(options: RunOptions): Omit<ResearchV2.ResearchRunRequest, "parent_version_id"> {
  const fields: { -readonly [K in keyof ResearchV2.ResearchRunRequest]?: ResearchV2.ResearchRunRequest[K] } = {
    discover_sources: options.discover === "on",
    auto_fetch_official: options.autoFetch === "auto",
  };
  if (options.selectedSourceIds !== null) fields.selected_source_ids = [...options.selectedSourceIds];
  const { budget } = options;
  const calls = integerOrNull(budget.modelCalls);
  const searches = integerOrNull(budget.searchCalls);
  const tokens = integerOrNull(budget.totalTokens);
  const minutes = integerOrNull(budget.minutes);
  const perCall = integerOrNull(budget.inputTokensPerCall);
  const cost = budget.costUsd.trim();
  if (calls !== null) fields.max_model_calls = calls;
  if (searches !== null) fields.max_search_calls = searches;
  if (tokens !== null) fields.max_total_tokens = tokens;
  if (minutes !== null) fields.max_elapsed_seconds = minutes * 60;
  if (perCall !== null) fields.max_input_tokens_per_call = perCall;
  if (cost !== "") fields.max_cost_usd = cost;
  return fields;
}

export function hasBudget(options: RunOptions): boolean {
  return BUDGET_FIELDS.some(({ key }) => options.budget[key].trim() !== "");
}

/** Converts a `datetime-local` value to an ISO instant (the backend requires a timezone). */
export function localInputToIso(value: string): string | null {
  if (!value) return null;
  const time = new Date(value).getTime();
  return Number.isNaN(time) ? null : new Date(time).toISOString();
}

export function isoToLocalInput(iso: string | null | undefined): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
