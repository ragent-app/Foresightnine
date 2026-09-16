import { describe, expect, it } from "vitest";
import { MARKET_DATA_STATUS, MARKET_SNAPSHOT_BUNDLE, fmInput } from "./fixtures";

describe("Web market snapshot integration", () => {
  it("uses the explicit mock bundle in the local FM input path", () => {
    expect(MARKET_DATA_STATUS).toBe("mock");
    expect(MARKET_SNAPSHOT_BUNDLE.provider.mode).toBe("mock");
    expect(fmInput().observations).toHaveLength(3);
    expect(fmInput().forwardReturns).toBeUndefined();
  });
});
