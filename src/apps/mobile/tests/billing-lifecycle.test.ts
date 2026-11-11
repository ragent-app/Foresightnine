import { describe, expect, it, vi } from "vitest";
import { subscribeToBillingForeground, type BillingAppStateSource } from "../src/billing-lifecycle";

function source(initial: string | null) {
  let listener: ((state: string) => void) | undefined;
  const remove = vi.fn();
  const appState: BillingAppStateSource = {
    currentState: initial,
    addEventListener: vi.fn((_event, nextListener) => { listener = nextListener; return { remove }; }),
  };
  return { appState, remove, emit: (state: string) => listener?.(state) };
}

describe("billing foreground synchronization", () => {
  it("refreshes on background-to-active only, not repeated active events", () => {
    const mock = source("active"); const refresh = vi.fn();
    subscribeToBillingForeground(mock.appState, refresh);
    mock.emit("active"); mock.emit("background"); mock.emit("background");
    expect(refresh).not.toHaveBeenCalled();
    mock.emit("active"); mock.emit("active");
    expect(refresh).toHaveBeenCalledOnce();
  });
  it("refreshes after returning from an inactive billing surface", () => {
    const mock = source("active"); const refresh = vi.fn();
    subscribeToBillingForeground(mock.appState, refresh);
    mock.emit("inactive"); mock.emit("active");
    expect(refresh).toHaveBeenCalledOnce();
  });
  it("does not duplicate the initial automatic restore when initial state is unknown", () => {
    const mock = source(null); const refresh = vi.fn();
    subscribeToBillingForeground(mock.appState, refresh);
    mock.emit("active"); expect(refresh).not.toHaveBeenCalled();
    mock.emit("background"); mock.emit("active"); expect(refresh).toHaveBeenCalledOnce();
  });
  it("removes its listener and ignores any queued callback after cleanup", () => {
    const mock = source("background"); const refresh = vi.fn();
    const subscription = subscribeToBillingForeground(mock.appState, refresh);
    subscription.remove(); mock.emit("active");
    expect(mock.remove).toHaveBeenCalledOnce(); expect(refresh).not.toHaveBeenCalled();
  });
});
