// Dependency-free mock of the ForesightNine research API for local UI QA.
// All companies, sources and numbers are fictional. State is in memory only.
import http from "node:http";
import { createHash, randomBytes } from "node:crypto";
import { pathToFileURL } from "node:url";
import {
  KEYED_MARKET_PROVIDERS,
  MARKET_PROVIDER_ORDER,
  PARTIAL_GAPS,
  PLATFORM_AVAILABLE,
  PLATFORM_MODEL,
  PROVIDER_LABELS,
  QUOTA_LIMIT,
  SEEDED_QUOTA_CONSUMED,
  Q_OPI25,
  Q_OPI26,
  RUN_STEPS,
  S1_ID,
  citation,
  fictionalPdfText,
  isEligibleForRun,
  iso,
  makeOperation,
  makeSource,
  operatingParagraphRange,
  rawEvent,
  seed,
  sha256,
  span,
  summarizeSources,
  usageFromEvents,
} from "./fixtures.mjs";

const DOCUMENT_OVERVIEW_QUESTION =
  "Summarize the supplied material's main verifiable findings, relevant figures, " +
  "limitations and unresolved questions. Cite the material and do not assume an investment decision.";
const FULL_EXPORT_DISCLOSURE = "Model interpretations and declared source permissions are not independently verified.";
const CALCULATION_DISCLOSURE =
  "Calculations use the recorded inputs; their source precision and prior rounding are not established.";
const METADATA_DISCLOSURE = "No research text or source content is included. Full export requires source permissions.";
const MAX_BODY_BYTES = 8 * 1024 * 1024; // PDF/HTML uploads carry up to 5 MB as base64
const MAX_ORIGINAL_BYTES = 5_000_000;
const HTML_EXTRACTION_DISCLOSURE =
  "Webpage text is extracted; table separators and span markers, when present, " +
  "are generated. Extraction does not reproduce browser rendering; check the original document for layout and visibility.";
const SEC_LIMITATION = "Current SEC ticker mapping only; exchange, security class and historical validity are not established.";
const HTML_METHODS = new Set(["official_html", "user_html", "sec_filing_html"]);
const ALLOWED_FETCH_HOSTS = new Set(["www.bea.gov", "www.federalreserve.gov", "www.sec.gov"]);
const SEC_FILING_PATH = /^\/Archives\/edgar\/data\/\d{1,10}\/\d{18}\/[A-Za-z0-9][A-Za-z0-9._-]{0,150}\.html?$/;
const SEC_INDEX = [
  { cik: 1000001, ticker: "NWEX", name: "Northwind Example Corp" },
  { cik: 1000002, ticker: "CTEX", name: "Contoso Example Holdings" },
  { cik: 1000003, ticker: "FBEX", name: "Fabrikam Example Inc" },
];
const DISCOVERY_CANDIDATES = [
  { url: "https://www.sec.gov/Archives/edgar/data/1000001/000100000126000012/nwex-20260630.htm", title: "Northwind Example Corp 10-K (fictional)", published_at: "2026-08-12T00:00:00Z" },
  { url: "https://news.example.com/northwind-capex", title: "示例新闻：Northwind 资本开支", published_at: "2026-10-20T00:00:00Z" },
];
const DISCOVERY_STEP = {
  kind: "tool",
  name: "news_search",
  status: "completed",
  rationale: "搜索公开线索",
  summary: {
    kind: "news_search",
    found_count: 2,
    eligible_count: 1,
    denied_count: 1,
    leads_preview: DISCOVERY_CANDIDATES.map((item) => ({
      title: item.title,
      host: new URL(item.url).host,
      published_at: item.published_at,
    })),
  },
};

class HttpError extends Error {
  constructor(status, code, message, { retryable = false, details = [], headers = {} } = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.retryable = retryable;
    this.details = details;
    this.headers = headers;
  }
}

const notFound = (what = "Resource") => new HttpError(404, "not_found", `${what} not found.`);
const invalid = (message, location = "body") =>
  new HttpError(422, "validation_error", "Request failed strict contract validation.", {
    details: [{ location, message }],
  });

function stableStringify(value) {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

const isRecord = (v) => typeof v === "object" && v !== null && !Array.isArray(v);
const isStringArray = (v) => Array.isArray(v) && v.every((item) => typeof item === "string");
const isNullableInt = (v) => v === null || (Number.isInteger(v) && v >= 0);

function send(res, status, body, headers = {}) {
  if (status === 204) {
    res.writeHead(204, { "Cache-Control": "no-store", ...headers });
    res.end();
    return;
  }
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(payload),
    "Cache-Control": "no-store",
    ...headers,
  });
  res.end(payload);
}

function sendError(res, error) {
  send(res, error.status, {
    error_code: error.code,
    message: error.message,
    request_id: "req_mock",
    retryable: error.retryable,
    details: error.details,
  }, error.headers);
}

async function readJson(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) throw new HttpError(413, "bad_request", "Request body is too large.");
    chunks.push(chunk);
  }
  const raw = Buffer.concat(chunks).toString("utf8");
  if (!raw.trim()) return undefined;
  try {
    return JSON.parse(raw);
  } catch {
    throw new HttpError(400, "bad_request", "Request body is not valid JSON.");
  }
}

// ---- Validation ---------------------------------------------------------------

const RUN_KEYS = new Set([
  "auto_fetch_official",
  "discover_sources",
  "focus_citation",
  "max_cost_usd",
  "max_elapsed_seconds",
  "max_input_tokens_per_call",
  "max_model_calls",
  "max_search_calls",
  "max_total_tokens",
  "parent_version_id",
  "question_override",
  "selected_source_ids",
  "source_ids",
]);

function validateRunRequest(body) {
  if (!isRecord(body)) throw invalid("Body must be a JSON object.");
  for (const key of Object.keys(body)) if (!RUN_KEYS.has(key)) throw invalid("Extra inputs are not permitted.", `body/${key}`);
  if (typeof body.parent_version_id !== "string" || !body.parent_version_id) {
    throw invalid("Field required.", "body/parent_version_id");
  }
  if ("auto_fetch_official" in body && typeof body.auto_fetch_official !== "boolean") throw invalid("Input should be a boolean.", "body/auto_fetch_official");
  if ("discover_sources" in body && body.discover_sources !== null && typeof body.discover_sources !== "boolean") throw invalid("Input should be a boolean.", "body/discover_sources");
  if ("question_override" in body && body.question_override !== null && typeof body.question_override !== "string") throw invalid("Input should be a string.", "body/question_override");
  if ("source_ids" in body && !isStringArray(body.source_ids)) throw invalid("Input should be a list of strings.", "body/source_ids");
  if ("selected_source_ids" in body && body.selected_source_ids !== null && !isStringArray(body.selected_source_ids)) throw invalid("Input should be a list of strings.", "body/selected_source_ids");
  if (Array.isArray(body.selected_source_ids) &&
      (body.selected_source_ids.some((id) => !id.trim()) || new Set(body.selected_source_ids).size !== body.selected_source_ids.length)) {
    throw invalid("Selected source IDs must be nonblank and unique.", "body/selected_source_ids");
  }
  for (const key of ["max_elapsed_seconds", "max_input_tokens_per_call", "max_model_calls", "max_search_calls", "max_total_tokens"]) {
    if (key in body && !isNullableInt(body[key])) throw invalid("Input should be a non-negative integer.", `body/${key}`);
  }
  if ("max_cost_usd" in body && body.max_cost_usd !== null && typeof body.max_cost_usd !== "number" && typeof body.max_cost_usd !== "string") {
    throw invalid("Input should be a number or string.", "body/max_cost_usd");
  }
  const focus = body.focus_citation;
  if (focus !== undefined && focus !== null) {
    if (!isRecord(focus) || typeof focus.claim_id !== "string" || !Number.isInteger(focus.citation_index) || focus.citation_index < 0 ||
        Object.keys(focus).some((key) => key !== "claim_id" && key !== "citation_index")) {
      throw invalid("Input should be {claim_id, citation_index}.", "body/focus_citation");
    }
  }
}

function validateResearchInput(body) {
  if (!isRecord(body)) throw invalid("Body must be a JSON object.");
  for (const key of Object.keys(body)) {
    if (!["as_of", "question", "scope", "source_ids"].includes(key)) throw invalid("Extra inputs are not permitted.", `body/${key}`);
  }
  if ("question" in body && typeof body.question !== "string") throw invalid("Input should be a string.", "body/question");
  if ("scope" in body && typeof body.scope !== "string") throw invalid("Input should be a string.", "body/scope");
  if ("source_ids" in body && !isStringArray(body.source_ids)) throw invalid("Input should be a list of strings.", "body/source_ids");
  if ("as_of" in body && body.as_of !== null && (typeof body.as_of !== "string" || Number.isNaN(Date.parse(body.as_of)))) {
    throw invalid("Input should be a valid datetime.", "body/as_of");
  }
}

// ---- Deterministic calculation engine (mirrors research_calculations.calculate) ------------

class CalcError extends Error {}

const DECIMAL = /^(-?)(\d+)(?:\.(\d+))?$/;

/** Exact decimal: value = n / 10^s. Mirrors NumericFact's finite, <=30-digit, |exponent|<=30 rule. */
function parseDecimal(text) {
  const match = typeof text === "string" ? DECIMAL.exec(text.trim()) : null;
  if (!match) throw new CalcError("Value must be a plain decimal string.");
  const [, sign, whole, fraction = ""] = match;
  const digits = (whole + fraction).replace(/^0+/, "");
  const adjusted = digits ? digits.length - 1 - fraction.length : -fraction.length;
  if (digits.length > 30 || Math.abs(adjusted) > 30) throw new CalcError("Value must be finite and within supported precision.");
  return { n: BigInt(`${sign}${whole}${fraction}`), s: fraction.length };
}

const pow10 = (exponent) => 10n ** BigInt(exponent);
const alignDecimals = (a, b) => {
  const s = Math.max(a.s, b.s);
  return { A: a.n * pow10(s - a.s), B: b.n * pow10(s - b.s), s };
};
const decimalEquals = (x, y) => {
  const { A, B } = alignDecimals(parseDecimal(x), parseDecimal(y));
  return A === B;
};

/** Round num/den (den > 0) half-up (ties away from zero) to `places` and print as a fixed-decimal string. */
function formatQuotient(num, den, places) {
  const scaled = num * pow10(places);
  const magnitude = scaled < 0n ? -scaled : scaled;
  let q = magnitude / den;
  if (2n * (magnitude % den) >= den) q += 1n;
  const digits = q.toString().padStart(places + 1, "0");
  const body = places ? `${digits.slice(0, -places)}.${digits.slice(-places)}` : digits;
  return `${scaled < 0n ? "-" : ""}${body}`;
}

const PERCENT_UNIT = /^(?:(?:annual|annualized|annualised)\s+)?(?:percent|percentage|%|百分比)(?:\s*\([^()]{1,80}\)|(?:,\s*|\s+)(?:at\s+an?\s+)?(?:seasonally\s+adjusted\s+)?(?:annualized(?:\s+rate)?|annualised(?:\s+rate)?|annual\s+rate))?$/;

function isPercentUnit(unit) {
  const normalized = String(unit).trim().toLowerCase().split(/\s+/).filter(Boolean).join(" ");
  if (/\bpoints?\b|百分点/.test(normalized)) return false;
  return PERCENT_UNIT.test(normalized);
}

const MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"];
const validDay = (y, m, d) => {
  const date = new Date(Date.UTC(y, m - 1, d));
  return date.getUTCFullYear() === y && date.getUTCMonth() === m - 1 && date.getUTCDate() === d;
};

function parseAnnualPeriodEnd(period) {
  const match = /^(?:(?:FY\s*((?:19|20)\d{2}))\s*\(\s*)?(?:fiscal\s+)?year\s+ended\s+([A-Za-z]+)\s+(\d{1,2}),?\s+((?:19|20)\d{2})(\s*\))?$/i.exec(period.trim());
  if (!match || Boolean(match[1]) !== Boolean(match[5])) return null;
  const month = MONTHS.indexOf(match[2].toLowerCase()) + 1;
  const year = Number(match[4]);
  if (!month || !validDay(year, month, Number(match[3]))) return null;
  return match[1] === undefined || Number(match[1]) === year ? [year, month, Number(match[3])] : null;
}

/** Only unambiguous period formats are comparable; returns [kind, tuple] or null. */
function comparablePeriod(period) {
  const value = period.trim();
  let match = /^(FY)?((?:19|20)\d{2})$/i.exec(value);
  if (match) return [match[1] ? "fiscal_year" : "calendar_year", [Number(match[2])]];
  match = /^((?:19|20)\d{2})[- ]?Q([1-4])$/i.exec(value);
  if (match) return ["calendar_quarter", [Number(match[1]), Number(match[2])]];
  match = /^((?:19|20)\d{2})-(0[1-9]|1[0-2])$/.exec(value);
  if (match) return ["calendar_month", [Number(match[1]), Number(match[2])]];
  const annual = parseAnnualPeriodEnd(value);
  if (annual) return ["year_ended_date", annual];
  match = /^((?:19|20)\d{2})-(\d{2})-(\d{2})$/.exec(value);
  if (match && validDay(Number(match[1]), Number(match[2]), Number(match[3]))) return ["date", match.slice(1).map(Number)];
  return null;
}

const GRANULARITY = { fiscal_year: "year", calendar_year: "year", year_ended_date: "year", calendar_quarter: "quarter", calendar_month: "month" };
const granularityOf = (period) => GRANULARITY[comparablePeriod(period)?.[0]] ?? null;
const tupleCompare = (x, y) => {
  for (let i = 0; i < Math.max(x.length, y.length); i += 1) if (x[i] !== y[i]) return x[i] < y[i] ? -1 : 1;
  return 0;
};

const TTM_QUARTER_RE = /^(?:FY\s*)?((?:19|20)\d{2})\s*[- ]?\s*Q([1-4])$/i;
const TTM_ZH_QUARTER_NUM = { "1": 1, "2": 2, "3": 3, "4": 4, "一": 1, "二": 2, "三": 3, "四": 4 };
const TTM_ZH_QUARTER_RE = /^((?:19|20)\d{2})\s*年?\s*第?\s*([1-4一二三四])\s*季(?:度)?$/;

function parseQuarterIndex(period) {
  const text = period.trim();
  const match = TTM_QUARTER_RE.exec(text);
  if (match) return { isFy: text.toUpperCase().startsWith("FY"), index: Number(match[1]) * 4 + (Number(match[2]) - 1) };
  const zh = TTM_ZH_QUARTER_RE.exec(text);
  if (zh) return { isFy: false, index: Number(zh[1]) * 4 + (TTM_ZH_QUARTER_NUM[zh[2]] - 1) };
  return null;
}

function plainDecimalStr({ n, s }) {
  if (n === 0n) return "0";
  const sign = n < 0n ? "-" : "";
  const abs = (n < 0n ? -n : n).toString();
  if (s === 0) return `${sign}${abs}`;
  const padded = abs.padStart(s + 1, "0");
  const whole = padded.slice(0, -s);
  const frac = padded.slice(-s).replace(/0+$/, "");
  return frac ? `${sign}${whole}.${frac}` : `${sign}${whole}`;
}

const EN_SCALE_TO_SPEC = {
  one: ["ones", 0], ones: ["ones", 0], "1": ["ones", 0], unit: ["ones", 0], units: ["ones", 0],
  thousand: ["thousands", 3], thousands: ["thousands", 3], k: ["thousands", 3],
  million: ["millions", 6], millions: ["millions", 6], m: ["millions", 6], mn: ["millions", 6], mm: ["millions", 6],
  billion: ["billions", 9], billions: ["billions", 9], b: ["billions", 9], bn: ["billions", 9],
  trillion: ["trillions", 12], trillions: ["trillions", 12], t: ["trillions", 12], tn: ["trillions", 12],
};
const ZH_SCALE_PATTERN = /(万亿元|万亿|萬億元|萬億|十亿元|十亿|十億元|十億|百万元|百万|百萬元|百萬|亿元|亿|億元|億|万元|万|萬元|萬|千元|千|元)/g;
const ZH_SCALE_TO_SPEC = {
  "元": ["元", 0, true], "千元": ["千元", 3, true], "千": ["thousands", 3, false],
  "万元": ["万元", 4, true], "萬元": ["万元", 4, true], "万": ["万元", 4, false], "萬": ["万元", 4, false],
  "百万元": ["百万元", 6, true], "百萬元": ["百万元", 6, true], "百万": ["millions", 6, false], "百萬": ["millions", 6, false],
  "亿元": ["亿元", 8, true], "億元": ["亿元", 8, true], "亿": ["亿元", 8, false], "億": ["亿元", 8, false],
  "十亿元": ["十亿元", 9, true], "十億元": ["十亿元", 9, true], "十亿": ["billions", 9, false], "十億": ["billions", 9, false],
  "万亿元": ["万亿元", 12, true], "萬億元": ["万亿元", 12, true], "万亿": ["trillions", 12, false], "萬億": ["trillions", 12, false],
};
const KNOWN_ISO_CURRENCIES = new Set([
  "USD", "EUR", "GBP", "JPY", "KRW", "CNY", "RMB", "HKD", "TWD",
  "CHF", "CAD", "AUD", "NZD", "SGD", "INR", "BRL", "MXN", "SEK",
  "NOK", "DKK", "ZAR", "SAR", "AED",
]);
const CURRENCY_PHRASES = {
  dollar: "USD", dollars: "USD", "us dollar": "USD", "us dollars": "USD", us$: "USD",
  euro: "EUR", euros: "EUR", won: "KRW", "korean won": "KRW", yen: "JPY", "japanese yen": "JPY",
  yuan: "CNY", renminbi: "CNY", pound: "GBP", pounds: "GBP", sterling: "GBP",
  "人民币": "CNY", "人民幣": "CNY", "美元": "USD", "美金": "USD",
  "港元": "HKD", "港币": "HKD", "港幣": "HKD", "欧元": "EUR", "歐元": "EUR",
  "日元": "JPY", "日圆": "JPY", "韩元": "KRW", "韓元": "KRW",
};
const ZH_CURRENCIES = ["人民币", "人民幣", "美元", "美金", "港元", "港币", "港幣", "欧元", "歐元", "日元", "日圆", "韩元", "韓元"];
const ZH_YUAN_UNITS = new Set(["元", "千元", "万元", "百万元", "亿元", "十亿元", "万亿元"]);

function parseUnitScale(unit, currency = null) {
  const norm = String(unit ?? "").trim().split(/\s+/).filter(Boolean).join(" ");
  if (!norm) throw new CalcError("Unit must not be empty.");
  const normCurr = typeof currency === "string" && currency.trim() ? currency.trim().toUpperCase() : null;
  if (isPercentUnit(norm)) {
    if (normCurr !== null) throw new CalcError("Percentage inputs cannot have a currency.");
    return { family: "percent", currency: null, scaleName: "ones", exponent: 0, canonicalUnit: "percent", isChineseYuan: false };
  }
  const lowered = norm.toLowerCase();
  if (/^(?:percentage\s+points?|百分点|百分點|pp)$/.test(lowered)) {
    if (normCurr !== null) throw new CalcError("Percentage-point inputs cannot have a currency.");
    return { family: "percentage_points", currency: null, scaleName: "ones", exponent: 0, canonicalUnit: "percentage points", isChineseYuan: false };
  }
  if (/^(?:basis\s+points?|bps|基点|基點)$/.test(lowered)) {
    if (normCurr !== null) throw new CalcError("Basis-point inputs cannot have a currency.");
    return { family: "bps", currency: null, scaleName: "ones", exponent: 0, canonicalUnit: "bps", isChineseYuan: false };
  }
  if (/(?:per\s+share|\/\s*shares?\b|每股)/.test(lowered)) {
    let foundCurr = null;
    for (const token of norm.toUpperCase().match(/[A-Za-z]{3}/g) ?? []) {
      if (KNOWN_ISO_CURRENCIES.has(token)) {
        foundCurr = token === "RMB" ? "CNY" : token;
        break;
      }
    }
    const normCurrCanon = normCurr === "RMB" ? "CNY" : normCurr;
    if (foundCurr && normCurrCanon && foundCurr !== normCurrCanon) {
      throw new CalcError("Input unit currency conflicts with declared currency.");
    }
    return { family: "price_per_share", currency: normCurr ?? foundCurr, scaleName: "ones", exponent: 0, canonicalUnit: norm, isChineseYuan: false };
  }

  let spaced = lowered.replaceAll(" of ", " ");
  for (const zhCurr of ZH_CURRENCIES) {
    if (spaced.includes(zhCurr)) spaced = spaced.replaceAll(zhCurr, ` ${zhCurr} `);
  }
  spaced = spaced.replace(ZH_SCALE_PATTERN, " $1 ");
  const tokens = spaced.split(/[\s,/()]+/).filter(Boolean);
  if (tokens.length === 0) throw new CalcError(`Invalid unit ${unit}.`);

  let unitCurrency = null;
  let scaleName = null;
  let exponent = 0;
  let isYuan = false;
  let isShares = false;
  const unmatched = [];

  for (let i = 0; i < tokens.length; i += 1) {
    const two = i + 1 < tokens.length ? `${tokens[i]} ${tokens[i + 1]}` : "";
    if (two && Object.hasOwn(CURRENCY_PHRASES, two)) {
      const mapped = CURRENCY_PHRASES[two];
      if (unitCurrency && unitCurrency !== mapped) throw new CalcError("Multiple currencies found in unit.");
      unitCurrency = mapped;
      i += 1;
      continue;
    }
    const tok = tokens[i];
    const upperTok = tok.toUpperCase();
    if (tok === "share" || tok === "shares" || tok === "股") {
      isShares = true;
    } else if (KNOWN_ISO_CURRENCIES.has(upperTok)) {
      const mapped = upperTok === "RMB" ? "CNY" : upperTok;
      if (unitCurrency && unitCurrency !== mapped) throw new CalcError("Multiple currencies found in unit.");
      unitCurrency = mapped;
    } else if (Object.hasOwn(CURRENCY_PHRASES, tok)) {
      const mapped = CURRENCY_PHRASES[tok];
      if (unitCurrency && unitCurrency !== mapped) throw new CalcError("Multiple currencies found in unit.");
      unitCurrency = mapped;
    } else if (Object.hasOwn(EN_SCALE_TO_SPEC, tok)) {
      const [sName, sExp] = EN_SCALE_TO_SPEC[tok];
      if (scaleName !== null && (scaleName !== sName || exponent !== sExp)) throw new CalcError("Multiple scales found in unit.");
      scaleName = sName;
      exponent = sExp;
    } else if (Object.hasOwn(ZH_SCALE_TO_SPEC, tok)) {
      const [sName, sExp, sYuan] = ZH_SCALE_TO_SPEC[tok];
      if (scaleName !== null && (scaleName !== sName || exponent !== sExp)) throw new CalcError("Multiple scales found in unit.");
      scaleName = sName;
      exponent = sExp;
      isYuan = isYuan || sYuan;
    } else {
      unmatched.push(tok);
    }
  }

  if (unmatched.length === 0 && isShares && unitCurrency === null) {
    const resolvedScale = scaleName ?? "ones";
    const canonicalUnit = resolvedScale === "ones" ? "shares" : `${resolvedScale} shares`;
    return { family: "shares", currency: null, scaleName: resolvedScale, exponent, canonicalUnit, isChineseYuan: false };
  }
  if (unmatched.length === 0 && !isShares && (unitCurrency !== null || scaleName !== null)) {
    const normCurrCanon = normCurr === "RMB" ? "CNY" : normCurr;
    if (unitCurrency !== null && normCurrCanon !== null && unitCurrency !== normCurrCanon) {
      throw new CalcError("Input unit currency conflicts with declared currency.");
    }
    const resolvedCurr = normCurr ?? unitCurrency;
    const resolvedScale = scaleName ?? (isYuan ? "元" : "ones");
    if (isYuan && ZH_YUAN_UNITS.has(resolvedScale)) {
      const canonicalUnit = resolvedCurr ? `${resolvedCurr} ${resolvedScale}` : resolvedScale;
      return { family: "monetary", currency: resolvedCurr, scaleName: resolvedScale, exponent, canonicalUnit, isChineseYuan: true };
    }
    if (resolvedCurr !== null) {
      const canonicalUnit = resolvedScale !== "ones" ? `${resolvedCurr} ${resolvedScale}` : resolvedCurr;
      return { family: "monetary", currency: resolvedCurr, scaleName: resolvedScale, exponent, canonicalUnit, isChineseYuan: false };
    }
    return { family: "monetary", currency: null, scaleName: resolvedScale, exponent, canonicalUnit: resolvedScale, isChineseYuan: false };
  }
  return { family: `custom:${lowered}`, currency: normCurr, scaleName: "ones", exponent: 0, canonicalUnit: norm, isChineseYuan: false };
}

function alignFactsScale(facts, parsedDecimals) {
  if (facts.length === 0) throw new CalcError("At least one input fact is required.");
  const rawCurrencies = new Set(facts.map((item) => item.currency ?? null));
  if (rawCurrencies.size !== 1) throw new CalcError("Input units and currencies must match.");
  const declaredCurrency = facts[0].currency ?? null;

  if (new Set(facts.map((item) => item.unit)).size === 1) {
    const spec0 = parseUnitScale(facts[0].unit, declaredCurrency);
    if (["percent", "percentage_points", "bps"].includes(spec0.family) && declaredCurrency !== null) {
      throw new CalcError("Percentage inputs cannot have a currency.");
    }
    return { alignedDecimals: parsedDecimals, targetUnit: facts[0].unit, targetCurrency: declaredCurrency, scaleAdjustments: [] };
  }

  const specs = facts.map((item) => parseUnitScale(item.unit, item.currency ?? null));
  if (new Set(specs.map((item) => item.family)).size !== 1) throw new CalcError("Input units and currencies must match.");
  const { family } = specs[0];
  if (new Set(specs.map((item) => item.currency)).size !== 1) throw new CalcError("Input units and currencies must match.");
  if (family !== "monetary" && family !== "shares") {
    if (new Set(specs.map((item) => item.canonicalUnit)).size !== 1) throw new CalcError("Input units and currencies must match.");
    return { alignedDecimals: parsedDecimals, targetUnit: specs[0].canonicalUnit, targetCurrency: declaredCurrency, scaleAdjustments: [] };
  }
  if (declaredCurrency === null && family === "monetary") {
    if (new Set(specs.map((item) => item.isChineseYuan)).size !== 1) throw new CalcError("Input units and currencies must match.");
  }

  const targetExponent = Math.min(...specs.map((item) => item.exponent));
  const preferredIdx = specs.findIndex((item) => item.exponent === targetExponent);
  const targetSpec = specs[preferredIdx];
  let targetUnit;
  if (targetSpec.isChineseYuan && ZH_YUAN_UNITS.has(facts[preferredIdx].unit.trim())) {
    targetUnit = facts[preferredIdx].unit.trim();
  } else if (targetSpec.family === "monetary" && declaredCurrency && facts[preferredIdx].unit.trim() === declaredCurrency) {
    targetUnit = declaredCurrency;
  } else {
    targetUnit = targetSpec.canonicalUnit;
  }

  const alignedDecimals = [];
  const scaleAdjustments = [];
  for (let i = 0; i < facts.length; i += 1) {
    const fact = facts[i];
    const spec = specs[i];
    const dec = parsedDecimals[i];
    const expDiff = spec.exponent - targetExponent;
    const alignedDec = expDiff > 0
      ? (dec.s >= expDiff ? { n: dec.n, s: dec.s - expDiff } : { n: dec.n * pow10(expDiff - dec.s), s: 0 })
      : dec;
    alignedDecimals.push(alignedDec);
    if (expDiff !== 0 || fact.unit !== targetUnit) {
      scaleAdjustments.push({
        fact_id: fact.fact_id,
        from_unit: fact.unit,
        to_unit: targetUnit,
        from_scale: spec.scaleName,
        to_scale: targetSpec.scaleName,
        multiplier: plainDecimalStr({ n: pow10(expDiff), s: 0 }),
        original_value: plainDecimalStr(dec),
        aligned_value: plainDecimalStr(alignedDec),
      });
    }
  }
  return { alignedDecimals, targetUnit, targetCurrency: declaredCurrency, scaleAdjustments };
}

function gcdBigInt(a, b) {
  let x = a < 0n ? -a : a;
  let y = b < 0n ? -b : b;
  while (y !== 0n) [x, y] = [y, x % y];
  return x;
}

function cagrPercent(endVal, baseVal, periodsDec, places) {
  if (endVal === 0n) return formatQuotient(-100n, 1n, places);
  const rawP = periodsDec.n;
  const rawQ = pow10(periodsDec.s);
  const g = gcdBigInt(rawP, rawQ);
  const p = rawP / g;
  const q = rawQ / g;
  if (p === 1n && q === 1n) return formatQuotient((endVal - baseVal) * 100n, baseVal, places);
  if (p <= 100n && q <= 50n) {
    const PREC = 40;
    const scale = pow10(PREC);
    const num = (endVal ** q) * (10n ** (BigInt(PREC) * p));
    const den = baseVal ** q;
    const target = num / den;
    const approx = Math.pow(Number(endVal) / Number(baseVal), Number(q) / Number(p));
    let y;
    if (Number.isFinite(approx) && approx > 0) {
      const exp = Math.floor(Math.log10(approx));
      const mantissa = Math.round(approx * (10 ** (15 - exp)));
      const shift = PREC - 15 + exp;
      y = shift >= 0 ? BigInt(mantissa) * pow10(shift) : BigInt(mantissa) / pow10(-shift);
      if (y <= 0n) y = 1n;
    } else {
      let lo = 1n;
      let hi = target > 1n ? target : 1n;
      while (lo < hi) {
        const mid = (lo + hi + 1n) >> 1n;
        if (mid ** p <= target) lo = mid;
        else hi = mid - 1n;
      }
      y = lo;
    }
    for (let i = 0; i < 12; i += 1) {
      const yPowMinus1 = y ** (p - 1n);
      if (yPowMinus1 === 0n) break;
      const next = ((p - 1n) * y + target / yPowMinus1) / p;
      if (next === y) break;
      y = next;
    }
    while ((y + 1n) ** p <= target) y += 1n;
    while (y ** p > target && y > 0n) y -= 1n;
    return formatQuotient((y - scale) * 100n, scale, places);
  }
  const approxPct = (Math.pow(Number(endVal) / Number(baseVal), Number(q) / Number(p)) - 1) * 100;
  return formatQuotient(BigInt(Math.round(approxPct * (10 ** 12))), pow10(12), places);
}

/**
 * Deterministic calculation engine mirroring research_calculations.calculate.
 * For binary formulas, `inputs[0]` is current/numerator/minuend and `inputs[1]` is baseline/denominator/subtrahend.
 */
function calculateEngine(formula, inputs, places, { periods = null, weights = null } = {}) {
  if (!FORMULAS.includes(formula)) throw new CalcError("Unsupported formula.");
  if (!Array.isArray(inputs) || inputs.length < 2 || inputs.length > 50) throw new CalcError("Invalid calculation inputs.");
  if (BINARY_FORMULAS.includes(formula) && inputs.length !== 2) throw new CalcError(`Formula '${formula}' requires exactly 2 facts.`);
  if (formula === "ttm_sum" && inputs.length !== 4) throw new CalcError("Formula 'ttm_sum' requires exactly 4 quarterly facts.");
  if (new Set(inputs.map((item) => item.fact_id)).size !== inputs.length) throw new CalcError("Inputs must identify distinct facts.");

  let parsedPeriods = null;
  if (formula === "cagr") {
    if (periods === null || periods === undefined) throw new CalcError("periods > 0 is required for formula 'cagr'.");
    parsedPeriods = parseDecimal(String(periods));
    if (parsedPeriods.n <= 0n || parsedPeriods.n > 1000n * pow10(parsedPeriods.s)) {
      throw new CalcError("periods must be a finite positive number <= 1000.");
    }
  } else if (periods !== null && periods !== undefined) {
    throw new CalcError("periods is only supported for formula 'cagr'.");
  }

  let parsedWeights = null;
  if (formula === "weighted_average") {
    if (!Array.isArray(weights) || weights.length !== inputs.length) {
      throw new CalcError("weights length must match the number of input facts.");
    }
    parsedWeights = weights.map((item) => {
      const dec = parseDecimal(String(item));
      if (dec.n < 0n) throw new CalcError("weights must be finite non-negative numbers within supported precision.");
      return dec;
    });
    if (parsedWeights.every((item) => item.n === 0n)) throw new CalcError("weights sum must be positive.");
  } else if (weights !== null && weights !== undefined) {
    throw new CalcError("weights is only supported for formula 'weighted_average'.");
  }

  const parsedDecimals = inputs.map((item) => parseDecimal(item.value));
  const { alignedDecimals, targetUnit, targetCurrency, scaleAdjustments } = alignFactsScale(inputs, parsedDecimals);
  const maxS = Math.max(...alignedDecimals.map((item) => item.s));
  const alignedVals = alignedDecimals.map((item) => item.n * pow10(maxS - item.s));

  let periodOrderCheck = "not_checked";
  if (formula === "margin_percent" && inputs[0].period !== inputs[1].period) {
    throw new CalcError("Margin inputs must have the same period.");
  }
  if (formula === "growth_percent" || formula === "cagr") {
    const label = formula === "cagr" ? "CAGR" : "Growth";
    if (inputs[0].period === inputs[1].period) throw new CalcError(`${label} inputs must refer to different periods.`);
    const current = comparablePeriod(inputs[0].period);
    const baseline = comparablePeriod(inputs[1].period);
    if (current && baseline && current[0] === baseline[0]) {
      if (tupleCompare(current[1], baseline[1]) <= 0) throw new CalcError(`${label} current period must be later than its baseline.`);
      periodOrderCheck = "recognized_formats_only";
    }
  }
  if (formula === "growth_percent" && alignedVals[1] <= 0n) throw new CalcError("Growth requires a positive baseline.");
  if (formula === "margin_percent" && alignedVals[1] <= 0n) throw new CalcError("Margin requires a positive denominator.");
  if (formula === "cagr") {
    if (alignedVals[1] <= 0n) throw new CalcError("CAGR requires a positive baseline.");
    if (alignedVals[0] < 0n) throw new CalcError("CAGR requires a non-negative ending value.");
  }
  if (formula === "ratio" && alignedVals[1] <= 0n) throw new CalcError("Ratio requires a positive denominator.");
  if (formula === "ttm_sum") {
    const rawPeriods = inputs.map((item) => item.period);
    if (new Set(rawPeriods).size !== 4) throw new CalcError("ttm_sum inputs must refer to distinct periods.");
    for (const period of rawPeriods) {
      const gran = granularityOf(period);
      if (gran === "year" || gran === "month") throw new CalcError("ttm_sum requires quarterly periods.");
    }
    const parsedQuarters = rawPeriods.map(parseQuarterIndex);
    if (parsedQuarters.every((item) => item !== null)) {
      const fyFlags = new Set(parsedQuarters.map((item) => item.isFy));
      if (fyFlags.size !== 1) throw new CalcError("ttm_sum cannot mix fiscal and calendar quarter labels.");
      const indices = parsedQuarters.map((item) => item.index).sort((x, y) => x - y);
      if (indices.some((idx, i) => idx !== indices[0] + i)) {
        throw new CalcError("ttm_sum requires 4 consecutive quarterly periods.");
      }
      periodOrderCheck = "recognized_formats_only";
    }
  }

  let value;
  let expression;
  let unit = "percent";
  let formulaVersion = "1";
  const isPctInput = isPercentUnit(inputs[0].unit);

  if (formula === "growth_percent") {
    value = formatQuotient((alignedVals[0] - alignedVals[1]) * 100n, alignedVals[1], places);
    expression = "(first / second - 1) * 100";
  } else if (formula === "margin_percent") {
    value = formatQuotient(alignedVals[0] * 100n, alignedVals[1], places);
    expression = "first / second * 100";
  } else if (formula === "difference") {
    value = formatQuotient(alignedVals[0] - alignedVals[1], pow10(maxS), places);
    expression = "first - second";
    if (isPctInput) {
      if ((inputs[0].currency ?? null) !== null) throw new CalcError("Percentage inputs cannot have a currency.");
      unit = "percentage points";
      formulaVersion = "2";
    } else {
      unit = targetUnit;
    }
  } else if (formula === "cagr") {
    value = cagrPercent(alignedVals[0], alignedVals[1], parsedPeriods, places);
    expression = "((first / second) ** (1 / periods) - 1) * 100";
  } else if (formula === "ratio") {
    value = formatQuotient(alignedVals[0], alignedVals[1], places);
    expression = "first / second";
    unit = "ratio";
  } else if (formula === "sum") {
    const total = alignedVals.reduce((acc, item) => acc + item, 0n);
    value = formatQuotient(total, pow10(maxS), places);
    expression = "sum(inputs)";
    unit = isPctInput ? "percent" : targetUnit;
  } else if (formula === "subtract") {
    const tailSum = alignedVals.slice(1).reduce((acc, item) => acc + item, 0n);
    value = formatQuotient(alignedVals[0] - tailSum, pow10(maxS), places);
    expression = "inputs[0] - sum(inputs[1:])";
    unit = isPctInput ? "percentage points" : targetUnit;
  } else if (formula === "weighted_average") {
    const wMaxS = Math.max(...parsedWeights.map((item) => item.s));
    const wAligned = parsedWeights.map((item) => item.n * pow10(wMaxS - item.s));
    const weightSum = wAligned.reduce((acc, item) => acc + item, 0n);
    if (weightSum <= 0n) throw new CalcError("Weighted average requires positive total weight.");
    const weightedTotal = wAligned.reduce((acc, weight, idx) => acc + weight * alignedVals[idx], 0n);
    value = formatQuotient(weightedTotal, weightSum * pow10(maxS), places);
    expression = "sum(w_i * x_i) / sum(w_i)";
    unit = isPctInput ? "percent" : targetUnit;
  } else {
    if (isPctInput) throw new CalcError("ttm_sum cannot sum percentage rates.");
    const total = alignedVals.reduce((acc, item) => acc + item, 0n);
    value = formatQuotient(total, pow10(maxS), places);
    expression = "sum(ttm_quarters)";
    unit = targetUnit;
  }

  const outCurrency = unit === "percent" || unit === "percentage points" || unit === "ratio" ? null : targetCurrency;
  const output = {
    formula_id: formula,
    formula_version: formulaVersion,
    expression,
    value,
    unit,
    currency: outCurrency,
    decimal_places: places,
    rounding: "half_up",
    inputs: inputs.map((item) => ({ fact_id: item.fact_id, value: item.value, unit: item.unit, currency: item.currency ?? null, period: item.period })),
    input_verification: "not_verified_by_calculator",
    input_precision_status: "not_established",
    period_order_check: formula === "growth_percent" || formula === "cagr" || formula === "ttm_sum" ? periodOrderCheck : null,
    period_granularity_check: formula === "growth_percent" || formula === "cagr"
      ? (() => {
          const g1 = granularityOf(inputs[0].period);
          const g2 = granularityOf(inputs[1].period);
          return g1 !== null && g2 !== null && g1 !== g2 ? "different_labels_verify_stock_or_flow" : "not_established";
        })()
      : null,
  };
  if (formula === "cagr") output.periods = plainDecimalStr(parsedPeriods);
  if (formula === "weighted_average") output.weights = parsedWeights.map(plainDecimalStr);
  if (scaleAdjustments.length > 0) output.scale_adjustments = scaleAdjustments;
  return output;
}

/** Mirrors calculation_result_status: recompute and compare stored output. */
function calcResultStatus(calc) {
  try {
    if (!FORMULAS.includes(calc.formula_id) || !Array.isArray(calc.inputs) || calc.inputs.length < 2 || !Number.isInteger(calc.decimal_places)) {
      return "unverifiable";
    }
    if (BINARY_FORMULAS.includes(calc.formula_id) && calc.inputs.length !== 2) return "unverifiable";
    if (calc.formula_id === "ttm_sum" && calc.inputs.length !== 4) return "unverifiable";
    if (calc.formula_id === "cagr" && (calc.periods === null || calc.periods === undefined)) return "unverifiable";
    if (calc.formula_id === "weighted_average" && !Array.isArray(calc.weights)) return "unverifiable";
    const expected = calculateEngine(calc.formula_id, calc.inputs, calc.decimal_places, {
      periods: calc.formula_id === "cagr" ? calc.periods : null,
      weights: calc.formula_id === "weighted_average" ? calc.weights : null,
    });
    if (!decimalEquals(String(calc.value), expected.value)) return "mismatch";
    if (["formula_version", "expression", "unit", "currency", "rounding"].some((key) => (calc[key] ?? null) !== expected[key])) {
      return "mismatch";
    }
    for (const key of ["periods", "weights", "scale_adjustments"]) {
      if (!same(calc[key] ?? null, expected[key] ?? null)) return "mismatch";
    }
    return "matched";
  } catch {
    return "unverifiable";
  }
}

const legacyPercentageDifference = (calc) =>
  calc.formula_id === "difference" && Array.isArray(calc.inputs) && calc.inputs.length === 2 &&
  calc.inputs.every((item) => isRecord(item) && typeof item.unit === "string" && isPercentUnit(item.unit)) && calc.unit !== "percentage points";

/** Mirrors calculation_input_status for the checks the mock models. */
function calcInputStatus(calc, facts) {
  if (legacyPercentageDifference(calc)) return "unverifiable";
  try {
    if (!FORMULAS.includes(calc.formula_id) || !Array.isArray(calc.inputs) || calc.inputs.length < 2 || calc.inputs.length > 50) return "unverifiable";
    if (BINARY_FORMULAS.includes(calc.formula_id) && calc.inputs.length !== 2) return "unverifiable";
    if (calc.formula_id === "ttm_sum" && calc.inputs.length !== 4) return "unverifiable";
    if (new Set(calc.inputs.map((item) => item.fact_id)).size !== calc.inputs.length) return "unverifiable";
    let unverifiable = false;
    for (const input of calc.inputs) {
      const matches = facts.filter((fact) => fact.fact_id === input.fact_id);
      if (matches.length !== 1) return "missing_or_ambiguous";
      const [fact] = matches;
      if (input.unit !== fact.unit || input.period !== fact.period || (input.currency ?? null) !== (fact.currency ?? null) || !decimalEquals(input.value, fact.value)) {
        return "changed";
      }
      if (fact.verification === "user_edited_value_not_in_quotation") unverifiable = true;
    }
    return unverifiable ? "unverifiable" : "matched";
  } catch {
    return "unverifiable";
  }
}

// ---- Version content validation (mirrors the research_topics models) ------------------------

const CONTENT_KEYS = ["summary", "sources", "source_candidates", "facts", "calculations", "charts", "claims", "gaps", "turns"];
const FACT_COMPUTED_FIELDS = ["currency_quote_missing", "scale_quote_missing", "currency_unspecified"];
const CALCULATION_COMPUTED_FIELDS = ["input_status", "legacy_unit_warning", "result_status"];
const FACT_KEYS = ["fact_id", "turn_id", "label", "value", "unit", "currency", "period", "source_id", "quotation", "character_start", "character_end", "location", "verification", ...FACT_COMPUTED_FIELDS];
const CLAIM_KEYS = ["claim_id", "turn_id", "role", "text", "citations", "challenges_claim_ids", "edited_by_user"];
const CALCULATION_KEYS = ["calculation_id", "turn_id", "formula_id", "formula_version", "expression", "input_verification", "input_precision_status", "period_order_check", "period_granularity_check", "inputs", "value", "unit", "currency", "rounding", "decimal_places", "periods", "weights", "scale_adjustments", ...CALCULATION_COMPUTED_FIELDS, "authorship", "verification"];
const SCALE_ADJUSTMENT_KEYS = ["fact_id", "from_unit", "to_unit", "from_scale", "to_scale", "multiplier", "original_value", "aligned_value"];
const BINARY_FORMULAS = ["growth_percent", "margin_percent", "difference", "cagr", "ratio"];
const MULTI_FACT_FORMULAS = ["sum", "subtract", "weighted_average", "ttm_sum"];
const FORMULAS = [...BINARY_FORMULAS, ...MULTI_FACT_FORMULAS];
const CREATE_CALCULATION_FORMULAS = ["growth_percent", "margin_percent", "difference"];

function exactKeys(value, allowed, location) {
  if (!isRecord(value)) throw invalid("Input should be a valid dictionary or object.", location);
  for (const key of Object.keys(value)) if (!allowed.includes(key)) throw invalid("Extra inputs are not permitted.", `${location}/${key}`);
  return value;
}

function readString(value, location, { min = 0, max = Infinity, nullable = false } = {}) {
  if (value === null && nullable) return null;
  if (typeof value !== "string" || value.length < min || value.length > max) {
    throw invalid(`Input should be a string${min ? " of at least 1 character" : ""}${max < Infinity ? ` and at most ${max} characters` : ""}.`, location);
  }
  return value;
}

function readInt(value, location, { min = 0, max = Infinity } = {}) {
  if (!Number.isInteger(value) || value < min || value > max) throw invalid("Input should be a valid integer in range.", location);
  return value;
}

function readLocation(value, location) {
  exactKeys(value, ["character_start", "character_end"], location);
  const start = readInt(value.character_start, `${location}/character_start`);
  const end = readInt(value.character_end, `${location}/character_end`);
  if (end <= start) throw invalid("character_end must be greater than character_start.", location);
  return { character_start: start, character_end: end };
}

function readClaim(raw, index) {
  const where = `body/content/claims/${index}`;
  exactKeys(raw, CLAIM_KEYS, where);
  if (!["support", "counterargument"].includes(raw.role)) throw invalid("Input should be 'support' or 'counterargument'.", `${where}/role`);
  const citations = raw.citations ?? [];
  if (!Array.isArray(citations) || citations.length > 10) throw invalid("Input should be a list of at most 10 citations.", `${where}/citations`);
  const challenges = raw.challenges_claim_ids ?? [];
  if (!isStringArray(challenges)) throw invalid("Input should be a list of strings.", `${where}/challenges_claim_ids`);
  if (raw.edited_by_user !== undefined && typeof raw.edited_by_user !== "boolean") throw invalid("Input should be a valid boolean.", `${where}/edited_by_user`);
  return {
    claim_id: raw.claim_id === undefined ? `claim-${randomBytes(16).toString("hex")}` : readString(raw.claim_id, `${where}/claim_id`, { min: 1, max: 100 }),
    turn_id: raw.turn_id === undefined ? null : readString(raw.turn_id, `${where}/turn_id`, { nullable: true }),
    role: raw.role,
    text: readString(raw.text, `${where}/text`, { min: 1, max: 3000 }),
    citations: citations.map((cite, i) => {
      exactKeys(cite, ["source_id", "quotation", "location"], `${where}/citations/${i}`);
      return {
        source_id: readString(cite.source_id, `${where}/citations/${i}/source_id`, { min: 1, max: 100 }),
        quotation: readString(cite.quotation, `${where}/citations/${i}/quotation`, { max: 1000 }),
        location: readLocation(cite.location, `${where}/citations/${i}/location`),
      };
    }),
    challenges_claim_ids: [...challenges],
    edited_by_user: raw.edited_by_user ?? false,
  };
}

/** Defaulted view of a stored claim, comparable with readClaim output. */
const claimIdentity = (claim) => ({
  claim_id: claim.claim_id,
  turn_id: claim.turn_id ?? null,
  role: claim.role,
  text: claim.text,
  citations: (claim.citations ?? []).map((cite) => ({ source_id: cite.source_id, quotation: cite.quotation, location: { character_start: cite.location?.character_start, character_end: cite.location?.character_end } })),
  challenges_claim_ids: claim.challenges_claim_ids ?? [],
  edited_by_user: claim.edited_by_user ?? false,
});

function readFact(raw, index) {
  const where = `body/content/facts/${index}`;
  exactKeys(raw, FACT_KEYS, where);
  for (const flag of FACT_COMPUTED_FIELDS) {
    if (raw[flag] !== undefined && raw[flag] !== null && typeof raw[flag] !== "boolean") throw invalid("Input should be a valid boolean.", `${where}/${flag}`);
  }
  return {
    fact_id: readString(raw.fact_id, `${where}/fact_id`, { min: 1, max: 200 }),
    turn_id: raw.turn_id === undefined ? null : readString(raw.turn_id, `${where}/turn_id`, { nullable: true }),
    label: readString(raw.label, `${where}/label`, { min: 1 }),
    value: readString(raw.value, `${where}/value`, { min: 1 }),
    unit: readString(raw.unit, `${where}/unit`, { min: 1 }),
    currency: raw.currency === undefined ? null : readString(raw.currency, `${where}/currency`, { nullable: true }),
    period: readString(raw.period, `${where}/period`, { min: 1 }),
    source_id: readString(raw.source_id, `${where}/source_id`, { min: 1 }),
    quotation: readString(raw.quotation, `${where}/quotation`),
    character_start: readInt(raw.character_start, `${where}/character_start`),
    character_end: readInt(raw.character_end, `${where}/character_end`, { min: 1 }),
    location: readLocation(raw.location, `${where}/location`),
    verification: readString(raw.verification, `${where}/verification`),
  };
}

const factIdentity = (fact) => ({
  fact_id: fact.fact_id,
  turn_id: fact.turn_id ?? null,
  label: fact.label,
  value: fact.value,
  unit: fact.unit,
  currency: fact.currency ?? null,
  period: fact.period,
  source_id: fact.source_id,
  quotation: fact.quotation,
  character_start: fact.character_start,
  character_end: fact.character_end,
  location: fact.location,
  verification: fact.verification,
});

function readCalculation(raw, index) {
  const where = `body/content/calculations/${index}`;
  exactKeys(raw, CALCULATION_KEYS, where);
  if (!FORMULAS.includes(raw.formula_id)) {
    throw invalid(`Input should be ${FORMULAS.map((item) => `'${item}'`).join(", ")}.`, `${where}/formula_id`);
  }
  const binary = BINARY_FORMULAS.includes(raw.formula_id);
  if (!Array.isArray(raw.inputs) || (binary ? raw.inputs.length !== 2 : raw.inputs.length < 2 || raw.inputs.length > 50)) {
    throw invalid(binary ? "List should have exactly 2 items." : "List should have between 2 and 50 items.", `${where}/inputs`);
  }
  const optional = (key) => (raw[key] === undefined ? null : readString(raw[key], `${where}/${key}`, { nullable: true }));
  let weights = null;
  if (raw.weights !== undefined && raw.weights !== null) {
    if (!Array.isArray(raw.weights)) throw invalid("Input should be a valid list.", `${where}/weights`);
    weights = raw.weights.map((item, i) => readString(item, `${where}/weights/${i}`));
  }
  let scaleAdjustments = null;
  if (raw.scale_adjustments !== undefined && raw.scale_adjustments !== null) {
    if (!Array.isArray(raw.scale_adjustments)) throw invalid("Input should be a valid list.", `${where}/scale_adjustments`);
    scaleAdjustments = raw.scale_adjustments.map((item, i) => {
      exactKeys(item, SCALE_ADJUSTMENT_KEYS, `${where}/scale_adjustments/${i}`);
      return Object.fromEntries(SCALE_ADJUSTMENT_KEYS.map((key) => [key, readString(item[key], `${where}/scale_adjustments/${i}/${key}`)]));
    });
  }
  const calc = {
    calculation_id: readString(raw.calculation_id, `${where}/calculation_id`, { min: 1, max: 200 }),
    turn_id: optional("turn_id"),
    formula_id: raw.formula_id,
    formula_version: readString(raw.formula_version, `${where}/formula_version`),
    expression: readString(raw.expression, `${where}/expression`),
    input_verification: readString(raw.input_verification, `${where}/input_verification`),
    input_precision_status: readString(raw.input_precision_status, `${where}/input_precision_status`),
    period_order_check: optional("period_order_check"),
    period_granularity_check: optional("period_granularity_check"),
    inputs: raw.inputs.map((item, i) => {
      exactKeys(item, ["fact_id", "value", "unit", "currency", "period"], `${where}/inputs/${i}`);
      return {
        fact_id: readString(item.fact_id, `${where}/inputs/${i}/fact_id`),
        value: readString(item.value, `${where}/inputs/${i}/value`),
        unit: readString(item.unit, `${where}/inputs/${i}/unit`),
        currency: item.currency === undefined ? null : readString(item.currency, `${where}/inputs/${i}/currency`, { nullable: true }),
        period: readString(item.period, `${where}/inputs/${i}/period`),
      };
    }),
    value: readString(raw.value, `${where}/value`),
    unit: readString(raw.unit, `${where}/unit`),
    currency: optional("currency"),
    rounding: readString(raw.rounding, `${where}/rounding`),
    decimal_places: readInt(raw.decimal_places, `${where}/decimal_places`, { max: 8 }),
    periods: optional("periods"),
    weights,
    scale_adjustments: scaleAdjustments,
    result_status: optional("result_status"),
    input_status: optional("input_status"),
    legacy_unit_warning: optional("legacy_unit_warning"),
    authorship: optional("authorship"),
    verification: optional("verification"),
  };
  return calc;
}

const calculationIdentity = (calc) => {
  const { input_status: _a, legacy_unit_warning: _b, result_status: _c, ...rest } = calc;
  return {
    ...rest,
    turn_id: calc.turn_id ?? null,
    period_order_check: calc.period_order_check ?? null,
    period_granularity_check: calc.period_granularity_check ?? null,
    currency: calc.currency ?? null,
    periods: calc.periods ?? null,
    weights: calc.weights ?? null,
    scale_adjustments: calc.scale_adjustments ?? null,
    authorship: calc.authorship ?? null,
    verification: calc.verification ?? null,
    inputs: (calc.inputs ?? []).map((item) => ({ ...item, currency: item.currency ?? null })),
  };
};

const omitKeys = (value, keys) => Object.fromEntries(Object.entries(value).filter(([key]) => !keys.includes(key)));
const same = (x, y) => stableStringify(x) === stableStringify(y);

function validateVersionInput(body) {
  exactKeys(body, ["parent_version_id", "content", "review", "note"], "body");
  readString(body.parent_version_id, "body/parent_version_id");
  if (!isRecord(body.content)) throw invalid("Input should be a valid dictionary or object.", "body/content");
  exactKeys(body.content, CONTENT_KEYS, "body/content");
  if (body.review !== undefined && !["draft", "confirmed", "reservation"].includes(body.review)) {
    throw invalid("Input should be 'draft', 'confirmed' or 'reservation'.", "body/review");
  }
  if (body.note !== undefined) readString(body.note, "body/note", { max: 10000 });
  const { content } = body;
  if (content.summary !== undefined) readString(content.summary, "body/content/summary", { max: 100000 });
  for (const key of ["sources", "source_candidates", "facts", "calculations", "charts", "claims", "gaps", "turns"]) {
    if (content[key] !== undefined && !Array.isArray(content[key])) throw invalid("Input should be a valid list.", `body/content/${key}`);
  }
  if ((content.source_candidates ?? []).length > 10) throw invalid("List should have at most 10 items.", "body/content/source_candidates");
}

function validateCalculationRequest(body, { recalculate }) {
  exactKeys(body, recalculate ? ["parent_version_id", "calculation_index"] : ["parent_version_id", "formula", "first_fact_id", "second_fact_id", "decimal_places"], "body");
  const parent = readString(body.parent_version_id, "body/parent_version_id", { min: 1 }).trim();
  if (parent.length < 1 || parent.length > 100) throw invalid("String should have at least 1 and at most 100 characters.", "body/parent_version_id");
  if (recalculate) return { parent, index: readInt(body.calculation_index, "body/calculation_index", { max: 1999 }) };
  if (!CREATE_CALCULATION_FORMULAS.includes(body.formula)) throw invalid("Input should be 'growth_percent', 'margin_percent' or 'difference'.", "body/formula");
  const ids = ["first_fact_id", "second_fact_id"].map((key) => {
    const id = readString(body[key], `body/${key}`, { min: 1 }).trim();
    if (id.length < 1 || id.length > 200) throw invalid("String should have at least 1 and at most 200 characters.", `body/${key}`);
    return id;
  });
  return { parent, formula: body.formula, first: ids[0], second: ids[1], places: body.decimal_places === undefined ? 2 : readInt(body.decimal_places, "body/decimal_places", { max: 8 }) };
}

// ---- Source validation and construction ----------------------------------------------

function strictKeys(body, allowed) {
  if (!isRecord(body)) throw invalid("Body must be a JSON object.");
  for (const key of Object.keys(body)) if (!allowed.includes(key)) throw invalid("Extra inputs are not permitted.", `body/${key}`);
}

const PERMISSION_KEYS = ["store", "model_input", "display", "export"];

function readPermissions(value) {
  if (value === undefined) throw invalid("Field required.", "body/permissions");
  if (!isRecord(value)) throw invalid("Input should be a valid dictionary or object.", "body/permissions");
  for (const key of Object.keys(value)) if (!PERMISSION_KEYS.includes(key)) throw invalid("Extra inputs are not permitted.", `body/permissions/${key}`);
  const result = {};
  for (const key of PERMISSION_KEYS) {
    if (value[key] !== undefined && typeof value[key] !== "boolean") throw invalid("Input should be a valid boolean.", `body/permissions/${key}`);
    result[key] = value[key] ?? false;
  }
  return result;
}

function readTitle(body) {
  if (body.title === undefined) throw invalid("Field required.", "body/title");
  if (typeof body.title !== "string" || body.title.length > 500) throw invalid("Input should be a string of at most 500 characters.", "body/title");
  const title = body.title.trim();
  if (!title) throw invalid("Title must not be blank.", "body/title");
  return title;
}

function readText(body) {
  if (body.text === undefined) throw invalid("Field required.", "body/text");
  if (typeof body.text !== "string" || body.text.length < 1 || body.text.length > 2_000_000) {
    throw invalid("String should have at least 1 and at most 2000000 characters.", "body/text");
  }
  if (!body.text.trim()) throw invalid("Source text must not be blank.", "body/text");
  return body.text;
}

function readBool(body, key, fallback = false) {
  if (body[key] === undefined) return fallback;
  if (typeof body[key] !== "boolean") throw invalid("Input should be a valid boolean.", `body/${key}`);
  return body[key];
}

const TIMESTAMP = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?)(Z|[+-]\d{2}(?::?\d{2})?)$/i;

function readTimestamp(body, key) {
  const value = body[key];
  if (value === undefined || value === null) return null;
  const match = typeof value === "string" ? TIMESTAMP.exec(value.trim()) : null;
  if (!match) throw invalid("Input should be a valid datetime with a timezone offset.", `body/${key}`);
  let [, day, clock, zone] = match;
  if (/^[+-]\d{2}$/.test(zone)) zone += ":00";
  else if (/^[+-]\d{4}$/.test(zone)) zone = `${zone.slice(0, 3)}:${zone.slice(3)}`;
  const parsed = Date.parse(`${day}T${clock}${zone.toUpperCase()}`);
  if (Number.isNaN(parsed)) throw invalid("Input should be a valid datetime with a timezone offset.", `body/${key}`);
  return iso(new Date(parsed));
}

const readDates = (body) => ({
  published_at: readTimestamp(body, "published_at"),
  event_at: readTimestamp(body, "event_at"),
  revised_at: readTimestamp(body, "revised_at"),
});

function parseUrl(value, location = "body/url") {
  if (value === undefined) throw invalid("Field required.", location);
  let url = null;
  try {
    url = typeof value === "string" && value.length <= 2083 ? new URL(value.trim()) : null;
  } catch {
    url = null;
  }
  if (!url || (url.protocol !== "http:" && url.protocol !== "https:") || !url.hostname) throw invalid("Input should be a valid URL.", location);
  return url;
}

function readOptionalUrl(body) {
  if (body.url === undefined || body.url === null) return null;
  const url = parseUrl(body.url);
  if (url.username || url.password) throw invalid("Source URLs cannot include credentials.");
  return url.href;
}

/** Strict base64 (no whitespace, correct padding); returns null when invalid. */
function decodeBase64(value, location) {
  if (value === undefined) throw invalid("Field required.", location);
  if (typeof value !== "string" || value.length < 1 || value.length > 4 * Math.ceil((MAX_ORIGINAL_BYTES + 2) / 3)) {
    throw invalid("String should have at least 1 and at most 6666668 characters.", location);
  }
  if (value.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(value)) return null;
  return Buffer.from(value, "base64");
}

const newSourceId = () => `source_${randomBytes(16).toString("hex")}`;

function storeSource(data, accountId, fields) {
  const now = new Date();
  const source = makeSource(now, {
    source_id: fields.source_id ?? newSourceId(),
    owner_id: accountId,
    title: fields.title,
    text: fields.text,
    url: fields.url ?? null,
    kind: fields.kind ?? "user_supplied",
    is_simulated: Boolean(fields.is_simulated),
    published_at: fields.published_at ?? null,
    event_at: fields.event_at ?? null,
    revised_at: fields.revised_at ?? null,
    obtained_at: iso(now),
    permissions: fields.permissions,
    retrieval_method: fields.retrieval_method ?? "user_supplied",
    pdf_text_order: fields.pdf_text_order ?? null,
    derived_from_source_id: fields.derived_from_source_id ?? null,
    response_sha256: fields.response_sha256 ?? null,
    response_content_type: fields.response_content_type ?? null,
  });
  if (fields.original_bytes) source.original_bytes = fields.original_bytes;
  if (fields.pdf_text_spacing_warning) source.pdf_text_spacing_warning = true;
  data.sources.set(source.source_id, source);
  return source;
}

const bytesSha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");

const HTML_ENTITIES = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&nbsp;": " " };

/** Dependency-free tag stripper for fictional QA uploads; not a real HTML parser. */
function extractHtmlText(html) {
  const stripped = html
    .replace(/<!--[\s\S]*?-->/g, " ")
    .replace(/<(script|style|ix:header)\b[\s\S]*?<\/\1\s*>/gi, " ")
    .replace(/<\/?(?:p|div|br|li|tr|h[1-6]|table|section|article)\b[^>]*>/gi, "\n")
    .replace(/<[^>]*>/g, " ")
    .replace(/&(?:amp|lt|gt|quot|nbsp|#39);/g, (entity) => HTML_ENTITIES[entity]);
  return stripped
    .split(/\r?\n/)
    .map((line) => line.replace(/\s+/g, " ").trim())
    .filter(Boolean)
    .join("\n");
}

function secConceptRecords(cik, tag, unit, filingBefore, maxRecords) {
  const seed = Number.parseInt(sha256(`${cik}:${tag}`).slice(0, 4), 16) % 500;
  const records = [];
  for (let i = 0; i < 12; i += 1) {
    const year = 2023 + Math.floor(i / 4);
    const quarter = i % 4;
    const end = new Date(Date.UTC(year, quarter * 3 + 3, 0));
    const filed = new Date(end.getTime() + 45 * 24 * 60 * 60 * 1000);
    records.push({
      period_end: end.toISOString().slice(0, 10),
      filed: filed.toISOString().slice(0, 10),
      form: quarter === 3 ? "10-K" : "10-Q",
      value: (1000 + i * 137 + seed) * 1_000_000,
    });
  }
  return records.filter((record) => !filingBefore || record.filed < filingBefore).slice(0, maxRecords).map(
    (record) => `period ${record.period_end} | filed ${record.filed} | form ${record.form} | ${unit} ${record.value.toLocaleString("en-US")}`,
  );
}

function resolveDiscovery(body) {
  if ("discover_sources" in body) return body.discover_sources === true;
  const selected = body.selected_source_ids;
  const searches = body.max_search_calls;
  return searches !== 0 && !(Array.isArray(selected) && selected.length === 0 && !(typeof searches === "number" && searches > 0));
}

// ---- Run simulation -------------------------------------------------------------

function ensureContent(content) {
  const next = structuredClone(content ?? {});
  for (const key of ["calculations", "charts", "claims", "facts", "gaps", "source_candidates", "sources", "turns"]) {
    if (!Array.isArray(next[key])) next[key] = [];
  }
  return next;
}

const nextNumber = (items, key, prefix) =>
  items.reduce((max, item) => {
    const match = new RegExp(`^${prefix}(\\d+)$`).exec(item[key] ?? "");
    return match ? Math.max(max, Number(match[1])) : max;
  }, 0) + 1;

const randomId = (prefix) => `${prefix}_${randomBytes(6).toString("hex")}`;

function effectiveQuestion(topic, run) {
  return run.question_override || topic.request.question || "";
}

function pickVariant(question) {
  if (question.includes("部分")) return "partial";
  if (question.includes("失败")) return "fail";
  if (question.includes("驳回")) return "rejected";
  return "normal";
}

function touch(run, now, status) {
  if (status) run.operation.status = status;
  run.operation.updated_at = iso(now);
  run.operation.last_event_id = `${run.operation.operation_id}:${run.events.length}`;
}

/** Adds the scripted fact (and, when completed, claim and calculation) for the Northwind source. */
function addScriptedFindings(content, source, turnId, completed) {
  const text = source.text;
  let fact = content.facts.find((item) => item.turn_id === turnId);
  const makeFact = (quotation, label, value, period) => {
    const location = span(text, quotation);
    return {
      fact_id: `F${nextNumber(content.facts, "fact_id", "F")}`,
      label,
      value,
      unit: "millions",
      currency: "USD",
      period,
      quotation,
      ...location,
      location,
      source_id: S1_ID,
      turn_id: turnId,
      verification: "numeric_and_location_match_semantics_not_verified",
    };
  };
  if (!fact) {
    fact = makeFact(Q_OPI26, "营业利润（FY2026）", "15104", "FY2026");
    content.facts.push(fact);
  }
  if (!completed) return;
  let prior = content.facts.find((item) => item.source_id === S1_ID && item.quotation === Q_OPI25);
  if (!prior) {
    prior = { ...makeFact(Q_OPI25, "营业利润（FY2025）", "12980", "FY2025"), turn_id: turnId };
    content.facts.push(prior);
  }
  content.claims.push({
    claim_id: `C${nextNumber(content.claims, "claim_id", "C")}`,
    role: "support",
    text: "营业利润由 12,980 百万美元增至 15,104 百万美元，同比增长 16.36%。",
    citations: [citation(text, Q_OPI26)],
    edited_by_user: false,
    turn_id: turnId,
  });
  const input = (f) => ({ fact_id: f.fact_id, period: f.period, unit: f.unit, value: f.value, currency: f.currency });
  content.calculations.push({
    calculation_id: `C-op-growth-${content.calculations.length + 1}`,
    formula_id: "growth_percent",
    formula_version: "1",
    expression: "(15104 / 12980 - 1) * 100",
    value: "16.36",
    unit: "percent",
    currency: null,
    decimal_places: 2,
    rounding: "half_up",
    inputs: [input(fact), input(prior)],
    input_verification: "not_verified_by_calculator",
    input_precision_status: "not_established",
    period_order_check: "recognized_formats_only",
    period_granularity_check: null,
    input_status: "matched",
    result_status: "matched",
    turn_id: turnId,
  });
}

/** Saves (or replaces) the run's turn and appends a new agent version. */
function commitTurn(data, run, mode, now) {
  const topic = data.topics.get(run.topic_id);
  const previous = topic.versions[topic.versions.length - 1];
  const content = ensureContent(previous.content);
  const sourceIds = previous.source_ids ?? topic.request.source_ids;
  // Only the sources this run read feed findings and summaries (seeded runs read every attached source).
  const readIds = run.readSourceIds ?? sourceIds;
  const attached = readIds.map((id) => data.sources.get(id)).filter(Boolean);
  const northwind = attached.find((source) => source.source_id === S1_ID && isEligibleForRun(source));
  const existing = run.replace_turn ? content.turns.find((turn) => turn.turn_id === run.turn_id) : null;
  const turnId = existing?.turn_id ?? run.turn_id ?? randomId("turn");
  run.turn_id = turnId;
  const completed = mode === "completed";

  if (northwind) addScriptedFindings(content, northwind, turnId, completed);
  const summarized = new Set(content.sources.map((row) => row.source_id));
  content.sources.push(...summarizeSources(attached.filter((source) => isEligibleForRun(source) && !summarized.has(source.source_id))));
  if (run.discoverActive) {
    const known = new Set(content.source_candidates.map((row) => row.url));
    for (const lead of DISCOVERY_CANDIDATES) {
      if (known.has(lead.url)) continue;
      content.source_candidates.push({
        title: lead.title,
        url: lead.url,
        published_at: lead.published_at,
        retrieved_at: iso(now),
        search_result_hash: sha256(lead.url).slice(0, 16),
        search_provider: "mock",
        status: "unverified",
      });
    }
  }
  const noSources = readIds.length === 0;

  const question = effectiveQuestion(topic, run);
  const turn = {
    turn_id: turnId,
    index: existing?.index ?? run.turn_index,
    question: question || DOCUMENT_OVERVIEW_QUESTION,
    question_origin: question ? "user" : "document_overview_default",
    status: completed ? "completed" : "partial",
    reason: completed ? "submitted" : "no_submission",
    started_at: existing?.started_at ?? run.started_at,
    finished_at: iso(now),
    parent_version_id: existing?.parent_version_id ?? run.parent_version_id,
    operation_ids: [...(existing?.operation_ids ?? []), run.operation.operation_id],
    usage: usageFromEvents(run.events),
    events: structuredClone(run.events),
    fact_ids: content.facts.filter((item) => item.turn_id === turnId).map((item) => item.fact_id),
    claim_ids: content.claims.filter((item) => item.turn_id === turnId).map((item) => item.claim_id),
    calculation_ids: content.calculations.filter((item) => item.turn_id === turnId).map((item) => item.calculation_id),
    chart_ids: content.charts.filter((item) => item.turn_id === turnId).map((item) => item.chart_id),
    gaps: completed ? (noSources ? [structuredClone(PARTIAL_GAPS[0])] : []) : structuredClone(PARTIAL_GAPS),
    read_ranges: northwind
      ? completed
        ? [operatingParagraphRange(northwind.text)]
        : [{ source_id: S1_ID, ...span(northwind.text, Q_OPI26) }]
      : [],
    official_fetch_status: "not_requested",
    search_status: run.discoverActive ? "completed" : "not_requested",
    stopping_note: null,
  };
  if (completed) {
    turn.report = northwind
      ? "## 营业利润与增速核验结论\n\n" +
        "本轮复核了年报摘录中的营业利润：2026 财年营业利润为 **15,104 百万美元**，上一财年为 **12,980 百万美元**，同比增长约 **+16.36%**。\n\n" +
        "| 财务指标 | FY2025 | FY2026 | 同比 |\n" +
        "| --- | ---: | ---: | ---: |\n" +
        "| 营业利润（USD M） | 12,980 | 15,104 | +16.36% |\n" +
        "| 示例对照规模（模拟参考） | $60.9B | $130.5B | +114.2% |\n\n" +
        "```text\n" +
        "growth_percent = (15104 / 12980 - 1) * 100 = 16.36%\n" +
        "```\n\n" +
        "来源与口径说明沿用年报摘录，延伸检索可参考 [SEC EDGAR](https://www.sec.gov/edgar/searchedgar/companysearch)。"
      : noSources
        ? "本轮没有选择任何来源，因此没有形成可引用的主张。\n\n这是本地 QA 模拟的运行结果，不含真实研究结论。"
        : "本轮已读取所附资料，但没有形成可引用的具体主张。\n\n这是本地 QA 模拟的运行结果，不含真实研究结论。";
  }
  const focus = run.focus_citation ?? existing?.focus_citation;
  if (focus) turn.focus_citation = { citation_index: focus.citation_index, claim_id: focus.claim_id };

  content.turns = existing ? content.turns.map((item) => (item.turn_id === turnId ? turn : item)) : [...content.turns, turn];
  content.gaps = structuredClone(turn.gaps);

  const version = {
    version_id: randomId("ver"),
    created_at: iso(now),
    created_by: "agent",
    // Backend ResearchVersionChain: an agent version resets review to draft but keeps the user's note.
    review: "draft",
    note: previous.note ?? "",
    content,
    source_ids: [...new Set([...sourceIds, ...(run.bodySourceIds ?? [])])],
    parent_version_id: previous.version_id,
  };
  topic.versions.push(version);
  topic.updated_at = version.created_at;
  run.version_id = version.version_id;
}

/** Advances one run by one step. */
function advance(data, run, broker) {
  const now = new Date();
  if (run.cancel_requested) {
    broker?.publishReset(run, { call_seq: run.streamCallSeq ?? 1, reason: "cancelled" });
    touch(run, now, "cancelled");
    settleQuota(data, run);
    broker?.publishTerminal(run);
    return;
  }
  if (run.operation.status === "pending") run.operation.status = "running";

  if (run.cursor === 0 && !run.streamThinkingDone) {
    run.streamThinkingDone = true;
    run.streamCallSeq = 1;
    touch(run, now);
    broker?.publishCurrent(run);
    broker?.publishDelta(run, {
      call_seq: 1,
      channel: "thinking",
      delta: "先核对 Northwind 2025 与 2026 财年营业利润与总收入所在章节，",
      seq: 0,
    });
    broker?.publishDelta(run, {
      call_seq: 1,
      channel: "thinking",
      delta: "确认单位均为百万美元（millions USD），并检查是否存在口径调整附注。",
      seq: 1,
    });
    broker?.publishDelta(run, {
      call_seq: 1,
      channel: "text",
      delta: "先读取年报摘录中的营业利润与收入段落，核对两年财报口径。",
      seq: 2,
    });
    return;
  }

  if (run.variant === "partial" && run.cursor === 2 && !run.streamPartialDraftDone) {
    run.streamPartialDraftDone = true;
    run.streamCallSeq = 2;
    touch(run, now);
    broker?.publishCurrent(run, { kind: "model", phase: "responding", retry_attempt: null, started_at: run.operation.updated_at });
    broker?.publishDelta(run, {
      call_seq: 2,
      channel: "tool_status",
      delta: "正在整理部分结论草稿…",
      seq: 0,
    });
    broker?.publishDelta(run, {
      call_seq: 2,
      channel: "draft_report",
      delta: "未完成草稿：正在尝试提交阶段性结果，等待服务端校验是否完整。",
      seq: 1,
    });
    return;
  }

  if (run.kind === "continue" && run.cursor === 3 && !run.streamContinueThinkingDone) {
    run.streamContinueThinkingDone = true;
    run.streamCallSeq = 1;
    touch(run, now);
    broker?.publishCurrent(run, { kind: "model", phase: "thinking", retry_attempt: null, started_at: run.operation.updated_at });
    broker?.publishDelta(run, {
      call_seq: 1,
      channel: "thinking",
      delta: "从断点继续执行：已读取总收入与前序步骤，接下来记录营业利润并计算同比。",
      seq: 0,
    });
    broker?.publishDelta(run, {
      call_seq: 1,
      channel: "text",
      delta: "续跑中：正在核对营业利润及增速计算。",
      seq: 1,
    });
    return;
  }

  if (run.cursor === RUN_STEPS.length - 1) {
    if (!run.streamDraftPhase) {
      run.streamDraftPhase = 1;
      run.streamCallSeq = 3;
      touch(run, now);
      broker?.publishCurrent(run, { kind: "model", phase: "responding", retry_attempt: null, started_at: run.operation.updated_at });
      broker?.publishDelta(run, {
        call_seq: 3,
        channel: "tool_status",
        delta: "正在起草研究报告并整理引文，完成后将自动核验原文位置…",
        seq: 0,
      });
      broker?.publishDelta(run, {
        call_seq: 3,
        channel: "draft_report",
        delta: "## 营业利润与增速核验结论\n\nNorthwind 示例公司在 FY2026 的营业利润较 FY2025 实现稳健增长（**+16.36%**）。\n\n",
        seq: 1,
      });
      broker?.publishDelta(run, {
        call_seq: 3,
        channel: "draft_report",
        delta: "| 财务指标 | FY2025 | FY2026 | 同比 |\n| --- | ---: | ---: | ---: |\n| 营业利润（USD M） | 12,980 | 15,104 | +16.36% |\n| 示例行业对照 | $60.9B | $130.5B | +114.2% |\n\n",
        seq: 2,
      });
      broker?.publishDelta(run, {
        call_seq: 3,
        channel: "draft_report",
        delta: run.variant === "rejected"
          ? "待核验段落：初步引用了一处未对齐的附注数字，服务端将进行位置核验。"
          : "```text\ngrowth_percent = (15104 / 12980 - 1) * 100 = 16.36%\n```\n\n参见 [SEC EDGAR](https://www.sec.gov/edgar/searchedgar/companysearch)。",
        seq: 3,
      });
      return;
    }
    if (run.variant === "rejected" && run.streamDraftPhase === 1) {
      run.streamDraftPhase = 2;
      touch(run, now);
      broker?.publishReset(run, { call_seq: 3, reason: "submission_rejected" });
      return;
    }
    if (run.variant === "rejected" && run.streamDraftPhase === 2) {
      run.streamDraftPhase = 3;
      run.streamCallSeq = 4;
      touch(run, now);
      broker?.publishDelta(run, {
        call_seq: 4,
        channel: "tool_status",
        delta: "正在修正引文位置并重新起草研究报告…",
        seq: 0,
      });
      broker?.publishDelta(run, {
        call_seq: 4,
        channel: "draft_report",
        delta: "修正后草稿：Northwind 示例公司 FY2026 营业利润同比增速约 16.36%，引文已重新对齐。",
        seq: 1,
      });
      return;
    }
  }

  if ((run.cursor === 0 && run.streamThinkingDone) || (run.kind === "continue" && run.cursor === 3 && run.streamContinueThinkingDone)) {
    broker?.publishReset(run, { call_seq: run.streamCallSeq ?? 1, reason: "tool_started" });
  }

  const eventIndex = run.events.length;
  run.events.push(rawEvent(RUN_STEPS[run.cursor]));
  run.cursor += 1;
  const cursor = run.cursor;
  if (cursor === RUN_STEPS.length) {
    broker?.publishReset(run, { call_seq: run.streamCallSeq ?? 3, reason: "call_completed" });
    commitTurn(data, run, "completed", now);
    touch(run, now, "completed");
  } else if (run.variant === "partial" && cursor === 3) {
    broker?.publishReset(run, { call_seq: run.streamCallSeq ?? 2, reason: "submission_partial" });
    commitTurn(data, run, "partial", now);
    touch(run, now, "failed");
  } else if (run.variant === "fail" && cursor === 2) {
    broker?.publishReset(run, { call_seq: run.streamCallSeq ?? 1, reason: "failed" });
    touch(run, now, "failed");
  } else {
    touch(run, now);
  }
  broker?.publishEvent(run, eventIndex);
  if (["completed", "failed", "cancelled"].includes(run.operation.status)) {
    settleQuota(data, run);
    broker?.publishTerminal(run);
  } else {
    broker?.publishCurrent(run);
  }
}

function currentActivity(run) {
  if (run.operation.status !== "running") return null;
  const step = RUN_STEPS[run.cursor];
  if (!step) return null;
  if (step.kind === "model") {
    return { kind: "model", phase: "thinking", retry_attempt: null, started_at: run.operation.updated_at };
  }
  if (step.kind === "tool") {
    return {
      kind: "tool",
      name: step.name,
      rationale: clip(step.rationale, 300),
      target: clip(step.target, 200),
      started_at: run.operation.updated_at,
    };
  }
  return null; // the submission step is not model or tool activity
}

const clip = (value, limit) => (typeof value === "string" ? value.slice(0, limit) : null);
const int = (value) => (Number.isInteger(value) ? value : null);

/** Mirrors research_progress.sanitize_event. */
function sanitizeEvent(event) {
  if (!isRecord(event)) return null;
  if (event.kind === "model") {
    const detail = isRecord(event.usage_detail) ? event.usage_detail : {};
    const note = clip(event.note, 600);
    return {
      kind: "model",
      finish_reason: clip(event.finish_reason, 100),
      usage: int(event.usage),
      input_tokens: int(detail.input_tokens),
      output_tokens: int(detail.output_tokens),
      settlement: clip(event.settlement, 100),
      ...(note ? { note } : {}),
    };
  }
  if (event.kind === "model_failure") return { kind: "model_failure", code: clip(event.code, 100) };
  if (event.kind === "tool") {
    return {
      kind: "tool",
      name: clip(event.name, 100),
      status: clip(event.status, 100),
      reason: clip(event.reason, 100),
      rationale: clip(event.rationale, 300),
      denied_count: int(event.denied_count),
      ...(isRecord(event.summary) ? { summary: structuredClone(event.summary) } : {}),
    };
  }
  if (event.kind === "submission") return { kind: "submission", status: clip(event.status, 100) };
  return null;
}

function interimFromEvents(events) {
  let leads = 0;
  let acquired = 0;
  let read = 0;
  let facts = 0;
  let calcs = 0;
  for (const event of events) {
    if (!isRecord(event) || event.kind !== "tool" || !isRecord(event.summary)) continue;
    const s = event.summary;
    if (s.kind === "news_search" && Number.isInteger(s.eligible_count)) leads += s.eligible_count;
    if (s.kind === "acquire_sources" && Number.isInteger(s.acquired_count)) acquired += s.acquired_count;
    if (s.kind === "read_sources" && Number.isInteger(s.read_count)) read += s.read_count;
    if (s.kind === "record_facts" && Number.isInteger(s.recorded_count)) facts += s.recorded_count;
    if (s.kind === "calculate_financials" && Number.isInteger(s.calculated_count)) calcs += s.calculated_count;
  }
  return {
    leads_count: leads,
    acquired_sources_count: acquired,
    read_sources_count: read,
    facts_count: facts,
    calculations_count: calcs,
  };
}

function buildProgress(run, after) {
  const events = [];
  run.events.forEach((event, index) => {
    const clean = sanitizeEvent(event);
    if (index >= after && clean) events.push({ index, ...clean });
  });
  return {
    operation_id: run.operation.operation_id,
    operation_status: run.operation.status,
    turn: { index: run.turn_index, question: run.question },
    usage: usageFromEvents(run.events),
    current: currentActivity(run),
    interim: interimFromEvents(run.events),
    events,
    event_count: run.events.length,
    started_at: run.started_at,
    updated_at: run.operation.updated_at,
    version_id: run.version_id ?? null,
  };
}

// ---- Account, model and market-data settings (mirrors model_credentials.py / market_credentials.py) ----

const STORAGE_UNAVAILABLE_KEY = "storage-unavailable-key";
const MODEL_PROTOCOLS = ["openai_chat_completions", "anthropic_messages"];
const unavailable = (code, message) => new HttpError(503, code, message, { retryable: true, headers: { "Retry-After": "30" } });
const modelUnavailable = () => unavailable("model_credential_storage_unavailable", "Model credential storage is unavailable.");
const marketUnavailable = () => unavailable("market_credential_storage_unavailable", "Market credential storage is unavailable.");
const modelProblem = (message) => new HttpError(422, "validation_error", message);

const codePointLength = (value) => Array.from(value).length;

/** Optional (nullable, default null) bounded string; returns null when absent or null. */
function readBounded(body, key, min, max, prefix = "body") {
  const value = body[key];
  if (value === undefined || value === null) return null;
  const length = typeof value === "string" ? codePointLength(value) : -1;
  if (length < min || length > max) throw invalid(`Input should be a string of ${min} to ${max} characters.`, `${prefix}/${key}`);
  return value;
}

function readStrictBool(body, key, prefix = "body") {
  if (body[key] === undefined) return false;
  if (typeof body[key] !== "boolean") throw invalid("Input should be a valid boolean.", `${prefix}/${key}`);
  return body[key];
}

function readEnum(value, allowed, location) {
  if (typeof value !== "string" || !allowed.includes(value)) throw invalid(`Input should be ${allowed.map((item) => `'${item}'`).join(", ")}.`, location);
  return value;
}

/** Mirrors ModelConfigUpdate (strict, extra forbid, plus its after-validator). */
function readModelConfigUpdate(body) {
  exactKeys(body, ["mode", "protocol", "base_url", "model", "api_key", "clear_api_key", "messages_options"], "body");
  if (body.mode === undefined) throw invalid("Field required.", "body/mode");
  const mode = readEnum(body.mode, ["platform", "personal"], "body/mode");
  const protocol = body.protocol === undefined || body.protocol === null ? null : readEnum(body.protocol, MODEL_PROTOCOLS, "body/protocol");
  const options = body.messages_options ?? null;
  if (options !== null) {
    exactKeys(options, ["version", "auth_scheme", "max_tokens"], "body/messages_options");
    if (typeof options.version !== "string" || !/^[!-~]{1,64}$/.test(options.version)) throw invalid("Invalid Messages version.");
    readEnum(options.auth_scheme, ["x-api-key", "bearer"], "body/messages_options/auth_scheme");
    readInt(options.max_tokens, "body/messages_options/max_tokens", { min: 1 });
  }
  const request = {
    mode,
    protocol,
    messages_options: options,
    base_url: readBounded(body, "base_url", 1, 2048),
    model: readBounded(body, "model", 1, 128),
    api_key: readBounded(body, "api_key", 8, 512),
    clear_api_key: readStrictBool(body, "clear_api_key"),
  };
  if (request.api_key !== null && request.clear_api_key) throw invalid("api_key and clear_api_key are mutually exclusive");
  if (mode === "platform" && (options !== null || protocol !== null || request.base_url !== null || request.model !== null || request.api_key !== null || request.clear_api_key)) {
    throw invalid("platform mode does not accept personal model fields");
  }
  for (const key of ["base_url", "model", "api_key"]) {
    if (request[key] !== null && request[key] !== request[key].trim()) throw invalid(`${key} must not contain surrounding whitespace`, `body/${key}`);
  }
  return request;
}

/** Mirrors `_hint`: last 4 chars when ASCII alphanumeric, else the last 4 hex of the sha256. */
function keyHint(apiKey) {
  const suffix = Array.from(apiKey).slice(-4).join("");
  return `••••${/^[A-Za-z0-9]{4}$/.test(suffix) ? suffix : sha256(apiKey).slice(-4)}`;
}

function ipv4Blocked(a, b, c) {
  return a === 0 || a === 10 || a === 127 || a >= 224 ||
    (a === 100 && b >= 64 && b <= 127) ||
    (a === 169 && b === 254) ||
    (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && b === 0 && (c === 0 || c === 2)) ||
    (a === 192 && b === 168) ||
    (a === 198 && (b === 18 || b === 19 || (b === 51 && c === 100))) ||
    (a === 203 && b === 0 && c === 113);
}

/** Expands a (bracket-stripped, WHATWG-normalized) IPv6 literal into 8 16-bit groups, or null. */
function ipv6Groups(text) {
  const halves = text.split("::");
  if (halves.length > 2) return null;
  const parse = (part) => (part ? part.split(":").map((group) => Number.parseInt(group, 16)) : []);
  const head = parse(halves[0]);
  const tail = halves.length === 2 ? parse(halves[1]) : [];
  const fill = halves.length === 2 ? 8 - head.length - tail.length : 0;
  const groups = [...head, ...Array(Math.max(fill, 0)).fill(0), ...tail];
  return groups.length === 8 && groups.every((group) => Number.isInteger(group)) ? groups : null;
}

/** Simulates the public-DNS check: private/loopback/link-local literals and local-only names fail. */
function hostBlocked(hostname) {
  const host = hostname.toLowerCase().replace(/\.$/, "");
  if (host === "localhost" || host.endsWith(".localhost") || host.endsWith(".local") || host.endsWith(".internal")) return true;
  const v4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(host);
  if (v4) return ipv4Blocked(Number(v4[1]), Number(v4[2]), Number(v4[3]));
  if (host.startsWith("[") && host.endsWith("]")) {
    const g = ipv6Groups(host.slice(1, -1));
    if (!g) return true;
    if (g.every((group) => group === 0) || (g.slice(0, 7).every((group) => group === 0) && g[7] === 1)) return true;
    if ((g[0] & 0xfe00) === 0xfc00 || (g[0] & 0xffc0) === 0xfe80 || (g[0] & 0xff00) === 0xff00) return true;
    if (g.slice(0, 5).every((group) => group === 0) && g[5] === 0xffff) return ipv4Blocked(g[6] >> 8, g[6] & 255, g[7] >> 8);
  }
  return false;
}

/** Mirrors OpenAICompatibleSettings.from_account_values; returns whether the values are acceptable. */
function personalValuesValid(baseUrl, model) {
  if (baseUrl.includes("\\") || /[\u0000-\u001f]/.test(model)) return false;
  let url;
  try {
    url = new URL(baseUrl);
  } catch {
    return false;
  }
  if (url.protocol !== "https:" || !url.hostname || url.username || url.password || url.search || url.hash || baseUrl.includes("?") || baseUrl.includes("#")) return false;
  if (url.port && url.port !== "443") return false;
  const host = url.hostname.toLowerCase().replace(/\.$/, "");
  if (host === "metadata.google.internal" || host === "metadata.azure.internal") return false;
  return !hostBlocked(host);
}

const originOf = (baseUrl) => {
  const url = new URL(baseUrl);
  return `${url.protocol}|${url.hostname}|${url.port || "443"}`;
};

/** Mirrors AccountModelSettings.snapshot (response_model_exclude_none). Never includes the key. */
function modelSnapshot(config) {
  if (!config) return { schema_version: "v1", mode: "platform", configured: true };
  return {
    schema_version: "v1", mode: config.mode, configured: true, updated_at: config.updated_at,
    ...(config.key_hint ? { protocol: config.protocol, base_url: config.base_url, model: config.model, key_hint: config.key_hint } : {}),
    ...(config.messages_options ? { messages_options: config.messages_options } : {}),
    ...(config.research_tools_verified_at ? { research_tools_verified_at: config.research_tools_verified_at } : {}),
  };
}

/** Mirrors AccountModelSettings.update. Returns the new stored config; throws HttpError. */
function applyModelUpdate(existing, request, now) {
  if (request.mode === "platform") {
    // Keep the personal configuration (and key) so switching back needs no re-entry; DELETE removes it.
    return { ...existing, mode: "platform", updated_at: iso(now) };
  }
  const prior = existing ?? null;
  const baseUrl = request.base_url ?? prior?.base_url ?? null;
  const model = request.model ?? prior?.model ?? null;
  const protocol = request.protocol ?? prior?.protocol ?? "openai_chat_completions";
  if (prior?.base_url && baseUrl && request.api_key === null) {
    let changed;
    try {
      changed = originOf(prior.base_url) !== originOf(baseUrl);
    } catch {
      throw modelProblem("Personal model configuration is invalid.");
    }
    if (changed || protocol !== prior.protocol) throw modelProblem("Changing model origin or protocol requires supplying the API key again.");
  }
  let apiKey = prior?.api_key ?? null;
  let hint = prior?.key_hint ?? null;
  if (request.clear_api_key) {
    apiKey = null;
    hint = null;
  } else if (request.api_key !== null) {
    if (request.api_key === STORAGE_UNAVAILABLE_KEY) throw modelUnavailable();
    apiKey = request.api_key;
    hint = keyHint(request.api_key);
  }
  if (!baseUrl || !model || !apiKey || !hint) throw modelProblem("Personal model configuration is incomplete.");
  if (!personalValuesValid(baseUrl, model)) throw modelProblem("Personal model configuration is invalid.");
  if (request.messages_options && protocol !== "anthropic_messages") throw modelProblem("Messages options require the Messages protocol.");
  const same = protocol === prior?.protocol && baseUrl === prior?.base_url && model === prior?.model;
  const options = request.messages_options ?? (same ? prior.messages_options : null);
  const verified = same && request.api_key === null && JSON.stringify(options) === JSON.stringify(prior.messages_options) ? prior.research_tools_verified_at : null;
  return { mode: "personal", protocol, messages_options: options, research_tools_verified_at: verified, base_url: baseUrl, model, api_key: apiKey, key_hint: hint, updated_at: iso(now) };
}

/** Mirrors MarketDataConfigUpdate (strict, extra forbid, plus its after-validators). */
function readMarketDataUpdate(body) {
  exactKeys(body, ["fallback_order", "providers"], "body");
  const exactlyOnce = (ids, location) => {
    if (ids.length !== MARKET_PROVIDER_ORDER.length || new Set(ids).size !== ids.length || !ids.every((id) => MARKET_PROVIDER_ORDER.includes(id))) {
      throw invalid(`${location.split("/").pop()} must contain every supported provider exactly once`, location);
    }
  };
  if (!Array.isArray(body.fallback_order)) throw invalid("Input should be a valid list.", "body/fallback_order");
  body.fallback_order.forEach((id, i) => readEnum(id, MARKET_PROVIDER_ORDER, `body/fallback_order/${i}`));
  if (!Array.isArray(body.providers)) throw invalid("Input should be a valid list.", "body/providers");
  const providers = body.providers.map((raw, i) => {
    const where = `body/providers/${i}`;
    exactKeys(raw, ["provider_id", "enabled", "api_key", "clear_api_key"], where);
    const providerId = readEnum(raw.provider_id, MARKET_PROVIDER_ORDER, `${where}/provider_id`);
    if (typeof raw.enabled !== "boolean") throw invalid("Input should be a valid boolean.", `${where}/enabled`);
    const item = {
      provider_id: providerId,
      enabled: raw.enabled,
      api_key: readBounded(raw, "api_key", 8, 512, where),
      clear_api_key: readStrictBool(raw, "clear_api_key", where),
    };
    if (item.api_key !== null && item.clear_api_key) throw invalid("api_key and clear_api_key are mutually exclusive", where);
    if (!KEYED_MARKET_PROVIDERS.has(providerId) && (item.api_key !== null || item.clear_api_key)) throw invalid("this provider does not accept an API key", where);
    if (item.api_key !== null && item.api_key !== item.api_key.trim()) throw invalid("api_key must not contain surrounding whitespace", `${where}/api_key`);
    return item;
  });
  exactlyOnce(body.fallback_order, "body/fallback_order");
  exactlyOnce(providers.map((item) => item.provider_id), "body/providers");
  return { fallback_order: [...body.fallback_order], providers };
}

/** Mirrors AccountMarketDataSettings.snapshot: providers in MARKET_PROVIDER_ORDER, null key_hint kept. */
function marketSnapshot(config) {
  return {
    schema_version: "v1",
    fallback_order: config ? [...config.fallback_order] : [...MARKET_PROVIDER_ORDER],
    providers: MARKET_PROVIDER_ORDER.map((id) => {
      const record = config?.providers[id];
      return {
        provider_id: id,
        display_name: PROVIDER_LABELS[id],
        enabled: record ? record.enabled : true,
        requires_key: KEYED_MARKET_PROVIDERS.has(id),
        personal_key_configured: Boolean(record?.api_key),
        platform_available: PLATFORM_AVAILABLE[id],
        key_hint: record?.key_hint ?? null,
      };
    }),
    updated_at: config?.updated_at ?? null,
  };
}

/** Mirrors AccountMarketDataSettings.update: keys are kept unless set or cleared. */
function applyMarketUpdate(existing, request, now) {
  if (request.providers.some((item) => item.api_key === STORAGE_UNAVAILABLE_KEY)) throw marketUnavailable();
  const providers = {};
  for (const item of request.providers) {
    const previous = existing?.providers[item.provider_id];
    let apiKey = previous?.api_key ?? null;
    let hint = previous?.key_hint ?? null;
    if (item.clear_api_key) {
      apiKey = null;
      hint = null;
    } else if (item.api_key !== null) {
      apiKey = item.api_key;
      hint = keyHint(item.api_key);
    }
    providers[item.provider_id] = { enabled: item.enabled, api_key: apiKey, key_hint: hint };
  }
  return { fallback_order: request.fallback_order, providers, updated_at: iso(now) };
}

/** Simulated provider probe: no network is used. */
function simulateMarketProbe(config, providerId) {
  const record = config?.providers[providerId];
  const enabled = record ? record.enabled : true;
  const personalKey = record?.api_key ?? null;
  const usable = !KEYED_MARKET_PROVIDERS.has(providerId) || personalKey !== null || PLATFORM_AVAILABLE[providerId];
  return enabled && usable && !(personalKey ?? "").includes("bad") ? "ready" : "unavailable";
}

/** Mirrors the platform quota ledger: only runs that started in platform mode consume tokens. */
function settleQuota(data, run) {
  if (run.quotaSettled) return;
  run.quotaSettled = true;
  if (!run.platformModel) return;
  const total = usageFromEvents(run.events).total_tokens;
  data.billing.consumed += Math.max(0, total - (run.quotaBaseline ?? 0));
}

function nextMonthStart(now) {
  return iso(new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1)));
}

// ---- Server -----------------------------------------------------------------------

export function createServer({ tickMs = 1500, heartbeatMs = 15000, maxSsePerAccount = 6 } = {}) {
  /** accountId -> { email, created_at, data } */
  const accounts = new Map();
  /** token -> { accountId, expiresAt } */
  const sessions = new Map();
  /** scoped key -> { hash, operationId } */
  const idempotency = new Map();
  /** operationId -> Set<{ res, cleanup, lastSentIndex }> */
  const sseSubscribers = new Map();
  /** accountId -> number of active SSE responses */
  const accountSseCounts = new Map();
  let runSeq = 1000;

  const writeSseFrame = (res, event, data, id = null) => {
    if (res.writableEnded || res.destroyed) return;
    const head = id !== null ? `event: ${event}\nid: ${id}\n` : `event: ${event}\n`;
    res.write(`${head}data: ${JSON.stringify(data)}\n\n`);
  };

  const sseBroker = {
    publishEvent(run, index) {
      const subs = sseSubscribers.get(run.operation.operation_id);
      if (!subs || subs.size === 0) return;
      const clean = sanitizeEvent(run.events[index]);
      if (!clean) return;
      const payload = {
        operation_id: run.operation.operation_id,
        event: { index, ...clean },
        event_count: run.events.length,
        usage: usageFromEvents(run.events),
        interim: interimFromEvents(run.events),
        updated_at: run.operation.updated_at,
      };
      for (const sub of subs) {
        if (index <= sub.lastSentIndex) continue;
        sub.lastSentIndex = index;
        writeSseFrame(sub.res, "step.event", payload, `ev:${index}`);
      }
    },
    publishCurrent(run, overrideCurrent) {
      const subs = sseSubscribers.get(run.operation.operation_id);
      if (!subs || subs.size === 0) return;
      const payload = {
        operation_id: run.operation.operation_id,
        operation_status: run.operation.status,
        current: overrideCurrent === undefined ? currentActivity(run) : overrideCurrent,
        usage: usageFromEvents(run.events),
        interim: interimFromEvents(run.events),
        updated_at: run.operation.updated_at,
      };
      for (const sub of subs) writeSseFrame(sub.res, "step.current", payload);
    },
    publishDelta(run, { call_seq, channel, delta, seq }) {
      const subs = sseSubscribers.get(run.operation.operation_id);
      if (!subs || subs.size === 0) return;
      const payload = {
        operation_id: run.operation.operation_id,
        call_seq,
        channel,
        delta,
        seq,
      };
      for (const sub of subs) writeSseFrame(sub.res, "stream.delta", payload);
    },
    publishReset(run, { call_seq, reason }) {
      const subs = sseSubscribers.get(run.operation.operation_id);
      if (!subs || subs.size === 0) return;
      const payload = {
        operation_id: run.operation.operation_id,
        call_seq,
        reason,
      };
      for (const sub of subs) writeSseFrame(sub.res, "stream.reset", payload);
    },
    publishTerminal(run) {
      const subs = sseSubscribers.get(run.operation.operation_id);
      if (!subs || subs.size === 0) return;
      const payload = {
        operation_id: run.operation.operation_id,
        operation_status: run.operation.status,
        version_id: run.version_id ?? null,
      };
      for (const sub of [...subs]) {
        writeSseFrame(sub.res, "run.terminal", payload, `done:${run.operation.status}`);
        sub.cleanup();
        if (!sub.res.writableEnded) sub.res.end();
      }
    },
  };

  function parseLastEventId(raw, after) {
    if (raw === undefined || raw === null) return { after, alreadyDone: false };
    const value = String(raw).trim();
    if (!value) return { after, alreadyDone: false };
    let match = /^ev:(\d+)$/.exec(value);
    if (match) return { after: Math.max(after, Number(match[1]) + 1), alreadyDone: false };
    match = /^cp:(\d+)$/.exec(value);
    if (match) return { after: Math.max(after, Number(match[1])), alreadyDone: false };
    if (/^done:(completed|failed|cancelled)$/.test(value)) return { after, alreadyDone: true };
    throw new HttpError(400, "invalid_last_event_id", "Invalid Last-Event-ID header.");
  }

  function issueSession(email, status, res) {
    const accountId = `acct_${sha256(email).slice(0, 16)}`;
    let account = accounts.get(accountId);
    if (!account) {
      account = { email, created_at: iso(new Date()), data: null };
      accounts.set(accountId, account);
    }
    const token = `mock_${randomBytes(24).toString("hex")}`;
    const expires = new Date(Date.now() + 12 * 60 * 60 * 1000);
    sessions.set(token, { accountId, expiresAt: expires.getTime() });
    send(res, status, {
      account: { account_id: accountId, email: account.email, created_at: account.created_at },
      session_id: `sess_${randomBytes(8).toString("hex")}`,
      access_token: token,
      expires_at: iso(expires),
      auth_mode: "local_mock",
    });
  }

  function bearer(req) {
    const header = req.headers.authorization;
    const match = typeof header === "string" ? /^Bearer\s+(\S+)$/i.exec(header) : null;
    if (!match) return null;
    const session = sessions.get(match[1]);
    if (!session) return null;
    if (session.expiresAt <= Date.now()) {
      sessions.delete(match[1]);
      return null;
    }
    return { token: match[1], accountId: session.accountId };
  }

  function dataFor(accountId) {
    const account = accounts.get(accountId);
    if (!account.data) {
      const fresh = seed(new Date());
      const data = {
        topics: new Map(),
        sources: new Map(),
        runs: new Map(),
        modelConfig: null,
        marketConfig: null,
        billing: { consumed: SEEDED_QUOTA_CONSUMED },
      };
      for (const [id, source] of Object.entries(fresh.sources)) data.sources.set(id, { ...source, owner_id: accountId });
      for (const topic of fresh.topics) data.topics.set(topic.topic_id, { ...topic, owner_id: accountId });
      for (const run of fresh.runs) data.runs.set(run.operation.operation_id, run);
      account.data = data;
    }
    return account.data;
  }

  const getTopic = (data, id) => {
    const topic = data.topics.get(id);
    if (!topic) throw notFound("Research topic");
    return topic;
  };
  const getRun = (data, topicId, operationId) => {
    const run = data.runs.get(operationId);
    if (!run || run.topic_id !== topicId) throw notFound("Operation");
    return run;
  };
  const getVersion = (topic, versionId) => {
    const version = topic.versions.find((item) => item.version_id === versionId);
    if (!version) throw notFound("Research version");
    return version;
  };
  const latestOf = (topic) => topic.versions[topic.versions.length - 1];
  const isActive = (run) => run.operation.status === "pending" || run.operation.status === "running";
  const hasActiveRun = (data, topicId) => [...data.runs.values()].some((run) => run.topic_id === topicId && isActive(run));
  const operationResponse = (run) => ({ operation: run.operation });

  function recoveryStatus(data, run) {
    const status = run.operation.status;
    const none = { can_continue: false, can_deliver: false, reason: null, checkpoint: null, version_id: null };
    if (status === "pending" || status === "running") return { ...none, reason: "running" };
    if (status === "completed") return { ...none, reason: "unavailable" };
    if (status === "cancelled") return { ...none, reason: "no_checkpoint" };
    // failed: a checkpoint exists only for the scripted partial / failure variants.
    if (run.variant !== "partial" && run.variant !== "fail") return { ...none, reason: "no_checkpoint" };
    const topic = data.topics.get(run.topic_id);
    const latest = latestOf(topic);
    const expected = run.variant === "partial" ? run.version_id : run.parent_version_id;
    let reason = null;
    if (run.continued_by) reason = "already_used";
    else if (latest.version_id !== expected) reason = "changed";
    return { can_continue: reason === null, can_deliver: false, reason, checkpoint: "tool_batch", version_id: latest.version_id };
  }

  function newRun(data, topic, init) {
    const now = new Date();
    runSeq += 1;
    const operationId = `op_${randomBytes(8).toString("hex")}`;
    const run = {
      operation: makeOperation({
        operationId,
        topicId: topic.topic_id,
        status: "pending",
        createdAt: now,
        updatedAt: now,
        key: init.key,
        body: init.body,
      }),
      topic_id: topic.topic_id,
      kind: init.kind,
      variant: init.variant,
      question_override: init.question_override ?? null,
      focus_citation: init.focus_citation ?? null,
      parent_version_id: latestOf(topic).version_id,
      turn_id: init.turn_id ?? null,
      turn_index: init.turn_index,
      replace_turn: init.replace_turn ?? false,
      events: init.events ?? [],
      cursor: init.cursor ?? 0,
      cancel_requested: false,
      continued_by: null,
      version_id: null,
      started_at: iso(now),
      seq: runSeq,
      readSourceIds: init.readSourceIds,
      bodySourceIds: init.bodySourceIds ?? [],
      resolvedDiscover: init.resolvedDiscover ?? false,
      discoverActive: init.discoverActive ?? false,
      // Personal models never consume platform quota; the mode is fixed at run start.
      platformModel: data.modelConfig === null || data.modelConfig.mode === "platform",
      quotaBaseline: init.quotaBaseline ?? 0,
      quotaSettled: false,
    };
    run.operation.last_event_id = `${operationId}:${run.events.length}`;
    run.question = init.question;
    data.runs.set(operationId, run);
    return run;
  }

  /** Idempotency: returns the stored run for the same key and body, throws on a body change. */
  function replay(data, scope, key, bodyHash) {
    const entry = idempotency.get(scope);
    if (!entry) return null;
    if (entry.hash !== bodyHash) {
      throw new HttpError(409, "idempotency_conflict", "Idempotency key was already used with a different request.");
    }
    return data.runs.get(entry.operationId) ?? null;
  }

  const requireKey = (req) => {
    const key = req.headers["idempotency-key"];
    if (typeof key !== "string" || !key.trim()) throw new HttpError(400, "bad_request", "Idempotency-Key header is required.");
    return key.trim();
  };

  function startRun({ req, res, auth, params, body }) {
    const data = dataFor(auth.accountId);
    const topic = getTopic(data, params.topic);
    const key = requireKey(req);
    validateRunRequest(body);
    const bodySourceIds = body.source_ids ?? [];
    const available = [...new Set([...topic.request.source_ids, ...latestOf(topic).source_ids, ...bodySourceIds])];
    const selected = Array.isArray(body.selected_source_ids) ? body.selected_source_ids : null;
    if (selected && !selected.every((id) => available.includes(id))) {
      throw new HttpError(422, "invalid_request", "Selected sources must belong to this research request.");
    }
    const readSourceIds = selected ?? available;
    for (const id of [...bodySourceIds, ...readSourceIds]) {
      if (!data.sources.has(id)) throw notFound("Research topic or source");
    }
    const scope = `${auth.accountId}|start|${topic.topic_id}|${key}`;
    const hash = sha256(stableStringify(body));
    const existing = replay(data, scope, key, hash);
    if (existing) return send(res, 200, operationResponse(existing));
    if (body.parent_version_id !== latestOf(topic).version_id) {
      throw new HttpError(409, "lifecycle_conflict", "The research topic has a newer version than the run parent.");
    }
    if (hasActiveRun(data, topic.topic_id)) {
      throw new HttpError(409, "lifecycle_conflict", "A research run is already active on this topic.");
    }
    const question = effectiveQuestion(topic, { question_override: body.question_override });
    const resolvedDiscover = resolveDiscovery(body);
    const discoverActive = resolvedDiscover && body.max_search_calls !== 0;
    const run = newRun(data, topic, {
      kind: "start",
      key,
      body,
      readSourceIds,
      bodySourceIds,
      resolvedDiscover,
      discoverActive,
      events: discoverActive ? [rawEvent(DISCOVERY_STEP)] : [],
      variant: pickVariant(question),
      question: question || DOCUMENT_OVERVIEW_QUESTION,
      question_override: body.question_override,
      focus_citation: body.focus_citation,
      turn_index: ensureContent(latestOf(topic).content).turns.length + 1,
    });
    idempotency.set(scope, { hash, operationId: run.operation.operation_id });
    send(res, 202, operationResponse(run));
  }

  function continueRun({ req, res, auth, params }, recover) {
    const data = dataFor(auth.accountId);
    const topic = getTopic(data, params.topic);
    const source = getRun(data, topic.topic_id, params.op);
    const key = requireKey(req);
    const scope = `${auth.accountId}|continue|${source.operation.operation_id}|${key}`;
    const hash = sha256("");
    const existing = replay(data, scope, key, hash);
    if (existing) return send(res, 200, operationResponse(existing));
    // Mock checkpoints never hold a finished submission, so there is nothing to deliver.
    if (recover) throw new HttpError(409, "lifecycle_conflict", "No completed runtime submission is available for recovery.");
    const status = recoveryStatus(data, source);
    if (!status.can_continue) throw new HttpError(409, "lifecycle_conflict", "This run has no continuable checkpoint.");
    if (hasActiveRun(data, topic.topic_id)) {
      throw new HttpError(409, "lifecycle_conflict", "A research run is already active on this topic.");
    }
    const partial = source.variant === "partial";
    const run = newRun(data, topic, {
      kind: "continue",
      key,
      body: {},
      variant: "normal",
      question: source.question,
      question_override: source.question_override,
      focus_citation: source.focus_citation,
      turn_id: partial ? source.turn_id : null,
      turn_index: source.turn_index,
      replace_turn: partial,
      events: structuredClone(source.events),
      cursor: source.cursor,
      readSourceIds: source.readSourceIds,
      bodySourceIds: source.bodySourceIds,
      resolvedDiscover: source.resolvedDiscover,
      discoverActive: source.discoverActive,
      quotaBaseline: usageFromEvents(source.events).total_tokens,
    });
    source.continued_by = run.operation.operation_id;
    idempotency.set(scope, { hash, operationId: run.operation.operation_id });
    send(res, 202, operationResponse(run));
  }

  /** Visible source metadata (no text, no status); mirrors the backend's _visible_research_source. */
  function sourceRow(source) {
    const { text: _text, owner_id: _owner, url: _url, original_bytes: _original, ...metadata } = source;
    if (source.url) metadata.url = source.url;
    if (source.retrieval_method === "user_pdf") metadata.original_pdf_available = Boolean(source.original_bytes);
    if (HTML_METHODS.has(source.retrieval_method)) {
      metadata.original_html_available = Boolean(source.original_bytes);
      metadata.extraction_disclosure = HTML_EXTRACTION_DISCLOSURE;
    }
    metadata.verification = "user_declared_not_verified";
    return metadata;
  }

  const visibleSource = (source, { withText = true } = {}) => {
    const row = sourceRow(source);
    if (withText && source.permissions.display) row.text = source.text;
    return row;
  };
  const getSource = (data, id) => {
    const source = data.sources.get(id);
    if (!source) throw notFound("Research source");
    return source;
  };

  function versionSources(data, topic, version) {
    return version.source_ids.map((id) => {
      const source = data.sources.get(id);
      if (!source) return { source_id: id, status: "unavailable" };
      return { ...sourceRow(source), status: "available", verification: "user_declared_not_verified" };
    });
  }

  function textAvailable(topic, source) {
    const cutoff = topic.request.as_of ? Date.parse(topic.request.as_of) : null;
    return !(
      !source.permissions.display ||
      source.is_simulated ||
      (cutoff !== null &&
        (!source.published_at || Date.parse(source.published_at) > cutoff ||
          (source.revised_at && Date.parse(source.revised_at) > cutoff)))
    );
  }

  const routes = [];
  const route = (method, pattern, auth, handler) => routes.push({ method, parts: pattern.split("/").filter(Boolean), auth, handler });

  // Auth (no bearer needed)
  route("GET", "/v1/auth/providers", false, ({ res }) => send(res, 200, { providers: [] }));
  const credentials = (body) => {
    if (!isRecord(body)) throw invalid("Body must be a JSON object.");
    const { email, password } = body;
    if (typeof email !== "string" || !/^[^\s@]+@[^\s@]+$/.test(email.trim())) throw invalid("Input should be a valid email address.", "body/email");
    if (typeof password !== "string" || password.length < 12 || password.length > 256) {
      throw invalid("String should have at least 12 and at most 256 characters.", "body/password");
    }
    return email.trim().toLowerCase();
  };
  route("POST", "/v1/auth/local/register", false, ({ res, body }) => issueSession(credentials(body), 201, res));
  route("POST", "/v1/auth/local/login", false, ({ res, body }) => issueSession(credentials(body), 200, res));
  route("POST", "/v1/auth/local/logout", false, ({ req, res }) => {
    const auth = bearer(req);
    if (auth) sessions.delete(auth.token);
    send(res, 204);
  });
  route("DELETE", "/v1/auth/local/account", false, ({ req, res }) => {
    const auth = bearer(req);
    if (auth) {
      accounts.delete(auth.accountId);
      for (const [token, session] of sessions) if (session.accountId === auth.accountId) sessions.delete(token);
    }
    send(res, 204);
  });

  // Topics
  route("GET", "/v2/research/topics", true, ({ res, auth }) => {
    const data = dataFor(auth.accountId);
    const topics = [...data.topics.values()].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at));
    send(res, 200, topics);
  });
  route("GET", "/v2/research/topics/:topic", true, ({ res, auth, params }) =>
    send(res, 200, getTopic(dataFor(auth.accountId), params.topic)));
  route("POST", "/v2/research/topics", true, ({ res, auth, body }) => {
    const data = dataFor(auth.accountId);
    validateResearchInput(body ?? {});
    const input = body ?? {};
    for (const id of input.source_ids ?? []) {
      if (!data.sources.has(id)) throw new HttpError(422, "invalid_request", "A referenced research source was not found.");
    }
    const now = iso(new Date());
    const sourceIds = input.source_ids ?? [];
    const topic = {
      topic_id: randomId("topic"),
      owner_id: auth.accountId,
      schema_version: "1",
      created_at: now,
      updated_at: now,
      request: { as_of: input.as_of ?? null, question: input.question ?? "", scope: input.scope ?? "", source_ids: sourceIds },
      versions: [
        {
          version_id: randomId("ver"),
          created_at: now,
          created_by: "user",
          review: "draft",
          content: {},
          source_ids: sourceIds,
          parent_version_id: null,
          note: "",
        },
      ],
    };
    data.topics.set(topic.topic_id, topic);
    send(res, 201, topic);
  });

  // Runs
  route("GET", "/v2/research/topics/:topic/runs", true, ({ res, auth, params }) => {
    const data = dataFor(auth.accountId);
    getTopic(data, params.topic);
    const runs = [...data.runs.values()]
      .filter((run) => run.topic_id === params.topic)
      .sort((a, b) => Date.parse(b.operation.created_at) - Date.parse(a.operation.created_at) || b.seq - a.seq);
    send(res, 200, runs.map(operationResponse));
  });
  route("POST", "/v2/research/topics/:topic/runs", true, startRun);
  route("GET", "/v2/research/topics/:topic/runs/:op", true, ({ res, auth, params }) => {
    const data = dataFor(auth.accountId);
    send(res, 200, operationResponse(getRun(data, getTopic(data, params.topic).topic_id, params.op)));
  });
  route("GET", "/v2/research/topics/:topic/runs/:op/progress", true, ({ res, auth, params, query }) => {
    const data = dataFor(auth.accountId);
    const run = getRun(data, getTopic(data, params.topic).topic_id, params.op);
    const raw = query.get("after");
    const after = raw === null ? 0 : /^\d+$/.test(raw) ? Number(raw) : NaN;
    if (!Number.isSafeInteger(after)) throw invalid("Input should be a non-negative integer.", "query/after");
    send(res, 200, buildProgress(run, after));
  });
  route("GET", "/v2/research/topics/:topic/runs/:op/events", true, ({ req, res, auth, params, query }) => {
    const data = dataFor(auth.accountId);
    const run = getRun(data, getTopic(data, params.topic).topic_id, params.op);
    const raw = query.get("after");
    const baseAfter = raw === null ? 0 : /^\d+$/.test(raw) ? Number(raw) : NaN;
    if (!Number.isSafeInteger(baseAfter)) throw invalid("Input should be a non-negative integer.", "query/after");
    const { after, alreadyDone } = parseLastEventId(req.headers["last-event-id"], baseAfter);
    const activeCount = accountSseCounts.get(auth.accountId) ?? 0;
    if (activeCount >= maxSsePerAccount) {
      throw new HttpError(429, "rate_limited", "Too many concurrent research event streams.", {
        retryable: true,
        headers: { "Retry-After": "5" },
      });
    }
    accountSseCounts.set(auth.accountId, activeCount + 1);
    res.writeHead(200, {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    });
    res.flushHeaders?.();
    const snapshot = buildProgress(run, after);
    writeSseFrame(res, "progress.snapshot", snapshot, `cp:${snapshot.event_count}`);

    const opId = run.operation.operation_id;
    let cleaned = false;
    let hbTimer = null;
    const sub = {
      res,
      lastSentIndex: snapshot.event_count - 1,
      cleanup: () => {
        if (cleaned) return;
        cleaned = true;
        if (hbTimer) clearInterval(hbTimer);
        const set = sseSubscribers.get(opId);
        if (set) {
          set.delete(sub);
          if (set.size === 0) sseSubscribers.delete(opId);
        }
        const nextCount = Math.max(0, (accountSseCounts.get(auth.accountId) ?? 1) - 1);
        if (nextCount === 0) accountSseCounts.delete(auth.accountId);
        else accountSseCounts.set(auth.accountId, nextCount);
      },
    };

    if (alreadyDone || ["completed", "failed", "cancelled"].includes(run.operation.status)) {
      writeSseFrame(res, "run.terminal", {
        operation_id: opId,
        operation_status: run.operation.status,
        version_id: run.version_id ?? null,
      }, `done:${run.operation.status}`);
      sub.cleanup();
      res.end();
      return;
    }

    let set = sseSubscribers.get(opId);
    if (!set) {
      set = new Set();
      sseSubscribers.set(opId, set);
    }
    set.add(sub);
    hbTimer = setInterval(() => {
      if (!res.writableEnded && !res.destroyed) res.write(": heartbeat\n\n");
    }, heartbeatMs);
    hbTimer.unref();
    req.on("close", sub.cleanup);
    res.on("close", sub.cleanup);
  });
  route("GET", "/v2/research/topics/:topic/runs/:op/recovery-status", true, ({ res, auth, params }) => {
    const data = dataFor(auth.accountId);
    send(res, 200, recoveryStatus(data, getRun(data, getTopic(data, params.topic).topic_id, params.op)));
  });
  route("POST", "/v2/research/topics/:topic/runs/:op/continue", true, (ctx) => continueRun(ctx, false));
  route("POST", "/v2/research/topics/:topic/runs/:op/recover", true, (ctx) => continueRun(ctx, true));
  route("POST", "/v1/operations/:op/cancel", true, ({ res, auth, params, body }) => {
    const data = dataFor(auth.accountId);
    if (!isRecord(body) || typeof body.cancel_token !== "string") throw invalid("Field required.", "body/cancel_token");
    const run = data.runs.get(params.op);
    if (!run) throw notFound("Operation");
    if (body.cancel_token !== run.operation.cancel_token) throw new HttpError(403, "forbidden", "Cancel token is not valid for this operation.");
    // The run becomes `cancelled` on its next tick; terminal runs are returned unchanged.
    if (isActive(run)) run.cancel_requested = true;
    send(res, 200, operationResponse(run));
  });

  // Versions, sources and exports
  route("GET", "/v2/research/topics/:topic/versions/:version/sources", true, ({ res, auth, params }) => {
    const data = dataFor(auth.accountId);
    const topic = getTopic(data, params.topic);
    send(res, 200, versionSources(data, topic, getVersion(topic, params.version)));
  });
  route("GET", "/v2/research/topics/:topic/versions/:version/sources/:source/text", true, ({ res, auth, params }) => {
    const data = dataFor(auth.accountId);
    const topic = getTopic(data, params.topic);
    const version = getVersion(topic, params.version);
    if (!version.source_ids.includes(params.source)) throw notFound("Research source");
    const source = data.sources.get(params.source);
    if (!source) throw notFound("Research source");
    if (!textAvailable(topic, source)) throw new HttpError(403, "invalid_request", "Source text is not available for this research version.");
    send(res, 200, { source_id: source.source_id, text: source.text, content_sha256: source.content_sha256 });
  });
  route("GET", "/v2/research/topics/:topic/versions/:version/export", true, ({ res, auth, params }) => {
    const data = dataFor(auth.accountId);
    const topic = getTopic(data, params.topic);
    const version = getVersion(topic, params.version);
    const content = ensureContent(version.content);
    const ids = new Set([...topic.request.source_ids, ...version.source_ids]);
    content.sources.forEach((row) => typeof row.source_id === "string" && ids.add(row.source_id));
    content.facts.forEach((fact) => ids.add(fact.source_id));
    content.claims.forEach((claim) => (claim.citations ?? []).forEach((c) => ids.add(c.source_id)));
    content.charts.forEach((chart) => {
      (chart.source_ids ?? []).forEach((id) => ids.add(id));
      (chart.series ?? []).forEach((s) => (s.points ?? []).forEach((p) => p?.provenance?.source_id && ids.add(p.provenance.source_id)));
    });
    content.turns.forEach((turn) => (turn.read_ranges ?? []).forEach((r) => ids.add(r.source_id)));
    const provenance = [];
    for (const id of [...ids].sort()) {
      const source = data.sources.get(id);
      if (!source) throw new HttpError(403, "invalid_request", "A research source is unavailable for export.");
      if (!source.permissions.export) throw new HttpError(403, "invalid_request", "A research source is not permitted for export.");
      provenance.push({ ...sourceRow(source), verification: "user_declared_not_verified" });
    }
    send(res, 200, {
      calculation_disclosure: CALCULATION_DISCLOSURE,
      disclosure: FULL_EXPORT_DISCLOSURE,
      format: "research_json",
      generated_at: iso(new Date()),
      request: topic.request,
      schema_version: "1",
      source_provenance: provenance,
      topic_id: topic.topic_id,
      version,
    });
  });
  route("GET", "/v2/research/topics/:topic/versions/:version/export/metadata", true, ({ res, auth, params }) => {
    const data = dataFor(auth.accountId);
    const topic = getTopic(data, params.topic);
    const version = getVersion(topic, params.version);
    const content = ensureContent(version.content);
    send(res, 200, {
      as_of: topic.request.as_of ?? null,
      calculation_count: content.calculations.length,
      claim_count: content.claims.length,
      created_by: version.created_by,
      disclosure: METADATA_DISCLOSURE,
      fact_count: content.facts.length,
      format: "metadata_only",
      gap_count: content.gaps.length,
      generated_at: iso(new Date()),
      parent_version_id: version.parent_version_id ?? null,
      review: version.review,
      schema_version: "1",
      topic_id: topic.topic_id,
      tracked_source_count: new Set([...topic.request.source_ids, ...version.source_ids]).size,
      turn_count: content.turns.length,
      version_created_at: version.created_at,
      version_id: version.version_id,
    });
  });

  // ---- Sources ----------------------------------------------------------------------

  const requirePermissionStore = (permissions, message = "Source storage permission is required.") => {
    if (!permissions.store) throw new HttpError(422, "invalid_request", message);
  };

  route("POST", "/v2/research/sources", true, ({ res, auth, body }) => {
    const data = dataFor(auth.accountId);
    strictKeys(body, ["title", "text", "url", "kind", "is_simulated", "published_at", "event_at", "revised_at", "permissions"]);
    const title = readTitle(body);
    const text = readText(body);
    const url = readOptionalUrl(body);
    if (body.kind !== undefined && !["user_supplied", "official", "market_data"].includes(body.kind)) {
      throw invalid("Input should be 'user_supplied', 'official' or 'market_data'.", "body/kind");
    }
    const fields = {
      title,
      text,
      url,
      kind: body.kind ?? "user_supplied",
      is_simulated: readBool(body, "is_simulated"),
      ...readDates(body),
      permissions: readPermissions(body.permissions),
    };
    requirePermissionStore(fields.permissions);
    send(res, 201, visibleSource(storeSource(data, auth.accountId, { ...fields, retrieval_method: "user_supplied" })));
  });

  route("POST", "/v2/research/sources/pdf", true, ({ res, auth, body }) => {
    const data = dataFor(auth.accountId);
    strictKeys(body, ["title", "pdf_base64", "text_order", "is_simulated", "published_at", "event_at", "revised_at", "permissions"]);
    const title = readTitle(body);
    const bytes = decodeBase64(body.pdf_base64, "body/pdf_base64");
    if (body.text_order !== undefined && body.text_order !== "layout" && body.text_order !== "reading") {
      throw invalid("Input should be 'layout' or 'reading'.", "body/text_order");
    }
    const textOrder = body.text_order ?? "layout";
    const isSimulated = readBool(body, "is_simulated");
    const dates = readDates(body);
    const permissions = readPermissions(body.permissions);
    // Magic title: the extractor is busy or timed out.
    if (title.includes("繁忙")) {
      throw new HttpError(503, "internal_error", "PDF extraction is unavailable or timed out.", { retryable: true });
    }
    if (!bytes || bytes.length > MAX_ORIGINAL_BYTES || !bytes.subarray(0, 5).equals(Buffer.from("%PDF-"))) {
      throw new HttpError(422, "invalid_request", "PDF is invalid, too large, encrypted, or has no extractable text.");
    }
    requirePermissionStore(permissions);
    const spacing = title.includes("间距");
    const source = storeSource(data, auth.accountId, {
      title,
      text: fictionalPdfText(title, textOrder, { joined: spacing }),
      is_simulated: isSimulated,
      ...dates,
      permissions,
      retrieval_method: "user_pdf",
      pdf_text_order: textOrder,
      response_sha256: bytesSha256(bytes),
      response_content_type: "application/pdf",
      original_bytes: bytes,
      pdf_text_spacing_warning: spacing,
    });
    send(res, 201, visibleSource(source, { withText: false }));
  });

  route("POST", "/v2/research/sources/html", true, ({ res, auth, body }) => {
    const data = dataFor(auth.accountId);
    strictKeys(body, ["title", "html_base64", "is_simulated", "published_at", "event_at", "revised_at", "permissions"]);
    const title = readTitle(body);
    const bytes = decodeBase64(body.html_base64, "body/html_base64");
    const isSimulated = readBool(body, "is_simulated");
    const dates = readDates(body);
    const permissions = readPermissions(body.permissions);
    const unusable = () => new HttpError(422, "invalid_request", "HTML is invalid, too large, not UTF-8, or has no extractable text.");
    if (!bytes || bytes.length < 1 || bytes.length > MAX_ORIGINAL_BYTES || !permissions.store) throw unusable();
    let text;
    try {
      text = extractHtmlText(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    } catch {
      throw unusable();
    }
    if (!text || text.length > 2_000_000) throw unusable();
    const source = storeSource(data, auth.accountId, {
      title,
      text,
      is_simulated: isSimulated,
      ...dates,
      permissions,
      retrieval_method: "user_html",
      response_sha256: bytesSha256(bytes),
      response_content_type: "text/html",
      original_bytes: bytes,
    });
    send(res, 201, visibleSource(source, { withText: false }));
  });

  route("POST", "/v2/research/sources/fetch", true, ({ res, auth, body }) => {
    const data = dataFor(auth.accountId);
    strictKeys(body, ["url", "title", "permissions", "is_simulated", "published_at", "event_at", "revised_at"]);
    const url = parseUrl(body.url);
    if (url.protocol !== "https:" || !ALLOWED_FETCH_HOSTS.has(url.hostname) || url.username || url.password ||
        (url.port !== "" && url.port !== "443") || url.hash || body.url.includes("#")) {
      throw invalid("Only supported official HTTPS sources are available.", "body/url");
    }
    const isSec = url.hostname === "www.sec.gov";
    if (isSec && (url.search || body.url.includes("?") || !SEC_FILING_PATH.test(url.pathname))) {
      throw invalid("Only direct SEC filing HTML paths are available.", "body/url");
    }
    const title = readTitle(body);
    const isSimulated = readBool(body, "is_simulated");
    const dates = readDates(body);
    const permissions = readPermissions(body.permissions);
    requirePermissionStore(permissions);
    // Magic path: the upstream source is unavailable or timed out.
    if (url.pathname.includes("timeout")) {
      throw new HttpError(503, "internal_error", "Source unavailable or timed out.", { retryable: true });
    }
    const text = isSec
      ? `Fictional SEC filing extract from ${url.href}. Item 7. Management's discussion (fictional). Total revenue 12,345 (fictional).\nCapital expenditure 987 (fictional).`
      : `Fictional official page extract from ${url.href}. Headline indicator 2.5 percent (fictional).\nThis page text is invented for QA.`;
    const original = Buffer.from(`<html><body><p>${text.replace(/\n/g, "</p><p>")}</p></body></html>`, "utf8");
    const source = storeSource(data, auth.accountId, {
      title,
      text,
      url: url.href,
      kind: "official",
      is_simulated: isSimulated,
      ...dates,
      permissions,
      retrieval_method: isSec ? "sec_filing_html" : "official_html",
      response_sha256: bytesSha256(original),
      response_content_type: "text/html",
      original_bytes: original,
    });
    send(res, 201, visibleSource(source, { withText: !isSec }));
  });

  route("POST", "/v2/research/identities/sec-search", true, ({ res, body }) => {
    strictKeys(body, ["query"]);
    if (body.query === undefined) throw invalid("Field required.", "body/query");
    if (typeof body.query !== "string") throw invalid("Input should be a valid string.", "body/query");
    const raw = body.query.trim();
    if (raw.length < 1 || raw.length > 100) throw invalid("String should have at least 1 and at most 100 characters.", "body/query");
    // Magic query: the SEC contact is not configured on this deployment.
    if (raw === "未配置") throw new HttpError(503, "internal_error", "SEC source contact is not configured.", { retryable: false });
    const query = raw.toLowerCase();
    const matches = [];
    for (const item of SEC_INDEX) {
      const ticker = item.ticker.toLowerCase();
      const name = item.name.toLowerCase();
      if (!ticker.includes(query) && !name.includes(query) && query !== String(item.cik)) continue;
      const rank = query === ticker || query === String(item.cik) ? 0 : name.startsWith(query) ? 1 : 2;
      matches.push({ rank, ticker, cik: item.cik, row: { cik: String(item.cik).padStart(10, "0"), ticker: item.ticker, name: item.name, exchange: null } });
    }
    matches.sort((a, b) => a.rank - b.rank || (a.ticker < b.ticker ? -1 : a.ticker > b.ticker ? 1 : 0) || a.cik - b.cik);
    send(res, 200, {
      candidates: matches.slice(0, 10).map((match) => match.row),
      source_url: "https://www.sec.gov/files/company_tickers.json",
      retrieved_at: new Date().toISOString(),
      response_sha256: sha256(JSON.stringify(SEC_INDEX)),
      limitation: SEC_LIMITATION,
    });
  });

  route("POST", "/v2/research/sources/sec-concept", true, ({ res, auth, body }) => {
    const data = dataFor(auth.accountId);
    strictKeys(body, ["cik", "taxonomy", "tag", "unit", "filing_before", "max_records", "permissions"]);
    if (body.cik === undefined) throw invalid("Field required.", "body/cik");
    if (typeof body.cik !== "string" || !/^\d{1,10}$/.test(body.cik.trim())) throw invalid("String should match pattern '^\\d{1,10}$'.", "body/cik");
    if (Number(body.cik.trim()) === 0) throw invalid("CIK must be nonzero.", "body/cik");
    const cik = body.cik.trim().padStart(10, "0");
    if (body.taxonomy !== undefined && !["us-gaap", "ifrs-full", "dei"].includes(body.taxonomy)) {
      throw invalid("Input should be 'us-gaap', 'ifrs-full' or 'dei'.", "body/taxonomy");
    }
    const taxonomy = body.taxonomy ?? "us-gaap";
    if (body.tag === undefined) throw invalid("Field required.", "body/tag");
    if (typeof body.tag !== "string" || !/^[A-Za-z][A-Za-z0-9_]*$/.test(body.tag.trim()) || body.tag.trim().length > 128) {
      throw invalid("String should match pattern '^[A-Za-z][A-Za-z0-9_]*$' and have at most 128 characters.", "body/tag");
    }
    const tag = body.tag.trim();
    let unit = "USD";
    if (body.unit !== undefined && body.unit !== null) {
      if (typeof body.unit !== "string" || !/^[A-Za-z0-9-]+$/.test(body.unit.trim()) || body.unit.trim().length > 64) {
        throw invalid("String should match pattern '^[A-Za-z0-9-]+$' and have at most 64 characters.", "body/unit");
      }
      unit = body.unit.trim();
    }
    let filingBefore = null;
    if (body.filing_before !== undefined && body.filing_before !== null) {
      const value = typeof body.filing_before === "string" ? body.filing_before.trim() : "";
      const parsed = /^\d{4}-\d{2}-\d{2}$/.test(value) ? new Date(`${value}T00:00:00Z`) : null;
      if (!parsed || Number.isNaN(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value) {
        throw invalid("Input should be a valid date (YYYY-MM-DD).", "body/filing_before");
      }
      filingBefore = value;
    }
    const maxRecords = body.max_records ?? 30;
    if (!Number.isInteger(maxRecords) || maxRecords < 1 || maxRecords > 100) throw invalid("Input should be an integer between 1 and 100.", "body/max_records");
    const permissions = readPermissions(body.permissions);
    const unusable = () => new HttpError(422, "invalid_request", "SEC source data could not be used.");
    if (!permissions.store || cik === "0009999999") throw unusable(); // 0009999999 is the unknown-company magic CIK
    const lines = secConceptRecords(cik, tag, unit, filingBefore, maxRecords);
    if (lines.length === 0) throw unusable();
    const title = `SEC companyconcept ${taxonomy}:${tag} CIK ${cik}`;
    const text = `${title} (fictional QA data)\nUnit: ${unit}\n${lines.join("\n")}`;
    const source = storeSource(data, auth.accountId, {
      title,
      text,
      url: `https://data.sec.gov/api/xbrl/companyconcept/CIK${cik}/${taxonomy}/${tag}.json`,
      kind: "official",
      permissions,
      retrieval_method: "sec_companyconcept",
      response_sha256: sha256(text),
      response_content_type: "application/json",
    });
    send(res, 201, visibleSource(source));
  });

  route("GET", "/v2/research/sources/:source", true, ({ res, auth, params }) =>
    send(res, 200, visibleSource(getSource(dataFor(auth.accountId), params.source))));

  route("GET", "/v2/research/sources/:source/original", true, ({ res, auth, params }) => {
    const source = getSource(dataFor(auth.accountId), params.source);
    if (!source.original_bytes || !source.permissions.display || !source.permissions.export) {
      throw new HttpError(403, "invalid_request", "The original document is not available for download.");
    }
    const html = HTML_METHODS.has(source.retrieval_method);
    res.writeHead(200, {
      "Content-Type": html ? "application/octet-stream" : "application/pdf",
      "Content-Length": source.original_bytes.length,
      "Content-Disposition": html ? 'attachment; filename="source.html"' : 'attachment; filename="source.pdf"',
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
    });
    res.end(source.original_bytes);
  });

  route("POST", "/v2/research/sources/:source/reextract", true, ({ req, res, auth, params, body }) => {
    const data = dataFor(auth.accountId);
    strictKeys(body, ["text_order"]);
    if (body.text_order === undefined) throw invalid("Field required.", "body/text_order");
    if (body.text_order !== "layout" && body.text_order !== "reading") throw invalid("Input should be 'layout' or 'reading'.", "body/text_order");
    const rawKey = req.headers["idempotency-key"];
    if (rawKey !== undefined && (typeof rawKey !== "string" || rawKey.length < 8 || rawKey.length > 128)) {
      throw invalid("String should have at least 8 and at most 128 characters.", "header/Idempotency-Key");
    }
    const original = getSource(data, params.source);
    if (original.retrieval_method !== "user_pdf" || !original.original_bytes) {
      throw new HttpError(422, "invalid_request", "The retained original PDF is unavailable.");
    }
    const title = original.title.slice(0, 470) + (body.text_order === "layout" ? " — layout" : " — reading");
    const stableId = rawKey === undefined ? undefined : `source_${sha256(`pdf-reextract-v1\0${auth.accountId}\0${rawKey}`).slice(0, 32)}`;
    const existing = stableId ? data.sources.get(stableId) : undefined;
    if (existing) {
      if (existing.derived_from_source_id !== original.source_id || existing.pdf_text_order !== body.text_order) {
        throw new HttpError(409, "idempotency_conflict", "This key was used for a different PDF extraction.");
      }
      return send(res, 200, visibleSource(existing, { withText: false }), { "Idempotency-Replayed": "true" });
    }
    const source = storeSource(data, auth.accountId, {
      source_id: stableId,
      title,
      text: fictionalPdfText(title, body.text_order, { normalized: true }),
      url: original.url,
      kind: original.kind,
      is_simulated: original.is_simulated,
      published_at: original.published_at,
      event_at: original.event_at,
      revised_at: original.revised_at,
      permissions: { ...original.permissions },
      retrieval_method: "user_pdf",
      pdf_text_order: body.text_order,
      derived_from_source_id: original.source_id,
      response_sha256: bytesSha256(original.original_bytes),
      response_content_type: "application/pdf",
      original_bytes: original.original_bytes,
    });
    send(res, 201, visibleSource(source, { withText: false }), { "Idempotency-Replayed": "false" });
  });

  route("POST", "/v2/research/topics/:topic/sources", true, ({ res, auth, params, body }) => {
    const data = dataFor(auth.accountId);
    strictKeys(body, ["parent_version_id", "source_ids"]);
    if (body.parent_version_id === undefined) throw invalid("Field required.", "body/parent_version_id");
    const parent = typeof body.parent_version_id === "string" ? body.parent_version_id.trim() : "";
    if (parent.length < 1 || parent.length > 100) throw invalid("String should have at least 1 and at most 100 characters.", "body/parent_version_id");
    if (body.source_ids === undefined) throw invalid("Field required.", "body/source_ids");
    if (!isStringArray(body.source_ids) || body.source_ids.length < 1) throw invalid("List should have at least 1 item.", "body/source_ids");
    const ids = body.source_ids.map((id) => id.trim());
    if (ids.some((id) => !id)) throw invalid("Source IDs must not be blank.", "body/source_ids");
    const missing = () => notFound("Research topic or source");
    const topic = data.topics.get(params.topic);
    if (!topic || ids.some((id) => !data.sources.has(id))) throw missing();
    const previous = latestOf(topic);
    if (previous.version_id !== parent) throw new HttpError(409, "lifecycle_conflict", "Research topic has changed; reload before saving.");
    const now = iso(new Date());
    topic.versions.push({
      version_id: `version_${randomBytes(16).toString("hex")}`,
      parent_version_id: parent,
      created_at: now,
      created_by: "user",
      review: "draft",
      note: previous.note,
      content: structuredClone(previous.content ?? {}),
      source_ids: [...new Set([...previous.source_ids, ...ids])],
    });
    topic.updated_at = now;
    send(res, 201, topic);
  });

  // ---- Version reads and user edits ---------------------------------------------------------

  route("GET", "/v2/research/topics/:topic/versions/:version", true, ({ res, auth, params }) => {
    const topic = getTopic(dataFor(auth.accountId), params.topic);
    send(res, 200, getVersion(topic, params.version));
  });

  /** Appends a new user version (parent = latest) and returns the full topic. */
  function pushUserVersion(topic, previous, { content, review = "draft", note }) {
    const now = iso(new Date());
    topic.versions.push({
      version_id: `version_${randomBytes(16).toString("hex")}`,
      parent_version_id: previous.version_id,
      created_at: now,
      created_by: "user",
      review,
      note,
      content,
      source_ids: [...previous.source_ids],
    });
    topic.updated_at = now;
  }

  const unprocessable = (message) => new HttpError(422, "invalid_request", message);

  route("POST", "/v2/research/topics/:topic/versions", true, ({ res, auth, params, body }) => {
    const data = dataFor(auth.accountId);
    validateVersionInput(body);
    const topic = getTopic(data, params.topic);
    const previous = latestOf(topic);
    if (previous.version_id !== body.parent_version_id) {
      throw new HttpError(409, "lifecycle_conflict", "Research topic has changed; reload before saving.");
    }
    const before = ensureContent(previous.content);
    const incoming = body.content;
    for (const key of ["charts", "turns", "sources", "source_candidates", "gaps"]) {
      if (!same(incoming[key] ?? [], before[key])) throw unprocessable("Server-produced research records cannot be edited.");
    }

    const allowedSources = new Set(previous.source_ids);
    const checkSpan = (sourceId, quotation, start, end, message) => {
      const source = allowedSources.has(sourceId) ? data.sources.get(sourceId) : null;
      if (!source || !textAvailable(topic, source)) throw unprocessable(message);
      if (!quotation.trim() || end > source.text.length || source.text.slice(start, end) !== quotation) throw unprocessable(message);
    };

    // Claims: unchanged ones pass untouched; changed or new ones are rechecked and marked.
    const claimsIn = (incoming.claims ?? []).map(readClaim);
    if (new Set(claimsIn.map((claim) => claim.claim_id)).size !== claimsIn.length) throw unprocessable("Research findings or their citations are invalid.");
    const parentClaims = new Map(before.claims.map((claim) => [claim.claim_id, claim]));
    const idMap = new Map();
    const claims = [];
    const changedClaims = [];
    for (const claim of claimsIn) {
      const original = parentClaims.get(claim.claim_id);
      if (original && same(claim, claimIdentity(original))) {
        claims.push(structuredClone(original));
        continue;
      }
      for (const cite of claim.citations) {
        checkSpan(cite.source_id, cite.quotation, cite.location.character_start, cite.location.character_end,
          "Citation must match an attached, displayable, time-eligible source.");
      }
      if (!original) idMap.set(claim.claim_id, `claim-${randomBytes(16).toString("hex")}`);
      const saved = { ...claim, claim_id: idMap.get(claim.claim_id) ?? claim.claim_id, turn_id: original ? original.turn_id ?? null : null, edited_by_user: true };
      claims.push(saved);
      changedClaims.push(saved);
    }
    const finalIds = new Set(claims.map((claim) => claim.claim_id));
    for (const claim of changedClaims) {
      claim.challenges_claim_ids = claim.challenges_claim_ids.map((id) => idMap.get(id) ?? id);
      if (claim.challenges_claim_ids.some((id) => !finalIds.has(id) || id === claim.claim_id)) {
        throw unprocessable("A challenged finding must be another existing finding.");
      }
    }

    // Facts: an unchanged fact (ignoring computed flags) is kept; edits are rechecked and downgraded.
    const factsIn = (incoming.facts ?? []).map(readFact);
    if (new Set(factsIn.map((fact) => fact.fact_id.trim())).size !== factsIn.length) throw unprocessable("Research fact identifiers must be unique.");
    const parentFacts = new Map(before.facts.map((fact) => [fact.fact_id, fact]));
    const facts = [];
    for (const fact of factsIn) {
      const original = parentFacts.get(fact.fact_id);
      if (original && same(factIdentity(fact), factIdentity(original))) {
        facts.push(structuredClone(original));
        continue;
      }
      try {
        parseDecimal(fact.value);
      } catch {
        throw unprocessable("Research fact value, unit, currency, or period is invalid.");
      }
      if (fact.label.length > 300 || fact.quotation.length > 1000 || fact.source_id.length > 100) throw unprocessable("Research fact provenance is invalid.");
      if (fact.location.character_start !== fact.character_start || fact.location.character_end !== fact.character_end) {
        throw unprocessable("Research fact location is inconsistent.");
      }
      checkSpan(fact.source_id, fact.quotation, fact.character_start, fact.character_end,
        "Research fact must use an attached, displayable, time-eligible real source.");
      const inQuotation = fact.quotation.replace(/,/g, "").includes(fact.value.replace(/,/g, ""));
      facts.push({
        ...fact,
        turn_id: original ? original.turn_id ?? null : null,
        verification: inQuotation ? "user_edited_not_independently_verified" : "user_edited_value_not_in_quotation",
      });
    }

    // Calculations: an unchanged one (ignoring computed fields) is kept; edits become user-authored.
    const calcsIn = (incoming.calculations ?? []).map(readCalculation);
    if (new Set(calcsIn.map((calc) => calc.calculation_id)).size !== calcsIn.length) throw unprocessable("Research calculation identifiers must be unique.");
    const parentCalcs = new Map(before.calculations.map((calc) => [calc.calculation_id, calc]));
    const calculations = calcsIn.map((calc) => {
      const original = parentCalcs.get(calc.calculation_id);
      if (original && same(calculationIdentity(calc), calculationIdentity(original))) {
        const kept = structuredClone(original);
        // Fail closed: a status the mock cannot re-derive stays unverifiable.
        kept.input_status = original.input_status === "unverifiable" ? "unverifiable" : calcInputStatus(kept, facts);
        return kept;
      }
      const edited = {
        ...calc,
        turn_id: original ? original.turn_id ?? null : null,
        legacy_unit_warning: null,
        authorship: "user",
        verification: "user_edited_not_independently_verified",
      };
      edited.input_status = calcInputStatus(edited, facts);
      edited.result_status = calcResultStatus(edited);
      return edited;
    });

    const content = { ...before, summary: incoming.summary ?? "", claims, facts, calculations };
    pushUserVersion(topic, previous, { content, review: body.review ?? "draft", note: body.note ?? "" });
    send(res, 201, topic);
  });

  // ---- Calculations -----------------------------------------------------------------------

  /** Mirrors _research_calculation_inputs: version-owned, unambiguous, quotation-backed facts. */
  function calculationInputs(content, factIds) {
    return factIds.map((id) => {
      const matches = content.facts.filter((fact) => fact.fact_id === id);
      if (matches.length !== 1) throw unprocessable("Calculation input facts are missing or ambiguous.");
      const [fact] = matches;
      if (fact.verification === "user_edited_value_not_in_quotation") throw unprocessable("The fact's source quotation requires review before calculation.");
      if (isPercentUnit(String(fact.unit)) && (fact.currency ?? null) !== null) throw unprocessable("Percentage currency context requires review before calculation.");
      return { fact_id: fact.fact_id, value: fact.value, unit: fact.unit, currency: fact.currency ?? null, period: fact.period };
    });
  }

  function runCalculation(formula, content, factIds, places, options = {}) {
    const facts = calculationInputs(content, factIds);
    try {
      return calculateEngine(formula, facts, places, options);
    } catch (error) {
      if (error instanceof CalcError) throw unprocessable("Calculation cannot be performed with the current facts.");
      throw error;
    }
  }

  function calculationTopic(data, topicId, parent) {
    const topic = getTopic(data, topicId);
    const previous = latestOf(topic);
    if (previous.version_id !== parent) throw new HttpError(409, "lifecycle_conflict", "Research topic has changed; reload before calculating.");
    return { topic, previous, content: ensureContent(previous.content) };
  }

  route("POST", "/v2/research/topics/:topic/calculations", true, ({ res, auth, params, body }) => {
    const request = validateCalculationRequest(body, { recalculate: false });
    const { topic, previous, content } = calculationTopic(dataFor(auth.accountId), params.topic, request.parent);
    if (content.calculations.length >= 2000) throw unprocessable("The research calculation limit has been reached.");
    const result = runCalculation(request.formula, content, [request.first, request.second], request.places);
    content.calculations.push({
      calculation_id: `calc-${randomBytes(16).toString("hex")}`,
      ...result,
      input_status: "matched",
      result_status: "matched",
      turn_id: null,
      legacy_unit_warning: null,
      authorship: "calculation_engine",
    });
    pushUserVersion(topic, previous, { content, note: previous.note });
    send(res, 201, topic);
  });

  route("POST", "/v2/research/topics/:topic/calculations/recalculate", true, ({ res, auth, params, body }) => {
    const request = validateCalculationRequest(body, { recalculate: true });
    const { topic, previous, content } = calculationTopic(dataFor(auth.accountId), params.topic, request.parent);
    if (request.index >= content.calculations.length) throw unprocessable("Calculation does not exist in this version.");
    const old = content.calculations[request.index];
    const validInputCount = Array.isArray(old.inputs) && (
      BINARY_FORMULAS.includes(old.formula_id)
        ? old.inputs.length === 2
        : old.inputs.length >= 2 && old.inputs.length <= 50
    );
    if (!FORMULAS.includes(old.formula_id) || !["1", "2"].includes(old.formula_version) || !validInputCount) {
      throw unprocessable("Calculation inputs or formula version are unavailable.");
    }
    const result = runCalculation(
      old.formula_id,
      content,
      old.inputs.map((item) => item.fact_id),
      old.decimal_places,
      {
        periods: old.formula_id === "cagr" ? old.periods ?? null : null,
        weights: old.formula_id === "weighted_average" ? old.weights ?? null : null,
      },
    );
    content.calculations[request.index] = {
      calculation_id: old.calculation_id,
      ...result,
      input_status: "matched",
      result_status: "matched",
      turn_id: old.turn_id ?? null,
      legacy_unit_warning: null,
      authorship: "calculation_engine",
    };
    pushUserVersion(topic, previous, { content, note: previous.note });
    send(res, 201, topic);
  });


  // Account summary, model and market-data settings (bearer auth, `Cache-Control: no-store` via send()).
  // Backend GET /v2/research/capabilities (research_source_catalog.source_capabilities). The mock
  // implements SEC endpoints, so SEC is reported available; set MOCK_SEC_AVAILABLE=0 to test the gate.
  route("GET", "/v2/research/capabilities", true, ({ res }) => {
    const sec = process.env.MOCK_SEC_AVAILABLE !== "0";
    send(res, 200, {
      schema_version: "v1", search_available: true, sec_available: sec, historical_prices_available: false,
      sources: [
        { name: "project_search", output_type: "leads", permission: "search", configured: true, markets: [], languages: [], historical: false, hosts: [], coverage: "unknown" },
        { name: "official_html_pdf", output_type: "document", permission: "official_retrieval", configured: true, markets: [], languages: [], historical: false, hosts: ["www.bea.gov", "www.federalreserve.gov"], coverage: "restricted_hosts" },
        { name: "sec_filing", output_type: "document", permission: "official_retrieval", configured: sec, markets: ["US"], languages: [], historical: false, hosts: ["www.sec.gov"], coverage: "restricted_paths" },
        { name: "sec_company_concept", output_type: "structured_data", permission: "official_retrieval", configured: sec, markets: ["US"], languages: [], historical: false, hosts: ["data.sec.gov"], coverage: "company_concept_only" },
      ],
    });
  });

  route("GET", "/v1/account", true, ({ res, auth }) => {
    const data = dataFor(auth.accountId);
    const account = accounts.get(auth.accountId);
    const consumed = data.billing.consumed;
    send(res, 200, {
      account: { account_id: auth.accountId, email: account.email },
      plan: "free",
      entitlement_status: "active",
      product_id: null,
      base_plan_id: null,
      quota_limit: QUOTA_LIMIT,
      quota_consumed: consumed,
      quota_reserved: 0,
      quota_remaining: Math.max(0, QUOTA_LIMIT - consumed),
      quota_resets_at: nextMonthStart(new Date()),
      provider_model: PLATFORM_MODEL,
      // Backend /v1/account: the model platform research runs on (billing label may differ).
      research_model: "research-model-example",
    });
  });
  route("GET", "/v1/account/model-config", true, ({ res, auth }) =>
    send(res, 200, modelSnapshot(dataFor(auth.accountId).modelConfig)));
  route("PUT", "/v1/account/model-config", true, ({ res, auth, body }) => {
    const data = dataFor(auth.accountId);
    const request = readModelConfigUpdate(body);
    data.modelConfig = applyModelUpdate(data.modelConfig, request, new Date());
    send(res, 200, modelSnapshot(data.modelConfig));
  });
  route("DELETE", "/v1/account/model-config", true, ({ res, auth }) => {
    const data = dataFor(auth.accountId);
    data.modelConfig = null;
    send(res, 200, modelSnapshot(null));
  });
  route("POST", "/v1/account/model-config/test", true, ({ res, auth }) => {
    const config = dataFor(auth.accountId).modelConfig;
    let status = "unavailable";
    if (config?.mode === "personal" && (config.protocol === "openai_chat_completions" || config.messages_options)) {
      // Simulated probe, no network: hosts or models named "down" are unreachable.
      const down = new URL(config.base_url).hostname.toLowerCase().includes("down") || config.model.toLowerCase().includes("down");
      status = down ? "unavailable" : "ready";
      if (!down) config.research_tools_verified_at ??= iso(new Date());
    }
    send(res, 200, { status });
  });
  route("GET", "/v1/account/market-data-config", true, ({ res, auth }) =>
    send(res, 200, marketSnapshot(dataFor(auth.accountId).marketConfig)));
  route("PUT", "/v1/account/market-data-config", true, ({ res, auth, body }) => {
    const data = dataFor(auth.accountId);
    const request = readMarketDataUpdate(body);
    data.marketConfig = applyMarketUpdate(data.marketConfig, request, new Date());
    send(res, 200, marketSnapshot(data.marketConfig));
  });
  route("POST", "/v1/account/market-data-config/test", true, ({ res, auth, body }) => {
    exactKeys(body, ["provider_id"], "body");
    if (body.provider_id === undefined) throw invalid("Field required.", "body/provider_id");
    const providerId = readEnum(body.provider_id, MARKET_PROVIDER_ORDER, "body/provider_id");
    send(res, 200, { provider_id: providerId, status: simulateMarketProbe(dataFor(auth.accountId).marketConfig, providerId) });
  });

  function resolve(method, segments) {
    for (const candidate of routes) {
      if (candidate.method !== method || candidate.parts.length !== segments.length) continue;
      const params = {};
      let matched = true;
      for (let i = 0; i < segments.length; i += 1) {
        const part = candidate.parts[i];
        if (part.startsWith(":")) params[part.slice(1)] = segments[i];
        else if (part !== segments[i]) {
          matched = false;
          break;
        }
      }
      if (matched) return { route: candidate, params };
    }
    return null;
  }

  const server = http.createServer(async (req, res) => {
    try {
      const url = new URL(req.url ?? "/", "http://127.0.0.1");
      let segments;
      try {
        segments = url.pathname.split("/").filter(Boolean).map(decodeURIComponent);
      } catch {
        throw notFound("Route");
      }
      const match = resolve(req.method ?? "GET", segments);
      if (!match) throw notFound("Route");
      // Backend security.py boundary: POST, PUT and PATCH must be JSON (415 before auth); main.py
      // only adds the JSON type for the body-less model probe. The UI sends {} for body-less writes.
      if (["POST", "PUT", "PATCH"].includes(req.method) && url.pathname !== "/v1/account/model-config/test") {
        const type = String(req.headers["content-type"] ?? "").split(";")[0].trim().toLowerCase();
        if (type !== "application/json") throw new HttpError(415, "unsupported_media_type", "JSON requests are required.");
      }
      let auth = null;
      if (match.route.auth) {
        auth = bearer(req);
        if (!auth) throw new HttpError(401, "unauthorized", "A valid bearer token is required.");
      }
      const body = req.method === "POST" || req.method === "PUT" ? await readJson(req) : undefined;
      await match.route.handler({ req, res, auth, params: match.params, query: url.searchParams, body });
    } catch (error) {
      if (error instanceof HttpError) sendError(res, error);
      else {
        console.error(error);
        sendError(res, new HttpError(500, "internal_error", "Internal mock error.", { retryable: true }));
      }
    }
  });

  const timer = setInterval(() => {
    for (const account of accounts.values()) {
      if (!account.data) continue;
      for (const run of account.data.runs.values()) {
        if (isActive(run)) advance(account.data, run, sseBroker);
      }
    }
  }, tickMs);
  timer.unref();
  server.on("close", () => {
    clearInterval(timer);
    for (const set of [...sseSubscribers.values()]) {
      for (const sub of [...set]) {
        sub.cleanup();
        if (!sub.res.writableEnded) sub.res.end();
      }
    }
  });
  // Test hook: inspect or mutate in-memory state (accountId -> { email, created_at, data }).
  server.mockState = { accounts, sessions };
  return server;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const port = Number(process.env.MOCK_API_PORT ?? 8787);
  const server = createServer();
  server.listen(port, "127.0.0.1", () => {
    console.log(`mock-api listening on http://127.0.0.1:${port}`);
  });
}
