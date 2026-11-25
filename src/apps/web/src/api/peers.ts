import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiRequest } from "./http";

export type QualityLight = "green" | "yellow" | "red" | "not_applicable";
export type PeriodBasis = "TTM" | "FY" | "partial";
export type WarningSeverity = "warning" | "error" | "info";

export interface CuratedPeerGroup {
  group_id: string;
  name: string;
  symbols: string[];
}

export interface UserPeerGroup {
  group_id: string;
  account_id: string;
  name: string;
  symbols: string[];
  created_at: string;
  updated_at: string;
}

export interface PeerSuggestResponse {
  symbol: string;
  recommended_group?: CuratedPeerGroup | null;
  recommended_peers: string[];
  curated_groups: CuratedPeerGroup[];
  user_groups: UserPeerGroup[];
}

export interface ComparabilityWarning {
  code: string;
  severity: WarningSeverity;
  symbols: string[];
  message: string;
}

export interface PeerCompanyMetrics {
  symbol: string;
  cik: string;
  company_name?: string | null;
  sic?: string | null;
  sector_category: string;
  is_financial_gated: boolean;
  is_comparable_for_median: boolean;
  non_comparable_reasons: string[];

  period_end_date?: string | null;
  fiscal_period_label?: string | null;
  period_basis: PeriodBasis;
  accounting_standard: string;
  currency: string;
  price_usd?: string | null;
  price_as_of_date?: string | null;
  price_source?: string | null;

  market_cap_usd?: string | null;
  enterprise_value_usd?: string | null;
  net_cash_usd?: string | null;
  ttm_revenue_usd?: string | null;
  ttm_fcf_usd?: string | null;

  ttm_revenue_yoy_pct?: string | null;
  latest_q_revenue_yoy_pct?: string | null;
  revenue_accel_pp?: string | null;

  ttm_gross_margin_pct?: string | null;
  ttm_operating_margin_pct?: string | null;
  ttm_net_margin_pct?: string | null;
  ttm_roic_pct?: string | null;

  ttm_fcf_margin_pct?: string | null;
  ocf_to_ni_ratio?: string | null;
  sbc_pct_of_fcf?: string | null;
  quality_light: QualityLight;
  quality_notes: string[];

  pe_ttm?: string | null;
  ev_to_sales_ttm?: string | null;
  ev_to_ebit_ttm?: string | null;
  ev_to_fcf_ttm?: string | null;
  ev_to_fcf_ex_sbc_ttm?: string | null;
  reverse_dcf_implied_5y_fcf_cagr_pct?: string | null;

  ranks: Record<string, number>;
  vs_median: Record<string, string>;
  notes: string[];
}

export interface PeerComparisonMatrix {
  comparison_id: string;
  primary_symbol: string;
  as_of?: string | null;
  exclude_non_comparable_from_median: boolean;
  reverse_dcf_assumptions: string;
  companies: PeerCompanyMetrics[];
  peer_medians: Record<string, string | null>;
  comparability_warnings: ComparabilityWarning[];
  primary_coordinate_summary: string;
  primary_watch_trigger_metrics: Record<string, string | null>;
  cache_hits: number;
  cache_misses: number;
  elapsed_ms: number;
  generated_at: string;
}

export interface PeerCitedMetric {
  symbol: string;
  metric_key: string;
  value: string;
  verified: boolean;
}

export interface PeerAttributionBrief {
  comparison_id: string;
  primary_symbol: string;
  summary: string;
  valuation_vs_fundamentals: string;
  relative_risks: string[];
  cited_metrics: PeerCitedMetric[];
  verified_metric_count: number;
  total_cited_metric_count: number;
  tokens_used: number;
  model_used?: string | null;
  generated_at: string;
}

export interface PeerCompareRequest {
  primary_symbol: string;
  symbols?: string[];
  as_of?: string | null;
  cache_only?: boolean;
  exclude_non_comparable_from_median?: boolean;
  manual_prices?: Record<string, string>;
}

export function usePeerSuggest(symbol: string, enabled = true) {
  const sym = symbol.trim().toUpperCase() || "NVDA";
  return useQuery<PeerSuggestResponse>({
    queryKey: ["peers", "suggest", sym],
    queryFn: () => apiRequest<PeerSuggestResponse>(`/v2/research/peers/suggest?symbol=${encodeURIComponent(sym)}`),
    enabled,
    staleTime: 60_000,
  });
}

export function useCreatePeerGroup() {
  const qc = useQueryClient();
  return useMutation<UserPeerGroup, Error, { name: string; symbols: string[] }>({
    mutationFn: (body) =>
      apiRequest<UserPeerGroup>("/v2/research/peers/groups", {
        method: "POST",
        body,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["peers", "suggest"] });
    },
  });
}

export function useDeletePeerGroup() {
  const qc = useQueryClient();
  return useMutation<{ deleted: boolean; group_id: string }, Error, string>({
    mutationFn: (groupId) =>
      apiRequest<{ deleted: boolean; group_id: string }>(`/v2/research/peers/groups/${encodeURIComponent(groupId)}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["peers", "suggest"] });
    },
  });
}

export function usePeerCompare() {
  return useMutation<PeerComparisonMatrix, Error, PeerCompareRequest>({
    mutationFn: (body) =>
      apiRequest<PeerComparisonMatrix>("/v2/research/peers/compare", {
        method: "POST",
        body,
      }),
  });
}

export function usePeerAttributionBrief() {
  return useMutation<PeerAttributionBrief, Error, { comparison_id: string }>({
    mutationFn: (body) =>
      apiRequest<PeerAttributionBrief>("/v2/research/peers/compare/brief", {
        method: "POST",
        body,
      }),
  });
}
