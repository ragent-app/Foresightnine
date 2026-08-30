import { describe, expect, it } from "vitest";
import { MARKET_DATA_STATUS as mobileStatus, MARKET_SNAPSHOT_BUNDLE as mobileBundle, fmInput as mobileFmInput } from "../src/fixtures";
import { MARKET_DATA_STATUS as webStatus, MARKET_SNAPSHOT_BUNDLE as webBundle, fmInput as webFmInput } from "../../web/src/fixtures";

describe("shared market snapshot path", () => {
  it("keeps Web and Android on the same deterministic bundle and FM conversion", () => {
    expect(mobileBundle).toEqual(webBundle);
    expect(mobileFmInput()).toEqual(webFmInput());
    expect(mobileStatus).toBe("mock");
    expect(webStatus).toBe("mock");
    expect(mobileFmInput().forwardReturns).toBeUndefined();
  });
});
