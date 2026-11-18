import { describe, expect, it } from "vitest";
import { MARKET_DATA_STATUS as mobileStatus, fmInput as mobileFmInput } from "../src/fixtures";

describe("shared market snapshot path", () => {
  it("keeps the deterministic mock bundle and FM conversion", () => {
    expect(mobileStatus).toBe("mock");
    expect(mobileFmInput()).toEqual(mobileFmInput());
    expect(mobileFmInput().forwardReturns).toBeUndefined();
  });
});
