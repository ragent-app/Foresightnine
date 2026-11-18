import { describe, expect, it } from "vitest";
import { ApiError } from "../api/http";
import { nextPollDelay } from "./polling";

function apiError(status: number, retryAfterMs: number | null): ApiError {
  return new ApiError({ status, code: `http_${status}`, message: "x", requestId: null, retryable: true, retryAfterMs, details: [] });
}

describe("nextPollDelay", () => {
  it("returns 1500 on success", () => {
    expect(nextPollDelay(30000)).toBe(1500);
    expect(nextPollDelay(0)).toBe(1500);
  });

  it("doubles on error up to 30000", () => {
    expect(nextPollDelay(1500, new Error("x"))).toBe(3000);
    expect(nextPollDelay(0, new Error("x"))).toBe(3000);
    expect(nextPollDelay(3000, new Error("x"))).toBe(6000);
    expect(nextPollDelay(20000, new Error("x"))).toBe(30000);
    expect(nextPollDelay(30000, new Error("x"))).toBe(30000);
  });

  it("respects Retry-After for 429 and 503 only", () => {
    expect(nextPollDelay(1500, apiError(429, 60000))).toBe(60000);
    expect(nextPollDelay(1500, apiError(503, 60000))).toBe(60000);
    expect(nextPollDelay(1500, apiError(503, 1000))).toBe(3000);
    expect(nextPollDelay(1500, apiError(500, 60000))).toBe(3000);
    expect(nextPollDelay(1500, apiError(429, null))).toBe(3000);
  });
});
