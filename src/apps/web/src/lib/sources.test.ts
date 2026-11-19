import { describe, expect, it } from "vitest";
import { DEFAULT_PERMISSIONS, fetchUrlProblem, fileToBase64, ineligibility, isFetchableUrl, isFredUrl, isSecHost, SEC_UNAVAILABLE, titleFromFilename, uploadKind } from "./sources";

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
  it("matches FRED/ALFRED hosts (including subdomains) and normalized /fred API paths only", () => {
    expect(isFredUrl("https://fred.stlouisfed.org/series/GDP")).toBe(true);
    expect(isFredUrl("https://research.fred.stlouisfed.org/")).toBe(true);
    expect(isFredUrl("https://alfred.stlouisfed.org/")).toBe(true);
    expect(isFredUrl("https://sub.alfred.stlouisfed.org/series/GDP")).toBe(true);
    expect(isFredUrl("https://api.stlouisfed.org/fred")).toBe(true);
    expect(isFredUrl("https://api.stlouisfed.org/fred/series")).toBe(true);
    expect(isFredUrl("https://api.stlouisfed.org/%66red/series/observations")).toBe(true);
    expect(isFredUrl("https://api.stlouisfed.org/%2566red/series/observations")).toBe(true);
    expect(isFredUrl("https://api.stlouisfed.org//fred/series")).toBe(true);
    expect(isFredUrl("https://api.stlouisfed.org/v1/../fred/series")).toBe(true);
    expect(isFredUrl("https://api.stlouisfed.org/v1/%2e%2e/FRED/series")).toBe(true);
    expect(isFredUrl("https://api.stlouisfed.org/fredrick")).toBe(false);
    expect(isFredUrl("https://api.stlouisfed.org/other")).toBe(false);
    expect(isFredUrl("https://www.federalreserve.gov/")).toBe(false);
    expect(isFredUrl("not a url")).toBe(false);
  });
});

describe("isSecHost", () => {
  it("matches sec.gov and subdomains while ignoring trailing dot and rejecting lookalikes", () => {
    expect(isSecHost("sec.gov")).toBe(true);
    expect(isSecHost("www.sec.gov")).toBe(true);
    expect(isSecHost("data.sec.gov.")).toBe(true);
    expect(isSecHost("efts.sec.gov")).toBe(true);
    expect(isSecHost("www.sec.gov.evil.com")).toBe(false);
  });
});

describe("fetchUrlProblem (mirrors FetchSourceRequest.validate_public_https_url)", () => {
  it("accepts open public https pages and direct SEC filing HTML", () => {
    expect(fetchUrlProblem("https://investor.nvidia.com/news/press-release-details/2025/nvidia-announces-financial-results")).toBeNull();
    expect(fetchUrlProblem("https://news.example.com/a")).toBeNull();
    expect(fetchUrlProblem("https://www.federalreserve.gov/newsevents/pressreleases/monetary20260917a.htm")).toBeNull();
    expect(fetchUrlProblem("https://www.bea.gov/news/2026/gdp")).toBeNull();
    expect(fetchUrlProblem("https://www.sec.gov/Archives/edgar/data/1000001/000100000126000012/nwex-20260630.htm")).toBeNull();
  });

  it("refuses SEC addresses when the deployment has no SEC contact configured", () => {
    const filing = "https://www.sec.gov/Archives/edgar/data/1000001/000100000126000012/nwex-20260630.htm";
    expect(fetchUrlProblem(filing, false)).toBe(SEC_UNAVAILABLE);
    expect(isFetchableUrl(filing, false)).toBe(false);
    expect(isFetchableUrl("https://www.bea.gov/news/2026/gdp", false)).toBe(true);
    expect(isFetchableUrl("https://investor.nvidia.com/reports/2026.pdf", false)).toBe(true);
  });

  it("respects restricted fetch allowlist when webFetchEnabled is false", () => {
    expect(fetchUrlProblem("https://news.example.com/a", true, { webFetchEnabled: false, allowedHosts: ["www.bea.gov"] })).toBe(
      "当前环境限制了开放网页抓取，可以保存后按文件添加",
    );
    expect(fetchUrlProblem("https://www.bea.gov/news/2026/gdp", true, { webFetchEnabled: false, allowedHosts: ["www.bea.gov"] })).toBeNull();
  });

  it("rejects unsafe, private, non-https, FRED and invalid SEC addresses with exact messages", () => {
    expect(fetchUrlProblem("")).toBe("请输入网址");
    expect(fetchUrlProblem("www.sec.gov")).toBe("网址格式不正确");
    expect(fetchUrlProblem("https://example.com\\path")).toBe("网址格式不正确");
    expect(fetchUrlProblem("http://www.bea.gov/")).toBe("只支持 https 网址");
    expect(fetchUrlProblem("https://user:pw@www.bea.gov/")).toBe("网址不能包含账号或密码");
    expect(fetchUrlProblem("https://www.bea.gov:8443/")).toBe("网址不能指定端口");
    expect(fetchUrlProblem("https://www.bea.gov/x#part")).toBe("网址不能包含 # 后的片段");
    expect(fetchUrlProblem("https://localhost/report")).toBe("只支持公网网址，不能使用本机或内网地址");
    expect(fetchUrlProblem("https://internal.local/report")).toBe("只支持公网网址，不能使用本机或内网地址");
    expect(fetchUrlProblem("https://127.0.0.1/report")).toBe("只支持公网网址，不能使用本机或内网地址");
    expect(fetchUrlProblem("https://127.1/report")).toBe("只支持公网网址，不能使用本机或内网地址");
    expect(fetchUrlProblem("https://2130706433/report")).toBe("只支持公网网址，不能使用本机或内网地址");
    expect(fetchUrlProblem("https://10.0.0.1/report")).toBe("只支持公网网址，不能使用本机或内网地址");
    expect(fetchUrlProblem("https://100.64.0.1/report")).toBe("只支持公网网址，不能使用本机或内网地址");
    expect(fetchUrlProblem("https://[::1]/report")).toBe("只支持公网网址，不能使用本机或内网地址");
    expect(fetchUrlProblem("https://[::ffff:127.0.0.1]/report")).toBe("只支持公网网址，不能使用本机或内网地址");
    expect(fetchUrlProblem("https://0177.0.0.1/report")).toBe("只支持公网网址，不能使用非标准格式的 IP 地址");
    expect(fetchUrlProblem("https://intranet/report")).toBe("只支持公网域名网址");
    expect(fetchUrlProblem("https://fred.stlouisfed.org/series/GDP")).toBe("FRED 资料暂未获准用于研究");
    expect(fetchUrlProblem("https://sub.alfred.stlouisfed.org/series/GDP")).toBe("FRED 资料暂未获准用于研究");
    expect(fetchUrlProblem("https://api.stlouisfed.org/fred/series?series_id=GDP")).toBe("FRED 资料暂未获准用于研究");
    expect(fetchUrlProblem("https://api.stlouisfed.org/%66red/series/observations")).toBe("FRED 资料暂未获准用于研究");
    expect(fetchUrlProblem("https://api.stlouisfed.org//fred/series")).toBe("FRED 资料暂未获准用于研究");
    expect(fetchUrlProblem("https://api.stlouisfed.org/v1/../fred/series")).toBe("FRED 资料暂未获准用于研究");
    expect(fetchUrlProblem("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany")).toBe("只支持 SEC EDGAR 申报文件的 HTML 页面地址");
    expect(fetchUrlProblem("https://www.sec.gov/Archives/edgar/data/1000001/000100000126000012/nwex.pdf")).toBe("只支持 SEC EDGAR 申报文件的 HTML 页面地址");
    const nonWwwSecMsg = "目前只支持 SEC 申报文件页面（www.sec.gov/Archives/edgar/data/...）和研究任务中的 SEC 结构化数据查询。";
    expect(fetchUrlProblem("https://sec.gov/edgar/searchedgar/companysearch")).toBe(nonWwwSecMsg);
    expect(fetchUrlProblem("https://efts.sec.gov/LATEST/search-index?q=NVDA")).toBe(nonWwwSecMsg);
    expect(fetchUrlProblem("https://data.sec.gov/api/xbrl/companyconcept/CIK0001045810/us-gaap/Revenues.json")).toBe(nonWwwSecMsg);
    expect(fetchUrlProblem("https://data.sec.gov/api/xbrl/companyconcept/CIK0001045810/us-gaap/Revenues.json", false)).toBe(SEC_UNAVAILABLE);
    expect(isFetchableUrl("https://efts.sec.gov/LATEST/search-index")).toBe(false);
    expect(fetchUrlProblem("https://www.sec.gov.evil.com/report")).toBeNull();
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
