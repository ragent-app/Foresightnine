import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createWorkspaceAutosave } from "../src/workspace-autosave";
beforeEach(() => vi.useFakeTimers()); afterEach(() => vi.useRealTimers());
describe("workspace save scheduling", () => {
  it("debounces to the newest immutable snapshot", async () => {
    const save = vi.fn(async () => {}); const c = createWorkspaceAutosave(save, vi.fn()); const input = { text: "a" }; c.schedule(input); input.text = "not captured"; c.schedule({ text: "b" });
    expect(save).not.toHaveBeenCalled(); await vi.advanceTimersByTimeAsync(600); expect(save).toHaveBeenCalledTimes(1); expect(save).toHaveBeenLastCalledWith({ text: "b" }); c.close();
  });
  it("does not resave identical state after status-only renders", async () => {
    const save = vi.fn(async () => {}); const c = createWorkspaceAutosave(save, vi.fn()); c.schedule({ text: "same" }); await c.flush(); c.schedule({ text: "same" }); await vi.advanceTimersByTimeAsync(600); expect(save).toHaveBeenCalledTimes(1); c.close();
  });
  it("serializes a newer snapshot behind the active write", async () => {
    let done!: () => void; const save = vi.fn().mockImplementationOnce(() => new Promise<void>(resolve => { done = resolve; })).mockResolvedValue(undefined); const c = createWorkspaceAutosave(save, vi.fn()); c.schedule("a"); const writing = c.flush(); c.schedule("b"); expect(save).toHaveBeenCalledTimes(1); done(); await writing; expect(save.mock.calls.map(call => call[0])).toEqual(["a", "b"]); c.close();
  });
  it("flushes the newest queued draft on close and suppresses stale UI notifications", async () => {
    let done!: () => void; const save = vi.fn().mockImplementationOnce(() => new Promise<void>(resolve => { done = resolve; })).mockResolvedValue(undefined); const status = vi.fn(); const c = createWorkspaceAutosave(save, status); c.schedule("a"); const writing = c.flush(); c.schedule("b"); c.close(); status.mockClear(); done(); await writing; expect(save.mock.calls.map(call => call[0])).toEqual(["a", "b"]); expect(status).not.toHaveBeenCalled(); c.schedule("ignored"); expect(save).toHaveBeenCalledTimes(2);
  });
  it("resumes after a StrictMode-style setup/cleanup/setup cycle", async () => {
    const save = vi.fn(async () => {}); const status = vi.fn(); const c = createWorkspaceAutosave(save, status); c.close(); expect(status).not.toHaveBeenCalled(); await Promise.resolve(); c.resume(); c.schedule("after remount"); await vi.advanceTimersByTimeAsync(600); expect(save).toHaveBeenCalledWith("after remount"); c.close();
  });
  it("does not show saved when an older write completes after a newer invalid snapshot", async () => {
    let done!: () => void; const save = vi.fn().mockImplementationOnce(() => new Promise<void>(resolve => { done = resolve; })).mockResolvedValue(undefined); const status = vi.fn(); const c = createWorkspaceAutosave(save, status); c.schedule("old valid"); const work = c.flush(); c.reject(); done(); await work; expect(status).toHaveBeenLastCalledWith("error"); c.schedule("corrected"); await c.flush(); expect(status).toHaveBeenLastCalledWith("saved"); c.close();
  });
  it("keeps a failed draft available for explicit retry, without a retry loop", async () => {
    const save = vi.fn().mockRejectedValueOnce(new Error("synthetic disk error")).mockResolvedValue(undefined); const status = vi.fn(); const c = createWorkspaceAutosave(save, status); c.schedule("draft"); await c.flush(); expect(status).toHaveBeenLastCalledWith("error"); await vi.advanceTimersByTimeAsync(6000); expect(save).toHaveBeenCalledTimes(1); await c.retry(); expect(save).toHaveBeenCalledTimes(2); expect(status).toHaveBeenLastCalledWith("saved"); c.close();
  });
  it("does not swallow a newer draft when an older write fails", async () => {
    let reject!: (error: Error) => void; const save = vi.fn().mockImplementationOnce(() => new Promise((_, fail) => { reject = fail; })).mockResolvedValue(undefined); const c = createWorkspaceAutosave(save, vi.fn()); c.schedule("old"); const work = c.flush(); c.schedule("new"); reject(new Error("synthetic")); await work; await c.retry(); expect(save.mock.calls.map(call => call[0])).toEqual(["old", "new"]); c.close();
  });
});
