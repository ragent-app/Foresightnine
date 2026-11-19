// Dependency-free mock of the ForesightNine research API for local UI QA.
// All companies, sources and numbers are fictional. State is in memory only.
import http from "node:http";
import { createHash, randomBytes } from "node:crypto";
import { pathToFileURL } from "node:url";
import {
  PARTIAL_GAPS,
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
const DISCOVERY_STEP = { kind: "tool", name: "discover_sources", status: "completed", rationale: "搜索公开线索" };
const DISCOVERY_CANDIDATES = [
  { url: "https://www.sec.gov/Archives/edgar/data/1000001/000100000126000012/nwex-20260630.htm", title: "Northwind Example Corp 10-K (fictional)", published_at: "2026-08-12T00:00:00Z" },
  { url: "https://news.example.com/northwind-capex", title: "示例新闻：Northwind 资本开支", published_at: "2026-10-20T00:00:00Z" },
];

class HttpError extends Error {
  constructor(status, code, message, { retryable = false, details = [] } = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.retryable = retryable;
    this.details = details;
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
  });
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
  for (const key of ["calculations", "claims", "facts", "gaps", "source_candidates", "sources", "turns"]) {
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
      ? "本轮复核了年报摘录中的营业利润：2026 财年营业利润为 15,104 百万美元，上一财年为 12,980 百万美元，同比增长约 16.36%。\n\n结果来自对摘录的读取和确定性计算；金额单位与币种沿用摘录开头的说明，尚未与原始披露逐项核对。"
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
    review: "draft",
    content,
    source_ids: [...new Set([...sourceIds, ...(run.bodySourceIds ?? [])])],
    parent_version_id: previous.version_id,
    note: "",
  };
  topic.versions.push(version);
  topic.updated_at = version.created_at;
  run.version_id = version.version_id;
}

/** Advances one run by one step. */
function advance(data, run) {
  const now = new Date();
  if (run.cancel_requested) {
    touch(run, now, "cancelled");
    return;
  }
  if (run.operation.status === "pending") run.operation.status = "running";
  run.events.push(rawEvent(RUN_STEPS[run.cursor]));
  run.cursor += 1;
  const cursor = run.cursor;
  if (cursor === RUN_STEPS.length) {
    commitTurn(data, run, "completed", now);
    touch(run, now, "completed");
  } else if (run.variant === "partial" && cursor === 3) {
    commitTurn(data, run, "partial", now);
    touch(run, now, "failed");
  } else if (run.variant === "fail" && cursor === 2) {
    touch(run, now, "failed");
  } else {
    touch(run, now);
  }
}

function currentActivity(run) {
  if (run.operation.status !== "running") return null;
  const step = RUN_STEPS[run.cursor];
  if (!step) return null;
  if (step.kind === "model") return { kind: "model" };
  if (step.kind === "tool") return { kind: "tool", name: step.name };
  return null; // the submission step is not model or tool activity
}

const clip = (value, limit) => (typeof value === "string" ? value.slice(0, limit) : null);
const int = (value) => (Number.isInteger(value) ? value : null);

/** Mirrors research_progress.sanitize_event. */
function sanitizeEvent(event) {
  if (!isRecord(event)) return null;
  if (event.kind === "model") {
    const detail = isRecord(event.usage_detail) ? event.usage_detail : {};
    return {
      kind: "model",
      finish_reason: clip(event.finish_reason, 100),
      usage: int(event.usage),
      input_tokens: int(detail.input_tokens),
      output_tokens: int(detail.output_tokens),
      settlement: clip(event.settlement, 100),
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
    };
  }
  if (event.kind === "submission") return { kind: "submission", status: clip(event.status, 100) };
  return null;
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
    events,
    event_count: run.events.length,
    started_at: run.started_at,
    updated_at: run.operation.updated_at,
    version_id: run.version_id ?? null,
  };
}

// ---- Server -----------------------------------------------------------------------

export function createServer({ tickMs = 1500 } = {}) {
  /** accountId -> { email, created_at, data } */
  const accounts = new Map();
  /** token -> { accountId, expiresAt } */
  const sessions = new Map();
  /** scoped key -> { hash, operationId } */
  const idempotency = new Map();
  let runSeq = 1000;

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
      const data = { topics: new Map(), sources: new Map(), runs: new Map() };
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
        if (isActive(run)) advance(account.data, run);
      }
    }
  }, tickMs);
  timer.unref();
  server.on("close", () => clearInterval(timer));
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
