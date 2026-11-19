import { describe, expect, it } from "vitest";
import { DEFAULT_PERMISSIONS, fetchUrlProblem, fileToBase64, ineligibility, isFetchableUrl, isFredUrl, SEC_UNAVAILABLE, titleFromFilename, uploadKind } from "./sources";

const ALL = DEFAULT_PERMISSIONS;
const CUTOFF = "2026-06-30T00:00:00Z";

describe("ineligibility (mirrors research_source_policy.source_eligibility)", () => {
  it("reads a permitted, real source without a cutoff", () => {
    expect(ineligibility({ permissions: ALL }, null)).toBeNull();
  });

  it("checks in backend order: simulated, FRED, model input, display", () => {
    expect(ineligibility({ is_simulated: true, url: "https://fred.stlouisfed.org/series/X", permissions: { ...ALL, model_input: false } }, null)).toBe("simulated");
    expect(ineligibility({ url: "https://fred.stlouisfed.org/series/X", permissions: { ...ALL, model_input: false } }, null)).toBe("source_not_authorized");
    expect(ineligibility({ permissions: { ...ALL, model_input: false, display: false } }, null)).toBe("model_permission_missing");
    expect(ineligibility({ permissions: { ...ALL, display: false } }, null)).toBe("display_permission_missing");
  });

  it("needs a publication time at or before the cutoff, and no later revision", () => {
    expect(ineligibility({ permissions: ALL }, CUTOFF)).toBe("no_published_at");
    expect(ineligibility({ permissions: ALL, published_at: "2026-07-01T00:00:00Z" }, CUTOFF)).toBe("after_cutoff");
    expect(ineligibility({ permissions: ALL, published_at: "2026-06-30T00:00:00Z" }, CUTOFF)).toBeNull();
    expect(ineligibility({ permissions: ALL, published_at: "2026-05-01T00:00:00Z", revised_at: "2026-07-02T00:00:00Z" }, CUTOFF)).toBe("after_cutoff");
  });
});

describe("isFredUrl", () => {
  it("matches FRED hosts and the FRED API path only", () => {
    expect(isFredUrl("https://fred.stlouisfed.org/series/GDP")).toBe(true);
    expect(isFredUrl("https://alfred.stlouisfed.org/")).toBe(true);
    expect(isFredUrl("https://api.stlouisfed.org/fred/series")).toBe(true);
    expect(isFredUrl("https://api.stlouisfed.org/other")).toBe(false);
    expect(isFredUrl("https://www.federalreserve.gov/")).toBe(false);
    expect(isFredUrl("not a url")).toBe(false);
  });
});

describe("fetchUrlProblem (mirrors FetchSourceRequest.allow_official_https_only)", () => {
  it("accepts allowlisted https pages and direct SEC filing HTML", () => {
    expect(fetchUrlProblem("https://www.federalreserve.gov/newsevents/pressreleases/monetary20260917a.htm")).toBeNull();
    expect(fetchUrlProblem("https://www.bea.gov/news/2026/gdp")).toBeNull();
    expect(fetchUrlProblem("https://www.sec.gov/Archives/edgar/data/1000001/000100000126000012/nwex-20260630.htm")).toBeNull();
  });

  it("refuses SEC addresses when the deployment has no SEC contact configured", () => {
    const filing = "https://www.sec.gov/Archives/edgar/data/1000001/000100000126000012/nwex-20260630.htm";
    expect(fetchUrlProblem(filing, false)).toBe(SEC_UNAVAILABLE);
    expect(isFetchableUrl(filing, false)).toBe(false);
    expect(isFetchableUrl("https://www.bea.gov/news/2026/gdp", false)).toBe(true);
  });

  it("rejects everything else with a reason", () => {
    expect(fetchUrlProblem("")).toBe("请输入网址");
    expect(fetchUrlProblem("www.sec.gov")).toBe("网址格式不正确");
    expect(fetchUrlProblem("http://www.bea.gov/")).toBe("只支持 https 网址");
    expect(fetchUrlProblem("https://user:pw@www.bea.gov/")).toBe("网址不能包含账号或密码");
    expect(fetchUrlProblem("https://www.bea.gov:8443/")).toBe("网址不能指定端口");
    expect(fetchUrlProblem("https://www.bea.gov/x#part")).toBe("网址不能包含 # 后的片段");
    expect(fetchUrlProblem("https://news.example.com/a")).toBe("这个网站目前不能直接获取");
    expect(fetchUrlProblem("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany")).toBe("只支持 SEC EDGAR 申报文件的 HTML 页面地址");
    expect(fetchUrlProblem("https://www.sec.gov/Archives/edgar/data/1000001/000100000126000012/nwex.pdf")).toBe("只支持 SEC EDGAR 申报文件的 HTML 页面地址");
  });
});

describe("file helpers", () => {
  it("detects PDF and HTML by type or extension", () => {
    expect(uploadKind({ name: "a.PDF", type: "" })).toBe("pdf");
    expect(uploadKind({ name: "x", type: "application/pdf" })).toBe("pdf");
    expect(uploadKind({ name: "page.htm", type: "" })).toBe("html");
    expect(uploadKind({ name: "notes.docx", type: "" })).toBeNull();
  });

  it("derives a title from the file name", () => {
    expect(titleFromFilename("annual_report-2026.pdf")).toBe("annual report 2026");
    expect(titleFromFilename(".pdf")).toBe(".pdf");
  });

  it("encodes raw bytes as base64 without a data-URL prefix", async () => {
    const bytes = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d, 0xff, 0x00]);
    const encoded = await fileToBase64(new Blob([bytes]));
    expect(encoded).toBe(btoa(String.fromCharCode(...bytes)));
  });
});
