import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { SourceProcessing } from "./SourceProcessing";

it("distinguishes unverified PDF reading order from unknown historical metadata", () => {
  const value = [{ source_id: "s", eligibility: "eligible", initial_selection: "complete" }];
  const sources = [{ source_id: "s", title: "Report", status: "available", retrieval_method: "user_pdf" }];
  const render = (segments: unknown) => renderToStaticMarkup(<SourceProcessing value={value} sources={sources} segments={segments} en={false} />);
  expect(render([{ source_id: "s", reading_order_status: "not_independently_verified" }])).toContain("PDF 阅读顺序尚未与原件独立核验");
  expect(render([{ source_id: "s" }])).toContain("PDF 阅读顺序核验状态未知");
  const unknown = renderToStaticMarkup(<SourceProcessing value={value} sources={sources} en={false}
    runSelection={[{ source_id: "s", coverage: "unknown", stage_ranges: [] }]} />);
  expect(unknown).toContain("累计范围未知");
});
