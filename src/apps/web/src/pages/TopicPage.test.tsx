import type { ResearchV2 } from "@foresightnine/contracts";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { jsonResponse, storeSession } from "../test/fixtures";
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

function renderTopic() {
  storeSession();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter(routes, { future: routerFuture, initialEntries: ["/research/t1"] });
  return render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} future={{ v7_startTransition: true }} />
    </QueryClientProvider>,
  );
}

const CLAIMS: ResearchV2.ResearchClaim[] = [
  { claim_id: "C1", role: "support", text: "总收入为 48,612 百万美元。", turn_id: "turn_2", citations: [{ source_id: "s1", quotation: QUOTE, location: { character_start: START, character_end: END } }] },
  { claim_id: "C2", role: "counterargument", text: "利润率没有提高。", turn_id: "turn_2", challenges_claim_ids: ["C1"], citations: [{ source_id: "s1", quotation: QUOTE, location: { character_start: START, character_end: END } }] },
];

describe("TopicPage", () => {
  it("shows a partial turn with its gaps and offers to continue when recovery allows it", async () => {
    const partial = turn(1, { status: "partial", reason: "no_submission", gaps: [{ code: "no_cited_claims", message: "x" }] });
    const completed = turn(2, { claim_ids: ["C1", "C2"], report: "第一段。\n\n第二段。" });
    mockApi((url) => {
      if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([partial, completed], CLAIMS));
      if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_2", "completed"), operation("op_1", "failed")]);
      return undefined;
    });
    renderTopic();
    expect(await screen.findByRole("heading", { level: 1, name: "Northwind 示例公司的收入增长有多快？" })).toBeTruthy();
    const turn2 = screen.getByRole("region", { name: "第 2 轮" });
    expect(within(turn2).getByText("完成")).toBeTruthy();
    expect(within(turn2).getByText("支持")).toBeTruthy();
    expect(within(turn2).getByText("待复核")).toBeTruthy();
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

  it("locks the composer and shows the live bar while a run is active", async () => {
    mockApi((url) => {
      if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([turn(1)]));
      if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_2", "running"), operation("op_1", "completed")]);
      if (url.pathname.endsWith("/op_2/progress")) {
        return jsonResponse({
          operation_id: "op_2", operation_status: "running", turn: { index: 2, question: "第二个问题" }, usage: { model_calls: 1, search_calls: 0, total_tokens: 900 },
          current: { kind: "tool", name: "read_sources" }, events: [{ index: 0, kind: "tool", name: "read_sources", status: "read", reason: null, rationale: "读取年报", denied_count: null }],
          event_count: 1, started_at: NOW, updated_at: NOW, version_id: null,
        });
      }
      return undefined;
    });
    renderTopic();
    expect(await screen.findByText("第二个问题")).toBeTruthy();
    expect(screen.getByRole("button", { name: "取消" })).toBeTruthy();
    expect(screen.getByText(/阅读资料：读取年报/)).toBeTruthy();
    const composer = screen.getByRole("textbox", { name: "追问" }) as HTMLTextAreaElement;
    expect(composer.disabled).toBe(true);
    expect(composer.placeholder).toBe("本轮研究进行中，可以先取消再提问");
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
      expect(body.discover_sources).toBeNull();
      expect("max_model_calls" in body).toBe(false);
    });
  });

  it("shows an interrupted run that saved no turn", async () => {
    mockApi((url) => {
      if (url.pathname === "/v2/research/topics/t1") return jsonResponse(topicWith([turn(1)]));
      if (url.pathname === "/v2/research/topics/t1/runs") return jsonResponse([operation("op_2", "failed"), operation("op_1", "completed")]);
      if (url.pathname.endsWith("/op_2/recovery-status")) return jsonResponse({ can_continue: false, can_deliver: false, reason: "unknown_external", checkpoint: "search", version_id: "v2" });
      if (url.pathname.endsWith("/op_2/progress")) {
        return jsonResponse({ operation_id: "op_2", operation_status: "failed", turn: { index: 2, question: "失败的问题" }, usage: { model_calls: 1, search_calls: 1, total_tokens: 10 }, current: null, events: [], event_count: 0, started_at: NOW, updated_at: NOW, version_id: null });
      }
      return undefined;
    });
    renderTopic();
    const region = await screen.findByRole("region", { name: "第 2 轮" });
    expect(within(region).getByText("运行中断")).toBeTruthy();
    expect(await within(region).findByText("恢复检查：外部请求结果未知")).toBeTruthy();
    expect(await within(region).findByText("失败的问题")).toBeTruthy();
    expect(within(region).getByRole("button", { name: "重新提问" })).toBeTruthy();
  });
});
