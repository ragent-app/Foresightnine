import type { SourcePermissionFlags } from "../api/types";

// Mirrors of backend source rules so the UI can say, before a run, whether a
// source will be read. The backend stays authoritative; these only explain.

export const MAX_UPLOAD_BYTES = 5_000_000; // research_uploads.py MAX_PDF_BYTES: largest base64 (JSON) upload
export const MAX_LARGE_UPLOAD_BYTES = 200_000_000; // FORESIGHTNINE_LARGE_DOCUMENT_MAX_BYTES default: streamed multipart upload
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

function normalizeUrlPathname(rawPath: string): string {
  let decoded = rawPath;
  for (let pass = 0; pass < 2; pass += 1) {
    const next = decoded.replace(/(?:%[0-9a-fA-F]{2})+/g, (sequence) => {
      try {
        return decodeURIComponent(sequence);
      } catch {
        return sequence;
      }
    });
    if (next === decoded) break;
    decoded = next;
  }
  const segments: string[] = [];
  for (const segment of decoded.replace(/\/+/g, "/").split("/")) {
    if (!segment || segment === ".") continue;
    if (segment === "..") segments.pop();
    else segments.push(segment);
  }
  return `/${segments.join("/")}`.toLowerCase();
}

export function isFredUrl(raw: string): boolean {
  let host = "";
  let path = "";
  try {
    const url = new URL(raw);
    host = url.hostname.toLowerCase().replace(/\.$/, "");
    path = normalizeUrlPathname(url.pathname);
  } catch {
    return false;
  }
  return (
    host === "fred.stlouisfed.org" ||
    host.endsWith(".fred.stlouisfed.org") ||
    host === "alfred.stlouisfed.org" ||
    host.endsWith(".alfred.stlouisfed.org") ||
    (host === "api.stlouisfed.org" && (path === "/fred" || path.startsWith("/fred/")))
  );
}

export function isSecHost(host: string): boolean {
  const normalized = host.toLowerCase().replace(/\.$/, "");
  return normalized === "sec.gov" || normalized.endsWith(".sec.gov");
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

const SEC_FILING_PATH = /^\/Archives\/edgar\/data\/\d{1,10}\/\d{18}\/[A-Za-z0-9][A-Za-z0-9._-]{0,150}\.html?$/;
const NON_PUBLIC_SUFFIXES = [".localhost", ".local", ".internal", ".test", ".invalid", ".arpa"] as const;
const MAX_FETCH_URL_LENGTH = 2048;

export const FETCH_HELP = "支持公开 HTTPS 网页（HTML、纯文本、PDF）以及 SEC EDGAR 申报文件页面，暂不支持 FRED；如果目标网站拦截自动访问，可以保存后按文件添加。巨潮资讯、上交所、深交所的链接仅在运营方开启个人使用模式时，才能按你粘贴的单个文档获取；港交所披露易始终不可用，可改用公司官网的副本。";

export const SEC_UNAVAILABLE = "这个环境还没有配置 SEC 联系信息，暂时无法获取 SEC 资料。";

export interface FetchUrlOptions {
  webFetchEnabled?: boolean;
  allowedHosts?: readonly string[] | undefined;
}

function parseIpv4Octets(host: string): readonly [number, number, number, number] | null {
  if (!/^\d+(?:\.\d+){0,3}$/.test(host)) return null;
  const parts = host.split(".").map((part) => Number(part));
  if (parts.length < 1 || parts.length > 4) return null;
  if (parts.length === 1) {
    const v = parts[0]!;
    if (!Number.isInteger(v) || v < 0 || v > 0xffffffff) return null;
    return [(v >>> 24) & 0xff, (v >>> 16) & 0xff, (v >>> 8) & 0xff, v & 0xff];
  }
  const lastLimit = parts.length === 2 ? 0xffffff : parts.length === 3 ? 0xffff : 0xff;
  for (let index = 0; index < parts.length; index += 1) {
    const value = parts[index]!;
    const limit = index < parts.length - 1 ? 0xff : lastLimit;
    if (!Number.isInteger(value) || value < 0 || value > limit) return null;
  }
  const value = parts.length === 2
    ? ((parts[0]! << 24) >>> 0) + parts[1]!
    : parts.length === 3
      ? ((parts[0]! << 24) >>> 0) + (parts[1]! << 16) + parts[2]!
      : ((parts[0]! << 24) >>> 0) + (parts[1]! << 16) + (parts[2]! << 8) + parts[3]!;
  return [(value >>> 24) & 0xff, (value >>> 16) & 0xff, (value >>> 8) & 0xff, value & 0xff];
}

function isNonPublicIpv4([a, b, c]: readonly [number, number, number, number]): boolean {
  return (
    a === 0 ||
    a === 10 ||
    (a === 100 && b >= 64 && b <= 127) ||
    a === 127 ||
    (a === 169 && b === 254) ||
    (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && b === 0 && (c === 0 || c === 2)) ||
    (a === 192 && b === 168) ||
    (a === 198 && (b === 18 || b === 19)) ||
    (a === 198 && b === 51 && c === 100) ||
    (a === 203 && b === 0 && c === 113) ||
    a >= 224
  );
}

function parseIpv6Groups(host: string): number[] | null {
  const halves = host.split("::");
  if (halves.length > 2) return null;
  const parseSide = (side: string): number[] | null => {
    if (!side) return [];
    const tokens = side.split(":");
    const groups: number[] = [];
    for (let index = 0; index < tokens.length; index += 1) {
      const token = tokens[index]!;
      if (index === tokens.length - 1 && token.includes(".")) {
        const octets = parseIpv4Octets(token);
        if (!octets || token.split(".").length !== 4) return null;
        groups.push((octets[0] << 8) | octets[1], (octets[2] << 8) | octets[3]);
      } else if (/^[0-9a-f]{1,4}$/i.test(token)) {
        groups.push(Number.parseInt(token, 16));
      } else {
        return null;
      }
    }
    return groups;
  };
  const left = parseSide(halves[0]!);
  if (!left) return null;
  if (halves.length === 1) return left.length === 8 ? left : null;
  const right = parseSide(halves[1]!);
  if (!right) return null;
  const missing = 8 - left.length - right.length;
  if (missing < 1) return null;
  return [...left, ...Array<number>(missing).fill(0), ...right];
}

function isNonPublicIpv6(groups: readonly number[]): boolean {
  const [g0, g1, g2, g3, g4, g5, g6, g7] = groups as [number, number, number, number, number, number, number, number];
  const allZeroPrefix = g0 === 0 && g1 === 0 && g2 === 0 && g3 === 0 && g4 === 0;
  if (allZeroPrefix && g5 === 0) {
    if (g6 === 0 && (g7 === 0 || g7 === 1)) return true;
    return isNonPublicIpv4([(g6 >> 8) & 0xff, g6 & 0xff, (g7 >> 8) & 0xff, g7 & 0xff]);
  }
  if (allZeroPrefix && g5 === 0xffff) {
    return isNonPublicIpv4([(g6 >> 8) & 0xff, g6 & 0xff, (g7 >> 8) & 0xff, g7 & 0xff]);
  }
  if (g0 === 0x0064 && g1 === 0xff9b && ((g2 === 0 && g3 === 0 && g4 === 0 && g5 === 0) || g2 === 1)) return true;
  if (g0 === 0x0100 && g1 === 0 && g2 === 0 && g3 === 0) return true;
  if (g0 === 0x2001 && g1 === 0x0db8) return true;
  if ((g0 & 0xfe00) === 0xfc00) return true;
  if ((g0 & 0xffc0) === 0xfe80) return true;
  if ((g0 & 0xff00) === 0xff00) return true;
  return false;
}

/**
 * Mirrors research_fetch.FetchSourceRequest.validate_public_https_url.
 * Returns a user-facing problem or null; the backend performs DNS resolution and remains authoritative.
 */
export function fetchUrlProblem(raw: string, secAvailable = true, options?: FetchUrlOptions): string | null {
  const value = raw.trim();
  if (!value) return "请输入网址";
  if (value.length > MAX_FETCH_URL_LENGTH || value.includes("\\")) return "网址格式不正确";
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

  const rawAuthority = /^https:\/\/([^/?#]+)/i.exec(value)?.[1] ?? "";
  const rawHost = rawAuthority.split("@").pop()?.replace(/:\d+$/, "").toLowerCase().replace(/\.$/, "") ?? "";
  if (rawHost && !rawHost.startsWith("[")) {
    const rawLabels = rawHost.split(".");
    if (
      rawLabels.every((label) => /^(?:0x[0-9a-f]+|0[0-7]+|\d+)$/.test(label)) &&
      rawLabels.some((label) => label.startsWith("0x") || (label.length > 1 && label.startsWith("0")))
    ) {
      return "只支持公网网址，不能使用非标准格式的 IP 地址";
    }
    const rawIpv4 = parseIpv4Octets(rawHost);
    if (rawIpv4 && isNonPublicIpv4(rawIpv4)) return "只支持公网网址，不能使用本机或内网地址";
  }

  const host = url.hostname.toLowerCase().replace(/\.$/, "");
  const bareHost = host.replace(/^\[|\]$/g, "");
  if (!bareHost || bareHost.length > 253 || bareHost === "localhost" || NON_PUBLIC_SUFFIXES.some((suffix) => bareHost.endsWith(suffix))) {
    return "只支持公网网址，不能使用本机或内网地址";
  }
  if (host.startsWith("[") || bareHost.includes(":")) {
    const groups = parseIpv6Groups(bareHost);
    if (!groups || isNonPublicIpv6(groups)) return "只支持公网网址，不能使用本机或内网地址";
  } else {
    const ipv4 = parseIpv4Octets(bareHost);
    if (ipv4) {
      if (isNonPublicIpv4(ipv4)) return "只支持公网网址，不能使用本机或内网地址";
    } else {
      const labels = bareHost.split(".");
      if (labels.length < 2 || labels.some((label) => !label)) return "只支持公网域名网址";
    }
  }

  if (isFredUrl(value)) return INELIGIBLE_TEXT.source_not_authorized;
  if (isSecHost(bareHost)) {
    if (bareHost === "www.sec.gov") {
      if (url.search || !SEC_FILING_PATH.test(url.pathname)) return "只支持 SEC EDGAR 申报文件的 HTML 页面地址";
      if (!secAvailable) return SEC_UNAVAILABLE;
      return null;
    }
    if (!secAvailable) return SEC_UNAVAILABLE;
    return "目前只支持 SEC 申报文件页面（www.sec.gov/Archives/edgar/data/...）和研究任务中的 SEC 结构化数据查询。";
  }
  if (options?.webFetchEnabled === false && !(options.allowedHosts ?? []).includes(bareHost)) {
    return "当前环境限制了开放网页抓取，可以保存后按文件添加";
  }
  return null;
}

export function isFetchableUrl(raw: string, secAvailable = true, options?: FetchUrlOptions): boolean {
  return fetchUrlProblem(raw, secAvailable, options) === null;
}

export type UploadKind = "pdf" | "html" | "text";

export const UPLOAD_KIND_LABEL: Readonly<Record<UploadKind, string>> = { pdf: "PDF", html: "HTML", text: "文本" };

/** Files above the JSON limit, and plain text, go through the streaming multipart endpoint. */
export function usesStreamingUpload(file: { size: number }, kind: UploadKind): boolean {
  return kind === "text" || file.size > MAX_UPLOAD_BYTES;
}

export function uploadKind(file: { name: string; type: string }): UploadKind | null {
  const name = file.name.toLowerCase();
  if (file.type === "application/pdf" || name.endsWith(".pdf")) return "pdf";
  if (file.type === "text/html" || name.endsWith(".html") || name.endsWith(".htm")) return "html";
  if (file.type === "text/plain" || name.endsWith(".txt")) return "text";
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
