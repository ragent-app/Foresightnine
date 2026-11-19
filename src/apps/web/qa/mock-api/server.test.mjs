// Run from apps/web: node --test qa/mock-api/server.test.mjs
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { after, before, describe, test } from "node:test";
import { createServer } from "./server.mjs";
import { S1_ID, S3_ID, S4_ID } from "./fixtures.mjs";

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
  const body = { parent_version_id: parent, ...extra };
  const response = await call("POST", `${topicPath(topicId)}/runs`, { body, headers: { "Idempotency-Key": key } });
  return { response, body, key };
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
});
