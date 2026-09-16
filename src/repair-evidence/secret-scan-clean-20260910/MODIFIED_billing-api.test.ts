import { describe, expect, it, vi } from "vitest";
import { fetchAccountBillingSummary, verifyGooglePlayPurchase } from "../src/billing-api";

const token = ["token", "fixture", "value", "123456789012345"].join("-");
const purchaseToken = ["purchase", "fixture", "value", "123456789012345"].join("-");
const summary = {
  plan: "free",
  entitlement_status: "active",
  product_id: null,
  base_plan_id: null,
  quota_limit: 200000,
  quota_consumed: 0,
  quota_reserved: 0,
  quota_remaining: 200000,
  quota_resets_at: null,
  provider_model: "glm-5.3-flash",
};

describe("mobile billing API boundary", () => {
  it("reads server-owned quota state without persisting a credential", async () => {
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify(summary), { status: 200 }));
    const result = await fetchAccountBillingSummary("https://api.example.test", token, fetcher);
    expect(result).toMatchObject({ plan: "free", quota_remaining: 200000, provider_model: "glm-5.3-flash" });
    expect(fetcher.mock.calls[0]?.[1]).toMatchObject({ credentials: "omit", headers: expect.objectContaining({ Authorization: `Bearer ${token}` }) });
  });

  it("sends a Play purchase to the server with the fixed monthly base plan", async () => {
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({ ...summary, plan: "pro", product_id: "foresightnine_pro", base_plan_id: "monthly" }), { status: 200 }));
    const result = await verifyGooglePlayPurchase("https://api.example.test", token, purchaseToken, "foresightnine_pro", fetcher);
    expect(result.summary).toMatchObject({ plan: "pro", product_id: "foresightnine_pro", base_plan_id: "monthly" });
    const init = fetcher.mock.calls[0]?.[1];
    expect(init?.method).toBe("POST");
    expect(init?.body).toBe(JSON.stringify({ purchase_token: purchaseToken, product_id: "foresightnine_pro", base_plan_id: "monthly" }));
  });

  it("rejects an invalid server-owned quota shape", async () => {
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({ ...summary, quota_remaining: -1 }), { status: 200 }));
    await expect(fetchAccountBillingSummary("https://api.example.test", token, fetcher)).rejects.toThrow("Account summary response is invalid.");
  });
});
