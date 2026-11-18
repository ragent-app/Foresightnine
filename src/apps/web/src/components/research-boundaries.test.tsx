import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { SourceProcessing } from "./SourceProcessing";
import { ResearchEvidenceLinks } from "./ResearchEvidenceLinks";

const source = { source_id: "s", title: "Document", retrieval_method: "user_pdf" } as any;

describe("recorded research boundaries", () => {
  it("reports actual mapping status without asserting complete PDF verification", () => {
    const html = renderToStaticMarkup(<SourceProcessing en={false} sources={[source]} value={[{source_id:"s", eligibility:"eligible", initial_selection:"partial", analysis_status:"produced"}]} pdfDiagnostics={[{source_id:"s", geometry_status:"limit_reached"}]} />);
    expect(html).toContain("几何分组达到上限");
    expect(html).toContain("其余结构未知");
    expect(html).toContain("阅读顺序核验状态未知");
  });
  it("shows unresolved assertions as suggestions, never verified support", () => {
    const value = {schema_version:"1", truncated:false, claims:[{claim_index:0, citations:[], semantic_support_status:"not_independently_verified", assertion_coverage:"model_suggested_not_verified", assertions:[{text:"A component", citation_indexes:[], calculation_indexes:[], link_status:"invalid_or_unlocated"}]}], calculations:[]};
    const html = renderToStaticMarkup(<ResearchEvidenceLinks en={false} value={value} claimIndex={0} facts={[]} calculationCount={0} />);
    expect(html).toContain("模型建议的结论分项");
    expect(html).toContain("关联无效或未定位");
    expect(html).toContain("未独立核验");
  });
});
