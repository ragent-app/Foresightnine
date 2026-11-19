// Run from apps/web: node --test qa/mock-api/server.test.mjs
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { after, before, describe, test } from "node:test";
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

async function call(method, path, { body, headers = {}, auth = true } = {}) {
  const init = { method, headers: { ...headers } };
  if (auth && token) init.headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
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
    assert.equal(content.facts.length, 5);
    assert.equal(content.calculations.length, 4);
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
    const discovery = on.events.find((event) => event.kind === "tool" && event.name === "discover_sources");
    assert.equal(discovery.status, "completed");
    assert.equal(discovery.rationale, "搜索公开线索");
    assert.ok(on.final.usage.search_calls >= 1);
    assert.ok(on.turn.usage.search_calls >= 1);
    assert.ok(on.turn.events.some((event) => event.kind === "tool" && event.name === "discover_sources"));
    assert.equal(on.turn.search_status, "completed");
    assert.deepEqual(on.saved.content.source_candidates.map((row) => row.url), leads);
    assert.deepEqual(Object.keys(on.saved.content.source_candidates[0]).sort(), ["published_at", "retrieved_at", "search_provider", "search_result_hash", "status", "title", "url"]);

    for (const extra of [{ discover_sources: null }, { discover_sources: false }, { max_search_calls: 0 }, { discover_sources: true, max_search_calls: 0 }]) {
      const off = await ran(extra);
      assert.equal(off.events.some((event) => event.name === "discover_sources"), false, JSON.stringify(extra));
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
    assert.ok(omittedRun.events.some((event) => event.name === "discover_sources"));

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
});
