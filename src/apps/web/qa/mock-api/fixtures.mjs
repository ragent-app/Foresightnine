// Fictional QA seed data. Every company, source and number here is invented.
import { createHash } from "node:crypto";

const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

export const sha256 = (value) => createHash("sha256").update(value).digest("hex");

/** ISO-8601 with a trailing Z and no zero milliseconds (like the backend's JSON). */
export const iso = (date) => new Date(date).toISOString().replace(".000Z", "Z");

export const S1_ID = "src_northwind_fy26";
export const S2_ID = "src_harbor_shipments";
export const S3_ID = "src_atlas_restricted";
export const S4_ID = "src_atlas_simulated";
export const S5_ID = "src_northwind_deck";

export const S1_TEXT =
  "Northwind Holdings Annual Report FY2026 (illustrative excerpt). Amounts are presented in millions of U.S. dollars unless otherwise noted.\n\n" +
  "Year ended June 30, 2026: total revenue 48,612. Year ended June 30, 2025: total revenue 41,270. Revenue growth was driven primarily by cloud services and recurring subscriptions.\n\n" +
  "Year ended June 30, 2026: operating income 15,104. Year ended June 30, 2025: operating income 12,980. Operating expenses increased as the company expanded data-center capacity.\n\n" +
  "Management expects capacity investment to remain elevated in the next fiscal year.";

export const Q_REV26 = "Year ended June 30, 2026: total revenue 48,612.";
export const Q_REV25 = "Year ended June 30, 2025: total revenue 41,270.";
export const Q_DRIVER = "Revenue growth was driven primarily by cloud services and recurring subscriptions.";
export const Q_OPI26 = "Year ended June 30, 2026: operating income 15,104.";
export const Q_OPI25 = "Year ended June 30, 2025: operating income 12,980.";
export const Q_OPEX = "Operating expenses increased as the company expanded data-center capacity.";

/** Exact span of `quotation` inside `text`; offsets always come from indexOf. */
export function span(text, quotation) {
  const start = text.indexOf(quotation);
  if (start < 0) throw new Error(`quotation not found: ${quotation}`);
  return { character_start: start, character_end: start + quotation.length };
}

export function citation(text, quotation, sourceId = S1_ID) {
  return { source_id: sourceId, quotation, location: span(text, quotation) };
}

/** Paragraph 3 (operating income / expenses), used as the "fully read" range. */
export function operatingParagraphRange(text, sourceId = S1_ID) {
  const start = span(text, Q_OPI26).character_start;
  const end = span(text, Q_OPEX).character_end;
  return { source_id: sourceId, character_start: start, character_end: end };
}

export function revenueParagraphRange(text, sourceId = S1_ID) {
  const start = span(text, Q_REV26).character_start;
  const end = span(text, Q_DRIVER).character_end;
  return { source_id: sourceId, character_start: start, character_end: end };
}

/** Mirrors research_source_policy.summarize_sources (one row per analyzed source). */
export function summarizeSources(sources) {
  return sources.map((s) => ({
    source_id: s.source_id,
    title: s.title,
    url: s.url ?? null,
    event_at: s.event_at ?? null,
    published_at: s.published_at ?? null,
    obtained_at: s.obtained_at,
    revised_at: s.revised_at ?? null,
    content_sha256: s.content_sha256,
    is_simulated: s.is_simulated,
    initial_prompt_coverage: {
      method: "initial_prompt_ranges_v1",
      text_characters: s.text.length,
      selected_characters: null,
      status: "unknown",
    },
    pdf_text_order: s.pdf_text_order ?? null,
    derived_from_source_id: s.derived_from_source_id ?? null,
    verification: "user_declared_not_verified",
  }));
}

/** Sources that a run may read and summarize (same gates as source_eligibility). */
export function isEligibleForRun(source) {
  return !source.is_simulated && source.permissions.model_input && source.permissions.display;
}

// ---- Scripted run: one step per tick ---------------------------------------

export const RUN_STEPS = [
  { kind: "model", usage: 2195, input: 1980, output: 215, finish_reason: "tool_calls" },
  { kind: "tool", name: "read_sources", status: "read", rationale: "读取年报摘录中与问题相关的段落" },
  { kind: "model", usage: 3764, input: 2428, output: 1336, finish_reason: "tool_calls" },
  { kind: "tool", name: "record_facts", status: "completed", rationale: "记录两期营业利润" },
  { kind: "tool", name: "calculate_financials", status: "completed", rationale: "计算两期营业利润率" },
  { kind: "submission", status: "completed" },
];

/** Raw (checkpoint-shaped) event as saved on a turn. */
export function rawEvent(step) {
  if (step.kind === "model") {
    return {
      kind: "model",
      usage: step.usage,
      usage_detail: { input_tokens: step.input, output_tokens: step.output },
      finish_reason: step.finish_reason,
    };
  }
  if (step.kind === "tool") {
    return { kind: "tool", name: step.name, status: step.status, reason: step.reason ?? null, rationale: step.rationale };
  }
  return { kind: "submission", status: step.status };
}

export function usageFromEvents(events) {
  const models = events.filter((e) => e.kind === "model");
  return {
    model_calls: models.length,
    search_calls: events.filter((e) => e.kind === "tool" && e.name === "discover_sources" && e.status === "completed").length,
    total_tokens: models.reduce((sum, e) => sum + (Number.isInteger(e.usage) ? e.usage : 0), 0),
  };
}

export const PARTIAL_GAPS = [
  { code: "no_cited_claims", message: "No claim has a verified citation location." },
  { code: "source_selection_partial", message: "Some sources were not fully read." },
];

// ---- Operation objects -------------------------------------------------------

export function makeOperation({ operationId, topicId, status, createdAt, updatedAt, key, body, lastEventId }) {
  return {
    operation_id: operationId,
    status,
    created_at: iso(createdAt),
    updated_at: iso(updatedAt),
    endpoint: `/v1/research/topics/${topicId}/runs`,
    content_hash: sha256(JSON.stringify(body ?? {})),
    idempotency_key_hash: sha256(String(key)),
    last_event_id: lastEventId ?? `${operationId}:0`,
    resume_token: sha256(`resume:${operationId}`).slice(0, 32),
    cancel_token: sha256(`cancel:${operationId}`).slice(0, 32),
    schema_version: "v1",
    source_refs: [],
  };
}

function seededRun({ operationId, topicId, question, turn, versionId, parentVersionId }) {
  const operation = makeOperation({
    operationId,
    topicId,
    status: "completed",
    createdAt: new Date(turn.started_at),
    updatedAt: new Date(turn.finished_at),
    key: `seed-${operationId}`,
    body: { parent_version_id: parentVersionId },
    lastEventId: `${operationId}:${turn.events.length}`,
  });
  return {
    operation,
    topic_id: topicId,
    kind: "start",
    variant: "normal",
    question,
    question_override: null,
    focus_citation: null,
    parent_version_id: parentVersionId,
    turn_id: turn.turn_id,
    turn_index: turn.index,
    replace_turn: false,
    events: structuredClone(turn.events),
    cursor: turn.events.length,
    cancel_requested: false,
    continued_by: null,
    version_id: versionId,
    started_at: turn.started_at,
    seq: 0,
  };
}

// ---- Seed -------------------------------------------------------------------

export function makeSource(now, fields) {
  return {
    schema_version: "1",
    owner_id: "",
    url: null,
    kind: "user_supplied",
    is_simulated: false,
    event_at: null,
    revised_at: null,
    retrieval_method: "user_supplied",
    pdf_text_order: null,
    derived_from_source_id: null,
    response_sha256: null,
    response_content_type: null,
    provenance: {},
    ...fields,
    content_sha256: sha256(fields.text),
  };
}

/** Tiny fictional stand-in for an uploaded PDF original (never a renderable document). */
export const fictionalPdfBytes = (label) =>
  Buffer.from(`%PDF-1.4\n% Fictional QA original: ${label}\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n`, "utf8");

/** Deterministic fictional PDF text: three pages separated by form feeds. */
export function fictionalPdfText(title, order, { joined = false, normalized = false } = {}) {
  const pages = [
    `Fictional PDF extract of "${title}" (${order} text order). Page 1 of 3. Total revenue 12,345 (fictional).${normalized ? " Spacing normalized on re-extraction." : ""}`,
    "Page 2 of 3. Operating income 2,468 (fictional). Segment notes are invented for QA.",
    "Page 3 of 3. Capital expenditure 987 (fictional). End of fictional extract.",
  ];
  return pages.map((page) => (joined ? page.replace(/ /g, "") : page)).join("\f");
}

export const REPORT_TURN2 =
  "营业利润率没有随收入一起提高。按摘录中的金额计算，2026 财年营业利润率约为 31.07%（15,104 / 48,612），上一财年约为 31.45%（12,980 / 41,270），小幅下降约 0.38 个百分点。\n\n" +
  "不支持“增长强劲”的证据主要有两点：一是利润率下滑，说明收入增速没有完全转化为利润；二是年报提到营业费用因扩建数据中心而上升，管理层预期容量投资下一财年仍将保持高位，可能继续压制利润率。";


/**
 * Fresh account state. Plain objects only; the server wraps them in Maps.
 * `now` is a Date; every relative time is derived from it.
 */
export function seed(now) {
  const at = (offsetMs) => new Date(now.getTime() + offsetMs);

  const s1 = makeSource(now, {
    source_id: S1_ID,
    title: "Northwind 示例公司 2026 财年年报摘录",
    kind: "user_supplied",
    retrieval_method: "user_pdf",
    pdf_text_order: "layout",
    original_bytes: fictionalPdfBytes("Northwind FY2026 annual report excerpt"),
    is_simulated: false,
    published_at: "2026-08-01T00:00:00Z",
    obtained_at: iso(at(-2 * HOUR)),
    permissions: { store: true, model_input: true, display: true, export: false },
    text: S1_TEXT,
  });
  // Downloadable seeded PDF (display and export allowed) so the original-download flow can be exercised.
  const s5Title = "Northwind 示例公司投资者介绍材料（PDF）";
  const s5 = makeSource(now, {
    source_id: S5_ID,
    title: s5Title,
    retrieval_method: "user_pdf",
    pdf_text_order: "layout",
    original_bytes: fictionalPdfBytes("Northwind investor presentation"),
    published_at: "2026-08-05T00:00:00Z",
    obtained_at: iso(at(-2 * HOUR)),
    permissions: { store: true, model_input: true, display: true, export: true },
    text: fictionalPdfText(s5Title, "layout"),
  });
  const s2 = makeSource(now, {
    source_id: S2_ID,
    title: "Harbor 示例公司季度公告摘录",
    published_at: "2026-10-15T00:00:00Z",
    obtained_at: iso(at(-3 * DAY)),
    permissions: { store: true, model_input: true, display: true, export: true },
    text:
      "Harbor Devices quarterly announcement (illustrative excerpt). Shipments in the third quarter were 1.9 million units, compared with 1.6 million units in the prior-year quarter.\n\n" +
      "Harbor 示例公司在公告中说明：第三季度出货量为 190 万台，去年同期为 160 万台。",
  });
  const s3 = makeSource(now, {
    source_id: S3_ID,
    title: "Atlas 示例行业报告（不可显示）",
    published_at: "2026-09-01T00:00:00Z",
    obtained_at: iso(at(-10 * DAY)),
    permissions: { store: true, model_input: true, display: false, export: true },
    text: "Atlas Industry Report (illustrative). Display of this text is not permitted for this account.",
  });
  const s4 = makeSource(now, {
    source_id: S4_ID,
    title: "Atlas 示例行业报告（模拟数据）",
    is_simulated: true,
    published_at: "2026-09-01T00:00:00Z",
    obtained_at: iso(at(-10 * DAY)),
    permissions: { store: true, model_input: true, display: true, export: true },
    text: "Atlas simulated dataset (illustrative). Simulated material is isolated from real evidence.",
  });

  const text = S1_TEXT;
  const asOf = new Date(now);
  asOf.setHours(23, 59, 0, 0);

  // ---- Topic A ----
  const questionA = "Northwind 示例公司最近两个财年的收入增长有多快？";
  const topicAId = "topic_northwind_growth";
  const createdA = at(-2 * HOUR);
  const t1Start = at(-2 * HOUR);
  const t1End = new Date(t1Start.getTime() + 2 * MIN + 41_000);
  const t2Start = at(-100 * MIN);
  const t2End = new Date(t2Start.getTime() + 3 * MIN + 2000);

  const fact = (id, label, value, period, quotation, turnId, extra = {}) => {
    const location = span(text, quotation);
    return {
      fact_id: id,
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
      ...extra,
    };
  };
  const calcInput = (f) => ({ fact_id: f.fact_id, period: f.period, unit: f.unit, value: f.value, currency: f.currency });
  const calc = (id, formula, expression, value, first, second, turnId, inputStatus) => ({
    calculation_id: id,
    formula_id: formula,
    formula_version: "1",
    expression,
    value,
    unit: "percent",
    currency: null,
    decimal_places: 2,
    rounding: "half_up",
    inputs: [calcInput(first), calcInput(second)],
    input_verification: "not_verified_by_calculator",
    input_precision_status: "not_established",
    period_order_check: formula === "growth_percent" ? "recognized_formats_only" : null,
    period_granularity_check: null,
    input_status: inputStatus,
    result_status: "matched",
    turn_id: turnId,
  });

  const F1 = fact("F1", "总收入（FY2026）", "48612", "FY2026", Q_REV26, "turn_a1");
  const F2 = fact("F2", "总收入（FY2025）", "41270", "FY2025", Q_REV25, "turn_a1");
  const F3 = fact("F3", "营业利润（FY2026）", "15104", "FY2026", Q_OPI26, "turn_a2", { currency_quote_missing: true });
  const F4 = fact("F4", "营业利润（FY2025）", "12980", "FY2025", Q_OPI25, "turn_a2", { currency_quote_missing: true });
  const CGROWTH = calc("C-growth", "growth_percent", "(48612 / 41270 - 1) * 100", "17.79", F1, F2, "turn_a1", "unverifiable");
  const CM26 = calc("C-margin-fy26", "margin_percent", "(15104 / 48612) * 100", "31.07", F3, F1, "turn_a2", "matched");
  const CM25 = calc("C-margin-fy25", "margin_percent", "(12980 / 41270) * 100", "31.45", F4, F2, "turn_a2", "matched");

  const claim = (id, role, textZh, quotation, extra = {}) => ({
    claim_id: id,
    role,
    text: textZh,
    citations: [citation(text, quotation)],
    edited_by_user: false,
    turn_id: "turn_a2",
    ...extra,
  });
  const claims = [
    claim("C1", "support", "2026 财年总收入为 48,612 百万美元，比上一财年增长 17.79%。", Q_REV26),
    claim("C2", "support", "年报把收入增长主要归因于云服务和订阅收入。", Q_DRIVER),
    claim("C3", "counterargument", "营业利润率从 31.45% 小幅降到 31.07%，收入增长没有带来利润率提升。", Q_OPI26),
    claim("C4", "counterargument", "营业费用因扩建数据中心而上升，可能继续压制利润率。", Q_OPEX, { challenges_claim_ids: ["C1"] }),
  ];

  const model = (usage, input, output) => ({
    kind: "model",
    usage,
    usage_detail: { input_tokens: input, output_tokens: output },
    finish_reason: "tool_calls",
  });
  const tool = (name, status, rationale, reason = null) => ({ kind: "tool", name, status, reason, rationale });

  const events1 = [
    model(4100, 3600, 500),
    tool("read_sources", "read", "读取年报摘录中与问题相关的段落"),
    model(5840, 4980, 860),
    tool("record_facts", "invalid", "记录 FY2026 与 FY2025 总收入", "citation_location_mismatch"),
    tool("record_facts", "completed", "修正引文位置后重新记录"),
    tool("calculate_financials", "completed", "收入同比增长率"),
    { kind: "model_failure", code: "provider_no_result" },
  ];
  const turn1 = {
    turn_id: "turn_a1",
    index: 1,
    question: questionA,
    question_origin: "user",
    status: "partial",
    reason: "no_submission",
    started_at: iso(t1Start),
    finished_at: iso(t1End),
    parent_version_id: "ver_a1",
    operation_ids: ["op_seed_a1"],
    usage: { model_calls: 5, search_calls: 0, total_tokens: 9940 },
    events: events1,
    fact_ids: ["F1", "F2"],
    claim_ids: [],
    calculation_ids: ["C-growth"],
    gaps: structuredClone(PARTIAL_GAPS),
    read_ranges: [revenueParagraphRange(text)],
    official_fetch_status: "not_requested",
    search_status: "not_requested",
    stopping_note: null,
  };

  const events2 = [
    model(5200, 4700, 500),
    tool("read_sources", "read", "读取年报摘录中与问题相关的段落"),
    model(7280, 6100, 1180),
    tool("record_facts", "completed", "记录两期营业利润"),
    tool("calculate_financials", "completed", "计算两期营业利润率"),
    { kind: "submission", status: "completed" },
  ];
  const questionA2 = "营业利润率有没有随收入一起提高？请同时列出不支持“增长强劲”的证据。";
  const turn2 = {
    turn_id: "turn_a2",
    index: 2,
    question: questionA2,
    question_origin: "user",
    status: "completed",
    reason: "submitted",
    started_at: iso(t2Start),
    finished_at: iso(t2End),
    parent_version_id: "ver_a2",
    operation_ids: ["op_seed_a2"],
    usage: { model_calls: 4, search_calls: 0, total_tokens: 12480 },
    events: events2,
    fact_ids: ["F3", "F4"],
    claim_ids: ["C1", "C2", "C3", "C4"],
    calculation_ids: ["C-margin-fy26", "C-margin-fy25"],
    gaps: [],
    read_ranges: [revenueParagraphRange(text), operatingParagraphRange(text)],
    official_fetch_status: "not_requested",
    search_status: "not_requested",
    stopping_note: null,
    report: REPORT_TURN2,
  };

  const sourceRowsA = summarizeSources([s1]);
  const candidates = [
    {
      title: "Northwind FY2026 earnings call transcript",
      url: "https://investor.example.com/northwind/fy2026-call",
      published_at: "2026-07-29T00:00:00Z",
      retrieved_at: iso(at(-90 * MIN)),
      search_result_hash: "a1",
      search_provider: "mock",
      status: "unverified",
    },
    {
      title: "Analyst note: Northwind margin outlook",
      url: "https://research.example.org/notes/northwind-margins",
      published_at: "2026-08-04T00:00:00Z",
      retrieved_at: iso(at(-90 * MIN)),
      search_result_hash: "b2",
      search_provider: "mock",
      status: "unverified",
    },
  ];

  const versionsA = [
    {
      version_id: "ver_a1",
      created_at: iso(createdA),
      created_by: "user",
      review: "draft",
      content: {},
      source_ids: [S1_ID, S5_ID],
      parent_version_id: null,
      note: "",
    },
    {
      version_id: "ver_a2",
      created_at: iso(t1End),
      created_by: "agent",
      review: "draft",
      content: {
        calculations: [CGROWTH],
        claims: [],
        facts: [F1, F2],
        gaps: structuredClone(PARTIAL_GAPS),
        source_candidates: [],
        sources: structuredClone(sourceRowsA),
        turns: [turn1],
      },
      source_ids: [S1_ID, S5_ID],
      parent_version_id: "ver_a1",
      note: "",
    },
    {
      version_id: "ver_a3",
      created_at: iso(t2End),
      created_by: "agent",
      review: "draft",
      content: {
        calculations: [CGROWTH, CM26, CM25],
        claims,
        facts: [F1, F2, F3, F4],
        gaps: [],
        source_candidates: candidates,
        sources: structuredClone(sourceRowsA),
        turns: [turn1, turn2],
      },
      source_ids: [S1_ID, S5_ID],
      parent_version_id: "ver_a2",
      note: "",
    },
  ];
  const topicA = {
    topic_id: topicAId,
    owner_id: "",
    schema_version: "1",
    created_at: iso(createdA),
    updated_at: iso(t2End),
    request: { as_of: iso(asOf), question: questionA, scope: "", source_ids: [S1_ID, S5_ID] },
    versions: versionsA,
  };

  // ---- Topic B ----
  const questionB = "核对一条新闻中的季度出货量是否与公告一致";
  const bUpdated = at(-3 * DAY);
  const bStart = new Date(bUpdated.getTime() - 4 * MIN);
  const eventsB = [
    model(2600, 2300, 300),
    tool("read_sources", "read", "读取公告摘录中的出货量段落"),
    model(1900, 1500, 400),
    { kind: "submission", status: "completed" },
  ];
  const turnB = {
    turn_id: "turn_b1",
    index: 1,
    question: questionB,
    question_origin: "user",
    status: "completed",
    reason: "submitted",
    started_at: iso(bStart),
    finished_at: iso(bUpdated),
    parent_version_id: "ver_b1",
    operation_ids: ["op_seed_b1"],
    usage: { model_calls: 2, search_calls: 0, total_tokens: 4500 },
    events: eventsB,
    fact_ids: [],
    claim_ids: [],
    calculation_ids: [],
    gaps: [],
    read_ranges: [{ source_id: S2_ID, character_start: 0, character_end: s2.text.length }],
    official_fetch_status: "not_requested",
    search_status: "not_requested",
    stopping_note: null,
    report:
      "公告摘录给出的第三季度出货量为 190 万台，去年同期为 160 万台。新闻中的数字需要与这两个数值逐项比对；本轮只读取了公告摘录，尚未取得新闻原文，因此不能判断两者是否一致。",
  };
  const topicB = {
    topic_id: "topic_harbor_news_check",
    owner_id: "",
    schema_version: "1",
    created_at: iso(new Date(bUpdated.getTime() - HOUR)),
    updated_at: iso(bUpdated),
    request: { as_of: null, question: questionB, scope: "", source_ids: [S2_ID] },
    versions: [
      {
        version_id: "ver_b1",
        created_at: iso(new Date(bUpdated.getTime() - HOUR)),
        created_by: "user",
        review: "draft",
        content: {},
        source_ids: [S2_ID],
        parent_version_id: null,
        note: "",
      },
      {
        version_id: "ver_b2",
        created_at: iso(bUpdated),
        created_by: "agent",
        review: "draft",
        content: {
          calculations: [],
          claims: [],
          facts: [],
          gaps: [],
          source_candidates: [],
          sources: summarizeSources([s2]),
          turns: [turnB],
        },
        source_ids: [S2_ID],
        parent_version_id: "ver_b1",
        note: "",
      },
    ],
  };

  // ---- Topic C ----
  const cUpdated = at(-10 * DAY);
  const topicC = {
    topic_id: "topic_industry_report_summary",
    owner_id: "",
    schema_version: "1",
    created_at: iso(cUpdated),
    updated_at: iso(cUpdated),
    request: { as_of: null, question: "总结一份行业报告的主要结论与未解决的问题", scope: "", source_ids: [S3_ID, S4_ID] },
    versions: [
      {
        version_id: "ver_c1",
        created_at: iso(cUpdated),
        created_by: "user",
        review: "draft",
        content: {},
        source_ids: [S3_ID, S4_ID],
        parent_version_id: null,
        note: "",
      },
    ],
  };

  const runs = [
    seededRun({ operationId: "op_seed_a1", topicId: topicAId, question: questionA, turn: turn1, versionId: "ver_a2", parentVersionId: "ver_a1" }),
    seededRun({ operationId: "op_seed_a2", topicId: topicAId, question: questionA2, turn: turn2, versionId: "ver_a3", parentVersionId: "ver_a2" }),
    seededRun({ operationId: "op_seed_b1", topicId: topicB.topic_id, question: questionB, turn: turnB, versionId: "ver_b2", parentVersionId: "ver_b1" }),
  ];
  runs.forEach((run, index) => {
    run.seq = index + 1;
  });

  return {
    sources: { [S1_ID]: s1, [S2_ID]: s2, [S3_ID]: s3, [S4_ID]: s4, [S5_ID]: s5 },
    topics: [topicA, topicB, topicC],
    runs,
  };
}
