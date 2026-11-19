import { describe, expect, it } from "vitest";
import {
  DATE_GROUP_LABEL, dateGroup, displayTurnNumber, formatClock, formatDateTime, formatDateTimeWithOffset, formatDuration,
  formatInteger, formatNumberString, formatQuantity, formatRelative, turnStamp, unitLabel,
} from "./format";

const p2 = (n: number): string => String(n).padStart(2, "0");
const local = (d: Date): string => `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}`;

describe("formatNumberString", () => {
  it.each([
    ["48612", "48,612"], ["-1234.500", "-1,234.500"], ["17.79", "17.79"], ["1e5", "1e5"], ["999", "999"],
    ["1000", "1,000"], ["+1234567", "+1,234,567"], ["1234567.1234567", "1,234,567.1234567"], ["", ""], ["abc", "abc"], ["1,234", "1,234"], [".5", ".5"],
    ["12345678901234567890.123456789", "12,345,678,901,234,567,890.123456789"],
  ])("%s -> %s", (input, expected) => expect(formatNumberString(input)).toBe(expected));
});

describe("formatInteger", () => {
  it("groups digits", () => {
    expect(formatInteger(0)).toBe("0");
    expect(formatInteger(1234567)).toBe("1,234,567");
    expect(formatInteger(-4200)).toBe("-4,200");
  });
});

describe("units", () => {
  it("labels units and currency", () => {
    expect(unitLabel("millions")).toBe("百万");
    expect(unitLabel("thousands")).toBe("千");
    expect(unitLabel("billions", "USD")).toBe("十亿 · USD");
    expect(unitLabel("percent")).toBe("");
    expect(unitLabel("percent", "USD")).toBe("USD");
    expect(unitLabel("shares")).toBe("shares");
    expect(unitLabel("shares", "")).toBe("shares");
    expect(unitLabel("millions", null)).toBe("百万");
  });
  it("formats quantities", () => {
    expect(formatQuantity("48612", "millions", "USD")).toEqual({ value: "48,612", unit: "百万 · USD" });
    expect(formatQuantity("17.79", "percent")).toEqual({ value: "17.79%", unit: "" });
    expect(formatQuantity("-1234.5", "percent", "USD")).toEqual({ value: "-1,234.5%", unit: "USD" });
  });
});

describe("date formatting", () => {
  const d = new Date(2026, 10, 5, 9, 7);
  it("formats local time", () => {
    expect(formatDateTime(d.toISOString())).toBe("2026-11-05 09:07");
    expect(formatDateTime("not a date")).toBe("");
    expect(formatDateTime("")).toBe("");
    expect(formatDateTime(null)).toBe("");
    expect(formatDateTime(undefined)).toBe("");
  });
  it("appends the UTC offset", () => {
    const off = -d.getTimezoneOffset();
    const abs = Math.abs(off);
    const zone = off === 0 ? "UTC" : `UTC${off > 0 ? "+" : "-"}${Math.floor(abs / 60)}${abs % 60 ? `:${p2(abs % 60)}` : ""}`;
    expect(formatDateTimeWithOffset(d.toISOString())).toBe(`2026-11-05 09:07 (${zone})`);
    expect(formatDateTimeWithOffset("bad")).toBe("");
  });
  it("formats relative time", () => {
    const now = new Date(2026, 10, 19, 12, 0, 0).getTime();
    const ago = (ms: number): string => new Date(now - ms).toISOString();
    expect(formatRelative(ago(30_000), now)).toBe("刚刚");
    expect(formatRelative(ago(60_000), now)).toBe("1 分钟前");
    expect(formatRelative(ago(59 * 60_000), now)).toBe("59 分钟前");
    expect(formatRelative(ago(3 * 3_600_000), now)).toBe("3 小时前");
    expect(formatRelative(ago(2 * 86_400_000), now)).toBe("2 天前");
    const old = new Date(now - 10 * 86_400_000);
    expect(formatRelative(old.toISOString(), now)).toBe(local(old).slice(0, 10));
    expect(formatRelative("bad", now)).toBe("");
  });
  it("formats durations", () => {
    expect(formatDuration(0)).toBe("0 秒");
    expect(formatDuration(45_000)).toBe("45 秒");
    expect(formatDuration(161_000)).toBe("2 分 41 秒");
    expect(formatDuration(182_000)).toBe("3 分 02 秒");
    expect(formatDuration(3_600_000 + 5 * 60_000 + 30_000)).toBe("1 小时 5 分");
    expect(formatDuration(-1)).toBe("");
    expect(formatDuration(Number.NaN)).toBe("");
  });
  it("formats clocks", () => {
    expect(formatClock(134_000)).toBe("2:14");
    expect(formatClock(5_000)).toBe("0:05");
    expect(formatClock(3_600_000 + 2 * 60_000 + 3_000)).toBe("1:02:03");
    expect(formatClock(Number.NaN)).toBe("");
  });
});

describe("turnStamp and groups", () => {
  const now = new Date(2026, 10, 19, 15, 30).getTime();
  const iso = (y: number, m: number, day: number, h: number, mi: number): string => new Date(y, m, day, h, mi).toISOString();
  it("describes same day, previous day, same year and older", () => {
    expect(turnStamp(2, iso(2026, 10, 19, 8, 5), now)).toBe("第 2 轮 · 今天 08:05");
    expect(turnStamp(1, iso(2026, 10, 18, 23, 59), now)).toBe("第 1 轮 · 昨天 23:59");
    expect(turnStamp(3, iso(2026, 2, 4, 9, 0), now)).toBe("第 3 轮 · 03-04 09:00");
    expect(turnStamp(4, iso(2025, 11, 31, 22, 15), now)).toBe("第 4 轮 · 2025-12-31 22:15");
  });
  it("converts the backend index (1-based) to a display number", () => {
    expect(displayTurnNumber({ index: 1 })).toBe(1);
    expect(displayTurnNumber({ index: 5 })).toBe(5);
    expect(displayTurnNumber({ index: 0 })).toBe(1);
  });
  it("groups dates", () => {
    expect(dateGroup(iso(2026, 10, 19, 0, 1), now)).toBe("today");
    expect(dateGroup(iso(2026, 10, 18, 23, 0), now)).toBe("week");
    expect(dateGroup(iso(2026, 10, 13, 10, 0), now)).toBe("week");
    expect(dateGroup(iso(2026, 10, 12, 10, 0), now)).toBe("earlier");
    expect(dateGroup("bad", now)).toBe("earlier");
    expect(DATE_GROUP_LABEL).toEqual({ today: "今天", week: "本周", earlier: "更早" });
  });
});
