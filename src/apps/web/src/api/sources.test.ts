import { describe, expect, it, vi } from "vitest";
import { jsonResponse, makeSession, storeSession, TOKEN } from "../test/fixtures";
import { uploadDocumentStream } from "./sources";

describe("uploadDocumentStream", () => {
  it("posts multipart form data with the file last and no JSON content type", async () => {
    storeSession(makeSession());
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => jsonResponse({ source_id: "source_1" }, { status: 201 }));
    vi.stubGlobal("fetch", fetchMock);
    const file = new File([new Uint8Array([0x25, 0x50, 0x44, 0x46])], "ar.pdf", { type: "application/pdf" });
    const permissions = { store: true, model_input: true, display: true, export: false };
    await uploadDocumentStream(file, { title: "Annual report", permissions, is_simulated: true, published_at: "2026-03-01T00:00:00Z", text_order: "layout" });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(String(url)).toContain("/v2/research/sources/upload");
    expect(init?.method).toBe("POST");
    const headers = (init?.headers ?? {}) as Record<string, string>;
    expect(headers["Content-Type"]).toBeUndefined();
    expect(headers.Authorization).toBe(`Bearer ${TOKEN}`);
    const form = init?.body as FormData;
    expect(form).toBeInstanceOf(FormData);
    expect(JSON.parse(String(form.get("permissions")))).toEqual(permissions);
    expect(form.get("title")).toBe("Annual report");
    expect(form.get("is_simulated")).toBe("true");
    expect(form.get("published_at")).toBe("2026-03-01T00:00:00Z");
    expect(form.has("event_at")).toBe(false);
    expect([...form.keys()].at(-1)).toBe("file");
    expect((form.get("file") as File).name).toBe("ar.pdf");
  });
});
