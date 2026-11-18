import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { RequirementCoverage } from "./RequirementCoverage";

it("shows traceable suggestions and gaps without claiming semantic verification", () => {
  const value = { requirements: [{ requirement_id: "question:0", dimensions: { metrics: ["growth"] }, source_ids: ["s"], candidate_fact_ids: ["f"], candidate_calculation_indexes: [0], gaps: ["no_recorded_finding"] }] };
  const text = renderToStaticMarkup(<RequirementCoverage value={value} en={false} />);
  for (const expected of ["growth", "事实关联", "未关联结论", "语义支持尚未独立核验"]) expect(text).toContain(expected);
  expect(renderToStaticMarkup(<RequirementCoverage value={null} en />)).toBe("");
});
