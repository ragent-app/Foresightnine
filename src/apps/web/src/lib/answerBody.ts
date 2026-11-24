import type { ResearchV2 } from "@foresightnine/contracts";

type Turn = ResearchV2.ResearchTurn;
type Calculation = ResearchV2.ResearchCalculation;
type Table = ResearchV2.ResearchTable;

export type AnswerBody = { text: string; kind: "report" | "draft" } | null;

/**
 * What the answer reads as: the reviewed report, or — when the run could not deliver one — the
 * writer's own wording, which the page labels as not independently reviewed.
 */
export function answerBody(turn: Turn): AnswerBody {
  const report = (turn.report ?? "").trim();
  if (report) return { text: report, kind: "report" };
  const draft = (turn.draft_report ?? "").trim();
  return draft ? { text: draft, kind: "draft" } : null;
}

export function draftNote(hasCheckedClaims: boolean): string {
  return hasCheckedClaims
    ? "部分完成：带编号的句子已对照原文核对，其余是模型根据资料作出的判断。"
    : "部分完成：这份报告是模型根据资料作出的分析，没有通过独立核验。";
}

function inputKey(calc: Calculation): string {
  const inputs = calc.inputs.map((input) => input.fact_id || `${input.value}|${input.period ?? ""}`).sort();
  return `${calc.formula_id}|${inputs.join(",")}`;
}

/** A later calculation over the same inputs and formula replaces an earlier one (e.g. corrected input order). */
export function currentCalculations(calculations: readonly Calculation[]): Calculation[] {
  const latest = new Map<string, Calculation>();
  for (const calc of calculations) {
    const key = inputKey(calc);
    latest.delete(key);
    latest.set(key, calc);
  }
  return [...latest.values()];
}

function mentioned(calc: Calculation, text: string): boolean {
  const digits = calc.value.replace(/^[-+]/, "");
  if (digits.replace(/\D/g, "").length < 3) return false;
  const index = text.indexOf(digits);
  if (index < 0) return false;
  // Not part of a longer number (e.g. "31.48" inside "131.485").
  const before = text[index - 1] ?? "";
  const after = text[index + digits.length] ?? "";
  return !/[\d.]/.test(before) && !/\d/.test(after);
}

/**
 * The key numbers set under the lead. With a written answer, only the calculations it actually
 * states, in the order it states them; otherwise the most recent distinct calculations.
 */
export function keyCalculations(calculations: readonly Calculation[], body: string | null, limit = 3): Calculation[] {
  const current = currentCalculations(calculations);
  if (!body) return current.slice(-limit);
  return current
    .filter((calc) => mentioned(calc, body))
    .sort((a, b) => body.indexOf(a.value.replace(/^[-+]/, "")) - body.indexOf(b.value.replace(/^[-+]/, "")))
    .slice(0, limit);
}

const DATE_PREFIX = /^(\d{4}-\d{2}-\d{2})\b/;

/** "2026-11-20 completed NASDAQ session" → "2026-11-20"; other period labels stay as written. */
export function shortPeriod(period: string): string {
  const match = DATE_PREFIX.exec(period.trim());
  return match ? match[1]! : period.trim();
}

const UNIT_ZH: Readonly<Record<string, string>> = {
  "USD per share": "美元/股",
  "per share": "/股",
  shares: "股",
  percent: "%",
  "percentage points": "个百分点",
  USD: "美元",
  CNY: "人民币",
  HKD: "港元",
  EUR: "欧元",
  JPY: "日元",
};

export function unitText(unit: string | null | undefined): string {
  if (!unit) return "";
  return UNIT_ZH[unit] ?? unit;
}

const LONG_FRACTION = /^([-+]?\d+)\.(\d{5,})$/;

/** Provider float noise such as 165.050003 is shown to two decimals; the exact value stays in the record. */
export function readableNumber(value: string): string {
  const match = LONG_FRACTION.exec(value);
  if (!match) return value;
  const rounded = Number(value).toFixed(2);
  return Number.isFinite(Number(rounded)) ? rounded : value;
}

/** A cell's text without the unit already named in its column header. */
export function cellText(display: string | null | undefined, unit: string | null | undefined): string {
  if (!display) return "—";
  let text = display.trim();
  if (unit) {
    for (const suffix of [unit, unitText(unit)]) {
      if (suffix && text.endsWith(` ${suffix}`)) { text = text.slice(0, -suffix.length - 1); break; }
    }
  }
  const space = text.indexOf(" ");
  const number = space < 0 ? text : text.slice(0, space);
  const rest = space < 0 ? "" : text.slice(space + 1).trim();
  const plain = number.replace(/,/g, "");
  const rounded = readableNumber(plain);
  const shown = rounded === plain ? number : rounded.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const suffix = unitText(rest);
  if (!suffix) return shown;
  return suffix === "%" ? `${shown}%` : `${shown} ${suffix}`;
}

/**
 * Columns to render: the first column is dropped when it only repeats the row labels, and the row
 * label column is dropped when no row has a label.
 */
export function tableLayout(table: Table): { rowLabels: boolean; columns: Table["columns"] } {
  const rowLabels = table.rows.some((row) => Boolean(row.label?.trim()));
  const [first, ...others] = table.columns;
  const repeats = rowLabels && first !== undefined && table.rows.every((row) =>
    (row.cells[first.key]?.display_value ?? "").trim() === (row.label ?? "").trim());
  return { rowLabels, columns: repeats ? others : table.columns };
}

export function columnHeader(column: Table["columns"][number]): string {
  const unit = unitText(column.unit);
  return unit ? `${column.label}（${unit}）` : column.label;
}

const CONCLUSION = /^\s*(?:\*\*|__)?\s*(?:核心判断|结论|总结|核心结论|一句话结论|Bottom line|Conclusion|Summary)\s*[:：]?/i;

/**
 * The sentence a returning reader needs: a paragraph the writer marked as the conclusion, else the
 * first prose paragraph. Markers, headings and emphasis are removed.
 */
export function conclusionText(body: string): string {
  const paragraphs = body.replace(/\r\n?/g, "\n").split(/\n\s*\n/).map((p) => p.trim())
    .filter((p) => p && !/^#{1,6}\s/.test(p) && !/^\[\[(?:table|chart):/.test(p) && !/^\s*([-*+]|\d+[.)])\s/.test(p) && !p.startsWith("|"));
  const chosen = paragraphs.slice(0, 6).find((p) => CONCLUSION.test(p)) ?? paragraphs[0] ?? "";
  return chosen
    .replace(/\s?\[\[(?:claim|table|chart):[A-Za-z0-9_-]+\]\]/g, "")
    .replace(/(\*\*|__)(.+?)\1/g, "$2")
    .replace(CONCLUSION, "")
    .replace(/\s+/g, " ")
    .trim();
}
