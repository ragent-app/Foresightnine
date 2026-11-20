import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { TransientStreamState } from "../../api/types";
import type { LiveStreamStore } from "./context";
import { LiveActiveStream, LiveDraftPreview } from "./LiveStreamView";

function createTestStreamStore(initial: Partial<TransientStreamState> = {}): {
  store: LiveStreamStore;
  update: (patch: Partial<TransientStreamState>) => void;
} {
  let state: TransientStreamState = {
    callSeq: null,
    thinking: "",
    text: "",
    toolStatus: null,
    draftReport: "",
    activeChannel: null,
    retractedNotice: null,
    lastSeq: -1,
    ...initial,
  };
  const listeners = new Set<() => void>();
  return {
    store: {
      getSnapshot: () => state,
      subscribe: (listener) => {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
    },
    update: (patch) => {
      state = { ...state, ...patch };
      for (const listener of listeners) listener();
    },
  };
}

describe("LiveStreamView", () => {
  it("shows live thinking expanded by default, toggles manually, and re-expands on new callSeq", () => {
    const { store, update } = createTestStreamStore({
      callSeq: 1,
      thinking: "第一步分析营收构成。",
      activeChannel: "thinking",
      lastSeq: 0,
    });
    render(<LiveActiveStream stream={store} />);

    expect(screen.getByText("模型思考过程 · 非正式结论")).toBeTruthy();
    expect(screen.getByText("第一步分析营收构成。")).toBeTruthy();
    const toggle = screen.getByRole("button", { name: "收起模型思考过程" });
    expect(toggle.getAttribute("aria-expanded")).toBe("true");

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("第一步分析营收构成。")).toBeNull();

    // More tokens on the same callSeq stay collapsed.
    act(() => {
      update({ thinking: "第一步分析营收构成。第二步核对现金流。", lastSeq: 1 });
    });
    expect(screen.queryByText(/第二步核对现金流/)).toBeNull();

    // Reset clears, and a new callSeq expands by default again.
    act(() => {
      update({ thinking: "", activeChannel: null });
    });
    expect(screen.queryByText("模型思考过程 · 非正式结论")).toBeNull();

    act(() => {
      update({ callSeq: 2, thinking: "新一步思考。", activeChannel: "thinking", lastSeq: 0 });
    });
    expect(screen.getByText("新一步思考。")).toBeTruthy();
    expect(screen.getByRole("button", { name: "收起模型思考过程" }).getAttribute("aria-expanded")).toBe("true");
  });

  it("renders process text, tool status with draft char count, and retraction notice", () => {
    const { store, update } = createTestStreamStore({
      callSeq: 1,
      text: "先读取年报附注说明。",
      toolStatus: "正在起草研究报告并整理引文，完成后将自动核验原文位置…",
      draftReport: "草稿第一段",
      activeChannel: "draft_report",
    });
    render(<LiveActiveStream stream={store} />);

    expect(screen.getByText("思路 · 过程说明（非正式结论）")).toBeTruthy();
    expect(screen.getByText("先读取年报附注说明。")).toBeTruthy();
    expect(screen.getByText("正在起草研究报告并整理引文，完成后将自动核验原文位置…（已生成约 5 字）")).toBeTruthy();

    act(() => {
      update({
        text: "",
        toolStatus: null,
        draftReport: "",
        activeChannel: null,
        retractedNotice: "草稿引文或格式未通过核验，模型正在修正…",
      });
    });
    expect(screen.getByText("草稿引文或格式未通过核验，模型正在修正…")).toBeTruthy();
    expect(screen.queryByText("先读取年报附注说明。")).toBeNull();
  });

  it("renders dashed unverified draft preview, triggers onGrow per paragraph, and retracts immediately on reset", () => {
    const onGrow = vi.fn();
    const { store, update } = createTestStreamStore({
      callSeq: 1,
      draftReport: "第一段未核验草稿。\n\n第二段未核验草稿。",
      activeChannel: "draft_report",
    });
    const { container } = render(<LiveDraftPreview stream={store} onGrow={onGrow} />);

    const box = screen.getByRole("region", { name: "未核验报告草稿预览" });
    expect(box.className).toContain("draft-preview");
    expect(screen.getByText("草稿生成中 · 引文与数值尚未经服务端核验，最终结果以核验后为准")).toBeTruthy();
    expect(screen.getByText("第一段未核验草稿。")).toBeTruthy();
    expect(screen.getByText("第二段未核验草稿。")).toBeTruthy();
    expect(container.querySelector(".stream-cursor")).toBeTruthy();
    expect(onGrow).toHaveBeenCalledTimes(1);

    // Appending within the same paragraph does not refire onGrow; starting a 3rd paragraph does.
    act(() => {
      update({ draftReport: "第一段未核验草稿。\n\n第二段未核验草稿。补充。" });
    });
    expect(onGrow).toHaveBeenCalledTimes(1);
    act(() => {
      update({ draftReport: "第一段未核验草稿。\n\n第二段未核验草稿。补充。\n\n第三段。" });
    });
    expect(onGrow).toHaveBeenCalledTimes(2);

    // Retraction unmounts preview immediately.
    act(() => {
      update({ draftReport: "", activeChannel: null, retractedNotice: "草稿引文或格式未通过核验，模型正在修正…" });
    });
    expect(screen.queryByRole("region", { name: "未核验报告草稿预览" })).toBeNull();
  });

  it("renders untrusted HTML payloads inside thinking, text, toolStatus, and draftReport strictly as plain text", () => {
    const { store } = createTestStreamStore({
      callSeq: 1,
      thinking: "<script>alert(1)</script>",
      text: "<img src=x onerror=alert(2)>",
      toolStatus: "<svg onload=alert(3)>",
      draftReport: "<b>加粗不生效</b>\n\n<a href=javascript:alert(4)>链接不生效</a>",
      activeChannel: "draft_report",
    });
    const { container } = render(
      <>
        <LiveActiveStream stream={store} />
        <LiveDraftPreview stream={store} />
      </>,
    );

    expect(screen.getByText("<script>alert(1)</script>")).toBeTruthy();
    expect(screen.getByText("<img src=x onerror=alert(2)>")).toBeTruthy();
    expect(screen.getByText("<svg onload=alert(3)>（已生成约 51 字）")).toBeTruthy();
    expect(screen.getByText("<b>加粗不生效</b>")).toBeTruthy();
    expect(screen.getByText("<a href=javascript:alert(4)>链接不生效</a>")).toBeTruthy();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("svg[onload]")).toBeNull();
    expect(container.querySelector("a")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
  });

  it("renders Markdown inside draftReport safely while stripping HTML/images and neutralizing links", () => {
    const { store } = createTestStreamStore({
      callSeq: 1,
      text: "步骤说明：**正在比对** ![x](https://evil.com/a.png)",
      draftReport: "## 草稿小节\n\n安全正文：<script>alert(1)</script> 营收 **$130.5B**。\n\n![追踪](https://evil.com/p.png) [点击](javascript:alert(1))",
      activeChannel: "draft_report",
    });
    const { container } = render(
      <>
        <LiveActiveStream stream={store} />
        <LiveDraftPreview stream={store} />
      </>,
    );

    expect(screen.getByRole("heading", { level: 2, name: "草稿小节" })).toBeTruthy();
    expect(screen.getByText("$130.5B")).toBeTruthy();
    expect(screen.getByText("[图片：追踪]")).toBeTruthy();
    expect(screen.getByText("正在比对")).toBeTruthy();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("a")).toBeNull();
  });
});
