import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MarkdownContent } from "./MarkdownContent";

describe("MarkdownContent", () => {
  it("reopens persisted table markers once, ignores missing IDs and preserves code literals", () => {
    const tables = new Map([["t1", <table><tbody><tr><td>100 USD</td></tr></tbody></table>]]);
    const { container } = render(<MarkdownContent variant="report" tablesById={tables} content={"Result\n\n[[table:t1]]\n\n[[table:t1]]\n\n[[table:missing]]\n\n```text\n[[table:t1]]\n```"} />);
    expect(screen.getAllByText("100 USD")).toHaveLength(1);
    expect(screen.queryByText("[[table:missing]]")).toBeNull();
    expect(container.querySelector("pre")?.textContent).toContain("[[table:t1]]");
  });
  it("renders report headings demoted, GFM financial tables with right-aligned numbers, TSV copy, code copy, and safe https links", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });

    const md = [
      "# 一级标题降级",
      "",
      "### 三级子标题",
      "",
      "收入由 $42.0B 增至 $130.5B，增速 +114.2%。",
      "",
      "| 指标 | FY2025 | FY2026 | 同比 |",
      "| --- | --- | --- | --- |",
      "| 总收入 | $42.0B | $130.5B | +114.2% |",
      "",
      "```python",
      "growth = (130.5 / 42.0 - 1) * 100",
      "```",
      "",
      "来源见 [SEC 披露](https://www.sec.gov/Archives/edgar/data/1/a.htm)。",
    ].join("\n");

    const { container } = render(<MarkdownContent content={md} variant="report" />);

    expect(container.querySelector("h1")).toBeNull();
    expect(screen.getByRole("heading", { level: 2, name: "一级标题降级" })).toBeTruthy();
    expect(screen.getByRole("heading", { level: 3, name: "三级子标题" })).toBeTruthy();
    // Single $ currency amounts must NOT be mangled into math blocks.
    expect(screen.getByText("收入由 $42.0B 增至 $130.5B，增速 +114.2%。")).toBeTruthy();

    const numCells = container.querySelectorAll("td.md-num");
    expect(numCells.length).toBe(3);

    fireEvent.click(screen.getByRole("button", { name: "复制表格" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("指标\tFY2025\tFY2026\t同比\n总收入\t$42.0B\t$130.5B\t+114.2%"));

    fireEvent.click(screen.getByRole("button", { name: "复制" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("growth = (130.5 / 42.0 - 1) * 100"));

    const link = screen.getByRole("link", { name: /SEC 披露/ }) as HTMLAnchorElement;
    expect(link.href).toBe("https://www.sec.gov/Archives/edgar/data/1/a.htm");
    expect(link.target).toBe("_blank");
    expect(link.rel).toBe("noopener noreferrer");
  });

  it("strips raw HTML, blocks <img> across all variants, and neutralizes unsafe links", () => {
    const malicious = [
      "## 安全检查",
      "",
      "<script>window.__pwned=1</script><svg onload=alert(1)>正文",
      "",
      "![追踪像素](https://evil.example.com/pixel.png)",
      "",
      "[XSS](javascript:alert(1)) / [Data](data:text/html,hi) / [HTTP](http://sec.gov) / [Meta](https://169.254.169.254/latest)",
    ].join("\n");

    const { container } = render(<MarkdownContent content={malicious} variant="report" />);
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("svg[onload]")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelectorAll("a")).toHaveLength(0);
    expect(screen.getByText("[图片：追踪像素]")).toBeTruthy();
    expect(screen.getByText("XSS")).toBeTruthy();
  });

  it("downgrades links and omits copy controls in draft, restricts notes, and flattens inline", () => {
    const md = "## 小结\n\n参见 [官网](https://investor.example.com/q4)。\n\n| 列A | 列B |\n| --- | --- |\n| 1 | 2 |";
    const { container, rerender } = render(<MarkdownContent content={md} variant="draft" streaming showCursor />);
    expect(container.querySelector("a")).toBeNull();
    expect(screen.getByText("官网")).toBeTruthy();
    expect(screen.getByText("（investor.example.com）")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "复制表格" })).toBeNull();
    expect(container.querySelector(".stream-cursor")).toBeTruthy();

    rerender(<MarkdownContent content={md} variant="notes" />);
    expect(container.querySelector("table")).toBeNull();
    expect(container.querySelector("a")).toBeNull();

    rerender(<MarkdownContent content="**核心结论**：同比增长 `17.8%` ![icon](https://evil.com/x.png)" variant="inline" />);
    expect(container.querySelector("strong")?.textContent).toBe("核心结论");
    expect(container.querySelector("code")?.textContent).toBe("17.8%");
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("p")).toBeNull();
  });

  it("renders valid [[chart:...]] anchors, ignores unregistered anchors, and preserves literal anchors in code blocks", () => {
    const charts = new Map([["c1", { id: "c1", label: "图1：收入柱状图" }]]);
    const md = [
      "段落一。",
      "",
      "[[chart:c1]]",
      "",
      "[[chart:unknown_99]]",
      "",
      "```text",
      "[[chart:c1]]",
      "```",
    ].join("\n");

    const { container } = render(
      <MarkdownContent
        content={md}
        variant="report"
        chartsById={charts}
        renderChart={(c) => <div data-testid="chart-box">{c.label}</div>}
      />,
    );

    expect(screen.getByTestId("chart-box").textContent).toBe("图1：收入柱状图");
    expect(screen.queryByText("[[chart:unknown_99]]")).toBeNull();
    expect(container.querySelector("pre")?.textContent).toContain("[[chart:c1]]");
  });

  it("streams progressive character chunks safely without crashing or leaving broken markers", () => {
    const chunks = [
      "## 财务对比\n\n| 指标 | FY2026 |",
      "## 财务对比\n\n| 指标 | FY2026 |\n| --- | ---: |\n| 收入 | **$130.5",
      "## 财务对比\n\n| 指标 | FY2026 |\n| --- | ---: |\n| 收入 | **$130.5B** |",
    ];
    const { rerender, container } = render(<MarkdownContent content={chunks[0]!} variant="draft" streaming showCursor />);
    expect(container.querySelector("table")).toBeTruthy();

    rerender(<MarkdownContent content={chunks[1]!} variant="draft" streaming showCursor />);
    expect(container.querySelector("strong")?.textContent).toBe("$130.5");

    rerender(<MarkdownContent content={chunks[2]!} variant="draft" streaming showCursor />);
    expect(container.querySelector("strong")?.textContent).toBe("$130.5B");
  });

  it("keeps DOM identity of completed stableBlocks frozen while activeTail streams", () => {
    const stablePrefix = "## 第一节：总收入核验\n\n已完成段落一。";
    const { rerender, container } = render(
      <MarkdownContent content={`${stablePrefix}\n\n正在输出`} variant="draft" streaming showCursor />,
    );
    const frozenHeading = container.querySelector("h2");
    const frozenParagraph = container.querySelector("p");
    expect(frozenHeading?.textContent).toBe("第一节：总收入核验");
    expect(frozenParagraph?.textContent).toBe("已完成段落一。");

    rerender(<MarkdownContent content={`${stablePrefix}\n\n正在输出更多字符 **加粗`} variant="draft" streaming showCursor />);
    expect(container.querySelector("h2")).toBe(frozenHeading);
    expect(container.querySelector("p")).toBe(frozenParagraph);
    expect(container.querySelector("strong")?.textContent).toBe("加粗");
  });
});
