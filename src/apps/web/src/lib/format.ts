const PLAIN_NUMBER = /^([-+]?)(\d+)(\.\d+)?$/;

/** Backend numbers are strings; they are never parsed to floats. Only the integer part is grouped. */
export function formatNumberString(value: string): string {
  const match = PLAIN_NUMBER.exec(value);
  if (!match) return value;
  const [, sign = "", integer = "", fraction = ""] = match;
  return `${sign}${integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}${fraction}`;
}

export function formatInteger(n: number): string {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(n);
}

const UNIT_LABEL: Readonly<Record<string, string>> = { millions: "百万", thousands: "千", billions: "十亿", percent: "" };

export function unitLabel(unit: string, currency?: string | null): string {
  const label = UNIT_LABEL[unit] ?? unit;
  if (currency) return label ? `${label} · ${currency}` : currency;
  return label;
}

export function formatQuantity(value: string, unit: string, currency?: string | null): { value: string; unit: string } {
  const formatted = formatNumberString(value);
  if (unit === "percent") return { value: `${formatted}%`, unit: unitLabel("percent", currency) };
  return { value: formatted, unit: unitLabel(unit, currency) };
}

const pad = (n: number): string => String(n).padStart(2, "0");
const parse = (iso: string | null | undefined): Date | null => {
  if (!iso) return null;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
};
const dayStamp = (date: Date): string => `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
const clockStamp = (date: Date): string => `${pad(date.getHours())}:${pad(date.getMinutes())}`;

export function formatDateTime(iso: string | null | undefined): string {
  const date = parse(iso);
  return date ? `${dayStamp(date)} ${clockStamp(date)}` : "";
}

export function formatDateTimeWithOffset(iso: string | null | undefined): string {
  const date = parse(iso);
  if (!date) return "";
  const offset = -date.getTimezoneOffset();
  let zone = "UTC";
  if (offset !== 0) {
    const abs = Math.abs(offset);
    const hours = Math.floor(abs / 60);
    const minutes = abs % 60;
    zone = `UTC${offset > 0 ? "+" : "-"}${hours}${minutes ? `:${pad(minutes)}` : ""}`;
  }
  return `${formatDateTime(iso)} (${zone})`;
}

export function formatRelative(iso: string | null | undefined, now: number = Date.now()): string {
  const date = parse(iso);
  if (!date) return "";
  const seconds = Math.floor((now - date.getTime()) / 1000);
  if (seconds < 60) return "刚刚";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days} 天前`;
  return dayStamp(date);
}

export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "";
  const total = Math.floor(ms / 1000);
  if (total < 60) return `${total} 秒`;
  if (total < 3600) return `${Math.floor(total / 60)} 分 ${pad(total % 60)} 秒`;
  return `${Math.floor(total / 3600)} 小时 ${Math.floor((total % 3600) / 60)} 分`;
}

export function formatClock(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "";
  const total = Math.floor(ms / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${minutes}:${pad(seconds)}`;
}

const startOfDay = (date: Date): number => new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
/** Whole local calendar days between `date` and `now` (0 = same local day). */
function dayDifference(date: Date, now: number): number {
  const a = startOfDay(new Date(now));
  const b = startOfDay(date);
  return Math.round((a - b) / 86_400_000);
}

export function turnStamp(index: number, iso: string, now: number = Date.now()): string {
  const prefix = `第 ${index} 轮 · `;
  const date = parse(iso);
  if (!date) return prefix.trimEnd();
  const diff = dayDifference(date, now);
  if (diff === 0) return `${prefix}今天 ${clockStamp(date)}`;
  if (diff === 1) return `${prefix}昨天 ${clockStamp(date)}`;
  if (date.getFullYear() === new Date(now).getFullYear()) return `${prefix}${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${clockStamp(date)}`;
  return `${prefix}${formatDateTime(iso)}`;
}

/**
 * Backend turn `index` is 1-based: apps/api/app/main.py sets
 * `'index': len(parent_version.content.turns) + 1`. A value below 1 (the progress
 * endpoint reports 0 when unknown) is shifted up so the display number is never 0.
 */
export function displayTurnNumber(turn: { readonly index: number }): number {
  return turn.index >= 1 ? turn.index : turn.index + 1;
}

export type DateGroup = "today" | "week" | "earlier";

export function dateGroup(iso: string | null | undefined, now: number = Date.now()): DateGroup {
  const date = parse(iso);
  if (!date) return "earlier";
  const diff = dayDifference(date, now);
  if (diff <= 0) return "today";
  if (diff <= 6) return "week";
  return "earlier";
}

export const DATE_GROUP_LABEL: Readonly<Record<DateGroup, string>> = { today: "今天", week: "本周", earlier: "更早" };
