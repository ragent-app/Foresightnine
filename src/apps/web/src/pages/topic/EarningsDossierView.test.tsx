import type { ResearchV2 } from "@foresightnine/contracts";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  downloadEarningsExport,
  exportEarningsDossierUrl,
  getEarningsDossier,
  parseOrgGraphSseFrame,
  renderDeepReport,
  runEarningsSquad,
  steerEarningsSquad,
  subscribeEarningsSquadEvents,
} from "../../api/earningsSquad";
import { jsonResponse, storeSession, TOKEN } from "../../test/fixtures";
import { EarningsDossierView } from "./EarningsDossierView";

function sseResponse(frames: string[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const f of frames) controller.enqueue(encoder.encode(f));
      controller.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": "text/event-stream; charset=utf-8" },
  });
}

function buildSampleDossier(overrides: Partial<ResearchV2.EarningsDossier> = {}): ResearchV2.EarningsDossier {
  const nonFinancialSector: ResearchV2.SectorApplicability = {
    sic_code: "7372",
    sic_description: "Prepackaged Software",
    sector_category: "non_financial",
    is_gated: false,
    disabled_metrics: [],
    recommended_alternatives: [],
    explanation: "SEC SIC 7372 is eligible for full non-financial corporate diagnostics and valuation.",
  };

  const base: ResearchV2.EarningsDossier = {
    dossier_id: "dossier_acme_q2",
    run_id: "sqrun_1",
    entity_id: "cik:0000320193",
    symbol: "ACME",
    cik: "0000320193",
    company_name: "ACME Cloud Corp.",
    period_label: "Q2 FY26",
    selected_l2_pack_id: "kpi_saas_cloud",
    locked: true,
    dossier_sha256: "deadbeef".repeat(8),
    sector_applicability: nonFinancialSector,
    dispatch_plan: {
      plan_id: "plan_1",
      l1_pack_id: "earnings_review",
      selected_l2_pack_id: "kpi_saas_cloud",
      selected_l2_pack_reason: "Matched SIC 7372 to SaaS & Cloud KPI pack.",
      tasks: [
        {
          task_id: "task_seg",
          worker_role: "worker_segments",
          status: "planned",
          objective: "Extract cloud revenue and RPO",
        },
      ],
    },
    snapshot: {
      snapshot_id: "snap_1",
      entity_id: "cik:0000320193",
      symbol: "ACME",
      cik: "0000320193",
      company_name: "ACME Cloud Corp.",
      sic_code: "7372",
      sic_description: "Prepackaged Software",
      period_label: "Q2 FY26",
      sector_applicability: nonFinancialSector,
      statements: [
        {
          period_label: "Q2 FY26",
          period_type: "quarter",
          period_end: "2026-06-30",
          currency: "USD",
          monetary_unit: "USD millions",
          revenue: { value: "1000", unit: "USD millions", currency: "USD", fact_id: "f:rev_q2", source_id: "sec_10q_q2", xbrl_tag: "us-gaap:Revenues" },
          gross_profit: { value: "720", unit: "USD millions", currency: "USD", fact_id: "f:gp_q2" },
          operating_income: { value: "230", unit: "USD millions", currency: "USD", fact_id: "f:opinc_q2" },
          net_income: { value: "180", unit: "USD millions", currency: "USD", fact_id: "f:ni_q2" },
          sbc: { value: "85", unit: "USD millions", currency: "USD" },
          operating_cash_flow: { value: "130", unit: "USD millions", currency: "USD" },
          capex: { value: "30", unit: "USD millions", currency: "USD" },
          free_cash_flow: { value: "100", unit: "USD millions", currency: "USD" },
          accounts_receivable: { value: "260", unit: "USD millions", currency: "USD" },
          shares_diluted: { value: "100", unit: "millions shares" },
        },
        {
          period_label: "Q2 FY25",
          period_type: "quarter",
          period_end: "2025-06-30",
          currency: "USD",
          monetary_unit: "USD millions",
          revenue: { value: "850", unit: "USD millions", currency: "USD", fact_id: "f:rev_q2_25" },
          gross_profit: { value: "610", unit: "USD millions", currency: "USD" },
          operating_income: { value: "200", unit: "USD millions", currency: "USD" },
          net_income: { value: "165", unit: "USD millions", currency: "USD" },
          operating_cash_flow: { value: "180", unit: "USD millions", currency: "USD" },
          free_cash_flow: { value: "155", unit: "USD millions", currency: "USD" },
          accounts_receivable: { value: "180", unit: "USD millions", currency: "USD" },
          shares_diluted: { value: "98", unit: "millions shares" },
        },
      ],
      diagnostics: {
        entity_id: "cik:0000320193",
        symbol: "ACME",
        period_label: "Q2 FY26",
        monetary_unit: "USD millions",
        currency: "USD",
        sector_applicability: nonFinancialSector,
        flag_counts: { red: 1, yellow: 1, green: 1 },
        thresholds_reference: {},
        margin_bridge: [
          {
            line: "gross_margin",
            current_pct: { name: "gross_margin", status: "computed", value: "72.00", unit: "percent", formula: "gp/rev" },
            qoq_change_bps: { name: "gross_margin_qoq_bps", status: "computed", value: "80", unit: "bps", formula: "qoq" },
            yoy_change_bps: { name: "gross_margin_yoy_bps", status: "computed", value: "24", unit: "bps", formula: "yoy" },
          },
        ],
        earnings_quality: {
          reported_or_derived_fcf: { name: "fcf", status: "computed", value: "100", unit: "USD millions", currency: "USD", formula: "ocf-capex" },
          ocf_to_ni: { name: "ocf_to_ni", status: "computed", value: "0.72", unit: "ratio", formula: "ocf/ni" },
          fcf_to_ni: { name: "fcf_to_ni", status: "computed", value: "0.56", unit: "ratio", formula: "fcf/ni" },
          fcf_margin_pct: { name: "fcf_margin_pct", status: "computed", value: "10.00", unit: "percent", formula: "fcf/rev" },
          sbc_to_revenue_pct: { name: "sbc_to_revenue_pct", status: "computed", value: "8.50", unit: "percent", formula: "sbc/rev" },
          sbc_to_fcf_pct: { name: "sbc_to_fcf_pct", status: "computed", value: "85.00", unit: "percent", formula: "sbc/fcf" },
          fcf_ex_sbc: { name: "fcf_ex_sbc", status: "computed", value: "15.00", unit: "USD millions", currency: "USD", formula: "fcf-sbc" },
          fcf_ex_sbc_margin_pct: { name: "fcf_ex_sbc_margin_pct", status: "computed", value: "1.50", unit: "percent", formula: "(fcf-sbc)/rev" },
        },
        working_capital: {
          ar_yoy_growth_pct: { name: "ar_yoy_growth_pct", status: "computed", value: "44.44", unit: "percent", formula: "yoy" },
          revenue_yoy_growth_pct: { name: "revenue_yoy_growth_pct", status: "computed", value: "17.65", unit: "percent", formula: "yoy" },
          ar_vs_revenue_growth_pp: { name: "ar_vs_revenue_growth_pp", status: "computed", value: "26.79", unit: "pp", formula: "ar-rev" },
          inventory_yoy_growth_pct: { name: "inventory_yoy", status: "unavailable", value: null, unit: "percent", formula: "yoy", reason: "No inventory" },
          cogs_yoy_growth_pct: { name: "cogs_yoy", status: "unavailable", value: null, unit: "percent", formula: "yoy" },
          inventory_vs_cogs_growth_pp: { name: "inv_vs_cogs", status: "unavailable", value: null, unit: "pp", formula: "inv-cogs" },
          inventory_vs_revenue_growth_pp: { name: "inv_vs_rev", status: "unavailable", value: null, unit: "pp", formula: "inv-rev" },
          inventory_turnover: { current: { name: "inventory_turnover", status: "unavailable", value: null, unit: "ratio", formula: "cogs/avg_inv" } },
          dso: { current: { name: "dso", status: "computed", value: "23.7", unit: "days", formula: "ar/rev*91.25" } },
          dio: { current: { name: "dio", status: "unavailable", value: null, unit: "days", formula: "inv/cogs*91.25", reason: "No inventory" } },
          dpo: { current: { name: "dpo", status: "unavailable", value: null, unit: "days", formula: "ap/cogs*91.25" } },
          ccc: { current: { name: "ccc", status: "unavailable", value: null, unit: "days", formula: "dso+dio-dpo" } },
        },
        capital_allocation: {
          capex_to_revenue_pct: { name: "capex_to_revenue_pct", status: "computed", value: "3.00", unit: "percent", formula: "capex/rev" },
          capital_returned: { name: "capital_returned", status: "computed", value: "50", unit: "USD millions", currency: "USD", formula: "buybacks+div" },
          payout_to_fcf_pct: { name: "payout_to_fcf_pct", status: "computed", value: "50.00", unit: "percent", formula: "ret/fcf" },
          diluted_shares_yoy_pct: { name: "diluted_shares_yoy_pct", status: "computed", value: "2.04", unit: "percent", formula: "yoy" },
        },
        balance_sheet: {
          position: "net_cash",
          total_cash: { name: "total_cash", status: "computed", value: "900", unit: "USD millions", currency: "USD", formula: "cash" },
          total_debt: { name: "total_debt", status: "computed", value: "300", unit: "USD millions", currency: "USD", formula: "debt" },
          net_debt: { name: "net_debt", status: "computed", value: "-600", unit: "USD millions", currency: "USD", formula: "debt-cash" },
          net_cash: { name: "net_cash", status: "computed", value: "600", unit: "USD millions", currency: "USD", formula: "cash-debt" },
          net_debt_to_ttm_ebitda: { name: "net_debt_to_ttm_ebitda", status: "computed", value: "-0.60", unit: "multiple", formula: "nd/ebitda" },
          interest_coverage: { name: "interest_coverage", status: "computed", value: "23.00", unit: "multiple", formula: "ebit/int" },
          current_ratio: { name: "current_ratio", status: "computed", value: "2.80", unit: "ratio", formula: "ca/cl" },
        },
        flags: [
          {
            flag_id: "wc_ar_vs_revenue_growth",
            group: "working_capital",
            severity: "red",
            metric_name: "ar_vs_revenue_growth_pp",
            observed_value: "26.79",
            unit: "pp",
            threshold_rule: "AR YoY growth - Revenue YoY growth > +15.0 pp",
            summary: "Accounts receivable grew 26.79 pp faster than revenue.",
            investigation_prompt: "Inspect ASC 606 Revenue Recognition & AR footnotes in 10-Q for extended payment terms.",
            input_ids: ["f:ar_q2", "f:rev_q2"],
          },
          {
            flag_id: "ca_diluted_shares_yoy",
            group: "capital_allocation",
            severity: "yellow",
            metric_name: "diluted_shares_yoy_pct",
            observed_value: "2.04",
            unit: "percent",
            threshold_rule: "+1.0% < Diluted shares YoY <= +3.0%",
            summary: "Net share dilution of +2.04% YoY.",
            investigation_prompt: "Check Equity Compensation footnote for SBC grant run-rate.",
            input_ids: ["f:shares_q2"],
          },
          {
            flag_id: "bs_current_ratio",
            group: "balance_sheet",
            severity: "green",
            metric_name: "current_ratio",
            observed_value: "2.80",
            unit: "ratio",
            threshold_rule: "Current Ratio >= 1.5x",
            summary: "Healthy short-term liquidity at 2.80x.",
            investigation_prompt: "Confirm restricted cash exclusions in Liquidity MD&A.",
            input_ids: ["f:ca_q2"],
          },
        ],
      },
      flags: [],
      guidance_nongaap: {
        entity_id: "cik:0000320193",
        symbol: "ACME",
        period_label: "Q2 FY26",
        guidance_evaluations: [
          {
            metric_name: "revenue",
            period_label: "Q2 FY26",
            vs_prior_guidance: {
              status: "computed",
              benchmark_type: "prior_guidance_midpoint",
              outcome: "Beat",
              actual_value: "1000",
              benchmark_value: "975",
              guidance_low: "960",
              guidance_high: "990",
              range_position: "above_high",
              abs_diff: "25",
              pct_diff: "2.56",
              unit: "USD millions",
              currency: "USD",
              formula: "actual - mid",
            },
            vs_consensus: {
              status: "computed",
              benchmark_type: "consensus",
              outcome: "Beat",
              actual_value: "1000",
              benchmark_value: "985",
              abs_diff: "15",
              pct_diff: "1.52",
              unit: "USD millions",
              currency: "USD",
              formula: "actual - consensus",
            },
            new_guidance_direction: {
              direction: "Raise",
              new_low: "1040",
              new_mid: "1055",
              new_high: "1070",
              benchmark_mid: "1020",
              abs_change: "35",
              pct_change: "3.43",
              unit: "USD millions",
              currency: "USD",
              formula: "new_mid - bench",
            },
          },
          {
            metric_name: "non_gaap_eps",
            period_label: "Q2 FY26",
            vs_prior_guidance: {
              status: "computed",
              benchmark_type: "prior_guidance_midpoint",
              outcome: "Beat",
              actual_value: "2.45",
              benchmark_value: "2.35",
              abs_diff: "0.10",
              pct_diff: "4.26",
              unit: "USD/share",
              currency: "USD",
              formula: "actual - mid",
            },
            vs_consensus: {
              status: "computed",
              benchmark_type: "consensus",
              outcome: "Beat",
              actual_value: "2.45",
              benchmark_value: "2.40",
              abs_diff: "0.05",
              pct_diff: "2.08",
              unit: "USD/share",
              currency: "USD",
              formula: "actual - consensus",
            },
            new_guidance_direction: {
              direction: "Maintain",
              new_low: "2.50",
              new_mid: "2.55",
              new_high: "2.60",
              pct_change: "0.00",
              unit: "USD/share",
              currency: "USD",
              formula: "new_mid - bench",
            },
          },
        ],
        nongaap_verifications: [
          {
            metric_name: "eps",
            period_label: "Q2 FY26",
            unit: "USD/share",
            currency: "USD",
            gaap_value: "1.80",
            reported_non_gaap_value: "2.45",
            adjustments_sum: "0.65",
            computed_non_gaap_value: "2.45",
            discrepancy: "0.00",
            ties: true,
            tie_status: "exact_tie",
            uplift_ratio: { name: "uplift_ratio", status: "computed", value: "0.36", unit: "ratio", formula: "(ng-gaap)/gaap" },
            uplift_pct: { name: "uplift_pct", status: "computed", value: "36.11", unit: "percent", formula: "ratio*100" },
            classified_adjustments: [
              {
                label: "Stock-based compensation",
                amount: "0.60",
                unit: "USD/share",
                currency: "USD",
                category: "sbc",
                recurring_flag: true,
                recurring_reason: "Structurally recurring SBC expense.",
              },
            ],
            warnings: [],
            formula: "gaap + sum(adj)",
          },
        ],
        warnings: [],
      },
      valuation: {
        entity_id: "cik:0000320193",
        symbol: "ACME",
        monetary_unit: "USD millions",
        currency: "USD",
        sector_applicability: nonFinancialSector,
        market_cap: { name: "market_cap", status: "computed", value: "12000", unit: "USD millions", currency: "USD", formula: "px * shares" },
        enterprise_value: { name: "enterprise_value", status: "computed", value: "11400", unit: "USD millions", currency: "USD", formula: "mcap + debt - cash" },
        pe_ttm: { name: "pe_ttm", status: "computed", value: "22.50", unit: "multiple", formula: "mcap / ttm_ni" },
        pe_forward: { name: "pe_forward", status: "computed", value: "19.80", unit: "multiple", formula: "px / ntm_eps" },
        ev_to_sales: { name: "ev_to_sales", status: "computed", value: "3.00", unit: "multiple", formula: "ev / ttm_rev" },
        ev_to_ntm_sales: { name: "ev_to_ntm_sales", status: "computed", value: "2.75", unit: "multiple", formula: "ev / ntm_rev" },
        ev_to_ebitda: { name: "ev_to_ebitda", status: "computed", value: "11.40", unit: "multiple", formula: "ev / ttm_ebitda" },
        ev_to_ntm_ebitda: { name: "ev_to_ntm_ebitda", status: "unavailable", value: null, unit: "multiple", formula: "ev / ntm_ebitda", reason: "NTM EBITDA unavailable" },
        ev_to_ebit: { name: "ev_to_ebit", status: "computed", value: "13.57", unit: "multiple", formula: "ev / ttm_ebit" },
        p_to_fcf: { name: "p_to_fcf", status: "computed", value: "26.67", unit: "multiple", formula: "mcap / ttm_fcf" },
        fcf_yield_pct: { name: "fcf_yield_pct", status: "computed", value: "3.75", unit: "percent", formula: "ttm_fcf / mcap * 100" },
        fcf_ex_sbc: { name: "fcf_ex_sbc", status: "computed", value: "230", unit: "USD millions", currency: "USD", formula: "fcf - sbc" },
        ev_to_fcf_ex_sbc: { name: "ev_to_fcf_ex_sbc", status: "computed", value: "49.57", unit: "multiple", formula: "ev / (fcf - sbc)" },
        fcf_ex_sbc_yield_pct: { name: "fcf_ex_sbc_yield_pct", status: "computed", value: "1.92", unit: "percent", formula: "(fcf-sbc)/mcap * 100" },
        reverse_dcf: {
          status: "computed",
          terminal_method: "gordon_growth",
          forecast_years: 5,
          wacc_pct: "9.00",
          terminal_growth_pct: "3.00",
          target_ev: "11400.00",
          base_ttm_fcf: "450.00",
          implied_5y_fcf_cagr_pct: "14.25",
          terminal_value_share_pct: "68.50",
          implied_exit_fcf_multiple: "17.17",
          wacc_axis_pct: ["8.00", "9.00", "10.00"],
          terminal_growth_axis_pct: ["2.50", "3.00", "3.50"],
          sensitivity_matrix: [
            [
              { wacc_pct: "8.00", terminal_growth_pct: "2.50", status: "computed", implied_5y_fcf_cagr_pct: "11.10" },
              { wacc_pct: "8.00", terminal_growth_pct: "3.00", status: "computed", implied_5y_fcf_cagr_pct: "10.20" },
              { wacc_pct: "8.00", terminal_growth_pct: "3.50", status: "computed", implied_5y_fcf_cagr_pct: "9.15" },
            ],
            [
              { wacc_pct: "9.00", terminal_growth_pct: "2.50", status: "computed", implied_5y_fcf_cagr_pct: "15.10" },
              { wacc_pct: "9.00", terminal_growth_pct: "3.00", status: "computed", implied_5y_fcf_cagr_pct: "14.25" },
              { wacc_pct: "9.00", terminal_growth_pct: "3.50", status: "computed", implied_5y_fcf_cagr_pct: "13.30" },
            ],
            [
              { wacc_pct: "10.00", terminal_growth_pct: "2.50", status: "computed", implied_5y_fcf_cagr_pct: "18.80" },
              { wacc_pct: "10.00", terminal_growth_pct: "3.00", status: "computed", implied_5y_fcf_cagr_pct: "18.00" },
              { wacc_pct: "10.00", terminal_growth_pct: "3.50", status: "computed", implied_5y_fcf_cagr_pct: "17.15" },
            ],
          ],
          formula: "Gordon growth Reverse-DCF",
        },
      },
      earnings_release: {
        entity_id: "cik:0000320193",
        cik: "0000320193",
        current: {
          period_role: "current",
          accession: "0000320193-26-000055",
          filed_at: "2026-07-29",
          index_json_url: "https://www.sec.gov/Archives/edgar/data/320193/000032019326000055/index.json",
          ex99_1: {
            exhibit_type: "EX-99.1",
            filename: "ex99-1.htm",
            url: "https://www.sec.gov/Archives/edgar/data/320193/000032019326000055/ex99-1.htm",
            text: "Cloud revenue reached $720M, up 24% YoY.",
            content_sha256: "a".repeat(64),
            tables: [
              {
                title: "分部收入拆解 (Segment Revenue)",
                headers: ["分部", "Q2 FY26 ($M)", "Q2 FY25 ($M)"],
                rows: [
                  ["Cloud Subscriptions", "720", "580"],
                  ["Professional Services", "280", "270"],
                ],
                is_nongaap: false,
              },
            ],
          },
        },
      },
      filing_table_catalog: {
        entity_id: "cik:0000320193",
        cik: "0000320193",
        accession: "0000320193-26-000055",
        filing_summary_url: "https://www.sec.gov/Archives/edgar/data/320193/000032019326000055/FilingSummary.xml",
        report_count: 1,
        all_reports: [
          {
            r_file: "R2.htm",
            short_name: "Consolidated Statements of Operations",
            long_name: "0000002 - Statement - Consolidated Statements of Operations",
            menu_category: "Statements",
            normalized_category: "Statements",
            url: "https://www.sec.gov/Archives/edgar/data/320193/000032019326000055/R2.htm",
          },
        ],
      },
      redline_summary: {
        total_added: 1,
        total_removed: 1,
        total_revised: 1,
        total_filtered_noise: 4,
        section_diffs: {
          item_1a: {
            section_key: "item_1a",
            has_substantive_changes: true,
            added: [
              {
                change_type: "added",
                section_key: "item_1a",
                current_text: "Added disclosure regarding extended enterprise billing terms in EMEA.",
              },
            ],
            removed: [
              {
                change_type: "removed",
                section_key: "item_1a",
                prior_text: "Removed legacy on-premise migration bottleneck language.",
              },
            ],
            revised: [
              {
                change_type: "revised",
                section_key: "item_1a",
                prior_text: "AI compute costs may fluctuate modestly.",
                current_text: "AI GPU inference costs increased 140 bps sequentially and may compress gross margin.",
                similarity: 0.62,
              },
            ],
          },
        },
      },
      consensus_summary: {
        status: "available",
        source_id: "fmp_consensus",
        provider: "fmp",
        permissions: { store: true, display: true, model_input: true, export: false },
        records: [],
      },
      transcript_index: {
        status: "available",
        source_id: "fmp_transcript",
        provider: "fmp",
        permissions: { store: true, display: true, model_input: true, export: false },
        sections: ["prepared_remarks", "qa"],
        speakers: ["Jane Doe (CEO)"],
      },
      prefetched_sources: [
        {
          source_id: "sec_10q_q2",
          title: "ACME Q2 FY26 Form 10-Q",
          kind: "official",
          retrieval_method: "sec_filing_html",
          content_sha256: "c".repeat(64),
          permissions: { store: true, display: true, model_input: true, export: true },
          total_characters: 12000,
        },
        {
          source_id: "sec_ex99_1",
          title: "ACME Q2 FY26 8-K EX-99.1",
          kind: "official",
          retrieval_method: "sec_edgar",
          content_sha256: "a".repeat(64),
          permissions: { store: true, display: true, model_input: true, export: true },
          total_characters: 4500,
        },
        {
          source_id: "fmp_consensus",
          title: "FMP Consensus Estimates",
          kind: "market_data",
          retrieval_method: "fmp_consensus",
          content_sha256: "b".repeat(64),
          permissions: { store: true, display: true, model_input: true, export: false },
          total_characters: 900,
        },
      ],
    },
    evidence_cards: [
      {
        card_id: "card_seg_1",
        worker_role: "worker_segments",
        topic_tag: "Cloud RPO & NRR",
        finding: "Remaining Performance Obligations (RPO) reached $3.4B (+28% YoY) with Net Revenue Retention at 118%.",
        supporting_fact_ids: ["f:rpo_q2"],
        numeric_claims: [
          { metric: "RPO", value: "3400", unit: "USD millions", currency: "USD", period: "Q2 FY26", yoy: "+28%" },
          { metric: "NRR", value: "118", unit: "percent", period: "Q2 FY26", yoy: "-200 bps" },
        ],
        citations: [
          {
            source_id: "sec_ex99_1",
            location: "EX-99.1 Paragraph 3",
            quotation: "Cloud revenue reached $720M, up 24% YoY; RPO grew 28% YoY.",
          },
        ],
      },
    ],
    bear_cases: [
      {
        bear_id: "bear_1",
        anchor_kind: "flag",
        anchored_evidence_ids: ["wc_ar_vs_revenue_growth", "card_seg_1"],
        thesis_challenge: "AR grew +26.79 pp faster than revenue while NRR dipped 200 bps, signaling potential pull-forward via extended billing terms.",
        next_quarter_invalidation_trigger: "Q3 FY26 DSO declines below 21 days and AR YoY growth falls within +5 pp of revenue growth.",
        supporting_numbers: ["AR vs Rev +26.79 pp", "OCF/NI 0.72x"],
        severity: "high",
      },
    ],
    rejected_bear_cases: [
      {
        bear_id: "bear_rej_1",
        rejection_reason: "generic macro platitude lacking concrete company metrics",
        thesis_challenge: "Macroeconomic headwinds may pressure enterprise budgets.",
      },
    ],
    open_gaps: [
      {
        gap_id: "gap_1",
        stage: "stage_0_prefetch",
        code: "transcript_qa_partial",
        message: "Only executive Q&A summary indexed; full verbatim Q&A table pending.",
        fallback: "Relied on 8-K EX-99.1 guidance passages.",
      },
    ],
    gate_report: {
      passed: true,
      verified_card_ids: ["card_seg_1"],
      dropped_card_ids: [],
      unverified_citations: [],
      verified_bear_ids: ["bear_1"],
      rejected_bear_ids: [],
      exportable: false,
      non_exportable_sources: ["fmp_consensus", "fmp_transcript"],
      notes: ["BYOK FMP consensus and transcript are restricted from export (export=False)."],
    },
    memo_markdown: "## 买方结论摘要\n\n**ACME** Q2 FY26 营收 $1,000M 超预期，但应收增速高于营收 +26.79 pp 需重点跟踪。",
    deep_report_markdown: null,
    slot_usage_summary: {
      total_calls: 6,
      total_tokens: 18400,
      stage_0_model_calls: 0,
      deep_report_tool_calls: 0,
      slots: {
        deep_reasoner: { slot: "deep_reasoner", model: "reasoner-v1", protocol: "openai_responses", fallback_used: false, calls: 2, total_tokens: 6400, elapsed_ms: 1400 },
        fast_extractor: { slot: "fast_extractor", model: "fast-v1", protocol: "openai_chat_completions", fallback_used: false, calls: 2, total_tokens: 5200, elapsed_ms: 900 },
        bear_reviewer: { slot: "bear_reviewer", model: "bear-alt", protocol: "anthropic_messages", fallback_used: false, calls: 1, total_tokens: 3800, elapsed_ms: 1100 },
        writer: { slot: "writer", model: "primary_default", protocol: "openai_chat_completions", fallback_used: true, calls: 1, total_tokens: 3000, elapsed_ms: 800 },
      },
    },
    ...overrides,
  };

  return base;
}

describe("earningsSquad API client", () => {
  it("calls run, getDossier, renderDeepReport, steer, and export URL helpers cleanly", async () => {
    storeSession();
    const fetchMock = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) => jsonResponse({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    await runEarningsSquad("topic/1", { symbol: "AAPL", execution_mode: "async" }, { idempotencyKey: "idem-1" });
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain("/v2/research/topics/topic%2F1/earnings-squad/run");
    expect((fetchMock.mock.calls[0]?.[1] as RequestInit).method).toBe("POST");
    expect((fetchMock.mock.calls[0]?.[1] as RequestInit).headers).toMatchObject({
      Authorization: `Bearer ${TOKEN}`,
      "Idempotency-Key": "idem-1",
    });

    await getEarningsDossier("topic/1");
    expect(String(fetchMock.mock.calls[1]?.[0])).toContain("/v2/research/topics/topic%2F1/earnings-squad/dossier");
    expect((fetchMock.mock.calls[1]?.[1] as RequestInit).method).toBe("GET");

    await renderDeepReport("topic/1", { force: false });
    expect(String(fetchMock.mock.calls[2]?.[0])).toContain("/v2/research/topics/topic%2F1/earnings-squad/deep-report");
    expect((fetchMock.mock.calls[2]?.[1] as RequestInit).method).toBe("POST");

    await steerEarningsSquad("topic/1", "Focus on SBC dilution");
    const steerInit = fetchMock.mock.calls[3]?.[1] as RequestInit;
    expect(String(fetchMock.mock.calls[3]?.[0])).toContain("/v2/research/topics/topic%2F1/earnings-squad/steer");
    expect(steerInit.body).toBe(JSON.stringify({ text: "Focus on SBC dilution" }));

    expect(exportEarningsDossierUrl("topic/1", "xlsx")).toContain(
      "/v2/research/topics/topic%2F1/earnings-squad/export?format=xlsx",
    );
    expect(exportEarningsDossierUrl("topic/1", "markdown", "deep_report")).toContain(
      "/v2/research/topics/topic%2F1/earnings-squad/export?format=markdown&report_mode=deep_report",
    );
  });

  it("downloads multi-format exports with auth headers, disposition filename, and redaction header", async () => {
    storeSession();
    vi.useFakeTimers();
    const createObjectUrl = vi.fn(() => "blob:mock-xlsx");
    const revokeObjectUrl = vi.fn();
    class MockURL extends URL {
      static override createObjectURL = createObjectUrl;
      static override revokeObjectURL = revokeObjectUrl;
    }
    vi.stubGlobal("URL", MockURL);
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);

    const fetchMock = vi.fn(async () =>
      new Response(new Uint8Array([80, 75, 3, 4]), {
        status: 200,
        headers: {
          "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          "Content-Disposition": 'attachment; filename="ACME-Q2-FY26-earnings-dossier.xlsx"',
          "X-Dossier-Id": "dossier_1",
          "X-Export-Redacted": "true",
        },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const res = await downloadEarningsExport("t1", "xlsx");
    expect(res.filename).toBe("ACME-Q2-FY26-earnings-dossier.xlsx");
    expect(res.dossierId).toBe("dossier_1");
    expect(res.redacted).toBe(true);
    expect(createObjectUrl).toHaveBeenCalledTimes(1);
    expect(clickSpy).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(1000);
    expect(revokeObjectUrl).toHaveBeenCalledWith("blob:mock-xlsx");
    clickSpy.mockRestore();
  });

  it("parses org_graph_event SSE frames and deduplicates sequence numbers", async () => {
    storeSession();
    const validFrame = parseOrgGraphSseFrame(
      `event: org_graph_event\nid: org:2\ndata: ${JSON.stringify({
        operation_id: "sqop_1",
        sequence: 2,
        stage: "stage_2_workers",
        node_id: "worker_segments",
        role: "worker_segments",
        display_name: "Segments & KPIs",
        runtime_state: "running",
        action_kind: "READING",
        current_activity: "Parsing R2.htm",
        blocker_text: null,
        edge_event: { from_node_id: "lead_coordinator", to_node_id: "worker_segments", is_review: false, edge_kind: "dispatch" },
      })}`,
    );
    expect(validFrame?.event).toBe("org_graph_event");
    expect(parseOrgGraphSseFrame('event: org_graph_event\ndata: {"sequence":0}')).toBeNull();

    const sseBody = sseResponse([
      `: heartbeat\n\n`,
      `event: org_graph_event\nid: org:1\ndata: ${JSON.stringify({
        sequence: 1,
        stage: "stage_1_plan",
        node_id: "lead_coordinator",
        role: "lead_coordinator",
        display_name: "Lead Coordinator",
        runtime_state: "running",
        action_kind: "DELEGATING",
        current_activity: "Planning tasks",
      })}\n\n`,
      // Duplicate sequence 1 should be ignored
      `event: org_graph_event\nid: org:1\ndata: ${JSON.stringify({
        sequence: 1,
        stage: "stage_1_plan",
        node_id: "lead_coordinator",
        role: "lead_coordinator",
        display_name: "Lead Coordinator",
        runtime_state: "running",
        action_kind: "DELEGATING",
        current_activity: "Duplicate",
      })}\n\n`,
      `event: org_graph_event\nid: org:2\ndata: ${JSON.stringify({
        sequence: 2,
        stage: "stage_3a_bear",
        node_id: "bear_reviewer",
        role: "bear_reviewer",
        display_name: "Bear Reviewer",
        runtime_state: "completed",
        action_kind: "RESPONDING",
        current_activity: "Emitted bear cases",
        edge_event: { from_node_id: "lead_coordinator", to_node_id: "bear_reviewer", is_review: true, edge_kind: "review" },
      })}\n\n`,
      `event: run.terminal\nid: done:completed\ndata: ${JSON.stringify({ operation_id: "sqop_1", operation_status: "completed", version_id: "dossier_1" })}\n\n`,
    ]);
    vi.stubGlobal("fetch", vi.fn(async () => sseBody));

    const received: number[] = [];
    const terminalPromise = new Promise<string>((resolve) => {
      subscribeEarningsSquadEvents({
        topicId: "t1",
        after: 0,
        lastEventId: "org:0",
        onEvent: (ev) => received.push(ev.sequence),
        onTerminal: (term) => resolve(term.operation_status),
      });
    });

    expect(await terminalPromise).toBe("completed");
    expect(received).toEqual([1, 2]);
  });
});

describe("EarningsDossierView", () => {
  it("opens the original edition of a corrected bank report only on request", async () => {
    storeSession();
    const original = buildSampleDossier({ memo_markdown: "原备忘录的估值结论" });
    const current = buildSampleDossier({
      dossier_id: "new-edition", memo_markdown: "本期银行披露已更正",
      supersedes_sha256: original.dossier_sha256 ?? "",
      open_gaps: [{ gap_id: "restatement", code: "bank_disclosure_restatement",
        stage: "disclosure_review", source: original.dossier_id ?? null, message: "旧估值已撤回" }],
    });
    const fetchMock = vi.fn(async (_url: RequestInfo | URL) => jsonResponse({ dossier: original }));
    vi.stubGlobal("fetch", fetchMock);
    render(<EarningsDossierView topicId="bank" dossier={current} />);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.queryByText(original.memo_markdown)).toBeNull();
    const details = screen.getByText("查看更正前的备忘录").closest("details")!;
    details.open = true;
    fireEvent(details, new Event("toggle"));
    await waitFor(() => expect(screen.getByText(original.memo_markdown)).toBeTruthy());
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain("revision=original&original_sha256=");
  });

  it("renders all 7 buy-side sections, transparent R/Y/G diagnostics without composite scores, and Reverse-DCF 3x3 matrix", () => {
    const dossier = buildSampleDossier();
    render(<EarningsDossierView dossier={dossier} topicId="t1" />);

    // Top bar & redaction tooltip
    expect(screen.getByText("ACME Cloud Corp.")).toBeTruthy();
    expect(screen.getByTestId("sector-applicability-badge").textContent).toContain("非金融企业");
    const redactionTip = screen.getByTestId("export-redaction-tooltip");
    expect(redactionTip.textContent).toContain("fmp_consensus");
    expect(redactionTip.textContent).toContain("fmp_transcript");

    // Section 1: Guidance, Valuation & Reverse-DCF
    expect(screen.getByText("Raise (上调)")).toBeTruthy();
    expect(screen.getByText("Maintain (维持)")).toBeTruthy();
    expect(screen.getByTestId("valuation-strip").textContent).toContain("12,000");
    expect(screen.getByTestId("valuation-strip").textContent).toContain("49.57x");
    expect(screen.getByTestId("reverse-dcf-cagr").textContent).toBe("14.25%");

    // Expandable 3x3 WACC x terminal-growth sensitivity matrix
    expect(screen.queryByTestId("reverse-dcf-matrix")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /展开 WACC × 永续增速 3×3 敏感性矩阵/ }));
    const matrix = screen.getByTestId("reverse-dcf-matrix");
    expect(within(matrix).getByText("WACC = 8.00%")).toBeTruthy();
    expect(within(matrix).getByText("g = 3.50%")).toBeTruthy();
    expect(within(matrix).getByText("11.10%")).toBeTruthy();
    expect(within(matrix).getByText("17.15%")).toBeTruthy();

    // Section 2: Transparent R/Y/G Flags (NO black-box composite score)
    const flagCounts = screen.getByTestId("flag-counts");
    expect(flagCounts.textContent).toContain("红灯 1");
    expect(flagCounts.textContent).toContain("黄灯 1");
    expect(flagCounts.textContent).toContain("绿灯 1");
    expect(screen.getByText(/AR YoY growth - Revenue YoY growth > \+15\.0 pp/)).toBeTruthy();
    expect(screen.getByText(/Inspect ASC 606 Revenue Recognition/)).toBeTruthy();
    expect(screen.queryByText(/综合评分|综合得分/)).toBeNull();

    // Section 3: Multi-quarter table + EX-99.1 segment table
    expect(screen.getByRole("table", { name: "多季财务趋势表" })).toBeTruthy();
    expect(screen.getByRole("table", { name: "分部收入拆解 (Segment Revenue)" })).toBeTruthy();

    // Section 4: Cross-period Redline diffs
    const redlineCounts = screen.getByTestId("redline-counts");
    expect(redlineCounts.textContent).toContain("Added +1");
    expect(redlineCounts.textContent).toContain("Removed -1");
    expect(redlineCounts.textContent).toContain("Revised ~1");
    expect(screen.getByText(/Added disclosure regarding extended enterprise billing terms/)).toBeTruthy();
    expect(screen.getByText(/Removed legacy on-premise migration bottleneck/)).toBeTruthy();
    expect(screen.getByText(/AI GPU inference costs increased 140 bps/)).toBeTruthy();

    // Section 5: Bear cases with anchor_kind and highlighted next_quarter_invalidation_trigger
    const bearCard = screen.getByTestId("bear-case-card");
    expect(within(bearCard).getByTestId("bear-anchor-kind").textContent).toContain("flag");
    expect(within(bearCard).getByTestId("bear-invalidation-trigger").textContent).toContain(
      "Q3 FY26 DSO declines below 21 days",
    );

    // Section 6: Open gaps & slot usage
    expect(screen.getByTestId("open-gaps-callout").textContent).toContain("transcript_qa_partial");
    expect(screen.getByTestId("slot-usage-summary").textContent).toContain("Stage 0 模型调用 0 次");
    expect(screen.getByTestId("slot-usage-summary").textContent).toContain("回落主研究模型");

    // Section 7: Memo markdown rendered initially
    expect(screen.getByTestId("dossier-report-body").textContent).toContain("Q2 FY26 营收 $1,000M 超预期");
  });

  it("supports instant dual-mode switching and caches on-demand Deep Report without duplicate calls", async () => {
    const dossier = buildSampleDossier({ deep_report_markdown: null });
    const onRequestDeepReport = vi.fn(async () => "## 完整深研报告正文\n\n基于锁定底稿合成，零新增检索。");

    render(
      <EarningsDossierView
        dossier={dossier}
        topicId="t1"
        onRequestDeepReport={onRequestDeepReport}
      />,
    );

    const memoTab = screen.getByRole("tab", { name: /买方备忘录/ });
    const deepTab = screen.getByRole("tab", { name: /完整深研报告/ });
    expect(memoTab.getAttribute("aria-selected")).toBe("true");

    fireEvent.click(deepTab);
    expect(onRequestDeepReport).toHaveBeenCalledTimes(1);

    await waitFor(() => {
      expect(screen.getByTestId("dossier-report-body").textContent).toContain("完整深研报告正文");
    });

    // Switch back to Memo and then back to Deep Report -> instant, NO second request!
    fireEvent.click(memoTab);
    expect(screen.getByTestId("dossier-report-body").textContent).toContain("买方结论摘要");

    fireEvent.click(deepTab);
    expect(screen.getByTestId("dossier-report-body").textContent).toContain("完整深研报告正文");
    expect(onRequestDeepReport).toHaveBeenCalledTimes(1);
  });

  it("triggers onInspectSource on citation & bear-anchor clicks and invokes multi-format export controls", async () => {
    const dossier = buildSampleDossier();
    const onInspectSource = vi.fn();
    const onExport = vi.fn(async () => undefined);

    render(
      <EarningsDossierView
        dossier={dossier}
        topicId="t1"
        onInspectSource={onInspectSource}
        onExport={onExport}
      />,
    );

    // Click citation button on evidence card (human-readable title + location)
    fireEvent.click(screen.getByRole("button", { name: /EX-99\.1 Paragraph 3/ }));
    expect(onInspectSource).toHaveBeenCalledWith("sec_ex99_1", "EX-99.1 Paragraph 3");

    // Click anchored_evidence_id on bear case card -> resolves card_seg_1 citation
    fireEvent.click(screen.getByTitle("card_seg_1"));
    expect(onInspectSource).toHaveBeenCalledWith("sec_ex99_1", "EX-99.1 Paragraph 3");

    // Click multi-quarter financial table citation and redline source button
    fireEvent.click(screen.getByRole("button", { name: "f:rev_q2" }));
    expect(onInspectSource).toHaveBeenCalledWith("sec_10q_q2", "us-gaap:Revenues");

    fireEvent.click(screen.getByRole("button", { name: "对照原文" }));
    expect(onInspectSource).toHaveBeenCalledWith("sec_10q_q2", "redline:item_1a");

    // Expand redaction notice details
    fireEvent.click(screen.getByRole("button", { name: /导出权限脱敏说明/ }));
    expect(screen.getByTestId("export-redaction-tooltip").textContent).toContain(
      "BYOK FMP consensus and transcript are restricted from export",
    );

    // Export buttons
    fireEvent.click(screen.getByRole("button", { name: "导出 Markdown" }));
    await waitFor(() => expect(onExport).toHaveBeenCalledWith("markdown", "memo"));

    fireEvent.click(screen.getByRole("button", { name: "导出 CSV" }));
    await waitFor(() => expect(onExport).toHaveBeenCalledWith("csv", "memo"));

    fireEvent.click(screen.getByRole("button", { name: "导出 Excel (.xlsx)" }));
    await waitFor(() => expect(onExport).toHaveBeenCalledWith("xlsx", "memo"));
  });

  it("handles Deep Report error + retry and fallback export download without injecting <style>", async () => {
    storeSession();
    const stylesBefore = document.querySelectorAll("style").length;
    const dossier = buildSampleDossier({ deep_report_markdown: null });
    let failFirst = true;
    const onRequestDeepReport = vi.fn(async () => {
      if (failFirst) {
        failFirst = false;
        throw new Error("Writer slot temporary timeout");
      }
      return "## 重试成功深研报告";
    });

    const fetchMock = vi.fn(async () =>
      new Response("col1,col2\n1,2\n", {
        status: 200,
        headers: {
          "Content-Type": "text/csv; charset=utf-8",
          "Content-Disposition": 'attachment; filename="ACME.csv"',
          "X-Dossier-Id": "dossier_acme_q2",
          "X-Export-Redacted": "true",
        },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<EarningsDossierView dossier={dossier} topicId="t1" onRequestDeepReport={onRequestDeepReport} />);
    expect(document.querySelectorAll("style").length).toBe(stylesBefore);

    fireEvent.click(screen.getByRole("tab", { name: /完整深研报告/ }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("Writer slot temporary timeout"));

    fireEvent.click(screen.getByRole("button", { name: "重试生成完整深研报告" }));
    await waitFor(() => expect(screen.getByTestId("dossier-report-body").textContent).toContain("重试成功深研报告"));
    expect(onRequestDeepReport).toHaveBeenCalledTimes(2);

    // Fallback export download when onExport prop is omitted
    fireEvent.click(screen.getByRole("button", { name: "导出 CSV" }));
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("已下载 ACME.csv"));
  });

  it("gracefully displays Sector Gate notice (Bank SIC 6021) and unavailable Reverse-DCF reason (FCF <= 0)", () => {
    const base = buildSampleDossier();
    const bankSector: ResearchV2.SectorApplicability = {
      sic_code: "6021",
      sic_description: "National Commercial Banks",
      sector_category: "bank",
      is_gated: true,
      disabled_metrics: ["gross_margin", "ev_to_ebitda", "reverse_dcf"],
      recommended_alternatives: ["Net Interest Margin (NIM)", "CET1 Capital Ratio", "P/TBV"],
      explanation: "SEC SIC 6021 falls in Banking/Credit Institutions (6000-6199); standard industrial FCF and EV/EBITDA are disabled.",
    };
    const bankDossier = buildSampleDossier({
      symbol: "JPM",
      company_name: "JPMorgan Chase & Co.",
      selected_l2_pack_id: null,
      sector_applicability: bankSector,
      snapshot: {
        ...base.snapshot,
        symbol: "JPM",
        company_name: "JPMorgan Chase & Co.",
        sic_code: "6021",
        sector_applicability: bankSector,
        flags: [],
        diagnostics: {
          ...base.snapshot.diagnostics!,
          sector_applicability: bankSector,
          flags: [],
          flag_counts: { red: 0, yellow: 0, green: 0 },
        },
        valuation: {
          ...base.snapshot.valuation!,
          sector_applicability: bankSector,
          ev_to_ebitda: {
            name: "ev_to_ebitda",
            status: "disabled_by_sector",
            value: null,
            unit: "multiple",
            formula: "ev / ttm_ebitda",
            reason: bankSector.explanation,
          },
          reverse_dcf: {
            status: "unavailable",
            reason: "TTM FCF <= 0 (-150.00); Reverse-DCF cannot compound non-positive base FCF.",
            terminal_method: "gordon_growth",
            forecast_years: 5,
            wacc_pct: "9.00",
            terminal_growth_pct: "3.00",
            formula: "Gordon growth",
            sensitivity_matrix: [],
          },
        },
      },
    });

    render(<EarningsDossierView dossier={bankDossier} topicId="t_jpm" />);
    expect(screen.getByTestId("sector-applicability-badge").textContent).toContain("银行");
    const gateNotice = screen.getByTestId("sector-gate-notice");
    expect(gateNotice.textContent).toContain("SIC 6021");
    expect(gateNotice.textContent).toContain("Net Interest Margin (NIM)");
    expect(screen.getByTestId("reverse-dcf-unavailable").textContent).toContain("TTM FCF <= 0 (-150.00)");
  });
});
