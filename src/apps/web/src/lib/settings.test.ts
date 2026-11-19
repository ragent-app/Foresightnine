import { describe, expect, it } from "vitest";
import { marketUpdateFrom, type MarketDataConfig, type ModelConfig } from "../api/settings";
import { apiKeyProblem, baseUrlProblem, canTestProtocol, keyRequired, modelNameProblem, quotaShare } from "./settings";

const SAVED: ModelConfig = {
  schema_version: "v1", mode: "personal", configured: true, protocol: "openai_chat_completions",
  base_url: "https://api.example.com/v1", model: "example-model", key_hint: "••••abcd",
};

describe("baseUrlProblem", () => {
  it("accepts public https addresses on port 443", () => {
    expect(baseUrlProblem("https://api.example.com/v1")).toBeNull();
    expect(baseUrlProblem("https://api.example.com:443/v1")).toBeNull();
  });

  it("rejects what the backend would reject", () => {
    expect(baseUrlProblem("")).toBe("请输入服务地址");
    expect(baseUrlProblem(" https://api.example.com")).toBe("地址前后不能有空格");
    expect(baseUrlProblem("http://api.example.com")).toBe("只支持 https 地址");
    expect(baseUrlProblem("https://api.example.com:8443")).toBe("只支持 443 端口");
    expect(baseUrlProblem("https://user:pw@api.example.com")).toBe("地址不能包含账号或密码");
    expect(baseUrlProblem("https://localhost/v1")).toBe("只支持公网地址");
    expect(baseUrlProblem("https://192.168.1.10/v1")).toBe("只支持公网地址");
    expect(baseUrlProblem("https://metadata.google.internal/")).toBe("只支持公网地址");
    expect(baseUrlProblem("not a url")).toBe("地址格式不正确");
    expect(baseUrlProblem("https://api.example.com/v1?x=1")).toBe("地址不能包含 ? 或 # 之后的部分");
    expect(baseUrlProblem("https://api.example.com/v1#a")).toBe("地址不能包含 ? 或 # 之后的部分");
    expect(baseUrlProblem("https://api.example.com\\v1")).toBe("地址格式不正确");
  });
});

describe("keyRequired", () => {
  it("needs a key for a first configuration", () => {
    expect(keyRequired(undefined, "openai_chat_completions", "https://api.example.com")).toBe(true);
    expect(keyRequired({ schema_version: "v1", mode: "platform", configured: true }, "openai_chat_completions", "https://api.example.com")).toBe(true);
  });

  it("keeps the saved key for a path or model change on the same origin and protocol", () => {
    expect(keyRequired(SAVED, "openai_chat_completions", "https://api.example.com/v2")).toBe(false);
    // Platform mode keeps the saved configuration, so its key can still be reused.
    expect(keyRequired({ ...SAVED, mode: "platform" }, "openai_chat_completions", "https://api.example.com/v1")).toBe(false);
  });

  it("needs the key again when the origin or protocol changes", () => {
    expect(keyRequired(SAVED, "openai_chat_completions", "https://other.example.com/v1")).toBe(true);
    expect(keyRequired(SAVED, "anthropic_messages", "https://api.example.com/v1")).toBe(true);
  });
});

describe("field checks", () => {
  it("validates keys and model names like the backend", () => {
    expect(apiKeyProblem("", true)).toBe("请输入 API Key");
    expect(apiKeyProblem("", false)).toBeNull();
    expect(apiKeyProblem("short", false)).toBe("API Key 至少 8 个字符");
    expect(apiKeyProblem(" sk-12345678", false)).toBe("API Key 前后不能有空格");
    expect(modelNameProblem("")).toBe("请输入模型名称");
    expect(modelNameProblem("m".repeat(129))).toBe("模型名称不能超过 128 个字符");
  });

  it("only offers a connection test for OpenAI-compatible endpoints", () => {
    expect(canTestProtocol("openai_chat_completions")).toBe(true);
    expect(canTestProtocol("anthropic_messages")).toBe(false);
  });

  it("splits the quota bar into used and reserved shares", () => {
    expect(quotaShare(50, 25, 100)).toEqual({ used: 0.5, reserved: 0.25 });
    expect(quotaShare(120, 10, 100)).toEqual({ used: 1, reserved: 0 });
    expect(quotaShare(0, 0, 0)).toEqual({ used: 0, reserved: 0 });
  });
});

describe("marketUpdateFrom", () => {
  const config: MarketDataConfig = {
    schema_version: "v1", updated_at: null, fallback_order: ["market_data", "yahoo"] as MarketDataConfig["fallback_order"],
    providers: [
      { provider_id: "market_data", display_name: "Market Data", enabled: true, requires_key: true, personal_key_configured: true, platform_available: false, key_hint: "••••abcd" },
      { provider_id: "yahoo", display_name: "Yahoo Finance", enabled: true, requires_key: false, personal_key_configured: false, platform_available: true, key_hint: null },
    ],
  };

  it("keeps saved keys unless a change names one", () => {
    expect(marketUpdateFrom(config)).toEqual({
      fallback_order: ["market_data", "yahoo"],
      providers: [{ provider_id: "market_data", enabled: true }, { provider_id: "yahoo", enabled: true }],
    });
  });

  it("applies order, enabled flags and one key change", () => {
    const body = marketUpdateFrom(config, { order: ["yahoo", "market_data"], enabled: { yahoo: false }, key: { providerId: "market_data", clear: true } });
    expect(body.fallback_order).toEqual(["yahoo", "market_data"]);
    expect(body.providers).toEqual([{ provider_id: "market_data", enabled: true, clear_api_key: true }, { provider_id: "yahoo", enabled: false }]);
    expect(marketUpdateFrom(config, { key: { providerId: "market_data", apiKey: "new-key-1234" } }).providers[0]).toEqual({ provider_id: "market_data", enabled: true, api_key: "new-key-1234" });
  });
});
