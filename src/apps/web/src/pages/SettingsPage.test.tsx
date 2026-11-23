import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { routerFuture, routes } from "../app/router";
import { jsonResponse, storeSession } from "../test/fixtures";

const PLATFORM = { schema_version: "v1", mode: "platform", configured: true };
const PERSONAL = {
  schema_version: "v1", mode: "personal", configured: true, protocol: "openai_chat_completions",
  base_url: "https://api.example.com/v1", model: "example-model", key_hint: "••••abcd",
};
const ACCOUNT = {
  account: { account_id: "acc_1", email: "a@example.com" }, plan: "free", entitlement_status: "active", product_id: null, base_plan_id: null,
  quota_limit: 200000, quota_consumed: 50000, quota_reserved: 0, quota_remaining: 150000, quota_resets_at: "2026-12-01T00:00:00Z", provider_model: "platform-model-example",
  research_model: "research-model-example",
};
const provider = (id: string, name: string, extra: Record<string, unknown> = {}) => ({
  provider_id: id, display_name: name, enabled: true, requires_key: id !== "yahoo", personal_key_configured: false, platform_available: id === "yahoo", key_hint: null, ...extra,
});
const MARKET = {
  schema_version: "v1", updated_at: null, fallback_order: ["market_data", "alpha_vantage", "twelve_data", "finnhub", "yahoo", "tushare"],
  providers: [provider("market_data", "Market Data"), provider("alpha_vantage", "Alpha Vantage"), provider("twelve_data", "Twelve Data"), provider("finnhub", "Finnhub"), provider("yahoo", "Yahoo Finance"), provider("tushare", "Tushare Pro (A-shares)")],
};

type Handler = (url: URL, init: RequestInit) => Response | undefined;

function mockApi(handler: Handler) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input));
    const response = handler(url, init);
    if (response) return response;
    if (url.pathname === "/v1/account") return jsonResponse(ACCOUNT);
    if (url.pathname === "/v2/research/topics") return jsonResponse([]);
    return jsonResponse({ error_code: "not_found", message: "nf" }, { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderSettings(path: string) {
  storeSession();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter(routes, { future: routerFuture, initialEntries: [path] });
  return render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} future={{ v7_startTransition: true }} />
    </QueryClientProvider>,
  );
}

const bodyOf = (fetchMock: ReturnType<typeof mockApi>, path: string, method: string) => {
  const call = fetchMock.mock.calls.find(([input, init]) => new URL(String(input)).pathname === path && init?.method === method);
  return call ? JSON.parse(String(call[1]!.body ?? "null")) as Record<string, unknown> : undefined;
};

describe("SettingsPage", () => {
  it("shows this period's usage on the account tab", async () => {
    mockApi(() => undefined);
    renderSettings("/settings");
    expect(await screen.findByText("免费方案")).toBeTruthy();
    expect(screen.getByText(/剩余 150,000 Token/)).toBeTruthy();
  });

  it("saves a personal model and tests the connection", async () => {
    let saved = false;
    const fetchMock = mockApi((url, init) => {
      if (url.pathname === "/v1/account/model-config" && init.method === "PUT") { saved = true; return jsonResponse(PERSONAL); }
      if (url.pathname === "/v1/account/model-config") return jsonResponse(saved ? PERSONAL : PLATFORM);
      if (url.pathname === "/v1/account/model-config/test") return jsonResponse({ status: "ready" });
      return undefined;
    });
    renderSettings("/settings?tab=model");
    // The research model is shown, not the general billing label (362 acceptance).
    expect(await screen.findByText(/研究使用 research-model-example/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "我的模型" }));
    fireEvent.change(await screen.findByLabelText("服务地址"), { target: { value: "https://api.example.com/v1" } });
    fireEvent.change(screen.getByLabelText("模型名称"), { target: { value: "example-model" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByText("请输入 API Key")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("API Key"), { target: { value: "sk-example-abcd" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByText("研究工具已验证")).toBeTruthy();
    expect(bodyOf(fetchMock, "/v1/account/model-config", "PUT")).toEqual({
      mode: "personal", protocol: "openai_chat_completions", base_url: "https://api.example.com/v1", model: "example-model", api_key: "sk-example-abcd",
    });
    expect(screen.getByText("OpenAI Chat Completions · example-model")).toBeTruthy();
  });

  it("discovers models without resending the saved key and preserves the chosen effort on verification failure", async () => {
    const fetchMock = mockApi((url, init) => {
      if (url.pathname === "/v1/account/model-config/models") {
        return jsonResponse({ models: [{ id: "example-model", efforts: ["low", "medium"], effort_source: "model_api" }] });
      }
      if (url.pathname === "/v1/account/model-config/test") {
        return jsonResponse({ status: "unavailable", upstream_status: 400, parameter: "reasoning_effort" });
      }
      if (url.pathname === "/v1/account/model-config") return jsonResponse(PERSONAL);
      return undefined;
    });
    renderSettings("/settings?tab=model");
    fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
    expect(await screen.findByText(/模型接口列出：low、medium/, {}, { timeout: 3000 })).toBeTruthy();
    expect(bodyOf(fetchMock, "/v1/account/model-config/models", "POST")).toEqual({
      protocol: "openai_chat_completions", base_url: PERSONAL.base_url,
    });
    const effort = screen.getByLabelText("推理强度（effort）") as HTMLInputElement;
    expect(effort.value).toBe("");
    expect(Array.from(effort.list!.options, (option) => option.value)).toEqual(["low", "medium"]);
    fireEvent.change(effort, { target: { value: "medium" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByText(/HTTP 400，请检查参数 reasoning_effort/)).toBeTruthy();
    expect(bodyOf(fetchMock, "/v1/account/model-config", "PUT")?.generation_parameters).toEqual({ reasoning_effort: "medium" });
  });

  it("keeps the saved configuration when switching to the platform model and reuses it when switching back", async () => {
    let mode: "platform" | "personal" = "personal";
    const fetchMock = mockApi((url, init) => {
      if (url.pathname === "/v1/account/model-config" && init.method === "PUT") {
        mode = (JSON.parse(String(init.body)) as { mode: "platform" | "personal" }).mode;
        return jsonResponse({ ...PERSONAL, mode });
      }
      if (url.pathname === "/v1/account/model-config") return jsonResponse({ ...PERSONAL, mode });
      if (url.pathname === "/v1/account/model-config/test") return jsonResponse({ status: "ready" });
      return undefined;
    });
    renderSettings("/settings?tab=model");
    await screen.findByText("OpenAI Chat Completions · example-model");
    fireEvent.click(screen.getByRole("button", { name: "平台模型" }));
    expect(await screen.findByText(/已保存，未使用/)).toBeTruthy();
    expect(screen.queryByRole("alertdialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "我的模型" }));
    expect(await screen.findByText("研究工具已验证")).toBeTruthy();
    expect(screen.queryByLabelText("API Key")).toBeNull();
    const puts = fetchMock.mock.calls.filter(([input, init]) => String(input).endsWith("/v1/account/model-config") && init?.method === "PUT").map(([, init]) => JSON.parse(String(init!.body)));
    expect(puts).toEqual([{ mode: "platform" }, { mode: "personal" }]);
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(false);
  });

  it("deletes the saved configuration only after confirmation", async () => {
    let deleted = false;
    const fetchMock = mockApi((url, init) => {
      if (url.pathname === "/v1/account/model-config" && init.method === "DELETE") { deleted = true; return jsonResponse(PLATFORM); }
      if (url.pathname === "/v1/account/model-config") return jsonResponse(deleted ? PLATFORM : { ...PERSONAL, mode: "platform" });
      return undefined;
    });
    renderSettings("/settings?tab=model");
    await screen.findByText(/已保存，未使用/);
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(true));
    await waitFor(() => expect(screen.queryByText(/已保存，未使用/)).toBeNull());
    // Without a saved configuration, choosing 我的模型 opens the form again.
    fireEvent.click(screen.getByRole("button", { name: "我的模型" }));
    expect(await screen.findByLabelText("API Key")).toBeTruthy();
  });

  it("offers a tool verification for Anthropic Messages", async () => {
    mockApi((url) => (url.pathname === "/v1/account/model-config" ? jsonResponse({ ...PERSONAL, protocol: "anthropic_messages", messages_options: { version: "test-version", auth_scheme: "bearer", max_tokens: 1000 } }) : undefined));
    renderSettings("/settings?tab=model");
    await screen.findByText("Anthropic Messages · example-model");
    expect(screen.getByRole("button", { name: "验证研究工具" })).toBeTruthy();
    expect(screen.getByText("研究工具待验证")).toBeTruthy();
  });

  it("reorders and disables data sources with full update bodies", async () => {
    const fetchMock = mockApi((url, init) => {
      if (url.pathname === "/v1/account/market-data-config") return jsonResponse(MARKET);
      void init;
      return undefined;
    });
    renderSettings("/settings?tab=data");
    const rows = await screen.findAllByText(/Market Data|Yahoo Finance/);
    expect(rows.length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "下移 Market Data" }));
    await waitFor(() => expect(bodyOf(fetchMock, "/v1/account/market-data-config", "PUT")).toBeTruthy());
    const body = bodyOf(fetchMock, "/v1/account/market-data-config", "PUT")!;
    expect(body.fallback_order).toEqual(["alpha_vantage", "market_data", "twelve_data", "finnhub", "yahoo", "tushare"]);
    expect((body.providers as unknown[]).every((item) => !("api_key" in (item as object)))).toBe(true);
    const yahoo = screen.getByRole("switch", { name: "启用 Yahoo Finance" });
    expect(within(yahoo.closest(".prow") as HTMLElement).getByText("无需密钥")).toBeTruthy();
    expect(screen.getAllByText("未设置密钥，暂不可用").length).toBe(5);
  });

  it("saves a Tushare token for A-share data", async () => {
    const fetchMock = mockApi((url) => {
      if (url.pathname === "/v1/account/market-data-config/test") return jsonResponse({ provider_id: "tushare", status: "ready" });
      if (url.pathname === "/v1/account/market-data-config") return jsonResponse(MARKET);
      return undefined;
    });
    renderSettings("/settings?tab=data");
    const row = (await screen.findByText("Tushare Pro (A-shares)")).closest(".prow") as HTMLElement;
    expect(screen.getByText(/A 股会改用免费的 BaoStock/)).toBeTruthy();
    fireEvent.click(within(row).getByRole("button", { name: "设置密钥" }));
    fireEvent.change(await screen.findByLabelText("Token"), { target: { value: "tushare-token-0123456789" } });
    expect(screen.getByText(/A 股（上交所、深交所、北交所）日线行情和财务数据/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(bodyOf(fetchMock, "/v1/account/market-data-config", "PUT")).toBeTruthy());
    const body = bodyOf(fetchMock, "/v1/account/market-data-config", "PUT")!;
    const sent = (body.providers as Array<Record<string, unknown>>).find((item) => item.provider_id === "tushare")!;
    expect(sent.api_key).toBe("tushare-token-0123456789");
  });
});
