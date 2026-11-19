import { describe, expect, it } from "vitest";
import { DEFAULT_OPTIONS, EMPTY_BUDGET, runRequestFields } from "./options";

describe("runRequestFields", () => {
  it("turns web search on by default and always sends it as a boolean", () => {
    const fields = runRequestFields(DEFAULT_OPTIONS);
    expect(fields).toEqual({ discover_sources: true, auto_fetch_official: true });
    expect(runRequestFields({ ...DEFAULT_OPTIONS, discover: "off", autoFetch: "off" })).toEqual({ discover_sources: false, auto_fetch_official: false });
  });

  it("sends a source selection only when narrowed, including an explicit empty one", () => {
    expect("selected_source_ids" in runRequestFields(DEFAULT_OPTIONS)).toBe(false);
    expect(runRequestFields({ ...DEFAULT_OPTIONS, selectedSourceIds: ["s1"] }).selected_source_ids).toEqual(["s1"]);
    expect(runRequestFields({ ...DEFAULT_OPTIONS, selectedSourceIds: [] }).selected_source_ids).toEqual([]);
  });

  it("omits empty budget fields and keeps explicit zeros", () => {
    const fields = runRequestFields({
      ...DEFAULT_OPTIONS,
      budget: { ...EMPTY_BUDGET, modelCalls: "0", searchCalls: " ", minutes: "3", costUsd: "1.50", inputTokensPerCall: "8000" },
    });
    expect(fields.max_model_calls).toBe(0);
    expect("max_search_calls" in fields).toBe(false);
    expect(fields.max_elapsed_seconds).toBe(180);
    expect(fields.max_cost_usd).toBe("1.50");
    expect(fields.max_input_tokens_per_call).toBe(8000);
    expect("max_total_tokens" in fields).toBe(false);
  });
});
