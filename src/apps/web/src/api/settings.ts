import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiRequest } from "./http";

// Account settings (spec §4.8). Each type mirrors one backend model:
// billing.BillingLedger.snapshot, model_credentials.ModelConfig*, market_credentials.MarketData*.

export interface AccountSummary {
  account: { account_id: string; email: string | null };
  plan: string;
  entitlement_status: string;
  product_id: string | null;
  base_plan_id: string | null;
  quota_limit: number;
  quota_consumed: number;
  quota_reserved: number;
  quota_remaining: number;
  quota_resets_at: string | null;
  provider_model: string | null;
  /** The model platform research runs use (may differ from provider_model). */
  research_model?: string | null;
}

export type ModelProtocol = "openai_chat_completions" | "anthropic_messages";

/**
 * GET/PUT/DELETE /v1/account/model-config (response_model_exclude_none: absent fields are unset).
 * In platform mode the personal fields, when present, describe a saved but inactive configuration;
 * PUT {mode: "personal"} reuses it without re-entering the key. DELETE removes it.
 */
export interface ModelConfig {
  schema_version: "v1";
  mode: "platform" | "personal";
  configured: boolean;
  protocol?: ModelProtocol;
  base_url?: string;
  model?: string;
  key_hint?: string;
  updated_at?: string;
}

export interface ModelConfigUpdate {
  mode: "platform" | "personal";
  protocol?: ModelProtocol | null;
  base_url?: string | null;
  model?: string | null;
  api_key?: string | null;
  clear_api_key?: boolean;
}

export type ConnectionStatus = "ready" | "unavailable";

export type MarketProviderId = "market_data" | "alpha_vantage" | "twelve_data" | "finnhub" | "yahoo" | "tushare";

export interface MarketProviderSummary {
  provider_id: MarketProviderId;
  display_name: string;
  enabled: boolean;
  requires_key: boolean;
  personal_key_configured: boolean;
  platform_available: boolean;
  key_hint: string | null;
}

export interface MarketDataConfig {
  schema_version: "v1";
  fallback_order: MarketProviderId[];
  providers: MarketProviderSummary[];
  updated_at: string | null;
}

export interface MarketProviderUpdate {
  provider_id: MarketProviderId;
  enabled: boolean;
  api_key?: string;
  clear_api_key?: boolean;
}

export interface MarketDataConfigUpdate {
  fallback_order: MarketProviderId[];
  providers: MarketProviderUpdate[];
}

export const settingsKeys = {
  account: ["account"] as const,
  model: ["modelConfig"] as const,
  market: ["marketDataConfig"] as const,
};

export function getAccountSummary(signal?: AbortSignal): Promise<AccountSummary> {
  return apiRequest<AccountSummary>("/v1/account", signal ? { signal } : {});
}

export function getModelConfig(signal?: AbortSignal): Promise<ModelConfig> {
  return apiRequest<ModelConfig>("/v1/account/model-config", signal ? { signal } : {});
}

export function putModelConfig(body: ModelConfigUpdate): Promise<ModelConfig> {
  return apiRequest<ModelConfig>("/v1/account/model-config", { method: "PUT", body });
}

export function deleteModelConfig(): Promise<ModelConfig> {
  return apiRequest<ModelConfig>("/v1/account/model-config", { method: "DELETE" });
}

export function testModelConfig(): Promise<{ status: ConnectionStatus }> {
  return apiRequest<{ status: ConnectionStatus }>("/v1/account/model-config/test", { method: "POST" });
}

export function getMarketDataConfig(signal?: AbortSignal): Promise<MarketDataConfig> {
  return apiRequest<MarketDataConfig>("/v1/account/market-data-config", signal ? { signal } : {});
}

export function putMarketDataConfig(body: MarketDataConfigUpdate): Promise<MarketDataConfig> {
  return apiRequest<MarketDataConfig>("/v1/account/market-data-config", { method: "PUT", body });
}

export function testMarketProvider(providerId: MarketProviderId): Promise<{ provider_id: MarketProviderId; status: ConnectionStatus }> {
  return apiRequest("/v1/account/market-data-config/test", { method: "POST", body: { provider_id: providerId } });
}

/** The full update body for the current config, keeping keys unless a change says otherwise. */
export function marketUpdateFrom(config: MarketDataConfig, change: {
  order?: MarketProviderId[];
  enabled?: Partial<Record<MarketProviderId, boolean>>;
  key?: { providerId: MarketProviderId; apiKey: string } | { providerId: MarketProviderId; clear: true };
} = {}): MarketDataConfigUpdate {
  return {
    fallback_order: change.order ?? [...config.fallback_order],
    providers: config.providers.map((provider) => {
      const update: MarketProviderUpdate = { provider_id: provider.provider_id, enabled: change.enabled?.[provider.provider_id] ?? provider.enabled };
      if (change.key && change.key.providerId === provider.provider_id) {
        if ("clear" in change.key) update.clear_api_key = true;
        else update.api_key = change.key.apiKey;
      }
      return update;
    }),
  };
}

export function useAccountSummary() {
  return useQuery({ queryKey: settingsKeys.account, queryFn: ({ signal }) => getAccountSummary(signal) });
}

export function useModelConfig() {
  return useQuery({ queryKey: settingsKeys.model, queryFn: ({ signal }) => getModelConfig(signal) });
}

export function useMarketDataConfig() {
  return useQuery({ queryKey: settingsKeys.market, queryFn: ({ signal }) => getMarketDataConfig(signal) });
}

export function useSaveModelConfig() {
  const queryClient = useQueryClient();
  return useMutation<ModelConfig, unknown, ModelConfigUpdate | "delete">({
    mutationFn: (body) => (body === "delete" ? deleteModelConfig() : putModelConfig(body)),
    onSuccess: (config) => {
      queryClient.setQueryData(settingsKeys.model, config);
      void queryClient.invalidateQueries({ queryKey: settingsKeys.account });
    },
  });
}

export function useSaveMarketDataConfig() {
  const queryClient = useQueryClient();
  return useMutation<MarketDataConfig, unknown, MarketDataConfigUpdate>({
    mutationFn: putMarketDataConfig,
    onSuccess: (config) => queryClient.setQueryData(settingsKeys.market, config),
  });
}
