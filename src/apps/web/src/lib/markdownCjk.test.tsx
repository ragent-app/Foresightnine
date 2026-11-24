import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MarkdownContent } from "../ui/MarkdownContent";

const html = (content: string, variant: "report" | "draft" | "inline" = "report") =>
  render(<MarkdownContent content={content} variant={variant} />).container.innerHTML;

describe("bold next to Chinese punctuation", () => {
  it("is bold whether or not a space follows the closing marker", () => {
    for (const md of ["**时点：**研究时钟显示", "**时点：** 研究时钟显示", "这是**“引号”**测试", "上涨 **31.48%**；盘中"]) {
      const out = html(md);
      expect(out).toContain("<strong>");
      expect(out).not.toContain("**");
    }
    expect(html("**时点：**研究时钟", "draft")).toContain("<strong>时点：</strong>研究时钟");
  });

  it("leaves code, lone markers and claim markers alone", () => {
    expect(html("`**时点：**研究`")).toContain("<code>**时点：**研究</code>");
    expect(html("2**3 等于 8")).toContain("2**3 等于 8");
    expect(html("**结论：**上涨 [[claim:c1]]")).toContain("<strong>结论：</strong>");
  });
});
