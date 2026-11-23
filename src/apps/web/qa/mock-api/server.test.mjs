// Run from apps/web: node --test qa/mock-api/server.test.mjs
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { after, afterEach, before, beforeEach, describe, test } from "node:test";
import { createServer } from "./server.mjs";
import { S1_ID, S3_ID, S4_ID, S5_ID } from "./fixtures.mjs";

let server;
let base;
let token;
let versionOf;

before(async () => {
  server = createServer({ tickMs: 20 });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${server.address().port}`;
});

after(async () => {
  server.closeAllConnections?.();
  await new Promise((resolve) => server.close(resolve));
});

async function call(method, path, { body, headers = {}, auth = true, rawWrite = false } = {}) {
  const init = { method, headers: { ...headers } };
  if (auth && token) init.headers.Authorization = `Bearer ${token}`;
  // Like the UI client: writes are always JSON, an absent body is sent as {}.
  const payload = body === undefined && (method === "POST" || method === "PUT") && !rawWrite ? {} : body;
  if (payload !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(payload);
  }
  const response = await fetch(base + path, init);
  const text = await response.text();
  return { status: response.status, headers: response.headers, json: text ? JSON.parse(text) : undefined };
}

async function login() {
  const email = `qa-${randomUUID()}@example.com`;
  const registered = await call("POST", "/v1/auth/local/register", { auth: false, body: { email, password: "correct-horse-battery" } });
  assert.equal(registered.status, 201);
  const again = await call("POST", "/v1/auth/local/login", { auth: false, body: { email, password: "correct-horse-battery" } });
  assert.equal(again.status, 200);
  assert.equal(again.json.account.account_id, registered.json.account.account_id);
  return { registered: registered.json, token: again.json.access_token };
}

const topicPath = (id) => `/v2/research/topics/${id}`;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitForTerminal(topicId, operationId, { collect } = {}) {
  let cursor = 0;
  for (let i = 0; i < 400; i += 1) {
    const progress = await call("GET", `${topicPath(topicId)}/runs/${operationId}/progress?after=${cursor}`);
    assert.equal(progress.status, 200);
    for (const event of progress.json.events) {
      collect?.push(event);
    }
    cursor += progress.json.events.length;
    if (["completed", "failed", "cancelled"].includes(progress.json.operation_status)) return progress.json;
    await sleep(10);
  }
  throw new Error("run did not finish");
}

async function startRun(topicId, extra = {}, key = randomUUID()) {
  const topic = (await call("GET", topicPath(topicId))).json;
  const parent = topic.versions.at(-1).version_id;
  // Discovery is on by default when the key is omitted; pin it off so the scripted event counts stay fixed.
  const body = { parent_version_id: parent, discover_sources: false, ...extra };
  const response = await call("POST", `${topicPath(topicId)}/runs`, { body, headers: { "Idempotency-Key": key } });
  return { response, body, key };
}

const ALL = { store: true, model_input: true, display: true, export: true };
const b64 = (value) => Buffer.from(value).toString("base64");
const SOURCE_KEYS = ["source_id", "title", "kind", "is_simulated", "published_at", "event_at", "revised_at", "obtained_at", "content_sha256", "retrieval_method", "pdf_text_order", "derived_from_source_id", "permissions", "verification"];

async function textSource(extra = {}) {
  const response = await call("POST", "/v2/research/sources", { body: { title: "Fictional note", text: "Fabrikam Example Inc reported revenue of 100 (fictional).", permissions: ALL, ...extra } });
  assert.equal(response.status, 201);
  return response.json;
}

async function pdfSource(extra = {}) {
  const response = await call("POST", "/v2/research/sources/pdf", { body: { title: "Fictional deck", pdf_base64: b64("%PDF-1.4 fictional"), permissions: ALL, ...extra } });
  assert.equal(response.status, 201);
  return response.json;
}

async function newTopic(sourceIds = [], question = "新研究主题") {
  const response = await call("POST", "/v2/research/topics", { body: { question, source_ids: sourceIds } });
  assert.equal(response.status, 201);
  return response.json;
}

/** Finds the in-memory run record (test hook) for the signed-in session. */
function runRecord(operationId) {
  for (const account of server.mockState.accounts.values()) {
    const run = account.data?.runs.get(operationId);
    if (run) return run;
  }
  throw new Error("run not found");
}

describe("mock research API", () => {
  before(async () => {
    token = (await login()).token;
  });

  test("session shape, listing and unauthenticated error body", async () => {
    const { registered } = await login();
    assert.deepEqual(Object.keys(registered).sort(), ["access_token", "account", "auth_mode", "expires_at", "session_id"]);
    assert.equal(registered.auth_mode, "local_mock");
    assert.match(registered.access_token, /^\S{32,256}$/);
    assert.deepEqual(Object.keys(registered.account).sort(), ["account_id", "created_at", "email"]);

    const short = await call("POST", "/v1/auth/local/register", { auth: false, body: { email: "a@b.co", password: "short" } });
    assert.equal(short.status, 422);

    const list = await call("GET", "/v2/research/topics");
    assert.equal(list.status, 200);
    assert.match(list.headers.get("content-type"), /^application\/json/);
    assert.equal(list.json.length, 3);

    const bare = await call("GET", "/v2/research/topics", { auth: false });
    assert.equal(bare.status, 401);
    assert.deepEqual(bare.json, { error_code: "unauthorized", message: bare.json.message, request_id: "req_mock", retryable: false, details: [] });

    const missing = await call("GET", "/v2/nope");
    assert.equal(missing.status, 404);
    assert.equal(missing.json.error_code, "not_found");
  });

  test("seed content: quotations and offsets are exact", async () => {
    const topics = (await call("GET", "/v2/research/topics")).json;
    const a = topics.find((t) => t.request.question.startsWith("Northwind"));
    assert.equal(a.versions.length, 3);
    const latest = a.versions.at(-1);
    const source = (await call("GET", `${topicPath(a.topic_id)}/versions/${latest.version_id}/sources/${S1_ID}/text`)).json;
    for (const claim of latest.content.claims) {
      for (const cite of claim.citations) {
        assert.equal(source.text.slice(cite.location.character_start, cite.location.character_end), cite.quotation);
      }
    }
    for (const fact of latest.content.facts) {
      assert.equal(source.text.slice(fact.character_start, fact.character_end), fact.quotation);
    }
    const rows = (await call("GET", `${topicPath(a.topic_id)}/versions/${latest.version_id}/sources`)).json;
    assert.equal(rows[0].status, "available");
    assert.equal(rows[0].verification, "user_declared_not_verified");
    assert.equal("text" in rows[0], false);
    assert.equal(latest.content.sources[0].initial_prompt_coverage.text_characters, source.text.length);
    // The attached source forbids export.
    assert.equal((await call("GET", `${topicPath(a.topic_id)}/versions/${latest.version_id}/export`)).status, 403);
    const meta = await call("GET", `${topicPath(a.topic_id)}/versions/${latest.version_id}/export/metadata`);
    assert.equal(meta.status, 200);
    assert.equal(meta.json.turn_count, 2);
    assert.equal(meta.json.claim_count, 4);
  });

  test("run to completion: 4 versions, 3 turns, contiguous events, idempotent replay", async () => {
    const a = (await call("GET", "/v2/research/topics")).json.find((t) => t.request.question.startsWith("Northwind"));
    const { response, body, key } = await startRun(a.topic_id, { focus_citation: { claim_id: "C1", citation_index: 0 } });
    assert.equal(response.status, 202);
    const operation = response.json.operation;
    assert.equal(operation.status, "pending");
    assert.equal(operation.schema_version, "v1");

    // A second start while the first is active conflicts; replaying the same key does not.
    const second = await startRun(a.topic_id);
    assert.equal(second.response.status, 409);
    assert.equal(second.response.json.error_code, "lifecycle_conflict");
    const replay = await call("POST", `${topicPath(a.topic_id)}/runs`, { body, headers: { "Idempotency-Key": key } });
    assert.equal(replay.status, 200);
    assert.equal(replay.json.operation.operation_id, operation.operation_id);
    const changed = await call("POST", `${topicPath(a.topic_id)}/runs`, {
      body: { ...body, question_override: "不同的问题" },
      headers: { "Idempotency-Key": key },
    });
    assert.equal(changed.status, 409);
    assert.equal(changed.json.error_code, "idempotency_conflict");
    const noKey = await call("POST", `${topicPath(a.topic_id)}/runs`, { body });
    assert.equal(noKey.status, 400);

    const events = [];
    const final = await waitForTerminal(a.topic_id, operation.operation_id, { collect: events });
    assert.equal(final.operation_status, "completed");
    assert.equal(final.event_count, 6);
    assert.deepEqual(events.map((e) => e.index), [0, 1, 2, 3, 4, 5]);
    assert.equal(events[0].kind, "model");
    assert.equal(events[0].input_tokens, 1980);
    assert.equal(final.usage.total_tokens, 5959);
    assert.ok(final.version_id);

    const topic = (await call("GET", topicPath(a.topic_id))).json;
    assert.equal(topic.versions.length, 4);
    const content = topic.versions.at(-1).content;
    assert.equal(content.turns.length, 3);
    const turn = content.turns[2];
    assert.equal(turn.index, 3);
    assert.equal(turn.status, "completed");
    assert.deepEqual(turn.operation_ids, [operation.operation_id]);
    assert.deepEqual(turn.usage, { model_calls: 2, search_calls: 0, total_tokens: 5959 });
    assert.deepEqual(turn.focus_citation, { claim_id: "C1", citation_index: 0 });
    assert.equal(topic.versions.at(-1).created_by, "agent");
    assert.equal(topic.versions.at(-1).parent_version_id, topic.versions.at(-2).version_id);
    assert.equal(content.claims.length, 5);
    assert.equal(content.facts.length, 7);
    assert.equal(content.calculations.length, 5);
    const newClaim = content.claims.at(-1);
    const sourceText = (await call("GET", `${topicPath(a.topic_id)}/versions/${topic.versions.at(-1).version_id}/sources/${S1_ID}/text`)).json.text;
    const cite = newClaim.citations[0];
    assert.equal(sourceText.slice(cite.location.character_start, cite.location.character_end), cite.quotation);
    for (const range of turn.read_ranges) assert.ok(range.character_end > range.character_start);

    const rec = await call("GET", `${topicPath(a.topic_id)}/runs/${operation.operation_id}/recovery-status`);
    assert.deepEqual(rec.json, { can_continue: false, can_deliver: false, reason: "unavailable", checkpoint: null, version_id: null });

    const runs = (await call("GET", `${topicPath(a.topic_id)}/runs`)).json;
    assert.equal(runs.length, 3);
    assert.equal(runs[0].operation.operation_id, operation.operation_id);
    // A stale parent version is rejected.
    const stale = await call("POST", `${topicPath(a.topic_id)}/runs`, {
      body: { parent_version_id: a.versions[0].version_id },
      headers: { "Idempotency-Key": randomUUID() },
    });
    assert.equal(stale.status, 409);
    versionOf = topic.versions.at(-1).version_id;
  });

  test("partial run saves a partial turn and continue completes the same turn", async () => {
    const a = (await call("GET", "/v2/research/topics")).json.find((t) => t.request.question.startsWith("Northwind"));
    const before = (await call("GET", topicPath(a.topic_id))).json;
    const { response } = await startRun(a.topic_id, { question_override: "请列出部分数据的缺口" });
    assert.equal(response.status, 202);
    const op = response.json.operation.operation_id;
    const progress = await waitForTerminal(a.topic_id, op);
    assert.equal(progress.operation_status, "failed");
    assert.equal(progress.event_count, 3);

    const topic = (await call("GET", topicPath(a.topic_id))).json;
    assert.equal(topic.versions.length, before.versions.length + 1);
    const partial = topic.versions.at(-1).content.turns.at(-1);
    assert.equal(partial.status, "partial");
    assert.equal(partial.reason, "no_submission");
    assert.deepEqual(partial.gaps.map((g) => g.code), ["no_cited_claims", "source_selection_partial"]);
    assert.equal(partial.fact_ids.length, 1);
    assert.equal(partial.claim_ids.length, 0);

    const rec = await call("GET", `${topicPath(a.topic_id)}/runs/${op}/recovery-status`);
    assert.deepEqual(rec.json, { can_continue: true, can_deliver: false, reason: null, checkpoint: "tool_batch", version_id: topic.versions.at(-1).version_id });

    const key = randomUUID();
    const cont = await call("POST", `${topicPath(a.topic_id)}/runs/${op}/continue`, { headers: { "Idempotency-Key": key } });
    assert.equal(cont.status, 202);
    const replay = await call("POST", `${topicPath(a.topic_id)}/runs/${op}/continue`, { headers: { "Idempotency-Key": key } });
    assert.equal(replay.status, 200);
    assert.equal(replay.json.operation.operation_id, cont.json.operation.operation_id);
    assert.equal((await call("POST", `${topicPath(a.topic_id)}/runs/${op}/recover`, { headers: { "Idempotency-Key": randomUUID() } })).status, 409);

    const events = [];
    const done = await waitForTerminal(a.topic_id, cont.json.operation.operation_id, { collect: events });
    assert.equal(done.operation_status, "completed");
    assert.equal(done.event_count, 6);
    assert.deepEqual(events.map((e) => e.index), [0, 1, 2, 3, 4, 5]);

    const after = (await call("GET", topicPath(a.topic_id))).json;
    assert.equal(after.versions.length, topic.versions.length + 1);
    const turns = after.versions.at(-1).content.turns;
    assert.equal(turns.length, topic.versions.at(-1).content.turns.length);
    const completed = turns.at(-1);
    assert.equal(completed.turn_id, partial.turn_id);
    assert.equal(completed.status, "completed");
    assert.deepEqual(completed.operation_ids, [op, cont.json.operation.operation_id]);
    assert.deepEqual(completed.gaps, []);
    assert.equal(completed.usage.total_tokens, 5959);

    const used = await call("GET", `${topicPath(a.topic_id)}/runs/${op}/recovery-status`);
    assert.equal(used.json.can_continue, false);
    assert.equal(used.json.reason, "already_used");
    assert.ok(versionOf);
  });

  test("failed run saves no turn and continue runs the remaining steps", async () => {
    const a = (await call("GET", "/v2/research/topics")).json.find((t) => t.request.question.startsWith("Northwind"));
    const before = (await call("GET", topicPath(a.topic_id))).json;
    const turnsBefore = before.versions.at(-1).content.turns.length;
    const { response } = await startRun(a.topic_id, { question_override: "失败场景：请重新计算收入增速" });
    const op = response.json.operation.operation_id;
    const progress = await waitForTerminal(a.topic_id, op);
    assert.equal(progress.operation_status, "failed");
    assert.equal(progress.event_count, 2);
    const unchanged = (await call("GET", topicPath(a.topic_id))).json;
    assert.equal(unchanged.versions.length, before.versions.length);
    const rec = await call("GET", `${topicPath(a.topic_id)}/runs/${op}/recovery-status`);
    assert.equal(rec.json.can_continue, true);
    const cont = await call("POST", `${topicPath(a.topic_id)}/runs/${op}/continue`, { headers: { "Idempotency-Key": randomUUID() } });
    assert.equal(cont.status, 202);
    const done = await waitForTerminal(a.topic_id, cont.json.operation.operation_id);
    assert.equal(done.operation_status, "completed");
    const after = (await call("GET", topicPath(a.topic_id))).json;
    assert.equal(after.versions.length, before.versions.length + 1);
    assert.equal(after.versions.at(-1).content.turns.length, turnsBefore + 1);
  });

  test("cancel: right token cancels without saving a turn; wrong token is 403", async () => {
    const topics = (await call("GET", "/v2/research/topics")).json;
    const b = topics.find((t) => t.request.question.startsWith("核对"));
    const { response } = await startRun(b.topic_id);
    const operation = response.json.operation;
    const wrong = await call("POST", `/v1/operations/${operation.operation_id}/cancel`, { body: { cancel_token: "nope" } });
    assert.equal(wrong.status, 403);
    assert.equal(wrong.json.request_id, "req_mock");
    const cancelled = await call("POST", `/v1/operations/${operation.operation_id}/cancel`, { body: { cancel_token: operation.cancel_token } });
    assert.equal(cancelled.status, 200);
    const final = await waitForTerminal(b.topic_id, operation.operation_id);
    assert.equal(final.operation_status, "cancelled");
    const topic = (await call("GET", topicPath(b.topic_id))).json;
    assert.equal(topic.versions.length, b.versions.length);
    const rec = await call("GET", `${topicPath(b.topic_id)}/runs/${operation.operation_id}/recovery-status`);
    assert.deepEqual(rec.json, { can_continue: false, can_deliver: false, reason: "no_checkpoint", checkpoint: null, version_id: null });
    const op = await call("GET", `${topicPath(b.topic_id)}/runs/${operation.operation_id}`);
    assert.equal(op.json.operation.status, "cancelled");
  });

  test("source text is 403 when display is off or the source is simulated (topic C seed)", async () => {
    // Seed topic C attaches a display=false source and a simulated source on purpose.
    const c = (await call("GET", "/v2/research/topics")).json.find((t) => t.request.question.startsWith("总结"));
    const v = c.versions[0].version_id;
    assert.deepEqual(c.versions[0].source_ids, [S3_ID, S4_ID]);
    for (const id of [S3_ID, S4_ID]) {
      const r = await call("GET", `${topicPath(c.topic_id)}/versions/${v}/sources/${id}/text`);
      assert.equal(r.status, 403);
      assert.equal(r.json.error_code, "invalid_request");
    }
    const rows = (await call("GET", `${topicPath(c.topic_id)}/versions/${v}/sources`)).json;
    assert.deepEqual(rows.map((row) => row.permissions.display), [false, true]);
    assert.equal(rows[1].is_simulated, true);
    // A source that is not attached to the version is not found.
    assert.equal((await call("GET", `${topicPath(c.topic_id)}/versions/${v}/sources/${S1_ID}/text`)).status, 404);
  });

  test("topic creation, news topic export and account deletion", async () => {
    const created = await call("POST", "/v2/research/topics", { body: { question: "新建主题", source_ids: [] } });
    assert.equal(created.status, 201);
    assert.deepEqual(created.json.versions[0].content, {});
    assert.equal(created.json.versions[0].created_by, "user");
    assert.equal(created.json.request.scope, "");
    assert.equal((await call("POST", "/v2/research/topics", { body: { source_ids: ["missing"] } })).status, 422);

    const b = (await call("GET", "/v2/research/topics")).json.find((t) => t.request.question.startsWith("核对"));
    const exported = await call("GET", `${topicPath(b.topic_id)}/versions/${b.versions.at(-1).version_id}/export`);
    assert.equal(exported.status, 200);
    assert.equal(exported.json.format, "research_json");
    assert.equal(exported.json.source_provenance.length, 1);

    const { token: other } = await login();
    const saved = token;
    token = other;
    assert.equal((await call("GET", topicPath(b.topic_id))).status, 200); // seeded per account with the same ids
    assert.equal((await call("DELETE", "/v1/auth/local/account", { auth: true })).status, 204);
    assert.equal((await call("GET", "/v2/research/topics")).status, 401);
    token = saved;
    assert.equal((await call("POST", "/v1/auth/local/logout")).status, 204);
    assert.equal((await call("GET", "/v2/research/topics")).status, 401);
    token = (await login()).token;
  });
  test("text source: display on returns text, display off omits it, strict body", async () => {
    const shown = await textSource({ url: "https://news.example.com/a", published_at: "2026-10-01T08:00:00+08:00" });
    assert.match(shown.source_id, /^source_[0-9a-f]{32}$/);
    assert.equal(shown.text, "Fabrikam Example Inc reported revenue of 100 (fictional).");
    assert.equal(shown.retrieval_method, "user_supplied");
    assert.equal(shown.kind, "user_supplied");
    assert.equal(shown.published_at, "2026-10-01T00:00:00Z");
    assert.equal(shown.url, "https://news.example.com/a");
    assert.equal(shown.verification, "user_declared_not_verified");
    assert.equal("status" in shown, false);
    for (const key of SOURCE_KEYS) assert.ok(key in shown, key);
    const hidden = await textSource({ permissions: { store: true } });
    assert.equal("text" in hidden, false);
    assert.deepEqual(hidden.permissions, { store: true, model_input: false, display: false, export: false });
    const fetched = await call("GET", `/v2/research/sources/${shown.source_id}`);
    assert.equal(fetched.status, 200);
    assert.equal(fetched.json.content_sha256, shown.content_sha256);
    assert.equal((await call("GET", "/v2/research/sources/source_missing")).status, 404);

    const base = { title: "T", text: "x", permissions: ALL };
    for (const extra of [{ surprise: 1 }, { title: "   " }, { text: "  \n" }, { published_at: "2026-10-01T08:00:00" }, { url: "https://u:p@news.example.com/" }, { kind: "other" }]) {
      const bad = await call("POST", "/v2/research/sources", { body: { ...base, ...extra } });
      assert.equal(bad.status, 422, JSON.stringify(extra));
      assert.equal(bad.json.error_code, "validation_error");
    }
    assert.equal((await call("POST", "/v2/research/sources", { body: { title: "T", text: "x" } })).status, 422);
    const noStore = await call("POST", "/v2/research/sources", { body: { ...base, permissions: { display: true } } });
    assert.equal(noStore.status, 422);
    assert.equal(noStore.json.error_code, "invalid_request");
    assert.equal(noStore.json.message, "Source storage permission is required.");
    assert.equal((await call("POST", "/v2/research/sources", { body: base, auth: false })).status, 401);
  });

  test("pdf upload: original kept without text; invalid, busy and spacing magic", async () => {
    const created = await pdfSource({ text_order: "reading" });
    assert.equal("text" in created, false);
    assert.equal(created.retrieval_method, "user_pdf");
    assert.equal(created.pdf_text_order, "reading");
    assert.equal(created.original_pdf_available, true);
    assert.equal("pdf_text_spacing_warning" in created, false);
    const full = (await call("GET", `/v2/research/sources/${created.source_id}`)).json;
    assert.ok(full.text.split("\f").length >= 3);
    const spaced = await pdfSource({ title: "间距问题文档" });
    assert.equal(spaced.pdf_text_spacing_warning, true);

    const notPdf = await call("POST", "/v2/research/sources/pdf", { body: { title: "T", pdf_base64: b64("hello"), permissions: ALL } });
    assert.equal(notPdf.status, 422);
    assert.equal(notPdf.json.error_code, "invalid_request");
    assert.equal(notPdf.json.message, "PDF is invalid, too large, encrypted, or has no extractable text.");
    const badBase64 = await call("POST", "/v2/research/sources/pdf", { body: { title: "T", pdf_base64: "%%%not-base64", permissions: ALL } });
    assert.equal(badBase64.status, 422);
    assert.equal(badBase64.json.error_code, "invalid_request");
    const tooLarge = await call("POST", "/v2/research/sources/pdf", { body: { title: "T", pdf_base64: Buffer.concat([Buffer.from("%PDF-"), Buffer.alloc(4_999_996)]).toString("base64"), permissions: ALL } });
    assert.equal(tooLarge.status, 422);
    assert.equal(tooLarge.json.error_code, "invalid_request");
    const busy = await call("POST", "/v2/research/sources/pdf", { body: { title: "繁忙的解析器", pdf_base64: b64("%PDF-1.4"), permissions: ALL } });
    assert.equal(busy.status, 503);
    assert.equal(busy.json.error_code, "internal_error");
    assert.equal(busy.json.retryable, true);
    assert.equal((await call("POST", "/v2/research/sources/pdf", { body: { title: "T", pdf_base64: b64("%PDF-"), text_order: "zigzag", permissions: ALL } })).status, 422);
  });

  test("html upload: tags stripped, invalid UTF-8 rejected", async () => {
    const html = "<html><head><style>p{}</style><script>var x=1</script></head><body><p>Contoso   Example Holdings</p><p>Revenue 7</p></body></html>";
    const created = await call("POST", "/v2/research/sources/html", { body: { title: "Fictional page", html_base64: b64(html), permissions: ALL } });
    assert.equal(created.status, 201);
    assert.equal("text" in created.json, false);
    assert.equal(created.json.retrieval_method, "user_html");
    assert.equal(created.json.original_html_available, true);
    assert.match(created.json.extraction_disclosure, /^Webpage text is extracted/);
    const full = (await call("GET", `/v2/research/sources/${created.json.source_id}`)).json;
    assert.equal(full.text, "Contoso Example Holdings\nRevenue 7");
    const invalidUtf8 = await call("POST", "/v2/research/sources/html", { body: { title: "T", html_base64: Buffer.from([0xff, 0xfe, 0x3c, 0x70, 0x3e]).toString("base64"), permissions: ALL } });
    assert.equal(invalidUtf8.status, 422);
    assert.equal(invalidUtf8.json.message, "HTML is invalid, too large, not UTF-8, or has no extractable text.");
    const empty = await call("POST", "/v2/research/sources/html", { body: { title: "T", html_base64: b64("<script>1</script>"), permissions: ALL } });
    assert.equal(empty.status, 422);
    assert.equal(empty.json.error_code, "invalid_request");
  });

  test("official fetch: SEC filing keeps text hidden; allowlist, store and timeout rules", async () => {
    const sec = "https://www.sec.gov/Archives/edgar/data/1000001/000100000126000012/nwex-20260630.htm";
    const filing = await call("POST", "/v2/research/sources/fetch", { body: { url: sec, title: "Fictional 10-K", permissions: ALL } });
    assert.equal(filing.status, 201);
    assert.equal("text" in filing.json, false);
    assert.equal(filing.json.kind, "official");
    assert.equal(filing.json.retrieval_method, "sec_filing_html");
    assert.equal(filing.json.original_html_available, true);
    assert.ok(filing.json.extraction_disclosure);
    const page = await call("POST", "/v2/research/sources/fetch", { body: { url: "https://www.bea.gov/data/example", title: "Fictional BEA page", permissions: ALL } });
    assert.equal(page.status, 201);
    assert.equal(page.json.retrieval_method, "official_html");
    assert.ok(page.json.text.length > 0);
    const bad = [
      "https://news.example.com/a",
      "http://www.bea.gov/data",
      "https://user:pw@www.bea.gov/data",
      "https://www.bea.gov:8443/data",
      "https://www.bea.gov/data#frag",
      "https://www.sec.gov/Archives/edgar/data/1000001/000100000126000012/nwex-20260630.htm?x=1",
      "https://www.sec.gov/cgi-bin/browse-edgar",
    ];
    for (const url of bad) {
      const response = await call("POST", "/v2/research/sources/fetch", { body: { url, title: "T", permissions: ALL } });
      assert.equal(response.status, 422, url);
      assert.equal(response.json.error_code, "validation_error");
      assert.equal(response.json.details[0].location, "body/url");
    }
    const noStore = await call("POST", "/v2/research/sources/fetch", { body: { url: sec, title: "T", permissions: { display: true } } });
    assert.equal(noStore.status, 422);
    assert.equal(noStore.json.message, "Source storage permission is required.");
    const slow = await call("POST", "/v2/research/sources/fetch", { body: { url: "https://www.federalreserve.gov/timeout/report", title: "T", permissions: ALL } });
    assert.equal(slow.status, 503);
    assert.equal(slow.json.retryable, true);
  });

  test("sec identity search and companyconcept", async () => {
    const byTicker = await call("POST", "/v2/research/identities/sec-search", { body: { query: "nwex" } });
    assert.equal(byTicker.status, 200);
    assert.deepEqual(byTicker.json.candidates, [{ cik: "0001000001", ticker: "NWEX", name: "Northwind Example Corp", exchange: null }]);
    assert.equal(byTicker.json.source_url, "https://www.sec.gov/files/company_tickers.json");
    assert.match(byTicker.json.response_sha256, /^[0-9a-f]{64}$/);
    assert.ok(byTicker.json.limitation);
    const byName = await call("POST", "/v2/research/identities/sec-search", { body: { query: "example" } });
    assert.deepEqual(byName.json.candidates.map((c) => c.ticker), ["CTEX", "FBEX", "NWEX"]);
    assert.deepEqual((await call("POST", "/v2/research/identities/sec-search", { body: { query: "1000002" } })).json.candidates.map((c) => c.ticker), ["CTEX"]);
    assert.deepEqual((await call("POST", "/v2/research/identities/sec-search", { body: { query: "zzz" } })).json.candidates, []);
    const unconfigured = await call("POST", "/v2/research/identities/sec-search", { body: { query: "未配置" } });
    assert.equal(unconfigured.status, 503);
    assert.equal(unconfigured.json.retryable, false);
    assert.equal(unconfigured.json.message, "SEC source contact is not configured.");
    assert.equal((await call("POST", "/v2/research/identities/sec-search", { body: { query: "  " } })).status, 422);

    const concept = await call("POST", "/v2/research/sources/sec-concept", { body: { cik: "1000001", tag: "Revenues", max_records: 4, permissions: ALL } });
    assert.equal(concept.status, 201);
    assert.equal(concept.json.title, "SEC companyconcept us-gaap:Revenues CIK 0001000001");
    assert.equal(concept.json.url, "https://data.sec.gov/api/xbrl/companyconcept/CIK0001000001/us-gaap/Revenues.json");
    assert.equal(concept.json.retrieval_method, "sec_companyconcept");
    assert.equal(concept.json.kind, "official");
    assert.equal(concept.json.text.split("\n").filter((line) => line.startsWith("period ")).length, 4);
    assert.match(concept.json.text, /USD/);
    const hidden = await call("POST", "/v2/research/sources/sec-concept", { body: { cik: "1000001", tag: "Revenues", permissions: { store: true } } });
    assert.equal("text" in hidden.json, false);
    for (const extra of [{ tag: "1bad-tag" }, { cik: "abc" }, { cik: "0" }, { max_records: 101 }, { taxonomy: "other" }, { unit: "US D" }, { filing_before: "2026-13-40" }, { surprise: true }]) {
      const bad = await call("POST", "/v2/research/sources/sec-concept", { body: { cik: "1000001", tag: "Revenues", permissions: ALL, ...extra } });
      assert.equal(bad.status, 422, JSON.stringify(extra));
      assert.equal(bad.json.error_code, "validation_error");
    }
    for (const body of [{ cik: "0009999999", tag: "Revenues", permissions: ALL }, { cik: "1000001", tag: "Revenues", permissions: { display: true } }]) {
      const unusable = await call("POST", "/v2/research/sources/sec-concept", { body });
      assert.equal(unusable.status, 422);
      assert.equal(unusable.json.error_code, "invalid_request");
      assert.equal(unusable.json.message, "SEC source data could not be used.");
    }
  });

  test("attach sources: union of ids, stale parent 409, unknown source or topic 404", async () => {
    const first = await textSource();
    const second = await textSource({ title: "Second note" });
    const topic = await newTopic([first.source_id]);
    const parent = topic.versions.at(-1).version_id;
    const attached = await call("POST", `${topicPath(topic.topic_id)}/sources`, { body: { parent_version_id: parent, source_ids: [first.source_id, second.source_id] } });
    assert.equal(attached.status, 201);
    assert.equal(attached.json.versions.length, 2);
    const version = attached.json.versions.at(-1);
    assert.match(version.version_id, /^version_[0-9a-f]{32}$/);
    assert.equal(version.parent_version_id, parent);
    assert.equal(version.created_by, "user");
    assert.equal(version.review, "draft");
    assert.deepEqual(version.source_ids, [first.source_id, second.source_id]);
    assert.ok(Date.parse(attached.json.updated_at) >= Date.parse(attached.json.created_at));
    assert.equal((await call("GET", topicPath(topic.topic_id))).json.versions.length, 2);

    const stale = await call("POST", `${topicPath(topic.topic_id)}/sources`, { body: { parent_version_id: parent, source_ids: [second.source_id] } });
    assert.equal(stale.status, 409);
    assert.equal(stale.json.error_code, "lifecycle_conflict");
    assert.equal(stale.json.message, "Research topic has changed; reload before saving.");
    const unknown = await call("POST", `${topicPath(topic.topic_id)}/sources`, { body: { parent_version_id: version.version_id, source_ids: ["source_missing"] } });
    assert.equal(unknown.status, 404);
    assert.equal(unknown.json.message, "Research topic or source not found.");
    assert.equal((await call("POST", `${topicPath("topic_missing")}/sources`, { body: { parent_version_id: parent, source_ids: [first.source_id] } })).status, 404);
    for (const body of [{ parent_version_id: parent, source_ids: [] }, { parent_version_id: parent }, { parent_version_id: parent, source_ids: [" "] }, { parent_version_id: parent, source_ids: ["x"], extra: 1 }]) {
      assert.equal((await call("POST", `${topicPath(topic.topic_id)}/sources`, { body })).status, 422, JSON.stringify(body));
    }
  });

  test("original download: bytes with attachment headers; export or display off is 403", async () => {
    const bytes = "%PDF-1.4 fictional original";
    const pdf = await pdfSource({ pdf_base64: b64(bytes) });
    const response = await fetch(`${base}/v2/research/sources/${pdf.source_id}/original`, { headers: { Authorization: `Bearer ${token}` } });
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("content-type"), "application/pdf");
    assert.equal(response.headers.get("content-disposition"), 'attachment; filename="source.pdf"');
    assert.equal(response.headers.get("cache-control"), "no-store");
    assert.equal(await response.text(), bytes);

    const html = await call("POST", "/v2/research/sources/html", { body: { title: "Page", html_base64: b64("<p>hi</p>"), permissions: ALL } });
    const htmlResponse = await fetch(`${base}/v2/research/sources/${html.json.source_id}/original`, { headers: { Authorization: `Bearer ${token}` } });
    assert.equal(htmlResponse.headers.get("content-type"), "application/octet-stream");
    assert.equal(htmlResponse.headers.get("content-disposition"), 'attachment; filename="source.html"');
    assert.equal(await htmlResponse.text(), "<p>hi</p>");

    // Seeded sources: S5 is downloadable; S1 has an original but forbids export; S2 has no original.
    const seeded = await fetch(`${base}/v2/research/sources/${S5_ID}/original`, { headers: { Authorization: `Bearer ${token}` } });
    assert.equal(seeded.status, 200);
    assert.equal(seeded.headers.get("content-type"), "application/pdf");
    assert.ok((await seeded.text()).startsWith("%PDF-"));
    for (const forbidden of [S1_ID, "src_harbor_shipments", (await pdfSource({ permissions: { store: true, display: true } })).source_id]) {
      const denied = await call("GET", `/v2/research/sources/${forbidden}/original`);
      assert.equal(denied.status, 403, forbidden);
      assert.equal(denied.json.error_code, "invalid_request");
      assert.equal(denied.json.message, "The original document is not available for download.");
    }
    assert.equal((await call("GET", "/v2/research/sources/source_missing/original")).status, 404);
    const topicA = (await call("GET", "/v2/research/topics")).json.find((t) => t.request.question.startsWith("Northwind"));
    assert.deepEqual(topicA.request.source_ids, [S1_ID, S5_ID]);
  });

  test("re-extract: derived source, idempotent replay, conflicts and non-PDF", async () => {
    const original = await pdfSource({ title: "Fictional deck", published_at: "2026-09-01T00:00:00Z", permissions: { store: true, display: true, export: false } });
    const key = `reextract-${randomUUID()}`;
    const created = await call("POST", `/v2/research/sources/${original.source_id}/reextract`, { body: { text_order: "reading" }, headers: { "Idempotency-Key": key } });
    assert.equal(created.status, 201);
    assert.equal(created.headers.get("idempotency-replayed"), "false");
    assert.equal("text" in created.json, false);
    assert.notEqual(created.json.source_id, original.source_id);
    assert.equal(created.json.derived_from_source_id, original.source_id);
    assert.equal(created.json.pdf_text_order, "reading");
    assert.equal(created.json.title, "Fictional deck — reading");
    assert.equal(created.json.retrieval_method, "user_pdf");
    assert.equal(created.json.original_pdf_available, true);
    assert.equal(created.json.published_at, "2026-09-01T00:00:00Z");
    assert.deepEqual(created.json.permissions, original.permissions);
    assert.notEqual(created.json.content_sha256, original.content_sha256);

    const replay = await call("POST", `/v2/research/sources/${original.source_id}/reextract`, { body: { text_order: "reading" }, headers: { "Idempotency-Key": key } });
    assert.equal(replay.status, 200);
    assert.equal(replay.headers.get("idempotency-replayed"), "true");
    assert.equal(replay.json.source_id, created.json.source_id);
    const otherOrder = await call("POST", `/v2/research/sources/${original.source_id}/reextract`, { body: { text_order: "layout" }, headers: { "Idempotency-Key": key } });
    assert.equal(otherOrder.status, 409);
    assert.equal(otherOrder.json.error_code, "idempotency_conflict");
    const otherSource = await pdfSource();
    const otherOriginal = await call("POST", `/v2/research/sources/${otherSource.source_id}/reextract`, { body: { text_order: "reading" }, headers: { "Idempotency-Key": key } });
    assert.equal(otherOriginal.status, 409);

    const plain = await call("POST", `/v2/research/sources/${original.source_id}/reextract`, { body: { text_order: "layout" } });
    assert.equal(plain.status, 201);
    assert.notEqual(plain.json.source_id, created.json.source_id);
    const text = await textSource();
    const notPdf = await call("POST", `/v2/research/sources/${text.source_id}/reextract`, { body: { text_order: "layout" } });
    assert.equal(notPdf.status, 422);
    assert.equal(notPdf.json.message, "The retained original PDF is unavailable.");
    assert.equal((await call("POST", "/v2/research/sources/source_missing/reextract", { body: { text_order: "layout" } })).status, 404);
    assert.equal((await call("POST", `/v2/research/sources/${original.source_id}/reextract`, { body: {} })).status, 422);
    assert.equal((await call("POST", `/v2/research/sources/${original.source_id}/reextract`, { body: { text_order: "layout" }, headers: { "Idempotency-Key": "short" } })).status, 422);
  });

  test("runs: source selection rules, body source ids and read sources", async () => {
    const attached = await textSource({ title: "Attached note" });
    const extra = await textSource({ title: "Extra note" });
    const topic = await newTopic([attached.source_id]);
    const subset = await startRun(topic.topic_id, { selected_source_ids: ["source_missing"] });
    assert.equal(subset.response.status, 422);
    assert.equal(subset.response.json.error_code, "invalid_request");
    assert.equal(subset.response.json.message, "Selected sources must belong to this research request.");
    for (const selected of [[attached.source_id, attached.source_id], [" "], "nope"]) {
      assert.equal((await startRun(topic.topic_id, { selected_source_ids: selected })).response.status, 422, JSON.stringify(selected));
    }
    assert.equal((await startRun(topic.topic_id, { source_ids: ["source_missing"] })).response.status, 404);
    assert.equal((await startRun(topic.topic_id, { source_ids: "x" })).response.status, 422);

    // Selecting nothing: no findings, a no_cited_claims gap and no summarized sources.
    const none = await startRun(topic.topic_id, { selected_source_ids: [] });
    assert.equal(none.response.status, 202);
    await waitForTerminal(topic.topic_id, none.response.json.operation.operation_id);
    const afterNone = (await call("GET", topicPath(topic.topic_id))).json;
    const noneTurn = afterNone.versions.at(-1).content.turns.at(-1);
    assert.deepEqual(noneTurn.gaps.map((gap) => gap.code), ["no_cited_claims"]);
    assert.equal(afterNone.versions.at(-1).content.sources.length, 0);

    // A body source id outside the selection is still merged into the version; the selection decides what is read.
    const picked = await startRun(topic.topic_id, { source_ids: [extra.source_id], selected_source_ids: [extra.source_id] });
    assert.equal(picked.response.status, 202);
    await waitForTerminal(topic.topic_id, picked.response.json.operation.operation_id);
    const afterPicked = (await call("GET", topicPath(topic.topic_id))).json;
    assert.deepEqual(afterPicked.versions.at(-1).source_ids, [attached.source_id, extra.source_id]);
    assert.deepEqual(afterPicked.versions.at(-1).content.sources.map((row) => row.source_id), [extra.source_id]);
    assert.equal((await call("GET", `${topicPath(topic.topic_id)}/versions/${afterPicked.versions.at(-1).version_id}/sources/${extra.source_id}/text`)).status, 200);
  });

  test("runs: source discovery follows the explicit flag, then the omitted-key rule", async () => {
    const leads = [
      "https://www.sec.gov/Archives/edgar/data/1000001/000100000126000012/nwex-20260630.htm",
      "https://news.example.com/northwind-capex",
    ];
    const ran = async (extra) => {
      const topic = await newTopic([], "发现来源测试");
      const { response } = await startRun(topic.topic_id, extra);
      assert.equal(response.status, 202);
      const operationId = response.json.operation.operation_id;
      const events = [];
      const final = await waitForTerminal(topic.topic_id, operationId, { collect: events });
      assert.equal(final.operation_status, "completed");
      const saved = (await call("GET", topicPath(topic.topic_id))).json.versions.at(-1);
      return { run: runRecord(operationId), events, final, saved, turn: saved.content.turns.at(-1) };
    };

    const on = await ran({ discover_sources: true });
    assert.equal(on.run.resolvedDiscover, true);
    const discovery = on.events.find((event) => event.kind === "tool" && event.name === "news_search");
    assert.equal(discovery.status, "completed");
    assert.equal(discovery.rationale, "搜索公开线索");
    assert.ok(on.final.usage.search_calls >= 1);
    assert.ok(on.turn.usage.search_calls >= 1);
    assert.ok(on.turn.events.some((event) => event.kind === "tool" && event.name === "news_search"));
    assert.equal(on.turn.search_status, "completed");
    assert.deepEqual(on.saved.content.source_candidates.map((row) => row.url), leads);
    assert.deepEqual(Object.keys(on.saved.content.source_candidates[0]).sort(), ["published_at", "retrieved_at", "search_provider", "search_result_hash", "status", "title", "url"]);

    for (const extra of [{ discover_sources: null }, { discover_sources: false }, { max_search_calls: 0 }, { discover_sources: true, max_search_calls: 0 }]) {
      const off = await ran(extra);
      assert.equal(off.events.some((event) => event.name === "news_search"), false, JSON.stringify(extra));
      assert.equal(off.turn.search_status, "not_requested");
      assert.equal(off.turn.usage.search_calls, 0);
      assert.deepEqual(off.saved.content.source_candidates, []);
    }
    assert.equal((await ran({ discover_sources: null })).run.resolvedDiscover, false);

    // The helper pins discover_sources: false, so build omitted-key bodies by hand.
    const omitted = async (extra) => {
      const topic = await newTopic([], "发现来源测试");
      const body = { parent_version_id: topic.versions.at(-1).version_id, ...extra };
      const response = await call("POST", `${topicPath(topic.topic_id)}/runs`, { body, headers: { "Idempotency-Key": randomUUID() } });
      assert.equal(response.status, 202);
      const operationId = response.json.operation.operation_id;
      await waitForTerminal(topic.topic_id, operationId);
      return runRecord(operationId);
    };
    assert.equal((await omitted({})).resolvedDiscover, true);
    assert.equal((await omitted({ max_search_calls: 3 })).resolvedDiscover, true);
    assert.equal((await omitted({ selected_source_ids: [] })).resolvedDiscover, false);
    assert.equal((await omitted({ selected_source_ids: [], max_search_calls: 2 })).resolvedDiscover, true);
    assert.equal((await omitted({ max_search_calls: 0 })).resolvedDiscover, false);
    const omittedRun = await omitted({});
    assert.equal(omittedRun.discoverActive, true);
    assert.ok(omittedRun.events.some((event) => event.name === "news_search"));

    // A candidate URL that is already saved is not duplicated by a later discovery.
    const topic = await newTopic([], "发现来源测试");
    for (let i = 0; i < 2; i += 1) {
      const { response } = await startRun(topic.topic_id, { discover_sources: true, question_override: `第 ${i + 1} 轮` });
      await waitForTerminal(topic.topic_id, response.json.operation.operation_id);
    }
    const saved = (await call("GET", topicPath(topic.topic_id))).json.versions.at(-1);
    assert.equal(saved.content.source_candidates.length, 2);
    assert.equal(saved.content.turns.length, 2);
  });
  // ---- Versions and calculations (fresh account per test: seeded topic A, latest = 3 versions) ----

  const versionPath = (topic) => `${topicPath(topic.topic_id)}/versions`;

  /** Runs `fn` with a fresh account's seeded topic A, then restores the suite session. */
  async function withTopicA(fn) {
    const saved = token;
    token = (await login()).token;
    try {
      const topic = (await call("GET", "/v2/research/topics")).json.find((t) => t.request.question.startsWith("Northwind"));
      await fn(topic, topic.versions.at(-1));
    } finally {
      token = saved;
    }
  }

  const saveVersion = (topic, parent, content, extra = {}) =>
    call("POST", versionPath(topic), { body: { parent_version_id: parent.version_id, content, ...extra } });

  test("get version: seeded history differs per version; unknown version or topic is 404", async () => {
    await withTopicA(async (topic, latest) => {
      assert.equal(topic.versions.length, 3);
      const [v1, v2, v3] = topic.versions;
      const read = await call("GET", `${versionPath(topic)}/${v3.version_id}`);
      assert.equal(read.status, 200);
      assert.deepEqual(read.json, v3);
      assert.deepEqual(v1.content, {});
      assert.equal(v2.content.claims.length, 0);
      assert.equal(v2.content.facts.length, 2);
      assert.equal(v3.content.claims.length, 4);
      assert.equal(v3.content.facts.length, 6);
      assert.equal(v3.content.calculations.length, 4);
      assert.equal(latest.version_id, v3.version_id);
      const missing = await call("GET", `${versionPath(topic)}/nope`);
      assert.equal(missing.status, 404);
      assert.equal(missing.json.error_code, "not_found");
      assert.equal((await call("GET", `${topicPath("nope")}/versions/${v3.version_id}`)).status, 404);
      assert.equal((await call("GET", `${versionPath(topic)}/${v3.version_id}`, { auth: false })).status, 401);
    });
  });

  test("seeded topic A offers growth, margin and a legacy percent difference", async () => {
    await withTopicA(async (topic, latest) => {
      const text = (await call("GET", `${versionPath(topic)}/${latest.version_id}/sources/${S1_ID}/text`)).json.text;
      const byId = Object.fromEntries(latest.content.facts.map((fact) => [fact.fact_id, fact]));
      for (const fact of Object.values(byId)) assert.equal(text.slice(fact.character_start, fact.character_end), fact.quotation);
      assert.deepEqual([byId.F1.period, byId.F2.period, byId.F3.period], ["FY2026", "FY2025", "FY2026"]);
      assert.equal(byId.F5.unit, "percent");
      assert.equal(byId.F5.currency, null);
      const legacy = latest.content.calculations[3];
      assert.equal(legacy.formula_id, "difference");
      assert.equal(legacy.legacy_unit_warning, "percentage_difference_requires_recalculation");
      assert.deepEqual(legacy.inputs.map((input) => input.fact_id), ["F5", "F6"]);
      // Unflagged seeded calculations carry no legacy warning.
      assert.equal(latest.content.calculations.slice(0, 3).some((calc) => calc.legacy_unit_warning), false);
    });
  });

  test("create version: review, note and summary change keep findings untouched", async () => {
    await withTopicA(async (topic, latest) => {
      const content = { ...structuredClone(latest.content), summary: "我的研究摘要" };
      const response = await saveVersion(topic, latest, content, { review: "confirmed", note: "复核通过" });
      assert.equal(response.status, 201);
      assert.equal(response.json.versions.length, 4);
      const saved = response.json.versions.at(-1);
      assert.match(saved.version_id, /^version_[0-9a-f]{32}$/);
      assert.equal(saved.created_by, "user");
      assert.equal(saved.review, "confirmed");
      assert.equal(saved.note, "复核通过");
      assert.equal(saved.parent_version_id, latest.version_id);
      assert.deepEqual(saved.source_ids, latest.source_ids);
      assert.equal(saved.content.summary, "我的研究摘要");
      assert.deepEqual(saved.content.claims, latest.content.claims);
      assert.equal(saved.content.claims.some((claim) => claim.edited_by_user), false);
      assert.deepEqual(saved.content.turns, latest.content.turns);
      assert.equal(response.json.updated_at, saved.created_at);
      // Defaults and strict bodies.
      const bare = await saveVersion(response.json, saved, saved.content);
      assert.equal(bare.json.versions.at(-1).review, "draft");
      assert.equal(bare.json.versions.at(-1).note, "");
      assert.equal((await saveVersion(topic, latest, content, { extra: 1 })).status, 422);
      assert.equal((await saveVersion(topic, latest, content, { review: "approved" })).status, 422);
      assert.equal((await saveVersion(topic, latest, { ...content, summary: "x".repeat(100001) })).status, 422);
      assert.equal((await saveVersion(topic, latest, content, { note: "x".repeat(10001) })).status, 422);
      assert.equal((await saveVersion(topic, latest, { ...content, bogus: [] })).status, 422);
    });
  });

  test("create version: claim edits, new claims, deletions and citation checks", async () => {
    await withTopicA(async (topic, latest) => {
      const source = (await call("GET", `${versionPath(topic)}/${latest.version_id}/sources/${S1_ID}/text`)).json.text;
      const [first, ...rest] = latest.content.claims;

      // Edited text keeps the id and turn, marks the claim and leaves the others alone.
      const edited = await saveVersion(topic, latest, { ...latest.content, claims: [{ ...first, text: "改写后的主张" }, ...rest] });
      assert.equal(edited.status, 201);
      const claims = edited.json.versions.at(-1).content.claims;
      assert.equal(claims[0].claim_id, first.claim_id);
      assert.equal(claims[0].text, "改写后的主张");
      assert.equal(claims[0].edited_by_user, true);
      assert.equal(claims[0].turn_id, first.turn_id);
      assert.deepEqual(claims.slice(1), rest);
      assert.equal(claims.slice(1).some((claim) => claim.edited_by_user), false);

      // A new claim gets a server id, no turn, and may challenge a temporary or existing id.
      const quotation = "Management expects capacity investment to remain elevated in the next fiscal year.";
      const start = source.indexOf(quotation);
      assert.ok(start > 0);
      const added = {
        claim_id: "tmp-1",
        role: "counterargument",
        text: "管理层预期投资将保持高位。",
        citations: [{ source_id: S1_ID, quotation, location: { character_start: start, character_end: start + quotation.length } }],
        challenges_claim_ids: ["C1"],
      };
      const withNew = await saveVersion(topic, edited.json.versions.at(-1), { ...edited.json.versions.at(-1).content, claims: [...claims, added] });
      assert.equal(withNew.status, 201);
      const created = withNew.json.versions.at(-1).content.claims.at(-1);
      assert.match(created.claim_id, /^claim-[0-9a-f]{32}$/);
      assert.equal(created.turn_id, null);
      assert.equal(created.edited_by_user, true);
      assert.deepEqual(created.challenges_claim_ids, ["C1"]);
      const latest2 = withNew.json.versions.at(-1);

      // Citation that does not match the source text, or that points at an unattached source.
      const bad = { ...added, citations: [{ ...added.citations[0], quotation: "Not in the source." }] };
      const mismatch = await saveVersion(withNew.json, latest2, { ...latest2.content, claims: [...latest2.content.claims, bad] });
      assert.equal(mismatch.status, 422);
      assert.equal(mismatch.json.error_code, "invalid_request");
      assert.equal(mismatch.json.message, "Citation must match an attached, displayable, time-eligible source.");
      const wrongSource = { ...added, citations: [{ ...added.citations[0], source_id: S4_ID }] };
      assert.equal((await saveVersion(withNew.json, latest2, { ...latest2.content, claims: [...latest2.content.claims, wrongSource] })).status, 422);
      // Duplicate ids and bad challenge targets.
      const dup = await saveVersion(withNew.json, latest2, { ...latest2.content, claims: [...latest2.content.claims, latest2.content.claims[0]] });
      assert.equal(dup.json.message, "Research findings or their citations are invalid.");
      const selfChallenge = await saveVersion(withNew.json, latest2, { ...latest2.content, claims: [...latest2.content.claims, { ...added, claim_id: "tmp-2", challenges_claim_ids: ["tmp-2"] }] });
      assert.equal(selfChallenge.json.message, "A challenged finding must be another existing finding.");
      const unknownChallenge = await saveVersion(withNew.json, latest2, { ...latest2.content, claims: [...latest2.content.claims, { ...added, claim_id: "tmp-3", challenges_claim_ids: ["missing"] }] });
      assert.equal(unknownChallenge.status, 422);

      // Omitted claims are deleted.
      const deleted = await saveVersion(withNew.json, latest2, { ...latest2.content, claims: latest2.content.claims.slice(1) });
      assert.equal(deleted.status, 201);
      assert.equal(deleted.json.versions.at(-1).content.claims.length, latest2.content.claims.length - 1);
    });
  });

  test("create version: server-produced records, stale parent and unknown topic", async () => {
    await withTopicA(async (topic, latest) => {
      for (const key of ["turns", "sources", "source_candidates", "gaps"]) {
        const changed = key === "gaps" ? [{ code: "x", message: "y" }] : [...structuredClone(latest.content[key]).slice(1)];
        const response = await saveVersion(topic, latest, { ...latest.content, [key]: changed.length || key === "gaps" ? changed : [{}] });
        assert.equal(response.status, 422, key);
        assert.equal(response.json.message, "Server-produced research records cannot be edited.", key);
      }
      // A missing key equals the parent's empty default: topic A's gaps are [] so omitting them is fine.
      const { gaps: _gaps, ...withoutGaps } = latest.content;
      assert.equal((await saveVersion(topic, latest, withoutGaps)).status, 201);

      const stale = await saveVersion(topic, latest, latest.content);
      assert.equal(stale.status, 409);
      assert.equal(stale.json.error_code, "lifecycle_conflict");
      assert.equal(stale.json.message, "Research topic has changed; reload before saving.");
      const missing = await call("POST", `${topicPath("nope")}/versions`, { body: { parent_version_id: latest.version_id, content: {} } });
      assert.equal(missing.status, 404);
      assert.equal((await call("POST", versionPath(topic), { auth: false, body: {} })).status, 401);
    });
  });

  test("create version: edited facts are rechecked and downgraded; edited calculations become user-authored", async () => {
    await withTopicA(async (topic, latest) => {
      const [f1, ...otherFacts] = latest.content.facts;
      // Value present in the quotation (ignoring thousands separators).
      const sameQuote = await saveVersion(topic, latest, { ...latest.content, facts: [{ ...f1, label: "总收入（改标签）" }, ...otherFacts] });
      assert.equal(sameQuote.status, 201);
      const kept = sameQuote.json.versions.at(-1).content.facts[0];
      assert.equal(kept.verification, "user_edited_not_independently_verified");
      assert.equal(kept.turn_id, f1.turn_id);
      assert.deepEqual(sameQuote.json.versions.at(-1).content.facts.slice(1), otherFacts);
      // The dependent growth calculation is unchanged and stays untouched except for its recomputed input status.
      const growth = sameQuote.json.versions.at(-1).content.calculations[0];
      assert.equal(growth.value, "17.79");
      assert.equal(growth.authorship, undefined);

      // A value that is not in the quotation is flagged and cannot feed calculations.
      const latest2 = sameQuote.json.versions.at(-1);
      const wrongValue = await saveVersion(sameQuote.json, latest2, { ...latest2.content, facts: [{ ...kept, value: "99999" }, ...latest2.content.facts.slice(1)] });
      assert.equal(wrongValue.status, 201);
      const latest3 = wrongValue.json.versions.at(-1);
      assert.equal(latest3.content.facts[0].verification, "user_edited_value_not_in_quotation");
      assert.equal(latest3.content.calculations[1].input_status, "changed"); // margin FY26 uses F1
      assert.equal(latest3.content.calculations[0].input_status, "unverifiable"); // seeded unverifiable stays closed
      const calc = await call("POST", `${topicPath(topic.topic_id)}/calculations`, {
        body: { parent_version_id: latest3.version_id, formula: "growth_percent", first_fact_id: "F1", second_fact_id: "F2" },
      });
      assert.equal(calc.status, 422);
      assert.equal(calc.json.message, "The fact's source quotation requires review before calculation.");

      // Facts with a span that does not match the source are rejected; duplicates too.
      const badSpan = { ...kept, fact_id: "F-new", character_start: 0, character_end: 5, location: { character_start: 0, character_end: 5 } };
      const spanResponse = await saveVersion(wrongValue.json, latest3, { ...latest3.content, facts: [...latest3.content.facts, badSpan] });
      assert.equal(spanResponse.status, 422);
      assert.equal(spanResponse.json.message, "Research fact must use an attached, displayable, time-eligible real source.");
      assert.equal((await saveVersion(wrongValue.json, latest3, { ...latest3.content, facts: [...latest3.content.facts, latest3.content.facts[1]] })).status, 422);

      // An edited calculation keeps its id and turn, loses the legacy warning and is user-authored.
      const legacy = latest3.content.calculations[3];
      const editedCalcs = [...latest3.content.calculations];
      editedCalcs[3] = { ...legacy, value: "-0.40" };
      const calcEdit = await saveVersion(wrongValue.json, latest3, { ...latest3.content, calculations: editedCalcs });
      assert.equal(calcEdit.status, 201);
      const saved = calcEdit.json.versions.at(-1).content.calculations[3];
      assert.equal(saved.calculation_id, legacy.calculation_id);
      assert.equal(saved.turn_id, legacy.turn_id);
      assert.equal(saved.authorship, "user");
      assert.equal(saved.verification, "user_edited_not_independently_verified");
      assert.equal(saved.legacy_unit_warning, null);
      assert.equal(saved.result_status, "mismatch");
    });
  });

  const CALC_KEYS = ["calculation_id", "formula_id", "formula_version", "expression", "value", "unit", "currency", "decimal_places", "rounding", "inputs", "input_verification", "input_precision_status", "period_order_check", "period_granularity_check", "input_status", "result_status", "turn_id", "legacy_unit_warning", "authorship"];
  const calculate = (topic, parent, extra) =>
    call("POST", `${topicPath(topic.topic_id)}/calculations`, { body: { parent_version_id: parent.version_id, ...extra } });

  test("calculations: growth, margin and difference give exact half-up decimal strings", async () => {
    await withTopicA(async (topic, latest) => {
      const growth = await calculate(topic, latest, { formula: "growth_percent", first_fact_id: "F1", second_fact_id: "F2" });
      assert.equal(growth.status, 201);
      assert.equal(growth.json.versions.length, 4);
      const version = growth.json.versions.at(-1);
      assert.equal(version.created_by, "user");
      assert.equal(version.review, "draft");
      assert.equal(version.note, latest.note);
      assert.deepEqual(version.source_ids, latest.source_ids);
      assert.equal(version.content.calculations.length, latest.content.calculations.length + 1);
      assert.deepEqual(version.content.calculations.slice(0, -1), latest.content.calculations);
      const created = version.content.calculations.at(-1);
      assert.deepEqual(Object.keys(created).sort(), [...CALC_KEYS].sort());
      assert.match(created.calculation_id, /^calc-[0-9a-f]{32}$/);
      assert.equal(created.value, "17.79");
      assert.equal(created.unit, "percent");
      assert.equal(created.currency, null);
      assert.equal(created.expression, "(first / second - 1) * 100");
      assert.equal(created.formula_version, "1");
      assert.equal(created.rounding, "half_up");
      assert.equal(created.decimal_places, 2);
      assert.equal(created.turn_id, null);
      assert.equal(created.authorship, "calculation_engine");
      assert.equal(created.input_status, "matched");
      assert.equal(created.result_status, "matched");
      assert.equal(created.period_order_check, "recognized_formats_only");
      assert.deepEqual(created.inputs.map((input) => input.fact_id), ["F1", "F2"]);
      assert.deepEqual(Object.keys(created.inputs[0]), ["fact_id", "value", "unit", "currency", "period"]);

      const precise = await calculate(topic, version, { formula: "growth_percent", first_fact_id: "F1", second_fact_id: "F2", decimal_places: 4 });
      assert.equal(precise.json.versions.at(-1).content.calculations.at(-1).value, "17.7902");
      const whole = await calculate(topic, precise.json.versions.at(-1), { formula: "growth_percent", first_fact_id: "F1", second_fact_id: "F2", decimal_places: 0 });
      assert.equal(whole.json.versions.at(-1).content.calculations.at(-1).value, "18");

      const margin = await calculate(topic, whole.json.versions.at(-1), { formula: "margin_percent", first_fact_id: "F3", second_fact_id: "F1" });
      const marginCalc = margin.json.versions.at(-1).content.calculations.at(-1);
      assert.equal(marginCalc.value, "31.07");
      assert.equal(marginCalc.expression, "first / second * 100");
      assert.equal(marginCalc.period_order_check, null);

      const diff = await calculate(topic, margin.json.versions.at(-1), { formula: "difference", first_fact_id: "F1", second_fact_id: "F2" });
      const diffCalc = diff.json.versions.at(-1).content.calculations.at(-1);
      assert.equal(diffCalc.value, "7342.00");
      assert.equal(diffCalc.unit, "millions");
      assert.equal(diffCalc.currency, "USD");
      const pp = await calculate(topic, diff.json.versions.at(-1), { formula: "difference", first_fact_id: "F5", second_fact_id: "F6", decimal_places: 1 });
      const ppCalc = pp.json.versions.at(-1).content.calculations.at(-1);
      assert.equal(ppCalc.value, "-0.4");
      assert.equal(ppCalc.unit, "percentage points");
      assert.equal(ppCalc.formula_version, "2");
      assert.equal(ppCalc.currency, null);
    });
  });

  test("calculations: rounding is half-up away from zero on exact decimals", async () => {
    const source = await textSource({ title: "Rounding note", text: "Revenue FY2025 was 100 and FY2026 was 99.5 and FY2027 was 0.1 and FY2028 was 0.3." });
    const topic = await newTopic([source.source_id]);
    const fact = (id, quotation, value, period) => {
      const start = source.text.indexOf(quotation);
      const location = { character_start: start, character_end: start + quotation.length };
      return { fact_id: id, label: id, value, unit: "millions", currency: "USD", period, source_id: source.source_id, quotation, ...location, location, verification: "x" };
    };
    const facts = [fact("a", "FY2025 was 100", "100", "FY2025"), fact("b", "FY2026 was 99.5", "99.5", "FY2026"),
      fact("c", "FY2027 was 0.1", "0.1", "FY2027"), fact("d", "FY2028 was 0.3", "0.3", "FY2028")];
    const saved = await saveVersion(topic, topic.versions[0], { facts });
    assert.equal(saved.status, 201);
    let latest = saved.json.versions.at(-1);
    assert.equal(latest.content.facts[0].turn_id, null);
    const run = async (extra) => {
      const response = await calculate(saved.json, latest, extra);
      assert.equal(response.status, 201);
      latest = response.json.versions.at(-1);
      return latest.content.calculations.at(-1).value;
    };
    // (99.5 / 100 - 1) * 100 = -0.5
    assert.equal(await run({ formula: "growth_percent", first_fact_id: "b", second_fact_id: "a", decimal_places: 0 }), "-1");
    assert.equal(await run({ formula: "growth_percent", first_fact_id: "b", second_fact_id: "a", decimal_places: 1 }), "-0.5");
    // (0.3 / 0.1 - 1) * 100 = 200 exactly (float math would give 199.99999999999997)
    assert.equal(await run({ formula: "growth_percent", first_fact_id: "d", second_fact_id: "c", decimal_places: 8 }), "200.00000000");
    // 0.1 - 0.3 = -0.2 exactly; 100 - 99.5 = 0.5 -> "1" half-up
    assert.equal(await run({ formula: "difference", first_fact_id: "c", second_fact_id: "d", decimal_places: 3 }), "-0.200");
    assert.equal(await run({ formula: "difference", first_fact_id: "a", second_fact_id: "b", decimal_places: 0 }), "1");
    // 100 - 0.1 = 99.9 exactly
    assert.equal(await run({ formula: "difference", first_fact_id: "a", second_fact_id: "c", decimal_places: 2 }), "99.90");
  });

  test("calculations: rejected inputs are 422 and stale parents are 409", async () => {
    await withTopicA(async (topic, latest) => {
      const rejected = async (extra, message) => {
        const response = await calculate(topic, latest, extra);
        assert.equal(response.status, 422, JSON.stringify(extra));
        assert.equal(response.json.error_code, message ? "invalid_request" : "validation_error", JSON.stringify(extra));
        if (message) assert.equal(response.json.message, message, JSON.stringify(extra));
      };
      const failed = "Calculation cannot be performed with the current facts.";
      await rejected({ formula: "margin_percent", first_fact_id: "F3", second_fact_id: "F2" }, failed); // different periods
      await rejected({ formula: "growth_percent", first_fact_id: "F1", second_fact_id: "F3" }, failed); // same period
      await rejected({ formula: "growth_percent", first_fact_id: "F2", second_fact_id: "F1" }, failed); // baseline later than current
      await rejected({ formula: "growth_percent", first_fact_id: "F1", second_fact_id: "F1" }, failed); // same fact
      await rejected({ formula: "growth_percent", first_fact_id: "F1", second_fact_id: "F5" }, failed); // unit mismatch
      await rejected({ formula: "growth_percent", first_fact_id: "F1", second_fact_id: "nope" }, "Calculation input facts are missing or ambiguous.");
      await rejected({ formula: "growth_percent", first_fact_id: "nope", second_fact_id: "F1" }, "Calculation input facts are missing or ambiguous.");
      await rejected({ formula: "ratio", first_fact_id: "F1", second_fact_id: "F2" });
      await rejected({ formula: "growth_percent", first_fact_id: "F1", second_fact_id: "F2", decimal_places: 9 });
      await rejected({ formula: "growth_percent", first_fact_id: "F1", second_fact_id: "F2", decimal_places: 1.5 });
      await rejected({ formula: "growth_percent", first_fact_id: "F1", second_fact_id: "F2", decimal_places: "2" });
      await rejected({ formula: "growth_percent", first_fact_id: "F1", second_fact_id: "F2", extra: true });
      assert.equal((await call("GET", topicPath(topic.topic_id))).json.versions.length, topic.versions.length);

      const ok = await calculate(topic, latest, { formula: "growth_percent", first_fact_id: "F1", second_fact_id: "F2" });
      assert.equal(ok.status, 201);
      const stale = await calculate(topic, latest, { formula: "growth_percent", first_fact_id: "F1", second_fact_id: "F2" });
      assert.equal(stale.status, 409);
      assert.equal(stale.json.error_code, "lifecycle_conflict");
      assert.equal((await call("POST", `${topicPath("nope")}/calculations`, { body: { parent_version_id: "x", formula: "difference", first_fact_id: "a", second_fact_id: "b" } })).status, 404);
    });
  });

  test("recalculate: clears the legacy warning and uses percentage points; bad index and stale parent fail", async () => {
    await withTopicA(async (topic, latest) => {
      const index = latest.content.calculations.findIndex((calc) => calc.legacy_unit_warning);
      assert.equal(index, 3);
      const old = latest.content.calculations[index];
      const recalc = (parent, calculation_index) =>
        call("POST", `${topicPath(topic.topic_id)}/calculations/recalculate`, { body: { parent_version_id: parent.version_id, calculation_index } });
      const response = await recalc(latest, index);
      assert.equal(response.status, 201);
      const version = response.json.versions.at(-1);
      assert.equal(version.created_by, "user");
      assert.equal(version.parent_version_id, latest.version_id);
      assert.equal(version.content.calculations.length, latest.content.calculations.length);
      const fresh = version.content.calculations[index];
      assert.equal(fresh.calculation_id, old.calculation_id);
      assert.equal(fresh.turn_id, old.turn_id);
      assert.equal(fresh.legacy_unit_warning, null);
      assert.equal(fresh.unit, "percentage points");
      assert.equal(fresh.formula_version, "2");
      assert.equal(fresh.value, "-0.38");
      assert.equal(fresh.expression, "first - second");
      assert.equal(fresh.input_status, "matched");
      assert.equal(fresh.result_status, "matched");
      assert.equal(fresh.authorship, "calculation_engine");
      assert.deepEqual(version.content.calculations.filter((_, i) => i !== index), latest.content.calculations.filter((_, i) => i !== index));

      // The old version is untouched; recalculating against it is stale.
      assert.equal((await recalc(latest, index)).status, 409);
      const latest2 = version;
      const outOfRange = await recalc(latest2, latest2.content.calculations.length);
      assert.equal(outOfRange.status, 422);
      assert.equal(outOfRange.json.message, "Calculation does not exist in this version.");
      assert.equal((await recalc(latest2, -1)).status, 422);
      assert.equal((await recalc(latest2, 2000)).status, 422);
      assert.equal((await recalc(latest2, 1.5)).status, 422);
      assert.equal((await call("POST", `${topicPath(topic.topic_id)}/calculations/recalculate`, { body: { parent_version_id: latest2.version_id, calculation_index: 0, extra: 1 } })).status, 422);
      // Recalculating an ordinary calculation keeps its value.
      const again = await recalc(latest2, 0);
      assert.equal(again.status, 201);
      assert.equal(again.json.versions.at(-1).content.calculations[0].value, "17.79");
      assert.equal(again.json.versions.at(-1).content.calculations[0].calculation_id, "C-growth");
    });
  });

  test("versions and recalculate: all 9 FormulaId values, multi-fact inputs, optional fields and contract rejections", async () => {
    const text = [
      "FY2022 rev 100; FY2025 rev 133.1; FY2025 rev_hi 200.",
      "EPS 35; shares_den 2.5.",
      "2025-Q1 10; 2025-Q2 12; 2025-Q3 15; 2025-Q4 23.",
      "Scale bn 2; Scale mn 500; Growth base 100; Margin num 25.",
    ].join(" ");
    const source = await textSource({ title: "9-formula sheet", text });
    const topic = await newTopic([source.source_id], "9 公式契约测试");
    const mkFact = (id, token, value, unit, currency, period) => {
      const start = text.indexOf(token);
      assert.ok(start >= 0, token);
      const location = { character_start: start, character_end: start + token.length };
      return { fact_id: id, label: id, value, unit, currency, period, source_id: source.source_id, quotation: token, ...location, location, verification: "x" };
    };
    const facts = [
      mkFact("f0", "FY2022 rev 100", "100", "USD millions", "USD", "FY2022"),
      mkFact("f3", "FY2025 rev 133.1", "133.1", "USD millions", "USD", "FY2025"),
      mkFact("num", "EPS 35", "35", "USD per share", "USD", "FY2025"),
      mkFact("den", "shares_den 2.5", "2.5", "USD per share", "USD", "FY2025"),
      mkFact("q1", "2025-Q1 10", "10", "USD millions", "USD", "2025-Q1"),
      mkFact("q2", "2025-Q2 12", "12", "USD millions", "USD", "2025-Q2"),
      mkFact("q3", "2025-Q3 15", "15", "USD millions", "USD", "2025-Q3"),
      mkFact("q4", "2025-Q4 23", "23", "USD millions", "USD", "2025-Q4"),
      mkFact("fb", "Scale bn 2", "2", "USD billions", "USD", "FY2025"),
      mkFact("fm", "Scale mn 500", "500", "USD millions", "USD", "FY2025"),
      mkFact("mnum", "Margin num 25", "25", "USD millions", "USD", "FY2022"),
    ];
    const factMap = Object.fromEntries(facts.map((f) => [f.fact_id, f]));
    const snap = (id) => {
      const f = factMap[id];
      return { fact_id: f.fact_id, value: f.value, unit: f.unit, currency: f.currency, period: f.period };
    };
    const baseCalc = (id, formula_id, inputIds, extra) => ({
      calculation_id: id,
      formula_id,
      formula_version: "1",
      input_verification: "not_verified_by_calculator",
      input_precision_status: "not_established",
      inputs: inputIds.map(snap),
      rounding: "half_up",
      decimal_places: 2,
      ...extra,
    });
    const calculations = [
      baseCalc("c-growth", "growth_percent", ["f3", "f0"], { expression: "(first / second - 1) * 100", value: "33.10", unit: "percent", currency: null, period_order_check: "recognized_formats_only", period_granularity_check: "not_established" }),
      baseCalc("c-margin", "margin_percent", ["mnum", "f0"], { expression: "first / second * 100", value: "25.00", unit: "percent", currency: null }),
      baseCalc("c-diff", "difference", ["f3", "f0"], { expression: "first - second", value: "33.10", unit: "USD millions", currency: "USD" }),
      baseCalc("c-cagr", "cagr", ["f3", "f0"], { expression: "((first / second) ** (1 / periods) - 1) * 100", value: "10.00", unit: "percent", currency: null, periods: "3", period_order_check: "recognized_formats_only", period_granularity_check: "not_established" }),
      baseCalc("c-ratio", "ratio", ["num", "den"], { expression: "first / second", value: "14.00", unit: "ratio", currency: null }),
      baseCalc("c-sum", "sum", ["q1", "q2", "q3"], { expression: "sum(inputs)", value: "37.00", unit: "USD millions", currency: "USD" }),
      baseCalc("c-sub", "subtract", ["q4", "q1", "q2"], { expression: "inputs[0] - sum(inputs[1:])", value: "1.00", unit: "USD millions", currency: "USD" }),
      baseCalc("c-wavg", "weighted_average", ["q1", "q4"], { expression: "sum(w_i * x_i) / sum(w_i)", value: "19.75", unit: "USD millions", currency: "USD", weights: ["1", "3"] }),
      baseCalc("c-ttm", "ttm_sum", ["q1", "q2", "q3", "q4"], { expression: "sum(ttm_quarters)", value: "60.00", unit: "USD millions", currency: "USD", period_order_check: "recognized_formats_only" }),
      baseCalc("c-scale", "sum", ["fb", "fm"], {
        expression: "sum(inputs)",
        value: "2500.00",
        unit: "USD millions",
        currency: "USD",
        scale_adjustments: [{ fact_id: "fb", from_unit: "USD billions", to_unit: "USD millions", from_scale: "billions", to_scale: "millions", multiplier: "1000", original_value: "2", aligned_value: "2000" }],
      }),
    ];

    const saved = await saveVersion(topic, topic.versions[0], { facts, calculations });
    assert.equal(saved.status, 201);
    const v1 = saved.json.versions.at(-1);
    assert.equal(v1.content.calculations.length, 10);
    for (const calc of v1.content.calculations) {
      assert.equal(calc.input_status, "matched", calc.calculation_id);
      assert.equal(calc.result_status, "matched", calc.calculation_id);
    }

    // Saving an unrelated summary edit preserves optional fields untouched without altering calculation identity.
    const summaryOnly = await saveVersion(saved.json, v1, { ...v1.content, summary: "保留计算" });
    assert.equal(summaryOnly.status, 201);
    const v2 = summaryOnly.json.versions.at(-1);
    assert.deepEqual(v2.content.calculations, v1.content.calculations);

    // Invalid calculation contracts fail with 422 validation_error.
    const badPayloads = [
      baseCalc("bad-formula", "median", ["q1", "q2"], { expression: "x", value: "1", unit: "USD millions", currency: "USD" }),
      baseCalc("bad-cagr-len", "cagr", ["f3", "f0", "q1"], { expression: "x", value: "10.00", unit: "percent", currency: null, periods: "3" }),
      baseCalc("bad-sum-1", "sum", ["q1"], { expression: "sum(inputs)", value: "10.00", unit: "USD millions", currency: "USD" }),
      baseCalc("bad-sum-51", "sum", Array.from({ length: 51 }, (_, i) => i % 2 === 0 ? "q1" : "q2"), { expression: "sum(inputs)", value: "10.00", unit: "USD millions", currency: "USD" }),
      baseCalc("bad-weights-type", "weighted_average", ["q1", "q4"], { expression: "sum(w_i * x_i) / sum(w_i)", value: "19.75", unit: "USD millions", currency: "USD", weights: [1, 3] }),
      baseCalc("bad-scale-extra", "sum", ["fb", "fm"], {
        expression: "sum(inputs)",
        value: "2500.00",
        unit: "USD millions",
        currency: "USD",
        scale_adjustments: [{ fact_id: "fb", from_unit: "USD billions", to_unit: "USD millions", from_scale: "billions", to_scale: "millions", multiplier: "1000", original_value: "2", aligned_value: "2000", extra: "1" }],
      }),
      baseCalc("bad-scale-missing", "sum", ["fb", "fm"], {
        expression: "sum(inputs)",
        value: "2500.00",
        unit: "USD millions",
        currency: "USD",
        scale_adjustments: [{ fact_id: "fb" }],
      }),
    ];
    for (const bad of badPayloads) {
      const res = await saveVersion(summaryOnly.json, v2, { ...v2.content, calculations: [bad] });
      assert.equal(res.status, 422, bad.calculation_id);
      assert.equal(res.json.error_code, "validation_error", bad.calculation_id);
    }

    // Update f3 value (still in source text) and recalculate cagr, weighted_average, ttm_sum, and scale-adjusted sum.
    const editedFacts = facts.map((f) => f.fact_id === "f3" ? mkFact("f3", "FY2025 rev_hi 200", "200", "USD millions", "USD", "FY2025") : f);
    const withUpdatedFact = await saveVersion(summaryOnly.json, v2, { ...v2.content, facts: editedFacts });
    assert.equal(withUpdatedFact.status, 201);
    let currentTopic = withUpdatedFact.json;
    let currentVer = currentTopic.versions.at(-1);
    assert.equal(currentVer.content.calculations[3].input_status, "changed");

    const recalcAt = async (index) => {
      const res = await call("POST", `${topicPath(currentTopic.topic_id)}/calculations/recalculate`, {
        body: { parent_version_id: currentVer.version_id, calculation_index: index },
      });
      assert.equal(res.status, 201, `recalc index ${index}`);
      currentTopic = res.json;
      currentVer = currentTopic.versions.at(-1);
      return currentVer.content.calculations[index];
    };

    const recalcCagr = await recalcAt(3);
    assert.equal(recalcCagr.value, "25.99");
    assert.equal(recalcCagr.periods, "3");
    assert.equal(recalcCagr.input_status, "matched");
    assert.equal(recalcCagr.result_status, "matched");

    const recalcWavg = await recalcAt(7);
    assert.equal(recalcWavg.value, "19.75");
    assert.deepEqual(recalcWavg.weights, ["1", "3"]);
    assert.equal(recalcWavg.result_status, "matched");

    const recalcTtm = await recalcAt(8);
    assert.equal(recalcTtm.value, "60.00");
    assert.equal(recalcTtm.inputs.length, 4);
    assert.equal(recalcTtm.result_status, "matched");

    const recalcScale = await recalcAt(9);
    assert.equal(recalcScale.value, "2500.00");
    assert.equal(recalcScale.scale_adjustments.length, 1);
    assert.equal(recalcScale.scale_adjustments[0].multiplier, "1000");
    assert.equal(recalcScale.result_status, "matched");

    // Unexpected optional metadata (e.g., spurious periods/scale_adjustments on growth_percent) marks result_status mismatch.
    const spurious = baseCalc("c-spurious", "growth_percent", ["f3", "f0"], {
      expression: "(first / second - 1) * 100",
      value: "33.10",
      unit: "percent",
      currency: null,
      periods: "3",
      period_order_check: "recognized_formats_only",
      period_granularity_check: "not_established",
    });
    const savedSpurious = await saveVersion(currentTopic, currentVer, { ...currentVer.content, calculations: [spurious] });
    assert.equal(savedSpurious.status, 201);
    assert.equal(savedSpurious.json.versions.at(-1).content.calculations[0].result_status, "mismatch");
    currentTopic = savedSpurious.json;
    currentVer = currentTopic.versions.at(-1);

    // Recalculating cagr without periods fails closed with 422.
    const cagrNoPeriods = baseCalc("c-cagr-missing", "cagr", ["f3", "f0"], { expression: "((first / second) ** (1 / periods) - 1) * 100", value: "25.99", unit: "percent", currency: null });
    const savedMissingPeriods = await saveVersion(currentTopic, currentVer, { ...currentVer.content, calculations: [cagrNoPeriods] });
    assert.equal(savedMissingPeriods.status, 201);
    const vMissing = savedMissingPeriods.json.versions.at(-1);
    assert.equal(vMissing.content.calculations[0].result_status, "unverifiable");
    const recalcFail = await call("POST", `${topicPath(currentTopic.topic_id)}/calculations/recalculate`, {
      body: { parent_version_id: vMissing.version_id, calculation_index: 0 },
    });
    assert.equal(recalcFail.status, 422);
    assert.equal(recalcFail.json.message, "Calculation cannot be performed with the current facts.");
  });
});

describe("account, model and market-data settings", () => {
  const MODEL_URL = "/v1/account/model-config";
  const MARKET_URL = "/v1/account/market-data-config";
  const PERSONAL = { mode: "personal", protocol: "openai_chat_completions", base_url: "https://llm.example.com/v1", model: "example-model", api_key: "sk-fictional-ABCD1234" };
  const PROVIDERS = ["market_data", "alpha_vantage", "twelve_data", "finnhub", "yahoo"];
  const marketBody = (overrides = {}, order = PROVIDERS) => ({
    fallback_order: order,
    providers: PROVIDERS.map((id) => ({ provider_id: id, enabled: true, ...(overrides[id] ?? {}) })),
  });

  let saved;
  beforeEach(async () => {
    saved = token;
    token = (await login()).token;
  });
  afterEach(() => {
    token = saved;
  });

  test("all endpoints require a bearer token and are never cached", async () => {
    for (const [method, path, body] of [
      ["GET", "/v1/account"],
      ["GET", MODEL_URL],
      ["PUT", MODEL_URL, { mode: "platform" }],
      ["DELETE", MODEL_URL],
      ["POST", `${MODEL_URL}/test`],
      ["GET", MARKET_URL],
      ["PUT", MARKET_URL, marketBody()],
      ["POST", `${MARKET_URL}/test`, { provider_id: "yahoo" }],
    ]) {
      const response = await call(method, path, { auth: false, body });
      assert.equal(response.status, 401, `${method} ${path}`);
      assert.equal(response.json.error_code, "unauthorized");
    }
    for (const path of ["/v1/account", MODEL_URL, MARKET_URL]) {
      assert.equal((await call("GET", path)).headers.get("cache-control"), "no-store");
    }
    assert.equal((await call("PUT", MODEL_URL, { body: PERSONAL })).headers.get("cache-control"), "no-store");
  });

  test("account summary shape and quota_remaining", async () => {
    const response = await call("GET", "/v1/account");
    assert.equal(response.status, 200);
    const summary = response.json;
    assert.deepEqual(Object.keys(summary).sort(), ["account", "base_plan_id", "entitlement_status", "plan", "product_id", "provider_model", "quota_consumed", "quota_limit", "quota_remaining", "quota_reserved", "quota_resets_at", "research_model"]);
    assert.deepEqual(Object.keys(summary.account).sort(), ["account_id", "email"]);
    assert.match(summary.account.account_id, /^acct_/);
    assert.equal(summary.plan, "free");
    assert.equal(summary.entitlement_status, "active");
    assert.equal(summary.product_id, null);
    assert.equal(summary.base_plan_id, null);
    assert.equal(summary.quota_limit, 200000);
    assert.equal(summary.quota_consumed, 18500);
    assert.equal(summary.quota_reserved, 0);
    assert.equal(summary.quota_remaining, 200000 - 18500);
    assert.equal(summary.provider_model, "platform-model-example");
    const now = new Date();
    assert.equal(summary.quota_resets_at, new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1)).toISOString().replace(".000Z", "Z"));
  });

  test("platform runs consume quota; personal-model runs do not", async () => {
    const consumed = async () => (await call("GET", "/v1/account")).json.quota_consumed;
    const runTopic = async () => {
      const topic = await newTopic([S1_ID]);
      const { response } = await startRun(topic.topic_id);
      assert.equal(response.status, 202);
      const final = await waitForTerminal(topic.topic_id, response.json.operation.operation_id);
      assert.equal(final.operation_status, "completed");
      return final.usage.total_tokens;
    };
    const platformTokens = await runTopic();
    assert.ok(platformTokens > 0);
    assert.equal(await consumed(), 18500 + platformTokens);
    assert.equal((await call("GET", "/v1/account")).json.quota_remaining, 200000 - 18500 - platformTokens);

    assert.equal((await call("PUT", MODEL_URL, { body: PERSONAL })).status, 200);
    await runTopic();
    assert.equal(await consumed(), 18500 + platformTokens);
  });

  test("default model config is a platform snapshot without null fields", async () => {
    const response = await call("GET", MODEL_URL);
    assert.equal(response.status, 200);
    assert.deepEqual(response.json, { schema_version: "v1", mode: "platform", configured: true });
  });

  test("personal save returns a masked snapshot and never the key", async () => {
    const response = await call("PUT", MODEL_URL, { body: PERSONAL });
    assert.equal(response.status, 200);
    const { updated_at: updatedAt, ...rest } = response.json;
    assert.deepEqual(rest, { schema_version: "v1", mode: "personal", configured: true, protocol: "openai_chat_completions", base_url: PERSONAL.base_url, model: "example-model", key_hint: "••••1234" });
    assert.ok(!Number.isNaN(Date.parse(updatedAt)));
    const read = await call("GET", MODEL_URL);
    assert.deepEqual(read.json, response.json);
    assert.ok(!JSON.stringify([response.json, read.json]).includes(PERSONAL.api_key));
    // A non-alphanumeric suffix falls back to the last 4 hex of the sha256.
    const odd = await call("PUT", MODEL_URL, { body: { ...PERSONAL, api_key: "sk-fictional-ab_!" } });
    assert.match(odd.json.key_hint, /^••••[0-9a-f]{4}$/);
  });

  test("personal update merges with the stored values and keeps the key", async () => {
    await call("PUT", MODEL_URL, { body: PERSONAL });
    const renamed = await call("PUT", MODEL_URL, { body: { mode: "personal", model: "other-model" } });
    assert.equal(renamed.status, 200);
    assert.equal(renamed.json.model, "other-model");
    assert.equal(renamed.json.key_hint, "••••1234");
    assert.equal(renamed.json.base_url, PERSONAL.base_url);
    // Same origin with a different path needs no key.
    assert.equal((await call("PUT", MODEL_URL, { body: { mode: "personal", base_url: "https://llm.example.com/v2" } })).json.base_url, "https://llm.example.com/v2");
    // Changing origin or protocol requires the key again; supplying it works.
    const moved = { mode: "personal", base_url: "https://other.example.org/v1" };
    const rejected = await call("PUT", MODEL_URL, { body: moved });
    assert.equal(rejected.status, 422);
    assert.equal(rejected.json.error_code, "validation_error");
    assert.equal(rejected.json.message, "Changing model origin or protocol requires supplying the API key again.");
    assert.equal((await call("PUT", MODEL_URL, { body: { mode: "personal", protocol: "anthropic_messages" } })).json.message, "Changing model origin or protocol requires supplying the API key again.");
    const withKey = await call("PUT", MODEL_URL, { body: { ...moved, protocol: "anthropic_messages", api_key: "another-key-WXYZ" } });
    assert.equal(withKey.status, 200);
    assert.deepEqual([withKey.json.protocol, withKey.json.base_url, withKey.json.key_hint], ["anthropic_messages", moved.base_url, "••••WXYZ"]);
    // The failed attempts changed nothing; clearing the key leaves the config incomplete.
    const cleared = await call("PUT", MODEL_URL, { body: { mode: "personal", clear_api_key: true } });
    assert.equal(cleared.status, 422);
    assert.equal(cleared.json.message, "Personal model configuration is incomplete.");
    assert.equal((await call("GET", MODEL_URL)).json.key_hint, "••••WXYZ");
  });

  test("incomplete or invalid personal configurations are 422", async () => {
    const incomplete = await call("PUT", MODEL_URL, { body: { mode: "personal", model: "example-model" } });
    assert.equal(incomplete.status, 422);
    assert.equal(incomplete.json.error_code, "validation_error");
    assert.equal(incomplete.json.message, "Personal model configuration is incomplete.");
    for (const baseUrl of [
      "http://llm.example.com/v1",
      "https://localhost/v1",
      "https://127.0.0.1/v1",
      "https://10.1.2.3/v1",
      "https://192.168.0.5/v1",
      "https://172.20.0.1/v1",
      "https://169.254.169.254/latest",
      "https://[::1]/v1",
      "https://[fe80::1]/v1",
      "https://metadata.google.internal/v1",
      "https://metadata.azure.internal/v1",
      "https://printer.local/v1",
      "https://svc.corp.internal/v1",
      "https://llm.example.com:8443/v1",
      "https://user:secret@llm.example.com/v1",
      "llm.example.com/v1",
    ]) {
      const response = await call("PUT", MODEL_URL, { body: { ...PERSONAL, base_url: baseUrl } });
      assert.equal(response.status, 422, baseUrl);
      assert.equal(response.json.message, "Personal model configuration is invalid.", baseUrl);
    }
    assert.equal((await call("PUT", MODEL_URL, { body: { ...PERSONAL, base_url: "https://llm.example.com:443/v1" } })).status, 200);
    assert.equal((await call("GET", MODEL_URL)).json.mode, "personal");
  });

  test("model-config request validation mirrors the strict pydantic model", async () => {
    for (const body of [
      { mode: "platform", model: "x" },
      { mode: "platform", protocol: "openai_chat_completions" },
      { mode: "platform", clear_api_key: true },
      { mode: "platform", api_key: "abcdefgh" },
      { ...PERSONAL, clear_api_key: true },
      { ...PERSONAL, clear_api_key: "true" },
      { ...PERSONAL, api_key: " sk-fictional-ABCD1234" },
      { ...PERSONAL, model: "example-model " },
      { ...PERSONAL, base_url: " https://llm.example.com/v1" },
      { ...PERSONAL, api_key: "short" },
      { ...PERSONAL, model: "" },
      { ...PERSONAL, model: "m".repeat(129) },
      { ...PERSONAL, protocol: "gemini" },
      { ...PERSONAL, api_key: 12345678 },
      { ...PERSONAL, extra: 1 },
      { model: "x" },
      { mode: "other" },
      [],
    ]) {
      const response = await call("PUT", MODEL_URL, { body });
      assert.equal(response.status, 422, JSON.stringify(body));
      assert.equal(response.json.error_code, "validation_error");
    }
    assert.deepEqual((await call("GET", MODEL_URL)).json, { schema_version: "v1", mode: "platform", configured: true });
  });

  test("switching to platform keeps the personal config for switching back; DELETE resets", async () => {
    const saved = await call("PUT", MODEL_URL, { body: PERSONAL });
    const platform = await call("PUT", MODEL_URL, { body: { mode: "platform" } });
    assert.equal(platform.status, 200);
    assert.equal(platform.json.mode, "platform");
    // The inactive personal configuration is reported masked; the key itself is never returned.
    assert.equal(platform.json.base_url, saved.json.base_url);
    assert.equal(platform.json.key_hint, saved.json.key_hint);
    assert.ok(!JSON.stringify(platform.json).includes(PERSONAL.api_key));
    assert.deepEqual((await call("GET", MODEL_URL)).json, platform.json);
    // The platform test reports unavailable: no personal model is active.
    assert.equal((await call("POST", `${MODEL_URL}/test`)).json.status, "unavailable");
    // Switching back needs no fields.
    const back = await call("PUT", MODEL_URL, { body: { mode: "personal" } });
    assert.equal(back.status, 200);
    assert.equal(back.json.mode, "personal");
    assert.equal(back.json.key_hint, saved.json.key_hint);

    const deleted = await call("DELETE", MODEL_URL);
    assert.equal(deleted.status, 200);
    assert.deepEqual(deleted.json, { schema_version: "v1", mode: "platform", configured: true });
    assert.deepEqual((await call("GET", MODEL_URL)).json, deleted.json);
    assert.equal((await call("PUT", MODEL_URL, { body: { mode: "personal" } })).json.message, "Personal model configuration is incomplete.");
  });

  test("model-config storage failure magic key is a retryable 503", async () => {
    const response = await call("PUT", MODEL_URL, { body: { ...PERSONAL, api_key: "storage-unavailable-key" } });
    assert.equal(response.status, 503);
    assert.equal(response.json.error_code, "model_credential_storage_unavailable");
    assert.equal(response.json.retryable, true);
    assert.equal(response.headers.get("retry-after"), "30");
    assert.deepEqual((await call("GET", MODEL_URL)).json, { schema_version: "v1", mode: "platform", configured: true });
  });

  test("model-config test endpoint is simulated", async () => {
    const status = async () => {
      const response = await call("POST", `${MODEL_URL}/test`);
      assert.equal(response.status, 200);
      assert.deepEqual(Object.keys(response.json), ["status"]);
      return response.json.status;
    };
    assert.equal(await status(), "unavailable"); // platform mode
    await call("PUT", MODEL_URL, { body: PERSONAL });
    assert.equal(await status(), "ready");
    await call("PUT", MODEL_URL, { body: { ...PERSONAL, base_url: "https://down.example.com/v1" } });
    assert.equal(await status(), "unavailable");
    await call("PUT", MODEL_URL, { body: { ...PERSONAL, model: "model-down-1" } });
    assert.equal(await status(), "unavailable");
    await call("PUT", MODEL_URL, { body: { ...PERSONAL, protocol: "anthropic_messages" } });
    assert.equal(await status(), "unavailable");
    await call("PUT", MODEL_URL, { body: { ...PERSONAL, protocol: "anthropic_messages",
      messages_options: { version: "2023-06-01", auth_scheme: "x-api-key", max_tokens: 4096 } } });
    assert.equal(await status(), "ready");
    assert.ok((await call("GET", MODEL_URL)).json.research_tools_verified_at);
    await call("DELETE", MODEL_URL);
    assert.equal(await status(), "unavailable");
  });

  test("default market-data snapshot", async () => {
    const response = await call("GET", MARKET_URL);
    assert.equal(response.status, 200);
    assert.deepEqual(response.json, {
      schema_version: "v1",
      fallback_order: PROVIDERS,
      providers: [
        { provider_id: "market_data", display_name: "Market Data", enabled: true, requires_key: true, personal_key_configured: false, platform_available: false, key_hint: null },
        { provider_id: "alpha_vantage", display_name: "Alpha Vantage", enabled: true, requires_key: true, personal_key_configured: false, platform_available: true, key_hint: null },
        { provider_id: "twelve_data", display_name: "Twelve Data", enabled: true, requires_key: true, personal_key_configured: false, platform_available: false, key_hint: null },
        { provider_id: "finnhub", display_name: "Finnhub", enabled: true, requires_key: true, personal_key_configured: false, platform_available: false, key_hint: null },
        { provider_id: "yahoo", display_name: "Yahoo Finance", enabled: true, requires_key: false, personal_key_configured: false, platform_available: true, key_hint: null },
      ],
      updated_at: null,
    });
  });

  test("market-data update reorders, toggles, sets and clears keys", async () => {
    const order = ["yahoo", "finnhub", "twelve_data", "alpha_vantage", "market_data"];
    const first = await call("PUT", MARKET_URL, { body: marketBody({ finnhub: { api_key: "fh-fictional-key-9Z9Z" }, alpha_vantage: { enabled: false } }, order) });
    assert.equal(first.status, 200);
    assert.deepEqual(first.json.fallback_order, order);
    // `providers` stays in the fixed provider order, as the backend does.
    assert.deepEqual(first.json.providers.map((p) => p.provider_id), PROVIDERS);
    const byId = Object.fromEntries(first.json.providers.map((p) => [p.provider_id, p]));
    assert.equal(byId.alpha_vantage.enabled, false);
    assert.equal(byId.finnhub.personal_key_configured, true);
    assert.equal(byId.finnhub.key_hint, "••••9Z9Z");
    assert.equal(byId.market_data.key_hint, null);
    assert.ok(!Number.isNaN(Date.parse(first.json.updated_at)));
    assert.ok(!JSON.stringify(first.json).includes("fh-fictional-key"));
    assert.deepEqual((await call("GET", MARKET_URL)).json, first.json);

    // Neither api_key nor clear_api_key keeps the stored key.
    const kept = await call("PUT", MARKET_URL, { body: marketBody({}, order) });
    const keptFinnhub = kept.json.providers.find((p) => p.provider_id === "finnhub");
    assert.deepEqual([keptFinnhub.personal_key_configured, keptFinnhub.key_hint], [true, "••••9Z9Z"]);
    assert.equal(kept.json.providers.find((p) => p.provider_id === "alpha_vantage").enabled, true);

    const cleared = await call("PUT", MARKET_URL, { body: marketBody({ finnhub: { clear_api_key: true, enabled: false } }, order) });
    const clearedFinnhub = cleared.json.providers.find((p) => p.provider_id === "finnhub");
    assert.deepEqual([clearedFinnhub.personal_key_configured, clearedFinnhub.key_hint, clearedFinnhub.enabled], [false, null, false]);
  });

  test("market-data request validation", async () => {
    const bodies = [
      marketBody({ yahoo: { api_key: "yahoo-key-12345" } }),
      marketBody({ yahoo: { clear_api_key: true } }),
      marketBody({ finnhub: { api_key: "fh-fictional-key", clear_api_key: true } }),
      marketBody({ finnhub: { api_key: " fh-fictional-key" } }),
      marketBody({ finnhub: { api_key: "short" } }),
      marketBody({ finnhub: { clear_api_key: "yes" } }),
      marketBody({ finnhub: { enabled: "true" } }),
      marketBody({}, ["market_data", "alpha_vantage", "twelve_data", "finnhub"]),
      marketBody({}, ["market_data", "market_data", "twelve_data", "finnhub", "yahoo"]),
      marketBody({}, [...PROVIDERS, "yahoo"]),
      marketBody({}, ["market_data", "alpha_vantage", "twelve_data", "finnhub", "bing"]),
      { ...marketBody(), providers: marketBody().providers.slice(1) },
      { ...marketBody(), providers: [...marketBody().providers.slice(1), { provider_id: "yahoo", enabled: true }] },
      { ...marketBody(), providers: marketBody().providers.map(({ enabled: _enabled, ...rest }) => rest) },
      { ...marketBody(), extra: true },
      { fallback_order: PROVIDERS },
    ];
    for (const body of bodies) {
      const response = await call("PUT", MARKET_URL, { body });
      assert.equal(response.status, 422, JSON.stringify(body));
      assert.equal(response.json.error_code, "validation_error");
    }
    assert.equal((await call("GET", MARKET_URL)).json.updated_at, null);
  });

  test("market-data storage failure magic key is a retryable 503", async () => {
    const response = await call("PUT", MARKET_URL, { body: marketBody({ twelve_data: { api_key: "storage-unavailable-key" } }) });
    assert.equal(response.status, 503);
    assert.equal(response.json.error_code, "market_credential_storage_unavailable");
    assert.equal(response.json.retryable, true);
    assert.equal(response.headers.get("retry-after"), "30");
    assert.equal((await call("GET", MARKET_URL)).json.updated_at, null);
  });

  test("market-data test endpoint is simulated", async () => {
    const probe = async (providerId) => {
      const response = await call("POST", `${MARKET_URL}/test`, { body: { provider_id: providerId } });
      assert.equal(response.status, 200);
      assert.equal(response.json.provider_id, providerId);
      return response.json.status;
    };
    assert.equal(await probe("yahoo"), "ready");
    assert.equal(await probe("alpha_vantage"), "ready"); // simulated deployment key
    assert.equal(await probe("finnhub"), "unavailable"); // key required, none available
    await call("PUT", MARKET_URL, { body: marketBody({ finnhub: { api_key: "fh-fictional-key" }, alpha_vantage: { api_key: "av-bad-key-12345" }, yahoo: { enabled: false } }) });
    assert.equal(await probe("finnhub"), "ready");
    assert.equal(await probe("alpha_vantage"), "unavailable"); // personal key contains "bad"
    assert.equal(await probe("yahoo"), "unavailable"); // disabled
    await call("PUT", MARKET_URL, { body: marketBody({ finnhub: { enabled: false } }) });
    assert.equal(await probe("finnhub"), "unavailable");
    for (const body of [{}, { provider_id: "bing" }, { provider_id: "yahoo", extra: 1 }, { provider_id: 3 }]) {
      assert.equal((await call("POST", `${MARKET_URL}/test`, { body })).status, 422, JSON.stringify(body));
    }
  });
});


test("writes without a JSON content type are rejected like the backend boundary", async () => {
  const server2 = (await import("./server.mjs")).createServer({ tickMs: 5 });
  await new Promise((resolve) => server2.listen(0, "127.0.0.1", resolve));
  const url = `http://127.0.0.1:${server2.address().port}`;
  try {
    const bare = await fetch(`${url}/v2/research/topics/t/runs/op/continue`, { method: "POST" });
    assert.equal(bare.status, 415);
    assert.equal((await bare.json()).error_code, "unsupported_media_type");
    const probe = await fetch(`${url}/v1/account/model-config/test`, { method: "POST" });
    assert.equal(probe.status, 401); // exempt from the JSON check, then authenticated as usual
  } finally {
    server2.closeAllConnections?.();
    await new Promise((resolve) => server2.close(resolve));
  }
});

describe("L2 SSE /runs/:op/events endpoint", () => {
  async function readSse(response) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    const frames = [];
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let match;
      while ((match = /\r?\n\r?\n/.exec(buffer)) !== null) {
        const chunk = buffer.slice(0, match.index);
        buffer = buffer.slice(match.index + match[0].length);
        if (!chunk) continue;
        if (chunk.startsWith(":")) {
          frames.push({ type: "comment", comment: chunk.slice(1).trim() });
          continue;
        }
        let id = null;
        let event = "message";
        const dataLines = [];
        for (const line of chunk.split(/\r?\n/)) {
          if (line.startsWith(":")) continue;
          const idx = line.indexOf(":");
          const field = idx === -1 ? line : line.slice(0, idx);
          const rawVal = idx === -1 ? "" : line.slice(idx + 1).replace(/^ /, "");
          if (field === "id") id = rawVal;
          else if (field === "event") event = rawVal;
          else if (field === "data") dataLines.push(rawVal);
        }
        frames.push({ type: "event", id, event, data: JSON.parse(dataLines.join("\n")) });
      }
    }
    return frames;
  }

  test("streams SSE snapshot, step updates, heartbeats, terminal frame, resume headers, and connection limits", async () => {
    const sseServer = createServer({ tickMs: 30, heartbeatMs: 12, maxSsePerAccount: 2 });
    await new Promise((resolve) => sseServer.listen(0, "127.0.0.1", resolve));
    const url = `http://127.0.0.1:${sseServer.address().port}`;
    try {
      const email = `sse-${randomUUID()}@example.com`;
      const reg = await fetch(`${url}/v1/auth/local/register`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password: "correct-horse-battery" }),
      });
      assert.equal(reg.status, 201);
      const authJson = await reg.json();
      const authHeaders = { Authorization: `Bearer ${authJson.access_token}` };

      const topicRes = await fetch(`${url}/v2/research/topics`, {
        method: "POST",
        headers: { ...authHeaders, "Content-Type": "application/json" },
        body: JSON.stringify({ question: "Northwind SSE L2 流式测试", source_ids: [S1_ID] }),
      });
      assert.equal(topicRes.status, 201);
      const topic = await topicRes.json();

      // 1. Live full run with discover_sources enabled (verifies eligible_count fix in interimFromEvents too)
      const run1Res = await fetch(`${url}/v2/research/topics/${topic.topic_id}/runs`, {
        method: "POST",
        headers: { ...authHeaders, "Content-Type": "application/json", "Idempotency-Key": randomUUID() },
        body: JSON.stringify({ parent_version_id: topic.versions[0].version_id, discover_sources: true }),
      });
      assert.equal(run1Res.status, 202);
      const { operation: op1 } = await run1Res.json();

      const unauth = await fetch(`${url}/v2/research/topics/${topic.topic_id}/runs/${op1.operation_id}/events`);
      assert.equal(unauth.status, 401);

      const missing = await fetch(`${url}/v2/research/topics/${topic.topic_id}/runs/op_missing/events`, { headers: authHeaders });
      assert.equal(missing.status, 404);

      const badHeader = await fetch(`${url}/v2/research/topics/${topic.topic_id}/runs/${op1.operation_id}/events`, {
        headers: { ...authHeaders, "Last-Event-ID": "bogus_id" },
      });
      assert.equal(badHeader.status, 400);
      assert.equal((await badHeader.json()).error_code, "invalid_last_event_id");

      const streamRes = await fetch(`${url}/v2/research/topics/${topic.topic_id}/runs/${op1.operation_id}/events`, {
        headers: authHeaders,
      });
      assert.equal(streamRes.status, 200);
      assert.match(streamRes.headers.get("content-type") ?? "", /text\/event-stream/);
      assert.equal(streamRes.headers.get("cache-control"), "no-cache, no-transform");
      assert.equal(streamRes.headers.get("x-accel-buffering"), "no");

      const frames = await readSse(streamRes);
      const events = frames.filter((f) => f.type === "event");
      const comments = frames.filter((f) => f.type === "comment");
      assert.equal(events[0].event, "progress.snapshot");
      assert.ok(events.some((f) => f.event === "step.event"));
      assert.ok(events.some((f) => f.event === "step.current"));
      assert.equal(events.at(-1).event, "run.terminal");
      assert.equal(events.at(-1).data.operation_status, "completed");
      assert.ok(comments.some((c) => c.comment === "heartbeat"));

      const stepEvents = events.filter((f) => f.event === "step.event");
      const discoverEvent = stepEvents.find((f) => f.data.event?.name === "news_search");
      if (discoverEvent?.data.event?.summary) {
        assert.equal(discoverEvent.data.interim.leads_count, discoverEvent.data.event.summary.eligible_count);
      }

      // 2. Completed run immediate snapshot + terminal + after / Last-Event-ID slicing
      const resumeAfter = await fetch(`${url}/v2/research/topics/${topic.topic_id}/runs/${op1.operation_id}/events?after=2`, {
        headers: authHeaders,
      });
      assert.equal(resumeAfter.status, 200);
      const resumeAfterFrames = (await readSse(resumeAfter)).filter((f) => f.type === "event");
      assert.equal(resumeAfterFrames[0].event, "progress.snapshot");
      assert.ok(resumeAfterFrames[0].data.events.every((e) => e.index >= 2));
      assert.equal(resumeAfterFrames.at(-1).event, "run.terminal");

      const resumeId = await fetch(`${url}/v2/research/topics/${topic.topic_id}/runs/${op1.operation_id}/events`, {
        headers: { ...authHeaders, "Last-Event-ID": "ev:2" },
      });
      assert.equal(resumeId.status, 200);
      const resumeIdFrames = (await readSse(resumeId)).filter((f) => f.type === "event");
      assert.ok(resumeIdFrames[0].data.events.every((e) => e.index >= 3));
      assert.equal(resumeIdFrames.at(-1).event, "run.terminal");

      const resumeCp = await fetch(`${url}/v2/research/topics/${topic.topic_id}/runs/${op1.operation_id}/events`, {
        headers: { ...authHeaders, "Last-Event-ID": "cp:3" },
      });
      assert.equal(resumeCp.status, 200);
      const resumeCpFrames = (await readSse(resumeCp)).filter((f) => f.type === "event");
      assert.ok(resumeCpFrames[0].data.events.every((e) => e.index >= 3));

      const resumeDone = await fetch(`${url}/v2/research/topics/${topic.topic_id}/runs/${op1.operation_id}/events`, {
        headers: { ...authHeaders, "Last-Event-ID": "done:completed" },
      });
      assert.equal(resumeDone.status, 200);
      const resumeDoneFrames = (await readSse(resumeDone)).filter((f) => f.type === "event");
      assert.equal(resumeDoneFrames.at(-1).event, "run.terminal");

      // 3. Per-account connection limit (maxSsePerAccount = 2 -> 3rd gets 429) & slot cleanup on abort
      const topicLatest = await (await fetch(`${url}/v2/research/topics/${topic.topic_id}`, { headers: authHeaders })).json();
      const run2Res = await fetch(`${url}/v2/research/topics/${topic.topic_id}/runs`, {
        method: "POST",
        headers: { ...authHeaders, "Content-Type": "application/json", "Idempotency-Key": randomUUID() },
        body: JSON.stringify({ parent_version_id: topicLatest.versions.at(-1).version_id, discover_sources: true }),
      });
      assert.equal(run2Res.status, 202);
      const { operation: op2 } = await run2Res.json();

      const ac1 = new AbortController();
      const ac2 = new AbortController();
      const s1 = await fetch(`${url}/v2/research/topics/${topic.topic_id}/runs/${op2.operation_id}/events`, { headers: authHeaders, signal: ac1.signal });
      const s2 = await fetch(`${url}/v2/research/topics/${topic.topic_id}/runs/${op2.operation_id}/events`, { headers: authHeaders, signal: ac2.signal });
      assert.equal(s1.status, 200);
      assert.equal(s2.status, 200);

      const s3 = await fetch(`${url}/v2/research/topics/${topic.topic_id}/runs/${op2.operation_id}/events`, { headers: authHeaders });
      assert.equal(s3.status, 429);
      assert.equal(s3.headers.get("retry-after"), "5");
      assert.equal((await s3.json()).error_code, "rate_limited");

      ac1.abort();
      await s1.body?.cancel().catch(() => {});
      await sleep(25);
      const s4 = await fetch(`${url}/v2/research/topics/${topic.topic_id}/runs/${op2.operation_id}/events`, { headers: authHeaders });
      assert.equal(s4.status, 200);

      ac2.abort();
      await s2.body?.cancel().catch(() => {});
      await s4.body?.cancel().catch(() => {});

      // 4. L3 transient stream.delta & stream.reset frames + submission_rejected & submission_partial scenarios
      const makeTopicAndRun = async (questionOverride) => {
        const tRes = await fetch(`${url}/v2/research/topics`, {
          method: "POST",
          headers: { ...authHeaders, "Content-Type": "application/json" },
          body: JSON.stringify({ question: questionOverride, source_ids: [S1_ID] }),
        });
        assert.equal(tRes.status, 201);
        const createdTopic = await tRes.json();
        const rRes = await fetch(`${url}/v2/research/topics/${createdTopic.topic_id}/runs`, {
          method: "POST",
          headers: { ...authHeaders, "Content-Type": "application/json", "Idempotency-Key": randomUUID() },
          body: JSON.stringify({ parent_version_id: createdTopic.versions[0].version_id, discover_sources: false }),
        });
        assert.equal(rRes.status, 202);
        const { operation: op } = await rRes.json();
        return { topicId: createdTopic.topic_id, opId: op.operation_id };
      };

      const normal = await makeTopicAndRun("正常 L3 流式分析");
      const normalStream = await fetch(`${url}/v2/research/topics/${normal.topicId}/runs/${normal.opId}/events`, { headers: authHeaders });
      const normalEvents = (await readSse(normalStream)).filter((f) => f.type === "event");
      assert.ok(normalEvents.some((f) => f.event === "stream.delta" && f.data.channel === "thinking"));
      assert.ok(normalEvents.some((f) => f.event === "stream.delta" && f.data.channel === "text"));
      assert.ok(normalEvents.some((f) => f.event === "stream.delta" && f.data.channel === "tool_status"));
      assert.ok(normalEvents.some((f) => f.event === "stream.delta" && f.data.channel === "draft_report"));
      assert.ok(normalEvents.some((f) => f.event === "stream.reset" && f.data.reason === "tool_started"));
      assert.ok(normalEvents.some((f) => f.event === "stream.reset" && f.data.reason === "call_completed"));
      assert.equal(normalEvents.at(-1).event, "run.terminal");
      assert.equal(normalEvents.at(-1).data.operation_status, "completed");
      // Persisted turn events and /progress never store token deltas.
      const normalProgress = await (await fetch(`${url}/v2/research/topics/${normal.topicId}/runs/${normal.opId}/progress?after=0`, { headers: authHeaders })).json();
      assert.equal(normalProgress.event_count, 6);
      assert.ok(normalProgress.events.every((e) => !("delta" in e) && !("thinking" in e)));

      const rejected = await makeTopicAndRun("请测试驳回草稿回退场景");
      const rejectedStream = await fetch(`${url}/v2/research/topics/${rejected.topicId}/runs/${rejected.opId}/events`, { headers: authHeaders });
      const rejectedEvents = (await readSse(rejectedStream)).filter((f) => f.type === "event");
      assert.ok(rejectedEvents.some((f) => f.event === "stream.reset" && f.data.reason === "submission_rejected"));
      assert.ok(rejectedEvents.some((f) => f.event === "stream.delta" && f.data.channel === "draft_report" && f.data.delta.includes("修正后草稿")));
      assert.equal(rejectedEvents.at(-1).data.operation_status, "completed");

      const partial = await makeTopicAndRun("请输出部分完成场景");
      const partialStream = await fetch(`${url}/v2/research/topics/${partial.topicId}/runs/${partial.opId}/events`, { headers: authHeaders });
      const partialEvents = (await readSse(partialStream)).filter((f) => f.type === "event");
      const partialDraftIdx = partialEvents.findIndex((f) => f.event === "stream.delta" && f.data.channel === "draft_report");
      const partialResetIdx = partialEvents.findIndex((f) => f.event === "stream.reset" && f.data.reason === "submission_partial");
      assert.ok(partialDraftIdx >= 0, "expected partial run to stream draft_report before retraction");
      assert.ok(partialResetIdx > partialDraftIdx, "expected submission_partial reset after partial draft_report");
      assert.equal(partialEvents.at(-1).event, "run.terminal");
      assert.equal(partialEvents.at(-1).data.operation_status, "failed");

      // Continuing that partial turn emits live thinking + GFM draft_report and finishes with GFM Markdown report.
      const contRes = await fetch(`${url}/v2/research/topics/${partial.topicId}/runs/${partial.opId}/continue`, {
        method: "POST",
        headers: { ...authHeaders, "Content-Type": "application/json", "Idempotency-Key": randomUUID() },
        body: JSON.stringify({}),
      });
      assert.equal(contRes.status, 202);
      const { operation: contOp } = await contRes.json();
      const contStream = await fetch(`${url}/v2/research/topics/${partial.topicId}/runs/${contOp.operation_id}/events`, { headers: authHeaders });
      const contEvents = (await readSse(contStream)).filter((f) => f.type === "event");
      assert.ok(contEvents.some((f) => f.event === "stream.delta" && f.data.channel === "thinking"));
      const draftChunks = contEvents
        .filter((f) => f.event === "stream.delta" && f.data.channel === "draft_report")
        .map((f) => f.data.delta)
        .join("");
      assert.match(draftChunks, /\|\s*财务指标\s*\|/);
      assert.match(draftChunks, /\$130\.5B/);
      assert.match(draftChunks, /\+114\.2%/);
      assert.equal(contEvents.at(-1).data.operation_status, "completed");
      const finalTopic = await (await fetch(`${url}/v2/research/topics/${partial.topicId}`, { headers: authHeaders })).json();
      const finalReport = finalTopic.versions.at(-1).content.turns.at(-1).report;
      assert.match(finalReport, /^##\s+/);
      assert.match(finalReport, /https:\/\/www\.sec\.gov/);
    } finally {
      sseServer.closeAllConnections?.();
      await new Promise((resolve) => sseServer.close(resolve));
    }
  });

  test("Topic A Turn 2 seeds V1 charts (combo_bar_line inline + price_series trailing + create_chart events) and protects charts on review save", async () => {
    const topics = (await call("GET", "/v2/research/topics")).json;
    const topicA = topics.find((t) => t.request.question.startsWith("Northwind"));
    const latest = topicA.versions.at(-1);
    const turn2 = latest.content.turns[1];

    assert.equal(latest.content.charts.length, 3);
    assert.deepEqual(turn2.chart_ids, ["chart_northwind_combo", "chart_northwind_price", "chart_northwind_causal"]);
    assert.match(turn2.report, /^\[\[chart:chart_northwind_combo\]\]$/m);
    assert.ok(turn2.events.some((e) => e.kind === "tool" && e.name === "create_chart" && e.summary?.chart_id === "chart_northwind_combo"));
    assert.ok(turn2.events.some((e) => e.kind === "tool" && e.name === "create_chart" && e.summary?.chart_id === "chart_northwind_price"));
    assert.ok(turn2.events.some((e) => e.kind === "tool" && e.name === "create_chart" && e.summary?.chart_id === "chart_northwind_causal"));
    const causal = latest.content.charts.find((c) => c.chart_id === "chart_northwind_causal");
    assert.equal(causal?.chart_type, "causal_graph");
    assert.equal(causal?.causal_graph?.nodes.length, 6);
    assert.equal(causal?.causal_graph?.edges.length, 5);

    // User review version save preserves charts unchanged and rejects tampered charts with 422.
    const saved = await call("POST", `${topicPath(topicA.topic_id)}/versions`, {
      body: { parent_version_id: latest.version_id, content: { ...latest.content, summary: "已复核图表" }, review: "confirmed", note: "" },
    });
    assert.equal(saved.status, 201);
    assert.deepEqual(saved.json.versions.at(-1).content.charts, latest.content.charts);

    const tampered = await call("POST", `${topicPath(topicA.topic_id)}/versions`, {
      body: {
        parent_version_id: saved.json.versions.at(-1).version_id,
        content: { ...saved.json.versions.at(-1).content, charts: [] },
        review: "draft",
        note: "",
      },
    });
    assert.equal(tampered.status, 422);
  });
});
