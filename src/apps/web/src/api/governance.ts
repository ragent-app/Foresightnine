import { useMutation } from "@tanstack/react-query";
import { apiRequest } from "./http";

export type GovernanceLight = "green" | "yellow" | "red" | "not_applicable" | "unavailable";

export interface CapitalAllocationAudit {
  quality_light: GovernanceLight;
  is_financial_gated: boolean;
  currency: string;
  sbc_ttm_usd?: string | null;
  sbc_pct_of_revenue?: string | null;
  sbc_pct_of_fcf?: string | null;
  sbc_tax_withholding_usd?: string | null;
  unrecognized_sbc_usd?: string | null;
  unrecognized_sbc_pct_of_mcap?: string | null;
  buybacks_ttm_usd?: string | null;
  dividends_ttm_usd?: string | null;
  buyback_to_sbc_ratio?: string | null;
  net_diluted_share_change_1y_pct?: string | null;
  net_diluted_share_change_3y_pct?: string | null;
  net_diluted_share_change_5y_pct?: string | null;
  true_net_shareholder_yield_pct?: string | null;
  fake_buyback_flag: boolean;
  possible_ma_equity_issuance: boolean;
  notes: string[];
}

export interface ClassifiedAnchor {
  cleaned_label: string;
  raw_label: string;
  tier: string;
  tier_rank: number;
  tier_label: string;
  is_csm: boolean;
}

export interface IncentiveGovernanceAudit {
  quality_light: GovernanceLight;
  disclosure_regime: string;
  latest_accession?: string | null;
  latest_filed_at?: string | null;
  company_selected_measure?: string | null;
  highest_anchor_tier: string;
  highest_anchor_label: string;
  classified_anchors: ClassifiedAnchor[];
  anchor_downgraded_yoy: boolean;
  removed_anchors_yoy: string[];
  added_anchors_yoy: string[];
  founder_ceo_exempt: boolean;
  pay_tsr_divergent: boolean;
  mnpi_red_flag: boolean;
  latest_peo_total_comp_usd?: string | null;
  latest_peo_cap_usd?: string | null;
  notes: string[];
}

export interface ScoredGuidanceRow {
  period_label: string;
  metric_key: string;
  basis: string;
  guidance_mid?: string | null;
  actual_value?: string | null;
  surprise_pct?: string | null;
  verdict: string;
  source_kind: string;
  source_accession?: string | null;
  disclosed_at?: string | null;
}

export interface GuidanceCredibilityAudit {
  quality_light: GovernanceLight;
  profile: string;
  scored_quarters: number;
  total_records: number;
  beat_count: number;
  inline_count: number;
  miss_count: number;
  beat_rate_pct?: string | null;
  avg_surprise_vs_mid_pct?: string | null;
  rows: ScoredGuidanceRow[];
  notes: string[];
}

export interface GovernanceAuditReport {
  audit_id: string;
  cik: string;
  symbol: string;
  company_name?: string | null;
  as_of?: string | null;
  rules_version: string;
  rules_sha256: string;
  overall_light: GovernanceLight;
  headline_anchor_summary: string;
  capital_allocation: CapitalAllocationAudit;
  incentive_governance: IncentiveGovernanceAudit;
  guidance_credibility: GuidanceCredibilityAudit;
  stale_projection: boolean;
  elapsed_ms: number;
  generated_at: string;
}

export interface CdaAttributionBrief {
  cik: string;
  symbol: string;
  def14a_accession: string;
  rules_sha256: string;
  summary: string;
  incentive_alignment_assessment: string;
  capital_allocation_verdict: string;
  skeptic_counterview: string;
  verified_metric_count: number;
  total_cited_metric_count: number;
  verified_quote_count: number;
  total_cited_quote_count: number;
  open_gaps: string[];
  tokens_used: number;
  model_used?: string | null;
}

export interface GuidanceBackfillResponse {
  cik: string;
  symbol: string;
  extracted_records: number;
  tokens_used: number;
  open_gaps: string[];
  guidance_credibility: GuidanceCredibilityAudit;
}

export function useGovernanceAudit() {
  return useMutation<GovernanceAuditReport | null, Error, { symbol: string; cik?: string | null; cache_only?: boolean }>({
    mutationFn: (body) =>
      apiRequest<GovernanceAuditReport | null>("/v2/research/governance/audit", {
        method: "POST",
        body,
      }),
  });
}

export function useGovernanceCdaBrief() {
  return useMutation<CdaAttributionBrief, Error, { symbol: string; cik?: string | null; force_refresh?: boolean }>({
    mutationFn: (body) =>
      apiRequest<CdaAttributionBrief>("/v2/research/governance/audit/cda-brief", {
        method: "POST",
        body,
      }),
  });
}

export function useGuidanceBackfill() {
  return useMutation<GuidanceBackfillResponse, Error, { symbol: string; cik?: string | null; max_quarters?: number }>({
    mutationFn: (body) =>
      apiRequest<GuidanceBackfillResponse>("/v2/research/governance/guidance/backfill", {
        method: "POST",
        body,
      }),
  });
}
