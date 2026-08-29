import assert from "node:assert/strict";
import test from "node:test";
import {
  ApiClient,
  ApiError,
  ContractValidationError,
  apiModeFor,
  normalizeApiBaseUrl,
  parseSseBody,
  parseSseText,
  validateResponse,
} from "../dist/index.js";

const HASH = "a".repeat(64);

function metadata(refId = "mock:ref") {
  return { schema_version: "v1", created_at: "2026-08-29T00:00:00Z", source_refs: [{ ref_id: refId, source_type: "model_inference", label: "Deterministic test" }], content_hash: HASH };
}

function operation(endpoint = "/v1/sessions") {
  return { ...metadata("mock:operation"), operation_id: "op-1", endpoint, status: "completed", updated_at: "2026-08-29T00:00:00Z", idempotency_key_hash: HASH, cancel_token: "c".repeat(16), resume_token: "r".repeat(16), last_event_id: "op-1:1" };
}

function sessionResponse() {
  return { operation: operation(), session: { ...metadata("mock:session"), session_id: "session-1", status: "DRAFT", expires_at: "2026-08-30T00:00:00Z" } };
}

function challengeResponse() {
  const criteria = ["premise_clarity", "time_boundary", "trigger_observability", "causal_chain", "counterfactuals", "evidence_quality", "asset_transmission", "already_priced", "falsifiability", "user_confirmation"];
  return { operation: operation("/v1/challenges"), challenge: {
    ...metadata("mock:challenge"), turn_id: "turn-1", challenge_id: "challenge-1", thesis_id: "thesis-1", claims: ["claim"], objections: ["objection"], missing_evidence: ["missing"], questions: ["question"], rubric_scores: criteria.map((criterion) => ({ criterion, score: 3, rationale: "test" })), unresolved_issues: [], citation_ids: [], public_fact_citation_ids: [], native_search_used: false, provenance: "test", user_confirmation: "pending", private_evidence_declared: false, status: "EVIDENCE_REQUIRED",
  } };
}

function sseEvent(sequence = 1) {
  return { schema_version: "v1", operation_id: "op-1", sequence, emitted_at: "2026-08-29T00:00:00Z", event: "operation.started", message: "started" };
}

function jsonResponse(value, status = 200, headers = {}) {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json", ...headers } });
}

test("normalizes API configuration and labels absent configuration offline/mock", () => {
  assert.equal(normalizeApiBaseUrl("https://api.example.test/base/"), "https://api.example.test/base");
  assert.equal(normalizeApiBaseUrl("http://127.0.0.1:8000"), "http://127.0.0.1:8000");
  assert.equal(normalizeApiBaseUrl("https://user:pass@example.test"), null);
  assert.equal(normalizeApiBaseUrl(""), null);
  assert.equal(apiModeFor(undefined), "offline-mock");
  assert.equal(apiModeFor("https://api.example.test"), "api");
});

test("POST sends generated Idempotency-Key and JSON request body", async () => {
  let captured;
  const client = new ApiClient({ baseUrl: "https://api.example.test/base", idempotencyKeyFactory: () => "k".repeat(16), fetcher: async (input, init) => {
    captured = { input: String(input), init };
    return jsonResponse(sessionResponse(), 201);
  } });
  await client.createSession({ client_locale: "en-US" });
  assert.equal(captured.input, "https://api.example.test/base/v1/sessions");
  assert.equal(captured.init.method, "POST");
  assert.equal(new Headers(captured.init.headers).get("Idempotency-Key"), "k".repeat(16));
  assert.deepEqual(JSON.parse(captured.init.body), { client_locale: "en-US" });
  assert.equal(new Headers(captured.init.headers).get("Content-Type"), "application/json");
});

test("default fetch is called with the global receiver for browser brand checks", async () => {
  const originalFetch = globalThis.fetch;
  let receiver;
  globalThis.fetch = function (input, init) {
    receiver = this;
    return Promise.resolve(jsonResponse(sessionResponse(), 201));
  };
  try {
    const client = new ApiClient({ baseUrl: "https://api.example.test", idempotencyKeyFactory: () => "k".repeat(16) });
    await client.createSession();
    assert.equal(receiver, globalThis);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("runtime response validation rejects malformed generated-contract payloads", async () => {
  const malformed = structuredClone(sessionResponse());
  malformed.session.content_hash = "not-a-sha256";
  const client = new ApiClient({ baseUrl: "https://api.example.test", fetcher: async () => jsonResponse(malformed) });
  await assert.rejects(client.createSession(), (error) => error instanceof ContractValidationError);
});

test("runtime validation rejects unknown fields and coerced rubric values", async () => {
  const unknown = structuredClone(sessionResponse());
  unknown.session.raw_provider_output = "must not be accepted";
  const unknownClient = new ApiClient({ baseUrl: "https://api.example.test", fetcher: async () => jsonResponse(unknown) });
  await assert.rejects(unknownClient.createSession(), (error) => error instanceof ContractValidationError);

  const coerced = challengeResponse();
  coerced.challenge.rubric_scores[0].score = "3";
  assert.throws(() => validateResponse(coerced, "ChallengeOperationResponse"), (error) => error instanceof ContractValidationError);
});

test("SSE validation applies unknown-field checks to each union variant", () => {
  const variants = [
    ["operation.started", { message: "started", progress: 0.5 }],
    ["operation.progress", { message: "working", result_ref: "result-1" }],
    ["heartbeat", { message: "unexpected" }],
    ["operation.error", { error: { ...{ schema_version: "v1", error_code: "rate_limited", message: "retry", request_id: "request-1", retryable: true, details: [] } }, result_ref: "result-1" }],
    ["operation.complete", { result_ref: "result-1", result_hash: HASH, error: {} }],
  ];
  for (const [event, extra] of variants) {
    const value = { ...sseEvent(), event, ...(event === "operation.progress" ? { progress: 0.5 } : {}), ...(event === "operation.complete" ? { result_ref: "result-1", result_hash: HASH } : {}), ...(event === "operation.error" ? { retryable: true } : {}), ...extra };
    assert.throws(() => validateResponse(value, "SseEvent"), (error) => error instanceof ContractValidationError);
  }
});

test("typed ApiError preserves status, error code, retryability, retry-after, and safe diagnostics", async () => {
  const providerOutput = "raw-provider-output-must-not-escape";
  const errorBody = { schema_version: "v1", error_code: "rate_limited", message: providerOutput, request_id: "request-1", retryable: true, details: [{ location: "quota", message: "try later" }] };
  const client = new ApiClient({ baseUrl: "https://api.example.test", fetcher: async () => jsonResponse(errorBody, 429, { "Retry-After": "7" }) });
  await assert.rejects(client.createSession(), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 429);
    assert.equal(error.errorCode, "rate_limited");
    assert.equal(error.retryable, true);
    assert.equal(error.retryAfter, 7);
    assert.equal(error.requestId, "request-1");
    assert.equal(error.details[0].location, "quota");
    assert.equal(error.details[0].message.includes(providerOutput), false);
    assert.equal(error.message.includes(providerOutput), false);
    return true;
  });
});

test("AbortSignal cancellation is passed through without exposing transport details", async () => {
  const controller = new AbortController();
  const client = new ApiClient({ baseUrl: "https://api.example.test", fetcher: (_input, init) => new Promise((_, reject) => {
    init.signal.addEventListener("abort", () => { const error = new Error("aborted"); error.name = "AbortError"; reject(error); }, { once: true });
  }) });
  const pending = client.createSession({}, { signal: controller.signal });
  controller.abort();
  await assert.rejects(pending, (error) => error?.name === "AbortError");
});

test("fails closed when no secure idempotency-key source exists", () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, "crypto");
  if (!descriptor?.configurable) return;
  const original = globalThis.crypto;
  Object.defineProperty(globalThis, "crypto", { configurable: true, value: undefined, writable: true });
  try {
    const client = new ApiClient({ baseUrl: "https://api.example.test", fetcher: async () => jsonResponse(sessionResponse()) });
    assert.throws(() => client.createSession(), /Secure idempotency key generation is unavailable/u);
  } finally {
    Object.defineProperty(globalThis, "crypto", descriptor ?? { configurable: true, value: original, writable: true });
  }
});

test("path parameters are URL encoded before transport", async () => {
  let url = "";
  const client = new ApiClient({ baseUrl: "https://api.example.test", idempotencyKeyFactory: () => "k".repeat(16), fetcher: async (input) => {
    url = String(input);
    return jsonResponse({ operation: operation("/v1/operations/op-1/cancel") });
  } });
  await client.cancelOperation("op/1 with space", "c".repeat(16));
  assert.match(url, /v1\/operations\/op%2F1%20with%20space\/cancel$/u);
});

test("SSE text helper validates replay frames and stream helper sends Last-Event-ID", async () => {
  const first = `id: op-1:1\nevent: operation.started\ndata: ${JSON.stringify(sseEvent(1))}\n\n`;
  const second = `id: op-1:2\nevent: operation.started\ndata: ${JSON.stringify(sseEvent(2))}\n\n`;
  assert.deepEqual(parseSseText(`${first}${second}`).map((item) => item.id), ["op-1:1", "op-1:2"]);
  assert.throws(() => parseSseText(first.replace("id: op-1:1", "id: attacker:9")));

  const encoder = new TextEncoder();
  const stream = new ReadableStream({ start(controller) {
    controller.enqueue(encoder.encode(first.slice(0, 30)));
    controller.enqueue(encoder.encode(first.slice(30)));
    controller.close();
  } });
  const streamed = [];
  for await (const item of parseSseBody(stream)) streamed.push(item);
  assert.equal(streamed[0].data.event, "operation.started");

  let lastEventId;
  const client = new ApiClient({ baseUrl: "https://api.example.test", fetcher: async (_input, init) => {
    lastEventId = new Headers(init.headers).get("Last-Event-ID");
    return new Response(second, { headers: { "Content-Type": "text/event-stream" } });
  } });
  const replayed = [];
  for await (const item of client.streamOperationEvents("op-1", { lastEventId: "op-1:1" })) replayed.push(item);
  assert.equal(lastEventId, "op-1:1");
  assert.deepEqual(replayed.map((item) => item.id), ["op-1:2"]);
});

test("SSE text fallback checks AbortSignal before and after reading the body", async () => {
  const controller = new AbortController();
  const client = new ApiClient({ baseUrl: "https://api.example.test", fetcher: async () => ({
    ok: true,
    body: null,
    text: async () => { controller.abort(); return ""; },
  }) });
  const iterator = client.streamOperationEvents("op-1", { signal: controller.signal });
  await assert.rejects(iterator.next(), (error) => error?.name === "AbortError");
});
