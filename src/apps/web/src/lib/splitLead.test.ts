import { describe, expect, it } from "vitest";
import { splitLead } from "../pages/topic/Conversation";

describe("splitLead", () => {
  it("keeps the opening paragraph as the lead when nothing is marked", () => {
    expect(splitLead("## T\n\n第一段。\n\n第二段。")).toEqual({ lead: "## T\n\n第一段。", rest: "第二段。" });
  });

  it("puts a marked conclusion first and keeps everything else in order", () => {
    const report = "## T\n\n**时间：** 165.05。\n\n**结论：** CEO 复职是直接催化剂。\n\n- 要点\n\n```text\na\n\n\nb\n```";
    const { lead, rest } = splitLead(report);
    expect(lead).toBe("## T\n\n**结论：** CEO 复职是直接催化剂。");
    expect(rest).toBe("**时间：** 165.05。\n\n\n- 要点\n\n```text\na\n\n\nb\n```");
  });

  it("has no lead when the answer opens with a list", () => {
    expect(splitLead("- a\n- b")).toEqual({ lead: "", rest: "- a\n- b" });
  });
});
