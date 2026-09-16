import { describe, expect, it } from "vitest";
import { buildMethodsGuide, methodsGuideFilename } from "./methods-guide";

describe("downloadable project guide", () => {
  it("documents the complete Chinese runtime and user flow", () => {
    const guide = buildMethodsGuide("zh");
    expect(guide.length).toBeGreaterThan(3_000);
    expect(guide).toContain("API / 金融数据源配置");
    expect(guide).toContain("个人模型配置（BYOK）");
    expect(guide).toContain("BYOK 模型请求不扣平台额度");
    expect(guide).toContain("个人模型调用失败时显示明确失败状态，不回退到平台模型");
    expect(guide).toContain("••••XXXX");
    expect(guide).toContain("Market Data → Alpha Vantage → Twelve Data → Finnhub → Yahoo Finance");
    expect(guide).toContain("https://www.alphavantage.co/support/#api-key");
    expect(guide).toContain("https://finnhub.io/dashboard");
    expect(guide).toContain("新建推演");
    expect(guide).toContain("反方审查");
    expect(guide).toContain("世界线");
    expect(guide).toContain("FM Raw");
    expect(guide).toContain("常见状态与处理");
    expect(guide).toContain("staging/evaluation");
    expect(guide).toContain("生产准入");
    expect(methodsGuideFilename("zh")).toBe("foresightnine-user-guide-zh-CN.txt");
  });

  it("provides the same operational sections in English", () => {
    const guide = buildMethodsGuide("en");
    expect(guide.length).toBeGreaterThan(3_500);
    expect(guide).toContain("API / financial data configuration");
    expect(guide).toContain("Personal model configuration (BYOK)");
    expect(guide).toContain("BYOK model requests do not consume platform quota");
    expect(guide).toContain("never falls back to the platform model");
    expect(guide).toContain("••••XXXX");
    expect(guide).toContain("End-to-end research workflow");
    expect(guide).toContain("Market-data runtime");
    expect(guide).toContain("To obtain keys:");
    expect(guide).toContain("Security and privacy");
    expect(guide).toContain("licensed staging/evaluation mode");
    expect(guide).toContain("production provider admission");
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
