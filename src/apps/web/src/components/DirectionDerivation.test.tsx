import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { DirectionDerivation } from "./DirectionDerivation";
import type { ResearchFact } from "../general-research-client";

const fact: ResearchFact = {
  fact_id: "f", label: "Output change", value: "-0.6", unit: "percent", period: "Q2 2022",
  source_id: "source-a", quotation: "Output decreased 0.6 percent.", verification: "unverified",
  direction_derivation: { method: "source_direction_percent_v1", source_id: "source-a", direction: "decrease",
    subject: "Output", verification: "textual_conversion_not_independent_fact_verification" },
};

describe("direction derivation", () => {
  it("shows the original expression, converted value, source and verification limit", () => {
    const text = renderToStaticMarkup(<DirectionDerivation fact={fact} en={false} />);
    for (const expected of ["Output decreased 0.6 percent.", "source-a", "-0.6", "percent", "Q2 2022", "规则解释", "未独立核验"]) expect(text).toContain(expected);
  });
  it("does not invent a proof for old facts or a mismatched source", () => {
    const { direction_derivation: _oldProof, ...oldFact } = fact;
    expect(renderToStaticMarkup(<DirectionDerivation fact={oldFact} en />)).toBe("");
    expect(renderToStaticMarkup(<DirectionDerivation fact={{ ...fact, source_id: "source-b" }} en />)).toBe("");
  });
});
