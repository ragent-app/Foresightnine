export interface SafeExternalLink {
  href: string;
  host: string;
}

const CONTROL_CHAR_RE = /[\u0000-\u001f\u007f]/;
const IPV4_RE = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/;

function isDisallowedIpv4(host: string): boolean {
  const match = IPV4_RE.exec(host);
  if (!match) return false;
  const octets = match.slice(1).map(Number);
  if (octets.some((n) => !Number.isInteger(n) || n < 0 || n > 255)) return true;
  const [a, b] = octets as [number, number, number, number];
  if (a === 0 || a === 10 || a === 127) return true;
  if (a === 100 && b >= 64 && b <= 127) return true;
  if (a === 169 && b === 254) return true;
  if (a === 172 && b >= 16 && b <= 31) return true;
  if (a === 192 && b === 168) return true;
  if (a === 192 && b === 0) return true;
  if (a === 198 && (b === 18 || b === 19)) return true;
  if (a >= 224) return true;
  return false;
}

function isDisallowedHost(rawHost: string): boolean {
  if (!rawHost) return true;
  const host = rawHost.toLowerCase().replace(/^\[|\]$/g, "");
  if (!host) return true;
  if (
    host === "localhost"
    || host.endsWith(".localhost")
    || host.endsWith(".local")
    || host.endsWith(".internal")
    || host.endsWith(".localdomain")
    || host.endsWith(".arpa")
  ) {
    return true;
  }
  if (host.includes(":")) {
    // Reject all literal IPv6 addresses (loopback ::1, link-local fe80::, ULA fc00::/7, v4-mapped).
    return true;
  }
  if (IPV4_RE.test(host)) {
    return isDisallowedIpv4(host);
  }
  if (!host.includes(".") || host.endsWith(".")) return true;
  return false;
}

/**
 * Validates and normalizes an external Markdown link.
 * Allows only public `https://` URLs with no embedded credentials; rejects `http:`,
 * `javascript:`, `data:`, `vbscript:`, `file:`, localhost, and private/metadata IPs.
 */
export function safeExternalHref(raw: string | null | undefined): SafeExternalLink | null {
  if (typeof raw !== "string") return null;
  const trimmed = raw.trim();
  if (!trimmed || CONTROL_CHAR_RE.test(trimmed)) return null;
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return null;
  }
  if (parsed.protocol !== "https:") return null;
  if (parsed.username || parsed.password) return null;
  if (isDisallowedHost(parsed.hostname)) return null;
  return { href: parsed.href, host: parsed.hostname.toLowerCase() };
}

const DASH_OR_NA_RE = /^(?:—|–|-|N\/A|n\/a|NA)$/i;
const FINANCIAL_NUMBER_RE =
  /^\(?[+-−]?(?:[$€£¥₩]|USD|RMB|CNY|HKD|EUR)?\s*\d[\d,]*(?:\.\d+)?(?:\s*(?:%|pp|bps|[xX]|[BMKT]|万亿|亿|千万|百万|万|倍|百分点|美元|港元|元|百万美元|亿美元))?\)?(?:\s*[–—~-]\s*\(?[+-−]?(?:[$€£¥₩]|USD|RMB|CNY|HKD|EUR)?\s*\d[\d,]*(?:\.\d+)?(?:\s*(?:%|pp|bps|[xX]|[BMKT]|万亿|亿|千万|百万|万|倍|百分点|美元|港元|元|百万美元|亿美元))?\)?)?$/u;

/** Identifies numeric/ratio/placeholder table cells for tabular right-alignment. */
export function isNumericCellText(raw: string): boolean {
  const trimmed = raw.trim();
  if (!trimmed) return false;
  if (DASH_OR_NA_RE.test(trimmed)) return true;
  return FINANCIAL_NUMBER_RE.test(trimmed);
}

/** Extracts a GFM table DOM element into clean tab-separated values for spreadsheet paste. */
export function extractTableTsv(tableEl: HTMLTableElement): string {
  const rows: string[] = [];
  for (const row of tableEl.querySelectorAll("tr")) {
    const cells: string[] = [];
    for (const cell of row.querySelectorAll("th, td")) {
      const normalized = (cell.textContent ?? "").replace(/[\t\r\n]+/g, " ").trim();
      cells.push(normalized);
    }
    if (cells.length > 0) rows.push(cells.join("\t"));
  }
  return rows.join("\n");
}
