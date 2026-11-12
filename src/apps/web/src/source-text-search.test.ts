import { describe, expect, it } from "vitest";
import { searchSourceText } from "./source-text-search";

describe("source text search", () => {
  it("treats metacharacters literally and preserves exact original text around Unicode", () => {
    const text = "收益😀 10.2 and 10x2; INCOME (net) 10.2";
    const matches = searchSourceText(text, "10.2");
    expect(matches.items).toHaveLength(2);
    for (const match of matches.items) expect(text.slice(match.start, match.end)).toBe("10.2");
    const name = searchSourceText(text, "income (NET)").items[0];
    expect(text.slice(name!.start, name!.end)).toBe("INCOME (net)");
    expect(searchSourceText(text, ".*").items).toHaveLength(0);
  });
  it("bounds results and requires a useful query", () => {
    expect(searchSourceText("aa ".repeat(21), "aa")).toMatchObject({ truncated: true, items: expect.arrayContaining([expect.any(Object)]) });
    expect(searchSourceText("abc", "a").items).toHaveLength(0);
    expect(searchSourceText("abc", "a".repeat(121)).items).toHaveLength(0);
  });
});

describe("citation source cutoff", () => {
  it("requires display permission and dates available at the research cutoff", async () => {
    const { canUseCitationSource } = await import("./source-text-search");
    const source = { source_id: "s", status: "available", permissions: { display: true, export: false },
      published_at: "2024-01-01T00:00:00Z" };
    expect(canUseCitationSource(source, "2024-01-02T00:00:00Z")).toBe(true);
    expect(canUseCitationSource(source, "2023-12-31T23:59:59Z")).toBe(false);
    expect(canUseCitationSource({ ...source, published_at: null }, "2024-01-02T00:00:00Z")).toBe(false);
    expect(canUseCitationSource({ ...source, revised_at: "2024-01-03T00:00:00Z" }, "2024-01-02T00:00:00Z")).toBe(false);
    expect(canUseCitationSource({ ...source, revised_at: "invalid" }, "2024-01-02T00:00:00Z")).toBe(false);
    expect(canUseCitationSource({ ...source, permissions: { display: false, export: true } }, null)).toBe(false);
    expect(canUseCitationSource({ ...source, is_simulated: true }, null)).toBe(false);
    expect(canUseCitationSource({ ...source, published_at: null }, null)).toBe(true);
  });
});
