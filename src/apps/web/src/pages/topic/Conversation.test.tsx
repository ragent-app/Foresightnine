import { render, screen } from "@testing-library/react";
import type { ResearchV2 } from "@foresightnine/contracts";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { GapCallout, usageMeta } from "./Conversation";

const NOW = "2026-11-20T00:00:00Z";

function turn(overrides: Partial<ResearchV2.ResearchTurn> = {}): ResearchV2.ResearchTurn {
  return {
    turn_id: "turn_1", index: 1, question: "问题", question_origin: "user", status: "completed", reason: "submitted",
    started_at: NOW, finished_at: NOW, parent_version_id: "v1", operation_ids: ["op_1"],
    official_fetch_status: "not_requested", search_status: "not_requested", usage: { model_calls: 2, search_calls: 0, total_tokens: 1200 },
    report: "", claim_ids: [], fact_ids: [], calculation_ids: [], gaps: [], events: [], ...overrides,
  };
}

describe("market data key hint", () => {
  it("links to the data settings when the personal market key is missing", () => {
    const gaps = [{ code: "market_data_key_required", message: "x" }] as ResearchV2.ResearchTurn["gaps"];
    render(<MemoryRouter><GapCallout turn={turn({ gaps: gaps! })} actions={null} tone="completed" /></MemoryRouter>);
    const link = screen.getByRole("link", { name: /Twelve Data key/ });
    expect(link.getAttribute("href")).toBe("/settings?tab=data");
  });

  it("shows no hint for other gaps", () => {
    const gaps = [{ code: "no_cited_claims", message: "x" }] as ResearchV2.ResearchTurn["gaps"];
    render(<MemoryRouter><GapCallout turn={turn({ gaps: gaps! })} actions={null} tone="completed" /></MemoryRouter>);
    expect(screen.queryByRole("link")).toBeNull();
  });
});

describe("usage meta", () => {
  it("marks an estimate when some call usage is unknown", () => {
    const usage = { model_calls: 3, search_calls: 0, total_tokens: 755987, usage_status: "partial_unknown" } as const;
    expect(usageMeta(turn({ usage }))).toContain("约 755,987 Token（部分调用用量未知，已按预留额度计入）");
  });

  it("shows the plain total when complete", () => {
    expect(usageMeta(turn())).toContain("1,200 Token");
    expect(usageMeta(turn())).not.toContain("约");
  });
});
