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

export type ModelProtocol = "openai_chat_completions" | "openai_responses" | "anthropic_messages";

/**
 * GET/PUT/DELETE /v1/account/model-config (response_model_exclude_none: absent fields are unset).
 * In platform mode the personal fields, when present, describe a saved but inactive configuration;
 * PUT {mode: "personal"} reuses it without re-entering the key. DELETE removes it.
 */
export interface MessagesOptions {
  version: string;
  auth_scheme: "x-api-key" | "bearer";
  max_tokens: number;
  effort?: string;
}

export interface ChatGenerationParameters {
  reasoning_effort?: string;
  temperature?: number;
  top_p?: number;
  seed?: number;
}

export type ModelSlot = "fast_extractor" | "deep_reasoner" | "bear_reviewer" | "writer";

export interface ModelSlotBinding {
  deployment_ref?: string | null;
  protocol?: ModelProtocol | null;
  base_url?: string | null;
  model?: string | null;
  credential_ref?: string | null;
  endpoint_version?: string | null;
  generation_parameters?: ChatGenerationParameters | null;
  messages_auth_scheme?: MessagesOptions["auth_scheme"] | null;
  messages_max_tokens?: number | null;
  messages_effort?: string | null;
  tools_verified?: boolean | null;
  allow_private_network?: boolean;
}

export interface ModelSlotConfig {
  fast_extractor?: ModelSlotBinding | null;
  deep_reasoner?: ModelSlotBinding | null;
  bear_reviewer?: ModelSlotBinding | null;
  writer?: ModelSlotBinding | null;
}

export interface SlotSummaryItem {
  slot: ModelSlot;
  role: string;
  configured: boolean;
  fallback_used: boolean;
  source: "slot_override" | "primary_fallback";
  protocol?: ModelProtocol | null;
  base_url?: string | null;
  endpoint_host?: string | null;
  model?: string | null;
  deployment_ref?: string | null;
  credential_ref?: string | null;
  capability_source?: string;
  mode?: string | null;
  native_tools_required?: boolean;
  native_tools_supported?: boolean | null;
  capability_valid?: boolean;
  validation_errors?: string[];
  context_tokens?: number | null;
  output_tokens?: number | null;
  input_counter?: string | null;
  differs_from_primary?: boolean;
  generation_parameters?: ChatGenerationParameters | null;
}

export interface SlotRoutingSummary {
  schema_version: "v1";
  is_valid: boolean;
  cross_family_bear_review: boolean;
  configured_slots: ModelSlot[];
  fallback_slots: ModelSlot[];
  heterogeneous_slots: ModelSlot[];
  errors: string[];
  slots: Record<ModelSlot, SlotSummaryItem>;
}

export interface ModelConfig {
  schema_version: "v1";
  mode: "platform" | "personal";
  configured: boolean;
  protocol?: ModelProtocol;
  base_url?: string;
  model?: string;
  key_hint?: string;
  messages_options?: MessagesOptions;
  generation_parameters?: ChatGenerationParameters;
  research_tools_verified_at?: string;
  model_slots?: ModelSlotConfig | null;
  model_slots_summary?: SlotRoutingSummary | null;
  updated_at?: string;
}

export interface ModelConfigUpdate {
  mode: "platform" | "personal";
  protocol?: ModelProtocol | null;
  base_url?: string | null;
  model?: string | null;
  api_key?: string | null;
  clear_api_key?: boolean;
  messages_options?: MessagesOptions;
  generation_parameters?: ChatGenerationParameters;
  model_slots?: ModelSlotConfig | null;
  clear_model_slots?: boolean;
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

export type EarningsProviderId = "fmp";

export interface EarningsProviderSummary {
  provider_id: EarningsProviderId;
  display_name: string;
  enabled: boolean;
  requires_key: boolean;
  personal_key_configured: boolean;
  platform_available: boolean;
  platform_key_allowed: boolean;
  key_hint: string | null;
}

export interface MarketDataConfig {
  schema_version: "v1";
  fallback_order: MarketProviderId[];
  providers: MarketProviderSummary[];
  earnings_providers?: EarningsProviderSummary[];
  updated_at: string | null;
}

export interface MarketProviderUpdate {
  provider_id: MarketProviderId;
  enabled: boolean;
  api_key?: string;
  clear_api_key?: boolean;
}

export interface EarningsProviderUpdate {
  provider_id: EarningsProviderId;
  enabled: boolean;
  api_key?: string;
  clear_api_key?: boolean;
}

export interface MarketDataConfigUpdate {
  fallback_order: MarketProviderId[];
  providers: MarketProviderUpdate[];
  fmp?: EarningsProviderUpdate;
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

export interface ModelDiscoveryRequest {
  protocol: ModelProtocol;
  base_url: string;
  api_key?: string;
  version?: string;
  auth_scheme?: MessagesOptions["auth_scheme"];
  model?: string;
}

export interface DiscoveredModel { id: string; efforts?: string[]; effort_source?: "model_api" | "official_docs" }

export function discoverModels(body: ModelDiscoveryRequest, signal: AbortSignal) {
  return apiRequest<{ models: DiscoveredModel[]; upstream_status?: number }>("/v1/account/model-config/models", {
    method: "POST", body, signal,
  });
}

interface ModelTestResult {
  status: ConnectionStatus;
  upstream_status?: number;
  parameter?: string;
}

export function testModelConfig(): Promise<ModelTestResult> {
  return apiRequest<ModelTestResult>("/v1/account/model-config/test", { method: "POST" });
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
  fmp?: { enabled?: boolean; apiKey?: string; clear?: true };
} = {}): MarketDataConfigUpdate {
  const existingFmp = config.earnings_providers?.find((item) => item.provider_id === "fmp");
  const body: MarketDataConfigUpdate = {
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
  if (change.fmp) {
    const fmpUpdate: EarningsProviderUpdate = {
      provider_id: "fmp",
      enabled: change.fmp.enabled ?? existingFmp?.enabled ?? true,
    };
    if (change.fmp.clear) fmpUpdate.clear_api_key = true;
    else if (change.fmp.apiKey !== undefined) fmpUpdate.api_key = change.fmp.apiKey;
    body.fmp = fmpUpdate;
  }
  return body;
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
