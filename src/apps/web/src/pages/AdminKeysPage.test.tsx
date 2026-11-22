import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AdminKeysPage } from "./AdminKeysPage";
import { jsonResponse, storeSession } from "../test/fixtures";

const key = { name: "FORESIGHTNINE_EXA_SEARCH_API_KEY", label: "Exa", category: "search", provider_id: "exa", signup_url: null, docs_url: null, notes: null, region: null, configured: true, source: "env", key_hint: "••••abcd", updated_at: null, last_test: null };

describe("AdminKeysPage", () => {
  it("fails closed for an ineligible account", async () => {
    storeSession();
    const fetchMock = vi.fn(async () => jsonResponse({ enabled: true, eligible: false, passphrase_required: false, elevated: false, expires_at: null }));
    vi.stubGlobal("fetch", fetchMock);
    render(<AdminKeysPage />);
    expect(await screen.findByText(/无权访问此页面/)).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Exa")).toBeNull();
  });

  it("requires elevation and clears entered credentials after replacement", async () => {
    storeSession();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      const path = new URL(String(input)).pathname;
      if (path === "/v1/admin/status") return jsonResponse({ enabled: true, eligible: true, passphrase_required: true, elevated: false, expires_at: null });
      if (path === "/v1/admin/elevation") return jsonResponse({ elevation_token: "temporary", expires_at: "2026-11-21T12:00:00Z" });
      if (path === "/v1/admin/keys" ) return jsonResponse({ keys: [key] });
      if (path === "/v1/admin/audit") return jsonResponse({ entries: [] });
      if (path.endsWith(key.name) && init.method === "PUT") return jsonResponse(key);
      return jsonResponse({}, { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<AdminKeysPage />);
    fireEvent.change(await screen.findByLabelText(/管理员口令/), { target: { value: "passphrase" } });
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/v1/admin/keys"))).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: /验证/ }));
    const input = await screen.findByLabelText("Exa 密钥 / Key") as HTMLInputElement;
    expect(screen.getByText(/环境变量/)).toBeTruthy();
    expect(screen.getByText(/••••abcd/)).toBeTruthy();
    fireEvent.change(input, { target: { value: "secret-value" } });
    fireEvent.click(screen.getByRole("button", { name: /替换/ }));
    await waitFor(() => expect(input.value).toBe(""));
    const put = fetchMock.mock.calls.find(([url, init]) => String(url).endsWith(key.name) && init?.method === "PUT");
    expect(JSON.parse(String(put?.[1]?.body))).toEqual({ value: "secret-value" });
    expect((put?.[1]?.headers as Record<string, string>)["X-Admin-Elevation"]).toBe("temporary");
    expect(document.body.textContent).not.toContain("secret-value");
    expect(JSON.stringify(localStorage)).not.toContain("secret-value");
  });
});
