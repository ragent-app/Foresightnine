import { describe, expect, it, vi } from "vitest";
import type { ResearchV2 } from "@foresightnine/contracts";
import { jsonResponse, storeSession } from "../test/fixtures";
import { ApiError, ApiTransportError } from "./http";
import { cancelRun, createResearch, idempotentWrite, runScope, startTurn } from "./writes";

function apiError(status: number): ApiError {
  return new ApiError({
    status,
    code: `http_${status}`,
    message: `http_${status}`,
    requestId: null,
    retryable: status === 503,
    retryAfterMs: null,
    details: [],
  });
}

const TOPIC = {
  topic_id: "t1",
  versions: [{ version_id: "v1" }, { version_id: "v2" }],
};
const OPERATION = { operation_id: "op1", cancel_token: "ctok", status: "pending" };

describe("idempotentWrite", () => {
  it("reuses the key after a transport failure and settles on success", async () => {
    const keys: string[] = [];
    await expect(
      idempotentWrite("s1", { a: 1 }, async (key) => {
        keys.push(key);
        throw new ApiTransportError();
      }),
    ).rejects.toBeInstanceOf(ApiTransportError);
    await idempotentWrite("s1", { a: 1 }, async (key) => {
      keys.push(key);
      return "ok";
    });
    expect(keys[1]).toBe(keys[0]);
    await idempotentWrite("s1", { a: 1 }, async (key) => {
      keys.push(key);
      return "ok";
    });
    expect(keys[2]).not.toBe(keys[0]);
  });

  it("settles the key on a 422 so the next call gets a new key", async () => {
    const keys: string[] = [];
    await expect(
      idempotentWrite("s2", { a: 1 }, async (key) => {
        keys.push(key);
        throw apiError(422);
      }),
    ).rejects.toMatchObject({ status: 422 });
    await idempotentWrite("s2", { a: 1 }, async (key) => {
      keys.push(key);
      return 1;
    });
    expect(keys[1]).not.toBe(keys[0]);
  });

  it("keeps the key on 503, 408 and 429", async () => {
    for (const status of [503, 408, 429]) {
      const scope = `s3:${status}`;
      const keys: string[] = [];
      await expect(
        idempotentWrite(scope, null, async (key) => {
          keys.push(key);
          throw apiError(status);
        }),
      ).rejects.toMatchObject({ status });
      await idempotentWrite(scope, null, async (key) => {
        keys.push(key);
        return 1;
      });
      expect(keys[1]).toBe(keys[0]);
    }
  });

  it("keeps the key on abort", async () => {
    const keys: string[] = [];
    const abort = new DOMException("aborted", "AbortError");
    await expect(
      idempotentWrite("s4", {}, async (key) => {
        keys.push(key);
        throw abort;
      }),
    ).rejects.toBe(abort);
    await idempotentWrite("s4", {}, async (key) => {
      keys.push(key);
      return 1;
    });
    expect(keys[1]).toBe(keys[0]);
  });

  it("uses a new key when the body changes", async () => {
    const keys: string[] = [];
    const fail = async (key: string): Promise<never> => {
      keys.push(key);
      throw new ApiTransportError();
    };
    await expect(idempotentWrite("s5", { a: 1 }, fail)).rejects.toBeInstanceOf(ApiTransportError);
    await expect(idempotentWrite("s5", { a: 2 }, fail)).rejects.toBeInstanceOf(ApiTransportError);
    expect(keys[1]).not.toBe(keys[0]);
  });
});

describe("startTurn", () => {
  it("throws without a session", async () => {
    await expect(startTurn("t1", { parent_version_id: "v1" })).rejects.toThrow("no_session");
  });

  it("scopes the key by account, topic and parent version", async () => {
    expect(runScope("acc_1", "t1", "v1")).toBe("run:acc_1:t1:v1");
    storeSession();
    const keys: Array<string | undefined> = [];
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      keys.push((init?.headers as Record<string, string>)["Idempotency-Key"]);
      if (keys.length === 1) throw new TypeError("network");
      return jsonResponse({ operation: OPERATION });
    });
    vi.stubGlobal("fetch", fetchMock);
    await expect(startTurn("t1", { parent_version_id: "v1" })).rejects.toBeInstanceOf(ApiTransportError);
    await startTurn("t1", { parent_version_id: "v1" });
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).toBe(keys[0]);
  });
});

describe("createResearch", () => {
  it("starts the run from the last version of the created topic", async () => {
    storeSession();
    const bodies: unknown[] = [];
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/v2/research/topics")) return jsonResponse(TOPIC);
      bodies.push(JSON.parse(String(init?.body)));
      return jsonResponse({ operation: OPERATION });
    });
    vi.stubGlobal("fetch", fetchMock);
    const result = await createResearch({ question: "q" }, { max_model_calls: 5 });
    expect(bodies).toEqual([{ max_model_calls: 5, parent_version_id: "v2" }]);
    expect(result.operation?.operation_id).toBe("op1");
    expect(result.startError).toBeNull();
    expect(fetchMock.mock.calls[1]?.[0]).toContain("/v2/research/topics/t1/runs");
  });

  it("returns the topic with startError when the run start fails with 503", async () => {
    storeSession();
    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith("/v2/research/topics")) return jsonResponse(TOPIC);
      return jsonResponse({ error_code: "unavailable", message: "later" }, { status: 503 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const result = await createResearch({ question: "q" }, {});
    expect(result.topic.topic_id).toBe("t1");
    expect(result.operation).toBeNull();
    expect(result.startError).toBeInstanceOf(ApiError);
    expect((result.startError as ApiError).status).toBe(503);
  });

  it("rethrows when createTopic fails", async () => {
    storeSession();
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ error_code: "bad", message: "bad" }, { status: 422 })));
    await expect(createResearch({ question: "q" }, {})).rejects.toMatchObject({ status: 422 });
  });
});

describe("cancelRun", () => {
  it("posts the cancel token with an idempotency key", async () => {
    storeSession();
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => jsonResponse({ operation: OPERATION }));
    vi.stubGlobal("fetch", fetchMock);
    await cancelRun(OPERATION as unknown as ResearchV2.Operation);
    const init = fetchMock.mock.calls[0]?.[1];
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain("/v1/operations/op1/cancel");
    expect(init?.body).toBe('{"cancel_token":"ctok"}');
    expect((init?.headers as Record<string, string>)["Idempotency-Key"]).toBeTruthy();
  });
});
