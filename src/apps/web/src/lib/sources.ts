import type { SourcePermissionFlags } from "../api/types";

// Mirrors of backend source rules so the UI can say, before a run, whether a
// source will be read. The backend stays authoritative; these only explain.

export const MAX_UPLOAD_BYTES = 5_000_000; // research_uploads.py MAX_PDF_BYTES
export const MAX_TEXT_CHARACTERS = 2_000_000; // research_sources.py SourceInput.text
export const MAX_SOURCES_PER_RUN = 20; // research_source_policy.py MAX_SOURCES

/** The user supplies these files, so all four uses are allowed unless they turn one off. */
export const DEFAULT_PERMISSIONS: SourcePermissionFlags = { store: true, model_input: true, display: true, export: true };

export type IneligibleCode = "simulated" | "source_not_authorized" | "model_permission_missing" | "display_permission_missing" | "no_published_at" | "after_cutoff";

export const INELIGIBLE_TEXT: Readonly<Record<IneligibleCode, string>> = {
  simulated: "模拟数据不会作为真实证据读取",
  source_not_authorized: "FRED 资料暂未获准用于研究",
  model_permission_missing: "没有允许供模型读取",
  display_permission_missing: "没有允许展示原文，引文无法核对",
  no_published_at: "没有公开时间，无法确认在截止时间前已公开",
  after_cutoff: "公开或修订时间晚于研究截止时间",
};

export interface EligibilityInput {
  is_simulated?: boolean | undefined;
  url?: string | null | undefined;
  permissions: SourcePermissionFlags;
  published_at?: string | null | undefined;
  revised_at?: string | null | undefined;
}

export function isFredUrl(raw: string): boolean {
  let host = "";
  let path = "";
  try {
    const url = new URL(raw);
    host = url.hostname.toLowerCase().replace(/\.$/, "");
    path = url.pathname.toLowerCase();
  } catch {
    return false;
  }
  return host === "fred.stlouisfed.org" || host === "alfred.stlouisfed.org" || host.endsWith(".fred.stlouisfed.org")
    || (host === "api.stlouisfed.org" && path.startsWith("/fred"));
}

/** research_source_policy.source_eligibility, in the same order. Null means the next run reads it. */
export function ineligibility(source: EligibilityInput, asOf: string | null | undefined): IneligibleCode | null {
  if (source.is_simulated) return "simulated";
  if (source.url && isFredUrl(source.url)) return "source_not_authorized";
  if (!source.permissions.model_input) return "model_permission_missing";
  if (!source.permissions.display) return "display_permission_missing";
  if (asOf) {
    const cutoff = Date.parse(asOf);
    if (!source.published_at) return "no_published_at";
    if (Date.parse(source.published_at) > cutoff) return "after_cutoff";
    if (source.revised_at && Date.parse(source.revised_at) > cutoff) return "after_cutoff";
  }
  return null;
}

const FETCH_HOSTS = new Set(["www.bea.gov", "www.federalreserve.gov", "www.sec.gov"]);
const SEC_FILING_PATH = /^\/Archives\/edgar\/data\/\d{1,10}\/\d{18}\/[A-Za-z0-9][A-Za-z0-9._-]{0,150}\.html?$/;

export const FETCH_HELP = "支持 SEC EDGAR 申报文件页面，以及美联储（www.federalreserve.gov）和美国经济分析局（www.bea.gov）的 HTTPS 页面。其他网页请保存后按文件添加。";

export const SEC_UNAVAILABLE = "这个环境还没有配置 SEC 联系信息，暂时无法获取 SEC 资料。";

/**
 * research_fetch.FetchSourceRequest.allow_official_https_only. Returns a user-facing problem or null.
 * `secAvailable` comes from /v2/research/capabilities; SEC addresses need a configured contact.
 */
export function fetchUrlProblem(raw: string, secAvailable = true): string | null {
  const value = raw.trim();
  if (!value) return "请输入网址";
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return "网址格式不正确";
  }
  if (url.protocol !== "https:") return "只支持 https 网址";
  if (url.username || url.password) return "网址不能包含账号或密码";
  if (url.port && url.port !== "443") return "网址不能指定端口";
  if (url.hash) return "网址不能包含 # 后的片段";
  if (!FETCH_HOSTS.has(url.hostname)) return "这个网站目前不能直接获取";
  if (url.hostname === "www.sec.gov" && (url.search || !SEC_FILING_PATH.test(url.pathname))) return "只支持 SEC EDGAR 申报文件的 HTML 页面地址";
  if (url.hostname === "www.sec.gov" && !secAvailable) return SEC_UNAVAILABLE;
  return null;
}

export function isFetchableUrl(raw: string, secAvailable = true): boolean {
  return fetchUrlProblem(raw, secAvailable) === null;
}

export type UploadKind = "pdf" | "html";

export function uploadKind(file: { name: string; type: string }): UploadKind | null {
  const name = file.name.toLowerCase();
  if (file.type === "application/pdf" || name.endsWith(".pdf")) return "pdf";
  if (file.type === "text/html" || name.endsWith(".html") || name.endsWith(".htm")) return "html";
  return null;
}

export function titleFromFilename(name: string): string {
  const base = name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ").trim();
  return (base || name).slice(0, 500);
}

export function formatBytes(bytes: number): string {
  if (bytes < 1000) return `${bytes} B`;
  if (bytes < 1_000_000) return `${Math.round(bytes / 100) / 10} KB`;
  return `${Math.round(bytes / 100_000) / 10} MB`;
}

/** Base64 of the raw bytes, without the data-URL prefix. */
export async function fileToBase64(file: Blob): Promise<string> {
  const buffer = await new Promise<ArrayBuffer>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.onerror = () => reject(reader.error ?? new Error("file_read_failed"));
    reader.readAsArrayBuffer(file);
  });
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunk = 0x8000;
  for (let index = 0; index < bytes.length; index += chunk) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunk));
  }
  return btoa(binary);
}
