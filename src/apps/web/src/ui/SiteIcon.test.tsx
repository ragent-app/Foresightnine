import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SiteIcons } from "./SiteIcon";

const wrap = (node: React.ReactNode) => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{node}</QueryClientProvider>,
);

afterEach(() => vi.unstubAllGlobals());

describe("site icons", () => {
  it("shows the site's own icon as a data URL, and its initial when there is none", async () => {
    const png = new Blob([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], { type: "image/png" });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) =>
      String(input).includes("/v2/research/favicons/www.cnbc.com")
        ? new Response(png, { status: 200, headers: { "Content-Type": "image/png" } })
        : new Response(JSON.stringify({ error_code: "not_found", message: "No icon" }), { status: 404, headers: { "Content-Type": "application/json" } })));
    const { container } = wrap(<SiteIcons hosts={["www.cnbc.com", "apnews.com", "a.example.com", "b.example.com"]} />);
    expect(screen.getByRole("img", { name: "来源网站：cnbc.com、apnews.com、a.example.com、b.example.com" })).toBeTruthy();
    await waitFor(() => expect(container.querySelector("img.site-icon")?.getAttribute("src")).toMatch(/^data:image\/png;base64,/));
    expect([...container.querySelectorAll(".site-initial")].map((node) => node.textContent)).toEqual(["A", "A"]);
    expect(container.querySelectorAll(".site-icon")).toHaveLength(3);
  });
});
