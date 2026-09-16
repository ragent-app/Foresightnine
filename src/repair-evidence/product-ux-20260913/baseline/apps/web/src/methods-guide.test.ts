import { describe, expect, it } from "vitest";
import { buildMethodsGuide, methodsGuideFilename } from "./methods-guide";

describe("downloadable project guide", () => {
  it("documents the complete Chinese runtime and user flow", () => {
    const guide = buildMethodsGuide("zh");
    expect(guide.length).toBeGreaterThan(3_000);
    expect(guide).toContain("API / 金融数据源配置");
    expect(guide).toContain("Market Data → Alpha Vantage → Twelve Data → Finnhub → Yahoo Finance");
    expect(guide).toContain("新建推演");
    expect(guide).toContain("反方审查");
    expect(guide).toContain("世界线");
    expect(guide).toContain("FM Raw");
    expect(guide).toContain("常见状态与处理");
    expect(methodsGuideFilename("zh")).toBe("foresightnine-user-guide-zh-CN.txt");
  });

  it("provides the same operational sections in English", () => {
    const guide = buildMethodsGuide("en");
    expect(guide.length).toBeGreaterThan(3_500);
    expect(guide).toContain("API / financial data configuration");
    expect(guide).toContain("End-to-end research workflow");
    expect(guide).toContain("Market-data runtime");
    expect(guide).toContain("Security and privacy");
    expect(methodsGuideFilename("en")).toBe("foresightnine-user-guide-en.txt");
  });

  it("does not embed credential-like fields", () => {
    for (const language of ["zh", "en"] as const) {
      const guide = buildMethodsGuide(language).toLowerCase();
      expect(guide).not.toContain("access_token=");
      expect(guide).not.toContain("private_key");
      expect(guide).not.toContain("client_secret");
    }
  });
});
