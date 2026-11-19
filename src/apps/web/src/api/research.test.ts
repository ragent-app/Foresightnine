import { describe, expect, it, vi } from "vitest";
import type { ResearchV2 } from "@foresightnine/contracts";
import { jsonResponse, storeSession } from "../test/fixtures";
import {
  cancelOperation,
  continueRun,
  createTopic,
  createVersion,
  exportFull,
  exportMetadata,
  getProgress,
  getRecoveryStatus,
  getTopic,
  getVersionSourceText,
  isActiveStatus,
  latestVersion,
  listRuns,
  listTopics,
  listVersionSources,
  recoverRun,
  startRun,
} from "./research";

const TOPIC = "top/ic 1";
const OP = "op/1";
const VER = "ver/1";
const SRC = "src/1";

function stubFetch() {
  const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => jsonResponse({ ok: true }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function lastCall(fetchMock: ReturnType<typeof stubFetch>) {
  const call = fetchMock.mock.calls[fetchMock.mock.calls.length - 1];
  const init = call?.[1] ?? {};
  return {
    url: String(call?.[0]),
    method: init.method,
    body: init.body,
    headers: (init.headers ?? {}) as Record<string, string>,
  };
}

const runRequest: ResearchV2.ResearchRunRequest = { parent_version_id: "v0" };
const versionInput = {
  content: {},
  parent_version_id: "v0",
} as unknown as ResearchV2.VersionInput;

describe("research api", () => {
  const cases: Array<{
    name: string;
    call: () => Promise<unknown>;
    method: string;
    path: string;
  }> = [
    { name: "listTopics", call: () => listTopics(), method: "GET", path: "/v2/research/topics" },
    { name: "getTopic", call: () => getTopic(TOPIC), method: "GET", path: "/v2/research/topics/top%2Fic%201" },
    { name: "createTopic", call: () => createTopic({ question: "q" }), method: "POST", path: "/v2/research/topics" },
    { name: "startRun", call: () => startRun(TOPIC, runRequest, "k1"), method: "POST", path: "/v2/research/topics/top%2Fic%201/runs" },
    { name: "listRuns", call: () => listRuns(TOPIC), method: "GET", path: "/v2/research/topics/top%2Fic%201/runs" },
    {
      name: "getProgress",
      call: () => getProgress(TOPIC, OP, 3),
      method: "GET",
      path: "/v2/research/topics/top%2Fic%201/runs/op%2F1/progress?after=3",
    },
    {
      name: "getRecoveryStatus",
      call: () => getRecoveryStatus(TOPIC, OP),
      method: "GET",
      path: "/v2/research/topics/top%2Fic%201/runs/op%2F1/recovery-status",
    },
    {
      name: "continueRun",
      call: () => continueRun(TOPIC, OP, "k2"),
      method: "POST",
      path: "/v2/research/topics/top%2Fic%201/runs/op%2F1/continue",
    },
    {
      name: "recoverRun",
      call: () => recoverRun(TOPIC, OP, "k3"),
      method: "POST",
      path: "/v2/research/topics/top%2Fic%201/runs/op%2F1/recover",
    },
    {
      name: "cancelOperation",
      call: () => cancelOperation(OP, "tok", "k4"),
      method: "POST",
      path: "/v1/operations/op%2F1/cancel",
    },
    {
      name: "listVersionSources",
      call: () => listVersionSources(TOPIC, VER),
      method: "GET",
      path: "/v2/research/topics/top%2Fic%201/versions/ver%2F1/sources",
    },
    {
      name: "getVersionSourceText",
      call: () => getVersionSourceText(TOPIC, VER, SRC),
      method: "GET",
      path: "/v2/research/topics/top%2Fic%201/versions/ver%2F1/sources/src%2F1/text",
    },
    {
      name: "createVersion",
      call: () => createVersion(TOPIC, versionInput),
      method: "POST",
      path: "/v2/research/topics/top%2Fic%201/versions",
    },
    {
      name: "exportFull",
      call: () => exportFull(TOPIC, VER),
      method: "GET",
      path: "/v2/research/topics/top%2Fic%201/versions/ver%2F1/export",
    },
    {
      name: "exportMetadata",
      call: () => exportMetadata(TOPIC, VER),
      method: "GET",
      path: "/v2/research/topics/top%2Fic%201/versions/ver%2F1/export/metadata",
    },
  ];

  for (const item of cases) {
    it(`${item.name} calls ${item.method} ${item.path}`, async () => {
      storeSession();
      const fetchMock = stubFetch();
      await item.call();
      const call = lastCall(fetchMock);
      expect(call.method).toBe(item.method);
      expect(call.url.endsWith(item.path)).toBe(true);
    });
  }

  it("sends idempotency keys on run writes and none on createTopic", async () => {
    const fetchMock = stubFetch();
    await startRun(TOPIC, runRequest, "key-run");
    expect(lastCall(fetchMock).headers["Idempotency-Key"]).toBe("key-run");
    expect(lastCall(fetchMock).body).toBe(JSON.stringify(runRequest));
    await continueRun(TOPIC, OP, "key-continue");
    expect(lastCall(fetchMock).headers["Idempotency-Key"]).toBe("key-continue");
    expect(lastCall(fetchMock).body).toBeUndefined();
    await createTopic({ question: "q" });
    expect(lastCall(fetchMock).headers["Idempotency-Key"]).toBeUndefined();
  });

  it("cancel sends the cancel token body with an Idempotency-Key header", async () => {
    const fetchMock = stubFetch();
    await cancelOperation(OP, "cancel-tok", "key-cancel");
    const call = lastCall(fetchMock);
    expect(call.body).toBe('{"cancel_token":"cancel-tok"}');
    expect(call.headers["Idempotency-Key"]).toBe("key-cancel");
  });

  it("latestVersion returns the last version and throws on none", () => {
    const topic = { versions: [{ version_id: "a" }, { version_id: "b" }] } as unknown as ResearchV2.ResearchTopic;
    expect(latestVersion(topic).version_id).toBe("b");
    const empty = { versions: [] } as unknown as ResearchV2.ResearchTopic;
    expect(() => latestVersion(empty)).toThrow("topic_without_versions");
  });

  it("isActiveStatus is true only for pending and running", () => {
    expect(isActiveStatus("pending")).toBe(true);
    expect(isActiveStatus("running")).toBe(true);
    expect(isActiveStatus("completed")).toBe(false);
    expect(isActiveStatus("failed")).toBe(false);
    expect(isActiveStatus("cancelled")).toBe(false);
  });
});
