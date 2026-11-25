"""Offline unit & edge-case tests for V2.5 Governance, Capital Allocation & Guidance Credibility Engine."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.research_governance_audit import (
    Def14aFactSnapshot,
    GuidanceCredibilityLedger,
    GuidanceCredibilityRecord,
    assemble_governance_audit_report,
    backfill_guidance_from_8k_releases,
    compute_capital_allocation_audit,
    evaluate_guidance_credibility,
    evaluate_incentive_governance,
    extract_guidance_records_from_dossier,
    generate_cda_attribution_brief,
    load_v25_governance_rules,
    parse_def14a_ixbrl_xml,
    upsert_guidance_ledger_records,
)
from app.research_peer_compare import project_companyfacts_to_snapshot


def _make_annual_cf(
    *,
    shares_rows: list[dict[str, Any]],
    sbc_vals: list[tuple[str, str, float, str]],
    buyback_vals: list[tuple[str, str, float, str]],
    ocf_vals: list[tuple[str, str, float, str]],
    capex_vals: list[tuple[str, str, float, str]],
    rev_vals: list[tuple[str, str, float, str]] | None = None,
    div_vals: list[tuple[str, str, float, str]] | None = None,
    unrec_sbc: float | None = 5_000_000_000,
    tax_wh: float | None = 800_000_000,
) -> dict[str, Any]:
    def _rows(lst: list[tuple[str, str, float, str]]) -> list[dict[str, Any]]:
        return [
            {"start": s, "end": e, "val": v, "filed": f, "form": "10-K", "accn": f"acc-{e[:4]}"}
            for s, e, v, f in lst
        ]

    gaap: dict[str, Any] = {
        "WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": shares_rows}},
        "ShareBasedCompensation": {"units": {"USD": _rows(sbc_vals)}},
        "PaymentsForRepurchaseOfCommonStock": {"units": {"USD": _rows(buyback_vals)}},
        "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": _rows(ocf_vals)}},
        "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": _rows(capex_vals)}},
    }
    if rev_vals:
        gaap["Revenues"] = {"units": {"USD": _rows(rev_vals)}}
    if div_vals:
        gaap["PaymentsOfDividendsCommonStock"] = {"units": {"USD": _rows(div_vals)}}
    if unrec_sbc is not None:
        gaap["EmployeeServiceShareBasedCompensationNonvestedAwardsTotalCompensationCostNotYetRecognized"] = {
            "units": {"USD": [{"end": sbc_vals[-1][1], "val": unrec_sbc, "filed": sbc_vals[-1][3], "form": "10-K"}]}
        }
    if tax_wh is not None:
        gaap["PaymentsRelatedToTaxWithholdingForShareBasedCompensation"] = {
            "units": {"USD": [{"start": sbc_vals[-1][0], "end": sbc_vals[-1][1], "val": tax_wh, "filed": sbc_vals[-1][3], "form": "10-K"}]}
        }
    return {"facts": {"us-gaap": gaap}}


def test_pillar1_chained_non_integer_split_and_fake_buyback_and_sign_norm() -> None:
    """Verify arbitrary-ratio (3:2) chained split adjustment, IFRS negative outflow sign norm, and fake buyback flag."""
    # Older 10-K (filed 2024-02-15) reports FY2022 & FY2023 pre-split (1.00B, 1.02B)
    # Newer 10-K (filed 2026-02-15) reports FY2023..FY2025 post 3:2 split (1.53B, 1.58B, 1.65B)
    # Chain factor on FY2023 overlap = 1.53B / 1.02B = 1.5000x -> FY2022 adjusted = 1.50B
    # 3Y net dilution (FY2022->FY2025) = (1.65 - 1.50) / 1.50 = +10.00% despite $2B buybacks -> fake_buyback!
    shares_rows = [
        {"start": "2022-01-01", "end": "2022-12-31", "val": 1_000_000_000, "filed": "2024-02-15", "form": "10-K", "accn": "old-10k"},
        {"start": "2023-01-01", "end": "2023-12-31", "val": 1_020_000_000, "filed": "2024-02-15", "form": "10-K", "accn": "old-10k"},
        {"start": "2023-01-01", "end": "2023-12-31", "val": 1_530_000_000, "filed": "2026-02-15", "form": "10-K", "accn": "new-10k"},
        {"start": "2024-01-01", "end": "2024-12-31", "val": 1_580_000_000, "filed": "2026-02-15", "form": "10-K", "accn": "new-10k"},
        {"start": "2025-01-01", "end": "2025-12-31", "val": 1_650_000_000, "filed": "2026-02-15", "form": "10-K", "accn": "new-10k"},
    ]
    yrs = [
        ("2022-01-01", "2022-12-31", "2023-02-15"),
        ("2023-01-01", "2023-12-31", "2024-02-15"),
        ("2024-01-01", "2024-12-31", "2025-02-15"),
        ("2025-01-01", "2025-12-31", "2026-02-15"),
    ]
    cf = _make_annual_cf(
        shares_rows=shares_rows,
        sbc_vals=[(s, e, 3_000_000_000, f) for s, e, f in yrs],
        buyback_vals=[(s, e, -2_000_000_000, f) for s, e, f in yrs],  # negative outflow sign normalized!
        ocf_vals=[(s, e, 12_000_000_000, f) for s, e, f in yrs],
        capex_vals=[(s, e, 2_000_000_000, f) for s, e, f in yrs],    # FCF = 10B -> SBC/FCF = 30.0%
        rev_vals=[(s, e, 40_000_000_000, f) for s, e, f in yrs],
    )
    snap = project_companyfacts_to_snapshot(
        cik="0001234567", symbol="SPLITCO", company_name="SplitCo", sic="7372", sic_description="Software",
        latest_periodic_accession="new-10k", latest_periodic_filed_at="2026-02-15", companyfacts_doc=cf,
    )
    assert snap.projection_version == 3
    rules, _sha, _comp = load_v25_governance_rules()
    ca = compute_capital_allocation_audit(snap, rules=rules, price_usd=Decimal("100.00"))
    assert any("1.5000x" in n for n in ca.split_adjustments_applied)
    assert ca.net_diluted_share_change_3y_pct == "10.00"
    assert ca.buybacks_ttm_usd == "2000000000"  # sign normalized to positive
    assert ca.sbc_pct_of_fcf == "30.0"
    assert ca.fake_buyback_flag is True
    assert ca.quality_light == "red"
    assert ca.true_net_shareholder_yield_pct is not None


def test_pillar2_def14a_xml_dimensions_multi_peo_xxe_and_anchor_downgrade() -> None:
    """Verify *_htm.xml XBRL dimension parsing, dirty label cleanup, XXE rejection, anchor downgrade & Founder-CEO exemption."""
    with pytest.raises(ValueError, match="XXE"):
        parse_def14a_ixbrl_xml(
            b'<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe "bad">]><xbrl/>',
            cik="0000200406", symbol="JNJ", accession="acc1", filed_at="2026-03-15",
        )

    xml_prior = b"""<?xml version="1.0" encoding="UTF-8"?>
    <xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
                xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
                xmlns:ecd="http://xbrl.sec.gov/ecd/2023">
      <xbrli:context id="c2024_peo1">
        <xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000200406</xbrli:identifier>
          <xbrli:segment><xbrldi:explicitMember dimension="ecd:IndividualAxis">jnj:CeoOneMember</xbrldi:explicitMember></xbrli:segment>
        </xbrli:entity>
        <xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate><xbrli:endDate>2024-12-31</xbrli:endDate></xbrli:period>
      </xbrli:context>
      <ecd:CoSelectedMeasureName contextRef="c2024_peo1"> "Return on Invested Capital (ROIC)" </ecd:CoSelectedMeasureName>
      <ecd:PeoTotalCompAmt contextRef="c2024_peo1">24000000</ecd:PeoTotalCompAmt>
      <ecd:PeoActuallyPaidCompAmt contextRef="c2024_peo1">26000000</ecd:PeoActuallyPaidCompAmt>
    </xbrli:xbrl>"""

    xml_latest = b"""<?xml version="1.0" encoding="UTF-8"?>
    <xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
                xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
                xmlns:ecd="http://xbrl.sec.gov/ecd/2023">
      <xbrli:context id="c2025_peo1">
        <xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000200406</xbrli:identifier>
          <xbrli:segment><xbrldi:explicitMember dimension="ecd:IndividualAxis">jnj:CeoOneMember</xbrldi:explicitMember></xbrli:segment>
        </xbrli:entity>
        <xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate></xbrli:period>
      </xbrli:context>
      <xbrli:context id="c2025_peo2">
        <xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000200406</xbrli:identifier>
          <xbrli:segment><xbrldi:explicitMember dimension="ecd:IndividualAxis">jnj:CeoTwoMember</xbrldi:explicitMember></xbrli:segment>
        </xbrli:entity>
        <xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate></xbrli:period>
      </xbrli:context>
      <ecd:CoSelectedMeasureName contextRef="c2025_peo1">&#8226; Adjusted EBITDA &#8226;</ecd:CoSelectedMeasureName>
      <ecd:PeoTotalCompAmt contextRef="c2025_peo1">28000000</ecd:PeoTotalCompAmt>
      <ecd:PeoActuallyPaidCompAmt contextRef="c2025_peo1">35000000</ecd:PeoActuallyPaidCompAmt>
      <ecd:PeoTotalCompAmt contextRef="c2025_peo2">12000000</ecd:PeoTotalCompAmt>
      <ecd:InsiderTrdPoliciesProcAdoptedFlag contextRef="c2025_peo1">true</ecd:InsiderTrdPoliciesProcAdoptedFlag>
      <ecd:MnpiDiscTimedForCompValFlag contextRef="c2025_peo1">true</ecd:MnpiDiscTimedForCompValFlag>
    </xbrli:xbrl>"""

    s_prior = parse_def14a_ixbrl_xml(xml_prior, cik="0000200406", symbol="JNJ", accession="acc-24", filed_at="2025-03-15")
    s_latest = parse_def14a_ixbrl_xml(xml_latest, cik="0000200406", symbol="JNJ", accession="acc-25", filed_at="2026-03-15")
    assert len(s_latest.pvp_rows) == 2  # multi-PEO separated cleanly by IndividualAxis!
    assert s_latest.company_selected_measure == "Adjusted EBITDA"

    rules, _sha, comp = load_v25_governance_rules()
    inc = evaluate_incentive_governance(s_latest, s_prior, rules=rules, compiled_anchors=comp)
    assert inc.highest_anchor_tier == "weak_adjusted"
    assert inc.anchor_downgraded_yoy is True
    assert inc.mnpi_red_flag is True
    assert inc.quality_light == "red"

    # Founder-CEO exemption (<$2M comp)
    xml_founder = b"""<?xml version="1.0" encoding="UTF-8"?>
    <xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:ecd="http://xbrl.sec.gov/ecd/2023">
      <xbrli:context id="c2025"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0001326801</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate></xbrli:period>
      </xbrli:context>
      <ecd:CoSelectedMeasureName contextRef="c2025">Operating Income</ecd:CoSelectedMeasureName>
      <ecd:PeoTotalCompAmt contextRef="c2025">1</ecd:PeoTotalCompAmt>
      <ecd:PeoActuallyPaidCompAmt contextRef="c2025">1</ecd:PeoActuallyPaidCompAmt>
    </xbrli:xbrl>"""
    s_f = parse_def14a_ixbrl_xml(xml_founder, cik="0001326801", symbol="META", accession="acc-meta", filed_at="2026-04-15")
    inc_f = evaluate_incentive_governance(s_f, None, rules=rules, compiled_anchors=comp)
    assert inc_f.founder_ceo_exempt is True
    assert inc_f.pay_tsr_divergent is False


def test_pillar3_guidance_credibility_basis_lock_and_rule_change_zero_rebuild(tmp_path: Path) -> None:
    """Verify Track A extraction, strict basis lock, sandbagger profile, and instant rule-change zero-rebuild."""
    dossier = {
        "generated_at": "2026-08-28T00:00:00Z",
        "snapshot": {
            "period_label": "Q2 FY27",
            "guidance_nongaap": {
                "period_label": "Q2 FY27",
                "guidance_evaluations": [
                    {
                        "metric_name": "revenue",
                        "basis": "gaap",
                        "actual_value": "35000",
                        "vs_prior_guidance": {"prior_low": "32000", "prior_mid": "32500", "prior_high": "33000"},
                    }
                ],
            },
        },
    }
    extracted = extract_guidance_records_from_dossier(dossier, source_accession="acc-q2")
    assert len(extracted) == 1 and extracted[0].basis == "gaap" and extracted[0].guidance_mid == "32500"

    # Build 4 quarters: 3 gaap beats (+5% each -> chronic_sandbagger) + 1 gaap vs non_gaap mismatch (unscored)
    ledger = GuidanceCredibilityLedger(
        cik="0001045810",
        symbol="NVDA",
        records=[
            GuidanceCredibilityRecord(period_label="Q1", metric_key="revenue", basis="gaap", guidance_mid="100", actual_value="105", actual_basis="gaap", disclosed_at="2025-08-01"),
            GuidanceCredibilityRecord(period_label="Q2", metric_key="revenue", basis="gaap", guidance_mid="110", actual_value="116", actual_basis="gaap", disclosed_at="2025-11-01"),
            GuidanceCredibilityRecord(period_label="Q3", metric_key="revenue", basis="gaap", guidance_mid="120", actual_value="126", actual_basis="gaap", disclosed_at="2026-02-01"),
            GuidanceCredibilityRecord(period_label="Q4", metric_key="eps", basis="gaap", guidance_mid="1.00", actual_value="1.30", actual_basis="non_gaap", disclosed_at="2026-05-01"),
        ],
    )
    rules, _sha, _comp = load_v25_governance_rules()
    gc = evaluate_guidance_credibility(ledger, rules=rules)
    assert gc.scored_quarters == 3
    assert gc.profile == "chronic_sandbagger"
    assert any(r.verdict == "basis_mismatch_unscored" for r in gc.rows)

    # Rule-change zero-rebuild test: same snapshot, tighten sbc_fcf_red_pct from 40% -> 25%
    yrs = [("2025-01-01", "2025-12-31", "2026-02-15")]
    cf = _make_annual_cf(
        shares_rows=[{"start": "2025-01-01", "end": "2025-12-31", "val": 1_000_000_000, "filed": "2026-02-15", "form": "10-K", "accn": "a1"}],
        sbc_vals=[(s, e, 3_000_000_000, f) for s, e, f in yrs],
        buyback_vals=[(s, e, 1_000_000_000, f) for s, e, f in yrs],
        ocf_vals=[(s, e, 12_000_000_000, f) for s, e, f in yrs],
        capex_vals=[(s, e, 2_000_000_000, f) for s, e, f in yrs],  # SBC/FCF = 30%
    )
    snap = project_companyfacts_to_snapshot(
        cik="0001045810", symbol="NVDA", company_name="NVIDIA", sic="3674", sic_description="Semis",
        latest_periodic_accession="a1", latest_periodic_filed_at="2026-02-15", companyfacts_doc=cf,
    )
    rep_default = assemble_governance_audit_report(snap=snap, def14a_latest=None, def14a_prior=None, guidance_ledger=ledger)
    assert rep_default.capital_allocation.quality_light == "yellow"  # 30% between 20% and 40%

    custom_rules = json.loads(Path("app/fin_data/adapters/v25_governance_rules.json").read_text(encoding="utf-8"))
    custom_rules["capital_allocation"]["sbc_fcf_red_pct"] = 25.0
    custom_path = tmp_path / "tight_rules.json"
    custom_path.write_text(json.dumps(custom_rules), encoding="utf-8")

    rep_tight = assemble_governance_audit_report(
        snap=snap, def14a_latest=None, def14a_prior=None, guidance_ledger=ledger, rules_path=custom_path
    )
    assert rep_tight.capital_allocation.quality_light == "red"
    assert rep_tight.rules_sha256 != rep_default.rules_sha256


def test_layer2_cda_brief_and_8k_backfill_verification() -> None:
    """Verify Layer 2 CD&A numeric + verbatim quote gates and 8-K backfill quote verification."""
    yrs = [("2025-01-01", "2025-12-31", "2026-02-15")]
    cf = _make_annual_cf(
        shares_rows=[{"start": "2025-01-01", "end": "2025-12-31", "val": 1_000_000_000, "filed": "2026-02-15", "form": "10-K", "accn": "a1"}],
        sbc_vals=[(s, e, 1_000_000_000, f) for s, e, f in yrs],
        buyback_vals=[(s, e, 5_000_000_000, f) for s, e, f in yrs],
        ocf_vals=[(s, e, 22_000_000_000, f) for s, e, f in yrs],
        capex_vals=[(s, e, 2_000_000_000, f) for s, e, f in yrs],  # SBC/FCF = 5.0%
    )
    snap = project_companyfacts_to_snapshot(
        cik="0001045810", symbol="NVDA", company_name="NVIDIA", sic="3674", sic_description="Semis",
        latest_periodic_accession="a1", latest_periodic_filed_at="2026-02-15", companyfacts_doc=cf,
    )
    rep = assemble_governance_audit_report(snap=snap, def14a_latest=None, def14a_prior=None, guidance_ledger=None)
    cda_text = "Executive Summary: Our Compensation Committee ties 75% of executive PSU awards to 3-year ROIC and FCF."

    class _FakeCdaRunner:
        def invoke(self, _req: Any) -> Any:
            return SimpleNamespace(
                content=json.dumps({
                    "summary": "Strong alignment with ROIC and low SBC burden.",
                    "incentive_alignment_assessment": "PSU awards tie 75% to ROIC and FCF.",
                    "capital_allocation_verdict": "Buybacks exceed SBC by 5x.",
                    "skeptic_counterview": "High absolute comp could still dilute if FCF normalizes.",
                    "cited_metrics": [
                        {"metric_key": "sbc_pct_of_fcf", "value": "5.0"},
                        {"metric_key": "sbc_pct_of_fcf", "value": "99.9"},
                    ],
                    "cited_quotes": [
                        {"quotation": "Our Compensation Committee ties 75% of executive PSU awards to 3-year ROIC and FCF."},
                        {"quotation": "Totally fabricated quote not in filing."},
                    ],
                }),
                usage={"total_tokens": 777},
            )

    brief = generate_cda_attribution_brief(rep, cda_excerpt=cda_text, runner=_FakeCdaRunner(), model_name="fake-glm")
    assert brief.verified_metric_count == 1 and brief.total_cited_metric_count == 2
    assert brief.verified_quote_count == 1 and brief.total_cited_quote_count == 2
    assert len(brief.open_gaps) == 2

    # Track B 8-K backfill
    ex_text = "Outlook for Q3 FY26: Revenue is expected to be $32.5 billion, plus or minus 2% on a GAAP basis."

    class _FakeBackfillRunner:
        def invoke(self, _req: Any) -> Any:
            return SimpleNamespace(
                content=json.dumps({
                    "records": [
                        {
                            "accession": "0001045810-25-000100",
                            "period_label": "Q3 FY26",
                            "metric_key": "revenue_usd_b",
                            "basis": "gaap",
                            "guidance_mid": "32.5",
                            "verbatim_quote": "Revenue is expected to be $32.5 billion, plus or minus 2% on a GAAP basis.",
                        },
                        {
                            "accession": "0001045810-25-000100",
                            "period_label": "Q3 FY26",
                            "metric_key": "eps",
                            "basis": "non_gaap",
                            "guidance_mid": "9.99",
                            "verbatim_quote": "Hallucinated EPS guidance line.",
                        },
                    ]
                }),
                usage={"total_tokens": 444},
            )

    recs, toks, gaps = backfill_guidance_from_8k_releases(
        cik="0001045810",
        symbol="NVDA",
        releases=[("0001045810-25-000100", "2025-08-28", ex_text)],
        runner=_FakeBackfillRunner(),
    )
    assert len(recs) == 1 and recs[0].quote_verified is True
    assert toks == 444 and len(gaps) == 1
