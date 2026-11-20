import { describe, expect, it } from "vitest";
import { hasMarkdownSyntax, repairIncompleteMarkdown, splitStableMarkdownBlocks } from "./markdownStream";

describe("hasMarkdownSyntax", () => {
  it("recognizes GFM Markdown and chart anchors while leaving plain prose and single $ currency untouched", () => {
    expect(hasMarkdownSyntax("普通财务说明：营收从 $42.0B 增长至 $130.5B。")).toBe(false);
    expect(hasMarkdownSyntax("调用 sec_company_concept 获取收入序列")).toBe(false);
    expect(hasMarkdownSyntax("## 核心结论")).toBe(true);
    expect(hasMarkdownSyntax("包含 **重点** 描述")).toBe(true);
    expect(hasMarkdownSyntax("| 指标 | FY2026 |\n| --- | ---: |")).toBe(true);
    expect(hasMarkdownSyntax("参考 [SEC 公告](https://www.sec.gov)")).toBe(true);
    expect(hasMarkdownSyntax("[[chart:c_rev_1]]")).toBe(true);
  });
});

describe("splitStableMarkdownBlocks", () => {
  it("splits at top-level blank lines and keeps fenced code blocks and tables unbroken", () => {
    const first = "## 收入分析\n\n第一段已完成。";
    const split1 = splitStableMarkdownBlocks(`${first}\n\n第二段正在流式`);
    expect(split1.stableBlocks).toEqual(["## 收入分析", "第一段已完成。"]);
    expect(split1.activeTail).toBe("第二段正在流式");

    // Blank lines inside an unclosed fenced code block must NOT freeze or split prematurely.
    const openFence = "```python\na = 1\n\nb = 2";
    const splitFence = splitStableMarkdownBlocks(`前置段落。\n\n${openFence}`);
    expect(splitFence.stableBlocks).toEqual(["前置段落。"]);
    expect(splitFence.activeTail).toBe(openFence);

    // Closed fenced block followed by blank line moves cleanly into stableBlocks.
    const closedFence = `${openFence}\n\`\`\`\n\n后续段落`;
    const splitClosed = splitStableMarkdownBlocks(closedFence);
    expect(splitClosed.stableBlocks).toEqual([`${openFence}\n\`\`\``]);
    expect(splitClosed.activeTail).toBe("后续段落");

    // Multi-row GFM table stays whole until blank line.
    const table = "| 指标 | FY2025 | FY2026 |\n| --- | ---: | ---: |\n| 总收入 | $41.27B | $48.61B |";
    const splitTable = splitStableMarkdownBlocks(`概览\n\n${table}`);
    expect(splitTable.stableBlocks).toEqual(["概览"]);
    expect(splitTable.activeTail).toBe(table);
  });
});

describe("repairIncompleteMarkdown", () => {
  it("closes unclosed fences, inline code, bold, strike, emphasis, links, images, and table headers without touching identifiers or $", () => {
    expect(repairIncompleteMarkdown("```ts\nconst x = 1;")).toBe("```ts\nconst x = 1;\n```");
    expect(repairIncompleteMarkdown("使用 `calculate_financials")).toBe("使用 `calculate_financials`");
    expect(repairIncompleteMarkdown("核心指标：**营业利润")).toBe("核心指标：**营业利润**");
    // Trailing empty `**` must NOT turn into `****` horizontal rule.
    expect(repairIncompleteMarkdown("收入 **")).toBe("收入 ");
    expect(repairIncompleteMarkdown("增速 ~~明显")).toBe("增速 ~~明显~~");
    expect(repairIncompleteMarkdown("* 列表：**重点")).toBe("* 列表：**重点**");
    expect(repairIncompleteMarkdown("调用 sec_company_concept 与 a * b 乘法")).toBe("调用 sec_company_concept 与 a * b 乘法");
    expect(repairIncompleteMarkdown("营收为 $130.5B，去年同期 $42.0B。")).toBe("营收为 $130.5B，去年同期 $42.0B。");
    expect(repairIncompleteMarkdown("详见 [SEC 10-K](https://www.sec.gov/arch")).toBe("详见 SEC 10-K");
    expect(repairIncompleteMarkdown("附图 ![图表](https://evil.com/img")).toBe("附图 [图片：图表]");
    expect(repairIncompleteMarkdown("指标 [[chart:c1]] 已就绪")).toBe("指标 [[chart:c1]] 已就绪");
    expect(repairIncompleteMarkdown("| 指标 | FY2025 | FY2026 |")).toBe(
      "| 指标 | FY2025 | FY2026 |\n| --- | --- | --- |",
    );
  });
});
