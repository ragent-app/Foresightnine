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
  it("shows the saved requirement and conditional review with a source link in the research record", async () => {
    const saved = turn(1, {
      claim_ids: ["C1"], report: "收入有条件成立。",
      requirement_plan: [{ requirement_id: "req", revision: 1, instruction_sequence: 0, source: { sequence: 0, excerpt: "收入" }, deliverable: "核对收入", evidence_needed: "年报", priority: "core", assumptions: ["以美元计"] }],
      semantic_reviews: [{ requirement_id: "req", claim_id: "C1", verdict: "conditional", reason: "需要对齐期间。", condition: "同一财年", review_key: "key", evidence: [{ source_id: "s1", content_sha256: "a".repeat(64), character_start: START, character_end: END, quotation: QUOTE }] }],
    });
    mockApi((url) => {
      if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([saved], CLAIMS));
      if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
      if (url.pathname.endsWith("/sources/s1/text")) return jsonResponse({ text: TEXT, content_sha256: "a".repeat(64) });
      return undefined;
    });
    renderTopic();
    // Requirement reviews live in this turn's record, under 过程.
    fireEvent.click(await screen.findByRole("button", { name: "本轮记录" }));
    fireEvent.click(await within(await screen.findByRole("dialog")).findByRole("tab", { name: "过程" }));
    const records = await screen.findByRole("region", { name: "问题要求与依据核验" });
    expect(within(records).getByText("核对收入", { exact: true })).toBeTruthy();
    expect(within(records).getByText("成立条件：同一财年")).toBeTruthy();
    fireEvent.click(within(records).getByRole("button", { name: "查看原文" }));
    expect(await screen.findByText(QUOTE, { selector: "mark" })).toBeTruthy();
  });

  it("shows the clarification and sends the answer to the same turn before continuing", async () => {
    const fetchMock = mockApi((url, init) => {
      if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([turn(1, { status: "partial", reason: "awaiting_user", partial_notes: "已有分析" })]));
      if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([{ ...operation("op_1", "completed"), turn_id: "turn_1", logical_status: "awaiting_user" }]);
      if (url.pathname.endsWith("/op_1/events")) return jsonResponse({}, { status: 405 });
      if (url.pathname.endsWith("/op_1/progress")) return jsonResponse({ operation_id: "op_1", operation_status: "completed", logical_status: "awaiting_user", current_operation_id: "op_1", turn: { turn_id: "turn_1", index: 1, question: "问题 1" }, clarification: { question: "何时支付学费？", why_it_matters: "日期影响流动性判断。", requirement_id: "payment" }, instructions: [], usage: { model_calls: 1, search_calls: 0, total_tokens: 10 }, current: null, events: [], event_count: 0, started_at: NOW, updated_at: NOW, version_id: "v2" });
      if (url.pathname.endsWith("/turn_1/instructions") && init.method === "POST") return jsonResponse({ instruction_id: "i1", turn_id: "turn_1", operation_id: "op_1", sequence: 1, text: "明年六月", received_at: NOW, status: "received" }, { status: 201 });
      if (url.pathname.endsWith("/op_1/continue") && init.method === "POST") return jsonResponse(operation("op_2", "pending"), { status: 202 });
      return undefined;
    });
    renderTopic();
    expect(await screen.findByText("何时支付学费？")).toBeTruthy();
    expect(within(screen.getByRole("region", { name: "第 1 轮" })).getByText("等待你的补充")).toBeTruthy();
    expect(within(screen.getByRole("region", { name: "第 1 轮" })).queryByText("已完成")).toBeNull();
    expect(screen.getByText("日期影响流动性判断。")).toBeTruthy();
    fireEvent.change(screen.getByPlaceholderText("补充这项信息…"), { target: { value: "明年六月" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/op_1/continue"))).toBe(true));
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/turn_1/instructions"))).toBe(true);
  });

  it("titles the page with the summary and keeps the first question as a bubble", async () => {
    const titled = { ...topicWith([turn(1, { question: "Northwind 示例公司的收入增长有多快？", report: "收入增长 18%。" })]), title: "Northwind 收入增速" };
    mockApi((url) => {
      if (url.pathname === "/v2/research/topics/t1") return jsonResponse(titled);
      if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
      return undefined;
    });
    renderTopic();
    expect(await screen.findByRole("heading", { level: 1, name: "Northwind 收入增速" })).toBeTruthy();
    const first = screen.getByRole("region", { name: "第 1 轮" });
    expect(within(first).getByText("Northwind 示例公司的收入增长有多快？")).toBeTruthy();
    expect(within(first).getByRole("button", { name: "复制问题" })).toBeTruthy();
  });

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
    // Completion is shown only for a saved final answer.
    expect(within(turn2).queryByText("已完成")).toBeNull();
    // The challenging claim stays visible; supporting claims sit in the collapsed evidence.
    expect(within(turn2).getByText("待复核")).toBeTruthy();
    // Every part is answered, so the parts are not repeated under the answer.
    expect(within(turn2).queryByRole("region", { name: "问题各部分的进展" })).toBeNull();
    expect(within(turn2).queryByRole("button", { name: /还差/ })).toBeNull();
    fireEvent.click(within(turn2).getByRole("button", { name: "依据 1" }));
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
    expect(within(turn1).getByText("研究限制")).toBeTruthy();
    expect(within(turn1).getByText("还没有引文位置已核实的结论。请添加或读取相关资料后继续研究。")).toBeTruthy();
    expect(within(turn1).queryByRole("button", { name: "继续研究" })).toBeNull();
  });

  it("does not ask the user to continue or deliver a saved partial result", async () => {
    const partial = turn(1, { status: "partial", reason: "no_submission", gaps: [{ code: "no_cited_claims", message: "x" }] });
    const fetchMock = mockApi((url) => {
      if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([partial]));
      if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "failed")]);
      if (url.pathname.endsWith("/op_1/recovery-status")) return jsonResponse({ can_continue: true, can_deliver: true, reason: null, checkpoint: "tool_batch", version_id: "v2" });
      return undefined;
    });
    renderTopic();
    expect(await screen.findByText("研究限制")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "继续研究" })).toBeNull();
    expect(screen.queryByRole("button", { name: "交付已有成果" })).toBeNull();
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/continue"))).toBe(false);
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
    // One status line (with the stage) and one line of what is happening now; counts, stages and
    // the model's notes are in the process panel the status line opens.
    expect(within(liveRegion).getByText("正在研究")).toBeTruthy();
    expect(within(liveRegion).getByText(/读资料/)).toBeTruthy();
    expect(within(liveRegion).getByText("读取年报附注")).toBeTruthy();
    expect(within(liveRegion).queryByText(/发现 3 条线索/)).toBeNull();
    expect(within(liveRegion).queryByText("先检查年报附注中的收入明细。")).toBeNull();
    fireEvent.click(within(liveRegion).getByRole("button", { name: /正在研究/ }));
    const panel = await screen.findByRole("dialog");
    expect(await within(panel).findByText("先检查年报附注中的收入明细。")).toBeTruthy();
    fireEvent.click(within(panel).getByRole("button", { name: "关闭" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    const composer = screen.getByRole("textbox", { name: "追问" }) as HTMLTextAreaElement;
    expect(composer.disabled).toBe(false);
    expect(composer.placeholder).toBe("追加研究要求…");
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
      expect(within(region).getByText("研究限制")).toBeTruthy();
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
      // The process is not repeated under the answer; it is in this turn's record.
      expect(within(region).queryByText("首先搜索公司官方披露。")).toBeNull();
      fireEvent.click(within(region).getByRole("button", { name: "本轮记录" }));
      const dialog = await screen.findByRole("dialog");
      fireEvent.click(within(dialog).getByRole("tab", { name: "过程" }));
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
      expect(within(region).queryByText("模型调用 · 400 输入 / 100 输出 Token")).toBeNull();
      fireEvent.click(within(region).getByRole("button", { name: "本轮记录" }));
      const dialog = await screen.findByRole("dialog");
      fireEvent.click(within(dialog).getByRole("tab", { name: "过程" }));
      expect(within(dialog).getByText("旧版读取")).toBeTruthy();
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
      // The live turn says what the current step is for; notes and targets are in the process panel.
      expect(await within(liveRegion).findByText("搜索年报")).toBeTruthy();

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

      expect(await within(liveRegion).findByText("精读收入附注")).toBeTruthy();
      // Tool-level detail lives in the process panel, one click from the live turn.
      expect(await within(liveRegion).findByText("精读收入附注")).toBeTruthy();
      fireEvent.click(within(liveRegion).getByRole("button", { name: /正在研究/ }));
      const processPanel = await screen.findByRole("dialog");
      expect(await within(processPanel).findByText("发现 2 条线索（可用 1 · 受限 1）")).toBeTruthy();
      expect(within(processPanel).getAllByText("先通过 SSE 检索年报。")).toHaveLength(1);
      expect(within(processPanel).getByText("《Northwind 10-K》0–8,000 字")).toBeTruthy();
      fireEvent.click(within(processPanel).getByRole("button", { name: "关闭" }));
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

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
      await waitFor(() => expect(screen.queryByTitle("查看完整过程")).toBeNull());
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
      expect(await within(liveRegion).findByText("<img src=x onerror=alert(1)>")).toBeTruthy();
      fireEvent.click(within(liveRegion).getByRole("button", { name: /正在研究/ }));
      const panel = await screen.findByRole("dialog");
      expect(await within(panel).findByText("<b>加粗不生效</b>")).toBeTruthy();
      expect(within(panel).getByText("<script>alert(2)</script>")).toBeTruthy();
      fireEvent.click(within(panel).getByRole("button", { name: "展开线索 (1)" }));
      expect(within(panel).getByText("<svg onload=alert(3)>")).toBeTruthy();
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

        // Raw model output is not on the page; it streams in the process panel the status line opens.
        expect(within(liveRegion).queryByText("模型思考过程 · 非正式结论")).toBeNull();
        fireEvent.click(within(liveRegion).getByRole("button", { name: /正在研究/ }));
        const panel = await screen.findByRole("dialog");
        expect(await within(panel).findByText("模型思考过程 · 非正式结论")).toBeTruthy();
        expect(within(panel).getByText("思考步骤A。思考步骤B。")).toBeTruthy();
        expect(within(panel).getByText("正在核对财报数据。")).toBeTruthy();
        expect(durationSpy.mock.calls.length).toBe(turn1CallsBeforeThinking);

        // Manual collapse toggle works and stays collapsed during the same call_seq.
        fireEvent.click(within(panel).getByRole("button", { name: "收起模型思考过程" }));
        expect(within(panel).queryByText(/思考步骤A/)).toBeNull();
        sendFrame("stream.delta", { operation_id: "op_2", call_seq: 1, channel: "thinking", delta: "思考步骤C。", seq: 3 });
        await new Promise((resolve) => setTimeout(resolve, 25));
        expect(within(panel).queryByText(/思考步骤C/)).toBeNull();

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
        await waitFor(() => expect(within(panel).queryByText("模型思考过程 · 非正式结论")).toBeNull());
        expect(await within(panel).findByText("《Northwind 年报》")).toBeTruthy();
        fireEvent.click(within(panel).getByRole("button", { name: "关闭" }));
        await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
        expect(within(liveRegion).getByText("阅读原文")).toBeTruthy();

        // Draft report bursts (25 deltas) render inside dashed preview WITHOUT re-rendering historical Turn 1.
        const turn1CallsBeforeDraftBurst = durationSpy.mock.calls.length;
        sendFrame("stream.delta", { operation_id: "op_2", call_seq: 2, channel: "tool_status", delta: "正在起草研究报告并整理引文，完成后将自动核验原文位置…", seq: 0 });
        for (let i = 1; i <= 25; i += 1) {
          sendFrame("stream.delta", { operation_id: "op_2", call_seq: 2, channel: "draft_report", delta: `草稿句${i} `, seq: i });
        }
        const draftBox = await within(liveRegion).findByRole("region", { name: "报告草稿" });
        expect(within(draftBox).getByText("正在写报告 · 写完后会核对引文和数字")).toBeTruthy();
        expect(await within(draftBox).findByText(/草稿句25/)).toBeTruthy();
        expect(durationSpy.mock.calls.length).toBe(turn1CallsBeforeDraftBurst);

        // submission_rejected keeps the draft readable while it is revised, with a notice.
        sendFrame("stream.reset", { operation_id: "op_2", call_seq: 2, reason: "submission_rejected" });
        expect(await within(liveRegion).findByText("核验发现需要修改的地方，正在修改这份草稿…")).toBeTruthy();
        expect(within(liveRegion).getByRole("region", { name: "报告草稿" })).toBeTruthy();
        expect(within(liveRegion).getByText(/草稿句25/)).toBeTruthy();

        // The revised draft replaces it and clears the notice; run.terminal transitions to verified AnswerBlock.
        sendFrame("stream.delta", { operation_id: "op_2", call_seq: 3, channel: "draft_report", delta: "修正后的草稿段落。", seq: 0 });
        expect(await within(liveRegion).findByText("修正后的草稿段落。")).toBeTruthy();
        expect(within(liveRegion).queryByText("核验发现需要修改的地方，正在修改这份草稿…")).toBeNull();
        expect(within(liveRegion).queryByText(/草稿句25/)).toBeNull();

        completed = true;
        sendFrame("run.terminal", { operation_id: "op_2", operation_status: "completed", version_id: "v2" }, "done:completed");
        streamController!.close();

        expect(await screen.findByText("正式核验完成报告。")).toBeTruthy();
        await waitFor(() => expect(screen.queryByRole("region", { name: "报告草稿" })).toBeNull());
      } finally {
        durationSpy.mockRestore();
      }
    });

    it("keeps the submitted draft until the saved partial turn replaces it", async () => {
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
      // The run is not over yet: the draft stays where the answer will appear.
      await new Promise((resolve) => setTimeout(resolve, 30));
      expect(screen.getByRole("region", { name: "报告草稿" })).toBeTruthy();

      sendFrame("run.terminal", { operation_id: "op_2", operation_status: "failed", version_id: "v2" }, "done:failed");
      streamController!.close();

      expect(await screen.findByText("研究限制")).toBeTruthy();
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
      expect(within(region).getByText("研究限制")).toBeTruthy();
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
      expect(within(region).getByText("研究限制")).toBeTruthy();
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
      let continued = true;

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
      expect(screen.queryByRole("button", { name: "继续研究" })).toBeNull();

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

      expect(await within(contRegion).findByText("续跑未核验草稿。")).toBeTruthy();
      fireEvent.click(await within(contRegion).findByRole("button", { name: /正在继续研究/ }));
      const contPanel = await screen.findByRole("dialog");
      expect(await within(contPanel).findByText("续跑思考轨迹。")).toBeTruthy();
      fireEvent.click(within(contPanel).getByRole("button", { name: "关闭" }));
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
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
      fireEvent.click(await within(refreshRegion).findByRole("button", { name: /正在继续研究/ }));
      expect(await within(await screen.findByRole("dialog")).findByText("重连思考恢复。")).toBeTruthy();
    });

    it("2) shows continuation deliver mode labels on partial turn '交付已有成果'", async () => {
      const partial = turn(1, { status: "partial", reason: "no_submission", gaps: [{ code: "no_cited_claims", message: "x" }] });
      const encoder = new TextEncoder();
      let streamController: ReadableStreamDefaultController<Uint8Array> | null = null;
      const body = new ReadableStream<Uint8Array>({ start(c) { streamController = c; } });
      let delivered = true;

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
      expect(screen.queryByRole("button", { name: "交付已有成果" })).toBeNull();

      const contRegion = await screen.findByRole("region", { name: "第 1 轮继续研究" });
      expect(within(contRegion).getByText("排队中 · 继续研究")).toBeTruthy();

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
      expect(await within(contRegion).findByText("正在继续研究")).toBeTruthy();
    });

    it("3) mounts Turn 2 LiveTurn immediately without flicker when continuing an orphanRun", async () => {
      let bodyCtrl: ReadableStreamDefaultController<Uint8Array> | null = null;
      const body = new ReadableStream<Uint8Array>({ start(c) { bodyCtrl = c; } });
      let continued = true;

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
      expect(screen.queryByRole("button", { name: "继续研究" })).toBeNull();
      await waitFor(() => expect(bodyCtrl).toBeTruthy());
      bodyCtrl!.enqueue(new TextEncoder().encode(`event: progress.snapshot\ndata: ${JSON.stringify({ operation_id: "op_3", operation_status: "running", logical_status: "running", turn: { turn_id: "turn_2", index: 2, question: "失败的追问问题" }, usage: { model_calls: 1, search_calls: 0, total_tokens: 100 }, current: null, events: [], event_count: 0, started_at: NOW, updated_at: NOW, version_id: null })}\n\n`));
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

      fireEvent.click(await within(liveRegion).findByRole("button", { name: /正在研究/ }));
      expect(await within(await screen.findByRole("dialog")).findByText("追问实时思考。")).toBeTruthy();
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

      fireEvent.click(await screen.findByTitle("查看完整过程"));
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

      // The report also leads the page as the current conclusion once there are two turns.
      expect((await screen.findAllByText("已落盘正式报告。")).length).toBeGreaterThan(0);
      await waitFor(() => expect(screen.queryByText("正在研究")).toBeNull());
      expect(screen.getAllByRole("region", { name: "第 2 轮" })).toHaveLength(1);
    });

    it("keeps a finished run and its draft on screen until the saved answer replaces it in place", async () => {
      const encoder = new TextEncoder();
      let streamCtrl: ReadableStreamDefaultController<Uint8Array> | null = null;
      const body = new ReadableStream<Uint8Array>({ start(controller) { streamCtrl = controller; } });
      let saved = false;
      let finished = false;
      mockApi((url) => {
        if (url.pathname === "/v2/research/topics/t1") {
          return jsonResponse(topicWith(saved ? [turn(1), turn(2, { operation_ids: ["op_2"], report: "核验后的报告。" })] : [turn(1)]));
        }
        if (url.pathname === "/v2/research/topics/t1/runs") {
          return jsonResponse([operation("op_2", finished ? "completed" : "running"), operation("op_1", "completed")]);
        }
        if (url.pathname.endsWith("/op_2/events")) {
          return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
        }
        return undefined;
      });
      const frame = (event: string, data: unknown, id?: string) =>
        streamCtrl!.enqueue(encoder.encode(`${id ? `id: ${id}\n` : ""}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
      const { client } = renderTopic();
      await waitFor(() => expect(streamCtrl).toBeTruthy());
      frame("progress.snapshot", {
        operation_id: "op_2", operation_status: "running", turn: { index: 2, question: "问题 2" },
        usage: { model_calls: 1, search_calls: 0, total_tokens: 100 }, interim: null,
        current: { kind: "model", phase: "responding", retry_attempt: null, started_at: NOW },
        events: [], event_count: 0, started_at: NOW, updated_at: NOW, version_id: null,
      }, "cp:0");
      frame("stream.delta", { operation_id: "op_2", call_seq: 1, channel: "draft_report", delta: "正在写的报告。", seq: 0 });
      expect(await screen.findByText("正在写的报告。")).toBeTruthy();

      frame("stream.reset", { operation_id: "op_2", call_seq: 1, reason: "call_completed" });
      finished = true;
      frame("run.terminal", { operation_id: "op_2", operation_status: "completed", version_id: "v2" }, "done:completed");
      streamCtrl!.close();

      // Finished but not yet saved here: the turn stays, with its draft, and says so.
      expect(await screen.findByText("正在保存结果")).toBeTruthy();
      expect(screen.getByText("正在写的报告。")).toBeTruthy();

      saved = true;
      await client.invalidateQueries({ queryKey: queryKeys.topic("t1") });
      expect((await screen.findAllByText("核验后的报告。")).length).toBeGreaterThan(0);
      await waitFor(() => expect(screen.queryByText("正在保存结果")).toBeNull());
      expect(screen.queryByText("正在写的报告。")).toBeNull();
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
        fireEvent.click(within(turn1Region).getByRole("button", { name: "依据 1" }));
        expect(within(turn1Region).getByRole("button", { name: "引用 1" })).toBeTruthy();

        // Model notes keep their inline Markdown in the turn's process record.
        fireEvent.click(within(turn1Region).getByRole("button", { name: "本轮记录" }));
        const processPanel = await screen.findByRole("dialog");
        fireEvent.click(within(processPanel).getByRole("tab", { name: "过程" }));
        expect(within(processPanel).getByText("10-K 附注").tagName).toBe("STRONG");
        fireEvent.click(within(processPanel).getByRole("button", { name: "关闭" }));
        await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

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

        const draftBox = await within(liveRegion).findByRole("region", { name: "报告草稿" });
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
          "报告结尾段落，收入增长 31.07%。",
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

  describe("research actions: regenerate, edit and resend, restore", () => {
    function runBodies(fetchMock: ReturnType<typeof vi.fn>) {
      return fetchMock.mock.calls
        .filter(([input, init]) => String(input).endsWith("/v2/research/topics/t1/runs") && (init as RequestInit | undefined)?.method === "POST")
        .map(([, init]) => JSON.parse(String((init as RequestInit).body)) as Record<string, unknown>);
    }
    function api(topic: ResearchV2.ResearchTopic, extra?: (url: URL, init: RequestInit) => Response | undefined) {
      return mockApi((url, init) => {
        const handled = extra?.(url, init);
        if (handled) return handled;
        if (url.pathname === "/v2/research/topics/t1/runs" && init.method === "POST") return jsonResponse(operation("op_9", "pending"), { status: 202 });
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topic);
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
        return undefined;
      });
    }

    it("regenerates the latest answer from the version before it", async () => {
      const fetchMock = api(topicWith([turn(1)]));
      renderTopic();
      const region = await screen.findByRole("region", { name: "第 1 轮" });
      fireEvent.click(within(region).getByRole("button", { name: "重新生成" }));
      await waitFor(() => expect(runBodies(fetchMock)).toHaveLength(1));
      const body = runBodies(fetchMock)[0]!;
      expect(body.replace_turn_id).toBe("turn_1");
      expect(body.parent_version_id).toBe("v2");
      expect("question_override" in body).toBe(false);
      expect("discard_review_edits" in body).toBe(false);
    });

    it("edits the latest question in place and resends it", async () => {
      const fetchMock = api(topicWith([turn(1)]));
      renderTopic();
      const region = await screen.findByRole("region", { name: "第 1 轮" });
      fireEvent.click(within(region).getByRole("button", { name: "编辑问题" }));
      const editor = within(region).getByRole("textbox", { name: "编辑问题" });
      fireEvent.change(editor, { target: { value: "改过的问题" } });
      fireEvent.click(within(region).getByRole("button", { name: "发送" }));
      await waitFor(() => expect(runBodies(fetchMock)).toHaveLength(1));
      expect(runBodies(fetchMock)[0]).toMatchObject({ replace_turn_id: "turn_1", question_override: "改过的问题" });
    });

    it("asks before discarding review edits made after the answer", async () => {
      const base = topicWith([turn(1)]);
      const edited: ResearchV2.ResearchTopic = {
        ...base,
        versions: [...base.versions, { version_id: "v3", parent_version_id: "v2", created_at: NOW, created_by: "user", review: "draft",
          content: { ...base.versions[1]!.content, summary: "我的判断" } }],
      };
      const fetchMock = api(edited);
      renderTopic();
      const region = await screen.findByRole("region", { name: "第 1 轮" });
      fireEvent.click(within(region).getByRole("button", { name: "重新生成" }));
      const dialog = await screen.findByRole("alertdialog");
      expect(runBodies(fetchMock)).toHaveLength(0);
      fireEvent.click(within(dialog).getByRole("button", { name: "放弃修改并重新生成" }));
      await waitFor(() => expect(runBodies(fetchMock)).toHaveLength(1));
      expect(runBodies(fetchMock)[0]).toMatchObject({ replace_turn_id: "turn_1", discard_review_edits: true, parent_version_id: "v3" });
    });

    it("restores the answer a regeneration replaced", async () => {
      const old = turn(1);
      const regenerated = { ...turn(1, { report: "新回答。" }), turn_id: "turn_1b", replaces_turn_id: "turn_1", parent_version_id: "v1" };
      const topic: ResearchV2.ResearchTopic = {
        ...topicWith([old]),
        versions: [
          ...topicWith([old]).versions,
          { version_id: "v3", parent_version_id: "v2", created_at: NOW, created_by: "agent", review: "draft", content: { turns: [regenerated], claims: [], sources: [] } },
        ],
      };
      const fetchMock = api(topic, (url) => (url.pathname === "/v2/research/topics/t1/versions/v2/restore" ? jsonResponse(topicWith([old]), { status: 201 }) : undefined));
      renderTopic();
      const region = await screen.findByRole("region", { name: "第 1 轮" });
      fireEvent.click(within(region).getByRole("button", { name: "更多回答操作" }));
      expect(await screen.findByRole("menuitem", { name: "查看上一次的回答" })).toBeTruthy();
      fireEvent.click(await screen.findByRole("menuitem", { name: "恢复上一次的回答" }));
      await waitFor(() => {
        const call = fetchMock.mock.calls.find(([input]) => String(input).endsWith("/versions/v2/restore"));
        expect(call).toBeTruthy();
        expect(JSON.parse(String((call![1] as RequestInit).body))).toEqual({ parent_version_id: "v3" });
      });
      expect(await screen.findByText("已恢复上一次的回答")).toBeTruthy();
    });
  });

  describe("research actions: fork", () => {
    function forkApi() {
      const original = topicWith([turn(1), turn(2)]);
      const forked: ResearchV2.ResearchTopic = {
        ...topicWith([turn(1)]), topic_id: "t9", title: "Northwind 示例公司的收入增长有多快？（分支）",
        forked_from: { topic_id: "t1", version_id: "v2", turn_id: "turn_1", turn_index: 1 },
      };
      return mockApi((url, init) => {
        if (url.pathname === "/v2/research/topics") return jsonResponse([original]);
        if (url.pathname === "/v2/research/topics/t1/fork" && init.method === "POST") return jsonResponse(forked, { status: 201 });
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(original);
        if (url.pathname === "/v2/research/topics/t9") return jsonResponse(forked);
        if (url.pathname.endsWith("/runs")) return jsonResponse([operation("op_1", "completed")]);
        return undefined;
      });
    }
    const forkBodies = (fetchMock: ReturnType<typeof vi.fn>) => fetchMock.mock.calls
      .filter(([input]) => String(input).endsWith("/v2/research/topics/t1/fork"))
      .map(([, init]) => JSON.parse(String((init as RequestInit).body)) as Record<string, unknown>);

    it("forks from an earlier answer and opens the new research", async () => {
      const fetchMock = forkApi();
      renderTopic();
      const region = await screen.findByRole("region", { name: "第 1 轮" });
      fireEvent.click(within(region).getByRole("button", { name: "更多回答操作" }));
      fireEvent.click(await screen.findByRole("menuitem", { name: "从这里分叉" }));
      await waitFor(() => expect(forkBodies(fetchMock)).toEqual([{ version_id: "v2", turn_id: "turn_1" }]));
      expect(await screen.findByRole("link", { name: /分叉自《Northwind 示例公司的收入增长有多快？》\s*第 1 轮/ })).toBeTruthy();
      expect(await screen.findByText("已创建分支")).toBeTruthy();
    });

    it("forks a whole research from its row menu", async () => {
      const fetchMock = forkApi();
      renderTopic();
      const nav = await screen.findByRole("navigation", { name: "研究列表" });
      fireEvent.contextMenu(await within(nav).findByRole("link", { name: /Northwind 示例公司/ }));
      fireEvent.click(await screen.findByRole("menuitem", { name: "分叉为新研究" }));
      await waitFor(() => expect(forkBodies(fetchMock)).toEqual([{ version_id: "v2" }]));
    });
  });

  describe("research actions: rename, pin, delete", () => {
    function lifecycleApi() {
      let topic = topicWith([turn(1)]);
      const other = { ...topicWith([turn(1)]), topic_id: "t2", request: { question: "另一项研究", source_ids: [] } };
      const fetchMock = mockApi((url, init) => {
        if (url.pathname === "/v2/research/topics") return jsonResponse([topic, other]);
        if (url.pathname === "/v2/research/topics/t2" && init.method === "PATCH") {
          const body = JSON.parse(String(init.body)) as { title?: string; pinned?: boolean };
          topic = other;
          return jsonResponse({ ...other, ...(body.title !== undefined ? { title: body.title } : {}), ...(body.pinned ? { pinned_at: NOW } : {}) });
        }
        if (url.pathname === "/v2/research/topics/t2" && init.method === "DELETE") return jsonResponse({ topic_id: "t2", deleted_at: NOW, purge_after: NOW });
        if (url.pathname === "/v2/research/topics/t2/restore") return jsonResponse(other);
        if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topic);
        if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
        return undefined;
      });
      return fetchMock;
    }
    const calls = (fetchMock: ReturnType<typeof vi.fn>, method: string) =>
      fetchMock.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === method);

    it("renames a research in place from its row menu", async () => {
      const fetchMock = lifecycleApi();
      renderTopic();
      const nav = await screen.findByRole("navigation", { name: "研究列表" });
      fireEvent.contextMenu(await within(nav).findByRole("link", { name: /^另一项研究(?:\s|$)/ }));
      fireEvent.click(await screen.findByRole("menuitem", { name: "重命名" }));
      const field = await within(nav).findByRole("textbox", { name: "研究名称" });
      fireEvent.change(field, { target: { value: "云厂商对比" } });
      fireEvent.keyDown(field, { key: "Enter" });
      await waitFor(() => expect(calls(fetchMock, "PATCH")).toHaveLength(1));
      expect(JSON.parse(String((calls(fetchMock, "PATCH")[0]![1] as RequestInit).body))).toEqual({ title: "云厂商对比" });
      expect(await within(nav).findByRole("link", { name: /^云厂商对比(?:\s|$)/ })).toBeTruthy();
    });

    it("pins a research into its own group", async () => {
      const fetchMock = lifecycleApi();
      renderTopic();
      const nav = await screen.findByRole("navigation", { name: "研究列表" });
      fireEvent.contextMenu(await within(nav).findByRole("link", { name: /^另一项研究(?:\s|$)/ }));
      fireEvent.click(await screen.findByRole("menuitem", { name: "置顶" }));
      await waitFor(() => expect(calls(fetchMock, "PATCH")).toHaveLength(1));
      expect(JSON.parse(String((calls(fetchMock, "PATCH")[0]![1] as RequestInit).body))).toEqual({ pinned: true });
      expect(await within(nav).findByText("置顶")).toBeTruthy();
    });

    it("deletes after confirmation and restores from the undo toast", async () => {
      const fetchMock = lifecycleApi();
      renderTopic();
      const nav = await screen.findByRole("navigation", { name: "研究列表" });
      fireEvent.contextMenu(await within(nav).findByRole("link", { name: /^另一项研究(?:\s|$)/ }));
      fireEvent.click(await screen.findByRole("menuitem", { name: "删除…" }));
      const dialog = await screen.findByRole("alertdialog");
      expect(within(dialog).getByText(/30 天内可以撤销删除/)).toBeTruthy();
      fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
      await waitFor(() => expect(calls(fetchMock, "DELETE")).toHaveLength(1));
      await waitFor(() => expect(within(nav).queryByRole("link", { name: /^另一项研究(?:\s|$)/ })).toBeNull());
      fireEvent.click(await screen.findByRole("button", { name: "撤销" }));
      await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("/v2/research/topics/t2/restore"))).toBe(true));
      expect(await within(nav).findByRole("link", { name: /^另一项研究(?:\s|$)/ })).toBeTruthy();
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

describe("research turn instructions", () => {
  it("uses the account snapshot for other topics and waits for the final version before showing completion", async () => {
    const other = { ...topicWith([turn(1)]), topic_id: "t2", request: { question: "另一个研究", source_ids: [] } };
    let status = { topic_id: "t2", turn_id: "other", current_operation_id: "op_other", logical_status: "running", version_id: null as string | null };
    mockApi((url) => {
      if (url.pathname === "/v2/research/topics") return jsonResponse([other]);
      if (url.pathname === "/v2/research/turn-statuses") return jsonResponse([status]);
      if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([]));
      if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([]);
      return undefined;
    });
    const { client } = renderTopic();
    const link = await screen.findByRole("link", { name: /另一个研究/ });
    await waitFor(() => expect(link.textContent).toContain("研究中"));
    status = { ...status, logical_status: "completed", version_id: "not-yet-readable" };
    await client.invalidateQueries({ queryKey: ["research-turn-statuses"] });
    await waitFor(() => expect(link.textContent).toContain("研究中"));
    status = { ...status, version_id: "v2" };
    await client.invalidateQueries({ queryKey: ["research-turn-statuses"] });
    await waitFor(() => expect(link.textContent).not.toContain("研究中"));
  });

  it("marks a final result with documented limits completed and preserves a stopped result", async () => {
    const limited: ResearchV2.ResearchTurn = { ...turn(1, { status: "partial", gaps: [
      { code: "claims_omitted_unverified_location", message: "x" },
      { code: "model_reported", message: "缺少同日发行在外股数。" },
    ] }), lifecycle_status: "completed" };
    const stopped: ResearchV2.ResearchTurn = { ...turn(2, { status: "partial", reason: "provider_cancelled" }), lifecycle_status: "stopped" };
    mockApi((url) => {
      if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([limited, stopped]));
      if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_2", "cancelled")]);
      return undefined;
    });
    renderTopic();
    const completed = within(await screen.findByRole("region", { name: "第 1 轮" }));
    // A final result needs no label; its stated limit reads beside it and the rest is one tap away.
    expect(completed.queryByText("已完成")).toBeNull();
    expect(completed.getByText("缺少同日发行在外股数。")).toBeTruthy();
    expect(completed.queryByText("研究限制")).toBeNull();
    expect(completed.queryByRole("button", { name: "重新提问" })).toBeNull();
    // With no report to read, the limits are already open.
    expect(completed.getByRole("button", { name: "限制" }).getAttribute("aria-expanded")).toBe("true");
    expect(completed.getByText("部分结论因引文位置无法核实而未保留。")).toBeTruthy();
    expect(within(screen.getByRole("region", { name: "第 2 轮" })).getByText("已停止")).toBeTruthy();
  });

  it("shows saved instruction disposition and lets the reader locate its evidence", async () => {
    const saved: ResearchV2.ResearchTurn = { ...turn(1, { report: "有来源的回答。", claim_ids: ["C1"] }), user_instructions: [{ instruction_id: "i1", turn_id: "turn_1", operation_id: "op_1", sequence: 1, text: "核对口径", received_at: NOW, status: "included", delivery_status: "linked_for_review", reason: "与收入披露相关", references: [{ claim_id: "C1" }] }, { instruction_id: "i2", turn_id: "turn_1", operation_id: "op_1", sequence: 2, text: "采用新口径", received_at: NOW, status: "included", delivery_status: "supersession_needs_review", text_locations: [{ field: "report", instruction_sequence: 1, start: 0, end: 3, excerpt: "原文内容" }] }] };
    mockApi((url) => {
      if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([saved], [{ ...CLAIMS[0]!, turn_id: "turn_1" }]));
      if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
      return undefined;
    });
    renderTopic();
    expect(await screen.findByText("核对口径")).toBeTruthy();
    expect(screen.getByText(/已关联依据，待核实 · 与收入披露相关/)).toBeTruthy();
    expect(screen.getByText("已定位原文，取代关系待核验")).toBeTruthy();
    fireEvent.click(screen.getByText("查看相关原文"));
    expect(screen.getByText("报告 · 指令 1：原文内容")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "查看结论 · C1" }));
    expect(await screen.findByText("总收入为 48,612 百万美元。", { exact: false })).toBeTruthy();
  });

  it("keeps the draft on an uncertain write, retries with the same key, shows receipts and stops the turn", async () => {
    const receipts: object[] = [];
    let attempts = 0;
    let stopped = false;
    const mock = mockApi((url, init) => {
      if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([]));
      if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_live", "running")]);
      if (url.pathname.endsWith("/op_live/events")) return jsonResponse({}, { status: 405 });
      if (url.pathname.endsWith("/op_live/progress")) return jsonResponse({ operation_id: "op_live", operation_status: "running", logical_status: "running", current_operation_id: "op_live", turn: { turn_id: "turn_live", index: 1, question: "研究问题" }, instructions: receipts, usage: { model_calls: 0, search_calls: 0, total_tokens: 0 }, current: null, events: [], event_count: 0, started_at: NOW, updated_at: NOW, version_id: null });
      if (url.pathname.endsWith("/turn_live/instructions")) {
        attempts++;
        if (attempts === 1) return jsonResponse({ message: "temporary" }, { status: 503 });
        const receipt = { instruction_id: "i1", turn_id: "turn_live", operation_id: "op_live", sequence: 1, text: "核对两端股数", received_at: NOW, status: "received" };
        receipts.push(receipt);
        return jsonResponse(receipt, { status: 201 });
      }
      if (url.pathname.endsWith("/turn_live/stop")) { stopped = true; return jsonResponse({ turn_id: "turn_live", status: "stopped" }); }
      return undefined;
    });
    renderTopic();
    await screen.findByText("研究问题");
    const input = screen.getByRole("textbox", { name: "追问" }) as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "核对两端股数" } });
    fireEvent.keyDown(input, { key: "Enter", isComposing: true });
    expect(attempts).toBe(0);
    fireEvent.keyDown(input, { key: "Enter" });
    await screen.findByText(/发送未确认/);
    expect(input.value).toBe("核对两端股数");
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await screen.findByText("已收到");
    expect(input.value).toBe("");
    const calls = mock.mock.calls.filter(([url]) => String(url).endsWith("/turn_live/instructions"));
    expect(calls).toHaveLength(2);
    expect((calls[0]![1]!.headers as Record<string, string>)["Idempotency-Key"]).toBe((calls[1]![1]!.headers as Record<string, string>)["Idempotency-Key"]);
    fireEvent.click(screen.getByRole("button", { name: "停止研究" }));
    await waitFor(() => expect(stopped).toBe(true));
    const stopCall = mock.mock.calls.find(([url]) => String(url).endsWith("/turn_live/stop"));
    expect(JSON.parse(String(stopCall![1]!.body))).toEqual({});
    expect((stopCall![1]!.headers as Record<string, string>)["Content-Type"]).toMatch(/application\/json/);
  });

  it("hydrates locked earnings squad dossier + collapsed OrgGraph, caches Deep Report, and opens ReaderPanel from citations", async () => {
    const sector: ResearchV2.SectorApplicability = {
      sic_code: "3674",
      sic_description: "Semiconductors",
      sector_category: "non_financial",
      is_gated: false,
      disabled_metrics: [],
      recommended_alternatives: [],
      explanation: "Non-financial tech issuer.",
    };
    const squadEvents: ResearchV2.OrgGraphEvent[] = [
      { sequence: 1, node_id: "prefetch", role: "prefetch", display_name: "官方资料与确定性预计算", action_kind: "READING", runtime_state: "completed", stage: "stage_0", current_activity: "完成 10-Q/EX-99.1 预抓取" },
      { sequence: 2, node_id: "lead", role: "lead", display_name: "Lead 主研究协调", parent_node_id: "prefetch", slot: "deep_reasoner", action_kind: "DELEGATING", runtime_state: "completed", stage: "stage_1", current_activity: "已派发并行 Worker" },
      { sequence: 3, node_id: "worker_forensics", role: "worker_forensics", display_name: "财务质量与排雷小组", parent_node_id: "lead", slot: "fast_extractor", action_kind: "INSPECTING", runtime_state: "completed", stage: "stage_2", current_activity: "完成 AR/FCF 核查" },
      { sequence: 4, node_id: "bear_reviewer", role: "bear_reviewer", display_name: "独立空头审查", parent_node_id: "lead", slot: "bear_reviewer", action_kind: "INSPECTING", runtime_state: "completed", stage: "stage_3", current_activity: "通过证据锚定反证" },
      { sequence: 5, node_id: "writer", role: "writer", display_name: "底稿撰稿 (Writer)", parent_node_id: "bear_reviewer", slot: "writer", action_kind: "RESPONDING", runtime_state: "completed", stage: "stage_4", current_activity: "已生成锁定 Memo" },
    ];
    const baseDossier: ResearchV2.EarningsDossier = {
      dossier_id: "dossier_nvda_q2",
      run_id: "sqrun_done",
      entity_id: "cik:0001045810",
      symbol: "NVDA",
      cik: "0001045810",
      company_name: "NVIDIA Corp.",
      period_label: "Q2 FY26",
      locked: true,
      dossier_sha256: "a".repeat(64),
      sector_applicability: sector,
      gate_report: { passed: true, exportable: true },
      slot_usage_summary: { total_calls: 6, total_tokens: 18500, stage_0_model_calls: 0, deep_report_tool_calls: 0, slots: {} },
      memo_markdown: "## 买方摘要\n\n云端与数据中心需求延续高景气。",
      deep_report_markdown: null,
      evidence_cards: [
        {
          card_id: "card_rev",
          worker_role: "worker_segments",
          topic_tag: "cloud_revenue",
          finding: "云服务收入贡献主要增长。",
          citations: [{ source_id: "s1", quotation: QUOTE, character_start: START, character_end: END, location: `${START}-${END}` }],
        },
      ],
      bear_cases: [
        {
          bear_id: "bear_ar",
          anchor_kind: "flag",
          anchored_evidence_ids: ["card_rev"],
          severity: "high",
          thesis_challenge: "应收增速显著快于营收，需警惕渠道压货。",
          next_quarter_invalidation_trigger: "下季 DSO 回落至 45 天以下",
        },
      ],
      open_gaps: [],
      snapshot: {
        entity_id: "cik:0001045810",
        symbol: "NVDA",
        cik: "0001045810",
        company_name: "NVIDIA Corp.",
        period_label: "Q2 FY26",
        sector_applicability: sector,
        prefetched_sources: [{ source_id: "s1", title: "NVDA 10-Q Q2 FY26", retrieval_method: "sec_filing", kind: "official", content_sha256: "a".repeat(64), permissions: { display: true, export: true } }],
        statements: [
          {
            period_label: "Q2 FY26",
            period_type: "quarter",
            currency: "USD",
            monetary_unit: "USD millions",
            revenue: { value: "48612", unit: "USD millions", currency: "USD" },
            free_cash_flow: { value: "14500", unit: "USD millions", currency: "USD" },
          },
        ],
        diagnostics: {
          entity_id: "cik:0001045810",
          symbol: "NVDA",
          period_label: "Q2 FY26",
          monetary_unit: "USD millions",
          currency: "USD",
          sector_applicability: sector,
          flag_counts: { red: 1, yellow: 0, green: 0 },
          thresholds_reference: {},
          margin_bridge: [],
          earnings_quality: {
            reported_or_derived_fcf: { name: "fcf", status: "computed", value: "14500", unit: "USD millions", currency: "USD", formula: "ocf-capex" },
            ocf_to_ni: { name: "ocf_to_ni", status: "computed", value: "1.15", unit: "ratio", formula: "ocf/ni" },
            fcf_to_ni: { name: "fcf_to_ni", status: "computed", value: "1.05", unit: "ratio", formula: "fcf/ni" },
            fcf_margin_pct: { name: "fcf_margin_pct", status: "computed", value: "29.8", unit: "percent", formula: "fcf/rev" },
            sbc_to_revenue_pct: { name: "sbc_to_revenue_pct", status: "computed", value: "3.2", unit: "percent", formula: "sbc/rev" },
            sbc_to_fcf_pct: { name: "sbc_to_fcf_pct", status: "computed", value: "10.7", unit: "percent", formula: "sbc/fcf" },
            fcf_ex_sbc: { name: "fcf_ex_sbc", status: "computed", value: "12945", unit: "USD millions", currency: "USD", formula: "fcf-sbc" },
            fcf_ex_sbc_margin_pct: { name: "fcf_ex_sbc_margin_pct", status: "computed", value: "26.6", unit: "percent", formula: "(fcf-sbc)/rev" },
          },
          working_capital: {
            ar_yoy_growth_pct: { name: "ar_yoy_growth_pct", status: "computed", value: "38", unit: "percent", formula: "yoy" },
            revenue_yoy_growth_pct: { name: "revenue_yoy_growth_pct", status: "computed", value: "20", unit: "percent", formula: "yoy" },
            ar_vs_revenue_growth_pp: { name: "ar_vs_revenue_growth_pp", status: "computed", value: "18", unit: "pp", formula: "ar-rev" },
            inventory_yoy_growth_pct: { name: "inv", status: "unavailable", value: null, unit: "percent", formula: "yoy" },
            cogs_yoy_growth_pct: { name: "cogs", status: "unavailable", value: null, unit: "percent", formula: "yoy" },
            inventory_vs_cogs_growth_pp: { name: "inv_cogs", status: "unavailable", value: null, unit: "pp", formula: "diff" },
            inventory_vs_revenue_growth_pp: { name: "inv_rev", status: "unavailable", value: null, unit: "pp", formula: "diff" },
            inventory_turnover: { current: { name: "inv_turn", status: "unavailable", value: null, unit: "ratio", formula: "x" } },
            dso: { current: { name: "dso", status: "computed", value: "48", unit: "days", formula: "dso" } },
            dio: { current: { name: "dio", status: "unavailable", value: null, unit: "days", formula: "dio" } },
            dpo: { current: { name: "dpo", status: "unavailable", value: null, unit: "days", formula: "dpo" } },
            ccc: { current: { name: "ccc", status: "unavailable", value: null, unit: "days", formula: "ccc" } },
          },
          capital_allocation: {
            capex_to_revenue_pct: { name: "capex", status: "computed", value: "4.0", unit: "percent", formula: "x" },
            capital_returned: { name: "ret", status: "computed", value: "3000", unit: "USD millions", formula: "x" },
            payout_to_fcf_pct: { name: "payout", status: "computed", value: "20.7", unit: "percent", formula: "x" },
            diluted_shares_yoy_pct: { name: "shares", status: "computed", value: "-0.8", unit: "percent", formula: "x" },
          },
          balance_sheet: {
            position: "net_cash",
            total_cash: { name: "cash", status: "computed", value: "30000", unit: "USD millions", formula: "x" },
            total_debt: { name: "debt", status: "computed", value: "9000", unit: "USD millions", formula: "x" },
            net_debt: { name: "nd", status: "computed", value: "-21000", unit: "USD millions", formula: "x" },
            net_cash: { name: "nc", status: "computed", value: "21000", unit: "USD millions", formula: "x" },
            net_debt_to_ttm_ebitda: { name: "nd_ebitda", status: "computed", value: "-0.5", unit: "multiple", formula: "x" },
            interest_coverage: { name: "ic", status: "computed", value: "42", unit: "multiple", formula: "x" },
            current_ratio: { name: "cr", status: "computed", value: "3.4", unit: "ratio", formula: "x" },
          },
          flags: [
            {
              flag_id: "wc_ar_gap",
              group: "working_capital",
              severity: "red",
              metric_name: "ar_vs_revenue_growth_pp",
              observed_value: "18.0",
              unit: "pp",
              threshold_rule: "> +15pp",
              summary: "AR增速高于营收18pp",
              investigation_prompt: "核查信用政策变化",
            },
          ],
        },
        valuation: null,
        guidance_nongaap: null,
        redline_summary: null,
      },
    };
    const deepDossier: ResearchV2.EarningsDossier = {
      ...baseDossier,
      deep_report_markdown: "## 完整深研报告全文\n\n基于锁定底稿按需生成，零新增检索调用。",
    };
    let deepCalls = 0;

    mockApi((url, init) => {
      if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([turn(1, { report: "基础对话结论。" })]));
      if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_1", "completed")]);
      if (url.pathname === "/v2/research/topics/t1/earnings-squad/dossier") {
        return jsonResponse({
          topic_id: "t1",
          run_id: "sqrun_done",
          operation_id: "op_sq_done",
          status: "completed",
          org_events: squadEvents,
          dossier: baseDossier,
        } satisfies ResearchV2.EarningsSquadDossierResponse);
      }
      if (url.pathname === "/v2/research/topics/t1/earnings-squad/deep-report" && init.method === "POST") {
        deepCalls += 1;
        return jsonResponse({
          topic_id: "t1",
          run_id: "sqrun_done",
          dossier_id: "dossier_nvda_q2",
          dossier_sha256: "a".repeat(64),
          cached: false,
          deep_report_markdown: deepDossier.deep_report_markdown!,
          deep_report_tool_calls: 0,
          dossier: deepDossier,
        } satisfies ResearchV2.EarningsSquadDeepReportResponse);
      }
      if (url.pathname.endsWith("/sources/s1/text")) return jsonResponse({ text: TEXT, content_sha256: "a".repeat(64) });
      return undefined;
    });

    renderTopic();

    // Collapsed OrgGraph summary bar + EarningsDossierView should hydrate cleanly.
    const orgSummary = await screen.findByTestId("org-graph-summary-bar");
    expect(within(orgSummary).getByText("投研小组执行摘要条")).toBeTruthy();
    expect(within(orgSummary).getByText(/已完成/)).toBeTruthy();
    const dossierView = await screen.findByTestId("earnings-dossier-view");
    expect(within(dossierView).getByRole("heading", { level: 2, name: "NVIDIA Corp." })).toBeTruthy();
    expect(within(dossierView).getByText("NVDA")).toBeTruthy();
    expect(screen.getByText(/云端与数据中心需求延续高景气/)).toBeTruthy();
    expect(screen.getByText(/下季 DSO 回落至 45 天以下/)).toBeTruthy();

    // Switching to Deep Report requests deep-report once, and toggling back/forth uses cached dossier.
    fireEvent.click(screen.getByRole("tab", { name: "完整深研报告 (Deep Report)" }));
    expect(await screen.findByText(/基于锁定底稿按需生成，零新增检索调用/)).toBeTruthy();
    expect(deepCalls).toBe(1);

    fireEvent.click(screen.getByRole("tab", { name: "买方备忘录 (Memo)" }));
    expect(await screen.findByText(/云端与数据中心需求延续高景气/)).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "完整深研报告 (Deep Report)" }));
    expect(await screen.findByText(/基于锁定底稿按需生成，零新增检索调用/)).toBeTruthy();
    expect(deepCalls).toBe(1);

    // Clicking a bear-case evidence anchor opens ReaderPanel with highlighted quote.
    const bearCard = screen.getByTestId("bear-case-card");
    fireEvent.click(within(bearCard).getByRole("button", { name: "card_rev" }));
    expect(await screen.findByText(QUOTE, { selector: "mark" })).toBeTruthy();
  });

  it("launches earnings squad, streams live SSE org_graph_event, steers Lead mid-run, and resolves locked dossier", async () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        matches:
          query === "(min-width: 600px)" ||
          query === "(min-width: 900px)" ||
          query === "(min-width: 1280px)",
        media: query,
        onchange: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    );
    const sector: ResearchV2.SectorApplicability = {
      sic_code: "7372",
      sic_description: "Prepackaged Software",
      sector_category: "non_financial",
      is_gated: false,
      disabled_metrics: [],
      recommended_alternatives: [],
      explanation: "Eligible non-financial issuer.",
    };
    const finalDossier: ResearchV2.EarningsDossier = {
      dossier_id: "dossier_aapl_live",
      run_id: "sqrun_live",
      entity_id: "cik:0000320193",
      symbol: "AAPL",
      cik: "0000320193",
      company_name: "Apple Inc.",
      period_label: "Q3 FY26",
      locked: true,
      dossier_sha256: "b".repeat(64),
      sector_applicability: sector,
      gate_report: { passed: true, exportable: true },
      slot_usage_summary: { total_calls: 4, total_tokens: 9200, stage_0_model_calls: 0, deep_report_tool_calls: 0, slots: {} },
      memo_markdown: "## Apple 买方快评\n\n服务毛利率创新高。",
      open_gaps: [],
      snapshot: {
        entity_id: "cik:0000320193",
        symbol: "AAPL",
        cik: "0000320193",
        company_name: "Apple Inc.",
        period_label: "Q3 FY26",
        sector_applicability: sector,
        statements: [],
      },
    };

    let squadFinished = false;
    let streamController: ReadableStreamDefaultController<Uint8Array> | null = null;
    const encoder = new TextEncoder();
    const steerBodies: Array<Record<string, unknown>> = [];

    mockApi((url, init) => {
      if (url.pathname === "/v2/research/topics/t1") {
        return jsonResponse({ ...topicWith([]), run_mode: "earnings_squad", squad_run_id: "sqrun_live", ticker: "AAPL", cik: "0000320193" });
      }
      if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([]);
      if (url.pathname === "/v2/research/topics/t1/earnings-squad/dossier") {
        if (!squadFinished) {
          return jsonResponse({
            topic_id: "t1",
            run_id: "sqrun_live",
            operation_id: "op_sq_live",
            status: "running",
            org_events: [
              { sequence: 1, node_id: "prefetch_engine", role: "prefetch", display_name: "官方资料与确定性预计算", action_kind: "READING", runtime_state: "completed", stage: "stage_0", current_activity: "完成 SEC 预抓取" },
            ],
            dossier: null,
          } satisfies ResearchV2.EarningsSquadDossierResponse);
        }
        return jsonResponse({
          topic_id: "t1",
          run_id: "sqrun_live",
          operation_id: "op_sq_live",
          status: "completed",
          org_events: [
            { sequence: 1, node_id: "prefetch_engine", role: "prefetch", display_name: "官方资料与确定性预计算", action_kind: "READING", runtime_state: "completed", stage: "stage_0", current_activity: "完成预计算" },
            { sequence: 2, node_id: "lead_coordinator", role: "lead_coordinator", display_name: "Lead 主研究协调", parent_node_id: "prefetch_engine", slot: "deep_reasoner", action_kind: "DELEGATING", runtime_state: "completed", stage: "stage_1", current_activity: "已完成调度" },
            { sequence: 3, node_id: "dossier_writer", role: "dossier_writer", display_name: "底稿撰稿 (Writer)", parent_node_id: "lead_coordinator", slot: "writer", action_kind: "RESPONDING", runtime_state: "completed", stage: "stage_3b_write", current_activity: "已锁定档案" },
          ],
          dossier: finalDossier,
        } satisfies ResearchV2.EarningsSquadDossierResponse);
      }
      if (url.pathname === "/v2/research/topics/t1/earnings-squad/events") {
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            streamController = controller;
          },
        });
        return new Response(body, {
          status: 200,
          headers: { "Content-Type": "text/event-stream; charset=utf-8" },
        });
      }
      if (url.pathname === "/v2/research/topics/t1/earnings-squad/steer" && init.method === "POST") {
        const parsed = JSON.parse(String(init.body)) as Record<string, unknown>;
        steerBodies.push(parsed);
        return jsonResponse({
          topic_id: "t1",
          run_id: "sqrun_live",
          operation_id: "op_sq_live",
          status: "queued",
          instruction: { sequence: 1, instruction_id: "steer_1", text: String(parsed["text"]), created_at: NOW },
        } satisfies ResearchV2.EarningsSquadSteerResponse);
      }
      return undefined;
    });

    const { container } = renderTopic();

    // Auto-routed squad renders directly without any manual idle ticker card.
    await waitFor(() => expect(streamController).not.toBeNull());
    expect(screen.queryByRole("button", { name: "启动美股财报买方小组" })).toBeNull();

    const liveEvent: ResearchV2.OrgGraphEvent = {
      sequence: 2,
      node_id: "lead_coordinator",
      role: "lead_coordinator",
      display_name: "Lead 主研究协调",
      parent_node_id: "prefetch_engine",
      slot: "deep_reasoner",
      action_kind: "DELEGATING",
      runtime_state: "running",
      stage: "stage_1_plan",
      current_activity: "正在指派财务质量排查",
    };
    streamController!.enqueue(
      encoder.encode(`id: org:2\nevent: org_graph_event\ndata: ${JSON.stringify(liveEvent)}\n\n`),
    );

    // Selecting a node opens the right-hand process drawer and tells AppLayout (.app.with-panel) to reserve drawer width.
    const leadNode = await waitFor(() => {
      const el = container.querySelector('[data-node-id="lead_coordinator"]');
      expect(el).toBeTruthy();
      return el!;
    });
    fireEvent.click(leadNode);
    const drawer = await screen.findByRole("complementary", { name: "Lead 主研究协调" });
    expect(within(drawer).getAllByText(/正在指派财务质量排查/).length).toBeGreaterThanOrEqual(1);
    await waitFor(() => {
      expect(container.querySelector(".app")?.classList.contains("with-panel")).toBe(true);
    });
    fireEvent.click(within(drawer).getByRole("button", { name: "关闭" }));
    await waitFor(() => {
      expect(container.querySelector(".app")?.classList.contains("with-panel")).toBe(false);
    });

    // Unified single bottom dock Composer steers Lead while squad is running.
    const steerField = await screen.findByPlaceholderText("向 Lead 发送中场调度指令…");
    fireEvent.change(steerField, { target: { value: "核查服务业务毛利率与 SBC 加回" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(steerBodies).toHaveLength(1));
    expect(steerBodies[0]?.["text"]).toBe("核查服务业务毛利率与 SBC 加回");
    expect(await screen.findByText(/#1 核查服务业务毛利率与 SBC 加回/)).toBeTruthy();

    // Terminal SSE frame triggers dossier fetch and reveals locked EarningsDossierView.
    squadFinished = true;
    streamController!.enqueue(
      encoder.encode(`id: done:completed\nevent: run.terminal\ndata: ${JSON.stringify({ operation_id: "op_sq_live", operation_status: "completed", version_id: null })}\n\n`),
    );
    streamController!.close();

    const dossierView = await screen.findByTestId("earnings-dossier-view");
    expect(within(dossierView).getByRole("heading", { level: 2, name: "Apple Inc." })).toBeTruthy();
    expect(within(dossierView).getByText("AAPL")).toBeTruthy();
    expect(await screen.findByText(/服务毛利率创新高/)).toBeTruthy();
    const orgSummary = await screen.findByTestId("org-graph-summary-bar");
    expect(within(orgSummary).getByText("投研小组执行摘要条")).toBeTruthy();
    expect(within(orgSummary).getByText(/已完成/)).toBeTruthy();
  });

  it("suppresses legacy InterruptedTurn on failed squad run, shows friendly Chinese error + collapsible detail, and cleanly retries/resumes from checkpoint", async () => {
    const sector: ResearchV2.SectorApplicability = {
      sic_code: "3674",
      sic_description: "Semiconductors",
      sector_category: "non_financial",
      is_gated: false,
      disabled_metrics: [],
      recommended_alternatives: [],
      explanation: "Non-financial issuer.",
    };
    const rawPydanticError = [
      "1 validation error for ResearchDispatchPlan",
      "tasks",
      "  Field required [type=missing, input_value={'worker_tasks': []}, input_type=dict]",
      "extra_field",
      "  Extra inputs are not permitted [type=extra_forbidden]",
    ].join("\n");
    const failedEvents: ResearchV2.OrgGraphEvent[] = [
      {
        sequence: 1,
        stage: "stage_0_prefetch",
        node_id: "prefetch_engine",
        role: "prefetch",
        display_name: "Stage 0 Prefetch",
        action_kind: "RESPONDING",
        runtime_state: "completed",
        current_activity: "完成 SEC 预抓取",
      },
      {
        sequence: 2,
        stage: "stage_1_plan",
        node_id: "lead_coordinator",
        role: "lead_coordinator",
        display_name: "Lead Coordinator",
        slot: "deep_reasoner",
        action_kind: "FAILED",
        runtime_state: "failed",
        current_activity: "Stage 1 规划失败",
        blocker_text: rawPydanticError,
      },
    ];
    const resolvedDossier: ResearchV2.EarningsDossier = {
      dossier_id: "dossier_nvda_retry",
      run_id: "sqrun_bad",
      entity_id: "cik:0001045810",
      symbol: "NVDA",
      cik: "0001045810",
      company_name: "NVIDIA Corp.",
      period_label: "Q2 FY26",
      locked: true,
      dossier_sha256: "c".repeat(64),
      sector_applicability: sector,
      gate_report: { passed: true, exportable: true },
      slot_usage_summary: { total_calls: 4, total_tokens: 11200, stage_0_model_calls: 0, deep_report_tool_calls: 0, slots: {} },
      memo_markdown: "## 恢复后买方摘要\n\n数据中心业务继续领先。",
      open_gaps: [],
      snapshot: {
        entity_id: "cik:0001045810",
        symbol: "NVDA",
        cik: "0001045810",
        company_name: "NVIDIA Corp.",
        period_label: "Q2 FY26",
        sector_applicability: sector,
        statements: [],
      },
    };

    let retried = false;
    let completed = false;
    let retryStreamController: ReadableStreamDefaultController<Uint8Array> | null = null;
    const encoder = new TextEncoder();
    const runBodies: Array<Record<string, unknown>> = [];
    const eventQueries: string[] = [];

    mockApi((url, init) => {
      if (url.pathname === "/v2/research/topics/t1") {
        return jsonResponse({ ...topicWith([]), run_mode: "earnings_squad", squad_run_id: "sqrun_bad", ticker: "NVDA", cik: "0001045810" });
      }
      if (url.pathname === "/v2/research/topics/t1/runs") {
        // Failed auto-routed operation with turns=[] would trigger legacy orphanRun if not suppressed.
        return jsonResponse([operation("op_bad", "failed")]);
      }
      if (url.pathname === "/v2/research/topics/t1/earnings-squad/dossier") {
        if (!retried) {
          return jsonResponse({
            topic_id: "t1",
            run_id: "sqrun_bad",
            operation_id: "op_bad",
            status: "failed",
            ticker: "NVDA",
            cik: "0001045810",
            checkpoint_completed_stages: ["stage_0_prefetch"],
            org_events: failedEvents,
            dossier: null,
            error: rawPydanticError,
          } satisfies ResearchV2.EarningsSquadDossierResponse);
        }
        if (!completed) {
          return jsonResponse({
            topic_id: "t1",
            run_id: "sqrun_bad",
            operation_id: "op_retry",
            status: "running",
            ticker: "NVDA",
            cik: "0001045810",
            checkpoint_completed_stages: ["stage_0_prefetch"],
            org_events: [failedEvents[0]!],
            dossier: null,
          } satisfies ResearchV2.EarningsSquadDossierResponse);
        }
        return jsonResponse({
          topic_id: "t1",
          run_id: "sqrun_bad",
          operation_id: "op_retry",
          status: "completed",
          ticker: "NVDA",
          cik: "0001045810",
          checkpoint_completed_stages: ["stage_0_prefetch", "stage_1_plan", "stage_3b_gate_memo"],
          org_events: [
            failedEvents[0]!,
            {
              sequence: 3,
              stage: "stage_3b_gate_memo",
              node_id: "dossier_writer",
              role: "writer",
              display_name: "Dossier Writer",
              slot: "writer",
              action_kind: "RESPONDING",
              runtime_state: "completed",
              current_activity: "底稿锁定完成",
            },
          ],
          dossier: resolvedDossier,
        } satisfies ResearchV2.EarningsSquadDossierResponse);
      }
      if (url.pathname === "/v2/research/topics/t1/earnings-squad/run" && init.method === "POST") {
        retried = true;
        runBodies.push(JSON.parse(String(init.body)) as Record<string, unknown>);
        return jsonResponse({
          topic_id: "t1",
          run_id: "sqrun_bad",
          operation_id: "op_retry",
          status: "running",
          ticker: "NVDA",
          cik: "0001045810",
          checkpoint_completed_stages: ["stage_0_prefetch"],
          org_events: [failedEvents[0]!],
          dossier: null,
        } satisfies ResearchV2.EarningsSquadRunResponse, { status: 202 });
      }
      if (url.pathname === "/v2/research/topics/t1/earnings-squad/events") {
        eventQueries.push(url.search);
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            retryStreamController = controller;
          },
        });
        return new Response(body, {
          status: 200,
          headers: { "Content-Type": "text/event-stream; charset=utf-8" },
        });
      }
      return undefined;
    });

    const { container } = renderTopic();

    // Friendly Chinese error callout renders without legacy <InterruptedTurn> collision.
    const errorCallout = await screen.findByTestId("squad-error-callout");
    expect(within(errorCallout).getByText("投研小组执行失败")).toBeTruthy();
    expect(within(errorCallout).getByText(/结构化研究计划或分析结果格式校验未通过/)).toBeTruthy();
    expect(within(errorCallout).getByText(/已保存检查点阶段：预热取证/)).toBeTruthy();
    expect(screen.queryByText("运行中断")).toBeNull();
    expect(screen.queryByText("这一轮没有完成，也没有保存新的版本。")).toBeNull();
    expect(screen.queryByText(/Extra inputs are not permitted/)).toBeNull();
    expect(errorCallout.querySelectorAll("[style]").length).toBe(0);

    // OrgGraphPanel shows aligned failed stage label in Chinese.
    expect(container.querySelector(".org-ragent-stage-pill")?.textContent).toBe("任务派发 · 执行失败");

    // Technical details stay collapsed by default and expand/collapse on click.
    fireEvent.click(within(errorCallout).getByRole("button", { name: /查看技术详情/ }));
    expect(screen.getByTestId("squad-error-detail").textContent).toContain("Extra inputs are not permitted");
    fireEvent.click(within(errorCallout).getByRole("button", { name: /收起技术详情/ }));
    expect(screen.queryByTestId("squad-error-detail")).toBeNull();

    // Clicking "重新运行 / 从检查点恢复" re-launches runEarningsSquad, resets error, and resumes SSE.
    fireEvent.click(within(errorCallout).getByRole("button", { name: /重新运行 \/ 从检查点恢复/ }));

    await waitFor(() => expect(runBodies).toHaveLength(1));
    expect(runBodies[0]).toMatchObject({
      execution_mode: "async",
      symbol: "NVDA",
      cik: "0001045810",
      resume_from_checkpoint: true,
      resume_run_id: "sqrun_bad",
    });
    await waitFor(() => expect(screen.queryByTestId("squad-error-callout")).toBeNull());
    await waitFor(() => expect(retryStreamController).not.toBeNull());
    expect(eventQueries[0]).toBe("?after=1");

    // Incoming SSE events + terminal frame transition straight to completed EarningsDossierView.
    retryStreamController!.enqueue(
      encoder.encode(
        `id: org:2\nevent: org_graph_event\ndata: ${JSON.stringify({
          sequence: 2,
          stage: "stage_2_5_review",
          node_id: "lead_coordinator",
          role: "lead_coordinator",
          display_name: "Lead Coordinator",
          slot: "deep_reasoner",
          action_kind: "INSPECTING",
          runtime_state: "running",
          current_activity: "交叉复核补证",
        })}\n\n`,
      ),
    );
    await waitFor(() => {
      expect(container.querySelector(".org-ragent-stage-pill")?.textContent).toBe("交叉补证");
    });

    completed = true;
    retryStreamController!.enqueue(
      encoder.encode(
        `id: done:completed\nevent: run.terminal\ndata: ${JSON.stringify({ operation_id: "op_retry", operation_status: "completed", version_id: "dossier_nvda_retry" })}\n\n`,
      ),
    );
    retryStreamController!.close();

    const dossierView = await screen.findByTestId("earnings-dossier-view");
    expect(within(dossierView).getByRole("heading", { level: 2, name: "NVIDIA Corp." })).toBeTruthy();
    expect(await screen.findByText(/数据中心业务继续领先/)).toBeTruthy();
  });

  it("maps timeout errors cleanly to Chinese and suppresses InterruptedTurn even when /dossier returns 404 for a failed auto-routed run", async () => {
    mockApi((url) => {
      if (url.pathname === "/v2/research/topics/t1") {
        return jsonResponse({ ...topicWith([]), run_mode: "earnings_squad", ticker: "MSFT" });
      }
      if (url.pathname === "/v2/research/topics/t1/runs") {
        return jsonResponse([operation("op_timeout", "failed")]);
      }
      if (url.pathname === "/v2/research/topics/t1/earnings-squad/dossier") {
        return jsonResponse({ error_code: "not_found", message: "No earnings squad run found" }, { status: 404 });
      }
      return undefined;
    });

    const { container } = renderTopic();

    const errorCallout = await screen.findByTestId("squad-error-callout");
    expect(within(errorCallout).getByText("投研小组执行失败")).toBeTruthy();
    expect(within(errorCallout).getByRole("button", { name: /重新运行 \/ 从检查点恢复/ })).toBeTruthy();
    expect(screen.queryByText("运行中断")).toBeNull();
    expect(container.querySelector(".org-ragent-stage-pill")?.textContent).toBe("执行失败");
  });

  it("maps timeout errors to calm Chinese explanation with collapsible technical detail", async () => {
    mockApi((url) => {
      if (url.pathname === "/v2/research/topics/t1") {
        return jsonResponse({ ...topicWith([]), run_mode: "earnings_squad", squad_run_id: "sqrun_to", ticker: "MSFT" });
      }
      if (url.pathname === "/v2/research/topics/t1/runs") {
        return jsonResponse([operation("op_to", "failed")]);
      }
      if (url.pathname === "/v2/research/topics/t1/earnings-squad/dossier") {
        return jsonResponse({
          topic_id: "t1",
          run_id: "sqrun_to",
          operation_id: "op_to",
          status: "failed",
          ticker: "MSFT",
          org_events: [],
          dossier: null,
          error: "ReadTimeoutError: HTTPSConnectionPool timed out after 20s",
        } satisfies ResearchV2.EarningsSquadDossierResponse);
      }
      return undefined;
    });

    renderTopic();

    const errorCallout = await screen.findByTestId("squad-error-callout");
    expect(within(errorCallout).getByText(/模型或数据源响应超时/)).toBeTruthy();
    expect(screen.queryByText(/ReadTimeoutError/)).toBeNull();
    fireEvent.click(within(errorCallout).getByRole("button", { name: /查看技术详情/ }));
    expect(screen.getByTestId("squad-error-detail").textContent).toContain("ReadTimeoutError");
  });
});
