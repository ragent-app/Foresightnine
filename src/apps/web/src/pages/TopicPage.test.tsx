import type { ResearchV2 } from "@foresightnine/contracts";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { queryKeys } from "../api/queries";
import { jsonResponse, storeSession } from "../test/fixtures";
import * as researchLib from "../lib/research";
import { routerFuture, routes } from "../app/router";

const TEXT = "Year ended June 30, 2026: total revenue 48,612. Revenue growth was driven primarily by cloud services.";
const QUOTE = "Year ended June 30, 2026: total revenue 48,612.";
const START = TEXT.indexOf(QUOTE);
const END = START + QUOTE.length;
const NOW = new Date().toISOString();

function turn(index: number, overrides: Partial<ResearchV2.ResearchTurn> = {}): ResearchV2.ResearchTurn {
  return {
    turn_id: `turn_${index}`, index, question: `问题 ${index}`, question_origin: "user", status: "completed", reason: "submitted",
    started_at: NOW, finished_at: NOW, parent_version_id: `v${index}`, operation_ids: [`op_${index}`],
    official_fetch_status: "not_requested", search_status: "not_requested", usage: { model_calls: 2, search_calls: 0, total_tokens: 1200 },
    report: "", claim_ids: [], fact_ids: [], calculation_ids: [], gaps: [], events: [], ...overrides,
  };
}

function topicWith(turns: ResearchV2.ResearchTurn[], claims: ResearchV2.ResearchClaim[] = []): ResearchV2.ResearchTopic {
  return {
    topic_id: "t1", owner_id: "acc_1", created_at: NOW, updated_at: NOW,
    request: { question: "Northwind 示例公司的收入增长有多快？", source_ids: ["s1"] },
    versions: [
      { version_id: "v1", created_at: NOW, created_by: "user", review: "draft", content: {} },
      { version_id: "v2", created_at: NOW, created_by: "agent", review: "draft", content: { turns, claims, sources: [] } },
    ],
  };
}

function operation(id: string, status: ResearchV2.Operation["status"]): ResearchV2.OperationResponse {
  return {
    operation: {
      operation_id: id, status, created_at: NOW, updated_at: NOW, endpoint: "/v1/research/topics/t1/runs", cancel_token: "c".repeat(32),
      resume_token: "r".repeat(32), content_hash: "h", idempotency_key_hash: "k", last_event_id: "e", schema_version: "v1", source_refs: [],
    },
  };
}

type Handler = (url: URL, init: RequestInit) => Response | undefined;

function sseResponse(frames: string[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) {
        controller.enqueue(encoder.encode(frame));
      }
      controller.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": "text/event-stream; charset=utf-8" },
  });
}

function mockApi(handler: Handler) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input));
    const response = handler(url, init);
    if (response) return response;
    if (url.pathname === "/v2/research/topics") return jsonResponse([]);
    if (url.pathname.endsWith("/sources")) return jsonResponse([]);
    return jsonResponse({ error_code: "not_found", message: "nf" }, { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderTopic(client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  storeSession();
  const router = createMemoryRouter(routes, { future: routerFuture, initialEntries: ["/research/t1"] });
  return {
    client,
    ...render(
      <QueryClientProvider client={client}>
        <RouterProvider router={router} future={{ v7_startTransition: true }} />
      </QueryClientProvider>,
    ),
  };
}

/** Simulates the reader scrolling up, away from the live turn; returns the scrollTo spy. */
function scrollAway(container: HTMLElement) {
  const scroller = container.querySelector('[data-page="TopicPage"]') as HTMLDivElement;
  const scrollToSpy = vi.fn();
  scroller.scrollTo = scrollToSpy;
  Object.defineProperty(scroller, "scrollHeight", { value: 1200, configurable: true });
  Object.defineProperty(scroller, "clientHeight", { value: 400, configurable: true });
  Object.defineProperty(scroller, "scrollTop", { value: 100, configurable: true, writable: true });
  fireEvent.scroll(scroller);
  return scrollToSpy;
}

/** Version and review moved from the header into the toolbar's 更多操作 menu (spec v6). */
async function openReview() {
  await screen.findByRole("region", { name: "第 1 轮" });
  fireEvent.click(screen.getByRole("button", { name: "更多操作" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "复核与版本" }));
}

const CLAIMS: ResearchV2.ResearchClaim[] = [
  { claim_id: "C1", role: "support", text: "总收入为 48,612 百万美元。", turn_id: "turn_2", citations: [{ source_id: "s1", quotation: QUOTE, location: { character_start: START, character_end: END } }] },
  { claim_id: "C2", role: "counterargument", text: "利润率没有提高。", turn_id: "turn_2", challenges_claim_ids: ["C1"], citations: [{ source_id: "s1", quotation: QUOTE, location: { character_start: START, character_end: END } }] },
];

describe("TopicPage", () => {
  it("shows a partial turn with its gaps and offers to continue when recovery allows it", async () => {
    const partial = turn(1, { status: "partial", reason: "no_submission", gaps: [{ code: "no_cited_claims", message: "x" }] });
    const completed = turn(2, {
      claim_ids: ["C1", "C2"], report: "披露收入 [[claim:C1]]。另附标记示例 `[[claim:C2]]`。",
      question_parts: [{ id: "revenue", question: "本期收入是多少？", status: "answered",
        answer: "总收入为 48,612 百万美元。", reason: "年报披露", references: [{ claim_id: "C1" }] }],
    });
    mockApi((url) => {
      if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([partial, completed], CLAIMS));
      if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_2", "completed"), operation("op_1", "failed")]);
      return undefined;
    });
    renderTopic();
    expect(await screen.findByRole("heading", { level: 1, name: "Northwind 示例公司的收入增长有多快？" })).toBeTruthy();
    const turn2 = screen.getByRole("region", { name: "第 2 轮" });
    // A completed answer carries no status line; the first question is the page title, not a bubble.
    expect(within(turn2).queryByText("完成")).toBeNull();
    // The challenging claim stays visible; supporting claims sit in the collapsed evidence.
    expect(within(turn2).getByText("待复核")).toBeTruthy();
    expect(within(turn2).queryByRole("region", { name: "问题各部分的进展" })).toBeNull();
    fireEvent.click(within(turn2).getByRole("button", { name: "已回答 1/1 部分" }));
    expect(within(turn2).getByRole("region", { name: "问题各部分的进展" })).toBeTruthy();
    fireEvent.click(within(turn2).getByRole("button", { name: "依据 1 条" }));
    expect(within(turn2).getByText("支持")).toBeTruthy();
    const marker = within(turn2).getByRole("link", { name: "查看依据" });
    expect(marker.textContent).toBe("1");
    expect(marker.getAttribute("href")).toBe("#claim-C1");
    expect(document.getElementById("claim-C1")?.textContent).toContain("48,612");
    expect(within(turn2).getByText("[[claim:C2]]", { selector: "code" })).toBeTruthy();
    // The same quotation cited twice reuses one citation number.
    expect(within(turn2).getAllByRole("button", { name: "引用 1" })).toHaveLength(2);
    // The partial turn is not the last one, so it shows its gaps without actions.
    const turn1 = screen.getByRole("region", { name: "第 1 轮" });
    expect(within(turn1).getByText("部分完成")).toBeTruthy();
    expect(within(turn1).getByText("还没有引文位置已核实的结论。请添加或读取相关资料后继续研究。")).toBeTruthy();
    expect(within(turn1).queryByRole("button", { name: "继续研究" })).toBeNull();
  });

  it("continues the last partial turn through the recovery endpoint", async () => {
    const partial = turn(1, { status: "partial", reason: "no_submission", gaps: [{ code: "no_cited_claims", message: "x" }] });
    const fetchMock = mockApi((url, init) => {
      if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([partial]));
      if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "failed")]);
      if (url.pathname.endsWith("/op_1/recovery-status")) return jsonResponse({ can_continue: true, can_deliver: false, reason: null, checkpoint: "tool_batch", version_id: "v2" });
      if (url.pathname.endsWith("/op_1/continue") && init.method === "POST") return jsonResponse(operation("op_3", "pending"), { status: 202 });
      return undefined;
    });
    renderTopic();
    const button = await screen.findByRole("button", { name: "继续研究" });
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(button);
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([input]) => String(input).endsWith("/runs/op_1/continue"));
      expect(call).toBeTruthy();
      expect((call![1]!.headers as Record<string, string>)["Idempotency-Key"]).toMatch(/[0-9a-f-]{36}/);
    });
  });

  it("locks the composer, renders inline LiveTurn, and turns send into stop while a run is active", async () => {
    mockApi((url) => {
      if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([turn(1)]));
      if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_2", "running"), operation("op_1", "completed")]);
      if (url.pathname.endsWith("/op_2/events")) return jsonResponse({ error_code: "method_not_allowed", message: "fallback" }, { status: 405 });
      if (url.pathname.endsWith("/op_2/progress")) {
        return jsonResponse({
          operation_id: "op_2",
          operation_status: "running",
          turn: { index: 2, question: "第二个问题" },
          usage: { model_calls: 1, search_calls: 0, total_tokens: 900 },
          interim: { leads_count: 3, acquired_sources_count: 1, read_sources_count: 1, facts_count: 2, calculations_count: 0 },
          current: { kind: "tool", name: "read_sources", rationale: "读取年报附注", target: "《Northwind 年报》0–8,000 字" },
          events: [
            { index: 0, kind: "model", finish_reason: "tool_calls", usage: 900, input_tokens: 700, output_tokens: 200, settlement: null, note: "先检查年报附注中的收入明细。" },
            { index: 1, kind: "tool", name: "read_sources", status: "read", reason: null, rationale: "读取年报", denied_count: null },
          ],
          event_count: 2,
          started_at: NOW,
          updated_at: NOW,
          version_id: null,
        });
      }
      return undefined;
    });
    renderTopic();
    expect(await screen.findByText("第二个问题")).toBeTruthy();
    // Stopping lives in the composer; the run's state shows once, inside the live turn.
    expect(screen.getAllByRole("button", { name: "停止研究" })).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "取消" })).toBeNull();
    const liveRegion = screen.getByRole("region", { name: "第 2 轮" });
    expect(within(liveRegion).getByText(/读取年报附注/)).toBeTruthy();
    expect(within(liveRegion).getByText("正在研究")).toBeTruthy();
    expect(within(liveRegion).getByText(/发现 3 条线索 · 获取 1 份资料 · 记下 2 个数字/)).toBeTruthy();
    expect(within(liveRegion).getByText("先检查年报附注中的收入明细。")).toBeTruthy();
    expect(within(liveRegion).getByText("《Northwind 年报》0–8,000 字")).toBeTruthy();
    fireEvent.click(within(liveRegion).getByRole("button", { name: "收起" }));
    expect(within(liveRegion).queryByText("先检查年报附注中的收入明细。")).toBeNull();
    fireEvent.click(within(liveRegion).getByRole("button", { name: "展开" }));
    expect(within(liveRegion).getByText("先检查年报附注中的收入明细。")).toBeTruthy();
    const composer = screen.getByRole("textbox", { name: "追问" }) as HTMLTextAreaElement;
    expect(composer.disabled).toBe(true);
    expect(composer.placeholder).toBe("研究进行中，可以先停止再提问");
  });

  it("sends a follow-up with question_override and the deep-dive citation", async () => {
    const completed = turn(1, { claim_ids: ["C1"] });
    const fetchMock = mockApi((url, init) => {
      if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([completed], [CLAIMS[0]!]));
      if (url.pathname === "/v2/research/topics/t1/runs" && init.method === "POST") return jsonResponse(operation("op_9", "pending"), { status: 202 });
      if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
      return undefined;
    });
    renderTopic();
    // Phone layout in jsdom (no matchMedia): the citation opens a sheet with “深挖这条引用”.
    fireEvent.click(await screen.findByRole("button", { name: "引用 1" }));
    fireEvent.click(await screen.findByRole("button", { name: "深挖这条引用" }));
    expect(await screen.findByText("深挖引用 1")).toBeTruthy();
    const composer = screen.getByRole("textbox", { name: "追问" });
    fireEvent.change(composer, { target: { value: "这个数字的口径是什么？" } });
    fireEvent.keyDown(composer, { key: "Enter", ctrlKey: true });
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([input, init]) => String(input).endsWith("/v2/research/topics/t1/runs") && init?.method === "POST");
      expect(call).toBeTruthy();
      const body = JSON.parse(String(call![1]!.body)) as Record<string, unknown>;
      expect(body.parent_version_id).toBe("v2");
      expect(body.question_override).toBe("这个数字的口径是什么？");
      expect(body.focus_citation).toEqual({ claim_id: "C1", citation_index: 0 });
      expect(body.auto_fetch_official).toBe(true);
      expect(body.discover_sources).toBe(true);
      expect("selected_source_ids" in body).toBe(false);
      expect("max_model_calls" in body).toBe(false);
    });
  });

  it("shows an interrupted run that saved no turn", async () => {
    mockApi((url) => {
      if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([turn(1)]));
      if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_2", "failed"), operation("op_1", "completed")]);
      if (url.pathname.endsWith("/op_2/recovery-status")) return jsonResponse({ can_continue: false, can_deliver: false, reason: "unknown_external", checkpoint: "search", version_id: "v2" });
      if (url.pathname.endsWith("/op_2/events")) return jsonResponse({ error_code: "method_not_allowed", message: "fallback" }, { status: 405 });
      if (url.pathname.endsWith("/op_2/progress")) {
        return jsonResponse({ operation_id: "op_2", operation_status: "failed", turn: { index: 2, question: "失败的问题" }, usage: { model_calls: 1, search_calls: 1, total_tokens: 10 }, current: null, events: [], event_count: 0, started_at: NOW, updated_at: NOW, version_id: null });
      }
      return undefined;
    });
    renderTopic();
    const region = await screen.findByRole("region", { name: "第 2 轮" });
    expect(within(region).getByText("运行中断")).toBeTruthy();
    expect(await within(region).findByText("外部请求结果未知")).toBeTruthy();
    expect(await within(region).findByText("失败的问题")).toBeTruthy();
    expect(within(region).getByRole("button", { name: "重新提问" })).toBeTruthy();
  });

  it("adds a pasted text source and attaches it to the latest version", async () => {
    const created = {
      source_id: "src_new", title: "年报摘录", kind: "user_supplied", is_simulated: false, published_at: null, event_at: null, revised_at: null,
      obtained_at: NOW, content_sha256: "a".repeat(64), retrieval_method: "user_supplied", pdf_text_order: null, derived_from_source_id: null,
      permissions: { store: true, model_input: true, display: true, export: true }, verification: "user_declared_not_verified", text: "收入 100。",
    };
    const fetchMock = mockApi((url, init) => {
      if (url.pathname === "/v2/research/topics/t1/sources" && init.method === "POST") {
        const topic = topicWith([turn(1)]);
        return jsonResponse({ ...topic, versions: [...topic.versions, { version_id: "v3", created_at: NOW, created_by: "user", review: "draft", content: topic.versions[1]!.content, source_ids: ["src_new"] }] }, { status: 201 });
      }
      if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([turn(1)]));
      if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
      if (url.pathname === "/v2/research/sources" && init.method === "POST") return jsonResponse(created, { status: 201 });
      return undefined;
    });
    renderTopic();
    fireEvent.click(await screen.findByRole("button", { name: "添加资料" }));
    fireEvent.click(await screen.findByRole("tab", { name: "文本" }));
    fireEvent.change(screen.getByLabelText("标题"), { target: { value: "年报摘录" } });
    fireEvent.change(screen.getByLabelText("正文"), { target: { value: "收入 100。" } });
    expect(screen.getByText("下一轮研究会读取这份资料。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "添加" }));
    expect(await screen.findByText("已添加「年报摘录」。下一轮研究会读取它。")).toBeTruthy();
    const createCall = fetchMock.mock.calls.find(([input, init]) => String(input).endsWith("/v2/research/sources") && init?.method === "POST");
    expect(JSON.parse(String(createCall![1]!.body))).toEqual({
      title: "年报摘录", text: "收入 100。", permissions: { store: true, model_input: true, display: true, export: true },
    });
    const attachCall = fetchMock.mock.calls.find(([input]) => String(input).endsWith("/v2/research/topics/t1/sources"));
    expect(JSON.parse(String(attachCall![1]!.body))).toEqual({ parent_version_id: "v2", source_ids: ["src_new"] });
  });

  it("explains why a source will not be read before it is added", async () => {
    mockApi((url) => {
      if (url.pathname === "/v2/research/topics/t1") return jsonResponse({ ...topicWith([turn(1)]), request: { question: "Q", source_ids: [], as_of: "2026-06-30T00:00:00Z" } });
      if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
      return undefined;
    });
    renderTopic();
    fireEvent.click(await screen.findByRole("button", { name: "添加资料" }));
    expect(await screen.findByText("添加后下一轮不会读取：没有公开时间，无法确认在截止时间前已公开。")).toBeTruthy();
    fireEvent.click(screen.getByRole("checkbox", { name: /供模型读取/ }));
    expect(screen.getByText("添加后下一轮不会读取：没有允许供模型读取。")).toBeTruthy();
  });

  it("sends only the sources selected for the next turn", async () => {
    const source = (id: string, title: string) => ({
      status: "available", source_id: id, title, kind: "user_supplied", is_simulated: false, published_at: null, event_at: null, revised_at: null, obtained_at: NOW,
      content_sha256: "b".repeat(64), retrieval_method: "user_supplied", pdf_text_order: null, derived_from_source_id: null,
      permissions: { store: true, model_input: true, display: true, export: true }, verification: "user_declared_not_verified",
    });
    const fetchMock = mockApi((url, init) => {
      if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([turn(1)]));
      if (url.pathname === "/v2/research/topics/t1/runs" && init.method === "POST") return jsonResponse(operation("op_9", "pending"), { status: 202 });
      if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
      if (url.pathname.endsWith("/versions/v2/sources")) return jsonResponse([source("s1", "年报"), source("s2", "新闻稿")]);
      return undefined;
    });
    renderTopic();
    await screen.findByRole("region", { name: "第 1 轮" });
    fireEvent.click(screen.getByRole("button", { name: "研究设置" }));
    fireEvent.click(await screen.findByRole("checkbox", { name: /新闻稿/ }));
    fireEvent.click(screen.getByRole("button", { name: "应用" }));
    expect(await screen.findByText("本轮读取 1 份资料")).toBeTruthy();
    const composer = screen.getByRole("textbox", { name: "追问" });
    fireEvent.change(composer, { target: { value: "只看年报" } });
    fireEvent.keyDown(composer, { key: "Enter", ctrlKey: true });
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([input, init]) => String(input).endsWith("/v2/research/topics/t1/runs") && init?.method === "POST");
      expect(JSON.parse(String(call![1]!.body)).selected_source_ids).toEqual(["s1"]);
    });
  });

  describe("review and versions", () => {
    const withV3 = (base: ResearchV2.ResearchTopic): ResearchV2.ResearchTopic => ({
      ...base,
      versions: [...base.versions, { version_id: "v3", created_at: NOW, created_by: "user", review: "confirmed", content: base.versions[1]!.content }],
    });

    it("edits a claim in the record panel and saves it as a new version", async () => {
      const completed = turn(2, { turn_id: "turn_2", claim_ids: ["C1", "C2"] });
      const topic = topicWith([turn(1), completed], CLAIMS);
      const fetchMock = mockApi((url, init) => {
        if (url.pathname === "/v2/research/topics/t1/versions" && init.method === "POST") return jsonResponse(withV3(topic), { status: 201 });
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topic);
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_2", "completed")]);
        return undefined;
      });
      renderTopic();
      await screen.findByRole("region", { name: "第 1 轮" });
      fireEvent.click(screen.getByRole("button", { name: "研究记录" }));
      fireEvent.click((await screen.findAllByRole("button", { name: "编辑" }))[0]!);
      fireEvent.change(screen.getByLabelText("结论文字"), { target: { value: "总收入为 48,612 百万美元（2026 财年）。" } });
      fireEvent.click(screen.getByRole("button", { name: "完成" }));
      expect(screen.getByText("1 条修改还没有保存。")).toBeTruthy();
      expect(screen.getByText("有未保存的修改")).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "去保存" }));
      fireEvent.click(await screen.findByRole("button", { name: "已确认" }));
      fireEvent.click(screen.getByRole("button", { name: "保存为新版本" }));
      await waitFor(() => expect(screen.queryByText("有未保存的修改")).toBeNull());
      const call = fetchMock.mock.calls.find(([input, init]) => String(input).endsWith("/v2/research/topics/t1/versions") && init?.method === "POST");
      const body = JSON.parse(String(call![1]!.body)) as ResearchV2.VersionInput;
      expect(body.parent_version_id).toBe("v2");
      expect(body.review).toBe("confirmed");
      expect(body.content.claims![0]!.text).toBe("总收入为 48,612 百万美元（2026 财年）。");
      expect(body.content.claims![1]).toEqual(CLAIMS[1]);
      expect(body.content.turns).toEqual(topic.versions[1]!.content.turns);
    });

    it("asks before sending with unsaved review changes and saves first when chosen", async () => {
      const topic = topicWith([turn(1)]);
      const fetchMock = mockApi((url, init) => {
        if (url.pathname === "/v2/research/topics/t1/versions" && init.method === "POST") return jsonResponse(withV3(topic), { status: 201 });
        if (url.pathname === "/v2/research/topics/t1/runs" && init.method === "POST") return jsonResponse(operation("op_9", "pending"), { status: 202 });
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topic);
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
        return undefined;
      });
      renderTopic();
      await openReview();
      fireEvent.change(await screen.findByLabelText("你的研究总结"), { target: { value: "我的判断" } });
      fireEvent.click(screen.getByRole("button", { name: "关闭" }));
      const composer = screen.getByRole("textbox", { name: "追问" });
      fireEvent.change(composer, { target: { value: "下一步看什么？" } });
      fireEvent.keyDown(composer, { key: "Enter", ctrlKey: true });
      expect(await screen.findByText("发送新问题前，先保存复核修改吗？")).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "保存并发送" }));
      await waitFor(() => {
        const run = fetchMock.mock.calls.find(([input, init]) => String(input).endsWith("/v2/research/topics/t1/runs") && init?.method === "POST");
        expect(JSON.parse(String(run![1]!.body)).parent_version_id).toBe("v3");
      });
      const save = fetchMock.mock.calls.find(([input]) => String(input).endsWith("/v2/research/topics/t1/versions"));
      expect(JSON.parse(String(save![1]!.body)).content.summary).toBe("我的判断");
    });

    it("discards the draft and sends on the current version when chosen", async () => {
      const fetchMock = mockApi((url, init) => {
        if (url.pathname === "/v2/research/topics/t1/runs" && init.method === "POST") return jsonResponse(operation("op_9", "pending"), { status: 202 });
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([turn(1)]));
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
        return undefined;
      });
      renderTopic();
      await openReview();
      fireEvent.click(await screen.findByRole("button", { name: "已确认" }));
      fireEvent.click(screen.getByRole("button", { name: "关闭" }));
      const composer = screen.getByRole("textbox", { name: "追问" });
      fireEvent.change(composer, { target: { value: "继续" } });
      fireEvent.keyDown(composer, { key: "Enter", ctrlKey: true });
      fireEvent.click(await screen.findByRole("button", { name: "放弃修改并发送" }));
      await waitFor(() => {
        const run = fetchMock.mock.calls.find(([input, init]) => String(input).endsWith("/v2/research/topics/t1/runs") && init?.method === "POST");
        expect(JSON.parse(String(run![1]!.body)).parent_version_id).toBe("v2");
      });
      expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("/versions"))).toBe(false);
      expect(screen.queryByText("有未保存的修改")).toBeNull();
    });

    it("creates a calculation from two recorded facts", async () => {
      const fact = (id: string, period: string, value: string): ResearchV2.ResearchFact => ({
        fact_id: id, turn_id: "turn_1", label: "总收入", value, unit: "millions", currency: "USD", period, source_id: "s1", quotation: QUOTE,
        character_start: START, character_end: END, location: { character_start: START, character_end: END }, verification: "numeric_and_location_match_semantics_not_verified",
      });
      const topic = topicWith([turn(1, { turn_id: "turn_1" })]);
      const content = { ...topic.versions[1]!.content, facts: [fact("F1", "FY2026", "48612"), fact("F2", "FY2025", "43310")] };
      const withFacts = { ...topic, versions: [topic.versions[0]!, { ...topic.versions[1]!, content }] };
      const fetchMock = mockApi((url, init) => {
        if (url.pathname === "/v2/research/topics/t1/calculations" && init.method === "POST") return jsonResponse(withV3(withFacts), { status: 201 });
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(withFacts);
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
        return undefined;
      });
      renderTopic();
      await screen.findByRole("region", { name: "第 1 轮" });
      fireEvent.click(screen.getByRole("button", { name: "研究记录" }));
      fireEvent.click(await screen.findByRole("tab", { name: "数字" }));
      fireEvent.change(screen.getByLabelText("本期数字"), { target: { value: "F1" } });
      fireEvent.change(screen.getByLabelText("基期数字"), { target: { value: "F2" } });
      fireEvent.click(screen.getByRole("button", { name: "计算" }));
      await waitFor(() => {
        const call = fetchMock.mock.calls.find(([input, init]) => String(input).endsWith("/v2/research/topics/t1/calculations") && init?.method === "POST");
        expect(JSON.parse(String(call![1]!.body))).toEqual({ parent_version_id: "v2", formula: "growth_percent", first_fact_id: "F1", second_fact_id: "F2" });
      });
    });
  });

  describe("362 acceptance follow-ups", () => {
    it("does not offer to re-deliver a partial turn the model already submitted", async () => {
      const partial = turn(1, { status: "partial", reason: "submitted", stopping_note: "资料不足", gaps: [{ code: "no_cited_claims", message: "x" }] });
      mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([partial]));
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
        if (url.pathname.endsWith("/op_1/recovery-status")) return jsonResponse({ can_continue: false, can_deliver: true, reason: null, checkpoint: "runtime", version_id: "v2" });
        return undefined;
      });
      renderTopic();
      const region = await screen.findByRole("region", { name: "第 1 轮" });
      await waitFor(() => expect(within(region).getByRole("button", { name: "重新提问" })).toBeTruthy());
      expect(within(region).queryByRole("button", { name: "交付已有成果" })).toBeNull();
    });

    it("offers SEC leads as web links when the deployment cannot fetch SEC filings", async () => {
      const sec = "https://www.sec.gov/Archives/edgar/data/1000001/000100000126000012/nwex-20260630.htm";
      const topic = topicWith([turn(1)]);
      const withLead = { ...topic, versions: [topic.versions[0]!, { ...topic.versions[1]!, content: { ...topic.versions[1]!.content, source_candidates: [{ title: "10-K", url: sec, published_at: null, status: "unverified" }] } }] };
      mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(withLead);
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
        if (url.pathname === "/v2/research/capabilities") return jsonResponse({ schema_version: "v1", search_available: true, sec_available: false, market_data_available: false, market_data_key_required: true, sec_financials_available: false, filings_available: false, sources: [] });
        return undefined;
      });
      renderTopic();
      await screen.findByRole("region", { name: "第 1 轮" });
      fireEvent.click(screen.getByRole("button", { name: "研究记录" }));
      fireEvent.click(await screen.findByRole("tab", { name: "资料" }));
      expect(await screen.findByText("10-K")).toBeTruthy();
      await waitFor(() => expect(screen.getByRole("link", { name: /打开网页/ })).toBeTruthy());
      expect(screen.queryByRole("button", { name: /获取原文/ })).toBeNull();
    });

    it("reopens saved partial question components with evidence and missing details", async () => {
      const partial = turn(1, { status: "partial", reason: "provider_timeout", question: "Revenue and entry timing?",
        question_parts: [
          { question: "Revenue", status: "answered", answer: "The filing reports 100 USD.", reason: "", references: [{ fact_id: "f1", origin: "prior" }] },
          { question: "entry timing", status: "unresolved", answer: "", reason: "Current price and entry criteria are missing.", references: [] },
        ] });
      const base = topicWith([partial]);
      const facts: ResearchV2.ResearchFact[] = [{ fact_id: "f1", label: "Revenue", value: "100", unit: "USD", currency: "USD", period: "2025", source_id: "s1", quotation: "Revenue was 100 USD.", character_start: 0, character_end: 20, location: { character_start: 0, character_end: 20 }, verification: "numeric_and_location_match_semantics_not_verified" }];
      const saved = { ...base, versions: [base.versions[0]!, { ...base.versions[1]!, content: { ...base.versions[1]!.content, facts } }] };
      mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(saved);
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
        return undefined;
      });
      const page = renderTopic();
      let region = await screen.findByRole("region", { name: "第 1 轮" });
      expect(within(region).getByText("The filing reports 100 USD.")).toBeTruthy();
      expect(within(region).getByText("Current price and entry criteria are missing.")).toBeTruthy();
      expect(within(region).getByText(/此前记录/)).toBeTruthy();
      page.unmount();
      renderTopic();
      region = await screen.findByRole("region", { name: "第 1 轮" });
      expect(within(region).getByText("The filing reports 100 USD.")).toBeTruthy();
      expect(within(region).getByRole("button", { name: "查看原文" })).toBeTruthy();
    });

    it("renders partial_notes as an explicitly labeled unverified note on a partial turn", async () => {
      const partial = turn(1, {
        status: "partial",
        reason: "submitted",
        stopping_note: "所有外部获取路径受阻",
        report: "",
        partial_notes: "已尝试检索 SEC 与官网 IR（返回 403）。\n\n建议：<b>上传 10-K PDF</b> 后继续。",
        gaps: [{ code: "no_cited_claims", message: "x" }],
      });
      mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([partial]));
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
        return undefined;
      });
      renderTopic();
      const region = await screen.findByRole("region", { name: "第 1 轮" });
      expect(within(region).getByText("部分完成")).toBeTruthy();
      expect(within(region).getByText("这一轮没有取得可用成果。")).toBeTruthy();
      expect(within(region).getByText("过程与缺口说明（未核实线索与建议，非正式结论）")).toBeTruthy();
      expect(within(region).getByText("模型说明：所有外部获取路径受阻")).toBeTruthy();
      const notesRegion = within(region).getByRole("note", { name: "过程与缺口说明（未核实线索与建议，非正式结论）" });
      expect(within(notesRegion).getByText("已尝试检索 SEC 与官网 IR（返回 403）。")).toBeTruthy();
      expect(within(notesRegion).getByText("建议：<b>上传 10-K PDF</b> 后继续。")).toBeTruthy();
      expect(notesRegion.querySelector("b")).toBeNull();
      expect(region.querySelector(".report")).toBeNull();
    });

    it("offers open HTTPS leads for fetching and handles web_pdf vs user_pdf spacing warnings", async () => {
      const topic = topicWith([turn(1)]);
      const withLeads = {
        ...topic,
        versions: [
          topic.versions[0]!,
          {
            ...topic.versions[1]!,
            content: {
              ...topic.versions[1]!.content,
              source_candidates: [
                { title: "NVIDIA Release", url: "https://investor.nvidia.com/news/q4", published_at: null, status: "unverified" },
                { title: "HTTP Only", url: "http://legacy.example.com/report", published_at: null, status: "unverified" },
              ],
            },
          },
        ],
      };
      mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(withLeads);
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
        if (url.pathname === "/v2/research/capabilities") {
          return jsonResponse({ schema_version: "v1", search_available: true, web_fetch_enabled: true, sec_available: true, market_data_available: false, market_data_key_required: true, sec_financials_available: false, filings_available: false, sources: [] });
        }
        if (url.pathname.endsWith("/versions/v2/sources")) {
          return jsonResponse([
            {
              status: "available", source_id: "s_web_pdf", title: "Web PDF", kind: "user_supplied", is_simulated: false,
              published_at: null, event_at: null, revised_at: null, obtained_at: NOW, content_sha256: "a".repeat(64),
              retrieval_method: "web_pdf", pdf_text_order: "layout", derived_from_source_id: null,
              permissions: { store: true, model_input: true, display: true, export: false },
              original_pdf_available: true, pdf_text_spacing_warning: true, verification: "user_declared_not_verified",
            },
            {
              status: "available", source_id: "s_user_pdf", title: "User PDF", kind: "user_supplied", is_simulated: false,
              published_at: null, event_at: null, revised_at: null, obtained_at: NOW, content_sha256: "b".repeat(64),
              retrieval_method: "user_pdf", pdf_text_order: "layout", derived_from_source_id: null,
              permissions: { store: true, model_input: true, display: true, export: true },
              original_pdf_available: true, pdf_text_spacing_warning: true, verification: "user_declared_not_verified",
            },
          ]);
        }
        return undefined;
      });
      renderTopic();
      await screen.findByRole("region", { name: "第 1 轮" });
      fireEvent.click(screen.getByRole("button", { name: "研究记录" }));
      fireEvent.click(await screen.findByRole("tab", { name: "资料" }));
      expect(await screen.findByText("网页 PDF", { exact: false })).toBeTruthy();
      expect(screen.getByText("PDF 提取的文字间距可能有误，可对照原始文档或保存后重新上传提取。")).toBeTruthy();
      expect(screen.getByText("PDF 提取的文字间距可能有误，可以换一种顺序重新提取。")).toBeTruthy();
      expect(screen.getAllByRole("button", { name: "按阅读顺序重新提取" })).toHaveLength(1);
      expect(screen.getByRole("button", { name: /获取原文/ })).toBeTruthy();
      expect(screen.getByRole("link", { name: /打开网页/ })).toBeTruthy();
    });
  });

  describe("L1 streaming research progress", () => {
    it("reads the live capsule current rationale/target/phase directly without borrowing prior tool rationale", async () => {
      let pollCount = 0;
      mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([turn(1)]));
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_2", "running"), operation("op_1", "completed")]);
        if (url.pathname.endsWith("/op_2/events")) return jsonResponse({ error_code: "method_not_allowed", message: "fallback" }, { status: 405 });
        if (url.pathname.endsWith("/op_2/progress")) {
          pollCount += 1;
          const current = pollCount === 1
            ? { kind: "tool", name: "read_sources" }
            : { kind: "model", phase: "retrying", retry_attempt: 2 };
          return jsonResponse({
            operation_id: "op_2",
            operation_status: "running",
            turn: { index: 2, question: "新问题" },
            usage: { model_calls: 1, search_calls: 0, total_tokens: 500 },
            current,
            events: [{ index: 0, kind: "tool", name: "read_sources", status: "read", reason: null, rationale: "旧步骤理由", denied_count: null }],
            event_count: 1,
            started_at: NOW,
            updated_at: NOW,
            version_id: null,
          });
        }
        return undefined;
      });
      const { container } = renderTopic();
      await screen.findByRole("region", { name: "第 2 轮" });
      // The current activity shows in the capsule once the reader scrolls away from the live turn.
      scrollAway(container);
      const capsule = await screen.findByRole("button", { name: "回到实时过程" });
      expect(capsule.textContent).toContain("阅读资料");
      expect(capsule.textContent).not.toContain("旧步骤理由");
    });

    it("shows the model retry phase in the live capsule and pauses auto-scroll when scrolled up with jump-back action", async () => {
      mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([turn(1)]));
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_2", "running"), operation("op_1", "completed")]);
        if (url.pathname.endsWith("/op_2/events")) return jsonResponse({ error_code: "method_not_allowed", message: "fallback" }, { status: 405 });
        if (url.pathname.endsWith("/op_2/progress")) {
          return jsonResponse({
            operation_id: "op_2",
            operation_status: "running",
            turn: { index: 2, question: "滚动测试" },
            usage: { model_calls: 2, search_calls: 1, total_tokens: 1500 },
            current: { kind: "model", phase: "retrying", retry_attempt: 2 },
            events: [{ index: 0, kind: "tool", name: "news_search", status: "completed", reason: null, rationale: "搜索新闻", denied_count: null }],
            event_count: 1,
            started_at: NOW,
            updated_at: NOW,
            version_id: null,
          });
        }
        return undefined;
      });
      const { container } = renderTopic();
      await screen.findByRole("region", { name: "第 2 轮" });
      // Near the live turn there is no floating bar at all.
      expect(screen.queryByRole("button", { name: "回到实时过程" })).toBeNull();
      const scrollToSpy = scrollAway(container);

      const jumpBtn = await screen.findByRole("button", { name: "回到实时过程" });
      expect(jumpBtn.textContent).toContain("重试模型请求（第 2 次）");
      fireEvent.click(jumpBtn);
      expect(scrollToSpy).toHaveBeenCalled();
      await waitFor(() => expect(screen.queryByRole("button", { name: "回到实时过程" })).toBeNull());
    });

    it("collapses completed turn process summary by default, expands inline, and shares StepsTab", async () => {
      const richTurn = turn(1, {
        usage: { model_calls: 2, search_calls: 1, total_tokens: 2400 },
        events: [
          { kind: "model", usage: 1200, usage_detail: { input_tokens: 900, output_tokens: 300 }, finish_reason: "tool_calls", note: "首先搜索公司官方披露。" },
          {
            kind: "tool",
            name: "news_search",
            status: "completed",
            rationale: "查找 10-K",
            summary: {
              kind: "news_search",
              found_count: 1,
              eligible_count: 1,
              denied_count: 0,
              leads_preview: [{ title: "Northwind 10-K", host: "www.sec.gov", published_at: "2026-08-01" }],
            },
          },
          { kind: "submission", status: "completed" },
        ],
        report: "正式研究报告段落。",
      });
      mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([richTurn]));
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
        return undefined;
      });
      renderTopic();
      const region = await screen.findByRole("region", { name: "第 1 轮" });
      const toggle = within(region).getByRole("button", { name: "过程 2 步" });
      expect(toggle.getAttribute("aria-expanded")).toBe("false");
      expect(within(region).queryByText("首先搜索公司官方披露。")).toBeNull();

      fireEvent.click(toggle);
      expect(toggle.getAttribute("aria-expanded")).toBe("true");
      expect(within(region).getByText("首先搜索公司官方披露。")).toBeTruthy();
      expect(within(region).getByText("发现 1 条线索（可用 1）")).toBeTruthy();

      fireEvent.click(within(region).getByRole("button", { name: /在研究记录中查看/ }));
      const dialog = await screen.findByRole("dialog");
      expect(within(dialog).getByText("首先搜索公司官方披露。")).toBeTruthy();
      expect(within(dialog).getByText("发现 1 条线索（可用 1）")).toBeTruthy();
    });

    it("renders interrupted run progress steps above the recovery callout", async () => {
      mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([turn(1)]));
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_2", "failed"), operation("op_1", "completed")]);
        if (url.pathname.endsWith("/op_2/recovery-status")) return jsonResponse({ can_continue: true, can_deliver: false, reason: null, checkpoint: "tool_batch", version_id: "v2" });
        if (url.pathname.endsWith("/op_2/events")) return jsonResponse({ error_code: "method_not_allowed", message: "fallback" }, { status: 405 });
        if (url.pathname.endsWith("/op_2/progress")) {
          return jsonResponse({
            operation_id: "op_2",
            operation_status: "failed",
            turn: { index: 2, question: "中断测试" },
            usage: { model_calls: 2, search_calls: 1, total_tokens: 900 },
            current: null,
            events: [
              { index: 0, kind: "model", finish_reason: "tool_calls", usage: 400, input_tokens: 300, output_tokens: 100, settlement: null, note: "第一步思考" },
              { index: 1, kind: "tool", name: "news_search", status: "completed", reason: null, rationale: "搜索财报", denied_count: null },
              { index: 2, kind: "tool", name: "acquire_sources", status: "failed", reason: "http_forbidden_or_challenge", rationale: "抓取官网", denied_count: null },
              { index: 3, kind: "model_failure", code: "provider_timeout" },
            ],
            event_count: 4,
            started_at: NOW,
            updated_at: NOW,
            version_id: null,
          });
        }
        return undefined;
      });
      renderTopic();
      const region = await screen.findByRole("region", { name: "第 2 轮" });
      expect(await within(region).findByText("模型调用失败 · 请求超时")).toBeTruthy();
      expect(within(region).getByRole("button", { name: "展开前 1 步" })).toBeTruthy();
      fireEvent.click(within(region).getByRole("button", { name: "展开前 1 步" }));
      expect(within(region).getByText("第一步思考")).toBeTruthy();
    });

    it("supports legacy turns without L1 note or summary fields", async () => {
      const legacyTurn = turn(1, {
        events: [
          { kind: "model", usage: 500, usage_detail: { input_tokens: 400, output_tokens: 100 }, finish_reason: "tool_calls" },
          { kind: "tool", name: "read_sources", status: "read", rationale: "旧版读取" },
        ],
        report: "旧版报告。",
      });
      mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([legacyTurn]));
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
        return undefined;
      });
      renderTopic();
      const region = await screen.findByRole("region", { name: "第 1 轮" });
      fireEvent.click(within(region).getByRole("button", { name: "过程 1 步" }));
      expect(within(region).getByText("旧版读取")).toBeTruthy();
      // Under the answer a model step only says it ran; token accounting lives in the record panel.
      expect(within(region).getByText("模型调用")).toBeTruthy();
      expect(within(region).queryByText("模型调用 · 400 输入 / 100 输出 Token")).toBeNull();
      fireEvent.click(within(region).getByRole("button", { name: /在研究记录中查看/ }));
      const dialog = await screen.findByRole("dialog");
      expect(within(dialog).getByText("模型调用 · 400 输入 / 100 输出 Token")).toBeTruthy();
    });
  });

  describe("L2 SSE live research progress", () => {
    it("streams live snapshot, step events, heartbeats, and terminal transition without polling /progress", async () => {
      const encoder = new TextEncoder();
      let streamController: ReadableStreamDefaultController<Uint8Array> | null = null;
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          streamController = controller;
        },
      });

      let completed = false;
      const fetchMock = mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") {
          return jsonResponse(
            topicWith(completed ? [turn(1), turn(2, { report: "SSE 完成报告。" })] : [turn(1)]),
          );
        }
        if (url.pathname === "/v2/research/topics/t1/runs") {
          return jsonResponse(
            completed
              ? [operation("op_2", "completed"), operation("op_1", "completed")]
              : [operation("op_2", "running"), operation("op_1", "completed")],
          );
        }
        if (url.pathname.endsWith("/op_2/events")) {
          return new Response(body, {
            status: 200,
            headers: { "Content-Type": "text/event-stream; charset=utf-8" },
          });
        }
        if (url.pathname.endsWith("/op_2/progress")) {
          return jsonResponse({ error_code: "unexpected_poll", message: "should not poll" }, { status: 500 });
        }
        return undefined;
      });

      const sendFrame = (id: string, event: string, data: unknown) => {
        streamController!.enqueue(encoder.encode(`id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
      };

      renderTopic();
      await waitFor(() => expect(streamController).toBeTruthy());

      sendFrame("ev:0", "progress.snapshot", {
        operation_id: "op_2",
        operation_status: "running",
        turn: { index: 2, question: "SSE 实时测试" },
        usage: { model_calls: 1, search_calls: 0, total_tokens: 600 },
        interim: { leads_count: 0, acquired_sources_count: 0, read_sources_count: 0, facts_count: 0, calculations_count: 0 },
        current: { kind: "tool", name: "news_search", rationale: "搜索年报", target: "Northwind 10-K" },
        events: [{ index: 0, kind: "model", finish_reason: "tool_calls", usage: 600, input_tokens: 450, output_tokens: 150, settlement: null, note: "先通过 SSE 检索年报。" }],
        event_count: 1,
        started_at: NOW,
        updated_at: NOW,
        version_id: null,
      });

      const liveRegion = await screen.findByRole("region", { name: "第 2 轮" });
      expect(await within(liveRegion).findByText("先通过 SSE 检索年报。")).toBeTruthy();
      expect(within(liveRegion).getByText("Northwind 10-K")).toBeTruthy();
      expect(within(liveRegion).getAllByText(/搜索年报/).length).toBeGreaterThan(0);

      // Send heartbeat, duplicate ev:0, new step.event, current update, and transient L3 stream frames.
      streamController!.enqueue(encoder.encode(": heartbeat\n\n"));
      sendFrame("ev:0", "step.event", {
        operation_id: "op_2",
        event: { index: 0, kind: "model", finish_reason: "tool_calls", usage: 600, input_tokens: 450, output_tokens: 150, settlement: null, note: "先通过 SSE 检索年报。" },
        event_count: 1,
        usage: { model_calls: 1, search_calls: 0, total_tokens: 600 },
        interim: null,
        updated_at: NOW,
      });
      sendFrame("ev:1", "step.event", {
        operation_id: "op_2",
        event: {
          index: 1,
          kind: "tool",
          name: "news_search",
          status: "completed",
          reason: null,
          rationale: "搜索年报",
          denied_count: 1,
          summary: { kind: "news_search", found_count: 2, eligible_count: 1, denied_count: 1, leads_preview: [{ title: "Northwind 10-K", host: "www.sec.gov", published_at: "2026-08-01" }] },
        },
        event_count: 2,
        usage: { model_calls: 1, search_calls: 1, total_tokens: 600 },
        interim: { leads_count: 1, acquired_sources_count: 0, read_sources_count: 0, facts_count: 0, calculations_count: 0 },
        updated_at: NOW,
      });
      sendFrame("cp:2", "step.current", {
        operation_id: "op_2",
        operation_status: "running",
        current: { kind: "tool", name: "read_sources", rationale: "精读收入附注", target: "《Northwind 10-K》0–8,000 字" },
        usage: { model_calls: 1, search_calls: 1, total_tokens: 600 },
        interim: { leads_count: 1, acquired_sources_count: 1, read_sources_count: 0, facts_count: 0, calculations_count: 0 },
        updated_at: NOW,
      });
      sendFrame("cp:2", "stream.delta", { operation_id: "op_2", call_seq: 1, channel: "thinking", delta: "暂存思考", seq: 0 });
      sendFrame("cp:2", "stream.reset", { operation_id: "op_2", call_seq: 1, reason: "next_step" });

      expect(await within(liveRegion).findByText("发现 2 条线索（可用 1 · 受限 1）")).toBeTruthy();
      expect(within(liveRegion).getAllByText("先通过 SSE 检索年报。")).toHaveLength(1);
      expect(within(liveRegion).getByText("《Northwind 10-K》0–8,000 字")).toBeTruthy();
      expect(within(liveRegion).getByText(/精读收入附注/)).toBeTruthy();

      completed = true;
      sendFrame("done:op_2", "run.terminal", {
        operation_id: "op_2",
        operation_status: "completed",
        version_id: "v2",
        usage: { model_calls: 2, search_calls: 1, total_tokens: 1400 },
        interim: { leads_count: 1, acquired_sources_count: 1, read_sources_count: 1, facts_count: 0, calculations_count: 0 },
        event_count: 2,
      });
      streamController!.close();

      expect(await screen.findByText("SSE 完成报告。")).toBeTruthy();
      const completedRegion = await screen.findByRole("region", { name: "第 2 轮" });
      expect(within(completedRegion).getByText("SSE 完成报告。")).toBeTruthy();
      await waitFor(() => expect(screen.queryByTitle("打开过程面板")).toBeNull());
      const progressCalls = fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/op_2/progress"));
      expect(progressCalls).toHaveLength(0);
    });

    it("renders untrusted SSE model notes and lead titles as plain text without HTML injection", async () => {
      const payload = {
        operation_id: "op_2",
        operation_status: "running",
        turn: { index: 2, question: "安全渲染" },
        usage: { model_calls: 1, search_calls: 1, total_tokens: 400 },
        interim: { leads_count: 1, acquired_sources_count: 0, read_sources_count: 0, facts_count: 0, calculations_count: 0 },
        current: { kind: "tool", name: "news_search", rationale: "<img src=x onerror=alert(1)>", target: "<script>alert(2)</script>" },
        events: [
          { index: 0, kind: "model", finish_reason: "tool_calls", usage: 400, input_tokens: 300, output_tokens: 100, settlement: null, note: "<b>加粗不生效</b>" },
          {
            index: 1,
            kind: "tool",
            name: "news_search",
            status: "completed",
            reason: null,
            rationale: "检查线索",
            denied_count: 0,
            summary: { kind: "news_search", found_count: 1, eligible_count: 1, denied_count: 0, leads_preview: [{ title: "<svg onload=alert(3)>", host: "example.com", published_at: null }] },
          },
        ],
        event_count: 2,
        started_at: NOW,
        updated_at: NOW,
        version_id: null,
      };
      mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([turn(1)]));
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_2", "running"), operation("op_1", "completed")]);
        if (url.pathname.endsWith("/op_2/events")) {
          return sseResponse([
            `id: ev:1\nevent: progress.snapshot\ndata: ${JSON.stringify(payload)}\n\n`,
            ": heartbeat\n\n",
          ]);
        }
        return undefined;
      });
      const { container } = renderTopic();
      const liveRegion = await screen.findByRole("region", { name: "第 2 轮" });
      expect(await within(liveRegion).findByText("<b>加粗不生效</b>")).toBeTruthy();
      expect(within(liveRegion).getByText("<img src=x onerror=alert(1)>")).toBeTruthy();
      expect(within(liveRegion).getByText("<script>alert(2)</script>")).toBeTruthy();
      fireEvent.click(within(liveRegion).getByRole("button", { name: "展开线索 (1)" }));
      expect(within(liveRegion).getByText("<svg onload=alert(3)>")).toBeTruthy();
      expect(container.querySelector("svg[onload]")).toBeNull();
      expect(container.querySelector("img")).toBeNull();
      expect(container.querySelector("script")).toBeNull();
    });
  });

  describe("L3 token streaming & draft preview", () => {
    it("streams thinking and dashed draft_report, isolates historical turns from token bursts, retracts on submission_rejected, and settles into verified TurnView", async () => {
      const durationSpy = vi.spyOn(researchLib, "turnDurationMs");
      const encoder = new TextEncoder();
      let streamController: ReadableStreamDefaultController<Uint8Array> | null = null;
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          streamController = controller;
        },
      });
      let completed = false;
      mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") {
          return jsonResponse(
            topicWith(completed ? [turn(1), turn(2, { report: "正式核验完成报告。" })] : [turn(1)]),
          );
        }
        if (url.pathname === "/v2/research/topics/t1/runs") {
          return jsonResponse(
            completed
              ? [operation("op_2", "completed"), operation("op_1", "completed")]
              : [operation("op_2", "running"), operation("op_1", "completed")],
          );
        }
        if (url.pathname.endsWith("/op_2/events")) {
          return new Response(body, {
            status: 200,
            headers: { "Content-Type": "text/event-stream; charset=utf-8" },
          });
        }
        return undefined;
      });
      const sendFrame = (event: string, data: unknown, id?: string) => {
        const lines = [...(id ? [`id: ${id}`] : []), `event: ${event}`, `data: ${JSON.stringify(data)}`];
        streamController!.enqueue(encoder.encode(`${lines.join("\n")}\n\n`));
      };

      try {
        renderTopic();
        await screen.findByRole("region", { name: "第 1 轮" });
        await waitFor(() => expect(streamController).toBeTruthy());

        sendFrame("progress.snapshot", {
          operation_id: "op_2",
          operation_status: "running",
          turn: { index: 2, question: "L3 流式问题" },
          usage: { model_calls: 1, search_calls: 0, total_tokens: 100 },
          interim: null,
          current: { kind: "model", phase: "thinking", retry_attempt: null, started_at: NOW },
          events: [],
          event_count: 0,
          started_at: NOW,
          updated_at: NOW,
          version_id: null,
        }, "cp:0");

        const liveRegion = await screen.findByRole("region", { name: "第 2 轮" });
        await within(liveRegion).findByText("模型思考中");

        // Thinking + process text stream deltas must NOT re-render historical Turn 1.
        const turn1CallsBeforeThinking = durationSpy.mock.calls.length;

        sendFrame("stream.delta", { operation_id: "op_2", call_seq: 1, channel: "thinking", delta: "思考步骤A。", seq: 0 });
        sendFrame("stream.delta", { operation_id: "op_2", call_seq: 1, channel: "thinking", delta: "思考步骤B。", seq: 1 });
        sendFrame("stream.delta", { operation_id: "op_2", call_seq: 1, channel: "text", delta: "正在核对财报数据。", seq: 2 });

        expect(await within(liveRegion).findByText("模型思考过程 · 非正式结论")).toBeTruthy();
        expect(within(liveRegion).getByText("思考步骤A。思考步骤B。")).toBeTruthy();
        expect(within(liveRegion).getByText("正在核对财报数据。")).toBeTruthy();
        expect(durationSpy.mock.calls.length).toBe(turn1CallsBeforeThinking);

        // Manual collapse toggle works and stays collapsed during the same call_seq.
        fireEvent.click(within(liveRegion).getByRole("button", { name: "收起模型思考过程" }));
        expect(within(liveRegion).queryByText(/思考步骤A/)).toBeNull();
        sendFrame("stream.delta", { operation_id: "op_2", call_seq: 1, channel: "thinking", delta: "思考步骤C。", seq: 3 });
        await new Promise((resolve) => setTimeout(resolve, 25));
        expect(within(liveRegion).queryByText(/思考步骤C/)).toBeNull();

        // Tool transition resets transient model thought stream automatically.
        sendFrame("stream.reset", { operation_id: "op_2", call_seq: 1, reason: "tool_started" });
        sendFrame("step.current", {
          operation_id: "op_2",
          operation_status: "running",
          current: { kind: "tool", name: "read_sources", rationale: "阅读原文", target: "《Northwind 年报》" },
          usage: { model_calls: 1, search_calls: 0, total_tokens: 300 },
          interim: null,
          updated_at: NOW,
        });
        await waitFor(() => expect(within(liveRegion).queryByText("模型思考过程 · 非正式结论")).toBeNull());
        expect(await within(liveRegion).findByText("《Northwind 年报》")).toBeTruthy();

        // Draft report bursts (25 deltas) render inside dashed preview WITHOUT re-rendering historical Turn 1.
        const turn1CallsBeforeDraftBurst = durationSpy.mock.calls.length;
        sendFrame("stream.delta", { operation_id: "op_2", call_seq: 2, channel: "tool_status", delta: "正在起草研究报告并整理引文，完成后将自动核验原文位置…", seq: 0 });
        for (let i = 1; i <= 25; i += 1) {
          sendFrame("stream.delta", { operation_id: "op_2", call_seq: 2, channel: "draft_report", delta: `草稿句${i} `, seq: i });
        }
        const draftBox = await within(liveRegion).findByRole("region", { name: "未核验报告草稿预览" });
        expect(within(draftBox).getByText("草稿生成中 · 引文与数值尚未经服务端核验，最终结果以核验后为准")).toBeTruthy();
        expect(await within(draftBox).findByText(/草稿句25/)).toBeTruthy();
        expect(durationSpy.mock.calls.length).toBe(turn1CallsBeforeDraftBurst);

        // submission_rejected retracts dashed draft preview immediately and displays notice.
        sendFrame("stream.reset", { operation_id: "op_2", call_seq: 2, reason: "submission_rejected" });
        await waitFor(() => expect(within(liveRegion).queryByRole("region", { name: "未核验报告草稿预览" })).toBeNull());
        expect(await within(liveRegion).findByText("草稿引文或格式未通过核验，模型正在修正…")).toBeTruthy();

        // Revised draft clears notice; run.terminal transitions to verified AnswerBlock.
        sendFrame("stream.delta", { operation_id: "op_2", call_seq: 3, channel: "draft_report", delta: "修正后的草稿段落。", seq: 0 });
        expect(await within(liveRegion).findByText("修正后的草稿段落。")).toBeTruthy();
        expect(within(liveRegion).queryByText("草稿引文或格式未通过核验，模型正在修正…")).toBeNull();

        completed = true;
        sendFrame("run.terminal", { operation_id: "op_2", operation_status: "completed", version_id: "v2" }, "done:completed");
        streamController!.close();

        expect(await screen.findByText("正式核验完成报告。")).toBeTruthy();
        await waitFor(() => expect(screen.queryByRole("region", { name: "未核验报告草稿预览" })).toBeNull());
      } finally {
        durationSpy.mockRestore();
      }
    });

    it("retracts unverified draft immediately on submission_partial before showing partial turn", async () => {
      const encoder = new TextEncoder();
      let streamController: ReadableStreamDefaultController<Uint8Array> | null = null;
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          streamController = controller;
        },
      });
      let savedPartial = false;
      mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") {
          return jsonResponse(
            topicWith(savedPartial ? [turn(1), turn(2, { status: "partial", reason: "token_budget_exhausted", report: "" })] : [turn(1)]),
          );
        }
        if (url.pathname === "/v2/research/topics/t1/runs") {
          return jsonResponse(
            savedPartial
              ? [operation("op_2", "failed"), operation("op_1", "completed")]
              : [operation("op_2", "running"), operation("op_1", "completed")],
          );
        }
        if (url.pathname.endsWith("/op_2/recovery-status")) {
          return jsonResponse({ can_continue: true, can_deliver: false, reason: null, checkpoint: "submission", version_id: "v2" });
        }
        if (url.pathname.endsWith("/op_2/events")) {
          return new Response(body, {
            status: 200,
            headers: { "Content-Type": "text/event-stream; charset=utf-8" },
          });
        }
        return undefined;
      });
      const sendFrame = (event: string, data: unknown, id?: string) => {
        const lines = [...(id ? [`id: ${id}`] : []), `event: ${event}`, `data: ${JSON.stringify(data)}`];
        streamController!.enqueue(encoder.encode(`${lines.join("\n")}\n\n`));
      };

      renderTopic();
      await waitFor(() => expect(streamController).toBeTruthy());

      sendFrame("progress.snapshot", {
        operation_id: "op_2",
        operation_status: "running",
        turn: { index: 2, question: "部分降级测试" },
        usage: { model_calls: 1, search_calls: 0, total_tokens: 200 },
        interim: null,
        current: { kind: "model", phase: "responding", retry_attempt: null, started_at: NOW },
        events: [],
        event_count: 0,
        started_at: NOW,
        updated_at: NOW,
        version_id: null,
      }, "cp:0");
      sendFrame("stream.delta", { operation_id: "op_2", call_seq: 1, channel: "draft_report", delta: "尚未完成核验的草稿内容。", seq: 0 });
      expect(await screen.findByText("尚未完成核验的草稿内容。")).toBeTruthy();

      savedPartial = true;
      sendFrame("stream.reset", { operation_id: "op_2", call_seq: 1, reason: "submission_partial" });
      await waitFor(() => expect(screen.queryByRole("region", { name: "未核验报告草稿预览" })).toBeNull());

      sendFrame("run.terminal", { operation_id: "op_2", operation_status: "failed", version_id: "v2" }, "done:failed");
      streamController!.close();

      expect(await screen.findByText("部分完成")).toBeTruthy();
      expect(screen.queryByText("尚未完成核验的草稿内容。")).toBeNull();
    });
  });

  describe("Stage 2 repeated truncation and recovery guidance", () => {
    it("suppresses continue retry, displays localized repeated_truncation guidance and partial_notes on a partial turn", async () => {
      const fact: ResearchV2.ResearchFact = {
        fact_id: "F1", turn_id: "turn_1", label: "总收入", value: "48612", unit: "millions", currency: "USD", period: "FY2026",
        source_id: "s1", quotation: QUOTE, character_start: START, character_end: END, location: { character_start: START, character_end: END },
        verification: "numeric_and_location_match_semantics_not_verified",
      };
      const partial = turn(1, {
        question: "对比过去五年所有附注与分部利润率",
        status: "partial",
        reason: "provider_output_truncated",
        report: "",
        fact_ids: ["F1"],
        partial_notes: "已获取 1 份资料并记录 1 个数字，因模型输出达到上限中止。\n\n建议先核对已记录数字，或缩小时间范围再提问。",
        gaps: [{ code: "provider_output_truncated", message: "Provider output was truncated." }],
      });
      const baseTopic = topicWith([partial]);
      const topicWithFact: ResearchV2.ResearchTopic = {
        ...baseTopic,
        versions: [
          baseTopic.versions[0]!,
          { ...baseTopic.versions[1]!, content: { ...baseTopic.versions[1]!.content, facts: [fact] } },
        ],
      };
      mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWithFact);
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
        if (url.pathname.endsWith("/op_1/recovery-status")) {
          return jsonResponse({ can_continue: false, can_deliver: false, reason: "repeated_truncation", checkpoint: "tool_batch", version_id: "v2" });
        }
        return undefined;
      });
      renderTopic();
      const region = await screen.findByRole("region", { name: "第 1 轮" });
      expect(within(region).getByText("部分完成")).toBeTruthy();
      expect(within(region).getByText("已记录 1 个数字，还没有形成可引用的结论。")).toBeTruthy();
      expect(within(region).getByText("模型输出达到单次长度上限而被截断")).toBeTruthy();
      expect(await within(region).findByText(/^连续多次达到模型输出或上下文长度上限/)).toBeTruthy();
      expect(within(region).queryByText(/repeated_truncation/)).toBeNull();
      expect(within(region).queryByText(/provider_output_truncated/)).toBeNull();

      // Partial notes remain visible and explicitly marked as unverified.
      const notesRegion = within(region).getByRole("note", { name: "过程与缺口说明（未核实线索与建议，非正式结论）" });
      expect(within(notesRegion).getByText("已获取 1 份资料并记录 1 个数字，因模型输出达到上限中止。")).toBeTruthy();
      expect(within(notesRegion).getByText("建议先核对已记录数字，或缩小时间范围再提问。")).toBeTruthy();

      // Circuit-breaker replaces retry with narrow/re-ask without showing "继续研究".
      expect(within(region).queryByRole("button", { name: "继续研究" })).toBeNull();
      const reaskButton = within(region).getByRole("button", { name: "重新提问" });
      fireEvent.click(within(region).getByRole("button", { name: "更多" }));
      const menu = await screen.findByRole("menu");
      expect(within(menu).getByRole("menuitem", { name: "添加资料" })).toBeTruthy();
      expect(within(menu).queryByRole("menuitem", { name: "补充缺口" })).toBeNull();
      fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "取消" }));

      fireEvent.click(reaskButton);
      expect((screen.getByRole("textbox", { name: "追问" }) as HTMLTextAreaElement).value).toBe("对比过去五年所有附注与分部利润率");
    });

    it("never exposes continue even if recovery payload carries can_continue alongside repeated_truncation", async () => {
      const partial = turn(1, {
        status: "partial",
        reason: "provider_context_limit",
        report: "",
        gaps: [{ code: "provider_context_limit", message: "Provider context limit was reached." }],
      });
      mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([partial]));
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
        if (url.pathname.endsWith("/op_1/recovery-status")) {
          return jsonResponse({ can_continue: true, can_deliver: true, reason: "repeated_truncation", checkpoint: "tool_batch", version_id: "v2" });
        }
        return undefined;
      });
      renderTopic();
      const region = await screen.findByRole("region", { name: "第 1 轮" });
      expect(await within(region).findByText(/^连续多次达到模型输出或上下文长度上限/)).toBeTruthy();
      expect(within(region).queryByRole("button", { name: "继续研究" })).toBeNull();
      expect(within(region).queryByRole("button", { name: "交付已有成果" })).toBeNull();
      expect(within(region).getByRole("button", { name: "重新提问" })).toBeTruthy();
    });

    it("shows localized repeated_truncation guidance on an interrupted orphan run without a continue button", async () => {
      mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([turn(1)]));
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_2", "failed"), operation("op_1", "completed")]);
        if (url.pathname.endsWith("/op_2/recovery-status")) {
          return jsonResponse({ can_continue: false, can_deliver: false, reason: "repeated_truncation", checkpoint: "tool_batch", version_id: "v2" });
        }
        if (url.pathname.endsWith("/op_2/events")) return jsonResponse({ error_code: "method_not_allowed", message: "fallback" }, { status: 405 });
        if (url.pathname.endsWith("/op_2/progress")) {
          return jsonResponse({
            operation_id: "op_2",
            operation_status: "failed",
            turn: { index: 2, question: "超长截断中断问题" },
            usage: { model_calls: 2, search_calls: 0, total_tokens: 12000 },
            current: null,
            events: [{ index: 0, kind: "model_failure", code: "provider_output_truncated" }],
            event_count: 1,
            started_at: NOW,
            updated_at: NOW,
            version_id: null,
          });
        }
        return undefined;
      });
      renderTopic();
      const region = await screen.findByRole("region", { name: "第 2 轮" });
      expect(within(region).getByText("运行中断")).toBeTruthy();
      expect(await within(region).findByText("模型调用失败 · 输出达到单次长度上限被截断")).toBeTruthy();
      expect(await within(region).findByText(/^连续多次达到模型输出或上下文长度上限/)).toBeTruthy();
      expect(within(region).queryByRole("button", { name: "继续研究" })).toBeNull();
      expect(within(region).getByRole("button", { name: "重新提问" })).toBeTruthy();
    });
  });

  describe("Phase 1.2 repeated timeout recovery guidance and button gating", () => {
    it("blocks continue and deliver, hides fill-gap, and displays localized repeated_timeout guidance on a partial turn", async () => {
      const partial = turn(1, {
        question: "分析纳斯达克最近走势，并分析预测未来的可能",
        status: "partial",
        reason: "provider_timeout",
        report: "",
        gaps: [{ code: "provider_timeout", message: "Provider timed out without a usable result." }],
      });
      mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([partial]));
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
        if (url.pathname.endsWith("/op_1/recovery-status")) {
          return jsonResponse({ can_continue: false, can_deliver: false, reason: "repeated_timeout", checkpoint: "tool_batch", version_id: "v2" });
        }
        return undefined;
      });
      renderTopic();
      const region = await screen.findByRole("region", { name: "第 1 轮" });
      expect(within(region).getByText("部分完成")).toBeTruthy();
      expect(within(region).getByText("模型服务请求超时")).toBeTruthy();
      expect(await within(region).findByText(/^连续多次发生模型单次调用超时/)).toBeTruthy();
      expect(within(region).queryByText(/repeated_timeout/)).toBeNull();
      expect(within(region).queryByText(/provider_timeout/)).toBeNull();

      expect(within(region).queryByRole("button", { name: "继续研究" })).toBeNull();
      expect(within(region).queryByRole("button", { name: "交付已有成果" })).toBeNull();
      const reaskButton = within(region).getByRole("button", { name: "重新提问" });
      fireEvent.click(within(region).getByRole("button", { name: "更多" }));
      const menu = await screen.findByRole("menu");
      expect(within(menu).getByRole("menuitem", { name: "添加资料" })).toBeTruthy();
      expect(within(menu).queryByRole("menuitem", { name: "补充缺口" })).toBeNull();
      fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "取消" }));

      fireEvent.click(reaskButton);
      expect((screen.getByRole("textbox", { name: "追问" }) as HTMLTextAreaElement).value).toBe("分析纳斯达克最近走势，并分析预测未来的可能");
    });

    it("never exposes continue or deliver even if recovery payload carries can_continue/can_deliver alongside repeated_timeout", async () => {
      const partial = turn(1, {
        status: "partial",
        reason: "provider_timeout",
        report: "",
        gaps: [{ code: "provider_timeout", message: "Provider timed out without a usable result." }],
      });
      mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([partial]));
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
        if (url.pathname.endsWith("/op_1/recovery-status")) {
          return jsonResponse({ can_continue: true, can_deliver: true, reason: "repeated_timeout", checkpoint: "tool_batch", version_id: "v2" });
        }
        return undefined;
      });
      renderTopic();
      const region = await screen.findByRole("region", { name: "第 1 轮" });
      expect(await within(region).findByText(/^连续多次发生模型单次调用超时/)).toBeTruthy();
      expect(within(region).queryByRole("button", { name: "继续研究" })).toBeNull();
      expect(within(region).queryByRole("button", { name: "交付已有成果" })).toBeNull();
      expect(within(region).getByRole("button", { name: "重新提问" })).toBeTruthy();
      fireEvent.click(within(region).getByRole("button", { name: "更多" }));
      const menu = await screen.findByRole("menu");
      expect(within(menu).getByRole("menuitem", { name: "添加资料" })).toBeTruthy();
      expect(within(menu).queryByRole("menuitem", { name: "补充缺口" })).toBeNull();
    });

    it("shows localized repeated_timeout guidance on an interrupted orphan run and falls back to re-ask", async () => {
      mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([turn(1)]));
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_2", "failed"), operation("op_1", "completed")]);
        if (url.pathname.endsWith("/op_2/recovery-status")) {
          return jsonResponse({ can_continue: false, can_deliver: false, reason: "repeated_timeout", checkpoint: "tool_batch", version_id: "v2" });
        }
        if (url.pathname.endsWith("/op_2/events")) return jsonResponse({ error_code: "method_not_allowed", message: "fallback" }, { status: 405 });
        if (url.pathname.endsWith("/op_2/progress")) {
          return jsonResponse({
            operation_id: "op_2",
            operation_status: "failed",
            turn: { index: 2, question: "连续超时中断问题" },
            usage: { model_calls: 2, search_calls: 0, total_tokens: 12000 },
            current: null,
            events: [{ index: 0, kind: "model_failure", code: "provider_timeout" }],
            event_count: 1,
            started_at: NOW,
            updated_at: NOW,
            version_id: null,
          });
        }
        return undefined;
      });
      renderTopic();
      const region = await screen.findByRole("region", { name: "第 2 轮" });
      expect(within(region).getByText("运行中断")).toBeTruthy();
      expect(await within(region).findByText("模型调用失败 · 请求超时")).toBeTruthy();
      expect(await within(region).findByText(/^连续多次发生模型单次调用超时/)).toBeTruthy();
      expect(within(region).queryByRole("button", { name: "继续研究" })).toBeNull();
      expect(within(region).queryByRole("button", { name: "交付已有成果" })).toBeNull();
      expect(within(region).getByRole("button", { name: "重新提问" })).toBeTruthy();
      fireEvent.click(within(region).getByRole("button", { name: "更多" }));
      const menu = await screen.findByRole("menu");
      expect(within(menu).getByRole("menuitem", { name: "添加资料" })).toBeTruthy();
      expect(within(menu).queryByRole("menuitem", { name: "补充缺口" })).toBeNull();
    });
  });

  describe("S1 continuation & multi-turn live streaming fixes", () => {
    it("1) shows continuation LiveTurn without duplicate bubble, hides old partial card actions, and survives refresh", async () => {
      const partial = turn(1, { status: "partial", reason: "no_submission", gaps: [{ code: "no_cited_claims", message: "x" }] });
      const encoder = new TextEncoder();
      let streamController: ReadableStreamDefaultController<Uint8Array> | null = null;
      const body = new ReadableStream<Uint8Array>({ start(c) { streamController = c; } });
      let continued = false;

      mockApi((url, init) => {
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([partial]));
        if (url.pathname === "/v2/research/topics/t1/runs") {
          return jsonResponse(
            continued ? [operation("op_3", "running"), operation("op_1", "failed")] : [operation("op_1", "failed")],
          );
        }
        if (url.pathname.endsWith("/op_1/recovery-status")) {
          return jsonResponse({ can_continue: true, can_deliver: false, reason: null, checkpoint: "tool_batch", version_id: "v2" });
        }
        if (url.pathname.endsWith("/op_1/continue") && init.method === "POST") {
          continued = true;
          return jsonResponse(operation("op_3", "pending"), { status: 202 });
        }
        if (url.pathname.endsWith("/op_3/events")) {
          return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
        }
        return undefined;
      });
      const sendFrame = (event: string, data: unknown, id?: string) => {
        streamController!.enqueue(encoder.encode(`${id ? `id: ${id}\n` : ""}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
      };

      const { unmount } = renderTopic();
      const continueBtn = await screen.findByRole("button", { name: "继续研究" });
      await waitFor(() => expect((continueBtn as HTMLButtonElement).disabled).toBe(false));
      fireEvent.click(continueBtn);

      const contRegion = await screen.findByRole("region", { name: "第 1 轮继续研究" });
      expect(within(contRegion).getByText("正在继续研究")).toBeTruthy();
      expect(screen.getAllByText("问题 1")).toHaveLength(1);
      const oldRegion = screen.getByRole("region", { name: "第 1 轮" });
      expect(within(oldRegion).queryByRole("button", { name: "继续研究" })).toBeNull();
      expect(within(oldRegion).queryByRole("button", { name: "重新提问" })).toBeNull();

      await waitFor(() => expect(streamController).toBeTruthy());
      sendFrame("progress.snapshot", {
        operation_id: "op_3",
        operation_status: "running",
        turn: { index: 1, question: "问题 1" },
        usage: { model_calls: 1, search_calls: 0, total_tokens: 150 },
        interim: null,
        current: { kind: "model", phase: "thinking", retry_attempt: null, started_at: NOW },
        events: [],
        event_count: 0,
        started_at: NOW,
        updated_at: NOW,
        version_id: null,
      }, "cp:0");
      sendFrame("stream.delta", { operation_id: "op_3", call_seq: 1, channel: "thinking", delta: "续跑思考轨迹。", seq: 0 });
      sendFrame("stream.delta", { operation_id: "op_3", call_seq: 1, channel: "draft_report", delta: "续跑未核验草稿。", seq: 1 });

      expect(await within(contRegion).findByText("续跑思考轨迹。")).toBeTruthy();
      expect(within(contRegion).getByText("续跑未核验草稿。")).toBeTruthy();
      expect(screen.getAllByText("问题 1")).toHaveLength(1);
      unmount();

      // Simulate hard refresh while op_3 is active (no pendingContinue local state).
      let refreshCtrl: ReadableStreamDefaultController<Uint8Array> | null = null;
      const refreshBody = new ReadableStream<Uint8Array>({ start(c) { refreshCtrl = c; } });
      mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([partial]));
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_3", "running"), operation("op_1", "failed")]);
        if (url.pathname.endsWith("/op_3/events")) {
          return new Response(refreshBody, { status: 200, headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
        }
        return undefined;
      });
      renderTopic();
      const refreshRegion = await screen.findByRole("region", { name: "第 1 轮继续研究" });
      expect(within(refreshRegion).getByText("正在继续研究")).toBeTruthy();
      expect(screen.getAllByText("问题 1")).toHaveLength(1);
      await waitFor(() => expect(refreshCtrl).toBeTruthy());
      refreshCtrl!.enqueue(encoder.encode(`id: cp:0\nevent: progress.snapshot\ndata: ${JSON.stringify({
        operation_id: "op_3",
        operation_status: "running",
        turn: { index: 1, question: "问题 1" },
        usage: { model_calls: 1, search_calls: 0, total_tokens: 150 },
        interim: null,
        current: { kind: "model", phase: "thinking", retry_attempt: null, started_at: NOW },
        events: [],
        event_count: 0,
        started_at: NOW,
        updated_at: NOW,
        version_id: null,
      })}\n\nevent: stream.delta\ndata: ${JSON.stringify({ operation_id: "op_3", call_seq: 1, channel: "thinking", delta: "重连思考恢复。", seq: 0 })}\n\n`));
      expect(await within(refreshRegion).findByText("重连思考恢复。")).toBeTruthy();
    });

    it("2) shows continuation deliver mode labels on partial turn '交付已有成果'", async () => {
      const partial = turn(1, { status: "partial", reason: "no_submission", gaps: [{ code: "no_cited_claims", message: "x" }] });
      const encoder = new TextEncoder();
      let streamController: ReadableStreamDefaultController<Uint8Array> | null = null;
      const body = new ReadableStream<Uint8Array>({ start(c) { streamController = c; } });
      let delivered = false;

      mockApi((url, init) => {
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([partial]));
        if (url.pathname === "/v2/research/topics/t1/runs") {
          return jsonResponse(
            delivered ? [operation("op_3", "pending"), operation("op_1", "failed")] : [operation("op_1", "failed")],
          );
        }
        if (url.pathname.endsWith("/op_1/recovery-status")) {
          return jsonResponse({ can_continue: false, can_deliver: true, reason: null, checkpoint: "submission", version_id: "v2" });
        }
        if (url.pathname.endsWith("/op_1/recover") && init.method === "POST") {
          delivered = true;
          return jsonResponse(operation("op_3", "pending"), { status: 202 });
        }
        if (url.pathname.endsWith("/op_3/events")) {
          return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
        }
        return undefined;
      });

      renderTopic();
      const deliverBtn = await screen.findByRole("button", { name: "交付已有成果" });
      await waitFor(() => expect((deliverBtn as HTMLButtonElement).disabled).toBe(false));
      fireEvent.click(deliverBtn);

      const contRegion = await screen.findByRole("region", { name: "第 1 轮继续研究" });
      expect(within(contRegion).getByText("排队中 · 交付已有成果")).toBeTruthy();

      await waitFor(() => expect(streamController).toBeTruthy());
      streamController!.enqueue(encoder.encode(`id: cp:0\nevent: progress.snapshot\ndata: ${JSON.stringify({
        operation_id: "op_3",
        operation_status: "running",
        turn: { index: 1, question: "问题 1" },
        usage: { model_calls: 1, search_calls: 0, total_tokens: 90 },
        interim: null,
        current: { kind: "model", phase: "responding", retry_attempt: null, started_at: NOW },
        events: [],
        event_count: 0,
        started_at: NOW,
        updated_at: NOW,
        version_id: null,
      })}\n\n`));
      expect(await within(contRegion).findByText("正在交付已有成果")).toBeTruthy();
    });

    it("3) mounts Turn 2 LiveTurn immediately without flicker when continuing an orphanRun", async () => {
      let bodyCtrl: ReadableStreamDefaultController<Uint8Array> | null = null;
      const body = new ReadableStream<Uint8Array>({ start(c) { bodyCtrl = c; } });
      let continued = false;

      mockApi((url, init) => {
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([turn(1)]));
        if (url.pathname === "/v2/research/topics/t1/runs") {
          return jsonResponse(
            continued
              ? [operation("op_3", "running"), operation("op_2", "failed"), operation("op_1", "completed")]
              : [operation("op_2", "failed"), operation("op_1", "completed")],
          );
        }
        if (url.pathname.endsWith("/op_2/recovery-status")) {
          return jsonResponse({ can_continue: true, can_deliver: false, reason: null, checkpoint: "tool_batch", version_id: "v2" });
        }
        if (url.pathname.endsWith("/op_2/events")) return jsonResponse({ error_code: "method_not_allowed", message: "fallback" }, { status: 405 });
        if (url.pathname.endsWith("/op_2/progress")) {
          return jsonResponse({
            operation_id: "op_2",
            operation_status: "failed",
            turn: { index: 2, question: "失败的追问问题" },
            usage: { model_calls: 1, search_calls: 0, total_tokens: 100 },
            current: null,
            events: [],
            event_count: 0,
            started_at: NOW,
            updated_at: NOW,
            version_id: null,
          });
        }
        if (url.pathname.endsWith("/op_2/continue") && init.method === "POST") {
          continued = true;
          return jsonResponse(operation("op_3", "running"), { status: 202 });
        }
        if (url.pathname.endsWith("/op_3/events")) {
          return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
        }
        return undefined;
      });

      renderTopic();
      const orphanRegion = await screen.findByRole("region", { name: "第 2 轮" });
      expect(await within(orphanRegion).findByText("失败的追问问题")).toBeTruthy();
      const continueBtn = within(orphanRegion).getByRole("button", { name: "继续研究" });
      await waitFor(() => expect((continueBtn as HTMLButtonElement).disabled).toBe(false));
      fireEvent.click(continueBtn);

      // Even before op_3/events pushes progress.snapshot, LiveTurn for Turn 2 mounts immediately.
      await waitFor(() => expect(bodyCtrl).toBeTruthy());
      const liveRegion = await screen.findByRole("region", { name: "第 2 轮" });
      expect(within(liveRegion).getByText("正在研究")).toBeTruthy();
      expect(within(liveRegion).getByText("失败的追问问题")).toBeTruthy();
      expect(screen.queryByRole("region", { name: /继续研究/ })).toBeNull();
    });

    it("4) settles pendingAsk by operationId cleanly on Turn 2 follow-up regardless of clock skew and preserves live process", async () => {
      const encoder = new TextEncoder();
      let streamCtrl: ReadableStreamDefaultController<Uint8Array> | null = null;
      const body = new ReadableStream<Uint8Array>({ start(c) { streamCtrl = c; } });
      let started = false;

      mockApi((url, init) => {
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([turn(1)]));
        if (url.pathname === "/v2/research/topics/t1/runs" && init.method === "POST") {
          started = true;
          return jsonResponse(operation("op_9", "pending"), { status: 202 });
        }
        if (url.pathname === "/v2/research/topics/t1/runs") {
          if (!started) return jsonResponse([operation("op_1", "completed")]);
          const base = operation("op_9", "running");
          const op9 = { ...base, operation: { ...base.operation, created_at: "2020-01-01T00:00:00Z" } };
          return jsonResponse([op9, operation("op_1", "completed")]);
        }
        if (url.pathname.endsWith("/op_9/events")) {
          return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
        }
        return undefined;
      });

      const { container } = renderTopic();
      await screen.findByRole("region", { name: "第 1 轮" });
      const scroller = container.querySelector('[data-page="TopicPage"]') as HTMLDivElement;
      const scrollToSpy = vi.fn();
      scroller.scrollTo = scrollToSpy;
      Object.defineProperty(scroller, "scrollHeight", { value: 1200, configurable: true });
      Object.defineProperty(scroller, "clientHeight", { value: 400, configurable: true });
      Object.defineProperty(scroller, "scrollTop", { value: 0, configurable: true, writable: true });

      const composer = screen.getByRole("textbox", { name: "追问" });
      fireEvent.change(composer, { target: { value: "第二轮新追问" } });
      fireEvent.keyDown(composer, { key: "Enter", ctrlKey: true });

      const liveRegion = await screen.findByRole("region", { name: "第 2 轮" });
      expect(within(liveRegion).getByText("第二轮新追问")).toBeTruthy();
      expect(screen.queryByRole("button", { name: "回到实时过程" })).toBeNull();
      await waitFor(() => expect(scrollToSpy).toHaveBeenCalled());
      await waitFor(() => expect(streamCtrl).toBeTruthy());

      streamCtrl!.enqueue(encoder.encode(`id: cp:0\nevent: progress.snapshot\ndata: ${JSON.stringify({
        operation_id: "op_9",
        operation_status: "running",
        turn: { index: 2, question: "第二轮新追问" },
        usage: { model_calls: 1, search_calls: 0, total_tokens: 100 },
        interim: null,
        current: { kind: "model", phase: "thinking", retry_attempt: null, started_at: NOW },
        events: [],
        event_count: 0,
        started_at: NOW,
        updated_at: NOW,
        version_id: null,
      })}\n\nevent: stream.delta\ndata: ${JSON.stringify({ operation_id: "op_9", call_seq: 1, channel: "thinking", delta: "追问实时思考。", seq: 0 })}\n\n`));

      expect(await within(liveRegion).findByText("追问实时思考。")).toBeTruthy();
      expect(screen.getAllByText("第二轮新追问")).toHaveLength(1);
    });

    it("5) streams live thinking and tool status inside the right RecordPanel StepsTab before committed rows arrive", async () => {
      const encoder = new TextEncoder();
      let streamCtrl: ReadableStreamDefaultController<Uint8Array> | null = null;
      const body = new ReadableStream<Uint8Array>({ start(c) { streamCtrl = c; } });

      mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([turn(1)]));
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_2", "running"), operation("op_1", "completed")]);
        if (url.pathname.endsWith("/op_2/events")) {
          return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
        }
        return undefined;
      });

      renderTopic();
      await waitFor(() => expect(streamCtrl).toBeTruthy());
      streamCtrl!.enqueue(encoder.encode(`id: cp:0\nevent: progress.snapshot\ndata: ${JSON.stringify({
        operation_id: "op_2",
        operation_status: "running",
        turn: { index: 2, question: "侧栏流式测试" },
        usage: { model_calls: 1, search_calls: 0, total_tokens: 50 },
        interim: null,
        current: null,
        events: [],
        event_count: 0,
        started_at: NOW,
        updated_at: NOW,
        version_id: null,
      })}\n\nevent: stream.delta\ndata: ${JSON.stringify({ operation_id: "op_2", call_seq: 1, channel: "thinking", delta: "面板中的实时思考", seq: 0 })}\n\nevent: stream.delta\ndata: ${JSON.stringify({ operation_id: "op_2", call_seq: 1, channel: "tool_status", delta: "面板正在起草报告…", seq: 1 })}\n\n`));

      fireEvent.click(await screen.findByTitle("打开过程面板"));
      const dialog = await screen.findByRole("dialog");
      expect(await within(dialog).findByText("面板中的实时思考")).toBeTruthy();
      expect(within(dialog).getByText("面板正在起草报告…")).toBeTruthy();
      expect(within(dialog).queryByText("这一轮还没有记录步骤。")).toBeNull();
    });

    it("6) prevents double rendering via liveCommitted when topic commits op_2 before runs cache drops activeRun", async () => {
      const encoder = new TextEncoder();
      let streamCtrl: ReadableStreamDefaultController<Uint8Array> | null = null;
      const body = new ReadableStream<Uint8Array>({ start(c) { streamCtrl = c; } });
      let topicCommitted = false;

      mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") {
          return jsonResponse(
            topicWith(topicCommitted ? [turn(1), turn(2, { operation_ids: ["op_2"], report: "已落盘正式报告。" })] : [turn(1)]),
          );
        }
        // Intentionally keep returning op_2 as running even after topic refreshes.
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_2", "running"), operation("op_1", "completed")]);
        if (url.pathname.endsWith("/op_2/events")) {
          return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
        }
        return undefined;
      });

      const { client } = renderTopic();
      await waitFor(() => expect(streamCtrl).toBeTruthy());
      streamCtrl!.enqueue(encoder.encode(`id: cp:0\nevent: progress.snapshot\ndata: ${JSON.stringify({
        operation_id: "op_2",
        operation_status: "running",
        turn: { index: 2, question: "问题 2" },
        usage: { model_calls: 1, search_calls: 0, total_tokens: 100 },
        interim: null,
        current: { kind: "model", phase: "responding", retry_attempt: null, started_at: NOW },
        events: [],
        event_count: 0,
        started_at: NOW,
        updated_at: NOW,
        version_id: null,
      })}\n\n`));

      expect(await screen.findByText("正在研究")).toBeTruthy();

      topicCommitted = true;
      await client.invalidateQueries({ queryKey: queryKeys.topic("t1") });

      expect(await screen.findByText("已落盘正式报告。")).toBeTruthy();
      await waitFor(() => expect(screen.queryByText("正在研究")).toBeNull());
      expect(screen.getAllByRole("region", { name: "第 2 轮" })).toHaveLength(1);
    });
  });

  describe("M2 restricted GFM Markdown rendering", () => {
    it("renders GFM report tables, code blocks, safe links, inline notes/claims, and streaming Markdown drafts without historical TurnView rerenders", async () => {
      const durationSpy = vi.spyOn(researchLib, "turnDurationMs");
      const encoder = new TextEncoder();
      let streamCtrl: ReadableStreamDefaultController<Uint8Array> | null = null;
      const body = new ReadableStream<Uint8Array>({ start(c) { streamCtrl = c; } });

      const richClaim: ResearchV2.ResearchClaim = {
        claim_id: "C_MD",
        role: "support",
        text: "**云服务收入**由 $42.0B 增至 `$130.5B`",
        turn_id: "turn_1",
        citations: [{ source_id: "s1", quotation: QUOTE, location: { character_start: START, character_end: END } }],
      };
      const richTurn1 = turn(1, {
        claim_ids: ["C_MD"],
        events: [
          { kind: "model", usage: 500, usage_detail: { input_tokens: 400, output_tokens: 100 }, finish_reason: "tool_calls", note: "优先核对 **10-K 附注**" },
          { kind: "submission", status: "completed" },
        ],
        report: [
          "# 收入核验报告",
          "",
          "| 期间 | 收入 | 同比 |",
          "| --- | ---: | ---: |",
          "| FY2025 | $42.0B | — |",
          "| FY2026 | $130.5B | +114.2% |",
          "",
          "```python",
          "growth = (130.5 / 42.0 - 1) * 100",
          "```",
          "",
          "参考 [SEC 原文](https://www.sec.gov/Archives/edgar/data/1/a.htm) ![pixel](https://evil.com/pixel.png)",
        ].join("\n"),
      });

      mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([richTurn1], [richClaim]));
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_2", "running"), operation("op_1", "completed")]);
        if (url.pathname.endsWith("/op_2/events")) {
          return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
        }
        return undefined;
      });

      try {
        const { container } = renderTopic();
        const turn1Region = await screen.findByRole("region", { name: "第 1 轮" });
        expect(within(turn1Region).getByRole("heading", { level: 2, name: "收入核验报告" })).toBeTruthy();
        expect(turn1Region.querySelector("h1")).toBeNull();
        expect(turn1Region.querySelector("img")).toBeNull();
        expect(within(turn1Region).getByText("[图片：pixel]")).toBeTruthy();
        expect(turn1Region.querySelectorAll("td.md-num").length).toBeGreaterThanOrEqual(4);
        const extLink = within(turn1Region).getByRole("link", { name: /SEC 原文/ }) as HTMLAnchorElement;
        expect(extLink.href).toBe("https://www.sec.gov/Archives/edgar/data/1/a.htm");
        expect(within(turn1Region).getByText("云服务收入").tagName).toBe("STRONG");
        fireEvent.click(within(turn1Region).getByRole("button", { name: "依据 1 条" }));
        expect(within(turn1Region).getByRole("button", { name: "引用 1" })).toBeTruthy();

        // Expand collapsed process summary and confirm inline Markdown in model.note.
        fireEvent.click(within(turn1Region).getByRole("button", { name: "过程 1 步" }));
        expect(within(turn1Region).getByText("10-K 附注").tagName).toBe("STRONG");

        await waitFor(() => expect(streamCtrl).toBeTruthy());
        streamCtrl!.enqueue(encoder.encode(`id: cp:0\nevent: progress.snapshot\ndata: ${JSON.stringify({
          operation_id: "op_2",
          operation_status: "running",
          turn: { index: 2, question: "Markdown 草稿测试" },
          usage: { model_calls: 1, search_calls: 0, total_tokens: 100 },
          interim: null,
          current: { kind: "model", phase: "responding", retry_attempt: null, started_at: NOW },
          events: [],
          event_count: 0,
          started_at: NOW,
          updated_at: NOW,
          version_id: null,
        })}\n\n`));

        const liveRegion = await screen.findByRole("region", { name: "第 2 轮" });
        const turn1CallsBeforeDraft = durationSpy.mock.calls.length;
        streamCtrl!.enqueue(encoder.encode(`event: stream.delta\ndata: ${JSON.stringify({
          operation_id: "op_2", call_seq: 1, channel: "draft_report", delta: "## 草稿对比\n\n| 指标 | 数值 |\n| --- | ---: |\n| 收入 | **$130.5B** |", seq: 0,
        })}\n\n`));

        const draftBox = await within(liveRegion).findByRole("region", { name: "未核验报告草稿预览" });
        expect(await within(draftBox).findByRole("heading", { level: 2, name: "草稿对比" })).toBeTruthy();
        expect(within(draftBox).getByText("$130.5B").tagName).toBe("STRONG");
        expect(draftBox.querySelector("table")).toBeTruthy();
        expect(container.querySelector("img")).toBeNull();
        expect(durationSpy.mock.calls.length).toBe(turn1CallsBeforeDraft);
      } finally {
        durationSpy.mockRestore();
      }
    });
  });

  describe("V1 evidence-bound charts in AnswerBlock", () => {
    it("renders [[chart:...]] inline, auto-places unreferenced charts after the report and key numbers, ignores invalid anchors, and opens ReaderPanel on provenance click", async () => {
      const cCombo: ResearchV2.ResearchChart = {
        chart_id: "c_combo",
        turn_id: "turn_1",
        chart_type: "combo_bar_line",
        title: "内联营业收入与利润率图",
        subtitle: "左轴：USD millions · 右轴：percent",
        x_label: "财年",
        axes: [
          { id: "left", label: "总收入", unit: "USD millions", currency: "USD", scale: "millions" },
          { id: "right", label: "利润率", unit: "percent", currency: null, scale: "ones" },
        ],
        series: [
          {
            name: "总收入",
            mark: "bar",
            y_axis_id: "left",
            is_projected: false,
            points: [
              {
                x: "FY2026",
                y: "48612",
                display_value: "USD 48,612 millions",
                is_projected: false,
                provenance: { fact_id: "f1", source_id: "s1", character_start: START, character_end: END },
              },
            ],
          },
          {
            name: "利润率",
            mark: "line",
            y_axis_id: "right",
            is_projected: false,
            points: [
              {
                x: "FY2026",
                y: "31.07",
                display_value: "31.07%",
                is_projected: false,
                provenance: { calculation_id: "calc_1", source_id: "s1", character_start: START, character_end: END },
              },
            ],
          },
        ],
        is_simulated: false,
        has_projected_points: false,
        replaces_chart_id: null,
        source_ids: ["s1"],
      };

      const cPrice: ResearchV2.ResearchChart = {
        chart_id: "c_price",
        turn_id: "turn_1",
        chart_type: "price_series",
        title: "文末自动排布股价走势图",
        subtitle: "单位：USD",
        x_label: "交易日",
        axes: [{ id: "left", label: "收盘价", unit: "USD", currency: "USD", scale: "ones" }],
        series: [
          {
            name: "NWD",
            mark: "area",
            y_axis_id: "left",
            is_projected: false,
            points: [
              {
                x: "2026-08-04",
                y: "136.2",
                display_value: "USD 136.2",
                is_projected: false,
                provenance: { source_id: "s1", character_start: START, character_end: END },
              },
            ],
          },
        ],
        is_simulated: false,
        has_projected_points: false,
        replaces_chart_id: null,
        source_ids: ["s1"],
      };

      const chartTurn = turn(1, {
        claim_ids: ["C1"],
        calculation_ids: ["calc_1"],
        chart_ids: ["c_combo", "c_price"],
        report: [
          "## 核心分析",
          "",
          "[[chart:c_combo]]",
          "",
          "[[chart:bogus_missing_chart]]",
          "",
          "```text",
          "[[chart:c_price]]",
          "```",
          "",
          "报告结尾段落。",
        ].join("\n"),
      });

      const calc: ResearchV2.ResearchCalculation = {
        calculation_id: "calc_1",
        turn_id: "turn_1",
        formula_id: "margin_percent",
        formula_version: "1",
        expression: "15104 / 48612 * 100",
        value: "31.07",
        unit: "percent",
        currency: null,
        decimal_places: 2,
        rounding: "half_up",
        inputs: [],
        input_verification: "not_verified_by_calculator",
        input_precision_status: "not_established",
      };

      const topicData: ResearchV2.ResearchTopic = {
        topic_id: "t1",
        owner_id: "acc_1",
        created_at: NOW,
        updated_at: NOW,
        request: { question: "Northwind 示例公司的收入增长有多快？", source_ids: ["s1"] },
        versions: [
          {
            version_id: "v2",
            created_at: NOW,
            created_by: "agent",
            review: "draft",
            source_ids: ["s1"],
            content: {
              turns: [chartTurn],
              claims: [{ ...CLAIMS[0]!, turn_id: "turn_1" }],
              calculations: [calc],
              charts: [cCombo, cPrice],
              sources: [],
            },
          },
        ],
      };

      mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicData);
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([]);
        if (url.pathname === "/v2/research/topics/t1/versions/v2/sources") {
          return jsonResponse([
            {
              status: "available",
              source_id: "s1",
              title: "Northwind 2026 年报",
              kind: "user_supplied",
              is_simulated: false,
              published_at: NOW,
              event_at: null,
              revised_at: null,
              obtained_at: NOW,
              content_sha256: "sha1234567890",
              retrieval_method: "user_supplied",
              pdf_text_order: null,
              derived_from_source_id: null,
              permissions: { store: true, model_input: true, display: true, export: true },
              verification: "user_declared_not_verified",
            },
          ]);
        }
        if (url.pathname === "/v2/research/topics/t1/versions/v2/sources/s1/text") {
          return jsonResponse({ source_id: "s1", text: TEXT, content_sha256: "sha1234567890" });
        }
        return undefined;
      });

      renderTopic();
      const turnRegion = await screen.findByRole("region", { name: "第 1 轮" });

      // 1) Inline chart appears inside .report .md-chart-slot exactly once; trailing chart appears in .turn-charts between .report and .stats.
      const inlineFigures = within(turnRegion.querySelector(".report") as HTMLElement).getAllByRole("figure");
      expect(inlineFigures).toHaveLength(1);
      expect(inlineFigures[0]!.getAttribute("aria-label")).toBe("内联营业收入与利润率图");

      const trailingFigures = within(turnRegion.querySelector(".turn-charts") as HTMLElement).getAllByRole("figure");
      expect(trailingFigures).toHaveLength(1);
      expect(trailingFigures[0]!.getAttribute("aria-label")).toBe("文末自动排布股价走势图");

      // Total figures = 2 (no duplication), invalid anchor silently ignored, fenced code preserves literal anchor.
      expect(within(turnRegion).getAllByRole("figure")).toHaveLength(2);
      expect(within(turnRegion).queryByText("[[chart:bogus_missing_chart]]")).toBeNull();
      expect(turnRegion.querySelector("pre")?.textContent).toContain("[[chart:c_price]]");

      // Ordering (spec v6): key numbers lead, then the report body, then trailing charts.
      const reportEl = turnRegion.querySelector(".report")!;
      const trailingEl = turnRegion.querySelector(".turn-charts")!;
      const statsEl = turnRegion.querySelector(".stats")!;
      expect(reportEl.compareDocumentPosition(trailingEl) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(statsEl.compareDocumentPosition(trailingEl) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

      // 2) Switch to table view and click provenance to open ReaderPanel highlighting exact source quotation.
      const comboFigure = screen.getByRole("figure", { name: "内联营业收入与利润率图" });
      fireEvent.click(within(comboFigure).getByRole("button", { name: "数据表" }));
      fireEvent.click(within(comboFigure).getByRole("button", { name: /USD 48,612 millions/ }));

      const readerPanel = await screen.findByRole("dialog", { name: "Northwind 2026 年报" });
      await waitFor(() => {
        const activeMark = readerPanel.querySelector("mark.on");
        expect(activeMark?.textContent).toBe(QUOTE);
      });
      expect(readerPanel.querySelectorAll("mark.on").length).toBeGreaterThanOrEqual(1);
    });
  });

  describe("research actions: row menus and copying", () => {
    function stubClipboard() {
      const writeText = vi.fn().mockResolvedValue(undefined);
      Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
      return writeText;
    }

    it("opens the same row menu from a right-click and copies the research link", async () => {
      const writeText = stubClipboard();
      const topic = topicWith([turn(1)]);
      mockApi((url) => {
        if (url.pathname === "/v2/research/topics") return jsonResponse([topic]);
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topic);
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
        return undefined;
      });
      renderTopic();
      const nav = await screen.findByRole("navigation", { name: "研究列表" });
      const row = await within(nav).findByRole("link", { name: /Northwind 示例公司/ });
      // The visible “···” trigger exists for every row; the context menu is only a shortcut to it.
      expect(within(nav).getByRole("button", { name: /的操作$/ })).toBeTruthy();
      fireEvent.contextMenu(row);
      fireEvent.click(await screen.findByRole("menuitem", { name: "复制链接" }));
      await waitFor(() => expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/research/t1`));
      expect(await screen.findByText("已复制链接")).toBeTruthy();
    });

    it("opens the row menu from the keyboard", async () => {
      const topic = topicWith([turn(1)]);
      mockApi((url) => {
        if (url.pathname === "/v2/research/topics") return jsonResponse([topic]);
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topic);
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
        return undefined;
      });
      renderTopic();
      const nav = await screen.findByRole("navigation", { name: "研究列表" });
      fireEvent.keyDown(await within(nav).findByRole("link", { name: /Northwind 示例公司/ }), { key: "F10", shiftKey: true });
      expect(await screen.findByRole("menuitem", { name: "导出完整记录" })).toBeTruthy();
    });

    it("copies an answer as Markdown with citation numbers and sources, and copies a question", async () => {
      const writeText = stubClipboard();
      const completed = turn(1, { claim_ids: ["C1"], report: "披露收入 [[claim:C1]]。" });
      mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([completed], [CLAIMS[0]!]));
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
        return undefined;
      });
      renderTopic();
      const region = await screen.findByRole("region", { name: "第 1 轮" });
      fireEvent.click(within(region).getByRole("button", { name: "复制回答" }));
      await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
      const copied = writeText.mock.calls[0]![0] as string;
      expect(copied).toContain("披露收入 [1]。");
      expect(copied).toContain("## 来源\n[1] 《");
      fireEvent.click(within(region).getByRole("button", { name: "复制问题" }));
      await waitFor(() => expect(writeText).toHaveBeenLastCalledWith("问题 1"));
    });
  });
});
